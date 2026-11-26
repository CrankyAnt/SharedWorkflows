from __future__ import annotations

import unittest

from tests.publication_spec_module import REPOSITORY_ROOT, update_spec

from tests.workflow_commands import script_invocations


class WorkflowInvocationTests(unittest.TestCase):
    def test_the_specification_updater_workflow_invokes_the_script_with_options_it_accepts(self) -> None:
        invocations = script_invocations(REPOSITORY_ROOT / ".github" / "workflows" / "update-publication-spec.yml", "update_spec.py")
        self.assertEqual(len(invocations), 2, "the check step and the apply step")
        plan_args = update_spec.build_parser().parse_args(invocations[0])
        propose_args = update_spec.build_parser().parse_args(invocations[1])
        self.assertEqual((str(plan_args.root), plan_args.candidate, plan_args.no_apply), (".", None, True))
        self.assertIsNotNone(plan_args.write_candidate)
        self.assertIsNotNone(plan_args.output)
        self.assertEqual((str(propose_args.root), propose_args.no_apply), (".", False))
        self.assertIsNotNone(propose_args.candidate, "propose job applies the verified candidate without re-querying")
        self.assertIsNotNone(propose_args.output)


if __name__ == "__main__":
    unittest.main()
