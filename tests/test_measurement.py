"""Tests of the measurement command against recorded GitHub API responses (Seam 6 of #171)."""

from __future__ import annotations

import unittest

from tests.measurement_fixtures import CONSUMER, MANAGEMENT, OTHER, MeasurementFixture


class MeasurementThreeMinuteTargetTests(MeasurementFixture):
    def test_reports_per_repository_share_within_3_minutes_over_rolling_four_weeks_excluding_manual_reruns_and_non_verdict_runs(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        # 2 runs within 3 minutes (120s and 180s), 1 run over 3 minutes (240s) -> 2/3 = 66.7%
        consumer.add_run("2026-03-20T10:00:00Z", verdict_completed_at="2026-03-20T10:02:00Z")
        consumer.add_run("2026-03-25T10:00:00Z", verdict_completed_at="2026-03-25T10:03:00Z")
        consumer.add_run("2026-03-30T10:00:00Z", verdict_completed_at="2026-03-30T10:04:00Z")
        # Excluded: older than 4 weeks (28 days before 2026-04-01T12:00:00Z is 2026-03-04T12:00:00Z)
        consumer.add_run("2026-03-01T10:00:00Z", verdict_completed_at="2026-03-01T10:10:00Z")
        # Excluded: manual re-run (run_attempt > 1)
        consumer.add_run("2026-03-26T10:00:00Z", verdict_completed_at="2026-03-26T10:10:00Z", run_attempt=2)
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

        self.assertEqual(3, by_repo[CONSUMER]["verdict_runs"])
        self.assertEqual(2, by_repo[CONSUMER]["within_3m_count"])
        self.assertAlmostEqual(2 / 3, by_repo[CONSUMER]["within_3m_share"], places=4)
        self.assertEqual(1, by_repo[OTHER]["verdict_runs"])
        self.assertEqual(1, by_repo[OTHER]["within_3m_count"])
        self.assertAlmostEqual(1.0, by_repo[OTHER]["within_3m_share"], places=4)
        self.assertIn(CONSUMER, outcome.summary)
        self.assertIn("66.7%", outcome.summary)
        self.assertIn(OTHER, outcome.summary)
        self.assertIn("100.0%", outcome.summary)


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


if __name__ == "__main__":
    unittest.main()
