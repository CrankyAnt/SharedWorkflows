"""The register check of the pipeline's hygiene job: each entry of the local workflow register names an issue of the
consumer, a permanent entry a closed one, read from a stand-in for the GitHub API."""

from __future__ import annotations

import unittest

from tests.consumer_checks_fixtures import CONSUMER, SHARED, WORKFLOWS, ConsumerFixture
from tests.pipeline_fixtures import CONFIGURATION

TEMPORARY = f"{WORKFLOWS}/nightly-report.yml"
PERMANENT = f"{WORKFLOWS}/weekly-monitor.yml"
REGISTER = (f"version: 1\nlocal-workflows:\n  - workflow: {TEMPORARY}\n    request: 12\n    waits-for: python-release\n"
            f"  - workflow: {PERMANENT}\n    declined-request: 15\n")


class RegisterCheckTests(ConsumerFixture):
    def setUp(self) -> None:
        super().setUp()
        self.consumer({CONFIGURATION: REGISTER})

    def test_entries_that_name_an_issue_and_a_closed_issue_pass(self) -> None:
        self.api.issue(12, "open")
        self.api.issue(15, "closed")
        checked = self.start("register")
        self.assertEqual(0, checked.status, checked.stdout + checked.stderr)
        self.assertIn("Every entry of the local workflow register names its issue", checked.summary)
        self.assertEqual([f"/repos/{CONSUMER}/issues/12", f"/repos/{CONSUMER}/issues/15"], [path for path, _ in self.api.requests])
        self.assertEqual({"Bearer token-for-the-test"}, {headers["authorization"] for _, headers in self.api.requests})

    def test_a_temporary_entry_whose_issue_does_not_exist_fails(self) -> None:
        self.api.issue(15, "closed")
        checked = self.start("register")
        self.assertEqual(1, checked.status, checked.stdout + checked.stderr)
        self.assertIn(f"::error file={CONFIGURATION}::", checked.stdout)
        self.assertIn(f"The entry of `{TEMPORARY}` names the request #12, which is no issue of this repository", checked.summary)
        self.assertNotIn(PERMANENT, checked.summary)

    def test_a_temporary_entry_that_names_a_closed_issue_passes(self) -> None:
        self.api.issue(12, "closed")
        self.api.issue(15, "closed")
        self.assertEqual(0, self.check("register").status)

    def test_an_entry_that_names_a_pull_request_fails(self) -> None:
        self.api.issue(12, "open", pull_request=True)
        self.api.issue(15, "closed")
        checked = self.check("register")
        self.assertEqual(1, checked.status)
        self.assertIn(f"The entry of `{TEMPORARY}` names the request #12, which is a pull request and no issue", checked.summary)

    def test_a_permanent_entry_whose_issue_is_open_or_missing_fails(self) -> None:
        self.api.issue(12, "open")
        self.api.issue(15, "open")
        checked = self.check("register")
        self.assertEqual(1, checked.status)
        self.assertIn(f"The entry of `{PERMANENT}` names the declined request #15, which is still open; a permanent entry names a closed request", checked.summary)
        del self.api.issues[15]
        checked = self.check("register")
        self.assertEqual(1, checked.status)
        self.assertIn(f"The entry of `{PERMANENT}` names the declined request #15, which is no issue of this repository", checked.summary)

    def test_a_temporary_entry_that_names_no_part_fails(self) -> None:
        self.api.issue(12, "open")
        self.consumer({CONFIGURATION: f"version: 1\nlocal-workflows:\n  - workflow: {TEMPORARY}\n    request: 12\n"})
        checked = self.check("register")
        self.assertEqual(1, checked.status)
        self.assertIn(f"`{CONFIGURATION}` cannot be used", checked.summary)
        self.assertIn("local-workflows[0]: is neither a temporary entry (workflow, request, waits-for) nor a permanent entry (workflow, declined-request)", checked.summary)
        self.assertEqual([], self.api.requests)

    def test_an_issue_that_cannot_be_read_fails(self) -> None:
        self.api.status = 403
        checked = self.check("register")
        self.assertEqual(1, checked.status)
        self.assertIn(f"The entry of `{TEMPORARY}` names the request #12, which could not be read: HTTP 403", checked.summary)

    def test_a_dependabot_pull_request_and_a_push_get_a_notice(self) -> None:
        for event, author in (("pull_request", "dependabot[bot]"), ("push", "")):
            with self.subTest(event=event):
                checked = self.check("register", event=event, author=author)
                self.assertEqual(0, checked.status, checked.stdout)
                self.assertIn("**Notice.**", checked.summary)
                self.assertIn("which is no issue of this repository", checked.summary)
                self.assertIn(f"::notice file={CONFIGURATION}::", checked.stdout)
                self.assertNotIn("::error", checked.stdout)

    def test_an_empty_register_asks_github_nothing(self) -> None:
        self.consumer({CONFIGURATION: "version: 1\n"})
        checked = self.check("register")
        self.assertEqual(0, checked.status)
        self.assertIn("The local workflow register is empty", checked.summary)
        self.assertEqual([], self.api.requests)

    def test_the_repository_of_the_shared_parts_passes_without_a_check(self) -> None:
        checked = self.check("register", repository=SHARED)
        self.assertEqual(0, checked.status)
        self.assertIn("This repository holds the shared parts", checked.summary)
        self.assertEqual([], self.api.requests)


if __name__ == "__main__":
    unittest.main()
