from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.hygiene_fixtures import fixture_repository, tools_available
from tests.hygiene_process import hygiene
from tests.zizmor_fixtures import CHECKOUT_SHA, WORKFLOW_HEAD


@unittest.skipUnless(tools_available("zizmor"), "the pinned tools are neither in HYGIENE_TOOLS nor on PATH")
class CheckRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_the_persona_input_selects_the_auditor_persona(self) -> None:
        repo = fixture_repository(self.root / "persona", {".github/workflows/steps.yml": "name: fixture\non: push\npermissions: {}\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n"})
        self.assertEqual(0, hygiene("zizmor", repo).returncode, "the default persona accepts the fixture")
        self.assertEqual(0, hygiene("zizmor", repo, "--persona", "").returncode, "an empty input leaves the default persona")
        result = hygiene("zizmor", repo, "--persona", "auditor")
        self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("--persona auditor", result.stdout)

    def test_zizmor_does_not_audit_dependabot_configuration(self) -> None:
        repo = fixture_repository(self.root / "dependabot", {
            ".github/workflows/steps.yml": WORKFLOW_HEAD + CHECKOUT_SHA,
            ".github/dependabot.yml": "version: 2\nupdates:\n  - package-ecosystem: github-actions\n    directory: /\n    schedule:\n      interval: weekly\n",
        })
        result = hygiene("zizmor", repo)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("dependabot", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
