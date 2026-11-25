"""The sync's settings comparison and its issues: with the App's token a ruleset or merge setting that differs is
reported and never changed, the local mode corrects it, and a run opens one issue in its own repository per finding."""

from __future__ import annotations

import unittest

from tests.consumer_checks_fixtures import SHARED, SYNC_BRANCH, WORKFLOWS
from tests.sync_fixtures import (APP_TOKEN, CONSUMER, MANAGEMENT, MERGE_SETTINGS, NOW, OWNER_TOKEN, RULESET, RUN_TOKEN, SyncFixture,
                                 expected_ruleset)

CI = f"{WORKFLOWS}/ci.yml"


def settings_writes(writes: list) -> list:
    return [item for item in writes if item.path == f"/repos/{CONSUMER}" or "/rulesets" in item.path]


class AppTokenTests(SyncFixture):
    def test_a_merge_setting_that_differs_is_reported_and_not_changed(self) -> None:
        consumer = self.consumer()
        consumer.settings.update(allow_rebase_merge=True, squash_merge_commit_title="COMMIT_OR_PR_TITLE")
        synced = self.start()
        self.assertEqual(0, synced.status, synced.stdout + synced.stderr)
        self.assertIn(f"### `{CONSUMER}`", synced.summary)
        self.assertIn("Settings difference: `allow_rebase_merge` is `true`, expected `false`.", synced.summary)
        self.assertIn('Settings difference: `squash_merge_commit_title` is `"COMMIT_OR_PR_TITLE"`, expected `"PR_TITLE"`.', synced.summary)
        self.assertEqual([], self.api.writes)
        self.assertTrue(consumer.settings["allow_rebase_merge"])

    def test_a_ruleset_that_differs_is_reported_and_not_changed(self) -> None:
        consumer = self.consumer()
        consumer.rulesets[0]["enforcement"] = "disabled"
        consumer.rulesets[0]["rules"] = [rule for rule in consumer.rulesets[0]["rules"] if rule["type"] != "deletion"]
        synced = self.run_sync()
        self.assertIn('Settings difference: the ruleset `main checks`: `enforcement` is `"disabled"`, expected `"active"`.', synced.summary)
        self.assertIn("Settings difference: the ruleset `main checks`: `rules` is ", synced.summary)
        self.assertEqual([], self.api.writes)
        self.assertEqual("disabled", consumer.rulesets[0]["enforcement"])

    def test_a_ruleset_that_still_requires_the_earlier_checks_differs(self) -> None:
        consumer = self.consumer()
        consumer.rulesets[0] = {"id": 7, **RULESET}
        self.assertEqual(4, len(RULESET["rules"][2]["parameters"]["required_status_checks"]), "the template still lists the earlier checks")
        synced = self.run_sync()
        self.assertIn("the ruleset `main checks`: `rules` is ", synced.summary)

    def test_a_missing_ruleset_is_reported(self) -> None:
        self.consumer().rulesets.clear()
        synced = self.run_sync()
        self.assertIn("Settings difference: the ruleset `main checks` does not exist.", synced.summary)
        self.assertEqual([], self.api.writes)

    def test_a_ruleset_whose_rules_stand_in_another_order_with_further_fields_matches(self) -> None:
        consumer = self.consumer()
        ruleset = consumer.rulesets[0]
        ruleset["rules"].reverse()
        ruleset.update(source_type="Repository", source=CONSUMER, created_at=NOW)
        ruleset["rules"][0]["parameters"]["required_status_checks"][0]["further"] = 1
        synced = self.run_sync()
        self.assertNotIn(CONSUMER, synced.summary)

    def test_the_bypass_list_that_the_app_cannot_read_is_not_a_difference(self) -> None:
        self.consumer().rulesets[0]["bypass_actors"] = []
        self.assertNotIn(CONSUMER, self.run_sync().summary)
        self.assertIn("`bypass_actors` is `[]`", self.local().summary)


class LocalModeTests(SyncFixture):
    def test_the_local_mode_corrects_merged_branch_deletion_with_the_owner_token(self) -> None:
        consumer = self.consumer()
        consumer.settings["delete_branch_on_merge"] = False
        synced = self.start("sync", "--local", "--repository", CONSUMER, token=OWNER_TOKEN)
        self.assertEqual(0, synced.status, synced.stderr)
        self.assertTrue(consumer.settings["delete_branch_on_merge"])
        self.assertIn("Corrected: `delete_branch_on_merge` is `false`, expected `true`.", synced.summary)
        written = settings_writes(self.api.writes)
        self.assertEqual(1, len(written))
        self.assertEqual(("PATCH", f"/repos/{CONSUMER}", {"delete_branch_on_merge": True}, OWNER_TOKEN),
                         (written[0].method, written[0].path, written[0].body, written[0].token))

    def test_matching_merged_branch_deletion_needs_no_comparison_finding_or_write(self) -> None:
        consumer = self.consumer()
        consumer.settings["delete_branch_on_merge"] = True
        for synced in (self.run_sync(), self.local()):
            self.assertEqual(0, synced.status, synced.stderr)
            self.assertNotIn("delete_branch_on_merge", synced.summary)
        self.assertEqual([], settings_writes(self.api.writes))

    def test_the_local_mode_corrects_the_merge_settings_and_the_ruleset_with_the_owner_s_token(self) -> None:
        consumer = self.consumer()
        consumer.settings.update(allow_rebase_merge=True, allow_auto_merge=False)
        consumer.rulesets[0]["enforcement"] = "disabled"
        synced = self.local()
        self.assertEqual(0, synced.status, synced.stderr)
        self.assertEqual(MERGE_SETTINGS, consumer.settings)
        self.assertEqual({"id": 7, **expected_ruleset()}, consumer.rulesets[0])
        self.assertIn("Corrected: `allow_rebase_merge` is `true`, expected `false`.", synced.summary)
        self.assertIn('Corrected: the ruleset `main checks`: `enforcement` is `"disabled"`, expected `"active"`.', synced.summary)
        self.assertNotIn("Settings difference", synced.summary)
        written = settings_writes(self.api.writes)
        self.assertEqual([("PATCH", f"/repos/{CONSUMER}"), ("PUT", f"/repos/{CONSUMER}/rulesets/7")], [(item.method, item.path) for item in written])
        self.assertEqual({"allow_rebase_merge": False, "allow_auto_merge": True}, written[0].body)
        self.assertTrue(all(item.token == OWNER_TOKEN for item in self.api.writes))

    def test_the_local_mode_creates_a_missing_ruleset(self) -> None:
        consumer = self.consumer()
        consumer.rulesets.clear()
        self.assertEqual(0, self.local().status)
        self.assertEqual(expected_ruleset(), {key: value for key, value in consumer.rulesets[0].items() if key != "id"})

    def test_the_local_mode_leaves_matching_settings_alone_and_opens_the_pull_request_for_files(self) -> None:
        consumer = self.consumer({CI: "name: other\n"})
        synced = self.local()
        self.assertEqual(0, synced.status, synced.stderr)
        self.assertEqual([], settings_writes(self.api.writes))
        self.assertEqual(SYNC_BRANCH, consumer.pulls[0]["head"]["ref"])

    def test_the_local_mode_names_its_repositories_and_asks_for_no_installation(self) -> None:
        self.consumer()
        self.local()
        self.assertEqual([], [path for _, path, _ in self.api.requests if path.startswith("/installation")])
        self.assertEqual(2, self.run_sync("sync", "--local", token=OWNER_TOKEN).status)
        self.assertEqual(2, self.run_sync("sync", "--repository", CONSUMER).status)
        refused = self.run_sync("sync", "--local", "--repository", SHARED, token=OWNER_TOKEN)
        self.assertEqual(1, refused.status)
        self.assertIn("the repository of the shared parts", refused.stderr)


class IssueTests(SyncFixture):
    """A run with ``--own-repository`` opens its issues in that repository with the token of the run."""

    def setUp(self) -> None:
        super().setUp()
        self.management = self.api.repository(MANAGEMENT, {"README.md": "The management repository.\n"}, installed=False)

    def scheduled(self, *arguments: str):
        return self.run_sync("sync", "--own-repository", MANAGEMENT, *arguments)

    def titles(self) -> list[str]:
        return [issue["title"] for issue in self.management.issues]

    def test_merged_branch_deletion_difference_is_reported_once_without_settings_writes(self) -> None:
        consumer = self.consumer()
        consumer.settings["delete_branch_on_merge"] = False
        for _ in range(2):
            synced = self.start("sync", "--own-repository", MANAGEMENT)
            self.assertEqual(0, synced.status, synced.stderr)
        self.assertEqual([f"Settings differ: {CONSUMER}"], self.titles())
        self.assertIn("- `delete_branch_on_merge` is `false`, expected `true`", self.management.issues[0]["body"])
        self.assertEqual([], settings_writes(self.api.writes))
        self.assertFalse(consumer.settings["delete_branch_on_merge"])

    def test_a_run_without_findings_opens_no_issue(self) -> None:
        self.consumer()
        self.consumer({CI: "name: other\n"}, name=f"{CONSUMER}-behind")
        synced = self.scheduled()
        self.assertEqual(0, synced.status, synced.stderr)
        self.assertEqual([], self.management.issues, "a pull request that was just opened is no finding")
        self.assertEqual([], [item for item in self.api.writes if item.token == RUN_TOKEN])

    def test_a_sync_pull_request_that_is_open_after_six_hours_gets_one_issue(self) -> None:
        consumer = self.consumer({CI: "name: other\n"})
        consumer.refs[f"heads/{SYNC_BRANCH}"] = consumer.refs["heads/main"]
        pull = consumer.pull_request(SYNC_BRANCH, "2026-03-10T06:00:00Z")
        synced = self.scheduled()
        self.assertEqual(0, synced.status, synced.stderr)
        self.assertEqual([f"Sync pull request not merged after six hours: {CONSUMER}#{pull['number']}"], self.titles())
        self.assertIn(pull["html_url"], self.management.issues[0]["body"])
        opened = [item for item in self.api.writes if item.path == f"/repos/{MANAGEMENT}/issues"]
        self.assertEqual([RUN_TOKEN], [item.token for item in opened], "the App holds no right in the run's own repository")
        self.assertEqual(1, len(consumer.pulls))

    def test_a_sync_pull_request_that_is_open_for_less_than_six_hours_gets_none(self) -> None:
        consumer = self.consumer({CI: "name: other\n"})
        consumer.refs[f"heads/{SYNC_BRANCH}"] = consumer.refs["heads/main"]
        consumer.pull_request(SYNC_BRANCH, "2026-03-10T06:00:01Z")
        self.scheduled()
        self.assertEqual([], self.titles())

    def test_an_open_request_for_shared_ci_gets_one_issue_and_a_pull_request_with_the_label_none(self) -> None:
        consumer = self.consumer()
        request = consumer.issue("A part for release notes", labels=("shared-ci",))
        consumer.issue("Another matter", labels=("type:feature",))
        consumer.issue("Closed request", labels=("shared-ci",), state="closed")
        consumer.issue("A pull request", labels=("shared-ci",), pull_request=True)
        self.scheduled()
        self.assertEqual([f"Request for shared CI: {CONSUMER}#{request['number']}"], self.titles())
        self.assertIn("A part for release notes", self.management.issues[0]["body"])
        self.assertEqual([], self.api.writes_to(CONSUMER))

    def test_a_settings_difference_gets_one_issue_that_lists_the_differences(self) -> None:
        self.consumer().settings["allow_rebase_merge"] = True
        self.scheduled()
        self.assertEqual([f"Settings differ: {CONSUMER}"], self.titles())
        self.assertIn("- `allow_rebase_merge` is `true`, expected `false`", self.management.issues[0]["body"])

    def test_an_unchanged_finding_opens_no_second_issue(self) -> None:
        consumer = self.consumer({CI: "name: other\n"})
        consumer.settings["allow_rebase_merge"] = True
        consumer.issue("A part for release notes", labels=("shared-ci",))
        consumer.refs[f"heads/{SYNC_BRANCH}"] = consumer.refs["heads/main"]
        consumer.pull_request(SYNC_BRANCH, "2026-03-09T12:00:00Z")
        self.scheduled()
        self.assertEqual(3, len(self.management.issues))
        synced = self.scheduled()
        self.assertEqual(0, synced.status, synced.stderr)
        self.assertEqual(3, len(self.management.issues))

    def test_a_closed_issue_of_a_request_or_a_pull_request_stays_closed_and_a_settings_difference_is_reported_again(self) -> None:
        consumer = self.consumer()
        consumer.settings["allow_rebase_merge"] = True
        consumer.issue("A part for release notes", labels=("shared-ci",))
        self.scheduled()
        for issue in self.management.issues:
            issue["state"] = "closed"
        self.scheduled()
        self.assertEqual([f"Settings differ: {CONSUMER}", f"Request for shared CI: {CONSUMER}#101", f"Settings differ: {CONSUMER}"], self.titles())

    def test_a_settings_difference_that_changed_gets_a_new_issue(self) -> None:
        consumer = self.consumer()
        consumer.settings["allow_rebase_merge"] = True
        self.scheduled()
        consumer.settings["allow_merge_commit"] = False
        self.scheduled()
        self.assertEqual(2, len(self.management.issues))

    def test_the_findings_are_found_again_beyond_the_first_page_of_issues(self) -> None:
        self.consumer().settings["allow_rebase_merge"] = True
        self.scheduled()
        first = self.management.issues.pop()
        for number in range(120):
            self.management.issue(f"An older issue {number}", state="closed")
        self.management.issues.append(first)
        self.scheduled()
        self.assertEqual(121, len(self.management.issues))

    def test_a_failed_run_opens_one_issue_with_the_run_s_address_until_it_is_closed(self) -> None:
        run = "https://github.example/example-owner/management/actions/runs/5"
        for _ in range(2):
            failed = self.start("failure", "--own-repository", MANAGEMENT, "--run-url", run)
            self.assertEqual(0, failed.status, failed.stdout + failed.stderr)
        self.assertEqual(["The sync failed"], self.titles())
        self.assertIn(run, self.management.issues[0]["body"])
        self.management.issues[0]["state"] = "closed"
        self.run_sync("failure", "--own-repository", MANAGEMENT, "--run-url", run)
        self.assertEqual(2, len(self.management.issues))

    def test_the_issues_of_the_other_repositories_are_opened_when_one_repository_fails(self) -> None:
        self.consumer({CI: "name: other\n"})
        self.consumer(name=f"{CONSUMER}-two").settings["allow_rebase_merge"] = True
        self.api.refused[f"/repos/{CONSUMER}/rulesets"] = 500
        synced = self.scheduled()
        self.assertEqual(1, synced.status)
        self.assertEqual([f"Settings differ: {CONSUMER}-two"], self.titles())

    def test_the_app_token_is_never_sent_to_the_run_s_own_repository(self) -> None:
        self.consumer().settings["allow_rebase_merge"] = True
        self.scheduled()
        self.assertEqual([], [path for _, path, token in self.api.requests if path.startswith(f"/repos/{MANAGEMENT}") and token == APP_TOKEN])


if __name__ == "__main__":
    unittest.main()
