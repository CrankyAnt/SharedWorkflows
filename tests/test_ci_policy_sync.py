"""Text contract of the shared workflow of the sync: it is only called, takes its repositories from the App's
installation and names none, pins its actions to full commits, reads the key only in an environment on main, and gives
the App's token to the sync program alone."""

from __future__ import annotations

import re
import sys
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, ci_definition, read_workflow, tracked_files, uses_values, workflow_triggers
from tests.workflow_commands import script_invocations

WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "sync.yml"
PART = "actions/sync"
SECRET = "SYNC_APP_PRIVATE_KEY"
TOKEN_ACTION = "actions/create-github-app-token"
TOKEN_PERMISSIONS = ("permission-contents", "permission-issues", "permission-pull-requests", "permission-workflows")
SHARED = "CrankyAnt/SharedWorkflows"
# The ways a file names a repository: one of the owner by its name, one in an address of the API or of GitHub, and one
# in a `uses:` value. A placeholder such as <owner>/<repository> or $GITHUB_REPOSITORY names none.
OWNED = re.compile(r"\bCrankyAnt/[\w-]+(?:\.[\w-]+)*")
ADDRESSED = re.compile(r"(?:\brepos/|github\.com/)([A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*)")
USED = re.compile(r"(?m)^\s*(?:- )?uses:\s*([A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*)")
LISTED = re.compile(r"(?m)^\s*repositor(?:y|ies):\s*(\S.*)$")

sys.path.insert(0, str(REPOSITORY_ROOT / PART))
import sync as sync_program  # noqa: E402


def sync_problems(text: str, document: dict) -> list[str]:
    """Each way the shared workflow of the sync breaks its rules."""
    problems = []
    if workflow_triggers(text, "sync.yml") != {"workflow_call"}:
        problems.append("the workflow starts on another event than workflow_call")
    if document.get("permissions") != "{}":
        problems.append("the workflow does not set permissions: {} at the top")
    jobs = document.get("jobs") or {}
    if list(jobs) != ["sync"]:
        problems.append("the workflow does not hold the one job sync")
    for name, job in jobs.items():
        steps = job.get("steps") or []
        reads_key = SECRET in str(job)
        if reads_key and job.get("environment") != "sync":
            problems.append(f"the job {name} reads the key outside the environment sync")
        if reads_key and "github.ref == 'refs/heads/main'" not in str(job.get("if", "")):
            problems.append(f"the job {name} reads the key on another ref than main")
        if job.get("permissions") != {"issues": "write"}:
            problems.append(f"the job {name} holds another permission than issues: write")
        for step in steps:
            uses = str(step.get("uses", ""))
            inputs = step.get("with") or {}
            if uses and not uses.startswith("$/") and not re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", uses):
                problems.append(f"the job {name} uses {uses} without a full commit pin")
            if uses.startswith(TOKEN_ACTION + "@"):
                if "repositories" in inputs:
                    problems.append("the token step names repositories")
                if inputs.get("owner") != "${{ github.repository_owner }}":
                    problems.append("the token step does not take the installation of the repository's owner")
                if any(name.startswith("permission-") and name not in TOKEN_PERMISSIONS for name in inputs) or not all(name in inputs for name in TOKEN_PERMISSIONS):
                    problems.append("the token step does not ask for exactly the permissions of the sync App")
            elif SECRET in str(step):
                problems.append(f"a step of the job {name} other than the token step reads the key")
            if "steps.app-token.outputs.token" in str(step) and "sync.py\" sync " not in str(step.get("run", "")):
                problems.append(f"a step of the job {name} other than the sync gets the App's token")
    return problems


def named_repositories(text: str) -> set[str]:
    """The repositories that a file names, apart from the actions of GitHub that it uses."""
    names = {*OWNED.findall(text), *ADDRESSED.findall(text), *USED.findall(text), *LISTED.findall(text)}
    return {name for name in names if not name.startswith("actions/")}


class SyncWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.document = read_workflow(WORKFLOW)
        self.job = self.document["jobs"]["sync"]

    def test_the_workflow_follows_its_rules(self) -> None:
        self.assertEqual([], sync_problems(self.text, self.document))

    def test_the_key_is_a_secret_of_the_call_that_only_the_token_step_reads_in_the_environment_sync_on_main(self) -> None:
        self.assertEqual([SECRET], list(self.document["on"]["workflow_call"]["secrets"]))
        self.assertEqual("sync", self.job["environment"])
        self.assertEqual("${{ github.ref == 'refs/heads/main' }}", self.job["if"])
        self.assertEqual(1, self.text.count(f"secrets.{SECRET}"))
        token = next(step for step in self.job["steps"] if str(step.get("uses", "")).startswith(TOKEN_ACTION))
        self.assertEqual({"client-id": "${{ vars.SYNC_APP_CLIENT_ID }}", "private-key": f"${{{{ secrets.{SECRET} }}}}", "owner": "${{ github.repository_owner }}",
                          "permission-contents": "write", "permission-issues": "read", "permission-pull-requests": "write", "permission-workflows": "write"},
                         token["with"], "without repositories the token reaches every repository of the installation, with the App's permissions and no other")

    def test_every_action_is_pinned_to_a_full_commit_with_a_version_comment(self) -> None:
        outside = [value for value in uses_values(self.text, "sync.yml") if not value.startswith("$/")]
        self.assertEqual([TOKEN_ACTION], [value.split("@")[0] for value in outside])
        for line in self.text.splitlines():
            if "uses:" in line and "$/" not in line:
                with self.subTest(line=line.strip()):
                    self.assertRegex(line, r"uses: actions/[a-z-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$")

    def test_no_file_of_the_sync_names_a_repository_but_the_one_of_the_shared_parts(self) -> None:
        self.assertEqual(set(), named_repositories(self.text))
        files = sorted(tracked_files(REPOSITORY_ROOT, [PART]))
        self.assertIn(f"{PART}/consumer-kit/sync.yml", files)
        for path in files:
            with self.subTest(file=path):
                self.assertLessEqual(named_repositories((REPOSITORY_ROOT / path).read_text(encoding="utf-8")), {SHARED})

    def test_the_sync_takes_its_repositories_from_the_installation(self) -> None:
        (arguments,) = script_invocations(WORKFLOW, "sync.py")[:1]
        options = sync_program.parser().parse_args(arguments)
        self.assertEqual("sync", options.command)
        self.assertFalse(options.local)
        self.assertIsNone(options.repository)
        self.assertTrue(options.own_repository)
        self.assertIn("api.pages(\"installation/repositories\", \"repositories\")", (REPOSITORY_ROOT / PART / "sync.py").read_text(encoding="utf-8"))

    def test_a_failed_run_opens_its_issue_with_the_token_of_the_run(self) -> None:
        failure = self.job["steps"][-1]
        self.assertEqual("${{ failure() }}", failure["if"])
        self.assertEqual("${{ github.token }}", failure["env"]["GITHUB_TOKEN"])
        self.assertNotIn("GH_TOKEN", failure["env"])
        options = sync_program.parser().parse_args(script_invocations(WORKFLOW, "sync.py")[1])
        self.assertEqual("failure", options.command)

    def test_the_program_loads_from_the_workflow_s_own_revision_and_one_run_at_a_time(self) -> None:
        self.assertEqual("$/actions/sync", self.job["steps"][0]["uses"])
        self.assertEqual({"group": "sync", "cancel-in-progress": "false"}, self.document["concurrency"])
        self.assertEqual("ubuntu-slim", self.job["runs-on"])

    def test_a_workflow_that_breaks_a_rule_is_named(self) -> None:
        cases = (
            ("    environment: sync\n", "", ["the job sync reads the key outside the environment sync"]),
            ("    if: ${{ github.ref == 'refs/heads/main' }}\n", "", ["the job sync reads the key on another ref than main"]),
            ("      issues: write\n", "      issues: write\n      contents: write\n", ["the job sync holds another permission than issues: write"]),
            ("          permission-contents: write\n", "          permission-contents: write\n          repositories: one-repository\n",
             ["the token step names repositories"]),
            ("          permission-contents: write\n", "          permission-contents: write\n          permission-administration: write\n",
             ["the token step does not ask for exactly the permissions of the sync App"]),
            ("@64ab729a6294000f5aa4a4bc0b73d1f32bc45510 # v3.3.0", "@v3", [f"the job sync uses {TOKEN_ACTION}@v3 without a full commit pin"]),
            ("  workflow_call:\n", "  workflow_dispatch:\n  workflow_call:\n", ["the workflow starts on another event than workflow_call"]),
            ("          GITHUB_TOKEN: ${{ github.token }}\n          RESOURCE_PATH: ${{ steps.resources.outputs.path }}\n        run: PYTHONPYCACHEPREFIX=\"$RUNNER_TEMP/repo-pycache\" python3 \"$RESOURCE_PATH/sync.py\" failure",
             "          GITHUB_TOKEN: ${{ steps.app-token.outputs.token }}\n          RESOURCE_PATH: ${{ steps.resources.outputs.path }}\n        run: PYTHONPYCACHEPREFIX=\"$RUNNER_TEMP/repo-pycache\" python3 \"$RESOURCE_PATH/sync.py\" failure",
             ["a step of the job sync other than the sync gets the App's token"]),
        )
        for old, new, problems in cases:
            with self.subTest(problems=problems):
                self.assertIn(old, self.text)
                changed = self.text.replace(old, new, 1)
                self.assertEqual(problems, sync_problems(changed, ci_definition().read_yaml(changed, "a changed sync.yml")))

    def test_a_named_repository_is_found(self) -> None:
        for line in ("        run: gh api repos/example-owner/consumer/pulls\n", "          repositories: example-owner/consumer\n",
                     "    uses: example-owner/consumer/.github/workflows/ci.yml@v1\n", "See https://github.com/example-owner/consumer for it.\n"):
            with self.subTest(line=line):
                self.assertEqual({"example-owner/consumer"}, named_repositories(line))
        self.assertEqual({"CrankyAnt/Invented"}, named_repositories("The repository CrankyAnt/Invented.\n"))
        self.assertEqual(set(), named_repositories("      - uses: actions/checkout@" + "a" * 40 + " # v1.0.0\n        run: gh api \"repos/$GITHUB_REPOSITORY/issues\"\n"))


if __name__ == "__main__":
    unittest.main()
