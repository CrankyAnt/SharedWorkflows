"""The v1 promotion workflow is constrained by the shared hygiene surface."""

from __future__ import annotations

import json
import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, read_workflow


PROMOTER = REPOSITORY_ROOT / ".github" / "workflows" / "promote-v1.yml"
MAINTAINING = REPOSITORY_ROOT / "docs" / "maintaining.md"
ENVIRONMENT = "promote-v1"
DEPLOY_KEY = "PROMOTE_V1_DEPLOY_KEY"
PROOF_KEY = "PROOF_REPOSITORY_DEPLOY_KEY"
# GitHub's Ed25519 host key, as https://api.github.com/meta lists it under ssh_keys.
GITHUB_HOST_KEY = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
ADMIN_ROLE = {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
DEPLOY_KEYS = {"actor_id": None, "actor_type": "DeployKey", "bypass_mode": "always"}


def ruleset_section() -> str:
    return MAINTAINING.read_text(encoding="utf-8").split("#### The ruleset on v1\n", 1)[1].split("\n#### ", 1)[0]


SSH_OPTIONS = ("-i '$key'", "-o IdentitiesOnly=yes", "-o IdentityAgent=none", "-o UserKnownHostsFile='$hosts'", "-o StrictHostKeyChecking=yes", "-F /dev/null")


class HygienePromotionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(PROMOTER)
        self.jobs = self.workflow["jobs"]

    def step(self, job: str, name: str) -> dict:
        return next(step for step in self.jobs[job]["steps"] if step.get("name") == name)

    def test_the_promotion_runs_decide_candidate_await_and_move_and_each_later_job_waits_for_its_condition(self) -> None:
        self.assertIn("on:\n  push:\n    branches: [main]\n\npermissions: {}\n", self.text)
        self.assertEqual({"group": "promote-v1", "cancel-in-progress": "false"}, self.workflow["concurrency"], "one promotion at a time, and none is cancelled")
        self.assertEqual(["decide", "candidate", "await", "move"], list(self.jobs))
        self.assertNotIn("needs", self.jobs["decide"])
        self.assertEqual(("decide", "needs.decide.outputs.candidate == 'true'"), (self.jobs["candidate"]["needs"], self.jobs["candidate"]["if"]))
        self.assertEqual("[decide, candidate]", self.jobs["await"]["needs"])
        self.assertNotIn("if", self.jobs["await"], "the wait runs only after a candidate job that succeeded")
        self.assertEqual(("[decide, await]", "needs.await.outputs.move == 'true'"), (self.jobs["move"]["needs"], self.jobs["move"]["if"]))
        self.assertNotIn("workflow_run", self.text)
        self.assertNotIn("git ls-remote", self.text)

    def test_no_job_of_the_promotion_workflow_holds_a_write_token(self) -> None:
        self.assertNotIn(": write", self.text, "the tag pushes need no token of the run")
        self.assertEqual({"decide": {"contents": "read", "pull-requests": "read"}, "candidate": {"contents": "read"}, "await": {"actions": "read", "contents": "read"}, "move": {"contents": "read"}},
                         {name: job["permissions"] for name, job in self.jobs.items()})
        self.assertEqual({"ubuntu-slim"}, {job["runs-on"] for job in self.jobs.values()})
        self.assertEqual({"decide": "5", "candidate": "5", "await": "14", "move": "5"}, {name: job["timeout-minutes"] for name, job in self.jobs.items()})
        self.assertEqual(4, self.text.count("persist-credentials: false"))
        self.assertEqual(["actions/checkout", "actions/create-github-app-token", *["actions/checkout"] * 3], [use.split("@")[0] for use in re.findall(r"uses: (\S+)", self.text)])

    def test_the_decision_learns_the_app_s_login_only_for_a_pins_only_commit_and_with_a_metadata_only_token(self) -> None:
        app = self.step("decide", "Identify the updater App")
        self.assertEqual("steps.facts.outputs.pins == 'true'", app["if"])
        self.assertEqual({"client-id": "${{ vars.PUBLICATION_CHECKS_APP_CLIENT_ID }}", "private-key": "${{ secrets.PUBLICATION_CHECKS_APP_PRIVATE_KEY }}",
                          "permission-metadata": "read"}, app["with"], "the token reads the App's own metadata and nothing of a repository")
        facts = self.step("decide", "Read the current v1 tag and the merged pull request")["run"]
        self.assertIn('select(.merge_commit_sha == \\"$COMMIT\\")', facts, "the pull request is the one whose merge commit is the pushed commit")
        self.assertIn('"actions/repository-hygiene/pins.json ") pins=true ;;', facts, "only a commit that changes the pins file alone counts as a pins update")
        self.assertEqual(1, facts.count("pins=true"))

    def test_the_decision_takes_the_three_facts_of_the_pins_exception(self) -> None:
        decision = self.step("decide", "Decide by promotion class whether the candidate tag moves")
        self.assertEqual({"COMMIT": "${{ github.sha }}", "V1": "${{ steps.facts.outputs.v1 }}", "V1_OBJECT": "${{ steps.facts.outputs.v1_object }}",
                          "APP_LOGIN": "${{ steps.app-token.outputs.app-slug && format('{0}[bot]', steps.app-token.outputs.app-slug) || '' }}",
                          "OWNER": "${{ github.repository_owner }}"}, decision["env"])
        for option in ('--pull-request "$RUNNER_TEMP/pull-request.json"', '--app-login "$APP_LOGIN"', '--owner "$OWNER"'):
            with self.subTest(option=option):
                self.assertIn(option, decision["run"])

    def test_each_step_invokes_its_subcommand_with_the_facts_of_the_decision(self) -> None:
        for fragment in ('promote_v1.py read-tag --repository "$GITHUB_REPOSITORY" --output "$GITHUB_OUTPUT"',
                         'promote_v1.py decide --commit "$COMMIT" --v1 "$V1" --v1-object "$V1_OBJECT" --pull-request "$RUNNER_TEMP/pull-request.json" --app-login "$APP_LOGIN" '
                         '--owner "$OWNER" --output "$GITHUB_OUTPUT" --summary "$GITHUB_STEP_SUMMARY"',
                         'promote_v1.py candidate --remote promote --new "$NEW" --summary "$GITHUB_STEP_SUMMARY"',
                         'promote_v1.py start-proof --candidate "$NEW" --work "$RUNNER_TEMP/proof-repository" --output "$GITHUB_OUTPUT" --summary "$GITHUB_STEP_SUMMARY"',
                         'promote_v1.py await --commit "$COMMIT" --old "$OLD" --proof-commit "$PROOF_COMMIT" --repository "$GITHUB_REPOSITORY"',
                         'promote_v1.py move --remote promote --old "$OLD" --new "$NEW" --summary "$GITHUB_STEP_SUMMARY"'):
            with self.subTest(fragment=fragment):
                self.assertEqual(1, self.text.count(fragment))
        wait = self.step("await", "Wait for the CI on main and the proof repository's run")
        self.assertEqual({"GH_TOKEN": "${{ github.token }}", "COMMIT": "${{ needs.decide.outputs.new }}", "OLD": "${{ needs.decide.outputs.old }}",
                          "PROOF_COMMIT": "${{ needs.candidate.outputs.proof-commit }}"}, wait["env"])
        self.assertEqual({"proof-commit": "${{ steps.proof.outputs.proof_commit }}"}, self.jobs["candidate"]["outputs"])
        self.assertEqual({"move": "${{ steps.outcome.outputs.move }}"}, self.jobs["await"]["outputs"])

    def test_only_the_candidate_job_and_the_move_job_read_a_deploy_key_and_only_in_the_promotion_environment(self) -> None:
        self.assertEqual({"candidate": ENVIRONMENT, "move": ENVIRONMENT}, {name: job["environment"] for name, job in self.jobs.items() if "environment" in job},
                         "the decision and the wait run without the keys' environment")
        self.assertEqual({"PUBLICATION_CHECKS_APP_PRIVATE_KEY": 1, DEPLOY_KEY: 2, PROOF_KEY: 1},
                         {name: self.text.count(f"secrets.{name}") for name in set(re.findall(r"secrets\.(\w+)", self.text))})
        decide = self.text.split("\n  candidate:\n", 1)[0]
        self.assertNotIn(DEPLOY_KEY, decide)
        self.assertNotIn(PROOF_KEY, decide)
        self.assertNotIn("PUBLICATION_CHECKS_APP_PRIVATE_KEY", self.text.split("\n  candidate:\n", 1)[1], "the App's key is read by the decision alone")
        for name in ("candidate", "move"):
            for step in self.jobs[name]["steps"]:
                with self.subTest(job=name, step=step.get("name")):
                    self.assertNotIn("GH_TOKEN", step.get("env", {}), "a job with a key holds no token: the keys alone push")
                    self.assertNotIn("github.token", str(step))
                    self.assertNotIn("credential.helper", str(step))
        self.assertEqual({"DEPLOY_KEY": f"${{{{ secrets.{DEPLOY_KEY} }}}}", "NEW": "${{ needs.decide.outputs.new }}"}, self.step("candidate", "Move the candidate tag")["env"])
        self.assertEqual({"PROOF_KEY": f"${{{{ secrets.{PROOF_KEY} }}}}", "NEW": "${{ needs.decide.outputs.new }}"},
                         self.step("candidate", "Start the proof repository's run on the candidate tag")["env"])
        push = self.jobs["move"]["steps"][-1]
        self.assertEqual("Move v1 under a lease on its previous commit", push["name"])
        self.assertEqual({"DEPLOY_KEY": f"${{{{ secrets.{DEPLOY_KEY} }}}}", "OLD": "${{ needs.decide.outputs.old }}", "NEW": "${{ needs.decide.outputs.new }}"}, push["env"])

    def test_each_push_goes_over_ssh_with_one_key_alone_and_a_pinned_host_key(self) -> None:
        pushes = {"the candidate tag": (self.step("candidate", "Move the candidate tag"), "DEPLOY_KEY", True),
                  "the proof repository": (self.step("candidate", "Start the proof repository's run on the candidate tag"), "PROOF_KEY", False),
                  "v1": (self.jobs["move"]["steps"][-1], "DEPLOY_KEY", True)}
        for name, (step, variable, own) in pushes.items():
            run = step["run"]
            with self.subTest(push=name):
                self.assertEqual(own, 'git remote add promote "git@github.com:$GITHUB_REPOSITORY.git"' in run, "the tags go to the repository of the run, over SSH")
                self.assertIn(f"printf '%s\\n' '{GITHUB_HOST_KEY}' > \"$hosts\"", run)
                for option in SSH_OPTIONS:
                    self.assertIn(option, run)
                self.assertRegex(run, r"(?m)^export GIT_SSH_COMMAND=\"ssh ")
                self.assertIn(f"(umask 077 && printf '%s\\n' \"${variable}\" > \"$key\")", run, "the key file is readable by the job's user only")
                self.assertEqual([variable], re.findall(r"\$\{?([A-Z_]*KEY)\b", run), "the step writes its own key and no other")
                self.assertIn("trap 'rm -f \"$key\"' EXIT", run)
                self.assertNotIn("set -x", run)
                self.assertNotIn("echo", run, "the key is never printed")
        self.assertNotEqual(*(re.search(r'key="(\S+)"', step["run"]).group(1) for step in self.jobs["candidate"]["steps"][1:]), "the two keys of the candidate job lie in two files")

    def test_the_promotion_workflow_names_only_this_repository(self) -> None:
        self.assertEqual(["$GITHUB_REPOSITORY"] * 2, re.findall(r"\brepos/([^/\s\"]+)", self.text), "every API path takes the repository of the run")
        self.assertEqual(2, self.text.count('--repository "$GITHUB_REPOSITORY"'), "read-tag and await read the repository of the run")
        self.assertEqual(["$GITHUB_REPOSITORY.git"] * 2, re.findall(r"git@github\.com:(\S+?)\"", self.text))
        self.assertNotIn("CrankyAnt/", self.text, "no repository is named by its slug: the record of the shared surface names the proof repository")
        for reach in ("dispatches", "repository_dispatch", "workflow_dispatch", "gh repo ", "gh search", "--repo ", " -R ", "repositories:", "owner:", "repository:"):
            with self.subTest(reach=reach):
                self.assertNotIn(reach, self.text, "nothing lists another repository or sends one an event")


class TagRulesetDocumentTests(unittest.TestCase):
    """The maintenance procedure states the ruleset on ``refs/tags/v1`` and the actors that move ``v1``."""

    def test_the_documented_ruleset_restricts_updates_and_deletions_of_the_tag_v1(self) -> None:
        blocks = re.findall(r"```json\n(.*?)\n```", ruleset_section(), re.DOTALL)
        self.assertEqual(1, len(blocks))
        ruleset = json.loads(blocks[0])
        self.assertEqual(("tag", "active"), (ruleset["target"], ruleset["enforcement"]))
        self.assertEqual({"ref_name": {"include": ["refs/tags/v1"], "exclude": []}}, ruleset["conditions"])
        self.assertEqual([{"type": "update"}, {"type": "deletion"}], ruleset["rules"])

    def test_the_bypass_list_holds_only_the_two_actors_that_move_v1(self) -> None:
        section = ruleset_section()
        ruleset = json.loads(re.search(r"```json\n(.*?)\n```", section, re.DOTALL).group(1))
        self.assertEqual([ADMIN_ROLE, DEPLOY_KEYS], ruleset["bypass_actors"])
        self.assertIn("| The automatic move of `promote-v1.yml` | the deploy key of the environment `promote-v1` | `DeployKey` |", section)
        self.assertIn("| A move by hand with `promote` | the repository owner's own login | `RepositoryRole` 5, the admin role |", section)
        self.assertEqual(2, len(re.findall(r"(?m)^\| (?:The automatic move|A move by hand)", section)), "one row per actor")
        self.assertIn("--jq '{target, enforcement, conditions, rules, bypass_actors}'", section, "the command that reads the ruleset back")

    def test_the_section_names_the_environment_and_the_secret_that_the_move_job_uses(self) -> None:
        section = ruleset_section()
        _, workflow = read_workflow(PROMOTER)
        self.assertIn(f"`{workflow['jobs']['move']['environment']}`", section)
        self.assertIn(f"`{DEPLOY_KEY}`", section)
        self.assertIn("deployment branches are limited to `main`", section)
        self.assertIn("the only deploy key of this repository", section)


if __name__ == "__main__":
    unittest.main()
