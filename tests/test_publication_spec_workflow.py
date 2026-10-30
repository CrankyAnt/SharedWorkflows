from __future__ import annotations

import unittest

from tests.publication_spec_module import REPOSITORY_ROOT, update_spec

from tests.workflow_commands import script_invocations


class WorkflowInvocationTests(unittest.TestCase):
    def test_the_specification_updater_workflow_invokes_the_script_with_options_it_accepts(self) -> None:
        invocations = script_invocations(REPOSITORY_ROOT / ".github" / "workflows" / "update-publication-spec.yml", "update_spec.py")
        self.assertEqual(len(invocations), 2, "the check step and the refresh step")
        for tokens in invocations:
            arguments = update_spec.build_parser().parse_args(tokens)
            self.assertEqual((str(arguments.root), arguments.candidate), (".", None))
            self.assertIsNotNone(arguments.output)


if __name__ == "__main__":
    unittest.main()
