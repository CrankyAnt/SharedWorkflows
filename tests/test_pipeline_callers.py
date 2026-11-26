"""The caller check of the pipeline's hygiene job: a consumer's fixed callers equal their templates, or for one month
the previous version, its CI guide equals its template, and every other workflow file is registered in the
configuration file."""

from __future__ import annotations

import calendar
import json
import unittest
from datetime import date, timedelta

from tests.consumer_checks_fixtures import CONSUMER, SHARED, SYNC_BRANCH, WORKFLOWS, ConsumerFixture, template
from tests.pipeline_fixtures import CONFIGURATION, PART_FOLDER, write

RULE = f"https://github.com/{SHARED}/blob/v1/catalogue.json"
LOCAL = f"{WORKFLOWS}/nightly-report.yml"
CI_GUIDE = "docs/agents/ci.md"
CI_GUIDE_TEXT = "# CI\n\nAn invented pointer to the catalogue.\n"
WITH_CHOICES = CI_GUIDE_TEXT + "\n## Repository choices\n"
REAL_KIT = PART_FOLDER / "consumer-kit"
CALLABLE = "name: Nightly report\non:\n  schedule:\n    - cron: '0 3 * * *'\njobs:\n  report:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n"
REGISTERED = f"version: 1\nlocal-workflows:\n  - workflow: {LOCAL}\n    request: 12\n    waits-for: python-release\n"


class FixedCallerTests(ConsumerFixture):
    def test_a_consumer_whose_callers_equal_the_templates_passes(self) -> None:
        checked = self.start("callers")
        self.assertEqual(0, checked.status, checked.stdout + checked.stderr)
        self.assertIn("Every fixed caller equals its template", checked.summary)
        self.assertEqual([], self.api.requests, "a consumer that is up to date asks GitHub nothing")

    def test_a_fixed_caller_at_another_tag_fails_as_a_caller_that_differs_from_its_template(self) -> None:
        self.consumer({f"{WORKFLOWS}/ci.yml": template("ci.yml").replace("@v1\n", "@v2\n")})
        checked = self.check("callers")
        self.assertEqual(1, checked.status, checked.stdout + checked.stderr)
        self.assertIn(f"`{WORKFLOWS}/ci.yml` differs from its template; a fixed caller equals its template byte for byte.", checked.summary)
        self.assertNotIn("candidate", checked.summary)

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
        self.consumer({LOCAL: CALLABLE, CONFIGURATION: REGISTERED})
        checked = self.check("callers")
        self.assertEqual(0, checked.status, checked.stdout)
        self.assertIn(f"`{LOCAL}` is registered", checked.summary)

    def test_a_deleted_workflow_that_is_still_registered_fails(self) -> None:
        self.consumer({CONFIGURATION: REGISTERED})
        checked = self.check("callers")
        self.assertEqual(1, checked.status, checked.stdout)
        self.assertIn(f"::error file={CONFIGURATION}::", checked.stdout)
        self.assertIn(f"`{LOCAL}` is registered under `local-workflows` in `{CONFIGURATION}`, but the workflow file does not exist.", checked.summary)

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
        self.consumer({LOCAL: CALLABLE, CONFIGURATION: "version: 1\nlocal-workflows: none\n"})
        checked = self.check("callers")
        self.assertEqual(1, checked.status)
        self.assertIn(f"`{CONFIGURATION}` cannot be used, so the local workflow register is unknown", checked.summary)
        self.assertIn("local-workflows: expected a list", checked.summary)
        self.assertNotIn("neither a fixed caller", checked.summary)

    def test_a_registered_non_fixed_workflow_calling_a_shared_workflow_fails(self) -> None:
        called = f"{SHARED}/.github/workflows/legacy-tests.yml@v1"
        workflow = f"name: Legacy\non:\n  workflow_dispatch:\njobs:\n  run:\n    uses: {called}\n"
        self.consumer({LOCAL: workflow, CONFIGURATION: REGISTERED})
        checked = self.check("callers")
        self.assertEqual(1, checked.status, checked.stdout + checked.stderr)
        self.assertIn(f"::error file={LOCAL}::", checked.stdout)
        self.assertIn(f"`{LOCAL}`", checked.summary)
        self.assertIn(f"`{called}`", checked.summary)

    def test_an_uncallable_registered_workflow_fails(self) -> None:
        cases = (
            ("no on section", "name: Orphan\njobs:\n  run:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n"),
            ("empty on mapping", "name: Empty\non: {}\njobs:\n  run:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n"),
            ("unreferenced workflow_call", "name: Helper\non:\n  workflow_call:\njobs:\n  run:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n"),
            ("self-calling workflow_call", f"name: Self\non:\n  workflow_call:\njobs:\n  run:\n    uses: ./{LOCAL}\n"),
        )
        for label, content in cases:
            with self.subTest(case=label):
                self.consumer({LOCAL: content, CONFIGURATION: REGISTERED})
                checked = self.check("callers")
                self.assertEqual(1, checked.status, checked.stdout + checked.stderr)
                self.assertIn(f"::error file={LOCAL}::", checked.stdout)
                self.assertIn(f"`{LOCAL}`", checked.summary)

    def test_callable_registered_workflows_pass(self) -> None:
        helper = f"{WORKFLOWS}/helper.yml"
        register = (f"version: 1\nlocal-workflows:\n"
                    f"  - workflow: {LOCAL}\n    request: 12\n    waits-for: python-release\n"
                    f"  - workflow: {helper}\n    request: 13\n    waits-for: python-release\n")
        helper_wf = "name: Helper\non:\n  workflow_call:\njobs:\n  work:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n"
        for prefix in ("./", "$/"):
            with self.subTest(prefix=prefix):
                caller_wf = f"name: Dispatch\non:\n  workflow_dispatch:\n  workflow_call:\njobs:\n  call:\n    uses: {prefix}{helper}\n"
                self.consumer({LOCAL: caller_wf, helper: helper_wf, CONFIGURATION: register})
                checked = self.check("callers")
                self.assertEqual(0, checked.status, checked.stdout + checked.stderr)
                self.assertNotIn("workflow_call", checked.summary)

    def test_an_unreadable_workflow_file_fails(self) -> None:
        self.consumer({LOCAL: "on: [unclosed\n", CONFIGURATION: REGISTERED})
        checked = self.check("callers")
        self.assertEqual(1, checked.status, checked.stdout + checked.stderr)
        self.assertIn(f"::error file={LOCAL}::", checked.stdout)
        self.assertIn(f"`{LOCAL}`", checked.summary)
        self.assertIn("could not be read", checked.summary)

    def test_workflow_inspection_findings_are_notices_on_dependabot_or_push_and_skipped_in_shared_repo(self) -> None:
        called = f"{SHARED}/.github/workflows/legacy.yml@v1"
        self.consumer({LOCAL: f"name: Legacy\non:\n  workflow_call:\njobs:\n  run:\n    uses: {called}\n", CONFIGURATION: REGISTERED})
        for event, author in (("pull_request", "dependabot[bot]"), ("push", "")):
            with self.subTest(event=event, author=author):
                checked = self.check("callers", event=event, author=author)
                self.assertEqual(0, checked.status, checked.stdout)
                self.assertIn(f"::notice file={LOCAL}::", checked.stdout)
                self.assertNotIn("::error", checked.stdout)
        skipped = self.check("callers", repository=SHARED)
        self.assertEqual(0, skipped.status, skipped.stdout)
        self.assertIn("nothing to check", skipped.stdout)


class CiGuideTests(ConsumerFixture):
    """The kit holds the template of the CI guide, which a consumer holds byte for byte as ``docs/agents/ci.md``."""

    def setUp(self) -> None:
        super().setUp()
        write(self.kit, CI_GUIDE, CI_GUIDE_TEXT)
        self.consumer({CI_GUIDE: CI_GUIDE_TEXT})

    def test_a_consumer_whose_ci_guide_equals_the_template_passes(self) -> None:
        checked = self.start("callers")
        self.assertEqual(0, checked.status, checked.stdout + checked.stderr)
        self.assertIn("Every fixed caller and the CI guide equal their templates", checked.summary)
        self.assertEqual([], self.api.requests)

    def test_a_ci_guide_with_a_rule_of_the_consumer_s_own_fails_with_the_open_sync_pull_request(self) -> None:
        url = self.api.sync_pull_request(31)
        self.consumer({CI_GUIDE: CI_GUIDE_TEXT + "\nRun the slow tests only at night.\n"})
        checked = self.start("callers")
        self.assertEqual(1, checked.status, checked.stdout + checked.stderr)
        self.assertIn(f"::error file={CI_GUIDE}::", checked.stdout)
        self.assertIn(f"`{CI_GUIDE}` differs from its template; the CI guide equals its template byte for byte.", checked.summary)
        self.assertIn(f"The open sync pull request brings the fixed callers up to date: {url}", checked.summary)

    def test_a_missing_ci_guide_fails(self) -> None:
        self.consumer({CI_GUIDE: None})
        checked = self.check("callers")
        self.assertEqual(1, checked.status)
        self.assertIn(f"`{CI_GUIDE}` does not exist; every consumer holds this CI guide.", checked.summary)

    def test_a_dependabot_pull_request_and_a_push_get_a_notice(self) -> None:
        self.consumer({CI_GUIDE: "# CI\n"})
        for event, author in (("pull_request", "dependabot[bot]"), ("push", "")):
            with self.subTest(event=event):
                checked = self.check("callers", event=event, author=author)
                self.assertEqual(0, checked.status, checked.stdout)
                self.assertIn(f"::notice file={CI_GUIDE}::", checked.stdout)
                self.assertNotIn("::error", checked.stdout)

    def test_a_kit_without_the_template_compares_no_ci_guide(self) -> None:
        (self.kit / CI_GUIDE).unlink()
        self.consumer({CI_GUIDE: "# A guide of the consumer's own\n"})
        checked = self.check("callers")
        self.assertEqual(0, checked.status, checked.stdout)
        self.assertIn("Every fixed caller equals its template", checked.summary)


class RepositoryChoicesTests(ConsumerFixture):
    """A template that ends with the heading ``## Repository choices`` is compared up to and including that heading;
    a consumer records its own choices below it."""

    def setUp(self) -> None:
        super().setUp()
        write(self.kit, CI_GUIDE, WITH_CHOICES)
        self.consumer({CI_GUIDE: WITH_CHOICES + "\nThe ruleset keeps the merge queue, because releases are batched.\n"})

    def test_choices_below_the_heading_pass(self) -> None:
        checked = self.check("callers")
        self.assertEqual(0, checked.status, checked.stdout)
        self.assertIn("Every fixed caller and the CI guide equal their templates", checked.summary)

    def test_a_document_that_ends_with_the_heading_passes(self) -> None:
        self.consumer({CI_GUIDE: WITH_CHOICES})
        self.assertEqual(0, self.check("callers").status)

    def test_a_change_above_the_heading_fails(self) -> None:
        self.consumer({CI_GUIDE: WITH_CHOICES.replace("invented", "changed") + "\nA choice.\n"})
        checked = self.check("callers")
        self.assertEqual(1, checked.status)
        self.assertIn(f"`{CI_GUIDE}` differs from its template up to and including the heading `## Repository choices`", checked.summary)

    def test_a_document_without_the_heading_fails(self) -> None:
        self.consumer({CI_GUIDE: CI_GUIDE_TEXT})
        checked = self.check("callers")
        self.assertEqual(1, checked.status)
        self.assertIn(f"`{CI_GUIDE}` differs from its template", checked.summary)

    def test_a_second_heading_of_that_name_belongs_to_the_consumer_s_part(self) -> None:
        self.consumer({CI_GUIDE: WITH_CHOICES + "\n## Repository choices\n\nA repeated heading.\n"})
        self.assertEqual(0, self.check("callers").status)


class PreviousCiGuideTests(ConsumerFixture):
    """A promotion on 2026-03-10 changed the template of the CI guide; the consumer still holds the version
    before it, with its own choices below the heading."""

    PREVIOUS = "# CI\n\nThe guide before the change.\n\n## Repository choices\n"

    def setUp(self) -> None:
        super().setUp()
        write(self.kit, CI_GUIDE, WITH_CHOICES)
        write(self.kit, f"previous/{CI_GUIDE}", self.PREVIOUS)
        self.record({CI_GUIDE: "2026-03-10"})
        self.consumer({CI_GUIDE: self.PREVIOUS + "\nA choice of the repository.\n"})

    def test_the_previous_version_passes_for_one_month_with_a_notice(self) -> None:
        for today in ("2026-03-10", "2026-04-10"):
            with self.subTest(today=today):
                checked = self.check("callers", "--today", today)
                self.assertEqual(0, checked.status, checked.stdout + checked.stderr)
                self.assertIn(f"`{CI_GUIDE}` equals the previous version of its template, which passes until 2026-04-10", checked.summary)
                self.assertIn(f"checked on {today}", checked.summary)
                self.assertIn(f"::notice file={CI_GUIDE}::", checked.stdout)

    def test_a_previous_guide_with_trailing_choices_still_matches_its_fixed_part(self) -> None:
        write(self.kit, f"previous/{CI_GUIDE}", self.PREVIOUS + "\nTrailing template text.\n")
        checked = self.check("callers", "--today", "2026-03-20")
        self.assertEqual(0, checked.status, checked.stdout + checked.stderr)
        self.assertIn("equals the previous version of its template", checked.summary)

    def test_after_that_month_a_pull_request_fails_with_the_open_sync_pull_request(self) -> None:
        url = self.api.sync_pull_request(31)
        checked = self.check("callers", "--today", "2026-04-11")
        self.assertEqual(1, checked.status, checked.stdout)
        self.assertIn(f"`{CI_GUIDE}` equals the previous version of its template, which passed until 2026-04-10", checked.summary)
        self.assertIn(url, checked.summary)

    def test_after_that_month_a_dependabot_pull_request_passes_with_a_notice(self) -> None:
        checked = self.check("callers", "--today", "2026-06-01", author="dependabot[bot]")
        self.assertEqual(0, checked.status, checked.stdout)
        self.assertIn("**Notice.**", checked.summary)
        self.assertNotIn("::error", checked.stdout)

    def test_a_document_that_equals_neither_version_fails_within_the_month(self) -> None:
        self.consumer({CI_GUIDE: "# CI\n\nOur own rules.\n"})
        self.assertEqual(1, self.check("callers", "--today", "2026-03-11").status)

    def test_a_record_that_names_the_ci_guide_without_its_previous_file_cannot_be_used(self) -> None:
        (self.kit / "previous" / CI_GUIDE).unlink()
        checked = self.check("callers")
        self.assertEqual(2, checked.status)
        self.assertIn(f"previous/{CI_GUIDE}", checked.stderr)


class RealKitTests(ConsumerFixture):
    """A consumer that holds the files of the real consumer kit, checked against that kit."""

    def setUp(self) -> None:
        super().setUp()
        self.kit = REAL_KIT
        for name in (*(f"{WORKFLOWS}/{caller}" for caller in ("ci.yml", "automation.yml", "release.yml")), CI_GUIDE):
            self.consumer({name: (REAL_KIT / name.removeprefix(f"{WORKFLOWS}/")).read_text(encoding="utf-8")})

    def test_a_consumer_that_holds_the_templates_of_the_kit_passes(self) -> None:
        checked = self.check("callers")
        self.assertEqual(0, checked.status, checked.stdout + checked.stderr)
        self.assertIn("Every fixed caller and the CI guide equal their templates", checked.summary)

    def test_a_consumer_with_choices_below_the_heading_of_the_real_template_passes(self) -> None:
        template_text = (REAL_KIT / CI_GUIDE).read_text(encoding="utf-8")
        self.assertTrue(template_text.endswith("\n## Repository choices\n"))
        self.consumer({CI_GUIDE: template_text + "\nThe ruleset requires a second check, because the product has a manual gate.\n"})
        self.assertEqual(0, self.check("callers").status)
        self.consumer({CI_GUIDE: template_text.replace("catalogue", "list", 1)})
        self.assertEqual(1, self.check("callers").status)

    def test_the_real_guides_follow_the_recorded_transition(self) -> None:
        record = json.loads((REAL_KIT / "fixed-callers.json").read_text(encoding="utf-8"))
        promoted = date.fromisoformat(record["previous"][CI_GUIDE])
        year, month = promoted.year + (promoted.month == 12), promoted.month % 12 + 1
        expires = date(year, month, min(promoted.day, calendar.monthrange(year, month)[1]))
        choices = "\nThe repository keeps its own choice.\n"
        current = (REAL_KIT / CI_GUIDE).read_text(encoding="utf-8")
        previous = (REAL_KIT / "previous" / CI_GUIDE).read_text(encoding="utf-8")
        self.assertNotEqual(current, previous)
        self.consumer({CI_GUIDE: previous + choices})
        for today, expected in ((promoted, 0), (expires, 0), (expires + timedelta(days=1), 1)):
            with self.subTest(today=today):
                checked = self.check("callers", "--today", today.isoformat())
                self.assertEqual(expected, checked.status, checked.stdout + checked.stderr)
        self.consumer({CI_GUIDE: current + choices})
        for today in (promoted, expires + timedelta(days=1)):
            with self.subTest(today=today):
                checked = self.check("callers", "--today", today.isoformat())
                self.assertEqual(0, checked.status, checked.stdout + checked.stderr)

    def test_a_consumer_without_the_kit_s_ci_guide_fails(self) -> None:
        self.consumer({CI_GUIDE: None})
        checked = self.check("callers")
        self.assertEqual(1, checked.status)
        self.assertIn(f"`{CI_GUIDE}` does not exist", checked.summary)


class SharedRepositoryTests(ConsumerFixture):
    def test_the_repository_of_the_shared_parts_holds_no_fixed_callers_and_passes(self) -> None:
        self.consumer({f"{WORKFLOWS}/ci.yml": "name: a local-path caller\n", LOCAL: "name: a shared workflow\n"})
        checked = self.check("callers", repository=SHARED)
        self.assertEqual(0, checked.status)
        self.assertIn("This repository holds the shared parts", checked.summary)
        self.assertIn("nothing to check", checked.stdout)
        self.assertEqual([], self.api.requests)


class AllConsumerChecksTests(ConsumerFixture):
    def test_all_runs_callers_register_and_dependabot_in_one_command(self) -> None:
        self.consumer({LOCAL: CALLABLE, CONFIGURATION: REGISTERED})
        self.api.issue(12, "open")
        checked = self.start("all")
        self.assertEqual(0, checked.status, checked.stdout + checked.stderr)
        self.assertIn("## Fixed callers and local workflows", checked.summary)
        self.assertIn("## Local workflow register", checked.summary)
        self.assertIn("## Generated Dependabot configuration", checked.summary)

    def test_all_reports_findings_from_every_check_and_fails_when_any_fails(self) -> None:
        self.consumer({f"{WORKFLOWS}/ci.yml": template("ci.yml") + "# drift\n",
                       CONFIGURATION: REGISTERED,
                       ".github/dependabot.yml": "version: 2\nupdates: []\n"})
        checked = self.check("all")
        self.assertEqual(1, checked.status, checked.stdout + checked.stderr)
        self.assertIn(f"`{WORKFLOWS}/ci.yml` differs from its template", checked.summary)
        self.assertIn("is no issue of this repository", checked.summary)
        self.assertIn("states no Dependabot ecosystems", checked.summary)

    def test_all_passes_without_checking_in_the_shared_repository(self) -> None:
        checked = self.check("all", repository=SHARED)
        self.assertEqual(0, checked.status)
        self.assertEqual(1, checked.summary.count("This repository holds the shared parts"))
        self.assertEqual([], self.api.requests)


if __name__ == "__main__":
    unittest.main()
