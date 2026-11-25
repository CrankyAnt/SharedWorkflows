"""The verdict of the pipeline on the results of its jobs: when it fails, and what its summary lists."""

from __future__ import annotations

import json
import unittest

from tests.pipeline_fixtures import CONFIGURATION, PipelineFixture


def plan_output(switched_off: tuple[str, ...] = (), added: tuple[str, ...] = (), compared: bool = True,
                parts: dict[str, dict] | None = None) -> dict:
    plan = {"version": 1, "compared": compared, "switched-off": list(switched_off), "local-workflows-added": list(added),
            "parts": {"fixture-floor": {"run": True, "reason": "always runs", "config": {}},
                      "fixture-test": {"run": False, "reason": "no changed path matches its paths", "config": {"paths": ["src/"]}}}
            if parts is None else parts}
    return {"plan": json.dumps(plan)}


def needs(plan: str = "success", outputs: dict | None = None, **jobs: str) -> dict:
    context = {"plan": {"result": plan, "outputs": plan_output() if outputs is None else outputs}}
    context.update({name.replace("_", "-"): {"result": result, "outputs": {}} for name, result in jobs.items()})
    return context


class VerdictTests(PipelineFixture):
    def test_a_started_verdict_passes_when_the_plan_succeeded_and_every_started_part_succeeded(self) -> None:
        path = self.directory / "needs.json"
        path.write_text(json.dumps(needs(fixture_floor="success", fixture_test="skipped")), encoding="utf-8")
        outcome = self.start_program("verdict", "--needs", str(path))
        self.assertEqual(0, outcome.status, outcome.stdout + outcome.stderr)
        self.assertIn("Verdict: passed", outcome.stdout)
        self.assertIn("**Passed.**", outcome.summary)
        self.assertIn("| `fixture-test` | skipped |", outcome.summary)

    def test_a_part_that_did_not_run_leaves_the_verdict_green(self) -> None:
        self.assertEqual(0, self.verdict(needs(fixture_floor="success", fixture_test="skipped", fixture_build="skipped")).status)

    def test_a_failed_or_skipped_plan_fails_the_verdict(self) -> None:
        for result in ("failure", "skipped", "cancelled"):
            with self.subTest(plan=result):
                outcome = self.verdict(needs(plan=result, outputs={}, fixture_floor="success"))
                self.assertEqual(1, outcome.status)
                self.assertIn(f"**Failed.** `plan` ended {result}.", outcome.summary)

    def test_a_needs_context_without_the_plan_job_fails_the_verdict(self) -> None:
        outcome = self.verdict({"fixture-floor": {"result": "success", "outputs": {}}})
        self.assertEqual(1, outcome.status)
        self.assertIn("the needs context has no `plan` job", outcome.summary)

    def test_a_started_part_that_failed_or_was_cancelled_fails_the_verdict_and_is_named(self) -> None:
        both_planned = plan_output(parts={
            "fixture-floor": {"run": True, "reason": "always runs", "config": {}},
            "fixture-test": {"run": True, "reason": "src/a.py changed", "config": {"paths": ["src/"]}},
        })
        for result in ("failure", "cancelled"):
            with self.subTest(part=result):
                outcome = self.verdict(needs(outputs=both_planned, fixture_floor="success", fixture_test=result))
                self.assertEqual(1, outcome.status)
                self.assertIn(f"**Failed.** `fixture-test` ended {result}.", outcome.summary)
        outcome = self.verdict(needs(plan="failure", outputs={}, fixture_floor="failure", fixture_test="cancelled"))
        self.assertIn("**Failed.** `plan` ended failure; `fixture-floor` ended failure; `fixture-test` ended cancelled.", outcome.summary)

    def test_a_missing_or_invalid_plan_output_fails_the_verdict(self) -> None:
        cases = (
            {},
            {"plan": ""},
            {"plan": "{"},
            {"plan": "[]"},
            {"plan": "{}"},
            {"plan": '{"parts": []}'},
            {"plan": '{"parts": {"fixture-floor": "bad"}}'},
            {"plan": '{"parts": {"fixture-floor": {"run": "yes"}}}'},
            {"plan": '{"parts": {"fixture-floor": {"run": false, "reused": "yes"}}}'},
        )
        for outputs in cases:
            with self.subTest(outputs=outputs):
                outcome = self.verdict(needs(plan="success", outputs=outputs, fixture_floor="success"))
                self.assertEqual(1, outcome.status)
                self.assertIn("`plan`", outcome.summary)
                self.assertIn("**Failed.**", outcome.summary)

    def test_an_unplanned_check_that_ran_anyway_does_not_count_in_the_verdict(self) -> None:
        for result in ("failure", "cancelled", "timed_out", "success"):
            with self.subTest(unplanned_part_result=result):
                outcome = self.verdict(needs(fixture_floor="success", fixture_test=result))
                self.assertEqual(0, outcome.status, outcome.stdout + outcome.summary)
                self.assertIn("Verdict: passed", outcome.stdout)
        extra_failed = self.verdict(needs(fixture_floor="success", fixture_test="skipped", fixture_extra="failure"))
        self.assertEqual(0, extra_failed.status, extra_failed.stdout + extra_failed.summary)

    def test_a_started_verdict_fails_with_status_1_on_a_failed_part(self) -> None:
        path = self.directory / "needs.json"
        path.write_text(json.dumps(needs(fixture_floor="failure")), encoding="utf-8")
        outcome = self.start_program("verdict", "--needs", str(path))
        self.assertEqual(1, outcome.status)
        self.assertIn("Verdict: failed: `fixture-floor` ended failure", outcome.stdout)

    def test_a_planned_check_that_ended_skipped_fails_the_verdict_and_is_named(self) -> None:
        outcome = self.verdict(needs(fixture_floor="skipped", fixture_test="skipped"))
        self.assertEqual(1, outcome.status)
        self.assertIn("`fixture-floor` ended skipped", outcome.stdout)
        self.assertIn("**Failed.** `fixture-floor` ended skipped.", outcome.summary)

    def test_a_planned_check_that_timed_out_or_never_started_fails_the_verdict_and_is_named(self) -> None:
        timed_out = self.verdict(needs(fixture_floor="timed_out", fixture_test="skipped"))
        self.assertEqual(1, timed_out.status)
        self.assertIn("`fixture-floor` ended timed_out", timed_out.stdout)
        self.assertIn("**Failed.** `fixture-floor` ended timed_out.", timed_out.summary)

        missing = self.verdict(needs(fixture_test="skipped"))
        self.assertEqual(1, missing.status)
        self.assertIn("`fixture-floor` never started", missing.stdout)
        self.assertIn("**Failed.** `fixture-floor` never started.", missing.summary)

        for empty_result in ("", None):
            with self.subTest(result=empty_result):
                context = needs(fixture_test="skipped")
                context["fixture-floor"] = {"result": empty_result, "outputs": {}}
                never_started = self.verdict(context)
                self.assertEqual(1, never_started.status)
                self.assertIn("`fixture-floor` never started", never_started.stdout)
                self.assertIn("**Failed.** `fixture-floor` never started.", never_started.summary)

    def test_suite_selection_jobs_must_succeed_when_suites_are_selected_and_count_as_not_run_otherwise(self) -> None:
        suite_plan = plan_output(parts={
            "repository-hygiene": {"run": True, "reason": "always runs", "config": {}},
            "suite-selection": {"run": True, "reason": "switched on", "config": {"suites": "tests/suites.json"}},
        })
        selected_outputs = {"shards": json.dumps([{"shard": 1, "entries": ["tests/a.py"]}]), "windows": ""}
        passed = needs(outputs=suite_plan, repository_hygiene="success", suite_selection_jobs="success", suite_selection_results="skipped")
        passed["suite-selection"] = {"result": "success", "outputs": selected_outputs}
        self.assertEqual(0, self.verdict(passed).status)

        for job_result, expected in (("skipped", "`suite-selection-jobs` ended skipped"),
                                     ("timed_out", "`suite-selection-jobs` ended timed_out"),
                                     ("failure", "`suite-selection-jobs` ended failure"),
                                     ("cancelled", "`suite-selection-jobs` ended cancelled")):
            with self.subTest(job_result=job_result):
                context = needs(outputs=suite_plan, repository_hygiene="success", suite_selection_jobs=job_result)
                context["suite-selection"] = {"result": "success", "outputs": selected_outputs}
                outcome = self.verdict(context)
                self.assertEqual(1, outcome.status)
                self.assertIn(expected, outcome.summary)

        missing_jobs = needs(outputs=suite_plan, repository_hygiene="success")
        missing_jobs["suite-selection"] = {"result": "success", "outputs": {"shards": "[]", "windows": '{"entries":["tests/w.py"]}'}}
        outcome = self.verdict(missing_jobs)
        self.assertEqual(1, outcome.status)
        self.assertIn("`suite-selection-jobs` never started", outcome.summary)

        none_selected = needs(outputs=suite_plan, repository_hygiene="success", suite_selection_jobs="skipped", suite_selection_results="skipped")
        none_selected["suite-selection"] = {"result": "success", "outputs": {"shards": "[]", "windows": ""}}
        self.assertEqual(0, self.verdict(none_selected).status)

        unplanned_jobs_ran = needs(outputs=suite_plan, repository_hygiene="success", suite_selection_jobs="failure")
        unplanned_jobs_ran["suite-selection"] = {"result": "success", "outputs": {"shards": "[]", "windows": ""}}
        self.assertEqual(0, self.verdict(unplanned_jobs_ran).status)

        bad_outputs = needs(outputs=suite_plan, repository_hygiene="success", suite_selection_jobs="skipped")
        bad_outputs["suite-selection"] = {"result": "success", "outputs": {"shards": "{"}}
        bad = self.verdict(bad_outputs)
        self.assertEqual(1, bad.status)
        self.assertIn("`suite-selection`", bad.summary)

        missing_outputs = needs(outputs=suite_plan, repository_hygiene="success", suite_selection_jobs="skipped")
        missing_outputs["suite-selection"] = {"result": "success", "outputs": {}}
        missing_out = self.verdict(missing_outputs)
        self.assertEqual(1, missing_out.status)
        self.assertIn("`suite-selection`", missing_out.summary)

        failed_store = needs(outputs=suite_plan, repository_hygiene="success", suite_selection_jobs="success", suite_selection_results="failure")
        failed_store["suite-selection"] = {"result": "success", "outputs": selected_outputs}
        store_out = self.verdict(failed_store)
        self.assertEqual(1, store_out.status)
        self.assertIn("`suite-selection-results` ended failure", store_out.summary)

        unplanned_selection = plan_output(parts={
            "repository-hygiene": {"run": True, "reason": "always runs", "config": {}},
            "suite-selection": {"run": False, "reason": "switched off", "config": {}},
        })
        ignored = needs(outputs=unplanned_selection, repository_hygiene="success", suite_selection="failure",
                        suite_selection_jobs="failure", suite_selection_results="failure")
        self.assertEqual(0, self.verdict(ignored).status)

    def test_an_unreadable_needs_context_is_an_error_of_its_own(self) -> None:
        path = self.directory / "needs.json"
        path.write_text("[", encoding="utf-8")
        self.assertEqual(2, self.run_program("verdict", "--needs", str(path)).status)


class VerdictSummaryTests(PipelineFixture):
    def test_the_summary_lists_every_part_switched_off_and_every_local_workflow_registered_compared_with_the_base(self) -> None:
        outputs = plan_output(switched_off=("fixture-build", "fixture-test"), added=(".github/workflows/monitor.yml", ".github/workflows/nightly.yml"))
        for context in (needs(outputs=outputs, fixture_floor="success"), needs(outputs=outputs, fixture_floor="failure")):
            summary = self.verdict(context).summary
            self.assertIn("Parts switched off compared with the base:\n\n- `fixture-build`\n- `fixture-test`\n", summary)
            self.assertIn("Local workflows registered compared with the base:\n\n- `.github/workflows/monitor.yml`\n- `.github/workflows/nightly.yml`\n", summary)

    def test_the_summary_says_so_when_nothing_is_switched_off_or_registered(self) -> None:
        summary = self.verdict(needs(fixture_floor="success")).summary
        self.assertIn("No part is switched off compared with the base.", summary)
        self.assertIn("No local workflow is registered compared with the base.", summary)

    def test_the_summary_says_so_when_the_base_could_not_be_compared_or_the_plan_gave_no_plan(self) -> None:
        self.assertIn("could not be compared with the base", self.verdict(needs(outputs=plan_output(compared=False), fixture_floor="success")).summary)
        for outputs in ({}, {"plan": ""}, {"plan": "{"}, {"plan": "[]"}):
            with self.subTest(outputs=outputs):
                self.assertIn("The `plan` job gave no plan", self.verdict(needs(plan="failure", outputs=outputs)).summary)

    def test_the_summary_lists_every_check_with_ran_skipped_or_taken_over_and_its_reason(self) -> None:
        outputs = plan_output(parts={
            "fixture-floor": {"run": True, "reason": "always runs", "config": {}},
            "fixture-test": {"run": False, "reason": "no changed path matches its paths", "config": {"paths": ["src/"]}},
            "fixture-build": {"run": False, "reused": True, "reason": "the tree equals a passed tested tree", "config": {}},
        })
        summary = self.verdict(needs(outputs=outputs, fixture_floor="success", fixture_test="skipped", fixture_build="skipped")).summary
        self.assertIn("| Check | Outcome | Result | Reason |", summary)
        self.assertIn("| `fixture-floor` | ran | success | always runs |", summary)
        self.assertIn("| `fixture-test` | skipped | not run | no changed path matches its paths |", summary)
        self.assertIn("| `fixture-build` | taken over | not run | the tree equals a passed tested tree |", summary)
        self.assertIn("| `plan` | success |", summary)
        self.assertIn("| `fixture-floor` | success |", summary)

    def test_the_summary_lists_suite_selection_jobs_as_ran_skipped_or_taken_over_with_its_reason(self) -> None:
        suite_plan = plan_output(parts={
            "repository-hygiene": {"run": True, "reason": "always runs", "config": {}},
            "suite-selection": {"run": True, "reason": "switched on", "config": {"suites": "tests/suites.json"}},
        })
        for label, sel_outputs, jobs_result, expected_row in (
            ("ran", {"shards": json.dumps([{"shard": 1}]), "windows": ""}, "success",
             "| `suite-selection-jobs` | ran | success | 1 Linux shard |"),
            ("skipped", {"shards": "[]", "windows": ""}, "skipped",
             "| `suite-selection-jobs` | skipped | not run | no suites selected |"),
            ("taken-over-identical", {"shards": "[]", "windows": "", "identical": "true"}, "skipped",
             "| `suite-selection-jobs` | taken over | not run | the tree equals a passed tested tree |"),
            ("taken-over-reused", {"shards": "[]", "windows": "", "reused": json.dumps({"tests/a.py": {"run_id": 7}})}, "skipped",
             "| `suite-selection-jobs` | taken over | not run | 1 passed unit taken over |"),
        ):
            with self.subTest(case=label):
                context = needs(outputs=suite_plan, repository_hygiene="success", suite_selection_jobs=jobs_result)
                context["suite-selection"] = {"result": "success", "outputs": sel_outputs}
                outcome = self.verdict(context)
                self.assertEqual(0, outcome.status, outcome.stdout + outcome.summary)
                self.assertIn(expected_row, outcome.summary)

    def test_the_verdict_lists_what_the_plan_of_a_real_change_found(self) -> None:
        base = self.commit({CONFIGURATION: "version: 1\nparts:\n  fixture-build: {}\n"})
        head = self.commit({CONFIGURATION: "version: 1\nlocal-workflows:\n  - workflow: .github/workflows/monitor.yml\n    declined-request: 9\n"})
        planned = self.plan("--base", base, "--head", head)
        summary = self.verdict({"plan": {"result": "success", "outputs": planned.outputs}, "fixture-floor": {"result": "success", "outputs": {}}}).summary
        self.assertIn("Parts switched off compared with the base:\n\n- `fixture-build`\n", summary)
        self.assertIn("Local workflows registered compared with the base:\n\n- `.github/workflows/monitor.yml`\n", summary)


if __name__ == "__main__":
    unittest.main()
