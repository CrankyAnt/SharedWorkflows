"""Every tracked test file directly under `tests/` is an entry file of a suite of `tests/suites.json`, named by path or
matched by a pattern of `entries`. A test file that no suite names runs in no suite, and CI passes without it."""

from __future__ import annotations

import functools
import json
import sys
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, tracked_files

SUITES = REPOSITORY_ROOT / "tests" / "suites.json"
TEST_FILES = "tests/test_*.py"


@functools.cache
def pattern_matcher():
    """The suite selection's own match of a pattern of the suite list on a path."""
    directory = str(REPOSITORY_ROOT / "actions" / "suite-selection")
    sys.path.insert(0, directory)
    try:
        import suite_selection
    finally:
        sys.path.remove(directory)
    return suite_selection.matches


def unnamed_test_files(suite_list: dict, tracked: list[str]) -> list[str]:
    """The tracked paths that `TEST_FILES` matches and that no suite of the suite list names in `entries`, in path order."""
    matches = pattern_matcher()
    entries = [entry for suite in suite_list["suites"] for entry in suite["entries"]]
    return sorted(path for path in tracked if matches(TEST_FILES, path) and not any(matches(entry, path) for entry in entries))


def check_every_test_file_is_an_entry_file(case: unittest.TestCase, suite_list: dict, tracked: list[str]) -> None:
    unnamed = unnamed_test_files(suite_list, tracked)
    if unnamed:
        case.fail(f"no suite of the suite list names {', '.join(unnamed)}: a test file is an entry file of a suite, by path or by a pattern of `entries`")


class UnnamedTestFilesTests(unittest.TestCase):
    SUITE_LIST = {"always": [], "suites": [{"name": "unit", "entries": ["tests/test_unit_*.py"], "command": ["python3", "-m", "unittest", "-v"]},
                                           {"name": "build", "entries": ["tests/test_build.py"], "command": ["python3", "-m", "unittest", "-v"]}]}
    NAMED = ["tests/test_unit_a.py", "tests/test_unit_b.py", "tests/test_build.py"]
    NO_TEST_FILES = ["tests/unit_support.py", "tests/README.md", "tests/fixtures/project/tests/test_greeting.py", "tests/test_data.json", "src/test_tool.py"]

    def test_a_file_named_by_path_or_matched_by_a_pattern_is_an_entry_file(self) -> None:
        self.assertEqual([], unnamed_test_files(self.SUITE_LIST, self.NAMED))
        check_every_test_file_is_an_entry_file(self, self.SUITE_LIST, self.NAMED)

    def test_a_helper_a_fixture_and_a_file_outside_the_directory_are_no_test_files(self) -> None:
        self.assertEqual([], unnamed_test_files(self.SUITE_LIST, [*self.NAMED, *self.NO_TEST_FILES]))

    def test_a_test_file_that_no_suite_names_fails_the_check_which_names_it(self) -> None:
        tracked = [*self.NAMED, *self.NO_TEST_FILES, "tests/test_unnamed.py", "tests/test_build_more.py"]
        self.assertEqual(["tests/test_build_more.py", "tests/test_unnamed.py"], unnamed_test_files(self.SUITE_LIST, tracked))
        with self.assertRaises(AssertionError) as raised:
            check_every_test_file_is_an_entry_file(self, self.SUITE_LIST, tracked)
        message = str(raised.exception)
        self.assertIn("tests/test_build_more.py", message)
        self.assertIn("tests/test_unnamed.py", message)
        for named in self.NAMED:
            self.assertNotIn(named, message)

    def test_a_pattern_does_not_name_a_file_in_a_directory_beneath_it(self) -> None:
        suite_list = {"always": [], "suites": [{"name": "unit", "entries": ["tests/*/test_*.py"], "command": ["python3"]}]}
        self.assertEqual(["tests/test_top.py"], unnamed_test_files(suite_list, ["tests/unit/test_a.py", "tests/test_top.py"]))


class RepositoryTestFilesTests(unittest.TestCase):
    def test_every_tracked_test_file_is_an_entry_file_of_a_suite(self) -> None:
        tracked = sorted(tracked_files(REPOSITORY_ROOT, [f":(glob){TEST_FILES}"]))
        self.assertIn("tests/test_ci_policy_entry_files.py", tracked, "the listing holds the test files")
        check_every_test_file_is_an_entry_file(self, json.loads(SUITES.read_text(encoding="utf-8")), tracked)


if __name__ == "__main__":
    unittest.main()
