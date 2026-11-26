"""Structural contract of the shared measurement workflow and the caller template for the management repository."""

from __future__ import annotations

import sys
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, read_workflow, uses_values, workflow_triggers
from tests.workflow_commands import script_invocations

WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "measurement.yml"
PART = "actions/measurement"
CALLER = REPOSITORY_ROOT / PART / "caller.yml"
ACTION = REPOSITORY_ROOT / PART / "action.yml"
SECRET = "MEASUREMENT_APP_PRIVATE_KEY"
TOKEN_ACTION = "actions/create-github-app-token"
SHARED = "CrankyAnt/SharedWorkflows"

sys.path.insert(0, str(REPOSITORY_ROOT / PART))
import measurement as measurement_program  # noqa: E402


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
