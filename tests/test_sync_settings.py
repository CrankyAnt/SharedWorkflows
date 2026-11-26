"""The sync's settings comparison and its issues: with the App's token a ruleset or merge setting that differs is
reported and never changed, the local mode corrects it, and a run opens one issue in its own repository per finding."""

from __future__ import annotations

import copy
import unittest

from tests.consumer_checks_fixtures import SHARED, SYNC_BRANCH, WORKFLOWS
from tests.pipeline_fixtures import CONFIGURATION
from tests.sync_fixtures import (APP_TOKEN, CONSUMER, MANAGEMENT, MERGE_SETTINGS, NOW, OWNER_TOKEN, RULESET, RUN_TOKEN, SyncFixture,
                                 expected_ruleset, template)

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
        self.assertEqual([{"context": "ci / verdict", "integration_id": 15368}],
                         RULESET["rules"][2]["parameters"]["required_status_checks"],
                         "the template requires only ci / verdict")
        legacy = copy.deepcopy(RULESET)
        legacy["rules"][2]["parameters"]["required_status_checks"].insert(0, {"context": "hygiene / hygiene", "integration_id": 15368})
        consumer.rulesets[0] = {"id": 7, **legacy}
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


class _ManagementFixture(SyncFixture):
    """Shared fixture for tests that run with ``--own-repository`` against the management repository."""

    def setUp(self) -> None:
        super().setUp()
        self.management = self.api.repository(MANAGEMENT, {"README.md": "The management repository.\n"}, installed=False)

    def scheduled(self, *arguments: str):
        return self.run_sync("sync", "--own-repository", MANAGEMENT, *arguments)

    def titles(self) -> list[str]:
        return [issue["title"] for issue in self.management.issues]

    def closure_comments(self, issue: dict) -> list[str]:
        path = f"/repos/{MANAGEMENT}/issues/{issue['number']}/comments"
        return [write.body["body"] for write in self.api.writes if write.path == path and write.method == "POST"]


class IssueTests(_ManagementFixture):
    """A run with ``--own-repository`` opens its issues in that repository with the token of the run."""

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

    def test_request_finding_stays_open_until_the_sync_reads_the_closed_request(self) -> None:
        consumer = self.consumer()
        request = consumer.issue("A part for release notes", labels=("shared-ci",))
        self.assertEqual(0, self.scheduled().status)
        finding = self.management.issues[0]

        request["labels"] = []
        self.assertEqual(0, self.scheduled().status)
        self.assertEqual("open", finding["state"])

        request["state"] = "closed"
        self.assertEqual(0, self.scheduled().status)
        self.assertEqual("closed", finding["state"])
        issue_path = f"/repos/{MANAGEMENT}/issues/{finding['number']}"
        comments = [write for write in self.api.writes if write.path == f"{issue_path}/comments"]
        self.assertEqual(1, len(comments))
        self.assertEqual(RUN_TOKEN, comments[0].token)
        self.assertIn(f"{CONSUMER}#{request['number']}", comments[0].body["body"])
        self.assertIn("closed", comments[0].body["body"])

        request["state"] = "open"
        request["labels"] = [{"name": "shared-ci"}]
        self.assertEqual(0, self.scheduled().status)
        self.assertEqual([finding], self.management.issues)
        self.assertEqual("closed", finding["state"])
        self.assertEqual(comments, [write for write in self.api.writes if write.path == f"{issue_path}/comments"])

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
        consumer = self.consumer({CI: "name: other\n"})
        consumer.settings["allow_rebase_merge"] = True
        request = consumer.issue("A part for release notes", labels=("shared-ci",))
        consumer.refs[f"heads/{SYNC_BRANCH}"] = consumer.refs["heads/main"]
        pull = consumer.pull_request(SYNC_BRANCH, "2026-03-09T12:00:00Z")
        self.assertEqual(0, self.scheduled().status)
        self.assertEqual(3, len(self.management.issues))
        for issue in self.management.issues:
            issue["state"] = "closed"
        self.assertEqual(0, self.scheduled().status)
        self.assertEqual([
            f"Settings differ: {CONSUMER}",
            f"Request for shared CI: {CONSUMER}#{request['number']}",
            f"Sync pull request not merged after six hours: {CONSUMER}#{pull['number']}",
            f"Settings differ: {CONSUMER}",
        ], self.titles())
        self.assertEqual(["closed", "closed", "closed", "open"], [issue["state"] for issue in self.management.issues])

    def test_a_settings_difference_that_changed_gets_a_new_issue(self) -> None:
        consumer = self.consumer()
        consumer.settings["allow_rebase_merge"] = True
        self.scheduled()
        consumer.settings["allow_merge_commit"] = False
        self.scheduled()
        self.assertEqual(2, len(self.management.issues))
        old, current = self.management.issues
        self.assertEqual("closed", old["state"])
        self.assertEqual([f"Superseded by #{current['number']}"], self.closure_comments(old))
        self.assertEqual("open", current["state"])

        consumer.settings["allow_merge_commit"] = True
        consumer.settings["allow_rebase_merge"] = False
        self.assertEqual(0, self.scheduled().status)
        self.assertEqual("closed", current["state"])
        self.assertEqual([f"The settings of {CONSUMER} match again."], self.closure_comments(current))

    def test_no_changes_and_an_unreadable_pull_request_do_not_confirm_closure(self) -> None:
        consumer = self.consumer({CI: "name: other\n"})
        consumer.refs[f"heads/{SYNC_BRANCH}"] = consumer.refs["heads/main"]
        pull = consumer.pull_request(SYNC_BRANCH, "2026-03-09T12:00:00Z")
        self.assertEqual(0, self.scheduled().status)
        finding = self.management.issues[0]
        consumer.push({CI: template(CI)})
        self.assertEqual(0, self.scheduled().status)
        self.assertEqual("open", finding["state"])

        pull.update(state="closed", merged=True)
        path = f"/repos/{CONSUMER}/pulls/{pull['number']}"
        for status in (404, 500):
            self.api.refused[path] = status
            self.assertEqual(0, self.scheduled().status)
            self.assertEqual("open", finding["state"])
        del self.api.refused[path]
        self.assertEqual(0, self.scheduled().status)
        self.assertEqual("closed", finding["state"])
        self.assertEqual([f"The sync pull request {CONSUMER}#{pull['number']} merged."], self.closure_comments(finding))
        self.assertIn(("GET", path, APP_TOKEN), self.api.requests)

        pull.update(state="open", merged=False)
        consumer.push({CI: "name: other\n"})
        self.assertEqual(0, self.scheduled().status)
        self.assertEqual([finding], self.management.issues)

    def test_a_sync_pull_request_closed_without_merging_retires_its_finding(self) -> None:
        consumer = self.consumer({CI: "name: other\n"})
        consumer.refs[f"heads/{SYNC_BRANCH}"] = consumer.refs["heads/main"]
        pull = consumer.pull_request(SYNC_BRANCH, "2026-03-09T12:00:00Z")
        self.assertEqual(0, self.scheduled().status)
        finding = self.management.issues[0]

        pull["state"] = "closed"
        self.assertEqual(0, self.scheduled().status)
        self.assertEqual("closed", finding["state"])
        self.assertEqual([f"The sync pull request {CONSUMER}#{pull['number']} is closed."], self.closure_comments(finding))

    def test_a_late_sync_failure_and_a_skipped_settings_check_leave_existing_settings_open(self) -> None:
        consumer = self.consumer()
        consumer.settings["allow_rebase_merge"] = True
        self.assertEqual(0, self.scheduled().status)
        finding = self.management.issues[0]

        consumer.settings["allow_rebase_merge"] = False
        consumer.push({CONFIGURATION: None})
        self.assertEqual(0, self.scheduled().status)
        self.assertEqual("open", finding["state"])

        consumer.push({CONFIGURATION: "version: 1\n", CI: "name: other\n"})
        self.api.refused[f"/repos/{CONSUMER}/git/trees"] = 500
        failed = self.scheduled()
        self.assertEqual(1, failed.status, failed.stderr)
        self.assertIn("/git/trees", failed.summary)
        self.assertEqual("open", finding["state"])
        self.assertEqual([], self.closure_comments(finding))

        self.api.refused.clear()
        self.assertEqual(0, self.scheduled().status)
        self.assertEqual("closed", finding["state"])

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

    def test_a_successful_workflow_closes_only_the_sync_failure_issue_once(self) -> None:
        self.consumer()
        run = "https://github.example/example-owner/management/actions/runs/5"
        self.assertEqual(0, self.run_sync("failure", "--own-repository", MANAGEMENT, "--run-url", run).status)
        finding = self.management.issues[0]
        measurement = self.management.issue("Measurement failure", "<!-- measurement-finding: failure -->")
        pull_request = self.management.issue("Sync reference", "<!-- sync-finding: failure -->", pull_request=True)
        for issue in (measurement, pull_request):
            issue["user"] = {"login": "github-actions[bot]"}
        others = [
            measurement,
            pull_request,
            self.management.issue("An issue quoting the marker", "<!-- sync-finding: failure -->"),
            self.management.issue("An unrelated issue"),
        ]
        self.api.refused[f"/repos/{CONSUMER}/rulesets"] = 500
        self.assertEqual(1, self.scheduled().status)
        self.assertEqual("open", finding["state"])
        self.assertEqual([], self.closure_comments(finding))

        self.api.refused.clear()
        self.assertEqual(0, self.scheduled().status)
        for _ in range(2):
            self.assertEqual(0, self.run_sync("success", "--own-repository", MANAGEMENT).status)
        self.assertEqual("closed", finding["state"])
        self.assertEqual(["A later run of the sync succeeded."], self.closure_comments(finding))
        self.assertTrue(all(issue["state"] == "open" and not self.closure_comments(issue) for issue in others))

    def test_archived_and_removed_repositories_retire_findings_but_a_partial_listing_does_not(self) -> None:
        archived = self.consumer()
        removed = self.consumer(name=f"{CONSUMER}-removed")
        for repository in (archived, removed):
            repository.issue("A part for release notes", labels=("shared-ci",))
            repository.settings["allow_rebase_merge"] = True
        self.assertEqual(0, self.scheduled().status)
        archived_findings, removed_findings = self.management.issues[:2], self.management.issues[2:]
        self.assertEqual((2, 2), (len(archived_findings), len(removed_findings)))

        archived.archived = True
        self.api.installed.remove(removed.full_name)
        self.api.installation_total_count = 2
        self.assertEqual(0, self.scheduled().status)
        for issue in archived_findings:
            self.assertEqual("closed", issue["state"])
            self.assertEqual([f"The repository {archived.full_name} is archived."], self.closure_comments(issue))
        for issue in removed_findings:
            self.assertEqual("open", issue["state"])
            self.assertEqual([], self.closure_comments(issue))

        self.api.installation_total_count = None
        self.assertEqual(0, self.scheduled().status)
        for issue in removed_findings:
            self.assertEqual("closed", issue["state"])
            self.assertEqual([f"The repository {removed.full_name} is no longer installed for the sync App."], self.closure_comments(issue))

    def test_a_failed_close_retries_without_a_second_comment(self) -> None:
        consumer = self.consumer()
        request = consumer.issue("A part for release notes", labels=("shared-ci",))
        self.assertEqual(0, self.scheduled().status)
        finding = self.management.issues[0]
        request["state"] = "closed"

        def refuse_close(method: str, path: str) -> None:
            if method == "POST" and path.endswith("/comments"):
                self.api.refused[path.removesuffix("/comments")] = 500

        self.api.on_request = refuse_close
        self.assertEqual(1, self.scheduled().status)
        self.assertEqual("open", finding["state"])
        self.api.on_request = None
        self.api.refused.clear()
        self.assertEqual(0, self.scheduled().status)
        self.assertEqual("closed", finding["state"])
        self.assertEqual([f"The shared CI request {CONSUMER}#{request['number']} is closed."], self.closure_comments(finding))

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


class WorkflowInspectionTests(_ManagementFixture):
    """The sync inspects direct workflow files under ``.github/workflows/`` on every reached repository's default
    branch, reports findings without editing or removing non-fixed workflows, and opens one deduplicated issue per
    repository and finding set."""

    def test_workflow_findings_close_after_replacement_and_recovery(self) -> None:
        first_path = f"{WORKFLOWS}/first.yml"
        second_path = f"{WORKFLOWS}/second.yml"
        consumer = self.consumer({first_path: "on: push\n"})
        self.assertEqual(0, self.scheduled().status)
        first = self.management.issues[0]
        older = self.management.issue("An earlier workflow finding", f"<!-- sync-finding: workflows:{CONSUMER}:000000000000 -->")
        older["user"] = {"login": "github-actions[bot]"}
        consumer.push({first_path: None, second_path: "on: push\n"})
        self.assertEqual(0, self.scheduled().status)
        current = self.management.issues[2]
        for issue in (first, older):
            self.assertEqual("closed", issue["state"])
            self.assertEqual([f"Superseded by #{current['number']}"], self.closure_comments(issue))
        self.assertEqual("open", current["state"])

        consumer.push({second_path: None})
        self.assertEqual(0, self.scheduled().status)
        self.assertEqual("closed", current["state"])
        self.assertEqual([f"The workflows of {CONSUMER} match again."], self.closure_comments(current))

    def test_unwanted_workflows_are_reported_in_scheduled_and_local_modes_without_editing_them_and_deduplicated_as_one_issue(self) -> None:
        unregistered = f"{WORKFLOWS}/stray.yml"
        shared_caller = f"{WORKFLOWS}/legacy.yml"
        uncallable = f"{WORKFLOWS}/orphan.yml"
        unreadable = f"{WORKFLOWS}/broken.yaml"
        called = f"{SHARED}/.github/workflows/old-suite.yml@v1"
        config = (f"version: 1\nlocal-workflows:\n"
                  f"  - workflow: {shared_caller}\n    request: 10\n    waits-for: python-test\n"
                  f"  - workflow: {uncallable}\n    request: 11\n    waits-for: python-test\n"
                  f"  - workflow: {unreadable}\n    request: 12\n    waits-for: python-test\n")
        consumer = self.consumer({
            CONFIGURATION: config,
            unregistered: "name: Stray\non:\n  workflow_dispatch:\njobs:\n  run:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n",
            shared_caller: f"name: Legacy\non:\n  push:\njobs:\n  run:\n    uses: {called}\n",
            uncallable: "name: Orphan\non:\n  workflow_call:\njobs:\n  run:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n",
            unreadable: "on: [unclosed\n",
        })
        synced = self.scheduled()
        self.assertEqual(0, synced.status, synced.stdout + synced.stderr)
        for text in (synced.stdout, synced.summary):
            self.assertIn(f"`{unregistered}` is neither a fixed caller nor registered", text)
            self.assertIn(f"`{shared_caller}` calls the shared workflow `{called}`", text)
            self.assertIn(f"`{uncallable}` has only `workflow_call`", text)
            self.assertIn(f"`{unreadable}` could not be read as YAML", text)
        self.assertEqual([], consumer.pulls)
        self.assertEqual([], self.api.writes_to(CONSUMER))
        self.assertEqual([f"Unwanted workflows: {CONSUMER}"], self.titles())
        body = self.management.issues[0]["body"]
        for path in (unregistered, shared_caller, uncallable, unreadable):
            self.assertIn(f"`{path}`", body)
        # Second scheduled run opens no duplicate while identical issue is open.
        self.assertEqual(0, self.scheduled().status)
        self.assertEqual(1, len(self.management.issues))
        # Closing the issue reopens it while findings remain.
        self.management.issues[0]["state"] = "closed"
        self.assertEqual(0, self.scheduled().status)
        self.assertEqual(2, len(self.management.issues))
        # Local mode also reports the findings in stdout and summary without touching the workflows.
        local = self.local()
        self.assertEqual(0, local.status, local.stdout + local.stderr)
        for path in (unregistered, shared_caller, uncallable, unreadable):
            self.assertIn(f"`{path}`", local.stdout)
            self.assertIn(f"`{path}`", local.summary)
        self.assertEqual([], consumer.pulls)
        self.assertEqual([], self.api.writes_to(CONSUMER))

    def test_unadopted_repository_checks_retired_shared_calls_and_uncallable_workflows_alongside_not_adopted_note(self) -> None:
        active = f"{WORKFLOWS}/pipeline-caller.yml"
        retired = f"{WORKFLOWS}/old.yml"
        orphan = f"{WORKFLOWS}/unused.yml"
        retired_target = f"{SHARED}/.github/workflows/retired-legacy.yml@v1"
        consumer = self.consumer({
            CONFIGURATION: None,
            CI: None,
            f"{WORKFLOWS}/automation.yml": None,
            f"{WORKFLOWS}/release.yml": None,
            active: f"name: Pipeline\non:\n  pull_request:\njobs:\n  check:\n    uses: {SHARED}/.github/workflows/pipeline.yml@v1\n",
            retired: f"name: Old\non:\n  pull_request:\njobs:\n  run:\n    uses: {retired_target}\n",
            orphan: "name: Unused\non:\n  workflow_call:\njobs:\n  run:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n",
        })
        synced = self.scheduled()
        self.assertEqual(0, synced.status, synced.stdout + synced.stderr)
        self.assertIn(f"`{CONSUMER}` holds no `{CONFIGURATION}`", synced.summary)
        self.assertIn(f"`{retired}` calls the retired shared workflow `{retired_target}`", synced.summary)
        self.assertIn(f"`{orphan}` has only `workflow_call`", synced.summary)
        self.assertNotIn(active, synced.summary)
        self.assertEqual([], consumer.pulls)
        self.assertEqual([], self.api.writes_to(CONSUMER))
        self.assertEqual([f"Unwanted workflows: {CONSUMER}"], self.titles())

    def test_clean_consumer_with_fixed_callers_and_valid_registered_callable_workflows_produces_no_findings(self) -> None:
        caller_wf = f"{WORKFLOWS}/nightly.yml"
        helper_wf = f"{WORKFLOWS}/helper.yml"
        config = (f"version: 1\nlocal-workflows:\n"
                  f"  - workflow: {caller_wf}\n    request: 10\n    waits-for: python-release\n"
                  f"  - workflow: {helper_wf}\n    request: 11\n    waits-for: python-release\n")
        self.consumer({
            CONFIGURATION: config,
            caller_wf: f"name: Nightly\non:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n  run:\n    uses: ./{helper_wf}\n",
            helper_wf: "name: Helper\non:\n  workflow_call:\njobs:\n  work:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n",
        })
        synced = self.scheduled()
        self.assertEqual(0, synced.status, synced.stdout + synced.stderr)
        self.assertNotIn(CONSUMER, synced.summary)
        self.assertEqual([], self.management.issues)


if __name__ == "__main__":
    unittest.main()
