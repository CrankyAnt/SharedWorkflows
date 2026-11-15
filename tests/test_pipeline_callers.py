"""The caller check of the pipeline's hygiene job: a consumer's fixed callers equal their templates, or for one month
the previous version, and every other workflow file is registered in the configuration file."""

from __future__ import annotations

import unittest

from tests.consumer_checks_fixtures import CONSUMER, SHARED, SYNC_BRANCH, WORKFLOWS, ConsumerFixture, template
from tests.pipeline_fixtures import CONFIGURATION

RULE = f"https://github.com/{SHARED}/blob/v1/catalogue.json"
LOCAL = f"{WORKFLOWS}/nightly-report.yml"
REGISTERED = f"version: 1\nlocal-workflows:\n  - workflow: {LOCAL}\n    request: 12\n    waits-for: python-release\n"


class FixedCallerTests(ConsumerFixture):
    def test_a_consumer_whose_callers_equal_the_templates_passes(self) -> None:
        checked = self.start("callers")
        self.assertEqual(0, checked.status, checked.stdout + checked.stderr)
        self.assertIn("Every fixed caller equals its template", checked.summary)
        self.assertEqual([], self.api.requests, "a consumer that is up to date asks GitHub nothing")

    def test_a_caller_that_differs_from_its_template_fails_with_the_open_sync_pull_request(self) -> None:
        url = self.api.sync_pull_request(31)
        self.api.sync_pull_request(32, branch="feat/another-branch")
        self.consumer({f"{WORKFLOWS}/ci.yml": template("ci.yml") + "# a local change\n"})
        checked = self.start("callers")
        self.assertEqual(1, checked.status, checked.stdout + checked.stderr)
        self.assertIn(f"::error file={WORKFLOWS}/ci.yml::", checked.stdout)
        self.assertIn(f"`{WORKFLOWS}/ci.yml` differs from its template", checked.summary)
        self.assertIn(f"The open sync pull request brings the fixed callers up to date: {url}", checked.summary)
        self.assertNotIn("/pull/32", checked.summary)
        path, headers = self.api.requests[0]
        self.assertEqual(f"/repos/{CONSUMER}/pulls?state=open&head=example-owner%3A{SYNC_BRANCH.replace('/', '%2F')}&per_page=1", path)
        self.assertEqual("Bearer token-for-the-test", headers["authorization"])

    def test_a_missing_caller_fails(self) -> None:
        self.consumer({f"{WORKFLOWS}/release.yml": None})
        checked = self.check("callers")
        self.assertEqual(1, checked.status)
        self.assertIn(f"`{WORKFLOWS}/release.yml` does not exist", checked.summary)
        self.assertIn(f"No sync pull request is open on the branch `{SYNC_BRANCH}`", checked.summary)
        self.assertIn(f"https://github.com/{SHARED}/tree/v1/actions/pipeline/consumer-kit", checked.summary)

    def test_a_sync_pull_request_that_cannot_be_looked_up_still_fails_the_caller(self) -> None:
        self.consumer({f"{WORKFLOWS}/ci.yml": "name: other\n"})
        self.api.status = 403
        checked = self.check("callers")
        self.assertEqual(1, checked.status)
        self.assertIn("The sync pull request could not be looked up: HTTP 403", checked.summary)


class PreviousVersionTests(ConsumerFixture):
    """A promotion on 2026-03-10 changed the template of ci.yml; the consumer still holds the version before it."""

    def setUp(self) -> None:
        super().setUp()
        self.consumer({f"{WORKFLOWS}/ci.yml": self.previous("ci.yml", "2026-03-10")})

    def test_the_previous_version_passes_for_one_month_after_the_promotion(self) -> None:
        for today in ("2026-03-10", "2026-03-25", "2026-04-10"):
            with self.subTest(today=today):
                checked = self.check("callers", "--today", today)
                self.assertEqual(0, checked.status, checked.stdout)
                self.assertIn(f"`{WORKFLOWS}/ci.yml` equals the previous version of its template, which passes until 2026-04-10", checked.summary)
                self.assertIn("::notice ", checked.stdout)

    def test_after_that_month_a_pull_request_fails_with_the_open_sync_pull_request(self) -> None:
        url = self.api.sync_pull_request(31)
        checked = self.check("callers", "--today", "2026-04-11")
        self.assertEqual(1, checked.status, checked.stdout)
        self.assertIn(f"`{WORKFLOWS}/ci.yml` equals the previous version of its template, which passed until 2026-04-10", checked.summary)
        self.assertIn(f"The open sync pull request brings the fixed callers up to date: {url}", checked.summary)

    def test_after_that_month_a_dependabot_pull_request_passes_with_a_notice(self) -> None:
        checked = self.check("callers", "--today", "2026-06-01", author="dependabot[bot]")
        self.assertEqual(0, checked.status, checked.stdout)
        self.assertIn("**Notice.** This pull request is Dependabot's, so these findings do not fail it", checked.summary)
        self.assertIn("which passed until 2026-04-10", checked.summary)
        self.assertIn("::notice ", checked.stdout)
        self.assertNotIn("::error", checked.stdout)

    def test_a_month_ends_on_the_last_day_of_a_shorter_month(self) -> None:
        self.previous("ci.yml", "2026-01-31")
        self.assertEqual(0, self.check("callers", "--today", "2026-02-28").status)
        self.assertEqual(1, self.check("callers", "--today", "2026-03-01").status)

    def test_a_consumer_on_the_previous_version_still_plans_every_part_it_switched_on(self) -> None:
        base = self.commit({CONFIGURATION: "version: 1\nparts:\n  fixture-build: {}\n  fixture-test:\n    paths: [src/]\n", "src/code.txt": "one\n"})
        head = self.commit({"src/code.txt": "two\n"})
        self.assertEqual(0, self.check("callers", "--today", "2026-03-25").status)
        planned = self.plan("--base", base, "--head", head)
        self.assertEqual(0, planned.status, planned.stdout)
        self.assertEqual({"fixture-build": True, "fixture-floor": True, "fixture-test": True}, {name: part["run"] for name, part in planned.plan["parts"].items()})

    def test_a_record_without_a_previous_file_or_with_a_date_that_is_none_cannot_be_used(self) -> None:
        self.record({"release.yml": "2026-03-10"})
        checked = self.check("callers")
        self.assertEqual(2, checked.status)
        self.assertIn("previous", checked.stderr)
        self.previous("ci.yml", "the tenth of March")
        self.assertEqual(2, self.check("callers").status)


class LocalWorkflowTests(ConsumerFixture):
    def test_a_workflow_that_is_no_fixed_caller_and_is_not_registered_fails_with_its_name_and_the_decision_rule(self) -> None:
        self.consumer({LOCAL: "name: Nightly report\n", f"{WORKFLOWS}/other.yaml": "name: Other\n"})
        checked = self.start("callers")
        self.assertEqual(1, checked.status, checked.stdout + checked.stderr)
        for path in (LOCAL, f"{WORKFLOWS}/other.yaml"):
            self.assertIn(f"::error file={path}::", checked.stdout)
            self.assertIn(f"`{path}` is neither a fixed caller nor registered under `local-workflows` in `{CONFIGURATION}`", checked.summary)
        self.assertIn(f"The decision rule in the catalogue says where its work belongs: {RULE}", checked.summary)
        self.assertEqual([], self.api.requests, "the callers equal their templates, so no sync pull request is looked up")

    def test_a_registered_workflow_passes(self) -> None:
        self.consumer({LOCAL: "name: Nightly report\n", CONFIGURATION: REGISTERED})
        checked = self.check("callers")
        self.assertEqual(0, checked.status, checked.stdout)
        self.assertIn(f"`{LOCAL}` is registered", checked.summary)

    def test_a_file_that_is_no_workflow_is_left_alone(self) -> None:
        self.consumer({f"{WORKFLOWS}/README.md": "Notes.\n", f"{WORKFLOWS}/data/values.yml": "a: 1\n"})
        self.assertEqual(0, self.check("callers").status)

    def test_an_unregistered_workflow_on_a_dependabot_pull_request_or_a_push_gets_a_notice(self) -> None:
        self.consumer({LOCAL: "name: Nightly report\n"})
        for event, author, reason in (("pull_request", "dependabot[bot]", "This pull request is Dependabot's"), ("push", "", "This run is no pull request")):
            with self.subTest(event=event, author=author):
                checked = self.check("callers", event=event, author=author)
                self.assertEqual(0, checked.status, checked.stdout)
                self.assertIn(f"**Notice.** {reason}, so these findings do not fail it", checked.summary)
                self.assertIn(f"::notice file={LOCAL}::", checked.stdout)

    def test_a_configuration_file_that_cannot_be_used_fails_and_names_no_workflow_as_unregistered(self) -> None:
        self.consumer({LOCAL: "name: Nightly report\n", CONFIGURATION: "version: 1\nlocal-workflows: none\n"})
        checked = self.check("callers")
        self.assertEqual(1, checked.status)
        self.assertIn(f"`{CONFIGURATION}` cannot be used, so the local workflow register is unknown", checked.summary)
        self.assertIn("local-workflows: expected a list", checked.summary)
        self.assertNotIn("neither a fixed caller", checked.summary)


class SharedRepositoryTests(ConsumerFixture):
    def test_the_repository_of_the_shared_parts_holds_no_fixed_callers_and_passes(self) -> None:
        self.consumer({f"{WORKFLOWS}/ci.yml": "name: a local-path caller\n", LOCAL: "name: a shared workflow\n"})
        checked = self.check("callers", repository=SHARED)
        self.assertEqual(0, checked.status)
        self.assertIn("This repository holds the shared parts", checked.summary)
        self.assertIn("nothing to check", checked.stdout)
        self.assertEqual([], self.api.requests)


if __name__ == "__main__":
    unittest.main()
