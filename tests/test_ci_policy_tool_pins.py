"""Repository workflows load tool and Node versions only from hygiene pins."""

from __future__ import annotations

import json
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT


WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
PINS = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "pins.json"


class ToolPinsTests(unittest.TestCase):
    def test_no_workflow_holds_a_tool_version_sha256_or_node_literal(self) -> None:
        pins = json.loads(PINS.read_text(encoding="utf-8"))
        versions = [tool["version"] for section in ("tools", "python") for tool in pins.get(section, {}).values()] + [pins["node"]["version"]]
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                for version in versions:
                    self.assertNotIn(version, text)
                self.assertNotRegex(text, r"[0-9a-f]{64}")
                self.assertNotRegex(text, r"node-version: \"\d")
                self.assertNotIn("releases/download", text)
                self.assertNotIn("curl", text)
