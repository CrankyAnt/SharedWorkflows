"""The caller check of the pipeline's hygiene job: a consumer's fixed callers equal their templates, or for one month
the previous version, its CI guide equals its template, and every other workflow file is registered in the
configuration file."""

from __future__ import annotations

import json
import unittest

from tests.consumer_checks_fixtures import CALLERS, CONSUMER, SHARED, SYNC_BRANCH, WORKFLOWS, Checked, ConsumerFixture, template
from tests.pipeline_fixtures import CONFIGURATION, PART_FOLDER, write

RULE = f"https://github.com/{SHARED}/blob/v1/catalogue.json"
LOCAL = f"{WORKFLOWS}/nightly-report.yml"
CI_GUIDE = "docs/agents/ci.md"
CI_GUIDE_TEXT = "# CI\n\nAn invented pointer to the catalogue.\n"
WITH_CHOICES = CI_GUIDE_TEXT + "\n## Repository choices\n"
REAL_KIT =PART_FOLDER / "consumer-kit"
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
                self.assertIn(f"::notice file={CI_GUIDE}::", checked.stdout)

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


PROOF = "example-owner/proof"
CANDIDATE = "v9-candidate"


class ProofRepositoryTests(ConsumerFixture):
    """The proof repository, which the record of the shared surface names: its callers equal the templates except that
    they call the shared workflows at the candidate tag."""

    def setUp(self) -> None:
        super().setUp()
        self.surface = self.directory / "shared-surface.json"
        self.surface.write_text(json.dumps({"proof_repository": PROOF, "candidate_tag": CANDIDATE}), encoding="utf-8")
        self.at_candidate()

    def at_candidate(self, names: tuple[str, ...] = CALLERS) -> None:
        self.consumer({f"{WORKFLOWS}/{name}": template(name).replace("@v1\n", f"@{CANDIDATE}\n") for name in names})

    def callers(self, repository: str = PROOF) -> Checked:
        return self.check("callers", "--surface", str(self.surface), repository=repository)

    def test_the_proof_repository_passes_with_callers_that_differ_from_the_templates_only_in_the_candidate_tag(self) -> None:
        checked = self.callers()
        self.assertEqual(0, checked.status, checked.stdout + checked.stderr)
        self.assertIn(f"Every fixed caller equals its template, except that a caller calls the shared workflow at the candidate tag `{CANDIDATE}`, and", checked.summary)
        self.assertEqual([], self.api.requests)

    def test_a_caller_of_the_proof_repository_that_calls_at_v1_fails(self) -> None:
        self.consumer({f"{WORKFLOWS}/ci.yml": template("ci.yml")})
        checked = self.callers()
        self.assertEqual(1, checked.status, checked.stdout)
        self.assertIn(f"`{WORKFLOWS}/ci.yml` differs from its template at the candidate tag `{CANDIDATE}`; in the proof repository a fixed caller equals its template "
                      "except for that tag.", checked.summary)
        self.assertNotIn("sync pull request", checked.summary, "no sync reaches the proof repository")
        self.assertIn(f"The proof kit holds its callers: https://github.com/{SHARED}/tree/v1/actions/pipeline/proof-kit", checked.summary)
        self.assertEqual([], self.api.requests)

    def test_any_other_difference_fails_in_the_proof_repository(self) -> None:
        for name, text in (("a second change", template("ci.yml").replace("@v1\n", f"@{CANDIDATE}\n") + "# a local change\n"),
                           ("another tag", template("ci.yml").replace("@v1\n", "@v2\n")),
                           ("the tag on another line", template("ci.yml").replace("name: ci\n", f"name: ci@{CANDIDATE}\n"))):
            with self.subTest(name=name):
                self.at_candidate()
                self.consumer({f"{WORKFLOWS}/ci.yml": text})
                checked = self.callers()
                self.assertEqual(1, checked.status, checked.stdout)
                self.assertIn(f"`{WORKFLOWS}/ci.yml` differs from its template at the candidate tag", checked.summary)

    def test_a_consumer_that_calls_at_the_candidate_tag_fails(self) -> None:
        checked = self.callers(repository=CONSUMER)
        self.assertEqual(1, checked.status, checked.stdout)
        for name in CALLERS:
            self.assertIn(f"`{WORKFLOWS}/{name}` differs from its template; a fixed caller equals its template byte for byte.", checked.summary)

    def test_the_previous_version_at_the_candidate_tag_passes_for_one_month_in_the_proof_repository(self) -> None:
        previous = self.previous("ci.yml", "2026-03-10")
        self.consumer({f"{WORKFLOWS}/ci.yml": previous.replace("@v1\n", f"@{CANDIDATE}\n")})
        self.assertEqual(0, self.check("callers", "--surface", str(self.surface), "--today", "2026-03-25", repository=PROOF).status)
        self.assertEqual(0, self.check("callers", "--surface", str(self.surface), "--today", "2026-04-10", repository=PROOF).status)
        self.assertEqual(1, self.check("callers", "--surface", str(self.surface), "--today", "2026-04-11", repository=PROOF).status)
        self.consumer({f"{WORKFLOWS}/ci.yml": previous})
        self.assertEqual(1, self.check("callers", "--surface", str(self.surface), "--today", "2026-03-25", repository=PROOF).status, "the previous version at v1 is no caller of the proof repository")

    def test_a_record_of_the_shared_surface_that_cannot_be_read_cannot_be_used(self) -> None:
        self.surface.write_text("{}", encoding="utf-8")
        checked = self.callers()
        self.assertEqual(2, checked.status)
        self.assertIn("proof_repository", checked.stderr)
        self.assertEqual(2, self.check("callers", "--surface", str(self.directory / "missing.json")).status)

    def test_the_other_checks_run_in_the_proof_repository_as_in_a_consumer(self) -> None:
        self.consumer({LOCAL: "name: Nightly report\n"})
        checked = self.callers()
        self.assertEqual(1, checked.status)
        self.assertIn(f"`{LOCAL}` is neither a fixed caller nor registered", checked.summary)


if __name__ == "__main__":
    unittest.main()
