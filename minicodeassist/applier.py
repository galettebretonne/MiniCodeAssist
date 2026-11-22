"""Apply SEARCH/REPLACE blocks with backups, tolerant matching and undo."""

from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .edits import EditBlock
from .gitutils import exclude_mca

MANIFEST = "manifest.json"
BACKUPS = "backup"


@dataclass
class Rejection:
    """A block that could not be applied."""

    path: str
    line: int
    reason: str
    hint: str = ""


@dataclass
class ApplyResult:
    """Summary of one apply pass."""

    applied: list[str] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)
    created: list[str] = field(default_factory=list)
    written: list[str] = field(default_factory=list)
    backup_dir: Path | None = None

    @property
    def ok(self) -> bool:
        return not self.rejected


@dataclass(frozen=True)
class Match:
    """A unique location of a SEARCH text and its adapted replacement."""

    start: int
    end: int
    tier: str
    replacement: str


class AmbiguousMatch(Exception):
    """The SEARCH text matches several places."""

    def __init__(self, lines: list[int]):
        self.lines = lines
        super().__init__(", ".join(str(line) for line in lines[:8]))


def _decode(raw: bytes) -> tuple[str, str]:
    """Return (text with \\n newlines, original newline style)."""
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return text, newline


def _encode(text: str, newline: str) -> bytes:
    return text.replace("\n", newline).encode("utf-8")


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _indent_prefix(lines: Iterable[str]) -> str:
    candidates = [_indent_of(line) for line in lines if line.strip()]
    if not candidates:
        return ""
    return candidates[0][: min(len(candidate) for candidate in candidates)]


def _has_content(lines: Iterable[str]) -> bool:
    return any(line.strip() for line in lines)


def _logical_lines(text: str) -> list[str]:
    lines = text.split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _dedent_lines(lines: list[str]) -> list[str]:
    """Remove the first line's indent and the common indent of the rest."""
    if not lines:
        return []
    first_prefix = _indent_prefix(lines[:1])
    rest_prefix = _indent_prefix(lines[1:])
    result = [lines[0][len(first_prefix) :]]
    for line in lines[1:]:
        result.append(line[len(rest_prefix) :] if line.strip() else "")
    return result


def _line_no(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _tolerant_regex(search: str) -> re.Pattern[str]:
    """Match ignoring trailing whitespace, but keeping the indentation shape."""
    lines = _logical_lines(search)
    first_indent = _indent_prefix(lines[:1])
    pattern = r"^(?P<indent>[ \t]*)" + re.escape(lines[0].lstrip()) + r"[ \t]*"
    for line in lines[1:]:
        indent = _indent_of(line)
        relative = indent[len(first_indent) :] if indent.startswith(first_indent) else indent
        pattern += r"\n(?P=indent)" + re.escape(relative) + re.escape(line.lstrip()) + r"[ \t]*"
    return re.compile(pattern + r"(?=\n|$)", re.MULTILINE)


def _hints(text: str, search: str) -> str:
    needle = "\n".join(_dedent_lines(_logical_lines(search)))
    lines = text.split("\n")
    count = len(_logical_lines(search))
    scored: list[tuple[float, int, str]] = []
    for index in range(max(0, len(lines) - count + 1)):
        window = lines[index : index + count]
        candidate = "\n".join(_dedent_lines(window))
        ratio = difflib.SequenceMatcher(None, needle, candidate, autojunk=False).ratio()
        scored.append((ratio, index + 1, "\n".join(window)))
    scored.sort(key=lambda item: item[0], reverse=True)
    best = [item for item in scored[:3] if item[0] >= 0.55]
    if not best:
        return ""
    parts = [f"line {line_no} ({ratio:.0%} similar):\n{sample}" for ratio, line_no, sample in best]
    return "Closest lines:\n" + "\n---\n".join(parts)


def _shift(line: str, search_prefix: str, original_prefix: str) -> str:
    """Move a replacement line from the SEARCH indentation to the file's."""
    if search_prefix and line.startswith(search_prefix):
        return original_prefix + line[len(search_prefix) :]
    indent = _indent_of(line)
    if search_prefix.startswith(indent):
        # The line is less indented than SEARCH: dedent the file's prefix as much.
        removed = len(search_prefix) - len(indent)
        kept = original_prefix[: max(0, len(original_prefix) - removed)]
        return kept + line[len(indent) :]
    return original_prefix + line


def _reindent(replacement: str, search: str, original: str) -> str:
    """Carry the matched indentation over to the replacement lines.

    The first line and the following lines may be shifted differently, because
    the indent tier tolerates a SEARCH whose first line lost its indentation.
    When SEARCH has a single line, the same shift is used for every line.
    """
    search_lines = _logical_lines(search)
    original_lines = _logical_lines(original)
    search_first = _indent_prefix(search_lines[:1])
    original_first = _indent_prefix(original_lines[:1])
    if _has_content(search_lines[1:]) and _has_content(original_lines[1:]):
        search_rest = _indent_prefix(search_lines[1:])
        original_rest = _indent_prefix(original_lines[1:])
    else:
        search_rest, original_rest = search_first, original_first
    result: list[str] = []
    for index, line in enumerate(_logical_lines(replacement)):
        if not line.strip():
            result.append("")
        elif index == 0:
            result.append(_shift(line, search_first, original_first))
        else:
            result.append(_shift(line, search_rest, original_rest))
    trailing = "\n" if replacement.endswith("\n") else ""
    return "\n".join(result) + trailing


def _exact_matches(text: str, search: str) -> list[int]:
    """Start offsets of occurrences of search that cover whole lines only."""
    starts: list[int] = []
    position = text.find(search)
    while position != -1:
        end = position + len(search)
        starts_line = position == 0 or text[position - 1] == "\n"
        ends_line = search.endswith("\n") or end == len(text) or text[end] == "\n"
        if starts_line and ends_line:
            starts.append(position)
        position = text.find(search, position + 1)
    return starts


def _find_match(text: str, search: str, replacement: str) -> Match | None:
    """Return the unique match of search, None if absent.

    Raises AmbiguousMatch when the first tier that matches finds several places.
    """
    if not search.strip():
        return None

    exact = _exact_matches(text, search)
    if len(exact) == 1:
        return Match(exact[0], exact[0] + len(search), "exact", replacement)
    if len(exact) > 1:
        raise AmbiguousMatch([_line_no(text, start) for start in exact])

    matches = list(_tolerant_regex(search).finditer(text))
    if len(matches) == 1:
        match = matches[0]
        end = match.end()
        if search.endswith("\n") and end < len(text) and text[end] == "\n":
            end += 1
        return Match(match.start(), end, "whitespace", _reindent(replacement, search, match.group(0)))
    if len(matches) > 1:
        raise AmbiguousMatch([_line_no(text, match.start()) for match in matches])

    needle = _dedent_lines(_logical_lines(search))
    lines = text.split("\n")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)
    count = len(needle)
    found: list[Match] = []
    for index in range(max(0, len(lines) - count + 1)):
        window = lines[index : index + count]
        if _dedent_lines(window) == needle:
            last = index + count - 1
            end = offsets[last] + len(lines[last])
            if search.endswith("\n") and end < len(text):
                end += 1
            adapted = _reindent(replacement, search, "\n".join(window))
            found.append(Match(offsets[index], end, "indent", adapted))
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise AmbiguousMatch([_line_no(text, item.start) for item in found])
    return None


def _backup_dir(repo: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = repo / ".mca" / BACKUPS / stamp
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class _Pending:
    """The new content of one file, computed before anything is written."""

    path: str
    target: Path
    original: bytes | None
    text: str
    newline: str
    applied: list[str]


def _plan_file(
    repo: Path, path_text: str, edits: list[EditBlock], result: ApplyResult
) -> _Pending | None:
    """Apply all blocks of one file in memory; None if any block is rejected."""
    target = repo / Path(path_text)
    first_line = edits[0].line
    if not target.resolve().is_relative_to(repo.resolve()):
        result.rejected.append(Rejection(path_text, first_line, "path outside the repository"))
        return None
    if target.exists() and not target.is_file():
        result.rejected.append(Rejection(path_text, first_line, "path is not a regular file"))
        return None

    original: bytes | None = None
    text, newline = "", "\n"
    if target.exists():
        try:
            original = target.read_bytes()
            text, newline = _decode(original)
        except UnicodeDecodeError:
            result.rejected.append(
                Rejection(path_text, first_line, "file is not UTF-8 text; edit it by hand")
            )
            return None
        except OSError as error:
            result.rejected.append(Rejection(path_text, first_line, f"cannot read file: {error}"))
            return None

    exists = original is not None
    applied: list[str] = []
    rejected: list[Rejection] = []
    for block in edits:
        if not block.search.strip():
            if exists:
                rejected.append(
                    Rejection(path_text, block.line, "file already exists; use SEARCH to modify it")
                )
                continue
            text = block.replace if block.replace.endswith("\n") or not block.replace else block.replace + "\n"
            exists = True
            applied.append(f"{path_text} (created)")
            continue

        try:
            match = _find_match(text, block.search, block.replace)
        except AmbiguousMatch as ambiguous:
            rejected.append(
                Rejection(path_text, block.line, "SEARCH text is ambiguous", f"Matches at lines: {ambiguous}")
            )
            continue
        if match is None:
            rejected.append(
                Rejection(path_text, block.line, "SEARCH text not found", _hints(text, block.search))
            )
            continue
        text = text[: match.start] + match.replacement + text[match.end :]
        applied.append(f"{path_text} (line {_line_no(text, match.start)}, {match.tier} match)")

    if rejected:
        result.rejected.extend(rejected)
        if applied:
            result.rejected.append(
                Rejection(
                    path_text,
                    first_line,
                    f"file left unchanged: {len(applied)} other block(s) for this file were not applied",
                    "Resend every block for this file once the rejected ones are fixed.",
                )
            )
        return None
    return _Pending(path_text, target, original, text, newline, applied)


def apply_edits(repo: Path, blocks: list[EditBlock]) -> ApplyResult:
    """Apply all blocks, all-or-nothing per file, after backing up touched files."""
    repo = Path(repo)
    result = ApplyResult()
    exclude_mca(repo)

    grouped: dict[str, list[EditBlock]] = {}
    for block in blocks:
        grouped.setdefault(block.path, []).append(block)

    pending = [
        item
        for path_text, edits in grouped.items()
        if (item := _plan_file(repo, path_text, edits, result)) is not None
    ]
    if not pending:
        return result

    # Back up first: nothing in the repository changes until the backup is complete.
    backup = _backup_dir(repo)
    result.backup_dir = backup
    for item in pending:
        if item.original is not None:
            destination = backup / item.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(item.original)
    manifest = {
        "created": [item.path for item in pending if item.original is None],
        "saved": [item.path for item in pending if item.original is not None],
        "undone": False,
    }
    (backup / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    for item in pending:
        try:
            item.target.parent.mkdir(parents=True, exist_ok=True)
            item.target.write_bytes(_encode(item.text, item.newline))
        except OSError as error:
            result.rejected.append(
                Rejection(item.path, 0, f"write failed: {error}", "Run --undo to restore the previous state.")
            )
            continue
        result.applied.extend(item.applied)
        result.written.append(item.path)
        if item.original is None:
            result.created.append(item.path)
    return result


def undo_last(repo: Path) -> str:
    """Restore the most recent backed-up state."""
    repo = Path(repo)
    root = repo / ".mca" / BACKUPS
    if not root.is_dir():
        return "Nothing to undo: no backups found."
    candidates = []
    for child in root.iterdir():
        manifest_path = child / MANIFEST
        if child.is_dir() and manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not manifest.get("undone"):
                candidates.append((child.name, child, manifest))
    if not candidates:
        return "Nothing to undo: all backups have already been restored."

    _name, backup, manifest = max(candidates, key=lambda item: item[0])
    restored: list[str] = []
    # "saved_empty" comes from backups written by older versions.
    for path_text in manifest.get("created", []) + manifest.get("saved_empty", []):
        target = repo / path_text
        if target.is_file():
            target.unlink()
            restored.append(f"removed {path_text}")
    for path_text in manifest.get("saved", []):
        source = backup / path_text
        if source.is_file():
            target = repo / path_text
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            restored.append(f"restored {path_text}")
        else:
            restored.append(f"missing backup for {path_text}")
    manifest["undone"] = True
    (backup / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return "Undo:\n" + "\n".join(restored or ["(nothing to restore)"])
