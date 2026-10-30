"""Dependabot auto-merge workflow invocation contract."""

import unittest

from tests.dependabot_auto_merge_source import REPOSITORY_ROOT, auto_merge
from tests.workflow_commands import script_invocations

WORKFLOW = REPOSITORY_ROOT / ".github/workflows/dependabot-auto-merge.yml"


class WorkflowInvocationTests(unittest.TestCase):
    def test_the_workflow_invokes_the_script_with_options_it_accepts(self) -> None:
        invocations = script_invocations(WORKFLOW, "auto_merge.py")
        self.assertEqual(len(invocations), 2)
        parsed = [auto_merge.build_parser().parse_args(invocation) for invocation in invocations]
        self.assertEqual(["pull-request", "workflow-run"], [arguments.event for arguments in parsed])
        self.assertEqual(1, parsed[0].number)
        self.assertTrue(parsed[1].head_branch)
        for arguments in parsed:
            self.assertIsNotNone(arguments.summary)


if __name__ == "__main__":
    unittest.main()
