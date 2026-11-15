"""The register check of the pipeline's hygiene job: each entry of the local workflow register names an issue of the
consumer, a permanent entry a closed one, read from a stand-in for the GitHub API, and a temporary entry whose part
exists by now tells the consumer to switch."""

from __future__ import annotations

import unittest

from tests.consumer_checks_fixtures import CONSUMER, SHARED, WORKFLOWS, ConsumerFixture
from tests.pipeline_fixtures import CONFIGURATION, FIXTURE_PARTS, SCHEMAS

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

    def test_a_temporary_entry_whose_request_was_closed_as_completed_passes(self) -> None:
        self.api.issue(12, "closed", reason="completed")
        self.api.issue(15, "closed", reason="not_planned")
        self.assertEqual(0, self.check("register").status)

    def test_a_temporary_entry_whose_request_was_declined_fails_a_pull_request_with_the_instruction_to_make_it_permanent(self) -> None:
        self.api.issue(12, "closed", reason="not_planned")
        self.api.issue(15, "closed", reason="not_planned")
        checked = self.start("register")
        self.assertEqual(1, checked.status, checked.stdout + checked.stderr)
        self.assertIn(f"::error file={CONFIGURATION}::", checked.stdout)
        self.assertIn(f"The entry of `{TEMPORARY}` names the request #12, which was closed as not planned: the request was declined, so make the entry permanent, "
                      "with `declined-request: 12` in place of `request` and `waits-for`.", checked.summary)
        self.assertNotIn(PERMANENT, checked.summary, "a permanent entry names a declined request")

    def test_a_declined_request_of_a_temporary_entry_is_a_notice_on_a_dependabot_pull_request_and_on_a_push(self) -> None:
        self.api.issue(12, "closed", reason="not_planned")
        self.api.issue(15, "closed")
        for event, author in (("pull_request", "dependabot[bot]"), ("push", "")):
            with self.subTest(event=event):
                checked = self.check("register", event=event, author=author)
                self.assertEqual(0, checked.status, checked.stdout)
                self.assertIn("**Notice.**", checked.summary)
                self.assertIn("which was closed as not planned: the request was declined, so make the entry permanent", checked.summary)
                self.assertIn(f"::notice file={CONFIGURATION}::", checked.stdout)
                self.assertNotIn("::error", checked.stdout)

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

    def test_an_entry_or_a_free_job_that_waits_for_an_exclusion_of_repository_hygiene_fails_in_the_real_schema(self) -> None:
        self.schemas = SCHEMAS
        for setting in ("coverage-exclude", "link-exclude"):
            with self.subTest(entry=setting):
                self.consumer({CONFIGURATION: self.WAITING.replace("fixture-build", f"repository-hygiene.{setting}")})
                checked = self.check("register")
                self.assertEqual(1, checked.status, checked.stdout)
                self.assertIn(f"The entry of `{TEMPORARY}` waits for the setting `{setting}` of the part `repository-hygiene`, which exists now: state it under "
                              f"`parts.repository-hygiene` in `{CONFIGURATION}`, remove the workflow and its entry, and close the request #12.", checked.summary)
            with self.subTest(free_job=setting):
                self.consumer({CONFIGURATION: f"version: 1\nparts:\n  custom:\n    script: ci/check.sh\n    request: 12\n    waits-for: repository-hygiene.{setting}\n"})
                checked = self.check("register")
                self.assertEqual(1, checked.status, checked.stdout)
                self.assertIn(f"the setting `{setting}` of the part `repository-hygiene`, which exists now", checked.summary)
        self.consumer({CONFIGURATION: self.WAITING.replace("fixture-build", "repository-hygiene.zizmor-persona")})
        self.assertEqual(0, self.check("register").status, "a setting that the part lacks keeps the entry")

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


class FreeJobRequestTests(ConsumerFixture):
    """The free job may name the request for the part that takes over its work, and what it waits for, as a temporary
    entry does. The fixture schema holds the invented parts and a free job with the two settings as the real fragment
    states them, by the definitions of the base; ``tests/test_pipeline_custom.py`` holds the real fragment."""

    FREE_JOB = {"description": "The free job with the settings of a request.",
                "configuration": {"type": "object", "additionalProperties": False, "required": ["script"],
                                  "properties": {"script": {"$ref": "#/$defs/script"}, "request": {"$ref": "#/$defs/request"},
                                                 "waits-for": {"$ref": "#/$defs/waits-for"}}}}

    def setUp(self) -> None:
        super().setUp()
        self.schemas = self.schema_folder("with-the-free-job", versions=(1,), parts={**FIXTURE_PARTS, "custom": self.FREE_JOB})
        self.api.issue(12, "open")

    def free_job(self, settings: str, register: str = "") -> None:
        self.consumer({CONFIGURATION: f"version: 1\nparts:\n  custom:\n    script: ci/check.sh\n{settings}{register}"})

    def test_a_free_job_without_a_request_and_an_empty_register_ask_github_nothing(self) -> None:
        self.free_job("")
        checked = self.check("register")
        self.assertEqual(0, checked.status, checked.stdout)
        self.assertIn("The local workflow register is empty.", checked.summary)
        self.assertEqual([], self.api.requests)

    def test_a_free_job_that_waits_for_a_part_that_does_not_exist_yet_passes_once_its_request_is_an_issue(self) -> None:
        self.free_job("    request: 12\n    waits-for: fixture-release\n")
        checked = self.start("register")
        self.assertEqual(0, checked.status, checked.stdout + checked.stderr)
        self.assertIn("The local workflow register is empty. The free job names its request, and what it waits for does not exist yet.", checked.summary)
        self.assertEqual([f"/repos/{CONSUMER}/issues/12"], [path for path, _ in self.api.requests])

    def test_a_free_job_whose_part_exists_fails_a_pull_request_with_the_instruction_to_switch(self) -> None:
        self.free_job("    request: 12\n    waits-for: fixture-build\n")
        checked = self.start("register")
        self.assertEqual(1, checked.status, checked.stdout + checked.stderr)
        self.assertIn(f"The free job, `parts.custom`, waits for the part `fixture-build`, which exists now: switch the part on under `parts` in `{CONFIGURATION}`, "
                      "remove the work from the free job with `request` and `waits-for` of `parts.custom`, and close the request #12.", checked.summary)
        self.assertIn(f"::error file={CONFIGURATION}::", checked.stdout)
        self.assertIn(f"The catalogue describes the part: https://github.com/{SHARED}/blob/v1/catalogue.json", checked.summary)

    def test_a_free_job_that_waits_for_a_setting_fails_once_the_part_knows_it_and_passes_until_then(self) -> None:
        self.free_job("    request: 12\n    waits-for: fixture-test.level\n")
        checked = self.check("register")
        self.assertEqual(1, checked.status, checked.stdout)
        self.assertIn("The free job, `parts.custom`, waits for the setting `level` of the part `fixture-test`, which exists now: state it under `parts.fixture-test`", checked.summary)
        self.free_job("    request: 12\n    waits-for: fixture-test.depth\n")
        self.assertEqual(0, self.check("register").status)

    def test_the_switch_over_of_the_free_job_is_a_notice_on_a_dependabot_pull_request_and_on_a_push(self) -> None:
        self.free_job("    request: 12\n    waits-for: fixture-build\n")
        for event, author in (("pull_request", "dependabot[bot]"), ("push", "")):
            with self.subTest(event=event):
                checked = self.check("register", event=event, author=author)
                self.assertEqual(0, checked.status, checked.stdout)
                self.assertIn("**Notice.**", checked.summary)
                self.assertIn("The free job, `parts.custom`, waits for the part `fixture-build`, which exists now", checked.summary)
                self.assertNotIn("::error", checked.stdout)

    def test_the_request_of_the_free_job_is_an_issue_of_the_repository(self) -> None:
        self.free_job("    request: 13\n    waits-for: fixture-release\n")
        checked = self.check("register")
        self.assertEqual(1, checked.status)
        self.assertIn("The free job, `parts.custom`, names the request #13, which is no issue of this repository.", checked.summary)
        self.api.issue(13, "open", pull_request=True)
        self.assertIn("names the request #13, which is a pull request and no issue.", self.check("register").summary)

    def test_a_declined_request_leaves_the_free_job_without_the_two_settings(self) -> None:
        self.api.issue(12, "closed", reason="not_planned")
        self.free_job("    request: 12\n    waits-for: fixture-release\n")
        checked = self.check("register")
        self.assertEqual(1, checked.status)
        self.assertIn("The free job, `parts.custom`, names the request #12, which was closed as not planned: the request was declined, so remove `request` and "
                      "`waits-for` from `parts.custom`; the work stays in the free job.", checked.summary)

    def test_one_of_the_two_settings_without_the_other_is_a_finding(self) -> None:
        for settings, stated, missing in (("    request: 12\n", "request", "waits-for"), ("    waits-for: fixture-build\n", "waits-for", "request")):
            with self.subTest(stated=stated):
                self.free_job(settings)
                checked = self.check("register")
                self.assertEqual(1, checked.status, checked.stdout)
                self.assertIn(f"The free job, `parts.custom`, states `{stated}` without `{missing}`: a request for shared CI names both, or neither.", checked.summary)
                self.assertEqual([], self.api.requests)
                self.assertNotIn("exists now", checked.summary)

    def test_the_free_job_and_the_entries_of_the_register_are_checked_together(self) -> None:
        self.api.issue(14, "open")
        self.free_job("    request: 12\n    waits-for: fixture-release\n", f"local-workflows:\n  - workflow: {TEMPORARY}\n    request: 14\n    waits-for: fixture-build\n")
        checked = self.check("register")
        self.assertEqual(1, checked.status)
        self.assertIn(f"The entry of `{TEMPORARY}` waits for the part `fixture-build`, which exists now", checked.summary)
        self.assertNotIn("The free job", checked.summary)
        self.free_job("    request: 12\n    waits-for: fixture-release\n", f"local-workflows:\n  - workflow: {TEMPORARY}\n    request: 14\n    waits-for: fixture-release\n")
        checked = self.check("register")
        self.assertEqual(0, checked.status)
        self.assertIn("Every entry of the local workflow register names its issue, and no part that an entry waits for exists yet. "
                      "The free job names its request, and what it waits for does not exist yet.", checked.summary)
        self.assertEqual([f"/repos/{CONSUMER}/issues/14", f"/repos/{CONSUMER}/issues/12"], [path for path, _ in self.api.requests][-2:], "the entries, then the free job")

    def test_the_two_settings_take_the_form_of_a_temporary_entry(self) -> None:
        for settings, reason in (("    request: 0\n    waits-for: fixture-build\n", "parts.custom.request"), ("    request: 12\n    waits-for: Fixture Build\n", "parts.custom.waits-for")):
            with self.subTest(reason=reason):
                self.free_job(settings)
                checked = self.check("register")
                self.assertEqual(1, checked.status)
                self.assertIn(f"`{CONFIGURATION}` cannot be used", checked.summary)
                self.assertIn(reason, checked.summary)


if __name__ == "__main__":
    unittest.main()
