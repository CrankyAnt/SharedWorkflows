"""The recording step's apply on fixture suite lists, on any operating system."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from tests.recording_fixtures import RecordingFixture, load_recording, requires_linux, suite
from tests.test_recording_check import BACKSLASH_READS, BACKSLASHES

FILES = {"tests/a.py": "", "tests/c1.py": "", "tests/c2.py": "", "tests/c3.py": "", "tests/c4.py": "", "tests/w.py": ""}
# The ratio of the wide trace set to the mid trace set, by which a Linux measurement of the full recording is divided.
WIDE_TO_MID = 1.65


def suites() -> list[dict]:
    return [
        suite("whole", ["tests/a.py"], paths=["tests/a.py"], setups=["deps"], entry_durations={"tests/a.py": 4.0},
              extra_paths=["docs/**"], directory_memberships=["lib"], extra_existence_reads=[{"path": "Directory.Build.props", "kind": "file"}]),
        suite("per-entry", ["tests/c1.py", "tests/c2.py"], entry_selection=True, paths=[],
              entry_paths={"tests/c1.py": ["tests/c1.py"], "tests/c2.py": ["tests/c2.py"]}, entry_setups={"tests/c1.py": ["deps"]}),
        suite("windows", ["tests/w.py"], os="windows", paths=["tests/w.py"]),
    ]


def additions(*changes: dict) -> dict:
    return {"kind": "additions", "suite_list": "suites.json", "suites": list(changes)}


def reads(paths=(), existence=(), memberships=()) -> dict:
    return {"paths": list(paths), "existence_reads": [{"path": path, "kind": kind} for path, kind in existence],
            "directory_memberships": list(memberships)}


class ApplyTests(unittest.TestCase):
    def fixture(self, suite_list: list[dict] | None = None) -> RecordingFixture:
        return RecordingFixture(self.addCleanup, FILES, suite_list or suites())

    def write(self, fixture: RecordingFixture, name: str, document: dict):
        path = fixture.base / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_apply_adds_the_additions_and_keeps_every_other_key_of_the_suite_list(self) -> None:
        fixture = self.fixture()
        change = additions(
            {"suite": "whole", "create": False, "recorded_entries": ["tests/a.py"],
             "reads": reads(["requirements.txt"], [("absent.txt", "any"), ("data", "directory")], ["listed"])},
            {"suite": "per-entry", "create": False, "recorded_entries": ["tests/c1.py"],
             "entries": {"tests/c1.py": reads(["lib/x.py"], [("probe", "any")], ["fixtures"])}})
        applied = fixture.apply(self.write(fixture, "additions.json", change))
        self.assertEqual(0, applied.returncode, applied.stderr)
        expected = suites()
        expected[0].update(paths=["requirements.txt", "tests/a.py"], directory_memberships=["lib", "listed"],
                           existence_reads=[{"path": "absent.txt", "kind": "any"}, {"path": "data", "kind": "directory"}])
        expected[1]["entry_paths"]["tests/c1.py"] = ["lib/x.py", "tests/c1.py"]
        expected[1]["paths"] = ["lib/x.py", "tests/c1.py", "tests/c2.py"]
        expected[1]["existence_reads"] = [{"path": "probe", "kind": "any"}]
        expected[1]["directory_memberships"] = ["fixtures"]
        expected[1]["entry_existence_reads"] = {"tests/c1.py": [{"path": "probe", "kind": "any"}]}
        expected[1]["entry_directory_memberships"] = {"tests/c1.py": ["fixtures"]}
        actual = {item["name"]: item for item in fixture.suites()["suites"]}
        for item in expected:
            self.assertEqual(item, actual[item["name"]])

    def test_apply_joins_the_additions_of_several_jobs(self) -> None:
        fixture = self.fixture([suite("new", ["tests/c1.py", "tests/c2.py"], entry_selection=True)])
        first = self.write(fixture, "one.json", additions({"suite": "new", "create": True, "recorded_entries": ["tests/c1.py"],
                                                           "entries": {"tests/c1.py": reads(["a.txt"])}}))
        refused = fixture.apply(first)
        self.assertEqual(2, refused.returncode)
        self.assertIn("tests/c2.py", refused.stderr)
        self.assertNotIn("paths", fixture.suite("new"))
        second = self.write(fixture, "two.json", additions({"suite": "new", "create": True, "recorded_entries": ["tests/c2.py"],
                                                            "entries": {"tests/c2.py": reads(["b.txt"])}}))
        applied = fixture.apply(first, second)
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertEqual({"tests/c1.py": ["a.txt"], "tests/c2.py": ["b.txt"]}, fixture.suite("new")["entry_paths"])

    def assert_loads_unchanged(self, fixture: RecordingFixture) -> None:
        """The written suite list loads: a further apply reads it, finds nothing to add and leaves it as it is."""
        written = (fixture.root / "suites.json").read_text(encoding="utf-8")
        again = fixture.apply(self.write(fixture, "nothing.json", additions()))
        self.assertEqual(0, again.returncode, again.stderr)
        self.assertEqual("No additions to apply.\n", again.stdout)
        self.assertEqual(written, (fixture.root / "suites.json").read_text(encoding="utf-8"))

    def test_apply_stores_a_list_per_matched_file_of_a_glob_suite_and_keeps_the_pattern(self) -> None:
        matched = ["tests/c1.py", "tests/c2.py", "tests/c3.py", "tests/c4.py"]
        fixture = self.fixture([suite("glob", ["tests/c*.py"], entry_selection=True)])
        change = additions({"suite": "glob", "create": True, "recorded_entries": matched,
                            "entries": {entry: reads([entry, "lib/x.py"]) for entry in matched}})
        applied = fixture.apply(self.write(fixture, "additions.json", change))
        self.assertEqual(0, applied.returncode, applied.stderr)
        stored = fixture.suite("glob")
        self.assertEqual(["tests/c*.py"], stored["entries"])
        self.assertEqual({entry: ["lib/x.py", entry] for entry in matched}, stored["entry_paths"])
        self.assertEqual(["lib/x.py", *matched], stored["paths"])
        self.assert_loads_unchanged(fixture)

    def test_apply_adds_the_key_of_a_new_matched_file_to_a_glob_suite_with_stored_lists(self) -> None:
        listed = ["tests/c1.py", "tests/c2.py", "tests/c3.py"]
        fixture = self.fixture([suite("glob", ["tests/c*.py"], entry_selection=True, paths=listed, entry_paths={entry: [entry] for entry in listed},
                                      entry_extra_paths={"tests/c4.py": ["docs/**"]})])
        change = additions({"suite": "glob", "create": False, "recorded_entries": ["tests/c4.py"],
                            "entries": {"tests/c4.py": reads(["tests/c4.py", "lib/x.py"], [("probe", "any")], ["fixtures"])}})
        applied = fixture.apply(self.write(fixture, "additions.json", change))
        self.assertEqual(0, applied.returncode, applied.stderr)
        stored = fixture.suite("glob")
        self.assertEqual(["tests/c*.py"], stored["entries"])
        self.assertEqual({**{entry: [entry] for entry in listed}, "tests/c4.py": ["lib/x.py", "tests/c4.py"]}, stored["entry_paths"])
        self.assertEqual({"tests/c4.py": [{"path": "probe", "kind": "any"}]}, stored["entry_existence_reads"])
        self.assertEqual({"tests/c4.py": ["fixtures"]}, stored["entry_directory_memberships"])
        self.assertEqual({"tests/c4.py": ["docs/**"]}, stored["entry_extra_paths"])
        self.assert_loads_unchanged(fixture)

    def test_a_full_recording_without_a_result_for_a_matched_file_without_a_list_leaves_it_without_one(self) -> None:
        listed = ["tests/c1.py", "tests/c2.py", "tests/c3.py"]
        fixture = self.fixture([suite("glob", ["tests/c*.py"], entry_selection=True, paths=listed, entry_paths={entry: [entry] for entry in listed})])
        result = {"kind": "full-recording", "suite_list": "suites.json", "entries": [
            {"suite": "glob", "entry": entry, "exit_code": 0, "skipped": 0, "seconds": 1.65, "reads": reads([entry])} for entry in listed]}
        applied = fixture.apply(self.write(fixture, "full.json", result))
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertIn("glob: left tests/c4.py without a stored list: it was not recorded", applied.stdout)
        self.assertNotIn("kept the list of tests/c4.py", applied.stdout)
        self.assertEqual(listed, list(fixture.suite("glob")["entry_paths"]))
        self.assert_loads_unchanged(fixture)

    def stale_suite(self) -> dict:
        """A glob suite whose recorded fields each hold the key of a file that no entry names any more."""
        gone = "tests/gone.py"
        return suite("glob", ["tests/c1.py", "tests/c?.py"], entry_selection=True, paths=["tests/c1.py", "only/gone.txt"],
                     entry_paths={**{f"tests/c{number}.py": [f"tests/c{number}.py"] for number in (1, 2, 3, 4)}, gone: ["only/gone.txt"]},
                     entry_directory_memberships={gone: ["gone-directory"]}, entry_existence_reads={gone: [{"path": "gone-probe", "kind": "any"}]},
                     entry_lookup_directories={gone: ["gone-lookups"]}, entry_durations={"tests/c1.py": 1.0, gone: 3.0},
                     directory_memberships=["gone-directory"], existence_reads=[{"path": "gone-probe", "kind": "any"}],
                     lookup_directories=["gone-lookups"])

    def assert_stale_keys_gone(self, fixture: RecordingFixture, applied) -> None:
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertIn("glob: removed the stored lists and the estimate of tests/gone.py: no entry names it", applied.stdout)
        stored = fixture.suite("glob")
        self.assertEqual(["tests/c1.py", "tests/c?.py"], stored["entries"])
        self.assertEqual({"tests/c1.py", "tests/c2.py", "tests/c3.py", "tests/c4.py"}, set(stored["entry_paths"]))
        for field in ("entry_directory_memberships", "entry_existence_reads", "entry_lookup_directories",
                      "directory_memberships", "existence_reads", "lookup_directories"):
            self.assertNotIn(field, stored)
        self.assertNotIn("tests/gone.py", stored["entry_durations"])
        self.assertNotIn("only/gone.txt", stored["paths"])

    def test_apply_removes_the_keys_of_a_file_that_no_entry_of_a_glob_suite_names_any_more(self) -> None:
        fixture = self.fixture([self.stale_suite()])
        self.assert_stale_keys_gone(fixture, fixture.apply(self.write(fixture, "additions.json", additions())))
        self.assertEqual({"tests/c1.py": 1.0}, fixture.suite("glob")["entry_durations"])
        self.assert_loads_unchanged(fixture)

    def test_the_apply_of_a_full_recording_removes_the_keys_of_a_file_that_no_entry_of_a_glob_suite_names_any_more(self) -> None:
        fixture = self.fixture([self.stale_suite()])
        result = {"kind": "full-recording", "suite_list": "suites.json", "entries": [
            {"suite": "glob", "entry": "tests/c2.py", "exit_code": 0, "skipped": 0, "seconds": 3.3, "reads": reads(["tests/c2.py", "lib/y.py"])}]}
        applied = fixture.apply(self.write(fixture, "full.json", result))
        self.assert_stale_keys_gone(fixture, applied)
        self.assertEqual(["lib/y.py", "tests/c2.py"], fixture.suite("glob")["entry_paths"]["tests/c2.py"])
        self.assertEqual({"tests/c1.py": 1.0, "tests/c2.py": 2.0}, fixture.suite("glob")["entry_durations"])
        self.assert_loads_unchanged(fixture)

    def path_stale_suite(self) -> dict:
        """A suite with only path entries whose recorded fields each hold the key of a file that no entry names any more,
        and whose entry file tests/c2.py has no stored list yet."""
        gone = "tests/gone.py"
        return suite("paths", ["tests/c1.py", "tests/c2.py"], entry_selection=True, paths=["tests/c1.py", "only/gone.txt"],
                     entry_paths={"tests/c1.py": ["tests/c1.py"], gone: ["only/gone.txt"]},
                     entry_directory_memberships={gone: ["gone-directory"]}, entry_existence_reads={gone: [{"path": "gone-probe", "kind": "any"}]},
                     entry_lookup_directories={gone: ["gone-lookups"]}, entry_durations={"tests/c1.py": 1.0, gone: 3.0},
                     directory_memberships=["gone-directory"], existence_reads=[{"path": "gone-probe", "kind": "any"}],
                     lookup_directories=["gone-lookups"])

    def assert_path_stale_keys_gone(self, fixture: RecordingFixture, applied) -> dict:
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertIn("paths: removed the stored lists and the estimate of tests/gone.py: no entry names it", applied.stdout)
        stored = fixture.suite("paths")
        self.assertEqual(["tests/c1.py", "tests/c2.py"], stored["entries"])
        self.assertEqual({"tests/c1.py": ["tests/c1.py"], "tests/c2.py": ["lib/y.py", "tests/c2.py"]}, stored["entry_paths"])
        for field in ("entry_directory_memberships", "entry_existence_reads", "entry_lookup_directories",
                      "directory_memberships", "existence_reads", "lookup_directories"):
            self.assertNotIn(field, stored)
        self.assertNotIn("tests/gone.py", stored["entry_durations"])
        self.assertEqual(["lib/y.py", "tests/c1.py", "tests/c2.py"], stored["paths"])
        self.assert_loads_unchanged(fixture)
        return stored

    def test_apply_of_additions_removes_the_keys_of_a_file_that_no_path_entry_names_and_adds_the_key_of_the_new_entry_file(self) -> None:
        fixture = self.fixture([self.path_stale_suite()])
        change = additions({"suite": "paths", "create": False, "recorded_entries": ["tests/c2.py"],
                            "entries": {"tests/c2.py": reads(["tests/c2.py", "lib/y.py"])}})
        stored = self.assert_path_stale_keys_gone(fixture, fixture.apply(self.write(fixture, "additions.json", change)))
        self.assertEqual({"tests/c1.py": 1.0}, stored["entry_durations"])

    def test_the_apply_of_a_full_recording_removes_the_keys_of_a_file_that_no_path_entry_names(self) -> None:
        fixture = self.fixture([self.path_stale_suite()])
        result = {"kind": "full-recording", "suite_list": "suites.json", "entries": [
            {"suite": "paths", "entry": "tests/c2.py", "exit_code": 0, "skipped": 0, "seconds": 3.3, "reads": reads(["tests/c2.py", "lib/y.py"])}]}
        applied = fixture.apply(self.write(fixture, "full.json", result))
        stored = self.assert_path_stale_keys_gone(fixture, applied)
        self.assertIn("paths: kept the list of tests/c1.py: it was not recorded", applied.stdout)
        self.assertEqual({"tests/c1.py": 1.0, "tests/c2.py": 2.0}, stored["entry_durations"])

    def test_apply_writes_duration_estimates_converted_by_the_wide_to_mid_ratio(self) -> None:
        fixture = self.fixture()
        result = {"kind": "full-recording", "suite_list": "suites.json", "entries": [
            {"suite": "whole", "entry": "tests/a.py", "job": "shard-1", "exit_code": 0, "skipped": 0, "seconds": 33.0, "reads": reads(["tests/a.py"])},
            {"suite": "per-entry", "entry": "tests/c2.py", "job": "shard-2", "exit_code": 0, "skipped": 0, "seconds": 16.5,
             "reads": reads(["tests/c2.py"])}]}
        applied = fixture.apply(self.write(fixture, "full.json", result))
        self.assertEqual(0, applied.returncode, applied.stderr)
        # tests/a.py is the only entry file of its job with a stored estimate, so the job's factor takes up its whole
        # difference; tests/c2.py has none, and its job counts with factor 1.
        self.assertEqual({"tests/a.py": 4.0}, fixture.suite("whole")["entry_durations"])
        self.assertEqual({"tests/c2.py": 10.0}, fixture.suite("per-entry")["entry_durations"])
        self.assertIn("per-entry: kept the list of tests/c1.py: it was not recorded", applied.stdout)
        # The whole suite ran every entry file without a skip: its recorded lists replace the stored ones, extra reads stay.
        whole = fixture.suite("whole")
        self.assertEqual(["tests/a.py"], whole["paths"])
        self.assertNotIn("directory_memberships", whole)
        self.assertEqual(["docs/**"], whole["extra_paths"])
        self.assertEqual([{"path": "Directory.Build.props", "kind": "file"}], whole["extra_existence_reads"])

    def test_apply_keeps_the_stored_estimate_of_an_entry_file_that_did_not_run_clean(self) -> None:
        # tests/a.py stores 4.0; a clean run of 1.65 s would make it 1.0. Each reason keeps the stored estimate instead.
        cases = [({"exit_code": 1, "skipped": 0}, "it exited with 1"),
                 ({"exit_code": 0, "skipped": None, "incomplete_report": "its TAP plan does not match its test lines"},
                  "it printed no complete test report: its TAP plan does not match its test lines"),
                 ({"exit_code": 0, "skipped": None}, "its test runner reports no skip count"),
                 ({"exit_code": 0, "skipped": 2}, "2 tests skipped")]
        for outcome, reason in cases:
            with self.subTest(reason=reason):
                fixture = self.fixture()
                result = {"kind": "full-recording", "suite_list": "suites.json", "entries": [
                    {"suite": "whole", "entry": "tests/a.py", "job": "shard-1", "seconds": 1.65, "reads": reads(["tests/a.py"]), **outcome}]}
                applied = fixture.apply(self.write(fixture, "full.json", result))
                self.assertEqual(0, applied.returncode, applied.stderr)
                self.assertEqual({"tests/a.py": 4.0}, fixture.suite("whole")["entry_durations"])
                self.assertIn(f"whole: kept the estimate of tests/a.py: {reason}", applied.stdout)

    def test_apply_writes_no_estimate_for_an_entry_file_without_one_that_did_not_run_clean(self) -> None:
        fixture = self.fixture()
        result = {"kind": "full-recording", "suite_list": "suites.json", "entries": [
            {"suite": "per-entry", "entry": "tests/c2.py", "job": "shard-1", "exit_code": 1, "skipped": 0, "seconds": 16.5,
             "reads": reads(["tests/c2.py"])}]}
        applied = fixture.apply(self.write(fixture, "full.json", result))
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertNotIn("entry_durations", fixture.suite("per-entry"))
        self.assertNotIn("kept the estimate of tests/c2.py", applied.stdout)

    def test_apply_writes_the_estimate_of_a_clean_entry_file_beside_a_failing_one_of_a_suite_selected_as_a_whole(self) -> None:
        # The failing tests/c1.py keeps the suite's lists and its own estimate, and does not count toward the factor of
        # its job: the job has no clean entry file with a stored estimate, so tests/a.py counts with factor 1. Counting
        # tests/c1.py (1.0 against 4.0) would have made tests/a.py 40.0.
        fixture = self.fixture([suite("whole", ["tests/a.py", "tests/c1.py"], paths=["tests/a.py", "tests/c1.py"],
                                      entry_durations={"tests/c1.py": 4.0})])
        result = {"kind": "full-recording", "suite_list": "suites.json", "entries": [
            {"suite": "whole", "entry": "tests/a.py", "job": "shard-1", "exit_code": 0, "skipped": 0, "seconds": 16.5, "reads": reads(["tests/a.py"])},
            {"suite": "whole", "entry": "tests/c1.py", "job": "shard-1", "exit_code": 1, "skipped": 0, "seconds": 1.65,
             "reads": reads(["tests/c1.py"])}]}
        applied = fixture.apply(self.write(fixture, "full.json", result))
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertEqual({"tests/a.py": 10.0, "tests/c1.py": 4.0}, fixture.suite("whole")["entry_durations"])
        self.assertIn("whole: kept the list of tests/c1.py: it exited with 1", applied.stdout)
        self.assertIn("whole: kept the estimate of tests/c1.py: it exited with 1", applied.stdout)
        self.assertNotIn("kept the estimate of tests/a.py", applied.stdout)

    def test_apply_writes_the_lists_of_a_suite_selected_per_entry_as_the_union_of_its_entry_lists(self) -> None:
        # The suite's own list holds nothing beyond its entry files' recorded lists, for additions and full recordings alike.
        fixture = self.fixture([suite("per-entry", ["tests/c1.py", "tests/c2.py"], entry_selection=True, paths=["stale/**"],
                                      entry_paths={"tests/c1.py": ["tests/c1.py"], "tests/c2.py": ["tests/c2.py", "tests/c1.py"]},
                                      entry_extra_paths={"tests/c2.py": ["docs/**"]}, directory_memberships=["stale"],
                                      existence_reads=[{"path": "stale.txt", "kind": "any"}])])
        change = additions({"suite": "per-entry", "create": False, "recorded_entries": ["tests/c1.py"],
                            "entries": {"tests/c1.py": reads(["lib/x.py"], [("probe", "any")], ["fixtures"])}})
        applied = fixture.apply(self.write(fixture, "additions.json", change))
        self.assertEqual(0, applied.returncode, applied.stderr)
        joined = fixture.suite("per-entry")
        self.assertEqual(["lib/x.py", "tests/c1.py", "tests/c2.py"], joined["paths"])
        self.assertEqual(["fixtures"], joined["directory_memberships"])
        self.assertEqual([{"path": "probe", "kind": "any"}], joined["existence_reads"])
        result = {"kind": "full-recording", "suite_list": "suites.json", "entries": [
            {"suite": "per-entry", "entry": entry, "exit_code": 0, "skipped": 0, "seconds": 1.0, "reads": reads([entry, "lib/y.py"])}
            for entry in ("tests/c1.py", "tests/c2.py")]}
        applied = fixture.apply(self.write(fixture, "full.json", result))
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertEqual(["lib/y.py", "tests/c1.py", "tests/c2.py"], fixture.suite("per-entry")["paths"])
        self.assertNotIn("directory_memberships", fixture.suite("per-entry"))
        self.assertNotIn("existence_reads", fixture.suite("per-entry"))
        self.assertEqual({"tests/c2.py": ["docs/**"]}, fixture.suite("per-entry")["entry_extra_paths"])

    def test_apply_stores_lookup_directories_per_entry_file_and_module_lookups_per_directory_of_the_suite_list(self) -> None:
        fixture = self.fixture()
        lookups = lambda directories, names: {**reads(), "lookup_directories": directories, "module_lookups": names}  # noqa: E731
        change = additions({"suite": "per-entry", "create": False, "recorded_entries": ["tests/c1.py", "tests/c2.py"],
                            "entries": {"tests/c1.py": lookups(["tests"], {"tests": ["helper"]}),
                                        "tests/c2.py": lookups(["lib", "tests"], {"lib": ["absent"], "tests": ["other"]})}})
        applied = fixture.apply(self.write(fixture, "additions.json", change))
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertEqual({"tests/c1.py": ["tests"], "tests/c2.py": ["lib", "tests"]}, fixture.suite("per-entry")["entry_lookup_directories"])
        self.assertEqual(["lib", "tests"], fixture.suite("per-entry")["lookup_directories"])
        self.assertEqual({"lib": ["absent"], "tests": ["helper", "other"]}, fixture.suites()["module_lookups"])

        def full(*entries: tuple[str, dict]) -> dict:
            return {"kind": "full-recording", "suite_list": "suites.json", "entries": [
                {"suite": "per-entry", "entry": entry, "exit_code": 0, "skipped": 0, "seconds": 1.0, "reads": {**reads([entry]), **found}}
                for entry, found in entries]}

        # tests/c2.py keeps its lists, so the directories it looks up modules in keep their stored module lookups.
        applied = fixture.apply(self.write(fixture, "full-one.json", full(("tests/c1.py", lookups(["tests"], {"tests": ["fresh"]})))))
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertEqual({"lib": ["absent"], "tests": ["fresh", "helper", "other"]}, fixture.suites()["module_lookups"])
        applied = fixture.apply(self.write(fixture, "full-both.json", full(("tests/c1.py", lookups(["tests"], {"tests": ["fresh"]})),
                                                                           ("tests/c2.py", lookups(["lib"], {})))))
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertEqual({"tests": ["fresh"]}, fixture.suites()["module_lookups"])
        self.assertEqual({"tests/c1.py": ["tests"], "tests/c2.py": ["lib"]}, fixture.suite("per-entry")["entry_lookup_directories"])

    def test_two_applies_of_the_same_results_in_another_order_write_the_same_suite_list(self) -> None:
        # The rendered order follows neither the order of the results nor the order of the applies: recorded lists are
        # sorted, and the per-entry fields follow the suite's entry files.
        lookups = {"lookup_directories": ["tests"], "module_lookups": {"tests": ["zeta"]}}
        first = additions({"suite": "whole", "create": False, "recorded_entries": ["tests/a.py"],
                           "reads": {**reads(["z.txt", "b.txt"], [("z-probe", "any")], ["zone"]), **lookups}},
                          {"suite": "per-entry", "create": False, "recorded_entries": ["tests/c2.py"],
                           "entries": {"tests/c2.py": reads(["z.txt"], [("z-probe", "any")], ["zone"])}})
        second = additions({"suite": "whole", "create": False, "recorded_entries": ["tests/a.py"],
                            "reads": {**reads(["a.txt"], [("a-probe", "file")], ["area"]), "lookup_directories": ["lib"],
                                      "module_lookups": {"tests": ["alpha"]}}},
                           {"suite": "per-entry", "create": False, "recorded_entries": ["tests/c1.py"],
                            "entries": {"tests/c1.py": reads(["a.txt"], [("a-probe", "file")], ["area"])}})
        full = {"kind": "full-recording", "suite_list": "suites.json", "entries": [
            {"suite": "per-entry", "entry": entry, "exit_code": 0, "skipped": 0, "seconds": seconds, "reads": reads(paths)}
            for entry, seconds, paths in (("tests/c2.py", 2.0, ["z.txt", "tests/c2.py"]), ("tests/c1.py", 1.0, ["tests/c1.py", "a.txt"]))]}
        written = []
        for order in ((first, second), (second, first)):
            fixture = self.fixture()
            files = [self.write(fixture, f"additions-{index}.json", document) for index, document in enumerate(order)]
            for path in files:
                applied = fixture.apply(path)
                self.assertEqual(0, applied.returncode, applied.stderr)
            applied = fixture.apply(*reversed(files))
            self.assertEqual(0, applied.returncode, applied.stderr)
            once = (fixture.root / "suites.json").read_text(encoding="utf-8")
            applied = fixture.apply(self.write(fixture, "full.json", full))
            self.assertEqual(0, applied.returncode, applied.stderr)
            written.append((once, (fixture.root / "suites.json").read_text(encoding="utf-8")))
        self.assertEqual(written[0], written[1])
        whole = json.loads(written[0][0])["suites"][0]
        self.assertEqual(["a.txt", "b.txt", "tests/a.py", "z.txt"], whole["paths"])
        self.assertEqual([{"path": "a-probe", "kind": "file"}, {"path": "z-probe", "kind": "any"}], whole["existence_reads"])
        self.assertEqual(["area", "lib", "zone"], whole["directory_memberships"])
        self.assertEqual(["lib", "tests"], whole["lookup_directories"])
        self.assertEqual({"tests": ["alpha", "zeta"]}, json.loads(written[0][0])["module_lookups"])
        per_entry = json.loads(written[0][1])["suites"][1]
        self.assertEqual(["tests/c1.py", "tests/c2.py"], list(per_entry["entry_durations"]))
        self.assertEqual({"tests/c1.py": ["a.txt", "tests/c1.py"], "tests/c2.py": ["tests/c2.py", "z.txt"]}, per_entry["entry_paths"])

    def test_apply_joins_the_full_recording_results_of_every_shard_and_the_windows_job(self) -> None:
        # The closing job of the full recording applies one result per Linux shard and one from the Windows job; a suite
        # selected as a whole whose entry files ran in different shards is replaced from the union of its results. The
        # Linux seconds are wide-traced and count at 1/1.65; the Windows job has one ETW set, so its seconds count as
        # recorded. No job has an entry file with a stored estimate, so each counts with factor 1. These results name no
        # job, as those of a full-recording workflow that passes none to start: each file counts as one job.
        fixture = self.fixture([suite("whole", ["tests/a.py", "tests/c1.py"], paths=["tests/a.py", "stale.txt"]),
                                suite("windows", ["tests/w.py"], os="windows", paths=["tests/w.py", "stale.txt"])])

        def result(suite_name: str, entry: str, seconds: float, paths: list[str]) -> dict:
            return {"kind": "full-recording", "suite_list": "suites.json", "entries": [
                {"suite": suite_name, "entry": entry, "exit_code": 0, "skipped": 0, "seconds": seconds, "reads": reads(paths)}]}

        shards = [self.write(fixture, "shard-1.json", result("whole", "tests/a.py", 3.3, ["tests/a.py"])),
                  self.write(fixture, "shard-2.json", result("whole", "tests/c1.py", 6.6, ["tests/c1.py"])),
                  self.write(fixture, "windows.json", result("windows", "tests/w.py", 16.5, ["tests/w.py"]))]
        applied = fixture.apply(*shards)
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertEqual(["tests/a.py", "tests/c1.py"], fixture.suite("whole")["paths"])
        self.assertEqual({"tests/a.py": 2.0, "tests/c1.py": 4.0}, fixture.suite("whole")["entry_durations"])
        self.assertEqual(["tests/w.py"], fixture.suite("windows")["paths"])
        self.assertEqual({"tests/w.py": 16.5}, fixture.suite("windows")["entry_durations"])

    def test_an_additions_document_of_the_wrong_shape_fails_naming_the_file_and_changes_nothing(self) -> None:
        valid = {"suite": "whole", "create": False, "recorded_entries": ["tests/a.py"], "reads": reads(["x.txt"])}
        for shape, document in (
                ("no suites", {"kind": "additions", "suite_list": "suites.json"}),
                ("suites no list", {"kind": "additions", "suite_list": "suites.json", "suites": {"whole": valid}}),
                ("a change no object", additions("whole")),
                ("no suite name", additions({**valid, "suite": 3})),
                ("create no boolean", additions({**valid, "create": "yes"})),
                ("no recorded entries", additions({key: value for key, value in valid.items() if key != "recorded_entries"})),
                ("a recorded entry no string", additions({**valid, "recorded_entries": [["tests/a.py"]]})),
                ("reads no object", additions({**valid, "reads": ["x.txt"]})),
                ("an unknown read field", additions({**valid, "reads": {"paths": [], "commands": ["rm -rf /"]}})),
                ("a path no string", additions({**valid, "reads": {"paths": [{"path": "x.txt"}]}})),
                ("an existence read without kind", additions({**valid, "reads": {"existence_reads": [{"path": "x"}]}})),
                ("module lookups no object", additions({**valid, "reads": {"module_lookups": ["x"]}})),
                ("a module name no string", additions({**valid, "reads": {"module_lookups": {".": [1]}}})),
                ("entries no object", additions({**valid, "entries": [reads(["x.txt"])]})),
                ("an entry's reads no object", additions({**valid, "entries": {"tests/a.py": "x.txt"}})),
                ("an unknown change field", additions({**valid, "command": "echo"}))):
            with self.subTest(shape=shape):
                fixture = self.fixture()
                before = (fixture.root / "suites.json").read_text(encoding="utf-8")
                path = self.write(fixture, "additions.json", document)
                refused = fixture.apply(path)
                self.assertEqual(2, refused.returncode, refused.stderr)
                self.assertIn(f"{path} is no valid additions document:", refused.stderr)
                self.assertNotIn("Traceback", refused.stderr)
                self.assertEqual(before, (fixture.root / "suites.json").read_text(encoding="utf-8"))


SELECTS = "the change since the recorded commit selects it"
UNSELECTED = "the change since the recorded commit could not be selected"


def clean(suite_name: str, entry: str, paths: list[str], seconds: float = 1.0) -> dict:
    return {"suite": suite_name, "entry": entry, "exit_code": 0, "skipped": 0, "seconds": seconds, "reads": reads(paths)}


@requires_linux
class AppliedBackslashTests(unittest.TestCase):
    def test_the_applied_existence_reads_with_a_backslash_are_no_additions_on_the_next_run(self) -> None:
        # tests/a.py probes the absent D:\Games\Example\game.exe and the directory tools\cache from the checkout root.
        for per_entry in (False, True):
            with self.subTest(per_entry=per_entry):
                fields = {"entry_selection": True, "entry_paths": {"tests/a.py": ["tests/a.py"]}} if per_entry else {}
                fixture = BACKSLASHES.fixture(self.addCleanup, [suite("a", ["tests/a.py"], paths=["tests/a.py"], **fields)])
                first = fixture.record(["tests/a.py"])
                self.assertEqual(1, first.returncode, first.stdout + first.stderr)
                applied = fixture.apply()
                self.assertEqual(0, applied.returncode, applied.stderr)
                self.assertEqual(BACKSLASH_READS, fixture.suite("a")["existence_reads"])
                if per_entry:
                    self.assertEqual({"tests/a.py": BACKSLASH_READS}, fixture.suite("a")["entry_existence_reads"])
                fixture.git("commit", "-q", "-am", "lists")
                second = fixture.record(["tests/a.py"])
                self.assertEqual(0, second.returncode, second.stdout + second.stderr)
                self.assertEqual([], fixture.additions()["suites"])


class KeptListTests(unittest.TestCase):
    """apply on the suite list of a newer commit: the entry files that the selection of the change since the recorded
    commit selects keep their stored reads, and results the suite list no longer holds are left out."""

    def fixture(self, suite_list: list[dict] | None = None) -> RecordingFixture:
        return RecordingFixture(self.addCleanup, FILES, suite_list or [
            suite("whole", ["tests/a.py"], paths=["tests/a.py", "added.txt"], entry_durations={"tests/a.py": 4.0}),
            suite("per-entry", ["tests/c1.py", "tests/c2.py"], entry_selection=True, paths=[],
                  entry_paths={"tests/c1.py": ["tests/c1.py", "added.txt"], "tests/c2.py": ["tests/c2.py", "stale.txt"]})])

    def write(self, fixture: RecordingFixture, name: str, text: str):
        path = fixture.base / name
        path.write_text(text, encoding="utf-8")
        return path

    def result(self, fixture: RecordingFixture, *entries: dict):
        return self.write(fixture, "full.json", json.dumps({"kind": "full-recording", "suite_list": "suites.json", "entries": list(entries)}))

    def selection(self, fixture: RecordingFixture, suites: dict, entries: dict):
        """The output of select --json-output for the change since the recorded commit."""
        return self.write(fixture, "selection.txt", f"suites={json.dumps(suites)}\nentries={json.dumps(entries)}\nshards=[]\nwindows=\n")

    def test_a_selected_entry_file_that_ran_clean_keeps_its_stored_reads_beside_the_recorded_ones(self) -> None:
        fixture = self.fixture()
        selected = self.selection(fixture, {"whole": "skip", "per-entry": "run"}, {"per-entry": ["tests/c1.py"]})
        applied = fixture.run("apply", "--keep-lists", str(selected), str(self.result(
            fixture, clean("per-entry", "tests/c1.py", ["tests/c1.py", "new.txt"]), clean("per-entry", "tests/c2.py", ["tests/c2.py"]),
            clean("whole", "tests/a.py", ["tests/a.py"]))))
        self.assertEqual(0, applied.returncode, applied.stderr)
        stored = fixture.suite("per-entry")["entry_paths"]
        self.assertEqual(["added.txt", "new.txt", "tests/c1.py"], stored["tests/c1.py"])
        self.assertIn(f"per-entry: kept the list of tests/c1.py: {SELECTS}", applied.stdout)
        # Not selected: replaced as before.
        self.assertEqual(["tests/c2.py"], stored["tests/c2.py"])
        self.assertEqual(["tests/a.py"], fixture.suite("whole")["paths"])
        self.assertNotIn("tests/c2.py", applied.stdout)
        self.assertNotIn("tests/a.py", applied.stdout)

    def test_every_entry_file_of_a_suite_selected_as_a_whole_keeps_its_stored_reads(self) -> None:
        # whole is selected as a whole; per-entry runs without a list of its own in entries, which counts as a whole too.
        for entries in ({}, {"per-entry": ["tests/c1.py", "tests/c2.py"]}):
            with self.subTest(entries=entries):
                fixture = self.fixture()
                selected = self.selection(fixture, {"whole": "run", "per-entry": "run"}, entries)
                applied = fixture.run("apply", "--keep-lists", str(selected), str(self.result(
                    fixture, clean("whole", "tests/a.py", ["tests/a.py", "new.txt"]), clean("per-entry", "tests/c1.py", ["tests/c1.py"]),
                    clean("per-entry", "tests/c2.py", ["tests/c2.py"]))))
                self.assertEqual(0, applied.returncode, applied.stderr)
                self.assertEqual(["added.txt", "new.txt", "tests/a.py"], fixture.suite("whole")["paths"])
                self.assertEqual({"tests/c1.py": ["added.txt", "tests/c1.py"], "tests/c2.py": ["stale.txt", "tests/c2.py"]},
                                 fixture.suite("per-entry")["entry_paths"])
                for name, entry in (("whole", "tests/a.py"), ("per-entry", "tests/c1.py"), ("per-entry", "tests/c2.py")):
                    self.assertIn(f"{name}: kept the list of {entry}: {SELECTS}", applied.stdout)

    def test_without_a_selection_every_entry_file_keeps_its_stored_reads(self) -> None:
        fixture = self.fixture()
        applied = fixture.run("apply", "--keep-all-lists", str(self.result(
            fixture, clean("whole", "tests/a.py", ["tests/a.py", "new.txt"]), clean("per-entry", "tests/c1.py", ["tests/c1.py"]),
            clean("per-entry", "tests/c2.py", ["tests/c2.py"]))))
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertEqual(["added.txt", "new.txt", "tests/a.py"], fixture.suite("whole")["paths"])
        self.assertEqual({"tests/c1.py": ["added.txt", "tests/c1.py"], "tests/c2.py": ["stale.txt", "tests/c2.py"]},
                         fixture.suite("per-entry")["entry_paths"])
        for name, entry in (("whole", "tests/a.py"), ("per-entry", "tests/c1.py"), ("per-entry", "tests/c2.py")):
            self.assertIn(f"{name}: kept the list of {entry}: {UNSELECTED}", applied.stdout)

    def test_a_kept_entry_file_that_ran_clean_gets_its_duration_estimate_as_before(self) -> None:
        # tests/c2.py has no stored estimate and its job no clean entry file with one: factor 1, 16.5 / 1.65 = 10.0.
        for option in (["--keep-all-lists"], ["--keep-lists", "selection"]):
            with self.subTest(option=option[0]):
                fixture = self.fixture()
                if option[0] == "--keep-lists":
                    option = ["--keep-lists", str(self.selection(fixture, {"whole": "skip", "per-entry": "run"}, {"per-entry": ["tests/c2.py"]}))]
                applied = fixture.run("apply", *option, str(self.result(fixture, clean("per-entry", "tests/c2.py", ["tests/c2.py"], 16.5))))
                self.assertEqual(0, applied.returncode, applied.stderr)
                self.assertEqual({"tests/c2.py": 10.0}, fixture.suite("per-entry")["entry_durations"])
                self.assertNotIn("kept the estimate of tests/c2.py", applied.stdout)

    def test_results_for_a_suite_or_an_entry_file_the_suite_list_lacks_are_left_out_and_reported(self) -> None:
        fixture = self.fixture()
        applied = fixture.apply(self.result(
            fixture, clean("gone", "tests/a.py", ["tests/a.py"]), clean("per-entry", "tests/c3.py", ["tests/c3.py"]),
            clean("per-entry", "tests/c1.py", ["tests/c1.py"])))
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertIn("left out the results of the suite gone: the suite list lacks it", applied.stdout)
        self.assertIn("per-entry: left out the result of tests/c3.py: the suite list lacks it", applied.stdout)
        self.assertEqual(["tests/c1.py"], fixture.suite("per-entry")["entry_paths"]["tests/c1.py"])
        self.assertNotIn("tests/c3.py", json.dumps(fixture.suites()))
        self.assertEqual({"whole", "per-entry"}, {item["name"] for item in fixture.suites()["suites"]})

    def test_the_kept_lists_go_with_full_recording_results_only(self) -> None:
        fixture = self.fixture()
        change = self.write(fixture, "additions.json", json.dumps(additions(
            {"suite": "whole", "create": False, "recorded_entries": ["tests/a.py"], "reads": reads(["x.txt"])})))
        refused = fixture.run("apply", "--keep-all-lists", str(change))
        self.assertEqual(2, refused.returncode)
        self.assertIn("--keep-lists and --keep-all-lists go with full-recording results", refused.stderr)


class JobCorrectionTests(unittest.TestCase):
    """apply corrects each measurement of a full recording for the speed of its shard job, and changes a stored duration
    estimate only when the corrected value differs from it by more than 25%."""

    STORED = {"tests/c1.py": 10.0, "tests/c2.py": 20.0, "tests/c3.py": 10.0}

    def fixture(self, linux: dict[str, float] | None = None, windows: dict[str, float] | None = None) -> RecordingFixture:
        entries = ["tests/c1.py", "tests/c2.py", "tests/c3.py", "tests/c4.py"]
        return RecordingFixture(self.addCleanup, FILES, [
            suite("linux", entries, entry_selection=True, paths=[], entry_paths={entry: [entry] for entry in entries},
                  **({"entry_durations": linux} if linux else {})),
            suite("windows", ["tests/w.py"], os="windows", paths=["tests/w.py"], **({"entry_durations": windows} if windows else {}))])

    def apply(self, fixture: RecordingFixture, *measurements: tuple[str, str, str, float]) -> tuple[dict | None, dict | None]:
        """Apply one full-recording result per job from (suite, entry, job, seconds); return the stored estimates of
        the Linux and the Windows suite."""
        jobs: dict[str, list[dict]] = {}
        for suite_name, entry, job, seconds in measurements:
            jobs.setdefault(job, []).append({"suite": suite_name, "entry": entry, "job": job, "exit_code": 0, "skipped": 0,
                                             "seconds": seconds, "reads": reads([entry])})
        paths = []
        for job, entries in jobs.items():
            path = fixture.base / f"{job}.json"
            path.write_text(json.dumps({"kind": "full-recording", "suite_list": "suites.json", "entries": entries}), encoding="utf-8")
            paths.append(path)
        applied = fixture.apply(*paths)
        self.assertEqual(0, applied.returncode, applied.stderr)
        return fixture.suite("linux").get("entry_durations"), fixture.suite("windows").get("entry_durations")

    def test_a_job_that_ran_every_entry_file_faster_or_slower_leaves_the_stored_estimates(self) -> None:
        stored = {**self.STORED, "tests/c4.py": 8.0}
        fixture = self.fixture(stored)
        linux, _ = self.apply(fixture, ("linux", "tests/c1.py", "shard-1", WIDE_TO_MID * 1.3 * 10.0), ("linux", "tests/c2.py", "shard-1", WIDE_TO_MID * 1.3 * 20.0),
                              ("linux", "tests/c3.py", "shard-2", WIDE_TO_MID * 0.8 * 10.0), ("linux", "tests/c4.py", "shard-2", WIDE_TO_MID * 0.8 * 8.0))
        self.assertEqual(stored, linux)

    def test_an_entry_file_that_moved_by_more_than_a_quarter_against_its_job_gets_its_corrected_value(self) -> None:
        fixture = self.fixture({**self.STORED, "tests/c4.py": 8.0})
        # The job ran at 1.2x: tests/c3.py's corrected value is 13.337 s, 33% above its stored 10 s.
        linux, _ = self.apply(fixture, ("linux", "tests/c1.py", "shard-1", WIDE_TO_MID * 1.2 * 10.0), ("linux", "tests/c2.py", "shard-1", WIDE_TO_MID * 1.2 * 20.0),
                              ("linux", "tests/c3.py", "shard-1", WIDE_TO_MID * 1.2 * 13.337))
        self.assertEqual({"tests/c1.py": 10.0, "tests/c2.py": 20.0, "tests/c3.py": 13.34, "tests/c4.py": 8.0}, linux)

    def test_the_threshold_compares_the_corrected_value_before_rounding(self) -> None:
        # 12.504 s is 25.04% above the stored 10 s, so the estimate changes, to 12.5 s.
        fixture = self.fixture(dict(self.STORED))
        linux, _ = self.apply(fixture, ("linux", "tests/c1.py", "shard-1", WIDE_TO_MID * 10.0), ("linux", "tests/c2.py", "shard-1", WIDE_TO_MID * 20.0),
                              ("linux", "tests/c3.py", "shard-1", WIDE_TO_MID * 12.504))
        self.assertEqual({**self.STORED, "tests/c3.py": 12.5}, linux)

    def test_an_entry_file_within_a_quarter_of_its_stored_estimate_keeps_it(self) -> None:
        for corrected in (12.49, 7.51, 12.0, 7.6):
            with self.subTest(corrected=corrected):
                fixture = self.fixture(dict(self.STORED))
                linux, _ = self.apply(fixture, ("linux", "tests/c1.py", "shard-1", WIDE_TO_MID * 1.2 * 10.0),
                                      ("linux", "tests/c2.py", "shard-1", WIDE_TO_MID * 1.2 * 20.0), ("linux", "tests/c3.py", "shard-1", WIDE_TO_MID * 1.2 * corrected))
                self.assertEqual(self.STORED, linux)

    def test_a_job_without_a_stored_estimate_counts_with_factor_1(self) -> None:
        fixture = self.fixture({"tests/c1.py": 10.0}, {"tests/w.py": 30.0})
        # shard-1 has a stored estimate and runs at 1.5x; shard-2 has none.
        linux, windows = self.apply(fixture, ("linux", "tests/c1.py", "shard-1", WIDE_TO_MID * 1.5 * 10.0),
                                    ("linux", "tests/c2.py", "shard-2", WIDE_TO_MID * 7.0), ("linux", "tests/c3.py", "shard-2", WIDE_TO_MID * 9.0))
        self.assertEqual({"tests/c1.py": 10.0, "tests/c2.py": 7.0, "tests/c3.py": 9.0}, linux)
        self.assertEqual({"tests/w.py": 30.0}, windows)

    def test_a_job_whose_median_ratio_is_0_counts_with_factor_1(self) -> None:
        # Two of the three entry files with a stored estimate measured 0 s, so the median ratio is 0.
        fixture = self.fixture(dict(self.STORED))
        linux, _ = self.apply(fixture, ("linux", "tests/c1.py", "shard-1", 0.0), ("linux", "tests/c2.py", "shard-1", 0.0),
                              ("linux", "tests/c3.py", "shard-1", WIDE_TO_MID * 10.0), ("linux", "tests/c4.py", "shard-1", WIDE_TO_MID * 5.0))
        self.assertEqual({"tests/c1.py": 0.0, "tests/c2.py": 0.0, "tests/c3.py": 10.0, "tests/c4.py": 5.0}, linux)

    def test_an_entry_file_without_a_stored_estimate_gets_its_corrected_value(self) -> None:
        fixture = self.fixture(dict(self.STORED))
        linux, windows = self.apply(fixture, ("linux", "tests/c1.py", "shard-1", WIDE_TO_MID * 1.2 * 10.0), ("linux", "tests/c2.py", "shard-1", WIDE_TO_MID * 1.2 * 20.0),
                                    ("linux", "tests/c4.py", "shard-1", WIDE_TO_MID * 1.2 * 7.0), ("windows", "tests/w.py", "windows", 12.345))
        self.assertEqual({**self.STORED, "tests/c4.py": 7.0}, linux)
        # The Windows job has no stored estimate: its measurement counts at its recorded seconds, rounded to hundredths.
        self.assertEqual({"tests/w.py": 12.35}, windows)

    def test_an_entry_file_that_was_not_recorded_keeps_its_stored_estimate(self) -> None:
        fixture = self.fixture({**self.STORED, "tests/c4.py": 8.0})
        linux, _ = self.apply(fixture, ("linux", "tests/c1.py", "shard-1", WIDE_TO_MID * 30.0))
        self.assertEqual({**self.STORED, "tests/c4.py": 8.0}, linux)

    def test_the_conversion_divides_by_the_wide_to_mid_ratio_on_linux_and_by_1_on_windows_before_the_job_correction(self) -> None:
        # The converted values of the entry files with a stored estimate are 1.0, 2.0 and 2.49 times those estimates, so
        # the job's factor is 2.0; each converted value is divided by it, and tests/c3.py's 12.45 s stays within 25%. The Windows job has no stored estimate and
        # shows the conversion alone.
        fixture = self.fixture({"tests/c1.py": 10.0, "tests/c2.py": 10.0, "tests/c3.py": 10.0})
        linux, windows = self.apply(fixture, ("linux", "tests/c1.py", "shard-1", WIDE_TO_MID * 10.0), ("linux", "tests/c2.py", "shard-1", WIDE_TO_MID * 20.0),
                                    ("linux", "tests/c3.py", "shard-1", WIDE_TO_MID * 24.9), ("linux", "tests/c4.py", "shard-1", WIDE_TO_MID * 30.0),
                                    ("windows", "tests/w.py", "windows", 16.5))
        self.assertEqual({"tests/c1.py": 5.0, "tests/c2.py": 10.0, "tests/c3.py": 10.0, "tests/c4.py": 15.0}, linux)
        self.assertEqual({"tests/w.py": 16.5}, windows)

    def test_an_entry_file_that_keeps_its_estimate_does_not_count_toward_its_job_factor(self) -> None:
        # shard-1 ran at 1.2x: tests/c1.py ran clean, tests/c2.py exited with 1 after 0.1x its stored time, and tests/c4.py
        # has no stored estimate. The factor comes from tests/c1.py alone, so tests/c4.py's corrected value is 7.0 s;
        # counting tests/c2.py would have made the factor 0.65 and tests/c4.py 12.92 s. tests/c3.py was not recorded.
        fixture = self.fixture(dict(self.STORED))
        outcomes = [("tests/c1.py", 0, 1.2 * 10.0), ("tests/c2.py", 1, 0.1 * 20.0), ("tests/c4.py", 0, 1.2 * 7.0)]
        path = fixture.base / "shard-1.json"
        path.write_text(json.dumps({"kind": "full-recording", "suite_list": "suites.json", "entries": [
            {"suite": "linux", "entry": entry, "job": "shard-1", "exit_code": exit_code, "skipped": 0, "seconds": WIDE_TO_MID * seconds,
             "reads": reads([entry])} for entry, exit_code, seconds in outcomes]}), encoding="utf-8")
        applied = fixture.apply(path)
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertEqual({"tests/c1.py": 10.0, "tests/c2.py": 20.0, "tests/c3.py": 10.0, "tests/c4.py": 7.0}, fixture.suite("linux")["entry_durations"])
        self.assertIn("linux: kept the estimate of tests/c2.py: it exited with 1", applied.stdout)
        self.assertIn("linux: kept the estimate of tests/c3.py: it was not recorded", applied.stdout)
        self.assertNotIn("kept the estimate of tests/c1.py", applied.stdout)
        self.assertNotIn("kept the estimate of tests/c4.py", applied.stdout)


class SuiteListMoveTests(unittest.TestCase):
    """The four moves of a suite between its choices, as the consumer kit of the suite selection states them: each case
    makes the kit's edit to a fixture suite list, reads what the next run runs from ``select`` and what it records from
    ``apply``."""

    A, B, HELPER, NEW = "checks/test_a.py", "checks/test_b.py", "checks/helper.py", "checks/test_new.py"
    TRACKED = {A: "", B: "", "lib/a.py": "", "lib/b.py": ""}
    UNION = [A, B, "lib/a.py", "lib/b.py"]
    ENTRY_PATHS = {A: [A, "lib/a.py"], B: [B, "lib/b.py"]}
    DURATIONS = {A: 40.0, B: 30.0}  # two entry files and a sum of a minute or more: the sign of selection per entry file
    SHORT = {A: 20.0, B: 10.0}  # a sum under one minute: a sign of selection as a whole
    SKIP_WHOLE = "unit: skip (no changed file matches its path list)"
    SKIP_PER_ENTRY = "unit: skip (no changed file matches the path list of any of its entry files)"

    def fixture(self, before: dict, files: dict[str, str] | None = None) -> RecordingFixture:
        return RecordingFixture(self.addCleanup, files or self.TRACKED, [before])

    def select(self, fixture: RecordingFixture, *arguments: str) -> tuple[int, str, list[str]]:
        """The exit code of ``select``, the line of the suite in its report or else its error, and the entry files that run."""
        outputs = fixture.base / "outputs.txt"
        outputs.write_text("", encoding="utf-8")
        report, error = io.StringIO(), io.StringIO()
        with redirect_stdout(report), redirect_stderr(error):
            code = load_recording().suite_selection.main(["select", "--root", str(fixture.root), "--suites", str(fixture.root / "suites.json"),
                                                          "--json-output", str(outputs), *arguments])
        if code != 0:
            return code, error.getvalue().strip(), []
        values = dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines())
        entries = sorted(item["entry"] for shard in json.loads(values["shards"]) for item in shard["entries"])
        return 0, next(line for line in report.getvalue().splitlines() if line.startswith("unit: ")), entries

    def commit_and_select(self, fixture: RecordingFixture, after: dict | None = None, files: dict[str, str] | None = None) -> tuple[int, str, list[str]]:
        """Commit the edited suite list and the added files, and select as the run of that push does."""
        base = fixture.git("rev-parse", "HEAD").strip()
        for path, text in (files or {}).items():
            (fixture.root / path).write_text(text, encoding="utf-8")
        if after is not None:
            fixture.write_suites([after])
        fixture.git("add", "-A")
        fixture.git("commit", "-q", "-m", "change")
        return self.select(fixture, "--base", base, "--head", fixture.git("rev-parse", "HEAD").strip(), "--compare", "direct")

    def apply(self, fixture: RecordingFixture, *changes: dict) -> str:
        path = fixture.base / "additions.json"
        path.write_text(json.dumps(additions(*changes)), encoding="utf-8")
        applied = fixture.apply(path)
        self.assertEqual(0, applied.returncode, applied.stderr)
        return applied.stdout

    def with_helper(self, **fields) -> dict:
        """A suite selected per entry file whose pattern also matches a helper, which so has stored lists of its own."""
        return suite("unit", ["checks/*.py"], entry_selection=True, paths=sorted([*self.UNION, self.HELPER]),
                     entry_paths={**self.ENTRY_PATHS, self.HELPER: [self.HELPER]}, **fields)

    def per_entry(self, entries: list[str], durations: dict[str, float] = DURATIONS, **fields) -> dict:
        return suite("unit", entries, entry_selection=True, paths=self.UNION, entry_paths=self.ENTRY_PATHS, entry_durations=durations, **fields)

    def test_path_to_glob_replaces_the_paths_by_the_pattern_and_the_next_run_runs_and_records_nothing(self) -> None:
        before = self.per_entry([self.A, self.B])
        after = dict(before, entries=["checks/test_*.py"])
        fixture = self.fixture(before)
        self.assertEqual((0, self.SKIP_PER_ENTRY, []), self.commit_and_select(fixture, after))
        self.assertEqual("No additions to apply.\n", self.apply(fixture))
        self.assertEqual(after, fixture.suite("unit"), "the pattern stays and the stored lists keep their keys, one per entry file")
        # From then on a test file that the pattern matches runs from the push that adds it.
        code, line, entries = self.commit_and_select(fixture, files={self.NEW: ""})
        self.assertEqual((0, [self.NEW]), (code, entries))
        self.assertIn(f"`{self.NEW}` runs on every change: no stored path list", line)

    def test_glob_to_path_names_each_entry_file_and_drops_the_keys_of_the_file_that_is_none(self) -> None:
        before = self.with_helper(entry_durations={**self.DURATIONS, self.HELPER: 1.0})
        after = dict(before, entries=[self.A, self.B], entry_paths=self.ENTRY_PATHS, entry_durations=self.DURATIONS)
        fixture = self.fixture(before, {**self.TRACKED, self.HELPER: ""})
        self.assertEqual((0, self.SKIP_PER_ENTRY, []), self.commit_and_select(fixture, after))
        self.assertEqual("No additions to apply.\n", self.apply(fixture))
        self.assertEqual(dict(after, paths=self.UNION), fixture.suite("unit"), "the suite's list is the union of the lists of its entry files again")
        # From then on a new test file runs only once the list names it.
        self.assertEqual((0, self.SKIP_PER_ENTRY, []), self.commit_and_select(fixture, files={self.NEW: ""}))
        code, line, entries = self.commit_and_select(fixture, dict(fixture.suite("unit"), entries=[self.A, self.B, self.NEW],
                                                      entry_paths={**self.ENTRY_PATHS, self.NEW: [self.NEW]}))
        self.assertEqual((0, [self.NEW]), (code, entries))
        self.assertIn(f"`{self.NEW}` is new in `suites.json`", line)

    def test_the_two_moves_of_naming_run_and_record_nothing_in_a_suite_selected_as_a_whole_either(self) -> None:
        with_helper = sorted([*self.UNION, self.HELPER])
        by_path = suite("unit", [self.A, self.B], paths=self.UNION, entry_durations=self.SHORT)
        by_glob = suite("unit", ["checks/*.py"], paths=with_helper, entry_durations={**self.SHORT, self.HELPER: 1.0})
        cases = {"path to glob": (by_path, dict(by_path, entries=["checks/test_*.py"]), self.TRACKED),
                 "glob to path": (by_glob, dict(by_path, paths=with_helper), {**self.TRACKED, self.HELPER: ""})}
        for move, (before, after, files) in cases.items():
            with self.subTest(move=move):
                fixture = self.fixture(before, files)
                self.assertEqual((0, self.SKIP_WHOLE, []), self.commit_and_select(fixture, after))
                self.assertEqual("No additions to apply.\n", self.apply(fixture))
                self.assertEqual(after, fixture.suite("unit"))

    def test_glob_to_path_with_a_key_of_a_file_that_no_path_names_stays_valid_and_the_next_apply_removes_it(self) -> None:
        before = self.with_helper(entry_durations={**self.DURATIONS, self.HELPER: 1.0})
        after = dict(before, entries=[self.A, self.B])
        fixture = self.fixture(before, {**self.TRACKED, self.HELPER: ""})
        self.assertEqual((0, self.SKIP_PER_ENTRY, []), self.commit_and_select(fixture, after))
        self.assertIn(f"unit: removed the stored lists and the estimate of {self.HELPER}: no entry names it", self.apply(fixture))
        self.assertEqual(dict(after, paths=self.UNION, entry_paths=self.ENTRY_PATHS, entry_durations=self.DURATIONS), fixture.suite("unit"))

    def test_glob_to_path_with_a_new_test_file_whose_additions_are_not_applied_runs_it_until_they_are(self) -> None:
        before = suite("unit", ["checks/test_*.py"], entry_selection=True, paths=self.UNION, entry_paths=self.ENTRY_PATHS, entry_durations=self.DURATIONS)
        fixture = self.fixture(before, {**self.TRACKED, self.NEW: ""})
        code, line, entries = self.commit_and_select(fixture, dict(before, entries=[self.A, self.B, self.NEW]))
        self.assertEqual((0, [self.NEW]), (code, entries))
        self.assertIn(f"`{self.NEW}` runs on every change: no stored path list", line)

    def test_renaming_a_path_entry_by_changing_entries_alone_runs_the_renamed_file_and_its_additions_complete_the_suite_list(self) -> None:
        before = self.per_entry([self.A, self.B])
        fixture = self.fixture(before)
        (fixture.root / self.B).unlink()
        code, line, entries = self.commit_and_select(fixture, dict(before, entries=[self.A, self.NEW]), files={self.NEW: ""})
        self.assertEqual((0, [self.NEW]), (code, entries), line)
        self.assertIn(f"`{self.NEW}` runs on every change: no stored path list", line)
        report = self.apply(fixture, {"suite": "unit", "create": False, "recorded_entries": [self.NEW],
                                      "entries": {self.NEW: reads([self.NEW, "lib/b.py"])}})
        self.assertIn(f"unit: removed the stored lists and the estimate of {self.B}: no entry names it", report)
        self.assertEqual(suite("unit", [self.A, self.NEW], entry_selection=True, paths=[self.A, self.NEW, "lib/a.py", "lib/b.py"],
                               entry_paths={self.A: self.ENTRY_PATHS[self.A], self.NEW: [self.NEW, "lib/b.py"]},
                               entry_durations={self.A: self.DURATIONS[self.A]}), fixture.suite("unit"))
        # The written suite list loads, and the renamed file is selected on its own list.
        self.assertEqual((0, [self.NEW]), self.select(fixture, "--changed-file", "lib/b.py")[::2])

    def test_whole_to_per_entry_file_drops_the_stored_lists_and_the_next_run_runs_and_records_every_entry_file(self) -> None:
        before = suite("unit", [self.A, self.B], paths=self.UNION, directory_memberships=["lib"], existence_reads=[{"path": "probe", "kind": "any"}],
                       lookup_directories=["checks"], entry_durations=self.DURATIONS)
        after = suite("unit", [self.A, self.B], entry_selection=True, entry_durations=self.DURATIONS)
        fixture = self.fixture(before)
        self.assertEqual((0, "unit: run (runs on every change: no stored path list)", [self.A, self.B]), self.commit_and_select(fixture, after))
        self.apply(fixture, {"suite": "unit", "create": True, "recorded_entries": [self.A, self.B],
                             "entries": {entry: reads(paths) for entry, paths in self.ENTRY_PATHS.items()}})
        self.assertEqual(dict(after, paths=self.UNION, entry_paths=self.ENTRY_PATHS), fixture.suite("unit"))
        # From then on a change runs the entry files whose own lists it reaches.
        code, line, entries = self.select(fixture, "--changed-file", "lib/a.py")
        self.assertEqual((0, [self.A]), (code, entries), line)

    def test_whole_to_per_entry_file_with_the_stored_lists_kept_makes_the_suite_list_invalid(self) -> None:
        before = suite("unit", [self.A, self.B], paths=self.UNION)
        fixture = self.fixture(before)
        code, error, _ = self.commit_and_select(fixture, dict(before, entry_selection=True))
        self.assertEqual(2, code)
        self.assertIn("a stored suite with entry_selection needs entry_paths", error)

    def test_per_entry_file_to_whole_drops_the_per_entry_fields_and_the_next_run_runs_every_entry_file_and_records_nothing(self) -> None:
        before = self.per_entry([self.A, self.B], self.SHORT, entry_setups={self.A: ["deps"]}, entry_extra_paths={self.B: ["docs/**"]},
                                entry_directory_memberships={self.A: ["lib"]}, directory_memberships=["lib"])
        after = suite("unit", [self.A, self.B], paths=self.UNION, directory_memberships=["lib"], setups=["deps"], extra_paths=["docs/**"],
                      entry_durations=self.SHORT)
        fixture = self.fixture(before)
        self.assertEqual((0, "unit: run (its command, setups, operating system or selection per entry file in `suites.json` changed)", [self.A, self.B]),
                         self.commit_and_select(fixture, after))
        # The suite's lists already hold the union of the lists of its entry files, so their reads add nothing.
        self.apply(fixture, {"suite": "unit", "create": False, "recorded_entries": [self.A, self.B], "reads": reads(self.UNION, memberships=["lib"])})
        self.assertEqual(after, fixture.suite("unit"))
        # From then on a change that reaches the suite's list runs every entry file.
        code, line, entries = self.select(fixture, "--changed-file", "lib/a.py")
        self.assertEqual((0, [self.A, self.B]), (code, entries), line)

    def test_per_entry_file_to_whole_with_a_per_entry_list_or_entry_setups_kept_makes_the_suite_list_invalid(self) -> None:
        whole = suite("unit", [self.A, self.B], paths=self.UNION, entry_durations=self.SHORT)
        kept = {"entry_paths": self.ENTRY_PATHS, "entry_directory_memberships": {self.A: ["lib"]},
                "entry_existence_reads": {self.A: [{"path": "probe", "kind": "any"}]}, "entry_lookup_directories": {self.A: ["checks"]},
                "entry_setups": {self.A: ["deps"]}}
        for field, value in kept.items():
            with self.subTest(field=field):
                fixture = self.fixture(self.per_entry([self.A, self.B], self.SHORT))
                code, error, _ = self.commit_and_select(fixture, dict(whole, **{field: value}))
                self.assertEqual(2, code)
                self.assertIn(f"{field} needs entry_selection", error)

    def test_per_entry_file_to_whole_with_entry_extra_paths_kept_stays_valid_and_selects_on_them_no_more(self) -> None:
        before = self.per_entry([self.A, self.B], self.SHORT, entry_extra_paths={self.B: ["docs/**"]})
        fixture = self.fixture(before)
        self.assertEqual((0, "unit: run (`docs/guide.md` matches `docs/**` of `checks/test_b.py`; entries: `checks/test_b.py`)", [self.B]),
                         self.select(fixture, "--changed-file", "docs/guide.md"))
        kept = suite("unit", [self.A, self.B], paths=self.UNION, entry_durations=self.SHORT, entry_extra_paths={self.B: ["docs/**"]})
        self.assertEqual(0, self.commit_and_select(fixture, kept)[0])
        self.assertEqual((0, self.SKIP_WHOLE, []), self.select(fixture, "--changed-file", "docs/guide.md"))


if __name__ == "__main__":
    unittest.main()
