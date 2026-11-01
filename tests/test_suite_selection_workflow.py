"""Reusable workflow input, output and permission contract."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.ci_policy_support import entry_pathspecs, read_workflow, tracked_files

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = REPOSITORY_ROOT / ".github/workflows"
# The suite list and the validation workflow with the workflows and local actions that its CI definition follows.
DEFINITION_FILES = ("tests/suites.json", ".github/workflows/validate.yml", ".github/workflows/suite-selection.yml",
                    ".github/workflows/suite-verdict.yml", "actions/suite-selection/action.yml",
                    "actions/recording/action.yml", "actions/python-language-step/action.yml")
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
    def test_select_reads_the_validation_workflow_as_the_selection_workflow_calls_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "outputs.txt"
            checkout = Path(directory) / "checkout"
            fixture_checkout(checkout, DEFINITION_FILES)
            result = subprocess.run(
                [sys.executable, str(REPOSITORY_ROOT / "actions/suite-selection/suite_selection.py"), "select", "--root", ".",
                 "--suites", "tests/suites.json", "--workflow", ".github/workflows/validate.yml", "--language-step", "python",
                 "--changed-file", "actions/python-language-step/python_language_step.py", "--json-output", str(output)],
                cwd=checkout, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('language-steps={"python":"run"}\n', output.read_text(encoding="utf-8"))

class SharedWorkflowTests(unittest.TestCase):
    def test_the_selection_workflow_holds_the_contract(self) -> None:
        workflow = (WORKFLOWS / "suite-selection.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_call:", workflow)
        self.assertRegex(workflow, r"(?m)^      suites:\n        description: .*\n        type: string\n        required: false\n        default: \.github/suites\.json$")
        self.assertRegex(workflow, r"(?m)^    outputs:\n      suites:\n        description: .*\n        value: \$\{\{ jobs\.select\.outputs\.suites \}\}$")
        self.assertIn("permissions: {}", workflow)
        self.assertRegex(workflow, r"(?m)^  select:\n    runs-on: ubuntu-26.04\n    permissions:\n      actions: read\n      contents: read\n      pull-requests: read\n"
                                   r"    outputs:\n      suites: \$\{\{ steps\.select\.outputs\.suites \}\}$")
        self.assertEqual(1, workflow.count("permissions:\n"), "the one job holds the read scopes of the push lookup")
        self.assertNotIn("write", workflow)
        self.assertNotIn("secrets", workflow)
        self.assertEqual(2, workflow.count("github.token"), "only the push lookup and the shared lookup read the API")
        self.assertIn("          GITHUB_TOKEN: ${{ github.token }}\n", workflow)
        self.assertEqual(["$/actions/suite-selection", "$/actions/recording"],
                         [reference for reference in re.findall(r"uses: (\$/\S+)", workflow)])
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("WORKFLOW: ${{ github.workflow_ref }}", workflow)
        self.assertIn("BASE: ${{ github.event_name == 'pull_request' && github.event.pull_request.base.sha || github.event.before }}", workflow)
        self.assertIn("HEAD: ${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}", workflow)
        self.assertIn("        with:\n          ref: ${{ github.event_name == 'repository_dispatch' && github.event.client_payload.sha || '' }}\n          fetch-depth: 0\n", workflow)
        self.assertIn('suite_selection.py" select --root . --suites "$SUITES" --workflow "$WORKFLOW" "$@" --json-output "$GITHUB_OUTPUT" --summary "$GITHUB_STEP_SUMMARY"', workflow)
        self.assertIn('pull_request)\n              set -- "$@" --base "$BASE" --head "$HEAD" --compare merge-base', workflow)
        self.assertIn('*) set -- "$@" --base "$BASE" --head "$HEAD" --compare direct ;;', workflow)

    def test_the_selection_workflow_passes_the_language_steps_and_exposes_the_shards_and_the_windows_job(self) -> None:
        workflow = (WORKFLOWS / "suite-selection.yml").read_text(encoding="utf-8")
        self.assertRegex(workflow, r"(?m)^      language-steps:\n        description: .*\n        type: string\n        required: false\n        default: ''$")
        self.assertIn("LANGUAGE_STEPS: ${{ inputs.language-steps }}", workflow)
        self.assertIn('for step in $LANGUAGE_STEPS; do set -- "$@" --language-step "$step"; done', workflow)
        for output in ("shards", "windows", "language-steps"):
            with self.subTest(output=output):
                self.assertRegex(workflow, rf"(?m)^      {output}:\n        description: .*\n        value: \$\{{\{{ jobs\.select\.outputs\.{output} \}}\}}$")
                self.assertIn(f"      {output}: ${{{{ steps.select.outputs.{output} }}}}\n", workflow)

    def test_both_shared_workflows_check_the_dispatched_commit_with_options_the_selection_accepts(self) -> None:
        import suite_selection

        for name in ("suite-selection.yml", "repository-hygiene.yml"):
            with self.subTest(workflow=name):
                lines = [line for line in (WORKFLOWS / name).read_text(encoding="utf-8").splitlines() if 'suite_selection.py" check-dispatch ' in line]
                self.assertEqual(1, len(lines))
                arguments = shlex.split(re.sub(r'"\$(\w+)"', r"\1", lines[0].split('suite_selection.py" ', 1)[1]))
                options = suite_selection.parse_arguments(arguments)
                self.assertEqual(("check-dispatch", "PAYLOAD", "BRANCH"), (options.command, options.sha, options.branch))

    def test_both_workflows_name_the_step_by_the_scripts_it_loads(self) -> None:
        for name, job in (("suite-selection.yml", "select"), ("suite-verdict.yml", "validation")):
            with self.subTest(workflow=name):
                _, workflow = read_workflow(WORKFLOWS / name)
                (resources,) = [step for step in workflow["jobs"][job]["steps"] if step.get("uses") == "$/actions/suite-selection"]
                self.assertEqual({"name": "Load the scripts of the suite selection from this workflow revision", "id": "resources",
                                  "uses": "$/actions/suite-selection"}, resources)

    def test_select_takes_the_digest_of_the_shared_recording_step_and_compares_it_with_the_base_branch(self) -> None:
        _, workflow = read_workflow(WORKFLOWS / "suite-selection.yml")
        steps = workflow["jobs"]["select"]["steps"]
        (recording,) = [step for step in steps if step.get("uses") == "$/actions/recording"]
        self.assertEqual({"name": "Load the shared recording step from this workflow revision", "id": "recording",
                          "uses": "$/actions/recording"}, recording, "it runs in every caller, this repository included")
        (digest,) = [step for step in steps if step.get("name") == DIGEST_STEP]
        self.assertEqual(("digest", "${{ steps.recording.outputs.path }}"), (digest["id"], digest["env"]["RECORDING"]))
        self.assertNotIn("if", digest)
        self.assertIn('python3 "$RECORDING/recording.py" digest --github-output "$GITHUB_OUTPUT"', digest["run"])
        (lookup,) = [step for step in steps if '" shared-lookup ' in str(step.get("run", ""))]
        self.assertNotIn("if", lookup)
        self.assertEqual({"DIGEST": "${{ steps.digest.outputs.shared-digest }}", "GITHUB_TOKEN": "${{ github.token }}",
                          "WORKFLOW": "${{ github.workflow_ref }}", "RESOURCE_PATH": "${{ steps.resources.outputs.path }}",
                          "BRANCH": "${{ github.event_name == 'pull_request' && github.base_ref || github.event_name == 'repository_dispatch' "
                                    "&& github.event.repository.default_branch || github.ref_name }}",
                          "PUSHED": "${{ (github.event_name == 'push' || github.event_name == 'repository_dispatch') "
                                    "&& (github.event.client_payload.sha || github.sha) || '' }}"}, lookup["env"])
        self.assertIn('suite_selection.py" shared-lookup --root . --digest "$DIGEST" --branch "$BRANCH" --pushed "$PUSHED" --workflow "$WORKFLOW" '
                      '--output "$RUNNER_TEMP/shared-lookup.json" --summary "$GITHUB_STEP_SUMMARY"', lookup["run"])
        names = [step.get("id") for step in steps]
        self.assertLess(names.index("digest"), steps.index(lookup))
        self.assertLess(steps.index(lookup), names.index("select"))
        select = steps[names.index("select")]["run"]
        self.assertIn('set -- "$@" --shared-lookup "$RUNNER_TEMP/shared-lookup.json"', [line.strip() for line in select.splitlines()])
        self.assertEqual("${{ steps.digest.outputs.shared-digest }}", workflow["jobs"]["select"]["outputs"]["shared-digest"])
        self.assertEqual("${{ jobs.select.outputs.shared-digest }}", workflow["on"]["workflow_call"]["outputs"]["shared-digest"]["value"])



if __name__ == "__main__":
    unittest.main()
