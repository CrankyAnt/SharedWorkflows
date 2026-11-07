"""The shared workflow of Python tests: its inputs, its one job, the order and conditions of its steps, and the
invocations of its script, parsed with the script's own parser."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

from tests.ci_policy_support import condition_holds, expression_value, read_workflow
from tests.workflow_commands import script_invocations

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "python-tests.yml"
RESOURCES = REPOSITORY_ROOT / "actions" / "python-tests"

sys.path.insert(0, str(RESOURCES))
import python_tests  # noqa: E402

STEPS = ["resources", "selection", "checkout", "dispatch", "lookup", "decide", "setup-python", "install", "language-step", "tests"]


def step_key(step: dict) -> str:
    uses = str(step.get("uses", ""))
    if uses.startswith("actions/checkout@"):
        return "checkout"
    if uses.startswith("actions/setup-python@"):
        return "setup-python"
    if uses == "$/actions/python-language-step":
        return "language-step"
    if step.get("name") == "Look up the tested tree of the pushed commit's pull request":
        return "lookup"
    if step.get("name") == "Run the tests":
        return "tests"
    return str(step.get("id"))


class PythonTestsWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(WORKFLOW)
        self.job = self.workflow["jobs"]["tests"]
        self.steps = {step_key(step): step for step in self.job["steps"]}

    def test_the_inputs_with_their_types_and_defaults(self) -> None:
        inputs = self.workflow["on"]["workflow_call"]["inputs"]
        self.assertEqual(["python-version", "working-directory", "install", "runner-label", "format-check"], list(inputs))
        self.assertEqual(("string", "true"), (inputs["python-version"]["type"], inputs["python-version"]["required"]))
        for name, default in (("working-directory", "."), ("install", ""), ("runner-label", "ubuntu-26.04"), ("format-check", "false")):
            with self.subTest(input=name):
                self.assertEqual(("string", "false", default), (inputs[name]["type"], inputs[name]["required"], inputs[name]["default"]))
                self.assertTrue(inputs[name]["description"])
        self.assertNotIn("secrets", self.workflow["on"]["workflow_call"])

    def test_one_job_tests_on_the_runner_label_with_the_read_rights_of_the_push_lookup(self) -> None:
        self.assertEqual("{}", self.workflow["permissions"])
        self.assertEqual(["tests"], list(self.workflow["jobs"]))
        self.assertEqual("${{ inputs.runner-label }}", self.job["runs-on"])
        self.assertEqual({"actions": "read", "contents": "read", "pull-requests": "read"}, self.job["permissions"])
        self.assertEqual({"run": {"shell": "bash"}}, self.job["defaults"], "Windows runs the same commands")

    def test_the_steps_run_in_their_order(self) -> None:
        self.assertEqual(STEPS, [step_key(step) for step in self.job["steps"]])
        self.assertEqual("$/actions/python-tests", self.steps["resources"]["uses"])
        self.assertEqual("$/actions/suite-selection", self.steps["selection"]["uses"])

    def test_the_checkout_takes_the_full_history_without_credentials_and_the_dispatched_commit(self) -> None:
        checkout = self.steps["checkout"]["with"]
        self.assertEqual(("0", "false"), (str(checkout["fetch-depth"]), str(checkout["persist-credentials"])))
        dispatch = {"github": {"event_name": "repository_dispatch", "event": {"client_payload": {"sha": "a" * 40}}, "sha": "b" * 40}}
        self.assertEqual("a" * 40, expression_value(checkout["ref"], dispatch))
        self.assertEqual("", expression_value(checkout["ref"], {"github": {"event_name": "push", "sha": "b" * 40}}))

    def test_setup_python_installs_the_input_version_as_the_job_s_python_without_a_cache(self) -> None:
        setup = self.steps["setup-python"]
        self.assertEqual({"python-version": "${{ inputs.python-version }}"}, setup["with"])
        self.assertNotIn("actions/cache", self.text)
        self.assertNotRegex(self.text, r"(?m)^\s*cache(?:-dependency-path)?:")
        self.assertNotIn("PIP_CACHE_DIR", self.text)

    def test_the_language_step_runs_from_the_working_directory_with_the_format_check(self) -> None:
        self.assertEqual({"root": "${{ inputs.working-directory }}", "format-check": "${{ inputs.format-check }}"}, self.steps["language-step"]["with"])

    def test_nothing_installs_or_runs_when_the_decision_skips_the_tests(self) -> None:
        values = {"steps.decide.outputs.run": "false", "steps.install.outcome": "skipped"}
        for key in ("setup-python", "install", "language-step", "tests"):
            with self.subTest(step=key):
                self.assertFalse(condition_holds(self.steps[key]["if"], values))

    def test_the_tests_run_also_when_the_language_step_failed_but_not_after_a_failed_installation(self) -> None:
        values = {"steps.decide.outputs.run": "true", "steps.install.outcome": "success"}
        for key in ("setup-python", "install", "language-step", "tests"):
            with self.subTest(step=key):
                self.assertTrue(condition_holds(self.steps[key]["if"], values))
        self.assertIn("!cancelled()", self.steps["tests"]["if"], "a failed language step does not skip the tests")
        self.assertNotIn("cancelled()", self.steps["language-step"]["if"])
        self.assertFalse(condition_holds(self.steps["tests"]["if"], {**values, "steps.install.outcome": "failure"}))

    def test_the_push_lookup_and_the_dispatch_check_run_as_in_the_hygiene_job(self) -> None:
        self.assertEqual("github.event_name == 'push' || github.event_name == 'repository_dispatch'", self.steps["lookup"]["if"])
        self.assertEqual("github.event_name == 'repository_dispatch' && github.event.client_payload.sha", self.steps["dispatch"]["if"])
        self.assertIn('suite_selection.py" lookup --root . --sha "$SHA" --before "$BEFORE" --workflow "$WORKFLOW"', self.steps["lookup"]["run"])
        self.assertIn('--output "$RUNNER_TEMP/push-lookup.json"', self.steps["lookup"]["run"])
        self.assertIn('suite_selection.py" check-dispatch --root . --sha "$PAYLOAD" --branch "$BRANCH"', self.steps["dispatch"]["run"])
        self.assertIn('--lookup "$RUNNER_TEMP/push-lookup.json"', self.steps["decide"]["run"])

    def test_every_step_runs_the_interpreter_of_its_runner_and_keeps_its_byte_code_out_of_the_checkout(self) -> None:
        for key in ("dispatch", "lookup", "decide", "install", "tests"):
            with self.subTest(step=key):
                self.assertEqual("${{ runner.os == 'Windows' && 'python' || 'python3' }}", self.steps[key]["env"]["PYTHON"])
                self.assertIn('PYTHONPYCACHEPREFIX="$RUNNER_TEMP/repo-pycache" "$PYTHON" ', self.steps[key]["run"])

    def test_every_invocation_names_a_subcommand_and_options_the_script_accepts(self) -> None:
        invocations = script_invocations(WORKFLOW, "python_tests.py")
        self.assertEqual(["decide", "install", "test"], [tokens[0] for tokens in invocations])
        for tokens in invocations:
            if tokens[0] == "decide":
                self.assertIn("$@", tokens, "the decision takes the push lookup or the pull request's commits from the case before it")
                rest = [token for token in tokens if token != "$@"]
                for event in (["--lookup", "x"], ["--base", "x", "--head", "x", "--compare", "merge-base"]):
                    with self.subTest(step="decide", event=event):
                        python_tests.parse_arguments([*rest, *event])
                continue
            with self.subTest(step=tokens[0]):
                options = python_tests.parse_arguments(tokens)
                self.assertEqual("x", options.root, "the working directory reaches the script")
        install = python_tests.parse_arguments(invocations[1])
        self.assertEqual("x", install.install, "the value reaches --install with an equals sign, so a leading - stays a value")


if __name__ == "__main__":
    unittest.main()
