"""The full-recording mode of the recording step on fixture repositories, against the stand-in strace and the stored
strace recordings of the fixtures' commands."""

from __future__ import annotations

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
LINKING = StoredFixture("full-link", {"tests/link.py": READLINK, "target.txt": ""}, {"link": ("python3", "tests/link.py")}, {"link": "target.txt"})
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
        # Under the mid set of a pull-request run, readlink is not traced.
        self.assertEqual(0, fixture.record(["tests/link.py"]).returncode)
        traced = [call[call.index("-e") + 1] for call in fixture.stand_in_calls()]
        self.assertEqual([True, False], ["?readlink" in calls.split("=", 1)[1].split(",") for calls in traced])

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

    def test_a_whole_suite_keeps_its_list_while_one_entry_file_skipped_a_test(self) -> None:
        fixture = RUNNERS.fixture(self.addCleanup, [suite("whole", ["tests/ok.py", "tests/skip.py"], paths=[STALE])])
        self.assertEqual(0, fixture.record(["tests/ok.py", "tests/skip.py"], full=True).returncode)
        applied = fixture.apply(fixture.output / "full-recording.json")
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertIn(STALE, fixture.suite("whole")["paths"])
        self.assertIn("data/ok.txt", fixture.suite("whole")["paths"])
        self.assertIn("whole: kept the list of tests/skip.py: 1 test skipped", applied.stdout)

    def test_it_writes_a_duration_estimate_per_recorded_entry_file(self) -> None:
        fixture = self.fixture()
        self.assertEqual(0, fixture.record(ENTRIES, full=True).returncode)
        self.assertEqual(0, fixture.apply(fixture.output / "full-recording.json").returncode)
        durations = fixture.suite("per-entry")["entry_durations"]
        self.assertEqual(set(ENTRIES), set(durations))
        self.assertTrue(all(estimate > 0 for estimate in durations.values()))


@requires_linux
class TapReportTests(unittest.TestCase):
    """An entry file whose output holds a TAP report, run under the stand-in with an empty recording."""

    def record(self) -> tuple[RecordingFixture, subprocess.CompletedProcess[str]]:
        files = {**{f"tests/tap_{name}.py": source for name, source in TAP.items()}, STALE: "", "data/ok.txt": ""}
        fixture = RecordingFixture(self.addCleanup, files, [suite("per-entry", TAP_ENTRIES, entry_selection=True, paths=[],
                                                                   entry_paths={entry: [entry, STALE] for entry in TAP_ENTRIES})])
        checked = fixture.record(TAP_ENTRIES, full=True)
        self.assertEqual(0, checked.returncode, checked.stderr)
        applied = fixture.apply(fixture.output / "full-recording.json")
        self.assertEqual(0, applied.returncode, applied.stderr)
        return fixture, applied

    def test_a_complete_tap_report_without_a_skip_line_replaces_the_list(self) -> None:
        fixture, applied = self.record()
        stored = fixture.suite("per-entry")["entry_paths"]
        # The plan may come after the test lines, and a description that holds the word skip is no skip directive.
        for entry in ("tests/tap_complete.py", "tests/tap_trailing-plan.py", "tests/tap_skip-word.py"):
            self.assertNotIn(STALE, stored[entry])
            self.assertNotIn(entry, applied.stdout)

    def test_an_incomplete_tap_report_leaves_a_complete_unittest_summary_to_count(self) -> None:
        fixture, applied = self.record()
        self.assertNotIn(STALE, fixture.suite("per-entry")["entry_paths"]["tests/tap_stray-plan.py"])
        self.assertNotIn("tests/tap_stray-plan.py", applied.stdout)

    def test_a_plan_of_zero_skips_the_whole_entry_file(self) -> None:
        fixture, applied = self.record()
        for entry in ("tests/tap_empty-plan.py", "tests/tap_skipped-plan.py"):
            self.assertIn(STALE, fixture.suite("per-entry")["entry_paths"][entry])
            self.assertIn(f"kept the list of {entry}: 1 test skipped", applied.stdout)

    def test_a_tap_report_with_skip_lines_keeps_the_list_and_reports_the_skipped_tests(self) -> None:
        fixture, applied = self.record()
        self.assertIn(STALE, fixture.suite("per-entry")["entry_paths"]["tests/tap_skipping.py"])
        self.assertIn("kept the list of tests/tap_skipping.py: 2 tests skipped", applied.stdout)

    def test_a_tap_report_whose_plan_does_not_match_its_test_lines_counts_as_no_complete_report(self) -> None:
        fixture, applied = self.record()
        self.assertIn(STALE, fixture.suite("per-entry")["entry_paths"]["tests/tap_short.py"])
        self.assertIn("kept the list of tests/tap_short.py: it printed no complete test report: its TAP plan does not match its test lines",
                      applied.stdout)

    def test_a_tap_report_with_more_than_one_plan_counts_as_no_complete_report(self) -> None:
        fixture, applied = self.record()
        self.assertIn(STALE, fixture.suite("per-entry")["entry_paths"]["tests/tap_two-plans.py"])
        self.assertIn("kept the list of tests/tap_two-plans.py: it printed no complete test report: its TAP report has more than one plan",
                      applied.stdout)


if __name__ == "__main__":
    unittest.main()
