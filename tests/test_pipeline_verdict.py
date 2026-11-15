"""The verdict of the pipeline on the results of its jobs: when it fails, and what its summary lists."""

from __future__ import annotations

import json
import unittest

from tests.pipeline_fixtures import CONFIGURATION, PipelineFixture


def plan_output(switched_off: tuple[str, ...] = (), added: tuple[str, ...] = (), compared: bool = True) -> dict:
    plan = {"version": 1, "compared": compared, "switched-off": list(switched_off), "local-workflows-added": list(added),
            "parts": {"fixture-floor": {"run": True, "reason": "always runs", "config": {}},
                      "fixture-test": {"run": False, "reason": "no changed path matches its paths", "config": {"paths": ["src/"]}}}}
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
        for result in ("failure", "cancelled"):
            with self.subTest(part=result):
                outcome = self.verdict(needs(fixture_floor="success", fixture_test=result))
                self.assertEqual(1, outcome.status)
                self.assertIn(f"**Failed.** `fixture-test` ended {result}.", outcome.summary)
        outcome = self.verdict(needs(plan="failure", outputs={}, fixture_floor="failure", fixture_test="cancelled"))
        self.assertIn("**Failed.** `plan` ended failure; `fixture-floor` ended failure; `fixture-test` ended cancelled.", outcome.summary)

    def test_a_started_verdict_fails_with_status_1_on_a_failed_part(self) -> None:
        path = self.directory / "needs.json"
        path.write_text(json.dumps(needs(fixture_floor="failure")), encoding="utf-8")
        outcome = self.start_program("verdict", "--needs", str(path))
        self.assertEqual(1, outcome.status)
        self.assertIn("Verdict: failed: `fixture-floor` ended failure", outcome.stdout)

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

    def test_the_summary_gives_each_part_with_its_planned_decision_and_each_job_with_its_result(self) -> None:
        summary = self.verdict(needs(fixture_floor="success", fixture_test="skipped")).summary
        self.assertIn("| `fixture-test` | does not run | no changed path matches its paths |", summary)
        self.assertIn("| `fixture-floor` | runs | always runs |", summary)
        self.assertIn("| `plan` | success |", summary)
        self.assertIn("| `fixture-floor` | success |", summary)

    def test_the_verdict_lists_what_the_plan_of_a_real_change_found(self) -> None:
        base = self.commit({CONFIGURATION: "version: 1\nparts:\n  fixture-build: {}\n"})
        head = self.commit({CONFIGURATION: "version: 1\nlocal-workflows:\n  - workflow: .github/workflows/monitor.yml\n    declined-request: 9\n"})
        planned = self.plan("--base", base, "--head", head)
        summary = self.verdict({"plan": {"result": "success", "outputs": planned.outputs}, "fixture-floor": {"result": "success", "outputs": {}}}).summary
        self.assertIn("Parts switched off compared with the base:\n\n- `fixture-build`\n", summary)
        self.assertIn("Local workflows registered compared with the base:\n\n- `.github/workflows/monitor.yml`\n", summary)


if __name__ == "__main__":
    unittest.main()
