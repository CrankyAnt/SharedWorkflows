"""Every GitHub Actions reference in repository workflows uses a full pin."""

from __future__ import annotations

import unittest

from tests.ci_policy_support import REPOSITORY_ROOT


WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"


class ActionPinsTests(unittest.TestCase):
    def test_every_actions_reference_is_a_full_sha_pin_with_a_version_comment(self) -> None:
        for path in sorted(WORKFLOWS.glob("*.yml")):
            text = path.read_text(encoding="utf-8")
            for line in text.splitlines():
                if "uses: actions/" in line:
                    with self.subTest(workflow=path.name, line=line.strip()):
                        self.assertRegex(line, r"uses: actions/[a-z-]+(?:/[a-z-]+)?@[0-9a-f]{40} # v\d+\.\d+\.\d+$")
