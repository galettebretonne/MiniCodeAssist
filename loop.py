#!/usr/bin/env python3
"""Apply Gem edit blocks, run checks and copy a bounded report back."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from minicodeassist.applier import ApplyResult, apply_edits, undo_last
from minicodeassist.checks import run_checks
from minicodeassist.clipboard import (
    ClipboardUnavailable,
    find_downloads_dir,
    read_clipboard,
    write_clipboard,
)
from minicodeassist.config import load_config
from minicodeassist.edits import EditBlock, ParseError, parse_response
from minicodeassist.packer import SecretFound, build_pack
from minicodeassist.report import build_report

MAX_RUN_LINES = 3000


def _read_response(path: Path | None, *, quiet: bool = False) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    try:
        return read_clipboard()
    except (ClipboardUnavailable, subprocess.SubprocessError, OSError) as error:
        if not quiet:
            print(f"Clipboard read failed: {error}", file=sys.stderr)
            print("Save the Gem response to a file and use --file response.md", file=sys.stderr)
        raise SystemExit(2) from error


def _run_main(repo: Path) -> str:
    lines: list[str] = []
    process = subprocess.Popen(
        [sys.executable, "main.py"],
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert process.stdout is not None
    try:
        for raw in process.stdout:
            text = raw.decode("utf-8", errors="replace")
            sys.stdout.write(text)
            sys.stdout.flush()
            lines.append(text)
            if len(lines) > MAX_RUN_LINES:
                lines.pop(0)
    except KeyboardInterrupt:
        process.terminate()
        lines.append("\n[interrupted by user]\n")
    code = process.wait()
    lines.append(f"\n[process exited with code {code}]\n")
    return "".join(lines)


def _finish(repo: Path, report: str) -> int:
    print("\n" + report)
    if write_clipboard(report):
        print("\nReport copied to the clipboard.")
    else:
        print(f"\nClipboard unavailable. Report saved to {repo / '.mca' / 'last_report.md'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="target repository")
    parser.add_argument("--file", type=Path, help="read the Gem response from this file")
    parser.add_argument("--undo", action="store_true", help="restore the last apply pass")
    parser.add_argument("--run", action="store_true", help="run python3 main.py interactively")
    parser.add_argument("--repack", action="store_true", help="rebuild the repository pack")
    parser.add_argument("--no-checks", action="store_true", help="skip py_compile/import/unittest")
    args = parser.parse_args(argv)

    repo = args.repo.resolve()
    config = load_config(repo)

    if args.undo:
        print(undo_last(repo))
        return 0

    if args.repack:
        try:
            result = build_pack(
                repo,
                config,
                downloads_dir=find_downloads_dir(),
            )
        except (RuntimeError, SecretFound) as error:
            print(str(error), file=sys.stderr)
            return 2
        print(f"Pack written: {result.output}")
        if result.downloads_copy:
            print(f"Downloads copy: {result.downloads_copy}")
        return 0

    run_output: str | None = None
    if args.run:
        run_output = _run_main(repo)

    try:
        response = _read_response(args.file, quiet=args.run)
    except SystemExit:
        if not args.run:
            raise
        response = ""

    blocks: list[EditBlock] = []
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
    elif not args.run:
        print("No Gem response found on the clipboard.", file=sys.stderr)
        return 2

    result = apply_edits(repo, blocks) if blocks else ApplyResult()
    checks = []
    if not args.no_checks and blocks:
        checks = run_checks(
            repo,
            [
                block.path
                for block in blocks
                if not any(rejection.path == block.path for rejection in result.rejected)
            ],
            config,
        )
    report = build_report(repo, result, checks, run_output)
    return _finish(repo, report)


if __name__ == "__main__":
    raise SystemExit(main())
