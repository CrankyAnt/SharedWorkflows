from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.hygiene_fixtures import fixture_repository, tools_available, tools_directory
from tests.hygiene_process import RESOURCES, hygiene


WORKFLOW_HEAD = "name: fixture\non: push\npermissions: {}\njobs:\n  a:\n    runs-on: ubuntu-latest\n    permissions:\n      contents: read\n    steps:\n"


@unittest.skipUnless(tools_available("actionlint", "shellcheck"), "the pinned tools are neither in HYGIENE_TOOLS nor on PATH")
class CheckRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_actionlint_ignores_only_the_missing_ref_diagnostic_for_self_repository_references(self) -> None:
        passing = fixture_repository(self.root / "passing", {".github/workflows/a.yml": WORKFLOW_HEAD + "      - uses: $/tools/resources\n      - run: echo ok\n"})
        result = hygiene("actionlint", passing)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        other = fixture_repository(self.root / "other", {".github/workflows/a.yml": WORKFLOW_HEAD + "      - uses: fixture/action\n      - run: echo ok\n"})
        result = hygiene("actionlint", other)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn('specifying action "fixture/action" in invalid format because ref is missing', result.stdout)

    def test_actionlint_ignores_only_the_unknown_ubuntu_26_04_label(self) -> None:
        head = WORKFLOW_HEAD.replace("ubuntu-latest", "{label}")
        for label, code in (("ubuntu-26.04", 0), ("ubuntu-26.10", 1)):
            with self.subTest(label=label):
                repo = fixture_repository(self.root / label, {".github/workflows/a.yml": head.replace("{label}", label) + "      - run: echo ok\n"})
                result = hygiene("actionlint", repo)
                self.assertEqual(code, result.returncode, result.stdout + result.stderr)
                self.assertEqual(code == 1, f'label "{label}" is unknown' in result.stdout, result.stdout)

    def test_actionlint_runs_with_exactly_the_two_self_repository_patterns_and_the_label_pattern(self) -> None:
        repo = fixture_repository(self.root / "flags", {".github/workflows/a.yml": WORKFLOW_HEAD + "      - run: echo ok\n"})
        result = hygiene("actionlint", repo)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        command = next(line for line in result.stdout.splitlines() if line.startswith("+ "))
        patterns = command.split(" -ignore ")[1:]
        self.assertEqual(['specifying action "\\$/[^"]*" in invalid format because ref is missing',
                          'reusable workflow call "\\$/\\.github/workflows/[A-Za-z0-9_.-]+\\.ya?ml" at "uses" is not following the format',
                          'label "ubuntu-26\\.04" is unknown'], patterns)

    def test_a_shellcheck_finding_in_a_run_block_fails(self) -> None:
        repo = fixture_repository(self.root / "shell", {".github/workflows/a.yml": WORKFLOW_HEAD + "      - run: |\n          files=$(ls)\n          echo $files\n"})
        result = hygiene("actionlint", repo)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("shellcheck reported issue", result.stdout)
        self.assertIn("SC2086", result.stdout)

    def test_self_repository_workflow_syntax_passes_but_malformed_workflow_calls_fail(self) -> None:
        child = "on: workflow_call\npermissions: {}\njobs:\n  unit:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n"
        caller = "on: push\npermissions: {}\njobs:\n  unit:\n    uses: {target}\n"
        for name, target, code in (
            ("self", "$/.github/workflows/unit.yml", 0),
            ("missing-ref", "fixture/repo/.github/workflows/unit.yml", 1),
            ("wrong-folder", "$/other/unit.yml", 1),
        ):
            with self.subTest(target=target):
                repo = fixture_repository(self.root / name, {
                    ".github/workflows/caller.yml": caller.replace("{target}", target),
                    ".github/workflows/unit.yml": child,
                })
                result = hygiene("actionlint", repo)
                self.assertEqual(code, result.returncode, result.stdout + result.stderr)

    def test_a_missing_shellcheck_fails_before_actionlint_runs(self) -> None:
        repo = fixture_repository(self.root / "missing", {".github/workflows/a.yml": WORKFLOW_HEAD + "      - run: echo ok\n"})
        partial = self.root / "partial-tools"
        partial.mkdir()
        source = tools_directory()
        actionlint = next(path for path in ([source / "actionlint", source / "actionlint.exe"] if source else [Path(shutil.which("actionlint") or "")]) if path.is_file())
        shutil.copy2(actionlint, partial / actionlint.name)
        result = subprocess.run([sys.executable, str(RESOURCES / "hygiene.py"), "actionlint", "--root", str(repo), "--tools", str(partial)], capture_output=True, text=True, check=False)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("shellcheck is missing from", result.stderr)
        self.assertNotIn("+ ", result.stdout, "actionlint must not run without ShellCheck")


if __name__ == "__main__":
    unittest.main()
