"""Regression tests for the fixes that came out of the quality audit."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minicodeassist.applier import ApplyResult  # noqa: E402
from minicodeassist.config import load_config  # noqa: E402
from minicodeassist.gitutils import git  # noqa: E402
from minicodeassist.packer import build_pack  # noqa: E402
from minicodeassist.report import build_report  # noqa: E402
from minicodeassist.secret_scan import mask_text  # noqa: E402


class TempRepo(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name)

    def run_git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def init_repo_with_fresh_file(self) -> None:
        (self.repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
        self.run_git("init", "-q")
        self.run_git("add", "-A")
        (self.repo / "fresh.py").write_text("y = 2\n", encoding="utf-8")


class PrivateKeyMasking(unittest.TestCase):
    # Built from pieces so that this file never contains a complete key header.
    BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
    END = "-----END " + "PRIVATE KEY-----"

    def test_whole_key_is_masked(self) -> None:
        text = f'KEY = """{self.BEGIN}\nbodyline1\nbodyline2\n{self.END}"""\n'
        masked = mask_text(text)
        self.assertNotIn("bodyline1", masked)
        self.assertNotIn("bodyline2", masked)
        self.assertIn("***MASKED***", masked)

    def test_lone_header_in_source_does_not_mask_the_rest(self) -> None:
        text = f'def is_key(line):\n    return line.startswith("{self.BEGIN}")\n\ndef other():\n    return 42\n'
        masked = mask_text(text)
        self.assertIn("***MASKED***", masked)
        self.assertIn("def other():", masked)
        self.assertIn("return 42", masked)

    def test_escaped_newlines_in_string_are_masked(self) -> None:
        text = 'K = "' + self.BEGIN + '\\nMIIEvQIBADANBgkq\\n' + self.END + '"\nafter = 1\n'
        masked = mask_text(text)
        self.assertNotIn("MIIEvQIBADANBgkq", masked)
        self.assertIn("after = 1", masked)

    def test_unterminated_key_is_masked_to_the_end(self) -> None:
        masked = mask_text(f"before\n{self.BEGIN}\nbodyline1\n")
        self.assertNotIn("bodyline1", masked)
        self.assertIn("before", masked)


class UntrackedFiles(TempRepo):
    def test_untracked_file_is_packed_but_ignored_file_is_not(self) -> None:
        self.init_repo_with_fresh_file()
        (self.repo / ".gitignore").write_text("ignored.py\n", encoding="utf-8")
        (self.repo / "ignored.py").write_text("z = 3\n", encoding="utf-8")
        result = build_pack(self.repo, load_config(self.repo), copy_to_downloads=False)
        self.assertIn("fresh.py", result.files)
        self.assertNotIn("ignored.py", result.files)

    def test_report_lists_untracked_files(self) -> None:
        self.init_repo_with_fresh_file()
        report = build_report(self.repo, ApplyResult(), [])
        self.assertIn("new (untracked): fresh.py", report)


class GitFailure(TempRepo):
    def test_failing_git_command_returns_none(self) -> None:
        self.assertIsNone(git(self.repo, "no-such-subcommand"))


class LoopMissingFile(TempRepo):
    def test_missing_response_file_exits_with_code_2(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "loop.py"),
                "--repo",
                str(self.repo),
                "--file",
                str(self.repo / "missing.md"),
                "--no-checks",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("Cannot read", completed.stderr)
        self.assertNotIn("Traceback", completed.stderr)


class ConfigPercentSign(TempRepo):
    def test_percent_sign_is_not_interpolated(self) -> None:
        (self.repo / ".mca.ini").write_text("[mca]\ndownloads_dir = /tmp/50%/dl\n", encoding="utf-8")
        self.assertEqual(load_config(self.repo).downloads_dir, Path("/tmp/50%/dl"))


if __name__ == "__main__":
    unittest.main()
