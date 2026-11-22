"""Bounded Markdown report copied back to the clipboard."""

from __future__ import annotations

from pathlib import Path

from .applier import ApplyResult
from .checks import CheckResult
from .gitutils import git
from .secret_scan import redact_message

MAX_DIFF_LINES = 30
MAX_RUN_CHARS = 3000
MAX_REPORT_CHARS = 12000


def _git_diff_stat(repo: Path) -> str:
    output = git(repo, "diff", "--stat", "--", ".")
    if output is None:
        return "(unavailable)"
    lines = output.strip().splitlines()
    if len(lines) > MAX_DIFF_LINES:
        lines = lines[:MAX_DIFF_LINES] + ["... (truncated)"]
    return "\n".join(lines) if lines else "(no tracked changes)"


def build_report(
    repo: Path,
    apply: ApplyResult,
    checks: list[CheckResult] | None = None,
    run_output: str | None = None,
) -> str:
    """Assemble the report, write it to .mca/last_report.md and return it."""
    repo = Path(repo)
    parts = ["# MiniCodeAssist report", ""]

    parts.append("## Applied")
    parts.extend(f"- {item}" for item in apply.applied)
    if not apply.applied:
        parts.append("- (none)")
    parts.append("")

    parts.append("## Rejected")
    for item in apply.rejected:
        parts.append(f"- {item.path} (line {item.line}): {item.reason}")
        if item.hint:
            parts.append("  " + item.hint.replace("\n", "\n  "))
    if not apply.rejected:
        parts.append("- (none)")
    parts.append("")

    parts.append("## Checks")
    for check in checks or []:
        line = f"- {check.status} {check.name}"
        if check.detail:
            line += ": " + check.detail.replace("\n", "\n  ")
        parts.append(line)
    if not checks:
        parts.append("- (not run)")
    parts.append("")

    parts.append("## git diff --stat")
    parts.append(_git_diff_stat(repo))
    parts.append("")

    if run_output is not None:
        parts.append("## Run output")
        trimmed = redact_message(run_output[-MAX_RUN_CHARS:]).strip()
        parts.append("```text")
        parts.append(trimmed or "(no output)")
        parts.append("```")
        parts.append("")

    if apply.backup_dir is not None:
        parts.append(f"Backup: {apply.backup_dir.relative_to(repo)}")
        parts.append("Undo with: python3 loop.py --undo")

    report = "\n".join(parts)
    if len(report) > MAX_REPORT_CHARS:
        report = report[:MAX_REPORT_CHARS] + "\n... (report truncated)"
    target = repo / ".mca" / "last_report.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(report, encoding="utf-8")
    return report
