"""Regression tests for the bugs found in the code review."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minicodeassist.applier import apply_edits, undo_last  # noqa: E402
from minicodeassist.checks import has_top_level_side_effects  # noqa: E402
from minicodeassist.config import ConfigError, load_config  # noqa: E402
from minicodeassist.edits import EditBlock, ParseError, parse_response, validate_path  # noqa: E402
from minicodeassist.packer import build_pack  # noqa: E402
from minicodeassist.secret_scan import is_sensitive_file, mask_text, scan_text  # noqa: E402


def block(path: str, search: str, replace: str, line: int = 1) -> EditBlock:
    return EditBlock(path=path, search=search, replace=replace, line=line)


class TempRepo(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name)

    def write(self, path: str, content: str | bytes) -> Path:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode("utf-8")
        target.write_bytes(content)
        return target

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout


class ApplierRegressions(TempRepo):
    def test_exact_match_requires_whole_lines(self) -> None:
        target = self.write("a.py", "xx = 10\ny = 2\n")
        result = apply_edits(self.repo, [block("a.py", "x = 1", "y")])
        self.assertFalse(result.ok)
        self.assertEqual(target.read_text(), "xx = 10\ny = 2\n")

    def test_exact_match_counts_whole_line_occurrences_only(self) -> None:
        target = self.write("a.py", "xx = 1\nx = 1\n")
        result = apply_edits(self.repo, [block("a.py", "x = 1", "x = 2")])
        self.assertTrue(result.ok, result.rejected)
        self.assertEqual(target.read_text(), "xx = 1\nx = 2\n")

    def test_single_line_search_multi_line_replace_keeps_relative_indent(self) -> None:
        target = self.write("a.py", "def f(x):\n        return x\n")
        result = apply_edits(
            self.repo, [block("a.py", "    return x\n", "    if x:\n        return x\n")]
        )
        self.assertTrue(result.ok, result.rejected)
        self.assertEqual(target.read_text(), "def f(x):\n        if x:\n            return x\n")

    def test_dedented_replacement_line(self) -> None:
        target = self.write("a.py", "class A:\n        def f(self):\n            return 1\n")
        result = apply_edits(
            self.repo,
            [block("a.py", "    def f(self):\n        return 1\n", "    def f(self):\n        return 1\n\n    def g(self):\n        return 2\n")],
        )
        self.assertTrue(result.ok, result.rejected)
        self.assertEqual(
            target.read_text(),
            "class A:\n        def f(self):\n            return 1\n\n        def g(self):\n            return 2\n",
        )

    def test_file_is_all_or_nothing(self) -> None:
        target = self.write("a.py", "x = 1\ny = 2\n")
        other = self.write("b.py", "z = 3\n")
        result = apply_edits(
            self.repo,
            [
                block("a.py", "x = 1", "x = 10"),
                block("a.py", "missing = 0", "q"),
                block("b.py", "z = 3", "z = 30"),
            ],
        )
        self.assertEqual(target.read_text(), "x = 1\ny = 2\n")
        self.assertEqual(other.read_text(), "z = 30\n")
        self.assertEqual(result.written, ["b.py"])
        self.assertTrue(any("left unchanged" in item.reason for item in result.rejected))

    def test_non_utf8_file_is_rejected_not_crashing(self) -> None:
        target = self.write("latin.py", "x = 'caf\xe9'\n".encode("latin-1"))
        result = apply_edits(self.repo, [block("latin.py", "x = 1", "x = 2")])
        self.assertFalse(result.ok)
        self.assertIn("UTF-8", result.rejected[0].reason)
        self.assertEqual(target.read_bytes(), "x = 'caf\xe9'\n".encode("latin-1"))

    def test_backup_exists_before_write_and_undo(self) -> None:
        self.write("a.py", "x = 1\n")
        result = apply_edits(self.repo, [block("a.py", "x = 1", "x = 2")])
        self.assertEqual((result.backup_dir / "a.py").read_text(), "x = 1\n")
        self.assertIn("restored a.py", undo_last(self.repo))
        self.assertEqual((self.repo / "a.py").read_text(), "x = 1\n")

    def test_nothing_applied_creates_no_backup(self) -> None:
        self.write("a.py", "x = 1\n")
        result = apply_edits(self.repo, [block("a.py", "nope", "x")])
        self.assertIsNone(result.backup_dir)


class EditsRegressions(unittest.TestCase):
    def test_protected_paths(self) -> None:
        for path in ("./.mca/backup/x/manifest.json", ".git/hooks/pre-commit", ".MCA/x", "sub/.Git/config", ".git", "./"):
            with self.subTest(path=path), self.assertRaises(ParseError):
                validate_path(path)
        self.assertEqual(validate_path("./src/app.py"), "src/app.py")
        self.assertEqual(validate_path(".github/workflows/ci.yml"), ".github/workflows/ci.yml")

    def test_markdown_fences_in_content_are_kept(self) -> None:
        response = (
            "### FILE: README.md\n<<<<<<< SEARCH\n```bash\nrun\n```\n=======\n"
            "```bash\nrun --fast\n```\n>>>>>>> REPLACE\n"
        )
        parsed = parse_response(response)[0]
        self.assertEqual(parsed.search, "```bash\nrun\n```")
        self.assertEqual(parsed.replace, "```bash\nrun --fast\n```")

    def test_only_wrapping_fence_pair_is_stripped(self) -> None:
        response = "### FILE: a.py\n<<<<<<< SEARCH\n```python\nx = 1\n```\n=======\nx = '```'\n>>>>>>> REPLACE\n"
        parsed = parse_response(response)[0]
        self.assertEqual(parsed.search, "x = 1")
        self.assertEqual(parsed.replace, "x = '```'")

    def test_each_block_needs_its_own_header(self) -> None:
        response = (
            "### FILE: a.py\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n"
            "<<<<<<< SEARCH\nc\n=======\nd\n>>>>>>> REPLACE\n"
        )
        with self.assertRaisesRegex(ParseError, "repeat ### FILE"):
            parse_response(response)
        repeated = response.replace("REPLACE\n<<<", "REPLACE\n### FILE: a.py\n<<<")
        self.assertEqual([item.path for item in parse_response(repeated)], ["a.py", "a.py"])

    def test_block_line_points_at_search_marker(self) -> None:
        response = "plan\n\n### FILE: a.py\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n"
        self.assertEqual(parse_response(response)[0].line, 4)


class SecretRegressions(unittest.TestCase):
    def test_prefixed_and_unquoted_keys(self) -> None:
        self.assertTrue(scan_text('DB_PASSWORD = "s3cr3tvalue"'))
        self.assertTrue(scan_text('OPENAI_API_KEY="abcdefgh1234"'))
        self.assertTrue(scan_text("password: hunter2", config=True))
        self.assertTrue(scan_text('key = "sk-proj-abcdefghijklmnopqrstuvwx"'))
        self.assertNotIn("hunter2", mask_text("password: hunter2", config=True))

    def test_false_positives(self) -> None:
        for text in ("password = get_password()", 'tokenizer = "bert-base"', "def check(pwd): pass"):
            with self.subTest(text=text):
                self.assertEqual(scan_text(text), [])
        for text in ("password: ${DB_PASS}", "password: null", "password:"):
            with self.subTest(text=text):
                self.assertEqual(scan_text(text, config=True), [])

    def test_no_catastrophic_backtracking(self) -> None:
        samples = ("a_" * 20000 + "x", "a-" * 50000, "password" * 5000, "x://" + "a" * 50000)
        start = time.monotonic()
        for sample in samples:
            scan_text(sample, config=True)
        self.assertLess(time.monotonic() - start, 2.0)

    def test_sensitive_file_names(self) -> None:
        for name in (".env", ".env.local", "credentials.json", "conf/secrets.yaml", "server.pem", "id_rsa"):
            with self.subTest(name=name):
                self.assertTrue(is_sensitive_file(name))
        for name in (".env.example", "config.json", "secret_scan.py"):
            with self.subTest(name=name):
                self.assertFalse(is_sensitive_file(name))


class PackRegressions(TempRepo):
    def setUp(self) -> None:
        super().setUp()
        self.write("main.py", "def main():\n    return 1\n")
        self.write("conf.yaml", "name: demo\n")
        self.write("notes.rst", "Notes\n")
        self.write("credentials.json", '{"user": "demo"}\n')
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")

    def test_extra_extensions_without_dot(self) -> None:
        self.write(".mca.ini", "[mca]\nextra_extensions = rst\n")
        result = build_pack(self.repo, load_config(self.repo), copy_to_downloads=False)
        self.assertIn("### FILE: notes.rst", result.text)
        result = build_pack(self.repo, load_config(self.repo), extra_extensions=("RST",), copy_to_downloads=False)
        self.assertIn("### FILE: notes.rst", result.text)

    def test_sensitive_files_are_excluded(self) -> None:
        result = build_pack(self.repo, load_config(self.repo), copy_to_downloads=False)
        self.assertNotIn("credentials.json", result.text)

    def test_pack_excludes_mca_from_git_status(self) -> None:
        build_pack(self.repo, load_config(self.repo), copy_to_downloads=False)
        self.assertEqual(self.git("status", "--porcelain").strip(), "")
        second = build_pack(self.repo, load_config(self.repo), copy_to_downloads=False)
        self.assertNotIn("uncommitted changes", second.text)

    def test_unquoted_secret_in_yaml_blocks_pack(self) -> None:
        self.write("conf.yaml", "password: hunter2secret\n")
        with self.assertRaises(Exception):
            build_pack(self.repo, load_config(self.repo), copy_to_downloads=False)


class ConfigAndChecksRegressions(TempRepo):
    def test_invalid_max_file_kb(self) -> None:
        for value in ("abc", "0", "-5"):
            with self.subTest(value=value):
                self.write(".mca.ini", f"[mca]\nmax_file_kb = {value}\n")
                with self.assertRaises(ConfigError):
                    load_config(self.repo)

    def test_assignments_with_calls_are_side_effects(self) -> None:
        self.assertTrue(has_top_level_side_effects("import subprocess\nx = subprocess.run(['ls'])\n"))
        self.assertTrue(has_top_level_side_effects("x: int = compute()\n"))
        self.assertFalse(has_top_level_side_effects("import logging\nlog = logging.getLogger(__name__)\n"))
        self.assertFalse(has_top_level_side_effects("import re\nPAT = re.compile('a')\n"))


class LoopRegressions(TempRepo):
    RESPONSE = "### FILE: a.py\n<<<<<<< SEARCH\nx = 1\n=======\nx = 1\nx = 1\n>>>>>>> REPLACE\n"

    def setUp(self) -> None:
        super().setUp()
        self.write("a.py", "x = 1\n")
        self.write("response.md", self.RESPONSE)
        self.git("init", "-q")

    def loop(self, *args: str) -> subprocess.CompletedProcess[str]:
        import os

        env = dict(os.environ, MCA_CLIPBOARD_WRITE_COMMAND="true", MCA_CLIPBOARD_READ_COMMAND="false")
        return subprocess.run(
            [sys.executable, str(ROOT / "loop.py"), "--repo", str(self.repo), "--no-checks", *args],
            capture_output=True, text=True, env=env, timeout=60,
        )

    def test_same_response_is_not_applied_twice(self) -> None:
        first = self.loop("--file", str(self.repo / "response.md"))
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.loop("--file", str(self.repo / "response.md"))
        self.assertEqual(second.returncode, 2)
        self.assertIn("already applied", second.stderr)
        self.assertEqual((self.repo / "a.py").read_text(), "x = 1\nx = 1\n")

    def test_rejection_gives_non_zero_exit(self) -> None:
        self.write("response.md", "### FILE: a.py\n<<<<<<< SEARCH\nnope\n=======\nx\n>>>>>>> REPLACE\n")
        self.assertEqual(self.loop("--file", str(self.repo / "response.md")).returncode, 1)

    def test_run_alone_does_not_read_clipboard(self) -> None:
        self.write("main.py", "print('hello', flush=False)\n")
        result = self.loop("--run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Clipboard read failed", result.stderr)
        self.assertIn("hello", result.stdout)


if __name__ == "__main__":
    unittest.main()
