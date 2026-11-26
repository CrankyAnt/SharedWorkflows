"""Tests of the measurement command against recorded GitHub API responses (Seam 6 of #171)."""

from __future__ import annotations

import unittest

from tests.measurement_fixtures import CONSUMER, MANAGEMENT, OTHER, MeasurementFixture


class MeasurementThreeMinuteTargetTests(MeasurementFixture):
    def test_reports_per_repository_share_within_3_minutes_over_rolling_four_weeks_counting_reruns_by_first_attempt(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        # 2 runs within 3 minutes (120s and 180s), 1 run over 3 minutes (240s)
        consumer.add_run("2026-03-20T10:00:00Z", verdict_completed_at="2026-03-20T10:02:00Z")
        consumer.add_run("2026-03-25T10:00:00Z", verdict_completed_at="2026-03-25T10:03:00Z")
        consumer.add_run("2026-03-30T10:00:00Z", verdict_completed_at="2026-03-30T10:04:00Z")
        # Excluded: older than 4 weeks (28 days before 2026-04-01T12:00:00Z is 2026-03-04T12:00:00Z)
        consumer.add_run("2026-03-01T10:00:00Z", verdict_completed_at="2026-03-01T10:10:00Z")
        # Re-run run (run_attempt=2): attempt 1 failed after 150s (within 3m), attempt 2 finished 10m later -> counted by attempt 1!
        consumer.add_run(
            "2026-03-26T10:00:00Z",
            verdict_completed_at="2026-03-26T10:10:00Z",
            run_attempt=2,
            attempt_1={"conclusion": "failure", "verdict_completed_at": "2026-03-26T10:02:30Z"},
        )
        # Excluded: manual workflow_dispatch
        consumer.add_run("2026-03-27T10:00:00Z", verdict_completed_at="2026-03-27T10:10:00Z", event="workflow_dispatch")
        # Excluded: cancelled run (no verdict)
        consumer.add_run("2026-03-28T10:00:00Z", verdict_completed_at=None, conclusion="cancelled", verdict_job_name=None)
        # Excluded: verdict skipped (did not reach a verdict)
        consumer.add_run("2026-03-29T10:00:00Z", verdict_completed_at="2026-03-29T10:05:00Z", verdict_conclusion="skipped")

        other = self.stand_in.repository(OTHER)
        other.add_run("2026-03-31T10:00:00Z", verdict_completed_at="2026-03-31T10:01:00Z")

        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, outcome.status, outcome.stderr)
        assert outcome.report is not None
        by_repo = outcome.report["repositories"]

        # 3 within 3m (120s, 180s, and attempt-1 150s) out of 4 verdict runs -> 75.0%
        self.assertEqual(4, by_repo[CONSUMER]["verdict_runs"])
        self.assertEqual(3, by_repo[CONSUMER]["within_3m_count"])
        self.assertAlmostEqual(3 / 4, by_repo[CONSUMER]["within_3m_share"], places=4)
        self.assertEqual(1, by_repo[OTHER]["verdict_runs"])
        self.assertEqual(1, by_repo[OTHER]["within_3m_count"])
        self.assertAlmostEqual(1.0, by_repo[OTHER]["within_3m_share"], places=4)
        self.assertIn(CONSUMER, outcome.summary)
        self.assertIn("75.0%", outcome.summary)
        self.assertIn("first attempt", outcome.summary)
        self.assertIn(OTHER, outcome.summary)
        self.assertIn("100.0%", outcome.summary)

    def test_rerun_attempt_1_is_counted_even_when_attempt_2_is_in_progress_or_cancelled_and_picks_attempt_1_plan(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        no_tests_plan = {
            "version": 1,
            "parts": {"repository-hygiene": {"run": True, "reason": "always runs"}, "python-test": {"run": False, "reason": "none"}},
            "departures": [],
        }
        with_tests_plan = {
            "version": 1,
            "parts": {"repository-hygiene": {"run": True, "reason": "always runs"}, "python-test": {"run": True, "reason": "rerun"}},
            "fallbacks": {"rerun-fallback": 1},
            "departures": [],
        }
        # Attempt 2 is still in_progress; attempt 1 completed in 45s with no_tests_plan created at 10:00:15,
        # while attempt 2 uploaded with_tests_plan at 10:05:15. Must use attempt 1's verdict and attempt 1's plan.
        consumer.add_run(
            "2026-03-20T10:00:00Z",
            status="in_progress",
            conclusion=None,
            verdict_completed_at=None,
            verdict_status="in_progress",
            verdict_conclusion=None,
            run_attempt=2,
            plan=with_tests_plan,
            plan_created_at="2026-03-20T10:05:15Z",
            extra_artifacts=[(no_tests_plan, "2026-03-20T10:00:15Z", False)],
            attempt_1={"conclusion": "failure", "verdict_completed_at": "2026-03-20T10:00:45Z"},
        )
        # Attempt 2 was cancelled; attempt 1 completed in 55s, only artifact was created AFTER attempt-1 verdict -> drop plan data.
        consumer.add_run(
            "2026-03-21T10:00:00Z",
            status="completed",
            conclusion="cancelled",
            verdict_completed_at=None,
            verdict_job_name=None,
            run_attempt=2,
            plan=with_tests_plan,
            plan_created_at="2026-03-21T10:04:00Z",
            attempt_1={"conclusion": "failure", "verdict_completed_at": "2026-03-21T10:00:55Z"},
        )
        # Attempt 1 itself was cancelled -> excluded.
        consumer.add_run(
            "2026-03-22T10:00:00Z",
            status="completed",
            conclusion="success",
            verdict_completed_at="2026-03-22T10:05:00Z",
            run_attempt=2,
            attempt_1={"conclusion": "cancelled", "verdict_completed_at": None, "verdict_job_name": None},
        )

        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, outcome.status, outcome.stderr)
        assert outcome.report is not None
        metrics = outcome.report["repositories"][CONSUMER]
        self.assertEqual(2, metrics["verdict_runs"])
        self.assertEqual(2, metrics["within_3m_count"])
        self.assertEqual(1, metrics["no_tests_runs"])
        self.assertAlmostEqual(45.0, metrics["no_tests_median_seconds"], places=2)
        self.assertEqual({}, metrics["fallbacks"])

    def test_unreadable_first_attempt_raises_an_error_and_produces_an_error_row(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        # Re-run run without attempt 1 recorded in the stand-in -> GET attempts/1 returns 404 -> MeasurementError.
        consumer.add_run("2026-03-26T10:00:00Z", verdict_completed_at="2026-03-26T10:05:00Z", run_attempt=2)

        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(1, outcome.status)
        self.assertIn("attempts/1", outcome.stderr)
        self.assertIn("could not be measured:", outcome.summary)
        assert outcome.report is not None
        self.assertIn("attempts/1", outcome.report["repositories"][CONSUMER]["error"])


class MeasurementPlanMetricsTests(MeasurementFixture):
    def test_reports_median_of_runs_whose_plan_selects_no_tests(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        no_tests_plan = {
            "version": 1,
            "parts": {
                "repository-hygiene": {"run": True, "reason": "always runs"},
                "python-test": {"run": False, "reason": "no changed path matches its paths"},
                "suite-selection": {"run": False, "reason": "no suites selected"},
            },
            "units": {"tests/test_a.py": {"run": False, "reason": "reused", "reused": True}},
            "departures": [],
        }
        with_tests_plan = {
            "version": 1,
            "parts": {
                "repository-hygiene": {"run": True, "reason": "always runs"},
                "python-test": {"run": True, "reason": "src/app.py changed"},
            },
            "departures": [],
        }
        # Three no-tests runs: 30s, 50s, 80s -> median 50.0s
        consumer.add_run("2026-03-20T10:00:00Z", verdict_completed_at="2026-03-20T10:00:30Z", plan=no_tests_plan)
        consumer.add_run("2026-03-21T10:00:00Z", verdict_completed_at="2026-03-21T10:00:50Z", plan=no_tests_plan)
        consumer.add_run("2026-03-22T10:00:00Z", verdict_completed_at="2026-03-22T10:01:20Z", plan=no_tests_plan)
        # One run that selects tests: 150s -> excluded from no-tests median
        consumer.add_run("2026-03-23T10:00:00Z", verdict_completed_at="2026-03-23T10:02:30Z", plan=with_tests_plan)
        # One run whose plan artifact expired -> excluded from no-tests median, still counted in verdict_runs
        consumer.add_run("2026-03-24T10:00:00Z", verdict_completed_at="2026-03-24T10:00:20Z", plan=no_tests_plan, plan_expired=True)

        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, outcome.status, outcome.stderr)
        assert outcome.report is not None
        metrics = outcome.report["repositories"][CONSUMER]
        self.assertEqual(5, metrics["verdict_runs"])
        self.assertEqual(3, metrics["no_tests_runs"])
        self.assertAlmostEqual(50.0, metrics["no_tests_median_seconds"], places=2)
        self.assertIn("50.0s", outcome.summary)

    def test_a_suite_selection_that_runs_only_the_language_step_selects_no_tests(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        language_step_only = {
            "version": 1,
            "parts": {
                "repository-hygiene": {"run": True, "reason": "always runs"},
                "suite-selection": {"run": True, "reason": "1 Linux shard"},
            },
            "units": {"unit:tests/test_a.py": {"run": False, "reason": "no read changed"}},
            "departures": [],
        }
        with_test_unit = {**language_step_only, "units": {"unit:tests/test_a.py": {"run": True, "reason": "src/a.py changed"}}}
        units_unknown = {key: value for key, value in language_step_only.items() if key != "units"}
        consumer.add_run("2026-03-20T10:00:00Z", verdict_completed_at="2026-03-20T10:00:40Z", plan=language_step_only)
        consumer.add_run("2026-03-21T10:00:00Z", verdict_completed_at="2026-03-21T10:02:00Z", plan=with_test_unit)
        consumer.add_run("2026-03-22T10:00:00Z", verdict_completed_at="2026-03-22T10:02:30Z", plan=units_unknown)

        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, outcome.status, outcome.stderr)
        assert outcome.report is not None
        metrics = outcome.report["repositories"][CONSUMER]
        self.assertEqual(3, metrics["verdict_runs"])
        self.assertEqual(1, metrics["no_tests_runs"])
        self.assertAlmostEqual(40.0, metrics["no_tests_median_seconds"], places=2)

    def test_lists_fallbacks_by_reason_and_parts_that_run_with_unknown_inputs(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        plan_a = {
            "version": 1,
            "parts": {
                "repository-hygiene": {"run": True, "reason": "always runs"},
                "custom": {"run": True, "reason": "inputs unknown"},
                "python-test": {"run": True, "reason": "inputs unknown"},
            },
            "departures": [
                {"part": "custom", "reason": "inputs unknown"},
                {"part": "python-test", "reason": "inputs unknown"},
            ],
            "fallbacks": {
                "missing-default-branch-record": 1,
                "changed-files-unknown": 2,
            },
        }
        plan_b = {
            "version": 1,
            "parts": {
                "repository-hygiene": {"run": True, "reason": "always runs"},
                "custom": {"run": True, "reason": "inputs unknown"},
            },
            "departures": [
                {"part": "custom", "reason": "inputs unknown"},
            ],
            "fallbacks": {
                "missing-default-branch-record": 3,
            },
        }
        consumer.add_run("2026-03-20T10:00:00Z", verdict_completed_at="2026-03-20T10:01:00Z", plan=plan_a)
        consumer.add_run("2026-03-21T10:00:00Z", verdict_completed_at="2026-03-21T10:01:00Z", plan=plan_b)

        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, outcome.status, outcome.stderr)
        assert outcome.report is not None
        metrics = outcome.report["repositories"][CONSUMER]
        self.assertEqual({"changed-files-unknown": 2, "missing-default-branch-record": 4}, metrics["fallbacks"])
        self.assertEqual(["custom", "python-test"], metrics["unknown_input_parts"])
        self.assertIn("missing-default-branch-record", outcome.summary)
        self.assertIn("changed-files-unknown", outcome.summary)
        self.assertIn("`custom`", outcome.summary)
        self.assertIn("`python-test`", outcome.summary)


class MeasurementMissIssueTests(MeasurementFixture):
    NO_TESTS_PLAN = {
        "version": 1,
        "parts": {
            "repository-hygiene": {"run": True, "reason": "always runs"},
            "python-test": {"run": False, "reason": "no changed path matches its paths"},
        },
        "departures": [],
    }

    def test_a_missed_target_opens_an_issue_naming_the_repository_and_the_target_and_deduplicates_while_open(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        # 1/2 within 3m (50% < 99%) AND no-tests median = 135s (> 60s) -> misses both targets
        consumer.add_run("2026-03-20T10:00:00Z", verdict_completed_at="2026-03-20T10:01:30Z", plan=self.NO_TESTS_PLAN)
        consumer.add_run("2026-03-21T10:00:00Z", verdict_completed_at="2026-03-21T10:03:30Z", plan=self.NO_TESTS_PLAN)

        other = self.stand_in.repository(OTHER)
        # 1/1 within 3m (100% >= 99%) AND no-tests median = 40s (<= 60s) -> meets both targets
        other.add_run("2026-03-22T10:00:00Z", verdict_completed_at="2026-03-22T10:00:40Z", plan=self.NO_TESTS_PLAN)

        first = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, first.status, first.stderr)
        self.assertEqual(2, len(self.management.issues))
        titles = [issue["title"] for issue in self.management.issues]
        self.assertTrue(any(CONSUMER in title and "3 minutes" in title for title in titles), titles)
        self.assertTrue(any(CONSUMER in title and "1 minute" in title for title in titles), titles)
        self.assertFalse(any(OTHER in title for title in titles), titles)
        # Every write used the run token, never the installation token.
        self.assertTrue(self.stand_in.writes)
        self.assertTrue(all(write.token != "token-of-the-measurement-app" for write in self.stand_in.writes))

        # Running again while both issues are open opens no duplicate issues.
        second = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, second.status, second.stderr)
        self.assertEqual(2, len(self.management.issues))

        # Closing one issue lets a subsequent miss open a new one for that target only.
        self.management.issues[0]["state"] = "closed"
        third = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, third.status, third.stderr)
        self.assertEqual(3, len(self.management.issues))

    def test_failure_subcommand_opens_one_issue_while_open(self) -> None:
        first = self.run_measurement("failure", "--own-repository", MANAGEMENT, "--run-url", "https://github.example/runs/42")
        self.assertEqual(0, first.status, first.stderr)
        self.assertEqual(1, len(self.management.issues))
        self.assertIn("https://github.example/runs/42", self.management.issues[0]["body"])
        second = self.run_measurement("failure", "--own-repository", MANAGEMENT, "--run-url", "https://github.example/runs/43")
        self.assertEqual(0, second.status, second.stderr)
        self.assertEqual(1, len(self.management.issues))

    def test_local_mode_measures_named_repositories_without_opening_issues(self) -> None:
        consumer = self.stand_in.repository(CONSUMER, installed=False)
        consumer.add_run("2026-03-20T10:00:00Z", verdict_completed_at="2026-03-20T10:05:00Z", plan=self.NO_TESTS_PLAN)
        outcome = self.run_measurement("measure", "--local", "--repository", CONSUMER)
        self.assertEqual(0, outcome.status, outcome.stderr)
        assert outcome.report is not None
        self.assertEqual([CONSUMER], list(outcome.report["repositories"]))
        self.assertEqual([], self.stand_in.writes)


class MeasurementCoverageAndCapTests(MeasurementFixture):
    def test_installed_repository_without_ci_workflow_opens_a_finding(self) -> None:
        self.stand_in.repository(CONSUMER, has_workflow=False)
        first = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, first.status, first.stderr)
        self.assertEqual(1, len(self.management.issues))
        self.assertIn(CONSUMER, self.management.issues[0]["title"])
        self.assertIn("ci.yml", self.management.issues[0]["body"])
        self.assertIn("no `ci.yml` workflow", first.summary)
        # Deduplicated while open.
        second = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, second.status, second.stderr)
        self.assertEqual(1, len(self.management.issues))

    def test_custom_workflow_option_is_named_consistently_in_no_workflow_report_row_and_finding(self) -> None:
        self.stand_in.repository(CONSUMER, has_workflow=False)
        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--workflow", "custom-ci.yml")
        self.assertEqual(0, outcome.status, outcome.stderr)
        self.assertIn("no `custom-ci.yml` workflow", outcome.summary)
        self.assertEqual(1, len(self.management.issues))
        self.assertIn("custom-ci.yml", self.management.issues[0]["title"])
        self.assertIn("custom-ci.yml", self.management.issues[0]["body"])

    def test_repository_with_completed_runs_but_no_verdict_opens_a_finding_while_zero_runs_does_not(self) -> None:
        broken = self.stand_in.repository(CONSUMER)
        broken.add_run("2026-03-20T10:00:00Z", verdict_completed_at="2026-03-20T10:01:00Z", verdict_conclusion="skipped")
        broken.add_run("2026-03-21T10:00:00Z", verdict_completed_at=None, verdict_job_name=None, conclusion="failure")
        # Quiet repository with no completed runs at all -> no finding.
        self.stand_in.repository(OTHER)

        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, outcome.status, outcome.stderr)
        assert outcome.report is not None
        self.assertEqual(0, outcome.report["repositories"][CONSUMER]["verdict_runs"])
        self.assertEqual(2, outcome.report["repositories"][CONSUMER]["non_verdict_runs"])
        self.assertEqual(1, len(self.management.issues))
        self.assertIn(CONSUMER, self.management.issues[0]["title"])
        self.assertIn("verdict", self.management.issues[0]["title"].lower())
        self.assertFalse(any(OTHER in issue["title"] for issue in self.management.issues))

    def test_repository_that_raises_an_error_gets_a_report_row_with_the_reason(self) -> None:
        self.stand_in.repository(CONSUMER)
        self.stand_in.refused[f"/repos/{CONSUMER}/actions/workflows/ci.yml/runs"] = 500
        other = self.stand_in.repository(OTHER)
        other.add_run("2026-03-31T10:00:00Z", verdict_completed_at="2026-03-31T10:01:00Z")

        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(1, outcome.status)
        self.assertIn(CONSUMER, outcome.summary)
        self.assertIn("could not be measured:", outcome.summary)
        self.assertIn("HTTP 500", outcome.summary)
        assert outcome.report is not None
        self.assertIn("HTTP 500", outcome.report["repositories"][CONSUMER]["error"])
        self.assertEqual(1, outcome.report["repositories"][OTHER]["verdict_runs"])

    def test_created_query_is_url_encoded_and_reaching_the_1000_result_cap_fails_loudly(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        for _ in range(1005):
            consumer.add_run("2026-03-20T10:00:00Z", verdict_completed_at="2026-03-20T10:01:00Z")

        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(1, outcome.status)
        self.assertIn("1000", outcome.stderr)
        self.assertIn("could not be measured:", outcome.summary)
        self.assertIn("1000", outcome.summary)
        run_requests = [path for method, path, _ in self.stand_in.requests if method == "GET" and "/actions/workflows/ci.yml/runs" in path]
        self.assertTrue(run_requests)
        self.assertTrue(all("created=%3E%3D" in path for path in run_requests), run_requests[:3])


class MeasurementFixedCostTests(MeasurementFixture):
    ENTRY_STEP = "Run the shard's entry files under the recording, two at a time"
    LANG_STEP = "Run the Python language step outside the recording"

    def _shard_job(
        self,
        number: int,
        *,
        started_at: str,
        entry_started_at: str,
        entry_completed_at: str,
        completed_at: str,
        entry_conclusion: str = "success",
        lang_step: tuple[str, str, str] | None = None,
    ) -> dict:
        steps = [
            {
                "name": self.ENTRY_STEP,
                "status": "completed",
                "conclusion": entry_conclusion,
                "started_at": entry_started_at,
                "completed_at": entry_completed_at,
            },
        ]
        if lang_step is not None:
            lang_conclusion, lang_start, lang_end = lang_step
            steps.append({
                "name": self.LANG_STEP,
                "status": "completed",
                "conclusion": lang_conclusion,
                "started_at": lang_start,
                "completed_at": lang_end,
            })
        return {
            "name": f"ci / suite-selection-jobs / shard {number}",
            "status": "completed",
            "conclusion": "success",
            "started_at": started_at,
            "completed_at": completed_at,
            "steps": steps,
        }

    def test_reports_fixed_cost_quantile_split_and_phases_and_opens_finding_above_assumed(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        # Run 1 (matches run 45744858027 shape):
        # created=10:00:00, plan_end=10:00:19 (19s), entry=10:00:47..10:03:16 (shard->entry 28s, entry 149s),
        # lang_step skipped (0s), shard_end=10:03:31 (after 15s), results_end=10:03:45 (results 14s),
        # verdict_end=10:03:58 (verdict 13s). Total=238s, estimate=71.0s -> outside=89.0s, excess=78.0s, fixed_cost=167.0s.
        # Shard 2 finishes earlier (10:02:00) so shard 1 is the critical shard.
        consumer.add_run(
            "2026-03-20T10:00:00Z",
            plan_completed_at="2026-03-20T10:00:19Z",
            verdict_completed_at="2026-03-20T10:03:58Z",
            plan={
                "version": 1,
                "parts": {"repository-hygiene": {"run": True, "reason": "always runs"}, "suite-selection": {"run": True, "reason": "2 Linux shards"}},
                "units": {"a": {"run": True, "reason": "changed"}},
                "shards": [{"shard": 1, "estimate": 71.0}, {"shard": 2, "estimate": 40.0}],
                "departures": [],
            },
            extra_jobs=[
                self._shard_job(
                    1,
                    started_at="2026-03-20T10:00:25Z",
                    entry_started_at="2026-03-20T10:00:47Z",
                    entry_completed_at="2026-03-20T10:03:16Z",
                    completed_at="2026-03-20T10:03:31Z",
                    lang_step=("skipped", "2026-03-20T10:03:20Z", "2026-03-20T10:03:25Z"),
                ),
                self._shard_job(
                    2,
                    started_at="2026-03-20T10:00:25Z",
                    entry_started_at="2026-03-20T10:00:40Z",
                    entry_completed_at="2026-03-20T10:01:40Z",
                    completed_at="2026-03-20T10:02:00Z",
                ),
                {
                    "name": "ci / suite-selection-results",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "2026-03-20T10:03:32Z",
                    "completed_at": "2026-03-20T10:03:45Z",
                },
            ],
        )
        # Run 2: fixed cost below 90s (outside=50s, lang_step=5s subtracted from outside, excess=5s -> fixed_cost=55s).
        consumer.add_run(
            "2026-03-21T10:00:00Z",
            plan_completed_at="2026-03-21T10:00:15Z",
            verdict_completed_at="2026-03-21T10:01:30Z",
            plan={
                "version": 1,
                "parts": {"repository-hygiene": {"run": True, "reason": "always runs"}, "suite-selection": {"run": True, "reason": "1 Linux shard"}},
                "units": {"a": {"run": True, "reason": "changed"}},
                "shards": [{"shard": 1, "estimate": 30.0}],
                "departures": [],
            },
            extra_jobs=[
                self._shard_job(
                    1,
                    started_at="2026-03-21T10:00:18Z",
                    entry_started_at="2026-03-21T10:00:30Z",
                    entry_completed_at="2026-03-21T10:01:05Z",
                    completed_at="2026-03-21T10:01:20Z",
                    lang_step=("success", "2026-03-21T10:01:10Z", "2026-03-21T10:01:15Z"),
                ),
            ],
        )
        # Run 3: Windows job finishes after the Linux shard -> left out and counted as non-shard-decided.
        consumer.add_run(
            "2026-03-22T10:00:00Z",
            plan_completed_at="2026-03-22T10:00:15Z",
            verdict_completed_at="2026-03-22T10:02:30Z",
            plan={
                "version": 1,
                "parts": {"repository-hygiene": {"run": True, "reason": "always runs"}, "suite-selection": {"run": True, "reason": "1 Linux shard and Windows"}},
                "units": {"a": {"run": True, "reason": "changed"}},
                "shards": [{"shard": 1, "estimate": 20.0}],
                "departures": [],
            },
            extra_jobs=[
                self._shard_job(
                    1,
                    started_at="2026-03-22T10:00:18Z",
                    entry_started_at="2026-03-22T10:00:30Z",
                    entry_completed_at="2026-03-22T10:00:50Z",
                    completed_at="2026-03-22T10:01:00Z",
                ),
                {
                    "name": "ci / suite-selection-jobs / windows",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "2026-03-22T10:00:18Z",
                    "completed_at": "2026-03-22T10:02:10Z",
                },
            ],
        )
        # Run 4: hygiene finishes after Linux shard -> left out and counted as non-shard-decided.
        consumer.add_run(
            "2026-03-23T10:00:00Z",
            plan_completed_at="2026-03-23T10:00:15Z",
            verdict_completed_at="2026-03-23T10:02:00Z",
            plan={
                "version": 1,
                "parts": {"repository-hygiene": {"run": True, "reason": "always runs"}, "suite-selection": {"run": True, "reason": "1 Linux shard"}},
                "units": {"a": {"run": True, "reason": "changed"}},
                "shards": [{"shard": 1, "estimate": 20.0}],
                "departures": [],
            },
            extra_jobs=[
                self._shard_job(
                    1,
                    started_at="2026-03-23T10:00:18Z",
                    entry_started_at="2026-03-23T10:00:30Z",
                    entry_completed_at="2026-03-23T10:00:50Z",
                    completed_at="2026-03-23T10:01:00Z",
                ),
                {
                    "name": "ci / repository-hygiene",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "2026-03-23T10:00:16Z",
                    "completed_at": "2026-03-23T10:01:50Z",
                },
            ],
        )
        # Run 5: unmatched step name -> left out and counted as unmatched.
        consumer.add_run(
            "2026-03-24T10:00:00Z",
            plan_completed_at="2026-03-24T10:00:15Z",
            verdict_completed_at="2026-03-24T10:01:30Z",
            plan={
                "version": 1,
                "parts": {"repository-hygiene": {"run": True, "reason": "always runs"}, "suite-selection": {"run": True, "reason": "1 Linux shard"}},
                "units": {"a": {"run": True, "reason": "changed"}},
                "shards": [{"shard": 1, "estimate": 20.0}],
                "departures": [],
            },
            extra_jobs=[
                {
                    "name": "ci / suite-selection-jobs / shard 1",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "2026-03-24T10:00:18Z",
                    "completed_at": "2026-03-24T10:01:15Z",
                    "steps": [{"name": "Some renamed step", "status": "completed", "conclusion": "success",
                               "started_at": "2026-03-24T10:00:30Z", "completed_at": "2026-03-24T10:01:00Z"}],
                },
            ],
        )
        # Run 6: empty language-step-only shard (entry step skipped) -> skipped, neither sample nor unmatched.
        consumer.add_run(
            "2026-03-25T10:00:00Z",
            plan_completed_at="2026-03-25T10:00:15Z",
            verdict_completed_at="2026-03-25T10:00:45Z",
            plan={
                "version": 1,
                "parts": {"repository-hygiene": {"run": True, "reason": "always runs"}, "suite-selection": {"run": True, "reason": "1 Linux shard"}},
                "units": {"a": {"run": False, "reason": "no read changed"}},
                "shards": [{"shard": 1, "estimate": 0.0}],
                "departures": [],
            },
            extra_jobs=[
                self._shard_job(
                    1,
                    started_at="2026-03-25T10:00:18Z",
                    entry_started_at="2026-03-25T10:00:25Z",
                    entry_completed_at="2026-03-25T10:00:25Z",
                    completed_at="2026-03-25T10:00:35Z",
                    entry_conclusion="skipped",
                    lang_step=("success", "2026-03-25T10:00:26Z", "2026-03-25T10:00:30Z"),
                ),
            ],
        )

        # Other consumer: fixed cost within assumed 90s (55s) -> no fixed-cost finding.
        other = self.stand_in.repository(OTHER)
        other.add_run(
            "2026-03-21T10:00:00Z",
            plan_completed_at="2026-03-21T10:00:15Z",
            verdict_completed_at="2026-03-21T10:01:30Z",
            plan={
                "version": 1,
                "parts": {"repository-hygiene": {"run": True, "reason": "always runs"}, "suite-selection": {"run": True, "reason": "1 Linux shard"}},
                "units": {"a": {"run": True, "reason": "changed"}},
                "shards": [{"shard": 1, "estimate": 30.0}],
                "departures": [],
            },
            extra_jobs=[
                self._shard_job(
                    1,
                    started_at="2026-03-21T10:00:18Z",
                    entry_started_at="2026-03-21T10:00:30Z",
                    entry_completed_at="2026-03-21T10:01:05Z",
                    completed_at="2026-03-21T10:01:20Z",
                    lang_step=("success", "2026-03-21T10:01:10Z", "2026-03-21T10:01:15Z"),
                ),
            ],
        )

        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, outcome.status, outcome.stderr)
        assert outcome.report is not None
        fixed_cost = outcome.report["repositories"][CONSUMER]["fixed_cost"]
        self.assertEqual(2, fixed_cost["runs"])
        self.assertEqual(2, fixed_cost["non_shard_decided_runs"])
        self.assertEqual(1, fixed_cost["unmatched_runs"])
        self.assertAlmostEqual(167.0, fixed_cost["p95_seconds"], places=1)
        self.assertAlmostEqual(89.0, fixed_cost["outside_entry_step_seconds"], places=1)
        self.assertAlmostEqual(78.0, fixed_cost["step_excess_seconds"], places=1)
        self.assertAlmostEqual(5.0, fixed_cost["language_step_p95_seconds"], places=1)
        # plan=19s (10:00:00..10:00:19), queue=6s (10:00:19..10:00:25), shard_to_entry=22s (10:00:25..10:00:47),
        # after_entry=15s (10:03:16..10:03:31), results=14s (10:03:31..10:03:45), verdict=13s (10:03:45..10:03:58).
        self.assertEqual(
            {"plan": 19.0, "queue": 6.0, "shard_to_entry": 22.0, "after_entry": 15.0, "results": 14.0, "verdict": 13.0},
            fixed_cost["phases"],
        )
        other_fixed_cost = outcome.report["repositories"][OTHER]["fixed_cost"]
        self.assertEqual(1, other_fixed_cost["runs"])
        self.assertAlmostEqual(55.0, other_fixed_cost["p95_seconds"], places=1)
        self.assertAlmostEqual(50.0, other_fixed_cost["outside_entry_step_seconds"], places=1)
        self.assertAlmostEqual(5.0, other_fixed_cost["step_excess_seconds"], places=1)
        self.assertAlmostEqual(5.0, other_fixed_cost["language_step_p95_seconds"], places=1)

        # Finding opened for CONSUMER fixed cost > 90s, not for OTHER.
        fixed_cost_issues = [issue for issue in self.management.issues if "fixed cost" in issue["title"].lower()]
        self.assertEqual(1, len(fixed_cost_issues), [i["title"] for i in self.management.issues])
        self.assertIn(CONSUMER, fixed_cost_issues[0]["title"])
        self.assertIn("167.0s", fixed_cost_issues[0]["body"])
        self.assertIn("89.0s", fixed_cost_issues[0]["body"])
        self.assertIn("78.0s", fixed_cost_issues[0]["body"])
        self.assertIn("language step p95: 5.0s", fixed_cost_issues[0]["body"].lower())
        self.assertIn("167.0s", outcome.summary)
        self.assertIn("language step p95 5.0s", outcome.summary)


if __name__ == "__main__":
    unittest.main()
