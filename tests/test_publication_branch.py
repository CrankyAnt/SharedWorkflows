from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.publication_branch_check import BranchCheckMixin
from tests.publication_fixtures import REPOSITORY_ROOT

RESOURCE_DIRECTORY = REPOSITORY_ROOT / "actions" / "publication-checks"


class PublicationBranchTests(BranchCheckMixin, unittest.TestCase):
    def test_branch_check_rejects_a_corrupted_specification_before_accepting_a_valid_branch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            for name in ("check-branch.cjs", "verify-spec.cjs", "spec.json", "spec-metadata.json"):
                shutil.copy2(RESOURCE_DIRECTORY / name, fixture / name)
            command = ["node", str(fixture / "check-branch.cjs"), "feat/valid-branch"]
            clean = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(0, clean.returncode, clean.stderr)
            with (fixture / "spec.json").open("ab") as handle:
                handle.write(b" ")
            corrupt = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(0, corrupt.returncode)
            self.assertIn("specification hash mismatch", corrupt.stderr)

    def test_branch_policy_accepts_the_selected_subset_and_rejects_other_prefixes(self) -> None:
        validator = RESOURCE_DIRECTORY / "check-branch.cjs"
        for branch in ["feat/add-check", "fix/issue-1-loader", "hotfix/repair", "chore/docs", "release/issue-1-prepare-release", "release/v1.2.3-rc-1", "release/v1.2.3-rc.1", "research/issue-1-loader-shape", "revert-1-fix-issue-1-loader", "revert-2-feat/issue-1-product", "revert-3-release/v1.2.3-rc.1"]:
            self._run_branch_check(validator, branch, 0)
        for branch in ["feature/add-check", "bugfix/repair", "ai/add-check", "copilot/add-check", "cursor/add-check", "claude/add-check", "codex/add-check", "dependabot/update-lockfile", "feat/has.dot", "research/has.dot", "release/1.2.3", "release/v1.2.3-rc..1", "revert-0-fix-issue-1-loader", "revert-1", "revert-abc-fix-issue-1-loader"]:
            self._run_branch_check(validator, branch, 1)


if __name__ == "__main__":
    unittest.main()
