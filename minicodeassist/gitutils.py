"""Small git helpers shared by the packer, the applier and the report."""

from __future__ import annotations

import subprocess
from pathlib import Path

GIT_TIMEOUT = 20
MCA_PATTERN = ".mca/"


def git(repo: Path, *args: str) -> str | None:
    """Run git in repo and return its stdout, or None if git could not run or failed."""
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.decode("utf-8", errors="replace")


def _exclude_file(repo: Path) -> Path | None:
    dot_git = repo / ".git"
    if dot_git.is_dir():
        return dot_git / "info" / "exclude"
    if dot_git.is_file():
        # Worktree or submodule: ask git where the exclude file lives.
        output = (git(repo, "rev-parse", "--git-path", "info/exclude") or "").strip()
        if output:
            path = Path(output)
            return path if path.is_absolute() else repo / path
    return None


def exclude_mca(repo: Path) -> None:
    """Add .mca/ to the git exclude file so MiniCodeAssist files stay untracked."""
    exclude = _exclude_file(Path(repo))
    if exclude is None:
        return
    try:
        content = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if MCA_PATTERN in (line.strip() for line in content.splitlines()):
            return
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a", encoding="utf-8") as handle:
            if content and not content.endswith("\n"):
                handle.write("\n")
            handle.write(MCA_PATTERN + "\n")
    except OSError:
        pass
