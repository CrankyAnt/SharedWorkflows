"""The recording step's apply on fixture suite lists, on any operating system."""

from __future__ import annotations

import json
import unittest

from tests.recording_fixtures import RecordingFixture, suite

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


if __name__ == "__main__":
    unittest.main()
