"""Tests of the measurement command against recorded GitHub API responses (Seam 6 of #171)."""

from __future__ import annotations

import datetime
import json
import time
import unittest
from unittest import mock

from tests.measurement_fixtures import CONSUMER, MANAGEMENT, OTHER, MeasurementFixture, plan_zip


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
    def test_edit_runs_are_excluded_from_both_ci_goal_metrics_without_a_plan_artifact(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        no_tests_plan = {"version": 1, "parts": {"python-test": {"run": False}}}
        code_run = consumer.add_run(
            "2026-03-20T10:00:00Z", verdict_completed_at="2026-03-20T10:01:15Z", plan=no_tests_plan,
        )
        consumer.attempt_jobs[(code_run, 1)][0]["name"] = "ci / plan (PR #123)"
        edit_run = consumer.add_run(
            "2026-03-21T10:00:00Z", verdict_completed_at="2026-03-21T10:00:20Z",
        )
        consumer.attempt_jobs[(edit_run, 1)][0]["name"] = "ci / plan (edited pull request)"
        store = self.directory / "measurement.json"
        measured = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--store-out", str(store))
        reported = self.run_measurement("report", "--store", str(store), "--now", "2026-04-01T12:00:00Z")

        for outcome in (measured, reported):
            self.assertEqual(0, outcome.status, outcome.stderr)
            assert outcome.report is not None
            metrics = outcome.report["repositories"][CONSUMER]
            self.assertEqual(1, metrics["verdict_runs"])
            self.assertEqual(1, metrics["within_3m_count"])
            self.assertEqual(1, metrics["no_tests_runs"])
            self.assertEqual(75.0, metrics["no_tests_median_seconds"])

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

        open_issues = [issue for issue in self.management.issues if issue["state"] == "open"]
        within_3m = next(issue for issue in open_issues if "3 minutes" in issue["title"])
        no_tests = next(issue for issue in open_issues if "1 minute" in issue["title"])
        next_window = ("--own-repository", MANAGEMENT, "--now", "2026-04-20T12:00:00Z")
        empty = self.run_measurement("measure", *next_window)
        self.assertEqual(0, empty.status, empty.stderr)
        self.assertEqual("open", within_3m["state"])
        self.assertEqual("open", no_tests["state"])

        consumer.add_run("2026-04-18T10:00:00Z", verdict_completed_at="2026-04-18T10:02:59Z")
        only_verdict = self.run_measurement("measure", *next_window)
        self.assertEqual(0, only_verdict.status, only_verdict.stderr)
        self.assertEqual("closed", within_3m["state"])
        self.assertIn("99% of verdict runs", only_verdict.summary)
        self.assertEqual("open", no_tests["state"])

        consumer.add_run("2026-04-19T10:00:00Z", verdict_completed_at="2026-04-19T10:01:00Z", plan=self.NO_TESTS_PLAN)
        recovered = self.run_measurement("measure", *next_window)
        self.assertEqual(0, recovered.status, recovered.stderr)
        self.assertEqual("closed", no_tests["state"])
        self.assertIn("1-minute no-tests median target", recovered.summary)

    def test_failure_subcommand_opens_one_issue_while_open(self) -> None:
        first = self.run_measurement("failure", "--own-repository", MANAGEMENT, "--run-url", "https://github.example/runs/42")
        self.assertEqual(0, first.status, first.stderr)
        self.assertEqual(1, len(self.management.issues))
        self.assertIn("https://github.example/runs/42", self.management.issues[0]["body"])
        second = self.run_measurement("failure", "--own-repository", MANAGEMENT, "--run-url", "https://github.example/runs/43")
        self.assertEqual(0, second.status, second.stderr)
        self.assertEqual(1, len(self.management.issues))

    def test_only_the_success_command_closes_the_failure_finding(self) -> None:
        failed = self.run_measurement("failure", "--own-repository", MANAGEMENT, "--run-url", "https://github.example/runs/42")
        self.assertEqual(0, failed.status, failed.stderr)
        issue = self.management.issues[0]
        unrelated = self.management.issue("Sync failure", "<!-- sync-finding: failure -->")

        measured = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, measured.status, measured.stderr)
        self.assertEqual("open", issue["state"])

        issue_path = f"/repos/{MANAGEMENT}/issues/{issue['number']}"
        self.stand_in.on_request = lambda method, path, query: (
            self.stand_in._json(500, {"message": "Issue could not be closed"})
            if method == "PATCH" and path == issue_path else None
        )
        success_command = ("success", "--own-repository", MANAGEMENT, "--api-url", self.stand_in.url)
        failed_closure = self.run_measurement(*success_command)
        self.assertEqual(1, failed_closure.status, failed_closure.stderr)
        self.assertEqual("open", issue["state"])

        self.stand_in.on_request = None
        recovered = self.run_measurement(*success_command)
        self.assertEqual(0, recovered.status, recovered.stderr)
        self.assertEqual("closed", issue["state"])
        self.assertEqual("open", unrelated["state"])
        self.assertIn("measurement run succeeded", recovered.summary)
        comments = [write for write in self.stand_in.writes if write.path == f"{issue_path}/comments"]
        self.assertEqual(1, len(comments))

        writes = list(self.stand_in.writes)
        retry = self.run_measurement(*success_command)
        self.assertEqual(0, retry.status, retry.stderr)
        self.assertEqual(writes, self.stand_in.writes)

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
        issue = self.management.issues[0]
        self.assertEqual("open", issue["state"])

        consumer = self.stand_in.repositories[CONSUMER]
        consumer.has_workflow = True
        consumer.add_run("2026-03-31T10:00:00Z", verdict_completed_at="2026-03-31T10:00:35Z")
        partial = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--request-budget", "2")
        self.assertEqual(0, partial.status, partial.stderr)
        assert partial.report is not None
        self.assertFalse(partial.report["repositories"][CONSUMER]["coverage"]["complete"])
        self.assertEqual("open", issue["state"])

        workflow_path = f"/repos/{CONSUMER}/actions/workflows/ci.yml/runs"
        self.stand_in.refused[workflow_path] = 500
        errored = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(1, errored.status, errored.stderr)
        self.assertEqual("open", issue["state"])
        del self.stand_in.refused[workflow_path]

        consumer.archived = True
        skipped = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, skipped.status, skipped.stderr)
        self.assertEqual("open", issue["state"])
        consumer.archived = False

        recovered = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, recovered.status, recovered.stderr)
        self.assertEqual("closed", issue["state"])
        issue_path = f"/repos/{MANAGEMENT}/issues/{issue['number']}"
        comments = [write for write in self.stand_in.writes if write.path == f"{issue_path}/comments"]
        self.assertEqual(1, len(comments))
        self.assertIn("`ci.yml`", comments[0].body["body"])
        self.assertIn("exists again", comments[0].body["body"])

        writes = list(self.stand_in.writes)
        retry = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, retry.status, retry.stderr)
        self.assertEqual(writes, self.stand_in.writes)

        consumer.has_workflow = False
        recurred = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--now", "2026-04-06T12:00:00Z")
        self.assertEqual(0, recurred.status, recurred.stderr)
        self.assertEqual(writes, self.stand_in.writes)
        after_cooldown = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--now", "2026-04-09T12:00:00Z")
        self.assertEqual(0, after_cooldown.status, after_cooldown.stderr)
        self.assertEqual(2, len(self.management.issues))
        self.assertEqual("open", self.management.issues[-1]["state"])

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
        issue = self.management.issues[0]

        # The old runs have left the new window. Without any verdict there is no evidence of recovery.
        quiet = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--now", "2026-04-24T12:00:00Z")
        self.assertEqual(0, quiet.status, quiet.stderr)
        self.assertEqual("open", issue["state"])

        broken.add_run("2026-04-22T10:00:00Z", verdict_completed_at="2026-04-22T10:00:50Z")
        recovered = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--now", "2026-04-24T12:00:00Z")
        self.assertEqual(0, recovered.status, recovered.stderr)
        self.assertEqual("closed", issue["state"])
        self.assertIn("reached a verdict", recovered.summary)

    def test_only_a_complete_installation_listing_closes_findings_for_a_removed_repository(self) -> None:
        self.stand_in.repository(CONSUMER, has_workflow=False)
        initial = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, initial.status, initial.stderr)
        issue = self.management.issues[0]
        body = issue["body"]
        sync_issue = self.management.issue("Sync finding", f"<!-- sync-finding: request:{CONSUMER}:1 -->")
        unknown_issue = self.management.issue("Unknown kind", f"<!-- measurement-finding: miss:{CONSUMER}:unknown -->")
        pull_request = self.management.issue("A pull request quotes the marker", body)
        pull_request["pull_request"] = {"url": "https://github.example/pulls/1"}
        for authored_by_actions in (sync_issue, unknown_issue, pull_request):
            authored_by_actions["user"] = {"login": "github-actions[bot]"}
        human_issue = self.management.issue("A human issue quotes the marker", body)
        human_issue["user"] = {"login": "CrankyAnt"}
        unrelated = self.management.issue("Unrelated issue")
        outside_management = self.stand_in.repositories[CONSUMER].issue("A consumer issue quotes the marker", body)

        self.stand_in.installed.remove(CONSUMER)
        self.stand_in.installation_total_count = 1
        incomplete = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, incomplete.status, incomplete.stderr)
        self.assertEqual("open", issue["state"])

        self.stand_in.installation_total_count = None
        removed = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, removed.status, removed.stderr)
        self.assertEqual("closed", issue["state"])
        self.assertIn("no longer installed", removed.summary)
        for unaffected in (sync_issue, unknown_issue, pull_request, human_issue, unrelated, outside_management):
            self.assertEqual("open", unaffected["state"], unaffected["title"])

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
        self.assertIn("created=%3E%3D", run_requests[0])
        self.assertTrue(all("exclude_pull_requests=true" in path for path in run_requests), run_requests[:3])
        self.assertTrue(any("..2026-" in path and "%3A" in path for path in run_requests[1:]), run_requests[:3])


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
        code_run = consumer.add_run(
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
        consumer.attempt_jobs[(code_run, 1)][0]["name"] = "ci / plan (PR #123)"
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
        self.assertAlmostEqual(15.0, fixed_cost["language_step_allowance_seconds"], places=1)
        self.assertAlmostEqual(15.0, other_fixed_cost["language_step_allowance_seconds"], places=1)

        # Finding opened for CONSUMER fixed cost > 90s, not for OTHER, and no language-step finding (5s <= 15s).
        fixed_cost_issues = [issue for issue in self.management.issues if "fixed cost" in issue["title"].lower()]
        self.assertEqual(1, len(fixed_cost_issues), [i["title"] for i in self.management.issues])
        self.assertIn(CONSUMER, fixed_cost_issues[0]["title"])
        self.assertIn("167.0s", fixed_cost_issues[0]["body"])
        self.assertIn("89.0s", fixed_cost_issues[0]["body"])
        self.assertIn("78.0s", fixed_cost_issues[0]["body"])
        self.assertIn("language step p95: 5.0s (allowance 15s)", fixed_cost_issues[0]["body"].lower())
        self.assertIn("167.0s", outcome.summary)
        self.assertIn("language step p95 5.0s (allowance 15s)", outcome.summary)
        self.assertEqual([], [issue for issue in self.management.issues if "language step" in issue["title"].lower()])

    def test_language_step_p95_above_allowance_opens_and_deduplicates_finding(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        # Fixed cost stays within 90s (50s outside + 5s excess = 55s), while Python language step takes 22s (> 15s allowance).
        consumer.add_run(
            "2026-03-21T10:00:00Z",
            plan_completed_at="2026-03-21T10:00:15Z",
            verdict_completed_at="2026-03-21T10:01:47Z",
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
                    completed_at="2026-03-21T10:01:37Z",
                    lang_step=("success", "2026-03-21T10:01:10Z", "2026-03-21T10:01:32Z"),
                ),
            ],
        )

        first = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, first.status, first.stderr)
        assert first.report is not None
        fixed_cost = first.report["repositories"][CONSUMER]["fixed_cost"]
        self.assertAlmostEqual(55.0, fixed_cost["p95_seconds"], places=1)
        self.assertAlmostEqual(22.0, fixed_cost["language_step_p95_seconds"], places=1)
        self.assertAlmostEqual(15.0, fixed_cost["language_step_allowance_seconds"], places=1)
        self.assertIn("language step p95 22.0s (allowance 15s)", first.summary)
        lang_issues = [issue for issue in self.management.issues if "language step" in issue["title"].lower()]
        self.assertEqual(1, len(lang_issues), [i["title"] for i in self.management.issues])
        self.assertIn("allowance 15s", lang_issues[0]["title"])
        self.assertIn("22.0s", lang_issues[0]["body"])
        self.assertIn(f"<!-- measurement-finding: miss:{CONSUMER}:language-step -->", lang_issues[0]["body"])
        self.assertEqual([], [issue for issue in self.management.issues if "fixed cost" in issue["title"].lower()])

        second = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, second.status, second.stderr)
        self.assertEqual(1, len([issue for issue in self.management.issues if "language step" in issue["title"].lower()]))

        fixed_issue = self.management.issue("Earlier fixed-cost finding", f"<!-- measurement-finding: miss:{CONSUMER}:fixed-cost -->")
        fixed_issue["user"] = {"login": "github-actions[bot]"}
        next_window = ("--own-repository", MANAGEMENT, "--now", "2026-04-20T12:00:00Z")
        consumer.add_run("2026-04-18T10:00:00Z", verdict_completed_at="2026-04-18T10:01:00Z")
        no_samples = self.run_measurement("measure", *next_window)
        self.assertEqual(0, no_samples.status, no_samples.stderr)
        self.assertEqual("open", lang_issues[0]["state"])
        self.assertEqual("open", fixed_issue["state"])

        consumer.add_run(
            "2026-04-19T10:00:00Z",
            plan_completed_at="2026-04-19T10:00:15Z",
            verdict_completed_at="2026-04-19T10:01:30Z",
            plan={"version": 1, "units": {"a": {"run": True}}, "shards": [{"shard": 1, "estimate": 30.0}]},
            extra_jobs=[self._shard_job(
                1,
                started_at="2026-04-19T10:00:18Z",
                entry_started_at="2026-04-19T10:00:30Z",
                entry_completed_at="2026-04-19T10:01:05Z",
                completed_at="2026-04-19T10:01:20Z",
                lang_step=("success", "2026-04-19T10:01:10Z", "2026-04-19T10:01:15Z"),
            )],
        )
        recovered = self.run_measurement("measure", *next_window)
        self.assertEqual(0, recovered.status, recovered.stderr)
        self.assertEqual("closed", lang_issues[0]["state"])
        self.assertEqual("closed", fixed_issue["state"])
        self.assertEqual(2, len(self.management.issues))
        self.assertIn("within its allowance again", recovered.summary)
        self.assertIn("within the assumed fixed cost again", recovered.summary)


class MeasurementIncrementalStoreTests(MeasurementFixture):
    NO_TESTS_PLAN = {
        "version": 1,
        "parts": {
            "repository-hygiene": {"run": True, "reason": "always runs"},
            "python-test": {"run": False, "reason": "no changed path matches its paths"},
        },
        "departures": [],
    }

    def test_overlap_upserts_without_double_counting_and_skips_fetching_frozen_runs(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        run_a = consumer.add_run("2026-03-30T10:00:00Z", verdict_completed_at="2026-03-30T10:01:00Z", plan=self.NO_TESTS_PLAN)
        run_cancelled = consumer.add_run(
            "2026-03-30T11:00:00Z",
            verdict_completed_at=None,
            conclusion="cancelled",
            verdict_job_name=None,
            updated_at="2026-03-30T11:02:00Z",
        )
        store_path = self.directory / "store.json"

        first = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--store-out", str(store_path))
        self.assertEqual(0, first.status, first.stderr)
        self.assertTrue(store_path.is_file())
        assert first.report is not None
        # Build-up is reported as store/coverage status, never counted as a fallback.
        self.assertEqual({}, first.report["repositories"][CONSUMER]["fallbacks"])
        self.assertEqual(1, first.report["repositories"][CONSUMER]["verdict_runs"])

        # Add one new run on the next day; run_a and run_cancelled fall inside the 1-day listing overlap.
        run_b = consumer.add_run("2026-03-31T15:00:00Z", verdict_completed_at="2026-03-31T15:00:40Z", plan=self.NO_TESTS_PLAN)
        self.stand_in.requests.clear()

        second = self.run_measurement(
            "measure",
            "--own-repository",
            MANAGEMENT,
            "--store-in",
            str(store_path),
            "--store-out",
            str(store_path),
            "--now",
            "2026-04-01T12:00:00Z",
        )
        self.assertEqual(0, second.status, second.stderr)
        assert second.report is not None
        metrics = second.report["repositories"][CONSUMER]
        self.assertEqual(2, metrics["verdict_runs"])
        self.assertEqual(0, metrics["non_verdict_runs"])
        # Neither frozen run_a nor excluded-cancelled run_cancelled should have its jobs fetched again.
        fetched_paths = [path for method, path, _ in self.stand_in.requests if method == "GET"]
        self.assertFalse(any(f"/runs/{run_a}/" in path for path in fetched_paths), fetched_paths)
        self.assertFalse(any(f"/runs/{run_cancelled}/" in path for path in fetched_paths), fetched_paths)
        self.assertTrue(any(f"/runs/{run_b}/attempts/1/jobs" in path for path in fetched_paths), fetched_paths)

    def test_unfinished_record_becomes_finished_on_next_run_including_outside_overlap(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        # Created 5 days before first run, still in_progress during run 1.
        run_id = consumer.add_run(
            "2026-03-25T10:00:00Z",
            status="in_progress",
            conclusion=None,
            verdict_status="in_progress",
            verdict_conclusion=None,
            verdict_completed_at=None,
            plan=self.NO_TESTS_PLAN,
        )
        store_path = self.directory / "store.json"

        first = self.run_measurement(
            "measure",
            "--own-repository",
            MANAGEMENT,
            "--store-out",
            str(store_path),
            "--now",
            "2026-03-31T12:00:00Z",
        )
        self.assertEqual(0, first.status, first.stderr)
        assert first.report is not None
        self.assertEqual(0, first.report["repositories"][CONSUMER]["verdict_runs"])

        # Complete the run before day 2; it is outside day 2's 1-day listing overlap (2026-03-25 vs 2026-03-30).
        consumer.runs[0]["status"] = "completed"
        consumer.runs[0]["conclusion"] = "success"
        consumer.runs[0]["updated_at"] = "2026-03-31T13:00:00Z"
        consumer.attempts[(run_id, 1)]["status"] = "completed"
        consumer.attempts[(run_id, 1)]["conclusion"] = "success"
        consumer.attempt_jobs[(run_id, 1)] = consumer._build_jobs(
            run_id,
            "2026-03-25T10:00:00Z",
            plan_completed_at="2026-03-25T10:00:15Z",
            verdict_completed_at="2026-03-25T10:01:30Z",
            verdict_status="completed",
            verdict_conclusion="success",
            verdict_job_name="ci / verdict",
            extra_jobs=None,
        )

        second = self.run_measurement(
            "measure",
            "--own-repository",
            MANAGEMENT,
            "--store-in",
            str(store_path),
            "--store-out",
            str(store_path),
            "--now",
            "2026-04-01T12:00:00Z",
        )
        self.assertEqual(0, second.status, second.stderr)
        assert second.report is not None
        self.assertEqual(1, second.report["repositories"][CONSUMER]["verdict_runs"])
        self.assertEqual(1, second.report["repositories"][CONSUMER]["within_3m_count"])

    def test_record_within_settle_time_stays_unfinished_until_settled(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        # Verdict completed 10 minutes before NOW (within 30-minute settle time).
        consumer.add_run(
            "2026-04-01T11:48:00Z",
            verdict_completed_at="2026-04-01T11:50:00Z",
            plan=self.NO_TESTS_PLAN,
            plan_created_at="2026-04-01T11:48:20Z",
        )
        store_path = self.directory / "store.json"

        first = self.run_measurement(
            "measure",
            "--own-repository",
            MANAGEMENT,
            "--store-out",
            str(store_path),
            "--now",
            "2026-04-01T12:00:00Z",
        )
        self.assertEqual(0, first.status, first.stderr)
        assert first.report is not None
        self.assertEqual(0, first.report["repositories"][CONSUMER]["verdict_runs"])
        saved = json.loads(store_path.read_text(encoding="utf-8"))
        rec = next(iter(saved["repositories"][CONSUMER]["records"].values()))
        self.assertEqual("unfinished", rec["state"])

        # 35 minutes after completion -> past 30-minute settle time -> freezes as verdict.
        second = self.run_measurement(
            "measure",
            "--own-repository",
            MANAGEMENT,
            "--store-in",
            str(store_path),
            "--store-out",
            str(store_path),
            "--now",
            "2026-04-01T12:25:00Z",
        )
        self.assertEqual(0, second.status, second.stderr)
        assert second.report is not None
        self.assertEqual(1, second.report["repositories"][CONSUMER]["verdict_runs"])
        saved2 = json.loads(store_path.read_text(encoding="utf-8"))
        rec2 = next(iter(saved2["repositories"][CONSUMER]["records"].values()))
        self.assertEqual("verdict", rec2["state"])

    def test_rerun_started_between_listing_and_jobs_still_judges_attempt_1_and_picks_attempt_1_plan(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        attempt_1_plan = {
            "version": 1,
            "parts": {"repository-hygiene": {"run": True, "reason": "always runs"}, "python-test": {"run": False, "reason": "none"}},
            "departures": [],
        }
        attempt_2_plan = {
            "version": 1,
            "parts": {"repository-hygiene": {"run": True, "reason": "always runs"}, "python-test": {"run": True, "reason": "rerun"}},
            "fallbacks": {"rerun-fallback": 1},
            "departures": [],
        }
        run_id = consumer.add_run(
            "2026-03-20T10:00:00Z",
            verdict_completed_at="2026-03-20T10:01:00Z",
            plan=attempt_1_plan,
            plan_created_at="2026-03-20T10:00:15Z",
        )
        mutated = False

        def trigger_rerun_after_listing(method: str, path: str, query: dict[str, str]):
            nonlocal mutated
            # Mutate the run right before jobs/artifacts are requested, simulating a re-run starting after listing returned run_attempt=1.
            if not mutated and f"/runs/{run_id}/" in path:
                mutated = True
                consumer.runs[0]["run_attempt"] = 2
                consumer.runs[0]["status"] = "in_progress"
                consumer.jobs[run_id] = consumer._build_jobs(
                    run_id,
                    "2026-03-20T10:00:00Z",
                    plan_completed_at="2026-03-20T10:05:10Z",
                    verdict_completed_at=None,
                    verdict_status="in_progress",
                    verdict_conclusion=None,
                    verdict_job_name="ci / verdict",
                    extra_jobs=None,
                    id_offset=200,
                )
                # Prepend attempt 2's ci-plan (newest first, as GitHub returns artifacts) AFTER attempt 1's verdict.
                art_id = consumer._next_artifact
                consumer._next_artifact += 1
                consumer.artifacts[run_id].insert(0, {
                    "id": art_id,
                    "name": "ci-plan",
                    "expired": False,
                    "created_at": "2026-03-20T10:05:15Z",
                    "archive_download_url": f"/repos/{CONSUMER}/actions/artifacts/{art_id}/zip",
                    "workflow_run": {"id": run_id, "head_branch": "main"},
                })
                consumer.artifact_zips[art_id] = plan_zip(attempt_2_plan)
            return None

        self.stand_in.on_request = trigger_rerun_after_listing
        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, outcome.status, outcome.stderr)
        assert outcome.report is not None
        metrics = outcome.report["repositories"][CONSUMER]
        self.assertEqual(1, metrics["verdict_runs"])
        self.assertEqual(1, metrics["no_tests_runs"])
        self.assertEqual({}, metrics["fallbacks"])

    def test_records_outside_90_days_are_dropped_and_removed_repo_stays_in_store(self) -> None:
        self.stand_in.repository(CONSUMER)
        store_in = self.directory / "in.json"
        store_out = self.directory / "out.json"
        removed_repo = "example-owner/removed-consumer"
        seed = {
            "schema_version": 1,
            "judge_version": 1,
            "retention_days": 90,
            "generated_at": "2026-03-31T12:00:00Z",
            "repositories": {
                CONSUMER: {
                    "has_workflow": True,
                    "listed_from": "2025-12-01T00:00:00Z",
                    "listed_to": "2026-03-31T12:00:00Z",
                    "records": {
                        "1": {
                            "run_id": 1,
                            "created_at": "2025-12-20T10:00:00Z",
                            "run_attempt": 1,
                            "status": "completed",
                            "state": "verdict",
                            "verdict_completed_at": "2025-12-20T10:01:00Z",
                            "duration_seconds": 60.0,
                            "plan_found": False,
                            "selects_no_tests": None,
                            "fallbacks": {},
                            "unknown_input_parts": [],
                            "fixed_cost_outcome": "skip",
                            "fixed_cost_sample": None,
                            "judged_at": "2026-03-31T12:00:00Z",
                            "judge_version": 1,
                        },
                        "2": {
                            "run_id": 2,
                            "created_at": "2026-01-15T10:00:00Z",
                            "run_attempt": 1,
                            "status": "completed",
                            "state": "verdict",
                            "verdict_completed_at": "2026-01-15T10:01:00Z",
                            "duration_seconds": 60.0,
                            "plan_found": False,
                            "selects_no_tests": None,
                            "fallbacks": {},
                            "unknown_input_parts": [],
                            "fixed_cost_outcome": "skip",
                            "fixed_cost_sample": None,
                            "judged_at": "2026-03-31T12:00:00Z",
                            "judge_version": 1,
                        },
                    },
                },
                removed_repo: {
                    "has_workflow": True,
                    "listed_from": "2026-02-01T00:00:00Z",
                    "listed_to": "2026-03-31T12:00:00Z",
                    "records": {
                        "9": {
                            "run_id": 9,
                            "created_at": "2026-03-20T10:00:00Z",
                            "run_attempt": 1,
                            "status": "completed",
                            "state": "verdict",
                            "verdict_completed_at": "2026-03-20T10:01:00Z",
                            "duration_seconds": 60.0,
                            "plan_found": False,
                            "selects_no_tests": None,
                            "fallbacks": {},
                            "unknown_input_parts": [],
                            "fixed_cost_outcome": "skip",
                            "fixed_cost_sample": None,
                            "judged_at": "2026-03-31T12:00:00Z",
                            "judge_version": 1,
                        },
                    },
                },
            },
        }
        store_in.write_text(json.dumps(seed), encoding="utf-8")

        outcome = self.run_measurement(
            "measure",
            "--own-repository",
            MANAGEMENT,
            "--store-in",
            str(store_in),
            "--store-out",
            str(store_out),
            "--now",
            "2026-04-01T12:00:00Z",
        )
        self.assertEqual(0, outcome.status, outcome.stderr)
        assert outcome.report is not None
        # Removed repo is not in measure's report, only current installation repos.
        self.assertNotIn(removed_repo, outcome.report["repositories"])
        saved = json.loads(store_out.read_text(encoding="utf-8"))
        # Record 1 (102 days old) dropped; record 2 (76 days old) kept.
        self.assertNotIn("1", saved["repositories"][CONSUMER]["records"])
        self.assertIn("2", saved["repositories"][CONSUMER]["records"])
        # Removed repo stays in store for offline `report`.
        self.assertIn(removed_repo, saved["repositories"])
        self.assertIn("9", saved["repositories"][removed_repo]["records"])


class MeasurementStoreDiscoveryAndMigrationTests(MeasurementFixture):
    def test_rejudges_recent_stored_edit_runs_using_the_old_plan_artifact_or_job_steps(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        no_tests_plan = {"version": 1, "parts": {"python-test": {"run": False}}}
        edited_plan = {**no_tests_plan, "head-results": {"run_id": 123, "passed": True}}
        consumer.add_run(
            "2026-03-20T10:00:00Z", verdict_completed_at="2026-03-20T10:01:15Z", plan=no_tests_plan,
        )
        consumer.add_run(
            "2026-03-21T10:00:00Z", verdict_completed_at="2026-03-21T10:00:20Z", plan=edited_plan,
        )
        expired_edit_run = consumer.add_run(
            "2026-03-22T10:00:00Z", verdict_completed_at="2026-03-22T10:00:30Z", plan=edited_plan, plan_expired=True,
        )
        consumer.attempt_jobs[(expired_edit_run, 1)][0]["steps"] = [
            {"name": "Record the checked-out commit and tree", "conclusion": "skipped"},
        ]
        records = {
            str(run["id"]): {
                "run_id": run["id"],
                "created_at": run["created_at"],
                "run_attempt": 1,
                "status": "completed",
                "state": "verdict",
                "verdict_completed_at": run["updated_at"],
                "duration_seconds": duration,
                "plan_found": True,
                "selects_no_tests": True,
                "fixed_cost_outcome": "skip",
                "judge_version": 1,
            }
            for run, duration in zip(consumer.runs, (75, 20, 30), strict=True)
        }
        self.management.add_store_artifact(
            {
                "schema_version": 1,
                "judge_version": 1,
                "repositories": {
                    CONSUMER: {
                        "listed_from": "2026-03-04T12:00:00Z",
                        "listed_to": "2026-03-31T12:00:00Z",
                        "records": records,
                    },
                },
            },
            created_at="2026-03-31T12:00:00Z",
        )
        store = self.directory / "measurement.json"
        measured = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--store-out", str(store))
        reported = self.run_measurement("report", "--store", str(store), "--now", "2026-04-01T12:00:00Z")
        for outcome in (measured, reported):
            self.assertEqual(0, outcome.status, outcome.stderr)
            assert outcome.report is not None
            metrics = outcome.report["repositories"][CONSUMER]
            self.assertEqual(1, metrics["verdict_runs"])
            self.assertEqual(1, metrics["no_tests_runs"])
            self.assertEqual(75.0, metrics["no_tests_median_seconds"])
            self.assertTrue(metrics["coverage"]["complete"])

    def test_store_listing_failure_ends_without_writing_store(self) -> None:
        self.stand_in.repository(CONSUMER).add_run("2026-03-20T10:00:00Z", verdict_completed_at="2026-03-20T10:01:00Z")
        self.stand_in.refused[f"/repos/{MANAGEMENT}/actions/artifacts"] = 502
        store_out = self.directory / "store-out.json"

        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--store-out", str(store_out))
        self.assertEqual(1, outcome.status)
        self.assertFalse(store_out.exists(), "a store listing error must not write or upload a replacement store")
        self.assertEqual([], [p for _, p, _ in self.stand_in.requests if f"/repos/{CONSUMER}/" in p])

    def test_invalid_newest_store_falls_back_to_next_valid_ignores_non_main_and_reports_age(self) -> None:
        self.stand_in.repository(CONSUMER)
        valid_store = {
            "schema_version": 1,
            "judge_version": 1,
            "retention_days": 90,
            "generated_at": "2026-03-31T12:00:00Z",
            "repositories": {
                CONSUMER: {
                    "has_workflow": True,
                    "listed_from": "2026-02-01T12:00:00Z",
                    "listed_to": "2026-03-31T12:00:00Z",
                    "records": {
                        "77": {
                            "run_id": 77,
                            "created_at": "2026-03-10T10:00:00Z",
                            "run_attempt": 1,
                            "status": "completed",
                            "state": "verdict",
                            "verdict_completed_at": "2026-03-10T10:01:00Z",
                            "duration_seconds": 60.0,
                            "plan_found": False,
                            "selects_no_tests": None,
                            "fallbacks": {},
                            "unknown_input_parts": [],
                            "fixed_cost_outcome": "skip",
                            "fixed_cost_sample": None,
                            "judged_at": "2026-03-31T12:00:00Z",
                            "judge_version": 1,
                        },
                    },
                },
            },
        }
        # Newer artifact on a non-main branch must be ignored.
        self.management.add_store_artifact(valid_store, created_at="2026-04-01T11:00:00Z", head_branch="feat/other")
        # Valid older artifact on main (24h old).
        valid_id = self.management.add_store_artifact(valid_store, created_at="2026-03-31T12:00:00Z", head_branch="main")
        # Newest artifact on main is corrupt (bad zip bytes).
        bad_id = self.management.add_store_artifact(b"not-a-zip", created_at="2026-04-01T06:00:00Z", head_branch="main", raw_bytes=True)

        store_out = self.directory / "store-out.json"
        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--store-out", str(store_out))
        self.assertEqual(0, outcome.status, outcome.stderr)
        assert outcome.report is not None
        self.assertEqual(valid_id, outcome.report["store"]["artifact_id"])
        self.assertEqual([bad_id], outcome.report["store"]["skipped_invalid_ids"])
        self.assertAlmostEqual(86400.0, outcome.report["store"]["age_seconds"], places=1)
        self.assertIn(f"artifact #{valid_id}", outcome.summary)
        self.assertIn("24.0h", outcome.summary)
        self.assertIn(f"#{bad_id}", outcome.summary)
        # Historical record 77 survived.
        saved = json.loads(store_out.read_text(encoding="utf-8"))
        self.assertIn("77", saved["repositories"][CONSUMER]["records"])
        self.assertEqual(1, outcome.report["repositories"][CONSUMER]["verdict_runs"])

    def test_all_unreadable_store_artifacts_fail_without_writing_store(self) -> None:
        self.stand_in.repository(CONSUMER)
        self.management.add_store_artifact("{bad json", created_at="2026-03-31T12:00:00Z", head_branch="main")
        self.management.add_store_artifact(b"corrupt", created_at="2026-04-01T06:00:00Z", head_branch="main", raw_bytes=True)
        store_out = self.directory / "store-out.json"

        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--store-out", str(store_out))
        self.assertEqual(1, outcome.status)
        self.assertFalse(store_out.exists(), "an unreadable store must never overwrite history with a build-up")
        self.assertIn("unreadable", outcome.stderr)

    def test_schema_migration_and_judge_version_change_rejudges_within_30_days_and_keeps_older(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        # Recent run (within 30 days) has real attempt-1 data with 50s duration and no-tests plan.
        no_tests_plan = {
            "version": 1,
            "parts": {"repository-hygiene": {"run": True, "reason": "always runs"}, "python-test": {"run": False, "reason": "none"}},
            "departures": [],
        }
        recent_id = consumer.add_run(
            "2026-03-20T10:00:00Z",
            verdict_completed_at="2026-03-20T10:00:50Z",
            plan=no_tests_plan,
        )
        # Seed a legacy schema_version=0 store with both the recent run (judge_version=0, stale value 999s)
        # and an older run (45 days old, judge_version=0, beyond 30-day ci-plan retention).
        legacy_store = {
            "schema_version": 0,
            "judge_version": 0,
            "generated_at": "2026-03-31T12:00:00Z",
            "repositories": {
                CONSUMER: {
                    "has_workflow": True,
                    "listed_from": "2026-02-01T12:00:00Z",
                    "listed_to": "2026-03-31T12:00:00Z",
                    "records": {
                        str(recent_id): {
                            "run_id": recent_id,
                            "created_at": "2026-03-20T10:00:00Z",
                            "run_attempt": 1,
                            "status": "completed",
                            "state": "verdict",
                            "verdict_completed_at": "2026-03-20T10:16:39Z",
                            "duration_seconds": 999.0,
                            "extra_legacy_field": "dropped",
                            "judge_version": 0,
                        },
                        "500": {
                            "run_id": 500,
                            "created_at": "2026-02-15T10:00:00Z",
                            "run_attempt": 1,
                            "status": "completed",
                            "state": "verdict",
                            "verdict_completed_at": "2026-02-15T10:01:00Z",
                            "duration_seconds": 60.0,
                            "judge_version": 0,
                        },
                    },
                },
            },
        }
        self.management.add_store_artifact(legacy_store, created_at="2026-03-31T12:00:00Z", head_branch="main")
        store_out = self.directory / "migrated.json"

        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--store-out", str(store_out))
        self.assertEqual(0, outcome.status, outcome.stderr)
        assert outcome.report is not None
        self.assertEqual(0, outcome.report["store"]["migrated_from"])
        self.assertIn("migrated from schema v0", outcome.summary)
        # Recent run was re-judged to 50s (within 3m, no-tests) with the current judge version.
        self.assertEqual(1, outcome.report["repositories"][CONSUMER]["within_3m_count"])
        self.assertEqual(1, outcome.report["repositories"][CONSUMER]["no_tests_runs"])
        saved = json.loads(store_out.read_text(encoding="utf-8"))
        self.assertEqual(1, saved["schema_version"])
        self.assertEqual(2, saved["repositories"][CONSUMER]["records"][str(recent_id)]["judge_version"])
        self.assertNotIn("extra_legacy_field", saved["repositories"][CONSUMER]["records"][str(recent_id)])
        # Older run (45 days old) was NOT re-fetched and kept judge_version=0.
        self.assertEqual(0, saved["repositories"][CONSUMER]["records"]["500"]["judge_version"])
        self.assertFalse(any("/runs/500/" in path for _, path, _ in self.stand_in.requests))


class MeasurementBudgetsRateLimitsAndCapSplitTests(MeasurementFixture):
    def test_request_budget_and_ratelimit_reserve_stop_fetching_save_consistent_store_and_resume(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        for day in range(10, 16):
            consumer.add_run(f"2026-03-{day:02d}T10:00:00Z", verdict_completed_at=f"2026-03-{day:02d}T10:01:00Z")
        store_path = self.directory / "budget-store.json"

        # 1. Request budget allows listing + a subset of runs, leaving remaining runs pending, exiting 0.
        first = self.run_measurement(
            "measure",
            "--own-repository",
            MANAGEMENT,
            "--store-out",
            str(store_path),
            "--workers",
            "1",
            "--request-budget",
            "4",
        )
        self.assertEqual(0, first.status, first.stderr)
        self.assertTrue(store_path.is_file())
        assert first.report is not None
        self.assertIsNotNone(first.report["budget_stop_reason"])
        self.assertIn("request budget", first.report["budget_stop_reason"])
        self.assertFalse(first.report["repositories"][CONSUMER]["coverage"]["complete"])
        self.assertGreater(first.report["repositories"][CONSUMER]["coverage"]["pending_runs"], 0)
        saved = json.loads(store_path.read_text(encoding="utf-8"))
        states = [r["state"] for r in saved["repositories"][CONSUMER]["records"].values()]
        self.assertIn("pending", states)
        self.assertIn("verdict", states)

        # 2. Rate-limit reserve stops fetching when x-ratelimit-remaining < reserve, exits 0.
        self.stand_in.ratelimit_remaining = 400
        second = self.run_measurement(
            "measure",
            "--own-repository",
            MANAGEMENT,
            "--store-in",
            str(store_path),
            "--store-out",
            str(store_path),
            "--workers",
            "1",
            "--ratelimit-reserve",
            "500",
        )
        self.assertEqual(0, second.status, second.stderr)
        assert second.report is not None
        self.assertIn("rate-limit reserve", second.report["budget_stop_reason"] or "")

        # 3. Normal budget finishes the remaining pending records.
        self.stand_in.ratelimit_remaining = 4500
        third = self.run_measurement(
            "measure",
            "--own-repository",
            MANAGEMENT,
            "--store-in",
            str(store_path),
            "--store-out",
            str(store_path),
        )
        self.assertEqual(0, third.status, third.stderr)
        assert third.report is not None
        self.assertTrue(third.report["repositories"][CONSUMER]["coverage"]["complete"])
        self.assertEqual(6, third.report["repositories"][CONSUMER]["verdict_runs"])

    def test_time_budget_stops_fetching_and_saves_consistent_store_with_pending_records(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        for day in range(10, 16):
            consumer.add_run(f"2026-03-{day:02d}T10:00:00Z", verdict_completed_at=f"2026-03-{day:02d}T10:01:00Z")
        store_path = self.directory / "time-budget.json"

        def slow_jobs(method: str, path: str, query: dict[str, str]):
            if "/attempts/1/jobs" in path:
                time.sleep(0.06)
            return None

        self.stand_in.on_request = slow_jobs
        outcome = self.run_measurement(
            "measure",
            "--own-repository",
            MANAGEMENT,
            "--store-out",
            str(store_path),
            "--workers",
            "1",
            "--time-budget",
            "0.15",
        )
        self.assertEqual(0, outcome.status, outcome.stderr)
        self.assertTrue(store_path.is_file())
        assert outcome.report is not None
        self.assertIn("time budget", outcome.report["budget_stop_reason"] or "")
        self.assertFalse(outcome.report["repositories"][CONSUMER]["coverage"]["complete"])
        self.assertGreater(outcome.report["repositories"][CONSUMER]["coverage"]["pending_runs"], 0)

    def test_secondary_limit_403_with_retry_after_backs_off_saves_store_and_fails(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        run_1 = consumer.add_run("2026-03-20T10:00:00Z", verdict_completed_at="2026-03-20T10:01:00Z")
        consumer.add_run("2026-03-25T10:00:00Z", verdict_completed_at="2026-03-25T10:01:00Z")
        store_path = self.directory / "secondary-limit.json"

        def hit_secondary(method: str, path: str, query: dict[str, str]):
            if f"/runs/{run_1}/attempts/1/jobs" in path:
                return self.stand_in._json(
                    403,
                    {"message": "You have exceeded a secondary rate limit."},
                    headers={"Retry-After": "12", "X-RateLimit-Remaining": "4000"},
                )
            return None

        self.stand_in.on_request = hit_secondary
        sleeps: list[float] = []
        with mock.patch("time.sleep", side_effect=lambda secs: sleeps.append(secs)):
            outcome = self.run_measurement(
                "measure",
                "--own-repository",
                MANAGEMENT,
                "--store-out",
                str(store_path),
                "--workers",
                "1",
                "--min-secondary-wait",
                "60",
            )
        self.assertEqual(1, outcome.status)
        self.assertTrue(store_path.is_file(), "store must still be saved after secondary rate limit")
        # Secondary rate limit with Retry-After waits for the full Retry-After value (12s) when within deadline.
        self.assertEqual([12.0], sleeps, "secondary rate limit must back off once for the response Retry-After")
        assert outcome.report is not None
        # run_2 (newer) finished and was kept; run_1 stayed pending.
        self.assertEqual(1, outcome.report["repositories"][CONSUMER]["verdict_runs"])
        self.assertEqual(1, outcome.report["repositories"][CONSUMER]["coverage"]["pending_runs"])
        self.assertIn("secondary rate limit", outcome.stderr)

    def test_primary_limit_403_stops_saves_store_and_fails(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        run_1 = consumer.add_run("2026-03-20T10:00:00Z", verdict_completed_at="2026-03-20T10:01:00Z")
        consumer.add_run("2026-03-25T10:00:00Z", verdict_completed_at="2026-03-25T10:01:00Z")
        store_path = self.directory / "primary-limit.json"

        def hit_primary(method: str, path: str, query: dict[str, str]):
            if f"/runs/{run_1}/attempts/1/jobs" in path:
                return self.stand_in._json(
                    403,
                    {"message": "API rate limit exceeded"},
                    headers={"X-RateLimit-Remaining": "0"},
                )
            return None

        self.stand_in.on_request = hit_primary
        outcome = self.run_measurement(
            "measure",
            "--own-repository",
            MANAGEMENT,
            "--store-out",
            str(store_path),
            "--workers",
            "1",
        )
        self.assertEqual(1, outcome.status)
        self.assertTrue(store_path.is_file())
        assert outcome.report is not None
        self.assertEqual(1, outcome.report["repositories"][CONSUMER]["verdict_runs"])
        self.assertEqual(1, outcome.report["repositories"][CONSUMER]["coverage"]["pending_runs"])

    def test_one_repository_error_keeps_other_repositories_in_store_without_advancing_failed_repo(self) -> None:
        self.stand_in.repository(CONSUMER)
        self.stand_in.refused[f"/repos/{CONSUMER}/actions/workflows/ci.yml/runs"] = 500
        other = self.stand_in.repository(OTHER)
        other.add_run("2026-03-25T10:00:00Z", verdict_completed_at="2026-03-25T10:01:00Z")
        store_path = self.directory / "partial-error.json"

        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--store-out", str(store_path))
        self.assertEqual(1, outcome.status)
        self.assertTrue(store_path.is_file())
        saved = json.loads(store_path.read_text(encoding="utf-8"))
        self.assertNotIn(CONSUMER, saved["repositories"])
        self.assertIn(OTHER, saved["repositories"])
        self.assertEqual(1, len(saved["repositories"][OTHER]["records"]))

    def test_cap_split_at_1000_lists_all_spread_out_runs_across_window(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        # Use a lower cap on the stand-in to exercise recursive halving without creating 1000+ fixtures,
        # and also verify real CREATED_SEARCH_CAP=1000 via spread-out runs.
        start = datetime.datetime(2026, 3, 5, 0, 0, tzinfo=datetime.timezone.utc)
        for index in range(1005):
            ts = start + datetime.timedelta(minutes=index * 30)
            vc = ts + datetime.timedelta(seconds=45)
            consumer.add_run(ts.strftime("%Y-%m-%dT%H:%M:%SZ"), verdict_completed_at=vc.strftime("%Y-%m-%dT%H:%M:%SZ"))
        store_path = self.directory / "cap-split.json"

        # List all 1005 runs via range-splitting; cap detail requests at 30 so the test stays fast while proving all 1005 are listed.
        outcome = self.run_measurement(
            "measure",
            "--own-repository",
            MANAGEMENT,
            "--store-out",
            str(store_path),
            "--request-budget",
            "40",
        )
        self.assertEqual(0, outcome.status, outcome.stderr)
        saved = json.loads(store_path.read_text(encoding="utf-8"))
        self.assertEqual(1005, len(saved["repositories"][CONSUMER]["records"]))

    def test_partial_coverage_row_opens_no_finding_of_any_kind_except_no_workflow(self) -> None:
        # CONSUMER has 2 runs that fail EVERY goal (slow, no-verdict if only non-verdict) — keep 1 pending via budget.
        consumer = self.stand_in.repository(CONSUMER)
        no_tests_plan = {
            "version": 1,
            "parts": {"repository-hygiene": {"run": True, "reason": "always runs"}, "python-test": {"run": False, "reason": "none"}},
            "departures": [],
        }
        # Newest run: no-verdict; older run: slow 5-minute run. With budget stopping before older run, coverage is partial!
        consumer.add_run("2026-03-20T10:00:00Z", verdict_completed_at="2026-03-20T10:05:00Z", plan=no_tests_plan)
        consumer.add_run("2026-03-25T10:00:00Z", verdict_completed_at=None, verdict_job_name=None, conclusion="failure")
        # OTHER has no workflow at all -> MUST still open its no-workflow finding!
        self.stand_in.repository(OTHER, has_workflow=False)

        outcome = self.run_measurement(
            "measure",
            "--own-repository",
            MANAGEMENT,
            "--workers",
            "1",
            "--request-budget",
            "4",
        )
        self.assertEqual(0, outcome.status, outcome.stderr)
        assert outcome.report is not None
        self.assertFalse(outcome.report["repositories"][CONSUMER]["coverage"]["complete"])
        self.assertEqual(1, len(self.management.issues), [i["title"] for i in self.management.issues])
        self.assertIn(OTHER, self.management.issues[0]["title"])
        self.assertIn("No ci.yml workflow", self.management.issues[0]["title"])


class MeasurementCooldownReportAndProgressTests(MeasurementFixture):
    NO_TESTS_PLAN = {
        "version": 1,
        "parts": {
            "repository-hygiene": {"run": True, "reason": "always runs"},
            "python-test": {"run": False, "reason": "no changed path matches its paths"},
        },
        "departures": [],
    }

    def test_closed_issue_suppresses_reopening_for_7_days_and_reopens_after(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        consumer.add_run("2026-03-20T10:00:00Z", verdict_completed_at="2026-03-20T10:05:00Z", plan=self.NO_TESTS_PLAN)

        first = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--now", "2026-04-01T12:00:00Z")
        self.assertEqual(0, first.status, first.stderr)
        self.assertEqual(2, len(self.management.issues))

        # Close both issues 2 days before NOW -> within 7-day cooldown -> no issues reopened.
        for issue in self.management.issues:
            issue["state"] = "closed"
            issue["closed_at"] = "2026-03-30T12:00:00Z"
            issue["updated_at"] = "2026-03-30T12:00:00Z"
        second = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--now", "2026-04-01T12:00:00Z")
        self.assertEqual(0, second.status, second.stderr)
        self.assertEqual(2, len(self.management.issues))

        # Move one closed issue to 8 days before NOW -> outside 7-day cooldown -> reopens that target only.
        self.management.issues[0]["closed_at"] = "2026-03-24T11:00:00Z"
        self.management.issues[0]["updated_at"] = "2026-03-24T11:00:00Z"
        third = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--now", "2026-04-01T12:00:00Z")
        self.assertEqual(0, third.status, third.stderr)
        self.assertEqual(3, len(self.management.issues))

    def test_report_subcommand_over_window_longer_than_coverage_reports_coverage_judge_versions_and_removed_repos(self) -> None:
        store_file = self.directory / "downloaded-store.json"
        removed = "example-owner/retired-repo"
        document = {
            "schema_version": 1,
            "judge_version": 1,
            "retention_days": 90,
            "generated_at": "2026-04-01T12:00:00Z",
            "repositories": {
                CONSUMER: {
                    "has_workflow": True,
                    "listed_from": "2026-03-04T12:00:00Z",
                    "listed_to": "2026-04-01T12:00:00Z",
                    "records": {
                        "10": {
                            "run_id": 10,
                            "created_at": "2026-02-01T10:00:00Z",
                            "run_attempt": 1,
                            "status": "completed",
                            "state": "verdict",
                            "verdict_completed_at": "2026-02-01T10:01:00Z",
                            "duration_seconds": 60.0,
                            "plan_found": True,
                            "selects_no_tests": True,
                            "fallbacks": {},
                            "unknown_input_parts": [],
                            "fixed_cost_outcome": "skip",
                            "fixed_cost_sample": None,
                            "judged_at": "2026-02-02T12:00:00Z",
                            "judge_version": 0,
                        },
                        "11": {
                            "run_id": 11,
                            "created_at": "2026-03-20T10:00:00Z",
                            "run_attempt": 1,
                            "status": "completed",
                            "state": "verdict",
                            "verdict_completed_at": "2026-03-20T10:02:00Z",
                            "duration_seconds": 120.0,
                            "plan_found": True,
                            "selects_no_tests": True,
                            "fallbacks": {},
                            "unknown_input_parts": [],
                            "fixed_cost_outcome": "skip",
                            "fixed_cost_sample": None,
                            "judged_at": "2026-04-01T12:00:00Z",
                            "judge_version": 1,
                        },
                    },
                },
                removed: {
                    "has_workflow": True,
                    "listed_from": "2026-01-07T12:00:00Z",
                    "listed_to": "2026-04-01T12:00:00Z",
                    "records": {
                        "20": {
                            "run_id": 20,
                            "created_at": "2026-02-10T10:00:00Z",
                            "run_attempt": 1,
                            "status": "completed",
                            "state": "verdict",
                            "verdict_completed_at": "2026-02-10T10:01:00Z",
                            "duration_seconds": 60.0,
                            "plan_found": False,
                            "selects_no_tests": None,
                            "fallbacks": {},
                            "unknown_input_parts": [],
                            "fixed_cost_outcome": "skip",
                            "fixed_cost_sample": None,
                            "judged_at": "2026-04-01T12:00:00Z",
                            "judge_version": 1,
                        },
                    },
                },
            },
        }
        store_file.write_text(json.dumps(document), encoding="utf-8")

        outcome = self.run_measurement("report", "--store", str(store_file), "--window-days", "84")
        self.assertEqual(0, outcome.status, outcome.stderr)
        self.assertEqual([], self.stand_in.requests, "report must be completely offline")
        assert outcome.report is not None
        self.assertEqual(84, outcome.report["window_days"])
        # CONSUMER covers only 28 of 84 days -> partial coverage, judge versions [0, 1].
        consumer_cov = outcome.report["repositories"][CONSUMER]["coverage"]
        self.assertFalse(consumer_cov["complete"])
        self.assertEqual([0, 1], consumer_cov["judge_versions"])
        self.assertIn("28/84d", outcome.summary)
        self.assertIn("judge v0, v1", outcome.summary)
        # Removed repo is included in offline report and covers all 84 days.
        self.assertIn(removed, outcome.report["repositories"])
        self.assertTrue(outcome.report["repositories"][removed]["coverage"]["complete"])
        self.assertEqual(1, outcome.report["repositories"][removed]["verdict_runs"])

    def test_progress_lines_totals_line_and_repo_level_ci_plan_artifact_listing(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        for day in range(20, 25):
            consumer.add_run(
                f"2026-03-{day:02d}T10:00:00Z",
                verdict_completed_at=f"2026-03-{day:02d}T10:01:00Z",
                plan=self.NO_TESTS_PLAN,
            )

        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT)
        self.assertEqual(0, outcome.status, outcome.stderr)
        # Progress line per repo and final totals line on stderr.
        self.assertIn(f"{CONSUMER}: new=5 unfinished=0 pending=0 kept=5", outcome.stderr)
        self.assertIn("Measurement totals: requests=", outcome.stderr)
        self.assertIn("ratelimit_remaining=4500", outcome.stderr)
        self.assertIn("pending=0", outcome.stderr)
        # Used repository-level ci-plan artifact listing instead of 5 separate per-run artifact listings.
        repo_art_calls = [p for _, p, _ in self.stand_in.requests if f"/repos/{CONSUMER}/actions/artifacts?name=ci-plan" in p]
        run_art_calls = [p for _, p, _ in self.stand_in.requests if f"/repos/{CONSUMER}/actions/runs/" in p and "/artifacts" in p]
        self.assertEqual(1, len(repo_art_calls), repo_art_calls)
        self.assertEqual([], run_art_calls)


class MeasurementSpecReviewFixTests(MeasurementFixture):
    NO_TESTS_PLAN = {
        "version": 1,
        "parts": {
            "repository-hygiene": {"run": True, "reason": "always runs"},
            "python-test": {"run": False, "reason": "no changed path matches its paths"},
        },
        "departures": [],
    }

    def test_periodic_checkpoint_persists_completed_details_before_any_repository_finishes(self) -> None:
        # Spread 30 runs across two repositories so newest-first order interleaves them and neither repo finishes before item 26.
        consumer = self.stand_in.repository(CONSUMER)
        other = self.stand_in.repository(OTHER)
        for index in range(15):
            day = 10 + index
            consumer.add_run(f"2026-03-{day:02d}T10:00:00Z", verdict_completed_at=f"2026-03-{day:02d}T10:01:00Z")
            other.add_run(f"2026-03-{day:02d}T10:30:00Z", verdict_completed_at=f"2026-03-{day:02d}T10:31:00Z")
        store_out = self.directory / "periodic.json"
        observed_verdicts_mid_run: list[int] = []
        jobs_seen = 0

        def observe_mid_run(method: str, path: str, query: dict[str, str]):
            nonlocal jobs_seen
            if "/attempts/1/jobs" in path:
                jobs_seen += 1
                if jobs_seen == 27 and store_out.is_file():
                    snapshot = json.loads(store_out.read_text(encoding="utf-8"))
                    verdicts = sum(
                        1
                        for repo_doc in snapshot["repositories"].values()
                        for rec in repo_doc["records"].values()
                        if rec["state"] == "verdict"
                    )
                    observed_verdicts_mid_run.append(verdicts)
            return None

        self.stand_in.on_request = observe_mid_run
        outcome = self.run_measurement(
            "measure",
            "--own-repository",
            MANAGEMENT,
            "--store-out",
            str(store_out),
            "--workers",
            "1",
        )
        self.assertEqual(0, outcome.status, outcome.stderr)
        self.assertTrue(observed_verdicts_mid_run, "expected mid-run checkpoint observation at job 27")
        self.assertGreaterEqual(
            observed_verdicts_mid_run[0],
            25,
            "store must checkpoint periodically during a build-up before any repository finishes",
        )

    def test_malformed_unexpired_store_artifact_entry_is_unreadable_not_absent(self) -> None:
        self.stand_in.repository(CONSUMER)
        # An unexpired measurement-records artifact missing workflow_run.head_branch must not trigger a fresh build-up.
        self.management.repo_artifacts.append({
            "id": 9001,
            "name": "measurement-records",
            "expired": False,
            "created_at": "2026-03-31T12:00:00Z",
            "workflow_run": {},
        })
        store_out = self.directory / "should-not-exist.json"

        outcome = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--store-out", str(store_out))
        self.assertEqual(1, outcome.status)
        self.assertFalse(store_out.exists(), "malformed unexpired store metadata must never trigger a replacement build-up")
        self.assertIn("unreadable", outcome.stderr)

    def test_store_zip_download_5xx_fails_without_writing_store_while_404_and_410_fall_back(self) -> None:
        self.stand_in.repository(CONSUMER)
        valid_store = {
            "schema_version": 1,
            "judge_version": 1,
            "retention_days": 90,
            "generated_at": "2026-03-30T12:00:00Z",
            "repositories": {
                CONSUMER: {
                    "has_workflow": True,
                    "listed_from": "2026-03-01T12:00:00Z",
                    "listed_to": "2026-03-30T12:00:00Z",
                    "records": {},
                },
            },
        }
        valid_id = self.management.add_store_artifact(valid_store, created_at="2026-03-30T12:00:00Z", head_branch="main")
        gone_id = self.management.add_store_artifact(valid_store, created_at="2026-03-31T12:00:00Z", head_branch="main")
        missing_id = self.management.add_store_artifact(valid_store, created_at="2026-04-01T06:00:00Z", head_branch="main")

        # 404 on newest and 410 on second-newest fall back to valid_id.
        self.stand_in.refused[f"/repos/{MANAGEMENT}/actions/artifacts/{missing_id}/zip"] = 404
        self.stand_in.refused[f"/repos/{MANAGEMENT}/actions/artifacts/{gone_id}/zip"] = 410
        store_out = self.directory / "fallback.json"
        fallback = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--store-out", str(store_out))
        self.assertEqual(0, fallback.status, fallback.stderr)
        assert fallback.report is not None
        self.assertEqual(valid_id, fallback.report["store"]["artifact_id"])
        self.assertEqual([missing_id, gone_id], fallback.report["store"]["skipped_invalid_ids"])

        # 503 on newest zip download fails immediately without writing store_out.
        self.stand_in.refused[f"/repos/{MANAGEMENT}/actions/artifacts/{missing_id}/zip"] = 503
        store_out_5xx = self.directory / "fail-5xx.json"
        failed = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--store-out", str(store_out_5xx))
        self.assertEqual(1, failed.status)
        self.assertFalse(store_out_5xx.exists(), "a 5xx during store zip download must abort before writing store")

    def test_rejudge_keeps_old_record_and_version_when_plan_artifact_has_expired(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        # Run at the 29-day edge whose ci-plan artifact has already expired.
        run_id = consumer.add_run(
            "2026-03-03T10:00:00Z",
            verdict_completed_at="2026-03-03T10:00:45Z",
            plan=self.NO_TESTS_PLAN,
            plan_expired=True,
        )
        old_store = {
            "schema_version": 1,
            "judge_version": 0,
            "retention_days": 90,
            "generated_at": "2026-03-31T12:00:00Z",
            "repositories": {
                CONSUMER: {
                    "has_workflow": True,
                    "listed_from": "2026-03-01T12:00:00Z",
                    "listed_to": "2026-03-31T12:00:00Z",
                    "records": {
                        str(run_id): {
                            "run_id": run_id,
                            "created_at": "2026-03-03T10:00:00Z",
                            "run_attempt": 1,
                            "status": "completed",
                            "state": "verdict",
                            "verdict_completed_at": "2026-03-03T10:00:45Z",
                            "duration_seconds": 45.0,
                            "plan_found": True,
                            "selects_no_tests": True,
                            "fallbacks": {"old-fallback": 1},
                            "unknown_input_parts": ["custom"],
                            "fixed_cost_outcome": "skip",
                            "fixed_cost_sample": None,
                            "judged_at": "2026-03-04T12:00:00Z",
                            "judge_version": 0,
                        },
                    },
                },
            },
        }
        store_in = self.directory / "old.json"
        store_out = self.directory / "kept.json"
        store_in.write_text(json.dumps(old_store), encoding="utf-8")

        outcome = self.run_measurement(
            "measure",
            "--own-repository",
            MANAGEMENT,
            "--store-in",
            str(store_in),
            "--store-out",
            str(store_out),
            "--now",
            "2026-04-01T12:00:00Z",
        )
        self.assertEqual(0, outcome.status, outcome.stderr)
        saved = json.loads(store_out.read_text(encoding="utf-8"))
        kept_rec = saved["repositories"][CONSUMER]["records"][str(run_id)]
        self.assertTrue(kept_rec["plan_found"])
        self.assertTrue(kept_rec["selects_no_tests"])
        self.assertEqual({"old-fallback": 1}, kept_rec["fallbacks"])
        self.assertEqual(0, kept_rec["judge_version"])

    def test_report_window_days_rejects_values_above_90(self) -> None:
        store_file = self.directory / "store.json"
        store_file.write_text(json.dumps({
            "schema_version": 1,
            "judge_version": 1,
            "retention_days": 90,
            "generated_at": "2026-04-01T12:00:00Z",
            "repositories": {},
        }), encoding="utf-8")
        outcome = self.run_measurement("report", "--store", str(store_file), "--window-days", "91")
        self.assertEqual(2, outcome.status)
        self.assertIn("90", outcome.stderr)

    def test_deleted_unfinished_run_404_uses_deleted_state_and_is_counted_separately(self) -> None:
        self.stand_in.repository(CONSUMER)
        store_in = self.directory / "with-unfinished.json"
        store_out = self.directory / "after-delete.json"
        store_in.write_text(json.dumps({
            "schema_version": 1,
            "judge_version": 1,
            "retention_days": 90,
            "generated_at": "2026-03-31T12:00:00Z",
            "repositories": {
                CONSUMER: {
                    "has_workflow": True,
                    "listed_from": "2026-03-04T12:00:00Z",
                    "listed_to": "2026-03-31T12:00:00Z",
                    "records": {
                        "4040": {
                            "run_id": 4040,
                            "created_at": "2026-03-20T10:00:00Z",
                            "run_attempt": 1,
                            "status": "in_progress",
                            "state": "unfinished",
                            "judged_at": "2026-03-31T12:00:00Z",
                            "judge_version": None,
                        },
                    },
                },
            },
        }), encoding="utf-8")

        outcome = self.run_measurement(
            "measure",
            "--own-repository",
            MANAGEMENT,
            "--store-in",
            str(store_in),
            "--store-out",
            str(store_out),
            "--now",
            "2026-04-01T12:00:00Z",
        )
        self.assertEqual(0, outcome.status, outcome.stderr)
        saved = json.loads(store_out.read_text(encoding="utf-8"))
        self.assertEqual("deleted", saved["repositories"][CONSUMER]["records"]["4040"]["state"])
        assert outcome.report is not None
        metrics = outcome.report["repositories"][CONSUMER]
        self.assertEqual(0, metrics["verdict_runs"])
        self.assertEqual(0, metrics["non_verdict_runs"])
        self.assertEqual(1, metrics["deleted_runs"])
        self.assertIn("1 deleted", outcome.summary)

    def test_secondary_rate_limit_waits_full_retry_after_within_deadline_and_skips_wait_when_deadline_too_short(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        run_1 = consumer.add_run("2026-03-20T10:00:00Z", verdict_completed_at="2026-03-20T10:01:00Z")
        consumer.add_run("2026-03-25T10:00:00Z", verdict_completed_at="2026-03-25T10:01:00Z")

        def hit_secondary_long(method: str, path: str, query: dict[str, str]):
            if f"/runs/{run_1}/attempts/1/jobs" in path:
                return self.stand_in._json(
                    403,
                    {"message": "You have exceeded a secondary rate limit."},
                    headers={"Retry-After": "180", "X-RateLimit-Remaining": "4000"},
                )
            return None

        self.stand_in.on_request = hit_secondary_long
        # Case A: 480s deadline allows waiting the full 180s Retry-After (not capped at 120s).
        store_a = self.directory / "retry-full.json"
        sleeps_a: list[float] = []
        with mock.patch("time.sleep", side_effect=lambda secs: sleeps_a.append(secs)):
            outcome_a = self.run_measurement(
                "measure",
                "--own-repository",
                MANAGEMENT,
                "--store-out",
                str(store_a),
                "--workers",
                "1",
                "--time-budget",
                "480",
                "--min-secondary-wait",
                "60",
            )
        self.assertEqual(1, outcome_a.status)
        self.assertEqual([180.0], sleeps_a)
        self.assertTrue(store_a.is_file())

        # Case B: 30s deadline cannot accommodate a 180s Retry-After -> skip sleeping, checkpoint and fail.
        store_b = self.directory / "retry-short-deadline.json"
        sleeps_b: list[float] = []
        with mock.patch("time.sleep", side_effect=lambda secs: sleeps_b.append(secs)):
            outcome_b = self.run_measurement(
                "measure",
                "--own-repository",
                MANAGEMENT,
                "--store-out",
                str(store_b),
                "--workers",
                "1",
                "--time-budget",
                "30",
                "--min-secondary-wait",
                "60",
            )
        self.assertEqual(1, outcome_b.status)
        self.assertEqual([], sleeps_b)
        self.assertTrue(store_b.is_file())


class MeasurementCoveragePrecisionTests(MeasurementFixture):
    NO_TESTS_PLAN = {
        "version": 1,
        "parts": {
            "repository-hygiene": {"run": True, "reason": "always runs"},
            "python-test": {"run": False, "reason": "no changed path matches its paths"},
        },
        "departures": [],
    }

    def test_a_reference_time_with_microseconds_lists_the_window_and_reports_coverage_complete_on_both_runs(self) -> None:
        consumer = self.stand_in.repository(CONSUMER)
        # 4 minutes is a miss of both targets, so a finding is due once coverage is complete.
        consumer.add_run("2026-03-30T10:00:00Z", verdict_completed_at="2026-03-30T10:04:00Z", plan=self.NO_TESTS_PLAN)
        store_path = self.directory / "store.json"
        # The reference time carries microseconds, as the default --now does; the store keeps whole seconds.
        now = "2026-04-01T12:00:00.250000Z"

        first = self.run_measurement("measure", "--own-repository", MANAGEMENT, "--store-out", str(store_path), "--now", now)
        self.assertEqual(0, first.status, first.stderr)
        assert first.report is not None
        self.assertTrue(first.report["repositories"][CONSUMER]["coverage"]["complete"], first.report["repositories"][CONSUMER]["coverage"])
        self.assertTrue(any(CONSUMER in issue["title"] for issue in self.management.issues), [i["title"] for i in self.management.issues])

        # A second run that reads the first run's store must also see complete coverage.
        second = self.run_measurement(
            "measure",
            "--own-repository",
            MANAGEMENT,
            "--store-in",
            str(store_path),
            "--store-out",
            str(store_path),
            "--now",
            "2026-04-01T12:00:01.750000Z",
        )
        self.assertEqual(0, second.status, second.stderr)
        assert second.report is not None
        self.assertTrue(second.report["repositories"][CONSUMER]["coverage"]["complete"], second.report["repositories"][CONSUMER]["coverage"])


if __name__ == "__main__":
    unittest.main()
