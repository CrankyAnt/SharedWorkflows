"""The sync's file comparison: a consumer whose fixed callers, CI guide or generated Dependabot configuration
differ from the templates gets one pull request with auto-merge, and a consumer that matches gets nothing."""

from __future__ import annotations

import unittest

from tests.consumer_checks_fixtures import SHARED, SYNC_BRANCH, WORKFLOWS, template
from tests.pipeline_fixtures import CONFIGURATION, PART_FOLDER, write
from tests.sync_fixtures import APP_TOKEN, CONSUMER, DEPENDABOT, MANAGEMENT, OTHER, CI_GUIDE, CI_GUIDE_TEXT, SyncFixture
from tests.test_pipeline_dependabot import GENERATED, STATED

CI = f"{WORKFLOWS}/ci.yml"


class FileDifferenceTests(SyncFixture):
    def test_a_consumer_whose_fixed_files_differ_gets_one_pull_request_with_the_templates_content_and_auto_merge(self) -> None:
        consumer = self.consumer({CI: template("ci.yml", " changed locally"), f"{WORKFLOWS}/release.yml": None, CI_GUIDE: "# CI\n\nOur own rules.\n",
                                  "src/code.txt": "the repository's own work\n"})
        synced = self.start()
        self.assertEqual(0, synced.status, synced.stdout + synced.stderr)
        self.assertEqual(1, len(consumer.pulls))
        pull = consumer.pulls[0]
        self.assertEqual((SYNC_BRANCH, "main"), (pull["head"]["ref"], pull["base"]["ref"]))
        self.assertEqual({"merge_method": "squash"}, pull["auto_merge"])
        self.assertRegex(pull["title"], r"^chore: \S")
        branch = consumer.files(SYNC_BRANCH)
        self.assertEqual({**consumer.files(), CI: template("ci.yml"), f"{WORKFLOWS}/release.yml": template("release.yml"), CI_GUIDE: CI_GUIDE_TEXT}, branch)
        self.assertEqual("the repository's own work\n", branch["src/code.txt"])
        self.assertEqual([consumer.refs["heads/main"]], consumer.commits[consumer.refs[f"heads/{SYNC_BRANCH}"]]["parents"])
        self.assertIn(f"`{CONSUMER}`", synced.summary)
        self.assertIn(pull["html_url"], synced.summary)
        for path in (CI, f"{WORKFLOWS}/release.yml", CI_GUIDE):
            self.assertIn(f"`{path}`", synced.summary)
        self.assertTrue(all(item.token == APP_TOKEN for item in self.api.writes))

    def test_the_sync_stops_after_opening_and_reads_no_pull_request_again(self) -> None:
        consumer = self.consumer({CI: "name: other\n"})
        self.assertEqual(0, self.run_sync().status)
        after_opening = [index for index, (method, path, _) in enumerate(self.api.requests) if method == "POST" and path == f"/repos/{CONSUMER}/pulls"]
        self.assertEqual(1, len(after_opening))
        later = [(method, path) for method, path, _ in self.api.requests[after_opening[0] + 1:]]
        self.assertEqual([("POST", "/graphql")], later, "after the pull request only auto-merge is requested")
        self.assertEqual("open", consumer.pulls[0]["state"])

    def test_a_consumer_that_matches_gets_nothing_and_is_not_named(self) -> None:
        self.consumer()
        synced = self.start()
        self.assertEqual(0, synced.status, synced.stdout + synced.stderr)
        self.assertEqual([], self.api.writes)
        self.assertNotIn(CONSUMER, synced.summary + synced.stdout)
        self.assertIn("Every repository that the sync reached matches the templates.", synced.summary)

    def test_only_the_consumer_that_differs_gets_a_pull_request(self) -> None:
        matching = self.consumer()
        behind = self.consumer({CI: "name: other\n"}, name=OTHER)
        synced = self.run_sync()
        self.assertEqual(0, synced.status, synced.stderr)
        self.assertEqual([], matching.pulls)
        self.assertEqual(1, len(behind.pulls))
        self.assertNotIn(f"`{CONSUMER}`", synced.summary)

    def test_a_second_run_opens_no_second_pull_request_and_leaves_the_branch(self) -> None:
        consumer = self.consumer({CI: "name: other\n"})
        self.run_sync()
        tip, writes = consumer.refs[f"heads/{SYNC_BRANCH}"], len(self.api.writes_to(CONSUMER))
        synced = self.run_sync()
        self.assertEqual(0, synced.status, synced.stderr)
        self.assertEqual(1, len(consumer.pulls))
        self.assertEqual(tip, consumer.refs[f"heads/{SYNC_BRANCH}"])
        self.assertEqual([], [item for item in self.api.writes_to(CONSUMER)[writes:] if "/git/trees" not in item.path], "only the tree is computed again")

    def test_a_template_that_changed_after_the_pull_request_opened_moves_the_branch_of_the_same_pull_request(self) -> None:
        consumer = self.consumer({CI: "name: other\n"})
        self.run_sync()
        (self.kit / "ci.yml").write_text(template("ci.yml", " newer"), encoding="utf-8", newline="\n")
        consumer.push({"src/later.txt": "work that landed meanwhile\n"})
        self.assertEqual(0, self.run_sync().status)
        self.assertEqual(1, len(consumer.pulls))
        self.assertEqual(template("ci.yml", " newer"), consumer.files(SYNC_BRANCH)[CI])
        self.assertEqual("work that landed meanwhile\n", consumer.files(SYNC_BRANCH)["src/later.txt"])

    def test_an_open_pull_request_without_auto_merge_gets_it_again(self) -> None:
        consumer = self.consumer({CI: "name: other\n"})
        self.run_sync()
        consumer.pulls[0]["auto_merge"] = None
        self.run_sync()
        self.assertEqual({"merge_method": "squash"}, consumer.pulls[0]["auto_merge"])

    def test_auto_merge_that_github_refuses_is_said_and_the_pull_request_stays_open(self) -> None:
        consumer = self.consumer({CI: "name: other\n"})
        consumer.auto_merge_allowed = False
        synced = self.run_sync()
        self.assertEqual(1, len(consumer.pulls))
        self.assertIn("auto-merge could not be enabled: Auto merge is not allowed for this repository", synced.summary)

    def test_one_sync_run_reads_each_consumer_s_files_from_one_snapshot(self) -> None:
        consumer = self.consumer({CONFIGURATION: STATED, DEPENDABOT: GENERATED})
        pushed = False

        def push_mid_sync(method: str, path: str) -> None:
            nonlocal pushed
            if not pushed and (path.startswith(f"/repos/{CONSUMER}/contents/") or (method == "POST" and path == "/graphql")):
                pushed = True
                consumer.push({DEPENDABOT: GENERATED.replace("weekly", "daily")})

        self.api.on_request = push_mid_sync
        synced = self.run_sync()
        self.assertEqual(0, synced.status, synced.stderr)
        self.assertTrue(pushed)
        self.assertEqual([], consumer.pulls)

    def test_a_sync_run_makes_fewer_api_calls_for_matching_and_differing_consumers(self) -> None:
        self.consumer()
        self.api.repository(MANAGEMENT, {"README.md": "The management repository.\n"}, installed=False)
        synced = self.run_sync("sync", "--own-repository", MANAGEMENT)
        self.assertEqual(0, synced.status, synced.stderr)
        self.assertEqual(5, len(self.api.requests))
        self.api.requests.clear()
        self.consumer({CI: "name: other\n"}, name=OTHER)
        synced = self.run_sync("sync", "--own-repository", MANAGEMENT)
        self.assertEqual(0, synced.status, synced.stderr)
        # 1 installation/repositories + 4 for matching consumer + 10 for differing consumer = 15 total.
        self.assertEqual(15, len(self.api.requests))


class RepositoryChoicesTests(SyncFixture):
    """A template of the CI guide that ends with the heading ``## Repository choices`` is compared up to and
    including that heading, and what a consumer recorded below it stays."""

    TEMPLATE = CI_GUIDE_TEXT + "\n## Repository choices\n"
    CHOICES = "\nThe ruleset keeps the merge queue, because releases are batched.\n"

    def setUp(self) -> None:
        super().setUp()
        write(self.kit, CI_GUIDE, self.TEMPLATE)

    def test_a_consumer_with_choices_below_the_heading_matches(self) -> None:
        self.consumer({CI_GUIDE: self.TEMPLATE + self.CHOICES})
        synced = self.run_sync()
        self.assertEqual(0, synced.status, synced.stderr)
        self.assertEqual([], self.api.writes)

    def test_a_change_above_the_heading_is_replaced_and_the_choices_below_it_stay(self) -> None:
        consumer = self.consumer({CI_GUIDE: self.TEMPLATE.replace("every rule", "some rules") + self.CHOICES})
        synced = self.run_sync()
        self.assertEqual(0, synced.status, synced.stderr)
        self.assertEqual(1, len(consumer.pulls))
        self.assertEqual(self.TEMPLATE + self.CHOICES, consumer.files(SYNC_BRANCH)[CI_GUIDE])

    def test_the_real_previous_guide_is_updated_and_keeps_the_consumer_choices(self) -> None:
        kit = PART_FOLDER / "consumer-kit"
        current = (kit / CI_GUIDE).read_text(encoding="utf-8")
        previous = (kit / "previous" / CI_GUIDE).read_text(encoding="utf-8")
        self.assertNotEqual(current, previous)
        write(self.kit, CI_GUIDE, current)
        consumer = self.consumer({CI_GUIDE: previous + self.CHOICES})
        synced = self.run_sync()
        self.assertEqual(0, synced.status, synced.stdout + synced.stderr)
        self.assertEqual(1, len(consumer.pulls))
        self.assertEqual(current + self.CHOICES, consumer.files(SYNC_BRANCH)[CI_GUIDE])

    def test_a_document_without_the_heading_becomes_the_template(self) -> None:
        consumer = self.consumer({CI_GUIDE: "# CI\n\nOur own rules.\n"})
        self.assertEqual(0, self.run_sync().status)
        self.assertEqual(self.TEMPLATE, consumer.files(SYNC_BRANCH)[CI_GUIDE])

    def test_a_missing_document_becomes_the_template(self) -> None:
        consumer = self.consumer({CI_GUIDE: None})
        self.assertEqual(0, self.run_sync().status)
        self.assertEqual(self.TEMPLATE, consumer.files(SYNC_BRANCH)[CI_GUIDE])


class GeneratedDependabotTests(SyncFixture):
    def test_a_dependabot_configuration_that_differs_from_the_generated_one_is_replaced_in_the_pull_request(self) -> None:
        consumer = self.consumer({CONFIGURATION: STATED, DEPENDABOT: GENERATED.replace("weekly", "daily")})
        self.assertEqual(0, self.run_sync().status)
        self.assertEqual(GENERATED, consumer.files(SYNC_BRANCH)[DEPENDABOT])

    def test_a_missing_generated_file_is_added_and_the_other_file_name_removed(self) -> None:
        consumer = self.consumer({CONFIGURATION: STATED, ".github/dependabot.yaml": GENERATED})
        self.assertEqual(0, self.run_sync().status)
        branch = consumer.files(SYNC_BRANCH)
        self.assertEqual(GENERATED, branch[DEPENDABOT])
        self.assertNotIn(".github/dependabot.yaml", branch)

    def test_a_generated_file_that_matches_gives_no_pull_request(self) -> None:
        consumer = self.consumer({CONFIGURATION: STATED, DEPENDABOT: GENERATED})
        self.assertEqual(0, self.run_sync().status)
        self.assertEqual([], consumer.pulls)

    def test_a_consumer_without_either_dependabot_file_syncs_normally(self) -> None:
        consumer = self.consumer()
        self.assertNotIn(DEPENDABOT, consumer.files())
        self.assertNotIn(".github/dependabot.yaml", consumer.files())
        synced = self.run_sync()
        self.assertEqual(0, synced.status, synced.stdout + synced.stderr)
        self.assertEqual([], consumer.pulls)

    def test_a_dependabot_configuration_without_stated_ecosystems_stays_and_is_said(self) -> None:
        consumer = self.consumer({DEPENDABOT: GENERATED})
        synced = self.run_sync()
        self.assertEqual([], consumer.pulls)
        self.assertIn(f"`{DEPENDABOT}` exists, but `{CONFIGURATION}` states no Dependabot ecosystems", synced.summary)

    def test_a_configuration_file_that_cannot_be_used_still_gets_the_fixed_callers(self) -> None:
        consumer = self.consumer({CONFIGURATION: "version: 1\ndependabot: none\n", CI: "name: other\n", DEPENDABOT: "version: 2\n"})
        synced = self.run_sync()
        self.assertEqual(template("ci.yml"), consumer.files(SYNC_BRANCH)[CI])
        self.assertEqual("version: 2\n", consumer.files(SYNC_BRANCH)[DEPENDABOT])
        self.assertIn(f"`{CONFIGURATION}` cannot be used, so `{DEPENDABOT}` was not compared", synced.summary)


class ReachTests(SyncFixture):
    def test_the_repositories_come_from_the_installation_and_from_nowhere_else(self) -> None:
        self.consumer({CI: "name: other\n"})
        outside = self.api.repository(OTHER, {CI: "name: other\n", CONFIGURATION: "version: 1\n"}, installed=False)
        self.assertEqual(0, self.run_sync().status)
        self.assertEqual(("GET", "/installation/repositories?per_page=100&page=1", APP_TOKEN), self.api.requests[0])
        self.assertEqual([], outside.pulls)
        self.assertEqual([], self.api.writes_to(OTHER))

    def test_a_repository_without_a_configuration_file_has_not_adopted_and_gets_no_pull_request(self) -> None:
        consumer = self.consumer({CONFIGURATION: None, CI: "name: its own workflow\n"})
        consumer.settings["delete_branch_on_merge"] = False
        synced = self.run_sync()
        self.assertEqual(0, synced.status)
        self.assertEqual([], self.api.writes)
        self.assertIn(f"`{CONSUMER}` holds no `{CONFIGURATION}`", synced.summary)
        self.assertEqual([], consumer.pulls)
        local = self.local()
        self.assertEqual(0, local.status, local.stderr)
        self.assertNotIn("delete_branch_on_merge", synced.summary + local.summary)
        self.assertFalse(consumer.settings["delete_branch_on_merge"])
        self.assertEqual([], self.api.writes)
        self.assertEqual([], [request for request in self.api.requests if request[1].startswith(f"/repos/{CONSUMER}/rulesets")])

    def test_an_archived_repository_and_the_run_s_own_repository_are_left_alone(self) -> None:
        self.consumer({CI: "name: other\n"}).archived = True
        self.api.repository(MANAGEMENT, {"README.md": "The management repository.\n"})
        synced = self.run_sync("sync", "--own-repository", MANAGEMENT)
        self.assertEqual(0, synced.status, synced.stderr)
        self.assertEqual([], self.api.writes)

    def test_an_installation_on_the_repository_of_the_shared_parts_fails_the_run_before_any_write(self) -> None:
        self.consumer({CI: "name: other\n"})
        self.api.repository(SHARED, {"catalogue.json": "{}\n"})
        synced = self.run_sync()
        self.assertEqual(1, synced.status)
        self.assertIn("the App is installed on the repository of the shared parts", synced.stderr)
        self.assertEqual([], self.api.writes)

    def test_a_repository_that_github_refuses_fails_the_run_after_the_others_were_synced(self) -> None:
        self.consumer({CI: "name: other\n"})
        behind = self.consumer({CI: "name: other\n"}, name=OTHER)
        self.api.refused[f"/repos/{CONSUMER}/rulesets"] = 500
        synced = self.run_sync()
        self.assertEqual(1, synced.status)
        self.assertEqual(1, len(behind.pulls))
        self.assertIn(f"`{CONSUMER}` could not be synced", synced.summary)
        self.assertIn("HTTP 500", synced.summary)

    def test_another_graphql_error_in_the_snapshot_still_fails_the_consumer(self) -> None:
        self.consumer()
        cases = (
            {"type": "FORBIDDEN", "path": ["repository", "pullRequests"], "message": "Resource not accessible by integration"},
            {"type": "INTERNAL", "path": ["repository", "defaultBranchRef", "target", "f0"], "message": "Something went wrong"},
        )
        for error in cases:
            with self.subTest(error=error):
                self.api.snapshot_errors[CONSUMER] = [error]
                synced = self.run_sync()
                self.assertEqual(1, synced.status)
                self.assertIn(f"`{CONSUMER}` could not be synced", synced.summary)
                self.assertIn(error["message"], synced.summary)
                self.assertNotIn("Could not resolve file for path", synced.summary)

    def test_without_a_token_the_sync_refuses(self) -> None:
        synced = self.run_sync(token="")
        self.assertEqual(2, synced.status)
        self.assertIn("GH_TOKEN", synced.stderr)


if __name__ == "__main__":
    unittest.main()
