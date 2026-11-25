"""Reusable workflow input, output and permission contract."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

from tests import temporary_support
from tests.ci_policy_support import BARE_DISPATCH_CONTEXT, BARE_DISPATCH_TIP, entry_pathspecs, expression_value, read_workflow, tracked_files
from tests.workflow_commands import script_invocations

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPOSITORY_ROOT / ".github/workflows"
# The suite list with the files of its setups, and the pipeline caller with the workflows and local actions that its CI
# definition follows to the suite jobs.
DEFINITION_FILES = ("tests/suites.json", "tests/setups/python.py", "tests/setups/node.py", "tests/setups/dotnet.py", "tests/setups/pinned-tool.sh",
                    "tests/setups/commitlint.sh", "actions/recording/etw-consumer/global.json", "actions/repository-hygiene/pins.json",
                    ".github/workflows/ci.yml", ".github/workflows/pipeline.yml", ".github/workflows/suite-jobs.yml",
                    "actions/suite-selection/action.yml", "actions/recording/action.yml", "actions/python-language-step/action.yml",
                    "actions/pipeline/action.yml", "actions/repository-hygiene/action.yml", "actions/publication-checks/action.yml",
                    "actions/python-tests/action.yml", "actions/dotnet-test/action.yml",
                    "actions/node-test/action.yml")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "actions/suite-selection"))

from reuse import DIGEST_STEP  # noqa: E402


def fixture_checkout(root: Path, kept: tuple[str, ...]) -> None:
    """A fixture repository with ``kept`` and the tracked entry files of the suite list, empty except ``kept``, so that the
    Git calls of ``select`` read the fixture and not this repository."""
    for name in sorted(tracked_files(REPOSITORY_ROOT, [*kept, *entry_pathspecs(REPOSITORY_ROOT / "tests/suites.json")])):
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes((REPOSITORY_ROOT / name).read_bytes() if name in kept else b"")
    for arguments in (["init", "-q"], ["add", "."], ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                                                       "-c", "commit.gpgsign=false", "commit", "-q", "-m", "fixture"]):
        subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, check=True)


class SelectionReadsTheWorkflowTests(unittest.TestCase):
    def test_select_reads_the_pipeline_caller_as_the_pipeline_s_selection_job_calls_it(self) -> None:
        with temporary_support.Directory() as directory:
            output = Path(directory) / "outputs.txt"
            checkout = Path(directory) / "checkout"
            fixture_checkout(checkout, DEFINITION_FILES)
            result = subprocess.run(
                [sys.executable, str(REPOSITORY_ROOT / "actions/suite-selection/suite_selection.py"), "select", "--root", ".",
                 "--suites", "tests/suites.json", "--workflow", ".github/workflows/ci.yml", "--language-step", "python",
                 "--pinned-tool", "gitleaks", "--pins", "actions/repository-hygiene/pins.json",
                 "--changed-file", "actions/python-language-step/python_language_step.py", "--json-output", str(output),
                 "--unit-decisions", str(Path(directory) / "unit-decisions.json")],
                cwd=checkout, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('language-steps={"python":"run"}\n', output.read_text(encoding="utf-8"))
            import json

            unit_doc = json.loads((Path(directory) / "unit-decisions.json").read_text(encoding="utf-8"))
            self.assertIsInstance(unit_doc["run"], bool)
            self.assertIsInstance(unit_doc["units"], dict)
            self.assertTrue(unit_doc["units"], "unit-decisions names every selected or skipped unit with its reason")
            for decision in unit_doc["units"].values():
                self.assertIsInstance(decision["run"], bool)
                self.assertIsInstance(decision["reason"], str)

class SharedWorkflowTests(unittest.TestCase):
    def test_the_legacy_suite_workflows_and_verdict_command_are_retired(self) -> None:
        import suite_selection

        for name in ("suite-selection.yml", "suite-verdict.yml"):
            with self.subTest(workflow=name):
                self.assertFalse((WORKFLOWS / name).exists(), f"{name} is retired")
        with self.assertRaises(SystemExit):
            suite_selection.parse_arguments(["verdict", "--needs", "needs.json"])

    def test_both_shared_workflows_check_the_dispatched_commit_with_options_the_selection_accepts(self) -> None:
        import suite_selection

        for name in ("pipeline.yml", "repository-hygiene.yml"):
            with self.subTest(workflow=name):
                lines = [line for line in (WORKFLOWS / name).read_text(encoding="utf-8").splitlines() if 'suite_selection.py" check-dispatch ' in line]
                self.assertEqual(1, len(lines))
                arguments = shlex.split(re.sub(r'"\$(\w+)"', r"\1", lines[0].split('suite_selection.py" ', 1)[1]))
                options = suite_selection.parse_arguments(arguments)
                self.assertEqual(("check-dispatch", "PAYLOAD", "BRANCH"), (options.command, options.sha, options.branch))

    def test_every_workflow_invocation_of_suite_selection_uses_options_the_program_accepts(self) -> None:
        import suite_selection

        int_flags = {"--pull-request", "--run-id", "--run-attempt"}
        for name in ("pipeline.yml", "repository-hygiene.yml"):
            for tokens in script_invocations(WORKFLOWS / name, "suite_selection.py"):
                cleaned = []
                for index, token in enumerate(tokens):
                    if token == "$@":
                        continue
                    cleaned.append("1" if index and tokens[index - 1] in int_flags else token)
                with self.subTest(workflow=name, command=cleaned[0]):
                    if cleaned[0] == "select":
                        suite_selection.parse_arguments([cleaned[0], "--base", "a", "--head", "b", *cleaned[1:]])
                    else:
                        suite_selection.parse_arguments(cleaned)



if __name__ == "__main__":
    unittest.main()
