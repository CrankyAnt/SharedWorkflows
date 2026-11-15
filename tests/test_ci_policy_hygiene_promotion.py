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
# GitHub's Ed25519 host key, as https://api.github.com/meta lists it under ssh_keys.
GITHUB_HOST_KEY = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
ADMIN_ROLE = {"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}
DEPLOY_KEYS = {"actor_id": None, "actor_type": "DeployKey", "bypass_mode": "always"}


def ruleset_section() -> str:
    return MAINTAINING.read_text(encoding="utf-8").split("#### The ruleset on v1\n", 1)[1].split("\n#### ", 1)[0]


class HygienePromotionTests(unittest.TestCase):
    def test_no_job_of_the_promotion_workflow_holds_a_write_token(self) -> None:
        promoter = PROMOTER.read_text(encoding="utf-8")
        self.assertIn("on:\n  push:\n    branches: [main]\n    paths: [actions/repository-hygiene/pins.json]\n\n", promoter)
        self.assertNotIn(": write", promoter, "the tag push needs no token of the run")
        self.assertRegex(promoter, r"(?m)^  move:\n    needs: decide\n    if: needs\.decide\.outputs\.move == 'true'\n    runs-on: ubuntu-slim\n    environment: promote-v1\n    permissions:\n      contents: read$")
        self.assertRegex(promoter, r"(?m)^  decide:\n    runs-on: ubuntu-slim\n    timeout-minutes: 14\n    permissions:\n      actions: read\n      contents: read\n      pull-requests: read$")
        self.assertEqual(1, promoter.count("actions: read"))
        self.assertNotIn("workflow_run", promoter)
        self.assertIn("permission-metadata: read", promoter)
        self.assertIn('promote_v1.py decide --commit "$COMMIT" --v1 "$V1"', promoter)
        self.assertIn('--repository "$GITHUB_REPOSITORY" --output "$GITHUB_OUTPUT" --summary "$GITHUB_STEP_SUMMARY"', promoter)
        self.assertIn('promote_v1.py move --remote promote --old "$OLD" --new "$NEW"', promoter)
        self.assertEqual(2, promoter.count("persist-credentials: false"))
        self.assertIn('promote_v1.py read-tag --repository "$GITHUB_REPOSITORY" --output "$GITHUB_OUTPUT"', promoter)
        self.assertNotIn("git ls-remote", promoter)

    def test_only_the_move_job_reads_the_deploy_key_and_only_in_the_promotion_environment(self) -> None:
        text, workflow = read_workflow(PROMOTER)
        jobs = workflow["jobs"]
        self.assertEqual(ENVIRONMENT, jobs["move"]["environment"])
        self.assertNotIn("environment", jobs["decide"], "the decision runs without the key's environment")
        self.assertEqual({"contents": "read"}, jobs["move"]["permissions"])
        self.assertEqual({"PUBLICATION_CHECKS_APP_PRIVATE_KEY": 1, DEPLOY_KEY: 1}, {name: text.count(f"secrets.{name}") for name in set(re.findall(r"secrets\.(\w+)", text))})
        decide, move = text.split("\n  move:\n", 1)
        self.assertNotIn(DEPLOY_KEY, decide)
        self.assertNotIn("github.token", move, "the move holds no token: the deploy key alone pushes")
        self.assertNotIn("GH_TOKEN", move)
        self.assertNotIn("credential.helper", move)
        push = jobs["move"]["steps"][-1]
        self.assertEqual("Move v1 under a lease on its previous commit", push["name"])
        self.assertEqual({"DEPLOY_KEY": f"${{{{ secrets.{DEPLOY_KEY} }}}}", "OLD": "${{ needs.decide.outputs.old }}", "NEW": "${{ needs.decide.outputs.new }}"}, push["env"])

    def test_the_move_pushes_over_ssh_to_this_repository_with_that_key_alone_and_a_pinned_host_key(self) -> None:
        _, workflow = read_workflow(PROMOTER)
        run = workflow["jobs"]["move"]["steps"][-1]["run"]
        self.assertIn('git remote add promote "git@github.com:$GITHUB_REPOSITORY.git"', run, "the target is the repository of the run, over SSH")
        self.assertIn(f"printf '%s\\n' '{GITHUB_HOST_KEY}' > \"$hosts\"", run)
        for option in ("-i '$key'", "-o IdentitiesOnly=yes", "-o IdentityAgent=none", "-o UserKnownHostsFile='$hosts'", "-o StrictHostKeyChecking=yes", "-F /dev/null"):
            with self.subTest(option=option):
                self.assertIn(option, run)
        self.assertRegex(run, r"(?m)^export GIT_SSH_COMMAND=\"ssh ")
        self.assertIn("umask 077", run, "the key file is readable by the job's user only")
        self.assertIn("trap 'rm -f \"$key\"' EXIT", run)
        self.assertNotIn("set -x", run)
        self.assertNotIn("echo", run, "the key is never printed")

    def test_the_promotion_workflow_reaches_and_names_only_this_repository(self) -> None:
        promoter = PROMOTER.read_text(encoding="utf-8")
        self.assertEqual(["$GITHUB_REPOSITORY"] * 2, re.findall(r"\brepos/([^/\s\"]+)", promoter), "every API path takes the repository of the run")
        self.assertEqual(2, promoter.count('--repository "$GITHUB_REPOSITORY"'), "read-tag and decide read the repository of the run")
        self.assertEqual(["$GITHUB_REPOSITORY.git"], re.findall(r"git@github\.com:(\S+?)\"", promoter))
        self.assertNotIn("CrankyAnt/", promoter, "no repository is named by its slug")
        for reach in ("dispatches", "repository_dispatch", "gh repo ", "gh search", "--repo ", " -R ", "repositories:", "owner:", "repository:"):
            with self.subTest(reach=reach):
                self.assertNotIn(reach, promoter, "nothing lists, reads or writes another repository")
        self.assertEqual({"actions/checkout", "actions/create-github-app-token"}, {use.split("@")[0] for use in re.findall(r"uses: (\S+)", promoter)})


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
