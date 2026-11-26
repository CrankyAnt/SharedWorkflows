"""Structural contract of the shared measurement workflow and the caller template for the management repository."""

from __future__ import annotations

import re
import sys
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, read_workflow, uses_values, workflow_triggers
from tests.workflow_commands import script_invocations

WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "measurement.yml"
SUITE_JOBS = REPOSITORY_ROOT / ".github" / "workflows" / "suite-jobs.yml"
MAINTAINING = REPOSITORY_ROOT / "docs" / "maintaining.md"
PART = "actions/measurement"
CALLER = REPOSITORY_ROOT / PART / "caller.yml"
ACTION = REPOSITORY_ROOT / PART / "action.yml"
SECRET = "MEASUREMENT_APP_PRIVATE_KEY"
TOKEN_ACTION = "actions/create-github-app-token"
SHARED = "CrankyAnt/SharedWorkflows"

sys.path.insert(0, str(REPOSITORY_ROOT / PART))
sys.path.insert(0, str(REPOSITORY_ROOT / "actions" / "suite-selection"))
import measurement as measurement_program  # noqa: E402
import suite_selection  # noqa: E402


def section(text: str, heading: str) -> str:
    """The text below a heading of the document, up to the next heading of the same or a higher level."""
    level = heading.split(" ", 1)[0]
    start = text.index(f"\n{heading}\n") + len(heading) + 2
    following = re.search(rf"(?m)^#{{1,{len(level)}}} ", text[start:])
    return text[start:start + following.start()] if following else text[start:]


class MeasurementWorkflowContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.document = read_workflow(WORKFLOW)
        self.job = self.document["jobs"]["measurement"]

    def test_the_shared_workflow_is_workflow_call_only_with_minimal_permissions_and_pinned_actions(self) -> None:
        self.assertEqual({"workflow_call"}, workflow_triggers(self.text, "measurement.yml"))
        self.assertEqual("{}", self.document.get("permissions"))
        self.assertEqual(["measurement"], list(self.document["jobs"]))
        self.assertEqual({"issues": "write"}, self.job["permissions"])
        self.assertEqual("measurement", self.job["environment"])
        self.assertEqual("${{ github.ref == 'refs/heads/main' }}", self.job["if"])
        self.assertEqual([SECRET], list(self.document["on"]["workflow_call"]["secrets"]))
        self.assertEqual(1, self.text.count(f"secrets.{SECRET}"))
        outside = [value for value in uses_values(self.text, "measurement.yml") if not value.startswith("$/")]
        self.assertEqual([TOKEN_ACTION], [value.split("@")[0] for value in outside])
        for line in self.text.splitlines():
            if "uses:" in line and "$/" not in line:
                with self.subTest(line=line.strip()):
                    self.assertRegex(line, r"uses: actions/[a-z-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$")

    def test_the_token_step_requests_actions_read_only_for_every_installed_repository(self) -> None:
        token = next(step for step in self.job["steps"] if str(step.get("uses", "")).startswith(TOKEN_ACTION))
        self.assertEqual(
            {
                "client-id": "${{ vars.MEASUREMENT_APP_CLIENT_ID }}",
                "private-key": f"${{{{ secrets.{SECRET} }}}}",
                "owner": "${{ github.repository_owner }}",
                "permission-actions": "read",
            },
            token["with"],
        )

    def test_the_workflow_invokes_measurement_py_measure_and_failure(self) -> None:
        invocations = script_invocations(WORKFLOW, "measurement.py")
        self.assertEqual(2, len(invocations))
        measure_options = measurement_program.parser().parse_args(invocations[0])
        self.assertEqual("measure", measure_options.command)
        self.assertFalse(measure_options.local)
        failure_options = measurement_program.parser().parse_args(invocations[1])
        self.assertEqual("failure", failure_options.command)
        failure_step = self.job["steps"][-1]
        self.assertEqual("${{ failure() || cancelled() }}", failure_step["if"])

    def test_step_name_constants_match_suite_jobs_and_assumed_constants_match_suite_selection(self) -> None:
        _, suite_jobs = read_workflow(SUITE_JOBS)
        linux_steps = {step.get("name") for step in suite_jobs["jobs"]["linux"]["steps"] if isinstance(step, dict)}
        self.assertIn(measurement_program.ENTRY_FILE_STEP, linux_steps)
        self.assertIn(measurement_program.PYTHON_LANGUAGE_STEP, linux_steps)
        self.assertEqual(suite_selection.FIXED_COST_SECONDS, measurement_program.assumed_fixed_cost_seconds())
        self.assertEqual(suite_selection.LANGUAGE_STEP_ALLOWANCE_SECONDS, measurement_program.language_step_allowance_seconds())


class MeasurementAppDocumentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.document = MAINTAINING.read_text(encoding="utf-8")
        self.app = section(self.document, "### Measurement App")
        self.environment = section(self.document, "### Set up the measurement environment")
        self.rotation = section(self.document, "### Rotate the Measurement App's private key")
        self.workflow, self.shared = read_workflow(WORKFLOW)

    def test_the_reference_states_the_app_s_permissions_as_the_token_step_asks_for_them(self) -> None:
        rows = dict(re.findall(r"(?m)^\| (Actions|Metadata) \| (read|write) \|", self.app))
        self.assertEqual({"Actions": "read", "Metadata": "read"}, rows)
        self.assertEqual(2, len(re.findall(r"(?m)^\| [A-Z][a-z ]+ \| (?:read|write) \|", self.app)))
        token = next(step for step in self.shared["jobs"]["measurement"]["steps"] if str(step.get("uses", "")).startswith(TOKEN_ACTION))
        asked = {name.removeprefix("permission-").replace("-", " ").capitalize(): level for name, level in token["with"].items() if name.startswith("permission-")}
        self.assertEqual({"Actions": "read"}, asked)

    def test_the_environment_rotation_and_adoption_match_the_workflow(self) -> None:
        self.assertIn("The App is installed on selected repositories only", self.app)
        self.assertIn("Create the environment `measurement`", self.environment)
        self.assertIn("add `main` as the only branch", self.environment)
        for name, reference in (("MEASUREMENT_APP_CLIENT_ID", "vars.MEASUREMENT_APP_CLIENT_ID"), (SECRET, f"secrets.{SECRET}")):
            self.assertIn(f"`{name}`", self.app)
            self.assertIn(f"`{name}`", self.environment)
            self.assertIn(reference, self.workflow)
        self.assertIn(f"gh secret set {SECRET} --env measurement --repo <management repository>", self.rotation)
        tasks = section(self.document, "### Recurring tasks")
        self.assertIn(
            "- The Measurement App: [Set up the measurement environment](#set-up-the-measurement-environment) and "
            "[Rotate the Measurement App's private key](#rotate-the-measurement-apps-private-key).",
            tasks,
        )
        adopt = section(self.document, "### Adopt the pipeline in a consumer")
        self.assertIn("Install the [Measurement App](#measurement-app) on the repository", adopt)


class MeasurementCallerTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.document = read_workflow(CALLER)

    def test_the_caller_schedules_the_measurement_and_calls_the_shared_workflow_at_v1(self) -> None:
        triggers = workflow_triggers(self.text, "caller.yml")
        self.assertEqual({"schedule", "workflow_dispatch"}, triggers)
        self.assertEqual("{}", self.document.get("permissions"))
        self.assertEqual(["measurement"], list(self.document["jobs"]))
        job = self.document["jobs"]["measurement"]
        self.assertEqual({"issues": "write"}, job["permissions"])
        self.assertEqual({SECRET: f"${{{{ secrets.{SECRET} }}}}"}, job["secrets"])
        self.assertEqual(f"{SHARED}/.github/workflows/measurement.yml@v1", job["uses"])
        self.assertNotIn("steps", job)
        self.assertTrue(ACTION.is_file())


if __name__ == "__main__":
    unittest.main()
