"""Workflow YAML parsing on in-memory fixtures."""

from __future__ import annotations

import unittest

from tests.suite_selection_fixtures import FIXTURE_UNIT
import sys
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "actions/suite-selection"
sys.path.insert(0, str(TOOL))

import ci_definition

class WorkflowReaderTests(unittest.TestCase):
    def test_the_reader_handles_mappings_sequences_inline_items_block_scalars_quotes_and_comments(self) -> None:
        document = ci_definition.read_yaml(FIXTURE_UNIT)
        self.assertEqual(document["on"]["workflow_call"]["inputs"]["suites"], {"type": "string", "required": "true"})
        steps = document["jobs"]["unit"]["steps"]
        self.assertEqual(steps[0], {"uses": "actions/checkout@0000000000000000000000000000000000000000"})
        self.assertEqual(steps[1]["with"], {"settings": "config\\settings.toml"})
        self.assertEqual(steps[2]["run"], 'cp "$GITHUB_WORKSPACE/ci/pipeline.yml" "$RUNNER_TEMP/"\npython -m unittest')
        self.assertEqual(steps[2]["working-directory"], "./checks")
        document = ci_definition.read_yaml('key: "quoted # not a comment"\nlist:\n  - a\n  -\n    b: 1\n    c: |-\n      x\n      y\n  - - nested\n')
        self.assertEqual(document, {"key": "quoted # not a comment", "list": ["a", {"b": "1", "c": "x\ny"}, ["nested"]]})
        with self.assertRaises(ci_definition.WorkflowReadError):
            ci_definition.read_yaml("a:\n  b:\n    broken line\n")



if __name__ == "__main__":
    unittest.main()
