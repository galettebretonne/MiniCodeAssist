"""Tests for MiniCodeAssist (standard library only)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from minicodeassist.applier import apply_edits, undo_last
from minicodeassist.checks import has_top_level_side_effects
from minicodeassist.config import load_config
from minicodeassist.edits import EditBlock, ParseError, parse_response, validate_path
from minicodeassist.packer import SecretFound, build_pack
from minicodeassist.secret_scan import mask_text, scan_text


def block(path: str, search: str, replace: str, line: int = 1) -> EditBlock:
    return EditBlock(path=path, search=search, replace=replace, line=line)


class RepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def write(self, path: str, content: str, newline: str = "\n") -> Path:
        target = self.repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.replace("\n", newline).encode("utf-8"))
        return target

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)


class ParseTests(unittest.TestCase):
    def test_parses_plan_and_multiple_blocks(self) -> None:
        response = """Here is the plan:
- change one
- add a file

### FILE: pkg/one.py
<<<<<<< SEARCH
old = 1
=======
old = 2
>>>>>>> REPLACE

### FILE: new.py
<<<<<<< SEARCH
=======
value = 3
>>>>>>> REPLACE
"""
        blocks = parse_response(response)
        self.assertEqual([item.path for item in blocks], ["pkg/one.py", "new.py"])
        self.assertEqual(blocks[0].search, "old = 1")
        self.assertEqual(blocks[0].replace, "old = 2")
        self.assertEqual(blocks[1].search, "")
        self.assertEqual(blocks[1].replace, "value = 3")

    def test_ignores_markdown_fences(self) -> None:
        response = """```text
### FILE: a.py
<<<<<<< SEARCH
x
=======
y
>>>>>>> REPLACE
```"""
        self.assertEqual(parse_response(response)[0].replace, "y")

    def test_rejects_escape_paths(self) -> None:
        for path in ("../secret.py", "/etc/passwd", "C:\\secret.py", ".mca/config"):
            with self.subTest(path=path), self.assertRaises(ParseError):
                validate_path(path)

    def test_rejects_response_without_blocks(self) -> None:
        with self.assertRaises(ParseError):
            parse_response("I think the answer is to rewrite everything.")

    def test_rejects_unterminated_block(self) -> None:
        with self.assertRaises(ParseError):
            parse_response("### FILE: a.py\n<<<<<<< SEARCH\nx\n=======\ny\n")


class ApplyTests(RepoTestCase):
    def test_exact_apply_and_backup(self) -> None:
        self.write("a.py", "x = 1\ny = 2\n")
        result = apply_edits(self.repo, [block("a.py", "x = 1", "x = 10")])
        self.assertTrue(result.ok)
        self.assertEqual((self.repo / "a.py").read_text(), "x = 10\ny = 2\n")
        self.assertIsNotNone(result.backup_dir)
        self.assertEqual(
            (result.backup_dir / "a.py").read_text(), "x = 1\ny = 2\n"
        )

    def test_whitespace_tier_and_crlf_preserved(self) -> None:
        target = self.write("a.py", "def f():\n    return 1   \n", newline="\r\n")
        result = apply_edits(
            self.repo,
            [block("a.py", "def f():\n    return 1\n", "def f():\n    return 2\n")],
        )
        self.assertTrue(result.ok, result.rejected)
        self.assertEqual(target.read_bytes(), b"def f():\r\n    return 2\r\n")

    def test_indent_tier(self) -> None:
        self.write("a.py", "def f():\n        return 1\n")
        result = apply_edits(
            self.repo,
            [block("a.py", "def f():\n    return 1\n", "def f():\n    return 2\n")],
        )
        self.assertTrue(result.ok, result.rejected)
        self.assertEqual((self.repo / "a.py").read_text(), "def f():\n        return 2\n")

    def test_create_and_undo(self) -> None:
        self.write("a.py", "x = 1\n")
        result = apply_edits(
            self.repo,
            [
                block("a.py", "x = 1", "x = 2"),
                block("fresh.py", "", "y = 9\n"),
            ],
        )
        self.assertTrue(result.ok, result.rejected)
        self.assertTrue((self.repo / "fresh.py").exists())
        message = undo_last(self.repo)
        self.assertIn("restored a.py", message)
        self.assertIn("removed fresh.py", message)
        self.assertEqual((self.repo / "a.py").read_text(), "x = 1\n")
        self.assertFalse((self.repo / "fresh.py").exists())
        self.assertIn("Nothing to undo", undo_last(self.repo))

    def test_missing_and_ambiguous_search(self) -> None:
        self.write("a.py", "x = 1\nx = 1\n")
        result = apply_edits(
            self.repo,
            [
                block("a.py", "x = 1", "x = 2"),
                block("a.py", "x = 11", "z"),
            ],
        )
        self.assertEqual(len(result.rejected), 2)
        self.assertIn("ambiguous", result.rejected[0].reason)
        self.assertIn("not found", result.rejected[1].reason)
        self.assertTrue(result.rejected[1].hint)

    def test_refuses_existing_file_creation(self) -> None:
        self.write("a.py", "x = 1\n")
        result = apply_edits(self.repo, [block("a.py", "", "x = 2\n")])
        self.assertFalse(result.ok)
        self.assertIn("already exists", result.rejected[0].reason)


class SecretTests(unittest.TestCase):
    def test_detects_and_masks(self) -> None:
        text = 'api_key = "abcdef1234567890"\nprint("ok")\n'
        findings = scan_text(text)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].line, 1)
        masked = mask_text(text)
        self.assertIn("***MASKED***", masked)
        self.assertNotIn("abcdef1234567890", masked)

    def test_redacted_finding_does_not_leak_value(self) -> None:
        finding = scan_text("password = 'hunter2secret'")[0]
        self.assertNotIn("hunter2secret", str(finding))


class PackTests(RepoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.write("main.py", '"""Entry point."""\n\n\ndef main():\n    return 1\n\n\nif __name__ == "__main__":\n    main()\n')
        self.write("pkg/__init__.py", "class Thing:\n    def go(self):\n        return 2\n")
        self.write("README.md", "# Demo\n")
        self.write(".env", "TOKEN=abc\n")
        self.write("config.json", '{"a": 1}\n')
        self.git("init", "-q")
        self.git("add", "-A")

    def test_pack_contents(self) -> None:
        result = build_pack(self.repo, load_config(self.repo), canary=True)
        self.assertIn("### FILE: main.py", result.text)
        self.assertIn("### FILE: pkg/__init__.py", result.text)
        self.assertNotIn("### FILE: .env", result.text)
        self.assertIn("class Thing", result.text)
        self.assertIn("def go", result.text)
        self.assertEqual(result.text.count("MCA-CANARY"), 3)
        self.assertEqual(result.output, self.repo / ".mca" / "repo_pack.md")

    def test_secret_refusal_and_mask(self) -> None:
        self.write("config.json", '{"api_key": "verysecretvalue123"}\n')
        self.git("add", "config.json")
        with self.assertRaises(SecretFound):
            build_pack(self.repo, load_config(self.repo))
        result = build_pack(self.repo, load_config(self.repo), mask=True)
        self.assertIn("***MASKED***", result.text)
        self.assertNotIn("verysecretvalue123", result.text)

    def test_config_and_extra_extensions(self) -> None:
        self.write(".mca.ini", "[mca]\nmax_file_kb = 1\nextra_extensions = pyi\n")
        self.write("types.pyi", "def f() -> int: ...\n")
        self.git("add", "-A")
        config = load_config(self.repo)
        self.assertEqual(config.max_file_bytes, 1024)
        self.assertEqual(config.extra_extensions, ("pyi",))
        result = build_pack(self.repo, config)
        self.assertIn("### FILE: types.pyi", result.text)


class CheckTests(unittest.TestCase):
    def test_side_effect_detection(self) -> None:
        self.assertFalse(has_top_level_side_effects("x = 1\n"))
        self.assertTrue(has_top_level_side_effects("print('hi')\n"))
        self.assertFalse(
            has_top_level_side_effects('if __name__ == "__main__":\n    print(1)\n')
        )


if __name__ == "__main__":
    unittest.main()
