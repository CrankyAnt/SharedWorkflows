"""Shared fixtures and helpers for suite-selection reuse tests."""

from __future__ import annotations

import io
import json
import shutil
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tests import temporary_support
from tests.suite_selection_fixtures import git, write

SHARED_FOLDER = Path(__file__).resolve().parents[1] / "actions"
TOOL = SHARED_FOLDER / "suite-selection"
if str(TOOL) not in sys.path:
    sys.path.insert(0, str(TOOL))

import reuse  # noqa: E402, F401
import suite_selection  # noqa: E402

SUITES = {
    "always": [],
    "suites": [
        {"name": "pr-only", "entries": ["tests/test_pr.py"], "paths": ["tests/test_pr.py", "src/feature.py"],
         "command": ["python3", "-m", "unittest"], "setups": ["python"]},
        {"name": "both", "entries": ["tests/test_both.py"], "paths": ["tests/test_both.py", "src/feature.py", "src/moved.py"],
         "command": ["python3", "-m", "unittest"], "setups": ["python"]},
        {"name": "main-only", "entries": ["tests/test_main.py"], "paths": ["tests/test_main.py", "src/moved.py"],
         "command": ["python3", "-m", "unittest"], "setups": ["python"]},
        {"name": "per-entry", "entries": ["tests/test_one.py", "tests/test_two.py"], "paths": ["tests/**", "src/**"],
         "entry_selection": True, "entry_paths": {"tests/test_one.py": ["tests/test_one.py", "src/feature.py"],
                                                  "tests/test_two.py": ["tests/test_two.py", "src/other.py"]},
         "command": ["python3", "-m", "unittest"], "setups": ["python"], "entry_setups": {"tests/test_two.py": ["node"]}},
        {"name": "windows", "entries": ["tests/test_win.py"], "paths": ["tests/test_win.py", "src/feature.py"],
         "command": ["python", "-m", "unittest"], "setups": ["python"], "os": "windows"},
    ],
}

class FixtureRepository(unittest.TestCase):
    """Base test case providing a fresh initialized fixture Git repository copied per test method."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._template_temporary = temporary_support.Directory()
        cls.addClassCleanup(cls._template_temporary.cleanup)
        cls._template_repo = Path(cls._template_temporary.name) / "repo"
        cls._template_repo.mkdir()
        git(cls._template_repo, "init", "-q", "-b", "main")
        git(cls._template_repo, "config", "core.autocrlf", "false")
        for path in ("tests/test_pr.py", "tests/test_both.py", "tests/test_main.py", "tests/test_one.py", "tests/test_two.py",
                     "tests/test_win.py", "src/feature.py", "src/moved.py", "src/other.py", "README.md"):
            write(cls._template_repo, path, path + "\n")
        write(cls._template_repo, "suites.json", json.dumps(SUITES, indent=2) + "\n")
        git(cls._template_repo, "add", "-A")
        git(cls._template_repo, "commit", "-q", "--allow-empty", "-m", "base")
        cls._template_base = git(cls._template_repo, "rev-parse", "HEAD")

    def setUp(self) -> None:
        self.temporary = temporary_support.Directory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        shutil.copytree(self._template_repo, self.root)
        self.base = self._template_base

    def commit(self, message: str, files: dict[str, str] | None = None) -> str:
        for path, text in (files or {}).items():
            write(self.root, path, text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def select(self, *arguments: str) -> tuple[dict[str, str], str, str]:
        outputs, summary = self.root.parent / "outputs.txt", self.root.parent / "summary.md"
        outputs.unlink(missing_ok=True)
        summary.unlink(missing_ok=True)
        printed = io.StringIO()
        with redirect_stdout(printed):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "suites.json"),
                                         "--json-output", str(outputs), "--summary", str(summary), *arguments])
        self.assertEqual(code, 0, printed.getvalue())
        return dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines()), printed.getvalue(), summary.read_text(encoding="utf-8")
