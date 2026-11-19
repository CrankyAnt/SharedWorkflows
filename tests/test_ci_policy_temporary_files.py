"""Every test in `tests/` creates its temporary files through `tests/temporary_support.py`, and no removal in `tests/` or
in the recording step ignores its errors through `ignore_errors` or `ignore_cleanup_errors`. An `onexc` or `onerror`
handler that swallows errors is left to review. Calls inside string literals, such as the fixture programs of the
recording tests, and the files under `tests/fixtures/` are not checked."""

from __future__ import annotations

import ast
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, tracked_files

HELPER = "tests/temporary_support.py"
CHECKED = [":(glob)tests/*.py", "actions/recording/recording.py"]
CREATORS = frozenset({"mkdtemp", "mkstemp", "mktemp", "TemporaryDirectory", "TemporaryFile", "NamedTemporaryFile", "SpooledTemporaryFile"})


def silenced(value: ast.expr) -> bool:
    """Whether an ``ignore_errors`` or ``ignore_cleanup_errors`` value may be true: anything but a false constant."""
    return not (isinstance(value, ast.Constant) and not value.value)


def violations(path: str, source: str) -> list[str]:
    """Each call in ``source`` that creates a temporary file outside the helper, or that removes a tree or a temporary
    directory with its errors ignored, as ``<path>:<line>: <reason>``."""
    tree = ast.parse(source, filename=path)
    modules = {"tempfile": set(), "shutil": set()}
    creators: dict[str, str] = {}
    removers: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in modules:
                    modules[alias.name].add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "tempfile":
            creators.update({alias.asname or alias.name: alias.name for alias in node.names if alias.name in CREATORS})
        elif isinstance(node, ast.ImportFrom) and node.module == "shutil":
            removers.update(alias.asname or alias.name for alias in node.names if alias.name == "rmtree")
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name):
            module, name = function.value.id, function.attr
            creator = name if module in modules["tempfile"] and name in CREATORS else None
            remover = module in modules["shutil"] and name == "rmtree"
        elif isinstance(function, ast.Name):
            creator, remover = creators.get(function.id), function.id in removers
        else:
            creator, remover = None, False
        if creator is not None and path != HELPER:
            found.append(f"{path}:{node.lineno}: tempfile.{creator} outside {HELPER}")
        keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}
        if remover and (silenced(keywords["ignore_errors"]) if "ignore_errors" in keywords else len(node.args) > 1 and silenced(node.args[1])):
            found.append(f"{path}:{node.lineno}: shutil.rmtree with ignore_errors")
        if "ignore_cleanup_errors" in keywords and silenced(keywords["ignore_cleanup_errors"]):
            found.append(f"{path}:{node.lineno}: ignore_cleanup_errors")
    return sorted(found, key=lambda item: int(item.split(":")[1]))


class ViolationTests(unittest.TestCase):
    def test_each_creator_is_found_by_its_module_its_module_alias_and_its_own_alias(self) -> None:
        source = ("import tempfile\nimport tempfile as temporary\nfrom tempfile import mkdtemp, NamedTemporaryFile as named\n"
                  "tempfile.TemporaryDirectory()\ntemporary.mkstemp()\nmkdtemp()\nnamed()\ntempfile.mktemp()\n"
                  "tempfile.SpooledTemporaryFile()\ntempfile.TemporaryFile()\n")
        self.assertEqual(["tests/x.py:4: tempfile.TemporaryDirectory outside tests/temporary_support.py",
                          "tests/x.py:5: tempfile.mkstemp outside tests/temporary_support.py",
                          "tests/x.py:6: tempfile.mkdtemp outside tests/temporary_support.py",
                          "tests/x.py:7: tempfile.NamedTemporaryFile outside tests/temporary_support.py",
                          "tests/x.py:8: tempfile.mktemp outside tests/temporary_support.py",
                          "tests/x.py:9: tempfile.SpooledTemporaryFile outside tests/temporary_support.py",
                          "tests/x.py:10: tempfile.TemporaryFile outside tests/temporary_support.py"], violations("tests/x.py", source))

    def test_the_helper_may_create_and_other_tempfile_calls_pass(self) -> None:
        source = "import tempfile\ntempfile.mkdtemp()\ntempfile.gettempdir()\n"
        self.assertEqual([], violations(HELPER, source))
        self.assertEqual(["tests/x.py:2: tempfile.mkdtemp outside tests/temporary_support.py"], violations("tests/x.py", source))

    def test_a_call_inside_a_string_literal_is_not_checked(self) -> None:
        source = 'import tempfile\nPROGRAM = """\nimport tempfile\ntempfile.mkdtemp()\nshutil.rmtree(x, ignore_errors=True)\n"""\n'
        self.assertEqual([], violations("tests/x.py", source))

    def test_a_removal_that_ignores_its_errors_by_keyword_or_position_is_found(self) -> None:
        source = ("import shutil\nimport shutil as sh\nfrom shutil import rmtree as remove\n"
                  "shutil.rmtree(path, ignore_errors=True)\nsh.rmtree(path, True)\nremove(path, ignore_errors=flag)\n"
                  "shutil.rmtree(path)\nshutil.rmtree(path, ignore_errors=False)\nshutil.rmtree(path, False)\n"
                  "Directory(ignore_cleanup_errors=True)\nDirectory(ignore_cleanup_errors=False)\n")
        self.assertEqual(["tests/x.py:4: shutil.rmtree with ignore_errors", "tests/x.py:5: shutil.rmtree with ignore_errors",
                          "tests/x.py:6: shutil.rmtree with ignore_errors", "tests/x.py:10: ignore_cleanup_errors"],
                         violations("tests/x.py", source))


class RepositoryTests(unittest.TestCase):
    def test_no_test_and_no_recording_step_creates_a_temporary_file_outside_the_helper_or_silences_a_removal(self) -> None:
        files = sorted(tracked_files(REPOSITORY_ROOT, CHECKED))
        self.assertIn(HELPER, files)
        self.assertIn("actions/recording/recording.py", files)
        self.assertFalse(any(path.startswith("tests/fixtures/") for path in files))
        found = [item for path in files for item in violations(path, (REPOSITORY_ROOT / path).read_text(encoding="utf-8"))]
        self.assertEqual([], found, "create temporary files with tests/temporary_support.py and remove them with remove_tree:\n" + "\n".join(found))


if __name__ == "__main__":
    unittest.main()
