"""Dependabot auto-merge workflow contract."""

import re
import unittest
from pathlib import Path

from tests.dependabot_auto_merge_source import auto_merge

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github/workflows/dependabot-auto-merge.yml"


class WorkflowTests(unittest.TestCase):
    def test_the_reusable_workflow_holds_the_contract(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("on:\n  workflow_call:\n\npermissions: {}\n", workflow)
        self.assertNotIn("inputs:", workflow)
        self.assertNotIn("secrets", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("github.actor", workflow)
        self.assertNotIn("triggering_actor", workflow)
        self.assertNotIn("checkout", workflow.lower())
        self.assertNotIn("curl", workflow)
        self.assertNotIn("fetch-metadata", workflow)
        self.assertNotIn("actions: write", workflow)
        self.assertEqual(["$/actions/dependabot-auto-merge"] * 2, re.findall(r"uses: (\S+)", workflow), "no third-party action runs in either job")
        self.assertEqual(["auto-merge", "merge"], re.findall(r"(?m)^  ([a-z-]+):\n", workflow), "the shared job ids form the second half of the check names")
        self.assertEqual(3, workflow.count("permissions:"))
        self.assertNotRegex(workflow, r"run: .*\$\{\{", "run blocks take their values through env, never inline")
        self.assertEqual(2, workflow.count("GH_TOKEN: ${{ github.token }}"))

    def test_the_pull_request_job_runs_only_for_dependabot_s_pull_requests_with_the_two_write_rights(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertRegex(workflow, r"(?m)^  auto-merge:\n    if: github\.event_name == 'pull_request' && github\.event\.pull_request\.user\.login == 'dependabot\[bot\]'\n    runs-on: ubuntu-slim\n    permissions:\n      contents: write\n      pull-requests: write\n")
        self.assertIn("PR_NUMBER: ${{ github.event.pull_request.number }}", workflow)
        self.assertIn('auto_merge.py" --repository "$GITHUB_REPOSITORY" --summary "$GITHUB_STEP_SUMMARY" pull-request --number "$PR_NUMBER"', workflow)

    def test_the_merge_job_runs_only_after_a_pull_request_run_by_dependabot_or_by_the_updater_app_on_a_dependabot_branch(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertRegex(workflow, r"(?m)^  merge:\n    if: github\.event_name == 'workflow_run' && github\.event\.workflow_run\.event == 'pull_request' && "
                                   r"\(github\.event\.workflow_run\.actor\.login == 'dependabot\[bot\]' \|\| \(github\.event\.workflow_run\.actor\.login == "
                                   r"'crankyant-publication-checks\[bot\]' && startsWith\(github\.event\.workflow_run\.head_branch, 'dependabot/'\)\)\)\n"
                                   r"    runs-on: ubuntu-slim\n    permissions:\n      contents: write\n      pull-requests: read\n    steps:\n")
        self.assertIn(f"'{auto_merge.UPDATER_APP}'", workflow, "the workflow and the decision name the same updater App")
        self.assertIn("HEAD_BRANCH: ${{ github.event.workflow_run.head_branch }}", workflow)
        self.assertIn('auto_merge.py" --repository "$GITHUB_REPOSITORY" --summary "$GITHUB_STEP_SUMMARY" workflow-run --head-branch "$HEAD_BRANCH"', workflow)


if __name__ == "__main__":
    unittest.main()
