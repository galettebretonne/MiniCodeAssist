#!/usr/bin/env python3
"""Apply Gem edit blocks, run checks and copy a bounded report back.

Exit codes: 0 everything applied and all checks passed, 1 some blocks were
rejected or a check failed, 2 the response could not be read or used.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from collections import deque
from pathlib import Path

from minicodeassist.applier import ApplyResult, apply_edits, undo_last
from minicodeassist.checks import CheckResult, run_checks
from minicodeassist.clipboard import (
    ClipboardUnavailable,
    find_downloads_dir,
    read_clipboard,
    write_clipboard,
)
from minicodeassist.config import ConfigError, load_config
from minicodeassist.edits import EditBlock, ParseError, parse_response
from minicodeassist.packer import SecretFound, build_pack, pack_summary
from minicodeassist.report import build_report

MAX_RUN_LINES = 3000
LAST_APPLIED = "last_applied.sha256"


def _read_response(path: Path | None) -> str:
    if path:
        try:
            return Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            print(f"Cannot read {path}: {error}", file=sys.stderr)
            raise SystemExit(2) from error
    try:
        return read_clipboard()
    except (ClipboardUnavailable, subprocess.SubprocessError, OSError) as error:
        print(f"Clipboard read failed: {error}", file=sys.stderr)
        print("Save the Gem response to a file and use --file response.md", file=sys.stderr)
        raise SystemExit(2) from error


def _response_hash(response: str) -> str:
    normalised = response.replace("\r\n", "\n").strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def _hash_file(repo: Path) -> Path:
    return repo / ".mca" / LAST_APPLIED


def _already_applied(repo: Path, digest: str) -> bool:
    try:
        return _hash_file(repo).read_text(encoding="utf-8").strip() == digest
    except OSError:
        return False


def _remember(repo: Path, digest: str) -> None:
    target = _hash_file(repo)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(digest + "\n", encoding="utf-8")


def _run_main(repo: Path) -> str:
    lines: deque[str] = deque(maxlen=MAX_RUN_LINES)
    environment = os.environ.copy()
    environment["PYTHONUNBUFFERED"] = "1"
    process = subprocess.Popen(
        [sys.executable, "-u", "main.py"],
        cwd=repo,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    try:
        # readline() instead of iteration: it returns each line as soon as it arrives.
        for raw in iter(process.stdout.readline, b""):
            text = raw.decode("utf-8", errors="replace")
            sys.stdout.write(text)
            sys.stdout.flush()
            lines.append(text)
    except KeyboardInterrupt:
        process.terminate()
        lines.append("\n[interrupted by user]\n")
    code = process.wait()
    lines.append(f"\n[process exited with code {code}]\n")
    return "".join(lines)


def _finish(repo: Path, report: str, ok: bool) -> int:
    print("\n" + report)
    if write_clipboard(report):
        print("\nReport copied to the clipboard.")
    else:
        print(f"\nClipboard unavailable. Report saved to {repo / '.mca' / 'last_report.md'}")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="target repository")
    parser.add_argument("--file", type=Path, help="read the Gem response from this file")
    parser.add_argument("--undo", action="store_true", help="restore the last apply pass")
    parser.add_argument(
        "--run",
        action="store_true",
        help="run python3 main.py interactively; the clipboard is not read (combine with --file to apply edits)",
    )
    parser.add_argument("--repack", action="store_true", help="rebuild the repository pack")
    parser.add_argument("--no-checks", action="store_true", help="skip py_compile/import/unittest")
    parser.add_argument("--force", action="store_true", help="apply a response even if it was already applied")
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    try:
        config = load_config(repo)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2

    if args.undo:
        print(undo_last(repo))
        # The undone response may legitimately be applied again.
        _hash_file(repo).unlink(missing_ok=True)
        return 0

    if args.repack:
        try:
            result = build_pack(repo, config, downloads_dir=find_downloads_dir())
        except (RuntimeError, SecretFound) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(pack_summary(result))
        return 0

    run_output: str | None = None
    if args.run:
        run_output = _run_main(repo)

    # --run alone never reads the clipboard: it usually still holds a response
    # that was already applied.
    response = ""
    if args.file or not args.run:
        response = _read_response(args.file)

    blocks: list[EditBlock] = []
    digest = ""
    if response.strip():
        try:
            blocks = parse_response(response)
        except ParseError as error:
            target = repo / ".mca" / "last_response.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(response, encoding="utf-8")
            print(str(error), file=sys.stderr)
            print(f"The raw response was saved to {target}")
            if not args.run:
                return 2
        digest = _response_hash(response)
        if blocks and not args.force and _already_applied(repo, digest):
            print(
                "This response was already applied. Copy the new Gem answer, "
                "or use --force to apply it again.",
                file=sys.stderr,
            )
            if not args.run:
                return 2
            blocks = []
    elif not args.run:
        print("No Gem response found on the clipboard.", file=sys.stderr)
        return 2

    result = apply_edits(repo, blocks) if blocks else ApplyResult()
    if result.written:
        _remember(repo, digest)
    checks: list[CheckResult] = []
    if not args.no_checks and result.written:
        checks = run_checks(repo, result.written, config)
    report = build_report(repo, result, checks, run_output)
    ok = result.ok and all(check.ok for check in checks)
    return _finish(repo, report, ok)


if __name__ == "__main__":
    raise SystemExit(main())
