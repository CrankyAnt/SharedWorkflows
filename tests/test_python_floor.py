"""The static check of the shared Python code against Python 3.12, the oldest system python3 of the images that
shared jobs and steps run on. mypy reports what fails under 3.12 and not under the interpreter that runs mypy: a
standard-library name or a syntax that 3.12 lacks. Type errors that both versions report are not this check's."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.hygiene_fixtures import REPOSITORY_ROOT, tools_available, tools_directory

FLOOR = "3.12"
SHARED_CODE = REPOSITORY_ROOT / "actions"


def mypy() -> str:
    directory = tools_directory()
    if directory is None:
        return "mypy"
    return str(next(candidate for candidate in (directory / "mypy.exe", directory / "mypy") if candidate.is_file()))


def errors(files: list[Path], root: Path, *version: str) -> set[str]:
    with tempfile.TemporaryDirectory() as cache:
        result = subprocess.run([mypy(), *version, "--cache-dir", cache, "--ignore-missing-imports", "--explicit-package-bases",
                                 "--no-error-summary", "--show-error-codes", *(str(path) for path in files)],
                                cwd=root, capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        raise AssertionError(f"mypy failed with exit {result.returncode}: {result.stdout}{result.stderr}")
    return {line for line in result.stdout.splitlines() if ": error: " in line}


def floor_problems(files: list[Path], root: Path) -> list[str]:
    """The errors mypy reports under the floor and not under its own interpreter."""
    return sorted(errors(files, root, "--python-version", FLOOR) - errors(files, root))


@unittest.skipUnless(tools_available("mypy"), "the pinned mypy is neither in HYGIENE_TOOLS nor on PATH")
class PythonFloorTests(unittest.TestCase):
    def test_the_shared_python_code_runs_on_python_3_12(self) -> None:
        files = sorted(SHARED_CODE.rglob("*.py"))
        self.assertTrue(files)
        self.assertEqual([], floor_problems(files, REPOSITORY_ROOT))

    def test_a_use_of_full_match_fails_the_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "newer.py").write_text('from pathlib import PurePosixPath\n\nPurePosixPath("a/b").full_match("**/b")\n', encoding="utf-8")
            (root / "floor.py").write_text('from pathlib import PurePosixPath\n\nPurePosixPath("a/b").match("*/b")\n', encoding="utf-8")
            problems = floor_problems([root / "newer.py", root / "floor.py"], root)
        self.assertEqual(1, len(problems), problems)
        self.assertIn("newer.py:3:", problems[0])
        self.assertIn('"PurePosixPath" has no attribute "full_match"', problems[0])


if __name__ == "__main__":
    unittest.main()
