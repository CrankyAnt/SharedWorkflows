"""Dependabot auto-merge decisions, update metadata, GitHub calls and command-line behavior over fixture inputs.

The seams are ``auto_merge.run_pull_request`` and ``auto_merge.run_workflow_run`` against a fake GitHub client that
records every call, the metadata reader and the decision helpers over fixture inputs, and the command line. The
expected metadata of three Dependabot pull requests is what ``dependabot/fetch-metadata`` v3.1.0 yields for them.
"""

from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.dependabot_auto_merge_source import auto_merge


PATCH = "version-update:semver-patch"
MINOR = "version-update:semver-minor"
MAJOR = "version-update:semver-major"
BOT = "dependabot[bot]"
REPOSITORY = "CrankyAnt/Fixture"
URL = "https://github.com/CrankyAnt/Fixture/pull/7"
HEAD = "a" * 40
MERGED = "b" * 40
BRANCH = "dependabot/npm_and_yarn/lodash-1.0.1"
REQUIRED_CHECKS = [{"type": "required_status_checks", "parameters": {"required_status_checks": [{"context": "hygiene / hygiene"}], "strict_required_status_checks_policy": False}}]
SIGNED_OFF = "\n\nSigned-off-by: dependabot[bot] <support@github.com>"
UPDATER = "crankyant-publication-checks[bot]"
THIS_REPOSITORY = "CrankyAnt/SharedWorkflows"
FIRST = "d" * 40
REBUILD = "chore: rebuild the ETW consumer"
CONSUMER_BUILD = "actions/recording/etw-consumer/build/"
REBUILT = [f"{CONSUMER_BUILD}EtwConsumer.dll", f"{CONSUMER_BUILD}EtwConsumer.deps.json", "tests/suites.json"]

# The first commit's message, the head branch and the title of three Dependabot pull requests.
PR_SETUP_NODE = ("chore(deps): bump actions/setup-node from 6.5.0 to 7.1.0\n\nBumps [actions/setup-node](https://github.com/actions/setup-node) from 6.5.0 to 7.1.0.\n"
          "- [Release notes](https://github.com/actions/setup-node/releases)\n"
          "- [Commits](https://github.com/actions/setup-node/compare/249970729cb0ef3589644e2896645e5dc5ba9c38...949feb2413d6458794dcd2491c4babbbce0c15c1)\n\n"
          "---\nupdated-dependencies:\n- dependency-name: actions/setup-node\n  dependency-version: 7.1.0\n  dependency-type: direct:production\n"
          "  update-type: version-update:semver-major\n..." + SIGNED_OFF,
          "dependabot/github_actions/actions/setup-node-7.1.0", "chore(deps): bump actions/setup-node from 6.5.0 to 7.1.0")
PR_SETUP_PYTHON = ("chore(deps): bump actions/setup-python from 6 to 7\n\nBumps [actions/setup-python](https://github.com/actions/setup-python) from 6 to 7.\n"
          "- [Release notes](https://github.com/actions/setup-python/releases)\n- [Commits](https://github.com/actions/setup-python/compare/v6...v7)\n\n"
          "---\nupdated-dependencies:\n- dependency-name: actions/setup-python\n  dependency-version: '7'\n  dependency-type: direct:production\n"
          "  update-type: version-update:semver-major\n..." + SIGNED_OFF,
          "dependabot/github_actions/actions/setup-python-7", "chore(deps): bump actions/setup-python from 6 to 7")
PR_FAST_URI = ("chore(deps): bump fast-uri in /actions/publication-checks\n\nBumps [fast-uri](https://github.com/fastify/fast-uri) from 3.1.8 to 3.1.9.\n"
          "- [Release notes](https://github.com/fastify/fast-uri/releases)\n- [Commits](https://github.com/fastify/fast-uri/compare/v3.1.8...v3.1.9)\n\n"
          "---\nupdated-dependencies:\n- dependency-name: fast-uri\n  dependency-version: 3.1.9\n  dependency-type: indirect\n..." + SIGNED_OFF,
          "dependabot/npm_and_yarn/actions/publication-checks/fast-uri-3.1.9", "chore(deps): bump fast-uri from 3.1.8 to 3.1.9 in /actions/publication-checks")


def message(*entries: str, intro: str = "Bumps [lodash](https://github.com/lodash/lodash) from 1.0.0 to 1.0.1.") -> str:
    """A Dependabot commit message whose block holds the given entry lines."""
    return f"chore(deps): bump lodash\n\n{intro}\n\n---\nupdated-dependencies:\n" + "".join(f"{line}\n" for line in entries) + "..." + SIGNED_OFF


LODASH = message("- dependency-name: lodash", "  dependency-version: 1.0.1", "  dependency-type: direct:production", f"  update-type: {PATCH}")


def commit(login: str | None = BOT, text: str = LODASH, verified: bool = True, sha: str = HEAD) -> dict:
    return {"sha": sha, "author": None if login is None else {"login": login}, "commit": {"message": text, "verification": {"verified": verified}}}


def pull(user: str = BOT, branch: str = BRANCH, title: str = "chore(deps): bump lodash from 1.0.0 to 1.0.1", auto_merge_state: dict | None = None) -> dict:
    return {"number": 7, "html_url": URL, "title": title, "user": {"login": user}, "base": {"ref": "main"}, "head": {"ref": branch, "sha": HEAD},
            "auto_merge": auto_merge_state, "merged": False}


def dependency(name: str, update_type: str, ecosystem: str = "npm_and_yarn"):
    return auto_merge.Dependency(name, ecosystem, update_type)


def read(text: str, branch: str = BRANCH, title: str = "") -> tuple:
    return auto_merge.read_metadata(pull(branch=branch, title=title), [commit(text=text)])


class MetadataTests(unittest.TestCase):
    def test_the_metadata_of_the_three_pull_requests_is_what_fetch_metadata_yields(self) -> None:
        for (text, branch, title), expected in (
            (PR_SETUP_NODE, (dependency("actions/setup-node", MAJOR, "github_actions"),)),
            (PR_SETUP_PYTHON, (dependency("actions/setup-python", MAJOR, "github_actions"),)),
            (PR_FAST_URI, (dependency("fast-uri", PATCH),)),
        ):
            with self.subTest(branch=branch):
                self.assertEqual(expected, read(text, branch, title))

    def test_an_entry_without_update_type_takes_the_type_from_its_versions(self) -> None:
        for previous, new, expected in (("3.1.8", "3.1.9", PATCH), ("1.2.3", "1.3.0", MINOR), ("1.2.3", "2.0.0", MAJOR), ("v1.2", "v1.3", MINOR),
                                        ("6", "7", MAJOR), ("1", "1.1", MINOR), ("1.2", "1.2.5", PATCH), ("1.0.0", "1.0.0", "")):
            with self.subTest(previous=previous, new=new):
                text = message("- dependency-name: lodash", intro=f"Bumps [lodash](https://github.com/lodash/lodash) from {previous} to {new}.")
                self.assertEqual((dependency("lodash", expected),), read(text))

    def test_the_new_version_of_an_entry_comes_before_the_bump_line_and_a_grouped_entry_takes_its_updates_line(self) -> None:
        text = message("- dependency-name: lodash", "  dependency-version: 2.0.0", intro="Bumps [lodash](https://github.com/lodash/lodash) from 1.0.0 to 1.0.1.")
        self.assertEqual((dependency("lodash", MAJOR),), read(text))
        grouped = message("- dependency-name: lodash", "  dependency-version: 1.0.1", "  dependency-group: minor-and-patch",
                          "- dependency-name: yaml", "  dependency-version: 2.4.0", "  dependency-group: minor-and-patch",
                          intro="Bumps the minor-and-patch group with 2 updates.\n\nUpdates `lodash` from 1.0.0 to 1.0.1\n\nUpdates `yaml` from 2.3.1 to 2.4.0")
        self.assertEqual((dependency("lodash", PATCH), dependency("yaml", MINOR)), read(grouped, "dependabot/npm_and_yarn/minor-and-patch-a1b2c3"))

    def test_an_entry_without_update_type_has_an_empty_type_when_a_version_is_missing(self) -> None:
        no_bump = message("- dependency-name: lodash", "  dependency-version: 1.0.1", intro="Updated the lockfile.")
        self.assertEqual((dependency("lodash", ""),), read(no_bump))
        no_new = message("- dependency-name: lodash", intro="Updated the lockfile.")
        self.assertEqual((dependency("lodash", ""),), read(no_new))
        second = message("- dependency-name: lodash", f"  update-type: {PATCH}", "- dependency-name: yaml", "  dependency-version: 2.4.0")
        self.assertEqual((dependency("lodash", PATCH), dependency("yaml", "")), read(second), "only the first entry takes the bump line's versions")

    def test_quoted_values_are_read_without_their_quotes(self) -> None:
        text = message("- dependency-name: \"@types/node\"", "  dependency-version: '20.1.0'", f"  update-type: '{MINOR}'")
        self.assertEqual((dependency("@types/node", MINOR),), read(text, "dependabot/npm_and_yarn/types/node-20.1.0"))
        self.assertEqual((dependency("it's", PATCH),), read(message("- dependency-name: 'it''s'", f'  update-type: "{PATCH}"')))

    def test_the_ecosystem_is_the_second_segment_of_the_head_branch_by_its_own_delimiter(self) -> None:
        text = message("- dependency-name: lodash", f"  update-type: {PATCH}")
        self.assertEqual("npm_and_yarn", read(text, "dependabot-npm_and_yarn-lodash-1.0.1")[0].ecosystem)
        self.assertEqual("pip", read(text, "dependabot/pip/requests-2.32.0")[0].ecosystem)

    def test_unverified_authorship_or_signature_is_an_error_that_names_the_failed_check(self) -> None:
        for pull_request, commits, fragment in (
            (pull(user="octocat"), [commit()], "the pull request is authored by `octocat`, not by `dependabot[bot]`"),
            (pull(), [commit("octocat")], "the first commit `aaaaaaa` is authored by `octocat`, not by `dependabot[bot]`"),
            (pull(), [commit(None)], "the first commit `aaaaaaa` is authored by an account GitHub cannot match"),
            (pull(), [commit(verified=False)], "the signature of the first commit `aaaaaaa` is not verified"),
            (pull(), [], "the pull request has no commit"),
        ):
            with self.subTest(fragment=fragment):
                with self.assertRaises(auto_merge.MetadataError) as caught:
                    auto_merge.read_metadata(pull_request, commits)
                self.assertIn(fragment, str(caught.exception))

    def test_a_message_without_the_block_or_with_a_line_outside_the_entry_shape_is_an_error(self) -> None:
        for text, fragment in (
            ("chore(deps): bump lodash\n\nBumps lodash." + SIGNED_OFF, "has no `updated-dependencies` block"),
            ("chore(deps): bump lodash\n\n---\nupdates:\n- dependency-name: lodash\n..." + SIGNED_OFF, "starts with `updates:`, not with `updated-dependencies:`"),
            (message("dependency-name: lodash"), "line 2 of the metadata block, `dependency-name: lodash`"),
            (message("  dependency-version: 1.0.1", "- dependency-name: lodash"), "line 2 of the metadata block"),
            (message("- dependency-name: lodash", ""), "line 3 of the metadata block, ``"),
            (message("- dependency-name: lodash", "  - nested: list"), "line 3 of the metadata block"),
            (message("- dependency-name: lodash", "    deeper: indent"), "line 3 of the metadata block"),
            (message("- dependency-name: lodash", "  dependency-name: other"), "repeats the key `dependency-name`"),
            (message("- dependency-version: 1.0.1"), "entry 1 of the metadata block has no `dependency-name`"),
            (message("- dependency-name: ''"), "entry 1 of the metadata block has no `dependency-name`"),
            (message(), "lists no updated dependency"),
        ):
            with self.subTest(fragment=fragment):
                with self.assertRaises(auto_merge.MetadataError) as caught:
                    read(text)
                self.assertIn(fragment, str(caught.exception))
        with self.assertRaises(auto_merge.MetadataError) as caught:
            read(LODASH, branch="renovate/lodash-1.x")
        self.assertIn("the head branch `renovate/lodash-1.x` is not a Dependabot branch", str(caught.exception))


class DecisionTests(unittest.TestCase):
    def test_patch_and_minor_updates_outside_github_actions_by_dependabot_under_required_checks_are_automatic(self) -> None:
        decision = auto_merge.decide((dependency("lodash", PATCH), dependency("yaml", MINOR)), [commit()], REQUIRED_CHECKS)
        self.assertEqual("automatic", decision.outcome, decision.reason)
        self.assertIn("every update is a patch or minor update", decision.reason)

    def test_actions_owned_github_actions_updates_are_automatic(self) -> None:
        decision = auto_merge.decide((dependency("actions/checkout", MINOR, "github_actions"), dependency("actions/setup-node", PATCH, "github_actions")), [commit()], REQUIRED_CHECKS)
        self.assertEqual("automatic", decision.outcome, decision.reason)

    def test_a_major_update_is_manual_and_the_reason_names_the_dependency(self) -> None:
        decision = auto_merge.decide((dependency("lodash", PATCH), dependency("react", MAJOR)), [commit()], REQUIRED_CHECKS)
        self.assertEqual("manual", decision.outcome)
        self.assertIn("`react` has `version-update:semver-major`, not a patch or minor update", decision.reason)
        self.assertNotIn("lodash", decision.reason)

    def test_an_empty_or_unknown_update_type_is_manual(self) -> None:
        for update_type, shown in [("", "an empty update type"), ("version-update:semver-unknown", "`version-update:semver-unknown`")]:
            with self.subTest(update_type=update_type):
                decision = auto_merge.decide((dependency("lodash", PATCH), dependency("mystery", update_type)), [commit()], REQUIRED_CHECKS)
                self.assertEqual("manual", decision.outcome)
                self.assertIn(f"`mystery` has {shown}, not a patch or minor update", decision.reason)

    def test_a_github_action_from_another_owner_is_manual_even_when_its_name_contains_actions(self) -> None:
        for name in ("someone/actions-tool", "someone/actions", "actions", "actionsfoo/bar", "Actions/checkout"):
            with self.subTest(name=name):
                decision = auto_merge.decide((dependency(name, PATCH, "github_actions"),), [commit()], REQUIRED_CHECKS)
                self.assertEqual("manual", decision.outcome)
                self.assertIn(f"`{name}` is a GitHub Action whose owner is not `actions`", decision.reason)
        decision = auto_merge.decide((dependency("someone/actions", PATCH, "npm_and_yarn"),), [commit()], REQUIRED_CHECKS)
        self.assertEqual("automatic", decision.outcome, "the owner rule applies to the github_actions ecosystem only")

    def test_a_grouped_pull_request_with_one_disqualifying_dependency_is_manual(self) -> None:
        dependencies = (dependency("actions/checkout", PATCH, "github_actions"), dependency("docker/login-action", PATCH, "github_actions"), dependency("actions/cache", MAJOR, "github_actions"))
        decision = auto_merge.decide(dependencies, [commit()], REQUIRED_CHECKS)
        self.assertEqual("manual", decision.outcome)
        self.assertIn("`docker/login-action` is a GitHub Action whose owner is not `actions`", decision.reason)
        self.assertIn("`actions/cache` has `version-update:semver-major`", decision.reason)
        self.assertEqual(3, len(decision.dependencies))

    def test_a_commit_by_another_author_is_manual(self) -> None:
        for login, shown in [("octocat", "`octocat`"), (None, "an account GitHub cannot match")]:
            with self.subTest(login=login):
                decision = auto_merge.decide((dependency("lodash", PATCH),), [commit(), commit(login, sha="0" * 40)], REQUIRED_CHECKS)
                self.assertEqual("manual", decision.outcome)
                self.assertIn(f"commit `0000000` is authored by {shown}, not by `dependabot[bot]`", decision.reason)

    def test_a_rebuild_by_the_updater_app_of_only_the_consumer_build_and_the_suite_list_is_automatic(self) -> None:
        commits = [commit(sha=FIRST), commit(UPDATER, REBUILD, sha=HEAD)]
        for files in (REBUILT, ["tests/suites.json"], [f"{CONSUMER_BUILD}EtwConsumer.dll"]):
            with self.subTest(files=files):
                decision = auto_merge.decide((dependency("lodash", MINOR),), commits, REQUIRED_CHECKS, {HEAD: files})
                self.assertEqual("automatic", decision.outcome, decision.reason)

    def test_an_updater_app_commit_that_changes_any_other_file_is_manual_and_names_the_file(self) -> None:
        commits = [commit(sha=FIRST), commit(UPDATER, REBUILD, sha=HEAD)]
        for outside in ("actions/recording/etw-consumer/EtwConsumer.csproj", ".github/workflows/validate.yml", "tests/suites.json.bak",
                        "actions/recording/etw-consumer/buildx/EtwConsumer.dll"):
            with self.subTest(outside=outside):
                decision = auto_merge.decide((dependency("lodash", PATCH),), commits, REQUIRED_CHECKS, {HEAD: [*REBUILT, outside]})
                self.assertEqual("manual", decision.outcome)
                self.assertIn(f"commit `aaaaaaa` by `{UPDATER}` changes `{outside}`, outside the ETW consumer's build and the suite list", decision.reason)
        decision = auto_merge.decide((dependency("lodash", PATCH),), commits, REQUIRED_CHECKS, {})
        self.assertEqual("manual", decision.outcome)
        self.assertIn(f"the changed files of commit `aaaaaaa` by `{UPDATER}` were not read", decision.reason)

    def test_outside_this_repository_an_updater_app_commit_counts_as_any_other_author(self) -> None:
        commits = [commit(sha=FIRST), commit(UPDATER, REBUILD, sha=HEAD)]
        decision = auto_merge.decide((dependency("lodash", PATCH),), commits, REQUIRED_CHECKS, None)
        self.assertEqual("manual", decision.outcome)
        self.assertIn(f"commit `aaaaaaa` is authored by `{UPDATER}`, not by `dependabot[bot]`", decision.reason)

    def test_another_app_s_commit_with_the_rebuild_s_files_is_manual(self) -> None:
        commits = [commit(sha=FIRST), commit("other-app[bot]", REBUILD, sha=HEAD)]
        decision = auto_merge.decide((dependency("lodash", PATCH),), commits, REQUIRED_CHECKS, {HEAD: REBUILT})
        self.assertEqual("manual", decision.outcome)
        self.assertIn("commit `aaaaaaa` is authored by `other-app[bot]`, not by `dependabot[bot]`", decision.reason)

    def test_a_rebuild_does_not_lift_the_update_rules(self) -> None:
        commits = [commit(sha=FIRST), commit(UPDATER, REBUILD, sha=HEAD)]
        decision = auto_merge.decide((dependency("Microsoft.Diagnostics.Tracing.TraceEvent", MAJOR, "nuget"),), commits, REQUIRED_CHECKS, {HEAD: REBUILT})
        self.assertEqual("manual", decision.outcome)
        self.assertIn("has `version-update:semver-major`, not a patch or minor update", decision.reason)

    def test_without_a_ruleset_that_requires_a_check_a_qualifying_pull_request_is_blocked_naming_the_ruleset(self) -> None:
        for rules in ([], [{"type": "deletion", "parameters": {}}], [{"type": "required_status_checks", "parameters": {"required_status_checks": []}}]):
            with self.subTest(rules=rules):
                decision = auto_merge.decide((dependency("lodash", PATCH),), [commit()], rules)
                self.assertEqual("blocked", decision.outcome)
                self.assertIn("no active ruleset rule on the base branch requires a status check", decision.reason)

    def test_a_manual_pull_request_stays_manual_whatever_the_ruleset_is(self) -> None:
        for rules in ([], REQUIRED_CHECKS):
            with self.subTest(rules=rules):
                self.assertEqual("manual", auto_merge.decide((dependency("react", MAJOR),), [commit()], rules).outcome)


def refused(status: int, text: str) -> auto_merge.GitHubError:
    response = f'{{"message": "{text}", "status": "{status}"}}'
    return auto_merge.GitHubError(f"gh: {text} (HTTP {status}) {response}", status, response)


class FakeGitHub:
    """One repository's API state with canned responses; every call is recorded, and a name in ``failures`` raises
    ``GitHubError`` when called. A successful merge marks the pull request merged; ``merge_errors`` are raised by the
    merge requests in turn before any merge succeeds."""

    def __init__(self, *, pull_request: dict | None = None, commits: list[dict] | None = None, rules: list[dict] | None = None,
                 failures: tuple[str, ...] = (), merge_errors: list[auto_merge.GitHubError] | None = None, listed: list[dict] | None = None,
                 files: dict[str, list[str]] | None = None) -> None:
        self.pull = pull() if pull_request is None else pull_request
        self.listed = [self.pull] if listed is None else listed
        self.commit_list = [commit()] if commits is None else commits
        self.files = files or {}
        self.rules = REQUIRED_CHECKS if rules is None else rules
        self.failures = failures
        self.merge_errors = list(merge_errors or [])
        self.calls: list[tuple] = []

    def _record(self, *call: object) -> None:
        self.calls.append(call)
        if call[0] in self.failures:
            raise auto_merge.GitHubError(f"{call[0]} failed: HTTP 500", 500, f'{{"message": "{call[0]} failed"}}')

    def pull_request(self, repository: str, number: int) -> dict:
        self._record("pull_request", repository, number)
        return dict(self.pull)

    def open_pull_requests(self, repository: str, head: str) -> list[dict]:
        self._record("open_pull_requests", repository, head)
        return [dict(entry) for entry in self.listed]

    def commits(self, repository: str, number: int) -> list[dict]:
        self._record("commits", repository, number)
        return self.commit_list

    def commit_files(self, repository: str, sha: str) -> list[str]:
        self._record("commit_files", repository, sha)
        return self.files[sha]

    def branch_rules(self, repository: str, branch: str) -> list[dict]:
        self._record("branch_rules", repository, branch)
        return self.rules

    def disable_auto_merge(self, url: str) -> None:
        self._record("disable_auto_merge", url)

    def merge(self, repository: str, number: int, sha: str, method: str) -> dict:
        self._record("merge", repository, number, sha, method)
        if self.pull["merged"]:
            raise refused(405, "Pull Request is not mergeable")
        if self.merge_errors:
            raise self.merge_errors.pop(0)
        self.pull["merged"] = True
        return {"sha": MERGED, "merged": True, "message": "Pull Request successfully merged"}

    def dispatch(self, repository: str, event_type: str, sha: str) -> None:
        self._record("dispatch", repository, event_type, sha)


def mutations(github: FakeGitHub) -> list[tuple]:
    return [call for call in github.calls if call[0] in ("disable_auto_merge", "merge", "dispatch")]


def merge_run(github: FakeGitHub) -> auto_merge.Result:
    (result,) = auto_merge.run_workflow_run(github, REPOSITORY, BRANCH)
    return result


class PullRequestRunTests(unittest.TestCase):
    def test_a_qualifying_pull_request_gets_no_auto_merge_and_the_run_succeeds_with_the_decision_summary(self) -> None:
        github = FakeGitHub()
        result = auto_merge.run_pull_request(github, REPOSITORY, 7)
        self.assertEqual(0, result.exit_code, result.summary)
        self.assertEqual([], mutations(github))
        self.assertEqual([("pull_request", REPOSITORY, 7), ("commits", REPOSITORY, 7), ("branch_rules", REPOSITORY, "main")], github.calls)
        self.assertIn("| `lodash` | npm_and_yarn | version-update:semver-patch |", result.summary)
        self.assertIn("**Decision:** automatic: every update is a patch or minor update", result.summary)
        self.assertIn("**Action:** nothing enabled and nothing to disable.", result.summary)

    def test_the_summary_lists_the_metadata_of_the_fast_uri_update_as_fetch_metadata_yields_it(self) -> None:
        text, branch, title = PR_FAST_URI
        result = auto_merge.run_pull_request(FakeGitHub(pull_request=pull(branch=branch, title=title), commits=[commit(text=text)]), REPOSITORY, 7)
        self.assertIn("| Dependency | Ecosystem | Update type |\n| --- | --- | --- |\n| `fast-uri` | npm_and_yarn | version-update:semver-patch |\n", result.summary)

    def test_the_summary_shows_quoted_values_without_their_quotes(self) -> None:
        text = message("- dependency-name: \"@types/node\"", "  dependency-version: '20.1.0'", f"  update-type: '{MINOR}'")
        result = auto_merge.run_pull_request(FakeGitHub(pull_request=pull(branch="dependabot/npm_and_yarn/types/node-20.1.0"), commits=[commit(text=text)]), REPOSITORY, 7)
        self.assertIn("| `@types/node` | npm_and_yarn | version-update:semver-minor |", result.summary)
        self.assertNotIn("'", result.summary.split("**Decision:**")[0])

    def test_a_pull_request_that_still_has_auto_merge_gets_it_disabled_whatever_the_method(self) -> None:
        for state in ({"enabled_by": {"login": "github-actions[bot]"}, "merge_method": "squash"}, {"merge_method": "merge"}):
            with self.subTest(state=state):
                github = FakeGitHub(pull_request=pull(auto_merge_state=state))
                result = auto_merge.run_pull_request(github, REPOSITORY, 7)
                self.assertEqual(0, result.exit_code, result.summary)
                self.assertEqual("automatic", result.decision.outcome)
                self.assertEqual([("disable_auto_merge", URL)], mutations(github))
                self.assertIn("**Action:** auto-merge disabled; nothing enabled.", result.summary)

    def test_running_again_on_an_unchanged_pull_request_changes_nothing_and_reports_the_same_decision(self) -> None:
        github = FakeGitHub()
        first = auto_merge.run_pull_request(github, REPOSITORY, 7)
        second = auto_merge.run_pull_request(github, REPOSITORY, 7)
        self.assertEqual([], mutations(github))
        self.assertEqual(first.summary, second.summary)

    def test_a_later_event_that_no_longer_meets_the_rules_disables_auto_merge(self) -> None:
        github = FakeGitHub(pull_request=pull(auto_merge_state={"merge_method": "squash"}), commits=[commit(), commit("octocat")])
        result = auto_merge.run_pull_request(github, REPOSITORY, 7)
        self.assertEqual(0, result.exit_code, result.summary)
        self.assertEqual([("disable_auto_merge", URL)], mutations(github))
        self.assertIn("auto-merge disabled", result.summary)
        github = FakeGitHub(commits=[commit(), commit("octocat")])
        result = auto_merge.run_pull_request(github, REPOSITORY, 7)
        self.assertEqual([], mutations(github), "a manual pull request without auto-merge is left alone")
        self.assertIn("**Action:** nothing to disable; the pull request is left to the user.", result.summary)

    def test_a_blocked_pull_request_fails_without_enabling_or_merging_anything(self) -> None:
        github = FakeGitHub(rules=[])
        result = auto_merge.run_pull_request(github, REPOSITORY, 7)
        self.assertEqual(1, result.exit_code)
        self.assertEqual([], mutations(github))
        self.assertIn("no active ruleset rule on the base branch requires a status check", result.summary)
        self.assertIn("**Action:** nothing enabled and nothing merged; the user settles the ruleset first.", result.summary)
        github = FakeGitHub(rules=[], pull_request=pull(auto_merge_state={"merge_method": "squash"}))
        result = auto_merge.run_pull_request(github, REPOSITORY, 7)
        self.assertEqual(1, result.exit_code)
        self.assertEqual([("disable_auto_merge", URL)], mutations(github))
        self.assertIn("**Action:** auto-merge disabled; nothing merged; the user settles the ruleset first.", result.summary)

    def test_unverified_metadata_fails_and_disables_auto_merge(self) -> None:
        for pull_request, commits, fragment in ((pull(), [commit(verified=False)], "is not verified"), (pull(), [commit(text="chore: x")], "has no `updated-dependencies` block"),
                                                (pull(user="octocat"), [commit()], "the pull request is authored by `octocat`, not by `dependabot[bot]`"),
                                                (pull(), [commit("octocat"), commit()], "the first commit `aaaaaaa` is authored by `octocat`, not by `dependabot[bot]`")):
            with self.subTest(fragment=fragment):
                github = FakeGitHub(pull_request={**pull_request, "auto_merge": {"merge_method": "squash"}}, commits=commits)
                result = auto_merge.run_pull_request(github, REPOSITORY, 7)
                self.assertEqual(1, result.exit_code)
                self.assertEqual("error", result.decision.outcome)
                self.assertEqual([("disable_auto_merge", URL)], mutations(github))
                self.assertIn(fragment, result.summary)
                self.assertIn("No updated dependency was reported.", result.summary)

    def test_a_failed_api_call_fails_and_disables_auto_merge_when_it_can(self) -> None:
        for failure in ("commits", "branch_rules"):
            with self.subTest(failure=failure):
                github = FakeGitHub(pull_request=pull(auto_merge_state={"merge_method": "squash"}), failures=(failure,))
                result = auto_merge.run_pull_request(github, REPOSITORY, 7)
                self.assertEqual(1, result.exit_code)
                self.assertEqual([("disable_auto_merge", URL)], mutations(github))
                self.assertIn(f"{failure} failed: HTTP 500", result.summary)
        result = auto_merge.run_pull_request(FakeGitHub(failures=("branch_rules",)), REPOSITORY, 7)
        self.assertIn("| `lodash` | npm_and_yarn | version-update:semver-patch |", result.summary, "the read metadata stays in the summary")
        github = FakeGitHub(failures=("pull_request",))
        result = auto_merge.run_pull_request(github, REPOSITORY, 7)
        self.assertEqual(1, result.exit_code)
        self.assertEqual([], mutations(github), "without the pull request nothing can be disabled")
        self.assertIn("pull_request failed: HTTP 500", result.summary)
        github = FakeGitHub(pull_request=pull(auto_merge_state={"merge_method": "squash"}), failures=("disable_auto_merge",))
        result = auto_merge.run_pull_request(github, REPOSITORY, 7)
        self.assertEqual(1, result.exit_code)
        self.assertEqual("error", result.decision.outcome)
        self.assertIn("disable_auto_merge failed: HTTP 500", result.summary)

    def test_the_summary_lists_every_dependency_with_ecosystem_and_update_type_and_the_decision_with_its_reason(self) -> None:
        text = message("- dependency-name: actions/checkout", f"  update-type: {MINOR}", "- dependency-name: docker/login-action", f"  update-type: {PATCH}")
        github = FakeGitHub(pull_request=pull(branch="dependabot/github_actions/minor-and-patch-a1b2c3"), commits=[commit(text=text)])
        result = auto_merge.run_pull_request(github, REPOSITORY, 7)
        self.assertIn("## Dependabot auto-merge", result.summary)
        self.assertIn("| `actions/checkout` | github_actions | version-update:semver-minor |", result.summary)
        self.assertIn("| `docker/login-action` | github_actions | version-update:semver-patch |", result.summary)
        self.assertIn("**Decision:** manual: `docker/login-action` is a GitHub Action whose owner is not `actions`.", result.summary)
        self.assertIn("**Action:** nothing to disable; the pull request is left to the user.", result.summary)


    def test_a_rebuilt_head_in_this_repository_is_decided_automatic_and_still_gets_no_auto_merge(self) -> None:
        for files, outcome in ((REBUILT, "automatic"), ([*REBUILT, "README.md"], "manual")):
            with self.subTest(outcome=outcome):
                github = FakeGitHub(commits=[commit(sha=FIRST), commit(UPDATER, REBUILD, sha=HEAD)], files={HEAD: files},
                                    pull_request=pull(auto_merge_state={"merge_method": "squash"}))
                result = auto_merge.run_pull_request(github, THIS_REPOSITORY, 7)
                self.assertEqual((outcome, 0), (result.decision.outcome, result.exit_code), result.summary)
                self.assertEqual([("disable_auto_merge", URL)], mutations(github))


class WorkflowRunTests(unittest.TestCase):
    def test_the_last_required_check_merges_with_squash_bound_to_the_head_read_and_dispatches_the_merged_commit_once(self) -> None:
        github = FakeGitHub()
        result = merge_run(github)
        self.assertEqual(0, result.exit_code, result.summary)
        self.assertEqual([("merge", REPOSITORY, 7, HEAD, "squash"), ("dispatch", REPOSITORY, "dependabot-merged", MERGED)], mutations(github))
        self.assertEqual(("open_pull_requests", REPOSITORY, f"CrankyAnt:{BRANCH}"), github.calls[0])
        self.assertIn(f"**Action:** merged `aaaaaaa` with the squash method as `{MERGED}` and sent `dependabot-merged` with that commit.", result.summary)

    def test_a_pending_or_failed_required_check_refuses_the_merge_and_the_run_succeeds_without_a_dispatch(self) -> None:
        for text in ("Repository rule violations found\\n\\nRequired status check \\\"validate\\\" is expected.", "Required status check \\\"hygiene / hygiene\\\" is failing."):
            with self.subTest(text=text):
                github = FakeGitHub(merge_errors=[refused(405, text)])
                result = merge_run(github)
                self.assertEqual(0, result.exit_code, result.summary)
                self.assertEqual(["merge"], [call[0] for call in mutations(github)])
                self.assertIn("GitHub refused the merge: a required check is pending or failed (", result.summary)
                self.assertIn("Required status check", result.summary)

    def test_a_head_that_moved_refuses_the_merge_and_the_run_succeeds_without_a_dispatch(self) -> None:
        github = FakeGitHub(merge_errors=[refused(409, "Head branch was modified. Review and try the merge again.")])
        result = merge_run(github)
        self.assertEqual(0, result.exit_code, result.summary)
        self.assertEqual(["merge"], [call[0] for call in mutations(github)])
        self.assertIn("GitHub refused the merge: the head moved after the decision read it (", result.summary)
        github = FakeGitHub(commits=[commit(), commit(sha="c" * 40)])
        result = merge_run(github)
        self.assertEqual(0, result.exit_code, result.summary)
        self.assertEqual([], mutations(github), "commits that do not end at the head read are no basis for a merge")
        self.assertIn("the head moved while the decision read the commits", result.summary)

    def test_two_runs_that_reach_the_merge_merge_and_dispatch_once_and_both_succeed(self) -> None:
        github = FakeGitHub()
        first = auto_merge.run_workflow_run(github, REPOSITORY, BRANCH)
        second = auto_merge.run_workflow_run(github, REPOSITORY, BRANCH)
        self.assertEqual([0, 0], [first[0].exit_code, second[0].exit_code])
        self.assertEqual(1, sum(1 for call in mutations(github) if call[0] == "dispatch"))
        self.assertTrue(github.pull["merged"])
        self.assertIn("GitHub refused the merge: the pull request is already merged.", second[0].summary)

    def test_a_manual_or_blocked_decision_merges_and_dispatches_nothing(self) -> None:
        for github, code in ((FakeGitHub(commits=[commit(), commit("octocat", sha=HEAD)]), 0),
                             (FakeGitHub(commits=[commit(text=message("- dependency-name: react", f"  update-type: {MAJOR}"))]), 0),
                             (FakeGitHub(rules=[]), 1)):
            with self.subTest(calls=github.commit_list[-1]["author"], rules=github.rules):
                result = merge_run(github)
                self.assertEqual(code, result.exit_code, result.summary)
                self.assertEqual([], mutations(github))
                self.assertIn("**Action:** nothing merged and nothing dispatched.", result.summary)

    def test_unverified_metadata_is_an_error_that_names_the_failed_check_merges_nothing_and_fails(self) -> None:
        for github, fragment in ((FakeGitHub(commits=[commit(verified=False)]), "the signature of the first commit `aaaaaaa` is not verified"),
                                 (FakeGitHub(commits=[commit("octocat"), commit()]), "the first commit `aaaaaaa` is authored by `octocat`"),
                                 (FakeGitHub(commits=[commit(text="chore(deps): bump lodash" + SIGNED_OFF)]), "has no `updated-dependencies` block"),
                                 (FakeGitHub(commits=[commit(text=message("dependency-name: lodash"))]), "is not a `key: value` pair of a dependency entry")):
            with self.subTest(fragment=fragment):
                result = merge_run(github)
                self.assertEqual(1, result.exit_code)
                self.assertEqual("error", result.decision.outcome)
                self.assertIn(fragment, result.summary)
                self.assertEqual([], [call for call in mutations(github) if call[0] in ("merge", "dispatch")])

    def test_any_other_merge_or_dispatch_failure_fails_the_run_naming_the_step_and_the_response(self) -> None:
        for github, fragment in ((FakeGitHub(merge_errors=[refused(405, "Base branch was modified. Review and try the merge again.")]),
                                  'the merge request failed: gh: Base branch was modified. Review and try the merge again. (HTTP 405) {"message": "Base branch was modified.'),
                                 (FakeGitHub(merge_errors=[refused(403, "Resource not accessible by integration")]),
                                  'the merge request failed: gh: Resource not accessible by integration (HTTP 403) {"message": "Resource not accessible by integration", "status": "403"}'),
                                 (FakeGitHub(failures=("dispatch",)), f"merged `aaaaaaa` with the squash method as `{MERGED}`, but the `dependabot-merged` dispatch failed: dispatch failed: HTTP 500")):
            with self.subTest(fragment=fragment):
                result = merge_run(github)
                self.assertEqual(1, result.exit_code, result.summary)
                self.assertEqual("error", result.decision.outcome)
                self.assertIn(fragment, result.summary)
        github = FakeGitHub(merge_errors=[refused(405, "Required status check \\\"validate\\\" is expected.")], failures=())
        github.failures = ("pull_request",)
        result = merge_run(github)
        self.assertEqual(1, result.exit_code, "a refusal that cannot be checked against the pull request fails")

    def test_a_head_that_the_updater_app_rebuilt_in_this_repository_merges_after_reading_only_the_app_commit_s_files(self) -> None:
        def rebuilt_run(github: FakeGitHub, repository: str = THIS_REPOSITORY) -> auto_merge.Result:
            (result,) = auto_merge.run_workflow_run(github, repository, BRANCH)
            return result

        github = FakeGitHub(commits=[commit(sha=FIRST), commit(UPDATER, REBUILD, sha=HEAD)], files={HEAD: REBUILT})
        result = rebuilt_run(github)
        self.assertEqual(0, result.exit_code, result.summary)
        self.assertEqual([("merge", THIS_REPOSITORY, 7, HEAD, "squash"), ("dispatch", THIS_REPOSITORY, "dependabot-merged", MERGED)], mutations(github))
        self.assertEqual([("commit_files", THIS_REPOSITORY, HEAD)], [call for call in github.calls if call[0] == "commit_files"])
        github = FakeGitHub(commits=[commit(sha=FIRST), commit(UPDATER, REBUILD, sha=HEAD)], files={HEAD: [*REBUILT, "README.md"]})
        result = rebuilt_run(github)
        self.assertEqual("manual", result.decision.outcome)
        self.assertEqual([], mutations(github))
        github = FakeGitHub(commits=[commit(sha=FIRST), commit(UPDATER, REBUILD, sha=HEAD)], files={HEAD: REBUILT}, failures=("commit_files",))
        result = rebuilt_run(github)
        self.assertEqual(("error", 1), (result.decision.outcome, result.exit_code))
        self.assertEqual([], mutations(github))
        github = FakeGitHub(commits=[commit(sha=FIRST), commit(UPDATER, REBUILD, sha=HEAD)], files={HEAD: REBUILT})
        result = rebuilt_run(github, REPOSITORY)
        self.assertEqual("manual", result.decision.outcome, "another repository accepts no App commit")
        self.assertEqual([], [call for call in github.calls if call[0] in ("commit_files", "merge", "dispatch")])

    def test_only_open_dependabot_pull_requests_of_the_branch_are_considered(self) -> None:
        github = FakeGitHub(listed=[])
        result = merge_run(github)
        self.assertEqual(0, result.exit_code)
        self.assertEqual("none", result.decision.outcome)
        self.assertIn(f"no open pull request by `dependabot[bot]` has the head branch `{BRANCH}`", result.summary)
        github = FakeGitHub(listed=[pull(user="octocat")])
        result = merge_run(github)
        self.assertEqual("none", result.decision.outcome)
        self.assertEqual([], mutations(github))
        github = FakeGitHub(failures=("open_pull_requests",))
        result = merge_run(github)
        self.assertEqual(1, result.exit_code)
        self.assertIn("the open pull requests could not be read", result.summary)


class CommandLineTests(unittest.TestCase):
    def test_the_gh_client_issues_the_documented_commands(self) -> None:
        issued: list[list[str]] = []
        responses = {
            f"repos/{REPOSITORY}/pulls/7": '{"html_url": "u"}',
            f"repos/{REPOSITORY}/pulls": '[{"number": 7}]',
            f"repos/{REPOSITORY}/pulls/7/commits": '[[{"sha": "a"}], [{"sha": "b"}]]',
            f"repos/{REPOSITORY}/rules/branches/main": '[]',
            f"repos/{REPOSITORY}/rules/branches/release%2Fv1%202": "[]",
            f"repos/{REPOSITORY}/pulls/7/merge": f'{{"sha": "{MERGED}", "merged": true}}',
            f"repos/{REPOSITORY}/commits/{HEAD}": '[{"files": [{"filename": "tests/suites.json"}, {"filename": "b/new.dll", "previous_filename": "b/old.dll"}]}, '
                                                  '{"files": [{"filename": "b/x.dll"}]}]',
        }

        def fake_run(command, capture_output, text, check):
            issued.append(command)
            path = next((part for part in command if part.startswith("repos/")), "")
            stdout = responses.get(path, "") if command[1] == "api" else ""
            return subprocess.CompletedProcess(command, 0, stdout, "")

        with mock.patch.object(auto_merge.subprocess, "run", fake_run):
            client = auto_merge.GitHub("gh-fixture")
            self.assertEqual("u", client.pull_request(REPOSITORY, 7)["html_url"])
            self.assertEqual([{"number": 7}], client.open_pull_requests(REPOSITORY, f"CrankyAnt:{BRANCH}"))
            self.assertEqual(["a", "b"], [entry["sha"] for entry in client.commits(REPOSITORY, 7)], "pages are flattened")
            self.assertEqual(["tests/suites.json", "b/new.dll", "b/old.dll", "b/x.dll"], client.commit_files(REPOSITORY, HEAD),
                             "pages are flattened and a rename names both paths")
            client.branch_rules(REPOSITORY, "main")
            client.branch_rules(REPOSITORY, "release/v1 2")
            client.disable_auto_merge("u")
            self.assertEqual(MERGED, client.merge(REPOSITORY, 7, HEAD, "squash")["sha"])
            client.dispatch(REPOSITORY, "dependabot-merged", MERGED)
        self.assertEqual([
            ["gh-fixture", "api", f"repos/{REPOSITORY}/pulls/7"],
            ["gh-fixture", "api", "--method", "GET", f"repos/{REPOSITORY}/pulls", "-f", "state=open", "-f", f"head=CrankyAnt:{BRANCH}"],
            ["gh-fixture", "api", "--paginate", "--slurp", f"repos/{REPOSITORY}/pulls/7/commits"],
            ["gh-fixture", "api", "--paginate", "--slurp", f"repos/{REPOSITORY}/commits/{HEAD}"],
            ["gh-fixture", "api", f"repos/{REPOSITORY}/rules/branches/main"],
            ["gh-fixture", "api", f"repos/{REPOSITORY}/rules/branches/release%2Fv1%202"],
            ["gh-fixture", "pr", "merge", "--disable-auto", "u"],
            ["gh-fixture", "api", "--method", "PUT", f"repos/{REPOSITORY}/pulls/7/merge", "-f", "merge_method=squash", "-f", f"sha={HEAD}"],
            ["gh-fixture", "api", "--method", "POST", f"repos/{REPOSITORY}/dispatches", "-f", "event_type=dependabot-merged", "-f", f"client_payload[sha]={MERGED}"],
        ], issued)

    def test_a_failed_gh_call_carries_the_http_status_and_the_response(self) -> None:
        body = '{"message":"Repository rule violations found","status":"405"}'

        def failing_run(command, capture_output, text, check):
            return subprocess.CompletedProcess(command, 1, body, "gh: Repository rule violations found (HTTP 405)")

        with mock.patch.object(auto_merge.subprocess, "run", failing_run):
            with self.assertRaises(auto_merge.GitHubError) as caught:
                auto_merge.GitHub().merge(REPOSITORY, 7, HEAD, "squash")
        self.assertEqual(405, caught.exception.status)
        self.assertEqual(body, caught.exception.response)
        self.assertIn(f"`gh api --method PUT repos/{REPOSITORY}/pulls/7/merge -f merge_method=squash -f sha={HEAD}` failed (exit 1): gh: Repository rule violations found (HTTP 405) {body}", str(caught.exception),
                      "the message names the call and carries the API response")

        def failing_without_status(command, capture_output, text, check):
            return subprocess.CompletedProcess(command, 1, "", "HTTP 403: Resource not accessible by integration")

        with mock.patch.object(auto_merge.subprocess, "run", failing_without_status):
            with self.assertRaises(auto_merge.GitHubError) as caught:
                auto_merge.GitHub().disable_auto_merge("u")
        self.assertIsNone(caught.exception.status)
        self.assertIn("`gh pr merge --disable-auto u` failed (exit 1): HTTP 403", str(caught.exception))

    def test_the_command_line_runs_either_event_and_appends_the_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            summary.write_text("earlier\n", encoding="utf-8")
            github = FakeGitHub()
            with mock.patch.object(auto_merge, "GitHub", lambda: github), contextlib.redirect_stdout(io.StringIO()) as stdout:
                code = auto_merge.main(["--repository", REPOSITORY, "--summary", str(summary), "pull-request", "--number", "7"])
            self.assertEqual(0, code)
            self.assertEqual([], mutations(github))
            self.assertTrue(summary.read_text(encoding="utf-8").startswith("earlier\n## Dependabot auto-merge\n"))
            self.assertIn("| `lodash` | npm_and_yarn | version-update:semver-patch |", stdout.getvalue())
            github = FakeGitHub()
            with mock.patch.object(auto_merge, "GitHub", lambda: github), contextlib.redirect_stdout(io.StringIO()):
                code = auto_merge.main(["--repository", REPOSITORY, "--summary", str(summary), "workflow-run", "--head-branch", BRANCH])
            self.assertEqual(0, code)
            self.assertEqual(["merge", "dispatch"], [call[0] for call in mutations(github)])
            self.assertEqual(2, summary.read_text(encoding="utf-8").count("## Dependabot auto-merge"))
            github = FakeGitHub(rules=[])
            with mock.patch.object(auto_merge, "GitHub", lambda: github), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(1, auto_merge.main(["--repository", REPOSITORY, "workflow-run", "--head-branch", BRANCH]))

    def test_the_command_line_takes_no_metadata_outcome(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            auto_merge.build_parser().parse_args(["--repository", REPOSITORY, "pull-request", "--number", "7", "--metadata-outcome", "success"])


if __name__ == "__main__":
    unittest.main()
