from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.publication_branch_check import BranchCheckMixin
from tests.publication_fixtures import PublicationFixture, REPOSITORY_ROOT

RESOURCE_DIRECTORY = REPOSITORY_ROOT / "actions" / "publication-checks"
SPECIFICATION = RESOURCE_DIRECTORY / "spec.json"
SPECIFICATION_METADATA = RESOURCE_DIRECTORY / "spec-metadata.json"


class PublicationAuthorTests(BranchCheckMixin, PublicationFixture):
    def test_author_decision_survives_human_events_and_rejects_branch_impersonation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            decision = temporary_path / "decide-author.cjs"
            workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "repository-hygiene.yml").read_text(encoding="utf-8")
            start = workflow.index("node <<'EOF'") + len("node <<'EOF'")
            end = workflow.index("\n          EOF", start)
            content = textwrap.dedent(workflow[start:end]).lstrip("\n") + "\n"
            decision.write_text(content, encoding="utf-8")
            validator = RESOURCE_DIRECTORY / "check-branch.cjs"
            payload = SPECIFICATION.read_bytes()
            self.assertEqual(json.loads(SPECIFICATION_METADATA.read_text())["sha256"], hashlib.sha256(payload).hexdigest())
            cases = [
                ("dependabot[bot]", "octocat", "dependabot/update-lockfile", "true"),
                ("renovate[bot]", "octocat", "renovate/update-lockfile", "true"),
                ("dependabot[bot]", "dependabot[bot]", "dependabot/update-lockfile", "true"),
                ("octocat", "octocat", "dependabot/update-lockfile", "false"),
                ("octocat", "dependabot[bot]", "dependabot/update-lockfile", "false"),
                ("dependabot", "octocat", "dependabot/update-lockfile", "false"),
            ]
            for index, (author, actor, branch, expected_skip) in enumerate(cases):
                with self.subTest(author=author, actor=actor, branch=branch):
                    output = temporary_path / f"decision-{index}.txt"
                    environment = self.environment.copy()
                    environment.update(PR_AUTHOR=author, ACTOR=actor, GITHUB_ACTOR=actor, HEAD_BRANCH=branch, GITHUB_OUTPUT=str(output))
                    result = subprocess.run(["node", str(decision)], capture_output=True, text=True, env=environment)
                    self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                    self.assertEqual(f"skip={expected_skip}\n", output.read_text(encoding="utf-8"))
                    for check in ["branch", "commits", "pull request title"]:
                        self.assertIn(f"{'Skipping' if expected_skip == 'true' else 'Running'} {check} check", result.stdout)
                    self.assertIn(f"PR author {author}", result.stdout)
                    if expected_skip == "false":
                        self._run_branch_check(validator, branch, 1)


if __name__ == "__main__":
    unittest.main()
