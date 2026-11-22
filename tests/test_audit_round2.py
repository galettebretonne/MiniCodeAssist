"""Regression tests for the parser, pack, line deletion and output trimming fixes."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from minicodeassist.applier import apply_edits  # noqa: E402
from minicodeassist.checks import _trim  # noqa: E402
from minicodeassist.config import load_config  # noqa: E402
from minicodeassist.edits import EditBlock, ParseError, parse_response  # noqa: E402
from minicodeassist.packer import build_pack  # noqa: E402


class ParserMarkers(unittest.TestCase):
    def test_file_header_line_inside_search_is_content(self) -> None:
        response = (
            "### FILE: README.md\n<<<<<<< SEARCH\n### FILE: old.py\n=======\n"
            "### FILE: new.py\n>>>>>>> REPLACE\n"
        )
        parsed = parse_response(response)
        self.assertEqual([item.path for item in parsed], ["README.md"])
        self.assertEqual(parsed[0].search, "### FILE: old.py")
        self.assertEqual(parsed[0].replace, "### FILE: new.py")

    def test_missing_replace_marker_is_still_detected(self) -> None:
        response = (
            "### FILE: a.py\n<<<<<<< SEARCH\nx\n=======\ny\n"
            "### FILE: b.py\n<<<<<<< SEARCH\nz\n=======\nw\n>>>>>>> REPLACE\n"
        )
        with self.assertRaisesRegex(ParseError, "unterminated"):
            parse_response(response)

    def test_second_split_marker_is_rejected(self) -> None:
        response = "### FILE: a.py\n<<<<<<< SEARCH\nTitle\n=======\nTitle\n=======\n>>>>>>> REPLACE\n"
        with self.assertRaisesRegex(ParseError, "ambiguous"):
            parse_response(response)


class PackFences(unittest.TestCase):
    def test_longer_fence_when_content_has_backtick_fences(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            (repo / "README.md").write_text("# Demo\n\n```bash\nrun\n```\n", encoding="utf-8")
            (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
            result = build_pack(repo, load_config(repo), copy_to_downloads=False)
        self.assertIn("````markdown\n# Demo", result.text)
        self.assertIn("```\n````\n", result.text)
        self.assertIn("```python\nx = 1\n```", result.text)


class DeletionKeepsLayout(unittest.TestCase):
    def apply(self, original: str, search: str) -> str:
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            target = repo / "a.py"
            target.write_text(original, encoding="utf-8")
            result = apply_edits(repo, [EditBlock("a.py", search, "", 1)])
            self.assertTrue(result.ok, result.rejected)
            return target.read_text(encoding="utf-8")

    def test_deleted_line_leaves_no_blank_line(self) -> None:
        self.assertEqual(self.apply("a = 1\nb = 2\nc = 3\n", "b = 2"), "a = 1\nc = 3\n")

    def test_deleted_lines_leave_no_blank_line(self) -> None:
        self.assertEqual(self.apply("a = 1\nb = 2\nc = 3\nd = 4\n", "b = 2\nc = 3"), "a = 1\nd = 4\n")

    def test_search_with_trailing_newline_keeps_following_blank_line(self) -> None:
        self.assertEqual(self.apply("a = 1\nb = 2\n\nc = 3\n", "b = 2\n"), "a = 1\n\nc = 3\n")


class TrimKeepsTheEnd(unittest.TestCase):
    def test_summary_line_survives_long_output(self) -> None:
        body = "\n".join(f"test_{index} ... ok" + " x" * 100 for index in range(60))
        trimmed = _trim(body + "\nRan 60 tests\n\nOK", Path("/nonexistent"))
        self.assertTrue(trimmed.startswith("..."))
        self.assertTrue(trimmed.endswith("OK"))


if __name__ == "__main__":
    unittest.main()
