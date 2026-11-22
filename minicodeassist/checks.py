"""Bounded verification of files changed by an apply pass."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .secret_scan import redact_message

TIMEOUT_IMPORT = 20
TIMEOUT_TESTS = 180
MAX_DETAIL = 4000
MAX_LINES = 40


@dataclass
class CheckResult:
    """The outcome of one verification step."""

    name: str
    ok: bool
    detail: str = ""

    @property
    def status(self) -> str:
        return "PASS" if self.ok else "FAIL"


def _trim(output: str, repo: Path) -> str:
    output = redact_message(output.strip())
    if not output:
        return ""
    lines = output.splitlines()
    repo_text = str(repo.resolve())
    kept = [
        line
        for line in lines
        if repo_text in line or "Error" in line or "error:" in line or "Traceback" in line
    ]
    if kept:
        lines = kept + lines[-1:]
    if len(lines) > MAX_LINES:
        lines = lines[-MAX_LINES:]
    result = "\n".join(lines)
    return result[:MAX_DETAIL] + ("..." if len(result) > MAX_DETAIL else "")


def _run(args: list[str], repo: Path, timeout: int) -> tuple[bool, str]:
    cache = repo / ".mca" / "pycache"
    cache.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONPYCACHEPREFIX"] = str(cache)
    try:
        completed = subprocess.run(
            args,
            cwd=repo,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"Timed out after {timeout}s: {' '.join(args)}"
    except OSError as error:
        return False, f"Could not run {' '.join(args)}: {error}"
    return completed.returncode == 0, _trim(completed.stdout.decode("utf-8", errors="replace"), repo)


def has_top_level_side_effects(source: str) -> bool:
    """Detect code that runs at import time, such as an unguarded main()."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return True
    for node in tree.body:
        if isinstance(node, ast.Expr) and not (
            isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        ):
            return True
        if isinstance(node, ast.If):
            test = ast.dump(node.test)
            if "__name__" not in test or "__main__" not in test:
                return True
        if isinstance(node, (ast.For, ast.While, ast.Try, ast.With)):
            return True
    return False


def _import_check(repo: Path, path: str) -> CheckResult | None:
    if not path.endswith(".py") or path.startswith("tests/") or path == "main.py":
        return None
    target = repo / path
    if not target.is_file():
        return None
    source = target.read_text(encoding="utf-8", errors="replace")
    if has_top_level_side_effects(source):
        return CheckResult("import " + path, True, "skipped: top-level side effects detected")
    module = path[:-3].replace("/", ".").replace(".__init__", "")
    code = (
        "import importlib, sys; sys.path.insert(0, '.'); "
        f"importlib.import_module({module!r})"
    )
    ok, detail = _run([sys.executable, "-c", code], repo, TIMEOUT_IMPORT)
    return CheckResult("import " + path, ok, detail)


def run_checks(repo: Path, changed: list[str], config: Config) -> list[CheckResult]:
    """Compile changed Python files, import safe modules and run unittest."""
    repo = Path(repo)
    python_files = [path for path in changed if path.endswith(".py") and (repo / path).is_file()]
    results: list[CheckResult] = []

    if python_files:
        ok, detail = _run(
            [sys.executable, "-m", "py_compile", *python_files], repo, TIMEOUT_IMPORT
        )
        results.append(CheckResult("py_compile", ok, detail))
        for path in python_files:
            imported = _import_check(repo, path)
            if imported is not None:
                results.append(imported)
    else:
        results.append(CheckResult("py_compile", True, "no changed Python files"))

    if (repo / config.test_dir).is_dir():
        ok, detail = _run(
            [sys.executable, "-m", "unittest", "discover", "-s", config.test_dir, "-v"],
            repo,
            TIMEOUT_TESTS,
        )
        results.append(CheckResult("unittest", ok, detail))
    else:
        results.append(CheckResult("unittest", True, f"no {config.test_dir}/ directory"))
    return results
