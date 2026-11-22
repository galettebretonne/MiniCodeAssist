"""Collect repository files and build the Markdown pack given to the Gem."""

from __future__ import annotations

import ast
import re
import secrets
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .config import Config
from .gitutils import exclude_mca, git
from .secret_scan import SecretFinding, is_sensitive_file, mask_text, scan_text

CONFIG_SUFFIXES = {".toml", ".ini", ".cfg", ".json", ".yaml", ".yml"}
CODE_SUFFIXES = {".py", ".pyi"}
TEXT_SUFFIXES = CONFIG_SUFFIXES | CODE_SUFFIXES | {".md", ".txt"}
LANGUAGE = {
    ".py": "python",
    ".pyi": "python",
    ".toml": "toml",
    ".ini": "ini",
    ".cfg": "ini",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".md": "markdown",
    ".txt": "text",
}


class SecretFound(RuntimeError):
    """Secrets were detected; the pack was not written."""

    def __init__(self, findings: dict[str, list[SecretFinding]]):
        self.findings = findings
        lines = [f"{path}: {finding}" for path, items in findings.items() for finding in items]
        super().__init__("Secrets detected; pack refused:\n" + "\n".join(lines))


@dataclass
class PackResult:
    """Where the pack went and what it contains."""

    text: str
    files: list[str]
    output: Path
    downloads_copy: Path | None = None
    masked: int = 0


def _suffix(ext: str) -> str:
    """Normalise "yaml", ".yaml" or "YAML" to ".yaml"."""
    return "." + ext.strip().lstrip(".").lower()


def _collect(repo: Path, config: Config, extra: tuple[str, ...] = ()) -> list[tuple[Path, str]]:
    """Tracked or untracked (not git-ignored) text files that are small enough, with their text."""
    raw = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard") or ""
    if raw and "\0" in raw:
        names = list(dict.fromkeys(name for name in raw.split("\0") if name))
    elif raw.strip():
        names = raw.splitlines()
    else:
        raise RuntimeError(f"No files found in {repo}. Is this a git repository?")

    allowed = CODE_SUFFIXES | CONFIG_SUFFIXES | {
        _suffix(ext) for ext in (*config.extra_extensions, *extra) if ext.strip(" .")
    }
    files: list[tuple[Path, str]] = []
    for name in names:
        path = Path(name)
        if any(part.lower() == ".mca" for part in path.parts):
            continue
        if is_sensitive_file(path.as_posix()):
            continue
        suffix = path.suffix.lower()
        if suffix in {".md", ".txt"} and suffix not in allowed:
            include = path.name.lower().startswith(("readme", "requirements"))
        else:
            include = suffix in allowed
        if not include:
            continue
        full = repo / path
        if not full.is_file() or full.stat().st_size > config.max_file_bytes:
            continue
        data = full.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        files.append((path, text))
    return sorted(files, key=lambda item: str(item[0]).lower())


def collect_files(repo: Path, config: Config, extra: tuple[str, ...] = ()) -> list[Path]:
    """List tracked or untracked (not git-ignored) text files that are small enough."""
    return [path for path, _text in _collect(repo, config, extra)]


def _fence(text: str) -> str:
    """Return a backtick fence longer than any run of backticks inside text."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def _render_tree(paths: list[Path]) -> str:
    tree: dict[str, dict] = {}
    for path in paths:
        node = tree
        for part in path.parts[:-1]:
            node = node.setdefault(part, {})
        node[path.name] = {}

    lines: list[str] = []

    def visit(node: dict[str, dict], indent: str) -> None:
        for name in sorted(node, key=str.lower):
            children = node[name]
            marker = "/" if children else ""
            lines.append(f"{indent}- {name}{marker}")
            if children:
                visit(children, indent + "  ")

    visit(tree, "")
    return "\n".join(lines)


def _symbols(path: Path, source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    results: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            self._record(node, "class")
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._record(node, "def")

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._record(node, "async def")

        def _record(self, node: ast.AST, kind: str) -> None:
            name = ".".join(self.stack + [getattr(node, "name", "?")])
            prefix = "  " * len(self.stack)
            results.append(f"{prefix}{path} :: {kind} {name} (line {node.lineno})")

    Visitor().visit(tree)
    return results


def build_pack(
    repo: Path,
    config: Config,
    *,
    canary: bool = False,
    mask: bool = False,
    extra_extensions: tuple[str, ...] = (),
    out: Path | None = None,
    copy_to_downloads: bool = True,
    downloads_dir: Path | None = None,
) -> PackResult:
    """Build and write .mca/repo_pack.md (or --out)."""
    repo = Path(repo)
    # Before "git status", so that .mca/ never shows up as an untracked change.
    exclude_mca(repo)
    collected = _collect(repo, config, extra_extensions)
    files = [path for path, _text in collected]
    commit = (git(repo, "rev-parse", "--short", "HEAD") or "").strip() or "unknown"
    dirty = bool((git(repo, "status", "--porcelain") or "").strip())

    header = [
        "# MiniCodeAssist repository pack",
        "",
        f"- Repository: {repo.name}",
        f"- Commit: {commit}",
        f"- Date: {date.today().isoformat()}",
        f"- Files: {len(files)}",
    ]
    if dirty:
        header.append("- WARNING: the working tree has uncommitted changes.")
    header.append("- Edit format: ### FILE: <path>, then <<<<<<< SEARCH / ======= / >>>>>>> REPLACE.")
    header.append("")

    nonce = secrets.token_hex(4)
    canary_lines = [
        f"<!-- MCA-CANARY 1/3 {nonce} -->",
        f"<!-- MCA-CANARY 2/3 {nonce} -->",
        f"<!-- MCA-CANARY 3/3 {nonce} -->",
    ]

    contents: list[tuple[Path, str]] = []
    findings: dict[str, list[SecretFinding]] = {}
    for path, text in collected:
        found = scan_text(text, config=path.suffix.lower() not in CODE_SUFFIXES)
        if found:
            findings[path.as_posix()] = found
    if findings and not mask:
        raise SecretFound(findings)

    masked_count = 0
    for path, text in collected:
        if mask and path.as_posix() in findings:
            masked = mask_text(text, config=path.suffix.lower() not in CODE_SUFFIXES)
            if masked != text:
                masked_count += 1
            text = masked
        if path.suffix in CODE_SUFFIXES:
            text = text.replace("\r\n", "\n")
        contents.append((path, text))

    sections = list(header)
    if canary:
        sections.append(canary_lines[0])
    sections.append("## Tree")
    sections.append(_render_tree(files))
    sections.append("")
    sections.append("## Symbol map")
    symbols: list[str] = []
    for path, text in contents:
        if path.suffix in CODE_SUFFIXES:
            symbols.extend(_symbols(path, text))
    sections.extend(symbols or ["(no Python symbols found)"])
    sections.append("")

    middle = len(contents) // 2
    for index, (path, text) in enumerate(contents):
        if canary and index == middle:
            sections.append(canary_lines[1])
            sections.append("")
        language = LANGUAGE.get(path.suffix.lower(), "text")
        fence = _fence(text)
        sections.append(f"### FILE: {path.as_posix()}")
        sections.append(f"{fence}{language}")
        sections.append(text.rstrip("\n"))
        sections.append(fence)
        sections.append("")
    if canary:
        sections.append(canary_lines[2])

    text = "\n".join(sections).rstrip() + "\n"
    destination = Path(out) if out else repo / ".mca" / "repo_pack.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(text, encoding="utf-8")

    copied: Path | None = None
    if copy_to_downloads:
        folder = downloads_dir or config.downloads_dir
        if folder is not None and folder.is_dir():
            copied = folder / f"{repo.name}_repo_pack.md"
            copied.write_text(text, encoding="utf-8")

    return PackResult(
        text=text,
        files=[path.as_posix() for path in files],
        output=destination,
        downloads_copy=copied,
        masked=masked_count,
    )


def pack_summary(result: PackResult) -> str:
    """Human-readable summary printed by pack_repo.py and loop.py --repack."""
    lines = [f"Pack written: {result.output}"]
    if result.downloads_copy:
        lines.append(f"Downloads copy: {result.downloads_copy}")
    lines.append(f"Files: {len(result.files)}")
    if result.masked:
        lines.append(f"Masked secrets in {result.masked} file(s)")
    lines.append(f"Rough token estimate: {len(result.text) // 4:,}")
    lines.append("Attach this file to the Gem at the start of a new conversation.")
    return "\n".join(lines)
