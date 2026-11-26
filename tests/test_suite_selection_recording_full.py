"""The full-recording mode of the recording step on fixture repositories, against the stand-in strace and the stored
strace recordings of the fixtures' commands."""

from __future__ import annotations

import json
import subprocess
import textwrap
import unittest

from tests.recording_fixtures import RecordingFixture, StoredFixture, requires_linux, suite

PASSING = """\
    import unittest

    class Case(unittest.TestCase):
        def test_reads(self):
            open("data/ok.txt").read()

    unittest.main()
    """

SKIPPING = """\
    import unittest

    class Case(unittest.TestCase):
        def test_reads(self):
            open("data/skip.txt").read()

        @unittest.skip("the tool is missing")
        def test_needs_a_tool(self):
            pass

    unittest.main()
    """

PLAIN = 'open("data/plain.txt").read()\n'
READLINK = 'import os\nprint(os.readlink("link"))\n'
OPEN_LINK = 'print(open("link").read())\n'
ENTRIES = ["tests/ok.py", "tests/skip.py", "tests/plain.py"]
STALE = "data/stale.txt"
RUNNERS = StoredFixture("full-runners", {"tests/ok.py": PASSING, "tests/skip.py": SKIPPING, "tests/plain.py": PLAIN, "data/ok.txt": "",
                                         "data/skip.txt": "", "data/plain.txt": "", STALE: ""},
                        {entry.removeprefix("tests/").removesuffix(".py"): ("python3", entry) for entry in ENTRIES})

def printing(*lines: str) -> str:
    return "".join(f"print({line!r})\n" for line in lines)


# Entry files whose output holds a TAP report, by name.
TAP = {
    "complete": printing("1..2", "ok 1 - first", "ok 2 - second"),
    "skipping": printing("1..3", "ok 1 - first", "ok 2 - second # SKIP not selected", "ok 3 - third # skip not selected"),
    "short": printing("1..3", "ok 1 - first", "not ok 2 - second"),
    "two-plans": printing("1..1", "ok 1 - first", "1..1"),
    "trailing-plan": printing("ok 1 - first", "ok 2 - second", "1..2"),
    "skip-word": printing("1..1", "ok 1 - parses #skipper"),
    "empty-plan": printing("1..0"),
    "skipped-plan": printing("1..0 # SKIP the tool is missing"),
    # A stray plan line beside a complete unittest summary.
    "stray-plan": printing("1..3") + textwrap.dedent(PASSING),
}
TAP_ENTRIES = [f"tests/tap_{name}.py" for name in TAP]
LINKING = StoredFixture("full-link", {"tests/link.py": READLINK, "tests/open_link.py": OPEN_LINK, "target.txt": "target\n"},
                        {"link": ("python3", "tests/link.py"), "open_link": ("python3", "tests/open_link.py")}, {"link": "target.txt"})
# The fixtures whose commands python3 -m tests.record_strace_fixture records.
STORED = (RUNNERS, LINKING)


@requires_linux
class FullRecordingTests(unittest.TestCase):
    def fixture(self) -> RecordingFixture:
        return RUNNERS.fixture(self.addCleanup, [suite("per-entry", ENTRIES, entry_selection=True, paths=[],
                                                       entry_paths={entry: [entry, STALE] for entry in ENTRIES})])

    def test_the_full_recording_records_the_wide_trace_set(self) -> None:
        fixture = LINKING.fixture(self.addCleanup, [suite("a", ["tests/link.py"], paths=["tests/link.py"])])
        checked = fixture.record(["tests/link.py"], full=True)
        self.assertEqual(0, checked.returncode, checked.stderr)
        self.assertIn("link", fixture.reads_of("tests/link.py")["paths"])
        # Pull-request runs also trace readlink and readlinkat, so link is reported as an addition.
        pr_checked = fixture.record(["tests/link.py"])
        self.assertEqual(1, pr_checked.returncode, pr_checked.stdout + pr_checked.stderr)
        self.assertIn("link", fixture.additions()["suites"][0]["reads"]["paths"])
        traced = [call[call.index("-e") + 1] for call in fixture.stand_in_calls()]
        self.assertEqual([True, True], ["?readlink" in calls.split("=", 1)[1].split(",") for calls in traced])

    def test_a_pull_request_run_records_a_tests_reads_through_link_targets(self) -> None:
        fixture = LINKING.fixture(self.addCleanup, [suite("a", ["tests/open_link.py"], paths=["tests/open_link.py"])])
        checked = fixture.record(["tests/open_link.py"])
        self.assertEqual(1, checked.returncode, checked.stdout + checked.stderr)
        added = fixture.additions()["suites"][0]["reads"]["paths"]
        self.assertIn("link", added)
        self.assertIn("target.txt", added)

    def test_it_removes_a_stored_read_only_for_an_entry_file_that_ran_with_no_test_skipped(self) -> None:
        fixture = self.fixture()
        checked = fixture.record(ENTRIES, full=True)
        self.assertEqual(0, checked.returncode, checked.stderr)
        skipped = {item["entry"]: item["skipped"] for item in fixture.full_result()["entries"]}
        self.assertEqual({"tests/ok.py": 0, "tests/skip.py": 1, "tests/plain.py": None}, skipped)
        applied = fixture.apply(fixture.output / "full-recording.json")
        self.assertEqual(0, applied.returncode, applied.stderr)
        stored = fixture.suite("per-entry")["entry_paths"]
        self.assertNotIn(STALE, stored["tests/ok.py"])
        self.assertIn("data/ok.txt", stored["tests/ok.py"])
        self.assertIn(STALE, stored["tests/skip.py"])
        self.assertIn("data/skip.txt", stored["tests/skip.py"])
        self.assertIn(STALE, stored["tests/plain.py"])
        self.assertIn("data/plain.txt", stored["tests/plain.py"])
        self.assertIn("kept the list of tests/skip.py: 1 test skipped", applied.stdout)
        self.assertIn("kept the list of tests/plain.py: its test runner reports no skip count", applied.stdout)
        self.assertNotIn("tests/ok.py", applied.stdout)

    def test_the_full_recording_stores_a_list_per_matched_file_of_a_glob_suite_and_keeps_the_pattern(self) -> None:
        # tests/ok.py has a stored list; the pattern matches the two other files, which have none; tests/gone.py is no file.
        fixture = RUNNERS.fixture(self.addCleanup, [suite("glob", ["tests/*.py"], entry_selection=True, paths=["tests/ok.py", STALE],
                                                          entry_paths={"tests/ok.py": ["tests/ok.py", STALE], "tests/gone.py": [STALE]},
                                                          entry_durations={"tests/gone.py": 2.0})])
        checked = fixture.record(ENTRIES, full=True)
        self.assertEqual(0, checked.returncode, checked.stderr)
        applied = fixture.apply(fixture.output / "full-recording.json")
        self.assertEqual(0, applied.returncode, applied.stderr)
        stored = fixture.suite("glob")
        self.assertEqual(["tests/*.py"], stored["entries"])
        self.assertEqual(sorted(ENTRIES), list(stored["entry_paths"]))
        self.assertNotIn(STALE, stored["paths"])
        for name in ("ok", "skip", "plain"):
            self.assertIn(f"data/{name}.txt", stored["entry_paths"][f"tests/{name}.py"])
        self.assertEqual(sorted(ENTRIES), list(stored["entry_durations"]))
        # The written suite list loads: a second apply of the same result reads it and exits 0.
        self.assertEqual(0, fixture.apply(fixture.output / "full-recording.json").returncode)

    def test_the_full_recording_removes_the_keys_of_a_file_that_no_path_entry_names_and_writes_a_suite_list_that_loads(self) -> None:
        # tests/ok.py has a stored list, the two other entry files have none; tests/gone.py is named by no entry.
        gone = "tests/gone.py"
        fixture = RUNNERS.fixture(self.addCleanup, [suite("paths", ENTRIES, entry_selection=True, paths=["tests/ok.py", STALE],
                                                          entry_paths={"tests/ok.py": ["tests/ok.py"], gone: [STALE]},
                                                          entry_directory_memberships={gone: ["data"]},
                                                          entry_existence_reads={gone: [{"path": "gone-probe", "kind": "any"}]},
                                                          entry_lookup_directories={gone: ["data"]}, entry_durations={gone: 2.0})])
        checked = fixture.record(ENTRIES, full=True)
        self.assertEqual(0, checked.returncode, checked.stderr)
        applied = fixture.apply(fixture.output / "full-recording.json")
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertIn(f"paths: removed the stored lists and the estimate of {gone}: no entry names it", applied.stdout)
        stored = fixture.suite("paths")
        self.assertEqual(ENTRIES, stored["entries"])
        self.assertEqual(ENTRIES, list(stored["entry_paths"]))
        for field in ("entry_directory_memberships", "entry_existence_reads", "entry_lookup_directories"):
            self.assertNotIn(gone, stored.get(field, {}))
        self.assertEqual(ENTRIES, list(stored["entry_durations"]))
        self.assertNotIn(STALE, stored["paths"])
        # The written suite list loads: a second apply of the same result reads it and exits 0.
        self.assertEqual(0, fixture.apply(fixture.output / "full-recording.json").returncode)

    def test_a_pull_request_run_of_a_glob_suite_reports_additions_that_apply_and_then_cover_every_read(self) -> None:
        for stored in ({}, {"paths": ["tests/ok.py"], "entry_paths": {"tests/ok.py": ["tests/ok.py"]}}):
            with self.subTest(stored=stored):
                fixture = RUNNERS.fixture(self.addCleanup, [suite("glob", ["tests/*.py"], entry_selection=True, **stored)])
                checked = fixture.record(ENTRIES)
                self.assertEqual(1, checked.returncode, checked.stdout + checked.stderr)
                [change] = fixture.additions()["suites"]
                # A matched file without a stored list reports every read, its own file included.
                self.assertEqual(set(ENTRIES), set(change["entries"]))
                self.assertLessEqual({"tests/plain.py", "data/plain.txt"}, set(change["entries"]["tests/plain.py"]["paths"]))
                applied = fixture.apply()
                self.assertEqual(0, applied.returncode, applied.stderr)
                self.assertEqual(["tests/*.py"], fixture.suite("glob")["entries"])
                self.assertEqual(sorted(ENTRIES), list(fixture.suite("glob")["entry_paths"]))
                again = fixture.check()
                self.assertEqual(0, again.returncode, again.stdout + again.stderr)

    def test_a_whole_suite_keeps_its_list_while_one_entry_file_skipped_a_test(self) -> None:
        fixture = RUNNERS.fixture(self.addCleanup, [suite("whole", ["tests/ok.py", "tests/skip.py"], paths=[STALE])])
        self.assertEqual(0, fixture.record(["tests/ok.py", "tests/skip.py"], full=True).returncode)
        applied = fixture.apply(fixture.output / "full-recording.json")
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertIn(STALE, fixture.suite("whole")["paths"])
        self.assertIn("data/ok.txt", fixture.suite("whole")["paths"])
        self.assertIn("whole: kept the list of tests/skip.py: 1 test skipped", applied.stdout)

    def test_every_measurement_names_the_shard_job_it_came_from(self) -> None:
        fixture = self.fixture()
        self.assertEqual(0, fixture.record(ENTRIES, full=True, job="shard-3").returncode)
        self.assertEqual({entry: "shard-3" for entry in ENTRIES}, {item["entry"]: item["job"] for item in fixture.full_result()["entries"]})
        units = [json.loads(path.read_text(encoding="utf-8")) for path in (fixture.output / "units").glob("*.json")]
        self.assertEqual(len(ENTRIES), len(units))
        self.assertTrue(all(unit["job"] == "shard-3" for unit in units))

    def test_start_takes_a_job_only_in_the_full_recording_mode(self) -> None:
        refused = self.fixture().start(job="shard-1")
        self.assertEqual(2, refused.returncode)
        self.assertIn("--job goes with --full", refused.stderr)

    def test_it_writes_a_duration_estimate_for_every_entry_file_that_exited_with_zero(self) -> None:
        # tests/skip.py skipped a test and tests/plain.py reports no skip count; all three exited with 0 so all three
        # get duration estimates, while only clean tests/ok.py replaces its stored list.
        fixture = self.fixture()
        self.assertEqual(0, fixture.record(ENTRIES, full=True).returncode)
        self.assertEqual(0, fixture.apply(fixture.output / "full-recording.json").returncode)
        durations = fixture.suite("per-entry")["entry_durations"]
        self.assertEqual(ENTRIES, list(durations))
        for entry in ENTRIES:
            self.assertGreater(durations[entry], 0)


@requires_linux
class TapReportTests(unittest.TestCase):
    """An entry file whose output holds a TAP report, run under the stand-in with an empty recording."""

    fixture: RecordingFixture
    applied: subprocess.CompletedProcess[str]

    @classmethod
    def setUpClass(cls) -> None:
        files = {**{f"tests/tap_{name}.py": source for name, source in TAP.items()}, STALE: "", "data/ok.txt": ""}
        fixture = RecordingFixture(cls.addClassCleanup, files, [suite("per-entry", TAP_ENTRIES, entry_selection=True, paths=[],
                                                                      entry_paths={entry: [entry, STALE] for entry in TAP_ENTRIES})])
        checked = fixture.record(TAP_ENTRIES, full=True)
        if checked.returncode != 0:
            raise AssertionError(checked.stderr or checked.stdout)
        applied = fixture.apply(fixture.output / "full-recording.json")
        if applied.returncode != 0:
            raise AssertionError(applied.stderr or applied.stdout)
        cls.fixture = fixture
        cls.applied = applied

    def test_a_complete_tap_report_without_a_skip_line_replaces_the_list(self) -> None:
        fixture, applied = self.fixture, self.applied
        stored = fixture.suite("per-entry")["entry_paths"]
        # The plan may come after the test lines, and a description that holds the word skip is no skip directive.
        for entry in ("tests/tap_complete.py", "tests/tap_trailing-plan.py", "tests/tap_skip-word.py"):
            self.assertNotIn(STALE, stored[entry])
            self.assertNotIn(entry, applied.stdout)

    def test_an_incomplete_tap_report_leaves_a_complete_unittest_summary_to_count(self) -> None:
        fixture, applied = self.fixture, self.applied
        self.assertNotIn(STALE, fixture.suite("per-entry")["entry_paths"]["tests/tap_stray-plan.py"])
        self.assertNotIn("tests/tap_stray-plan.py", applied.stdout)

    def test_a_plan_of_zero_skips_the_whole_entry_file(self) -> None:
        fixture, applied = self.fixture, self.applied
        for entry in ("tests/tap_empty-plan.py", "tests/tap_skipped-plan.py"):
            self.assertIn(STALE, fixture.suite("per-entry")["entry_paths"][entry])
            self.assertIn(f"kept the list of {entry}: 1 test skipped", applied.stdout)

    def test_a_tap_report_with_skip_lines_keeps_the_list_and_reports_the_skipped_tests(self) -> None:
        fixture, applied = self.fixture, self.applied
        self.assertIn(STALE, fixture.suite("per-entry")["entry_paths"]["tests/tap_skipping.py"])
        self.assertIn("kept the list of tests/tap_skipping.py: 2 tests skipped", applied.stdout)

    def test_a_tap_report_whose_plan_does_not_match_its_test_lines_counts_as_no_complete_report(self) -> None:
        fixture, applied = self.fixture, self.applied
        self.assertIn(STALE, fixture.suite("per-entry")["entry_paths"]["tests/tap_short.py"])
        self.assertIn("kept the list of tests/tap_short.py: it printed no complete test report: its TAP plan does not match its test lines",
                      applied.stdout)

    def test_a_tap_report_with_more_than_one_plan_counts_as_no_complete_report(self) -> None:
        fixture, applied = self.fixture, self.applied
        self.assertIn(STALE, fixture.suite("per-entry")["entry_paths"]["tests/tap_two-plans.py"])
        self.assertIn("kept the list of tests/tap_two-plans.py: it printed no complete test report: its TAP report has more than one plan",
                      applied.stdout)


PYTEST = {
    "framed": printing("======== 34 passed in 1.23s ========"),
    "quiet": printing("34 passed in 1.23s"),
    "quiet-warnings": printing("12 passed, 2 warnings in 120.50s (0:02:00)"),
    "colored": printing("\x1b[32m5 passed\x1b[0m in 0.42s"),
    "quiet-skipped": printing("3 passed, 1 skipped in 0.45s"),
    "quiet-deselected": printing("3 passed, 2 deselected in 0.45s"),
    "framed-deselected": printing("======== 3 passed, 1 deselected in 0.45s ========"),
    "quiet-both": printing("2 passed, 1 skipped, 2 deselected in 0.45s"),
    "prose": printing("finished 3 items in 1.23s"),
}
PYTEST_ENTRIES = [f"tests/pytest_{name}.py" for name in PYTEST]


@requires_linux
class PytestReportTests(unittest.TestCase):
    """Entry files whose output holds a pytest summary, framed or quiet, with or without ANSI colour codes."""

    fixture: RecordingFixture
    applied: subprocess.CompletedProcess[str]

    @classmethod
    def setUpClass(cls) -> None:
        files = {**{f"tests/pytest_{name}.py": source for name, source in PYTEST.items()}, STALE: ""}
        fixture = RecordingFixture(cls.addClassCleanup, files, [suite("per-entry", PYTEST_ENTRIES, entry_selection=True, paths=[],
                                                                      entry_paths={entry: [entry, STALE] for entry in PYTEST_ENTRIES})])
        checked = fixture.record(PYTEST_ENTRIES, full=True)
        if checked.returncode != 0:
            raise AssertionError(checked.stderr or checked.stdout)
        applied = fixture.apply(fixture.output / "full-recording.json")
        if applied.returncode != 0:
            raise AssertionError(applied.stderr or applied.stdout)
        cls.fixture = fixture
        cls.applied = applied

    def test_quiet_framed_and_colored_pytest_summaries_without_skipped_or_deselected_replace_the_list(self) -> None:
        fixture, applied = self.fixture, self.applied
        stored = fixture.suite("per-entry")["entry_paths"]
        for entry in ("tests/pytest_framed.py", "tests/pytest_quiet.py", "tests/pytest_quiet-warnings.py", "tests/pytest_colored.py"):
            with self.subTest(entry=entry):
                self.assertNotIn(STALE, stored[entry])
                self.assertNotIn(entry, applied.stdout)

    def test_pytest_runs_with_skipped_or_deselected_tests_keep_the_list_and_report_why(self) -> None:
        fixture, applied = self.fixture, self.applied
        stored = fixture.suite("per-entry")["entry_paths"]
        cases = (
            ("tests/pytest_quiet-skipped.py", "1 test skipped"),
            ("tests/pytest_quiet-deselected.py", "2 tests deselected"),
            ("tests/pytest_framed-deselected.py", "1 test deselected"),
            ("tests/pytest_quiet-both.py", "1 test skipped, 2 tests deselected"),
            ("tests/pytest_prose.py", "its test runner reports no skip count"),
        )
        for entry, reason in cases:
            with self.subTest(entry=entry):
                self.assertIn(STALE, stored[entry])
                self.assertIn(f"kept the list of {entry}: {reason}", applied.stdout)


if __name__ == "__main__":
    unittest.main()
