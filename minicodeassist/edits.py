"""Parsing of the SEARCH/REPLACE edit format used by the Gem."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

FILE_PREFIX = "### FILE:"
SEARCH_MARK = "<<<<<<< SEARCH"
SPLIT_MARK = "======="
REPLACE_MARK = ">>>>>>> REPLACE"
_FENCE_OPEN = re.compile(r"^```[A-Za-z0-9_+-]*$")
_FENCE_CLOSE = "```"
# Files whose content legitimately starts or ends with a Markdown fence.
_PROSE_SUFFIXES = {".md", ".markdown", ".rst", ".txt"}
_PROTECTED = {".git", ".mca"}


class ParseError(ValueError):
    """The assistant response is not a valid edit block."""


@dataclass(frozen=True)
class EditBlock:
    """One file edit. An empty search means 'create this file'.

    line is the line of the SEARCH marker in the assistant response.
    """

    path: str
    search: str
    replace: str
    line: int


def _protected_key(part: str) -> str:
    # Windows filesystems ignore case, trailing dots/spaces and ":stream" suffixes.
    return part.split(":", 1)[0].rstrip(". ").lower()


def validate_path(path: str) -> str:
    """Normalise a relative repo path and reject escapes and protected folders."""
    cleaned = path.strip().replace("\\", "/")
    if not cleaned:
        raise ParseError("empty file path")
    if re.match(r"^[A-Za-z]:", cleaned) or cleaned.startswith("/"):
        raise ParseError(f"path outside the repository: {cleaned}")
    candidate = PurePosixPath(cleaned)
    if any(part == ".." for part in candidate.parts):
        raise ParseError(f"path outside the repository: {cleaned}")
    if not candidate.parts:
        raise ParseError(f"not a file path: {cleaned}")
    if any(_protected_key(part) in _PROTECTED for part in candidate.parts):
        raise ParseError(f"refusing to edit .git/ or MiniCodeAssist internals (.mca/): {cleaned}")
    return candidate.as_posix()


def _strip_fences(lines: list[str], path: str) -> list[str]:
    """Remove one Markdown fence pair wrapped around a whole SEARCH/REPLACE part.

    Fences inside the content are kept, and prose files are never touched,
    because their content may itself start or end with a code block.
    """
    if PurePosixPath(path).suffix.lower() in _PROSE_SUFFIXES:
        return lines
    filled = [index for index, line in enumerate(lines) if line.strip()]
    if len(filled) < 2:
        return lines
    first, last = filled[0], filled[-1]
    if _FENCE_OPEN.match(lines[first].strip()) and lines[last].strip() == _FENCE_CLOSE:
        return lines[first + 1 : last]
    return lines


def parse_response(text: str) -> list[EditBlock]:
    """Extract all edit blocks from an assistant response.

    Every block needs its own ### FILE: header, even for repeated files.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[EditBlock] = []
    current_path: str | None = None
    state = "outside"
    search: list[str] = []
    replace: list[str] = []
    start_line = 0

    for number, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if stripped.startswith(FILE_PREFIX):
            if state not in ("outside", "header"):
                raise ParseError(f"line {start_line}: unterminated SEARCH/REPLACE block")
            current_path = validate_path(stripped[len(FILE_PREFIX) :].strip().strip("`").strip())
            state = "header"
            continue

        if stripped == SEARCH_MARK:
            if current_path is None:
                raise ParseError(
                    f"line {number}: SEARCH without a ### FILE: header "
                    "(repeat ### FILE: before every block, even for the same file)"
                )
            if state not in ("header", "outside"):
                raise ParseError(f"line {number}: nested SEARCH marker")
            state = "search"
            search = []
            start_line = number
            continue

        if state == "search":
            if stripped == SPLIT_MARK:
                state = "replace"
                replace = []
                continue
            search.append(raw)
            continue

        if state == "replace":
            if stripped == REPLACE_MARK:
                assert current_path is not None
                blocks.append(
                    EditBlock(
                        path=current_path,
                        search="\n".join(_strip_fences(search, current_path)),
                        replace="\n".join(_strip_fences(replace, current_path)),
                        line=start_line,
                    )
                )
                current_path = None
                state = "outside"
                continue
            replace.append(raw)
            continue

        if stripped in (SPLIT_MARK, REPLACE_MARK):
            raise ParseError(f"line {number}: unexpected {stripped} marker")

    if state in ("search", "replace"):
        raise ParseError(f"line {start_line}: unterminated SEARCH/REPLACE block")
    if not blocks:
        raise ParseError(
            "No edit block found. Use:\n"
            "### FILE: relative/path.py\n"
            "<<<<<<< SEARCH\nold text\n=======\nnew text\n>>>>>>> REPLACE"
        )
    return blocks
