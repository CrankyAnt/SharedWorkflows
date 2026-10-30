"""Text contract of this repository's repository-hygiene caller."""

from __future__ import annotations

import unittest

from tests.ci_policy_support import REPOSITORY_ROOT


WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
CALLER = WORKFLOWS / "repository-hygiene-caller.yml"


class HygieneCallerTests(unittest.TestCase):
    def test_the_own_hygiene_caller_runs_on_pull_requests_and_their_edits_main_pushes_and_dependabot_merges_by_local_path(self) -> None:
        caller = CALLER.read_text(encoding="utf-8")
        self.assertIn("on:\n  pull_request:\n    types: [opened, synchronize, reopened, edited]\n  push:\n    branches: [main]\n"
                      "  repository_dispatch:\n    types: [dependabot-merged]\n\n", caller)
        self.assertIn("permissions: {}", caller)
        self.assertIn("jobs:\n  hygiene:\n    permissions:\n      actions: read\n      contents: read\n      pull-requests: read\n"
                      "    uses: $/.github/workflows/repository-hygiene.yml\n", caller)
        self.assertNotIn("zizmor: ignore[self-repository]", caller)
        self.assertIn("gitleaks-config: .gitleaks.toml", caller)
        self.assertIn("gitleaks-baseline: .gitleaks-baseline.json", caller)
        self.assertNotIn("@v1", caller)
        self.assertNotIn("run:", caller)
        self.assertNotIn("coverage-exclude", caller, "This repository's fixtures are generated in temporary directories, so nothing needs excluding")

    def test_the_publication_checks_workflow_and_its_caller_are_gone(self) -> None:
        for name in ("publication-checks.yml", "publication-checks-caller.yml"):
            with self.subTest(workflow=name):
                self.assertFalse((WORKFLOWS / name).exists())


if __name__ == "__main__":
    unittest.main()
