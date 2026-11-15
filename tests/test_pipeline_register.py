"""The register check of the pipeline's hygiene job: each entry of the local workflow register names an issue of the
consumer, a permanent entry a closed one, read from a stand-in for the GitHub API, and a temporary entry whose part
exists by now tells the consumer to switch."""

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


class SwitchOverTests(ConsumerFixture):
    """A temporary entry waits for a part; once the schema of the pipeline's own revision holds that part, the entry is
    a finding. The fixture schema holds the parts ``fixture-build``, ``fixture-test`` and ``fixture-floor``."""

    WAITING = f"version: 1\nlocal-workflows:\n  - workflow: {TEMPORARY}\n    request: 12\n    waits-for: fixture-build\n"
    INSTRUCTION = (f"The entry of `{TEMPORARY}` waits for the part `fixture-build`, which exists now: switch the part on under `parts` in "
                   f"`{CONFIGURATION}`, remove the workflow and its entry, and close the request #12.")

    def setUp(self) -> None:
        super().setUp()
        self.consumer({CONFIGURATION: self.WAITING})
        self.api.issue(12, "open")

    def test_a_pull_request_fails_with_the_instruction_to_switch_remove_the_workflow_and_close_the_request(self) -> None:
        checked = self.start("register")
        self.assertEqual(1, checked.status, checked.stdout + checked.stderr)
        self.assertIn(self.INSTRUCTION, checked.summary)
        self.assertIn(f"::error file={CONFIGURATION}::", checked.stdout)
        self.assertIn(f"The catalogue describes the part: https://github.com/{SHARED}/blob/v1/catalogue.json", checked.summary)

    def test_a_dependabot_pull_request_gets_a_notice_and_passes(self) -> None:
        checked = self.check("register", author="dependabot[bot]")
        self.assertEqual(0, checked.status, checked.stdout)
        self.assertIn("**Notice.** This pull request is Dependabot's, so these findings do not fail it", checked.summary)
        self.assertIn(self.INSTRUCTION, checked.summary)
        self.assertIn(f"::notice file={CONFIGURATION}::", checked.stdout)
        self.assertNotIn("::error", checked.stdout)

    def test_a_push_gets_a_notice_and_passes(self) -> None:
        checked = self.check("register", event="push", author="")
        self.assertEqual(0, checked.status, checked.stdout)
        self.assertIn(self.INSTRUCTION, checked.summary)
        self.assertNotIn("::error", checked.stdout)

    def test_a_part_that_always_runs_exists_too(self) -> None:
        self.consumer({CONFIGURATION: self.WAITING.replace("fixture-build", "fixture-floor")})
        checked = self.check("register")
        self.assertEqual(1, checked.status)
        self.assertIn("waits for the part `fixture-floor`, which exists now", checked.summary)

    def test_an_entry_whose_part_does_not_exist_yet_passes(self) -> None:
        self.consumer({CONFIGURATION: self.WAITING.replace("fixture-build", "fixture-release")})
        checked = self.check("register")
        self.assertEqual(0, checked.status, checked.stdout)
        self.assertNotIn("exists now", checked.summary)

    def test_a_permanent_entry_waits_for_no_part(self) -> None:
        self.consumer({CONFIGURATION: f"version: 1\nlocal-workflows:\n  - workflow: {PERMANENT}\n    declined-request: 15\n"})
        self.api.issue(15, "closed")
        self.assertEqual(0, self.check("register").status)

    def test_an_entry_that_waits_for_a_setting_fails_once_the_part_knows_that_setting(self) -> None:
        self.consumer({CONFIGURATION: self.WAITING.replace("fixture-build", "fixture-test.level")})
        checked = self.start("register")
        self.assertEqual(1, checked.status, checked.stdout + checked.stderr)
        self.assertIn(f"The entry of `{TEMPORARY}` waits for the setting `level` of the part `fixture-test`, which exists now: state it under "
                      f"`parts.fixture-test` in `{CONFIGURATION}`, remove the workflow and its entry, and close the request #12.", checked.summary)
        self.assertIn(f"The catalogue describes the part: https://github.com/{SHARED}/blob/v1/catalogue.json", checked.summary)

    def test_an_entry_that_waits_for_a_setting_passes_while_the_part_lacks_it(self) -> None:
        for waits_for in ("fixture-test.depth", "fixture-build.level", "fixture-floor.level", "fixture-release.level"):
            with self.subTest(waits_for=waits_for):
                self.consumer({CONFIGURATION: self.WAITING.replace("fixture-build", waits_for)})
                checked = self.check("register")
                self.assertEqual(0, checked.status, checked.stdout)
                self.assertNotIn("exists now", checked.summary)

    def test_a_setting_of_an_item_of_a_list_counts_as_a_setting_of_the_part(self) -> None:
        nested = {"fixture-matrix": {"description": "An invented part with a list of projects.",
                                     "configuration": {"type": "object", "additionalProperties": False, "properties": {
                                         "projects": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                                                 "properties": {"runner-label": {"type": "string"}}}}}}}}
        self.schemas = self.schema_folder("nested", versions=(1,), parts=nested)
        self.consumer({CONFIGURATION: self.WAITING.replace("fixture-build", "fixture-matrix.runner-label")})
        checked = self.check("register")
        self.assertEqual(1, checked.status, checked.stdout)
        self.assertIn("waits for the setting `runner-label` of the part `fixture-matrix`, which exists now", checked.summary)

    def test_a_dependabot_pull_request_gets_a_notice_for_a_setting_too(self) -> None:
        self.consumer({CONFIGURATION: self.WAITING.replace("fixture-build", "fixture-test.level")})
        checked = self.check("register", author="dependabot[bot]")
        self.assertEqual(0, checked.status, checked.stdout)
        self.assertIn("waits for the setting `level`", checked.summary)
        self.assertNotIn("::error", checked.stdout)

    def test_waits_for_names_a_part_or_a_part_and_one_setting_and_nothing_else(self) -> None:
        for waits_for in ("fixture-test.", ".level", "fixture-test.level.more", "fixture-test.Level", "fixture test"):
            with self.subTest(waits_for=waits_for):
                self.consumer({CONFIGURATION: self.WAITING.replace("fixture-build", f'"{waits_for}"')})
                checked = self.check("register")
                self.assertEqual(1, checked.status)
                self.assertIn(f"`{CONFIGURATION}` cannot be used", checked.summary)

    def test_the_entry_is_named_once_when_its_request_is_no_issue_either(self) -> None:
        del self.api.issues[12]
        checked = self.check("register")
        self.assertEqual(1, checked.status)
        self.assertIn(self.INSTRUCTION, checked.summary)
        self.assertIn("names the request #12, which is no issue of this repository", checked.summary)


if __name__ == "__main__":
    unittest.main()
