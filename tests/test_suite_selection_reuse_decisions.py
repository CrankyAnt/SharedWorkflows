"""Result reuse and additions keying decisions of select and store on fixture repositories."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tests import temporary_support
from tests.suite_selection_fixtures import force_add, git, write
from tests.suite_selection_reuse_support import (
    SHARED_FOLDER,
    SUITES,
    FixtureRepository,
    suite_selection,
)


class ReuseTests(FixtureRepository):
    """Later heads of a pull request reuse passed results whose exact key did not change."""

    LABELS = ("--runner-label", "linux=ubuntu-26.04", "--runner-label", "windows=windows-2025", "--setup-version", "python=3.13")

    def setUp(self) -> None:
        super().setUp()
        self.record = self.root.parent / "record.json"

    def head(self, files: dict[str, str]) -> str:
        return self.commit("head", files)

    def select_head(self, head: str, *extra: str, labels: tuple[str, ...] = LABELS) -> dict[str, str]:
        git(self.root, "checkout", "-q", head)
        outputs, _, _ = self.select("--base", self.base, "--head", head, "--compare", "merge-base", "--results", str(self.record), *labels, *extra)
        return outputs

    def store(self, outputs: dict[str, str], passed: list[tuple[str, str]] | dict[str, list[dict]], run: int, result: str = "success",
              job: str = "select", additions: Path | None = None) -> bool:
        """Store a run: ``passed`` is a list of passed entry files of one job, or the passed list each job uploaded by
        artifact name; ``additions`` the folder with the run's downloaded ``recording-additions-*`` artifacts."""
        needs = self.root.parent / "needs.json"
        needs.write_text(json.dumps({job: {"result": result, "outputs": outputs}, "linux": {"result": "success", "outputs": {}}}), encoding="utf-8")
        directory = self.root.parent / f"passed-{run}"
        if directory.exists():
            temporary_support.remove_tree(directory)
        lists = passed if isinstance(passed, dict) else {"suite-results-shard-1": [{"suite": suite, "entry": entry} for suite, entry in passed]}
        for name, items in lists.items():
            (directory / name).mkdir(parents=True)
            (directory / name / "passed.json").write_text(json.dumps(items), encoding="utf-8")
        github_output = self.root.parent / "store-output.txt"
        github_output.unlink(missing_ok=True)
        with redirect_stdout(io.StringIO()):
            code = suite_selection.main(["store", "--needs", str(needs), "--passed", str(directory), "--run-id", str(run),
                                         *(["--selection-job", job] if job != "select" else []),
                                         *(["--additions", str(additions), "--root", str(self.root)] if additions is not None else []),
                                         "--output", str(self.record), "--github-output", str(github_output)])
        self.assertEqual(code, 0)
        return github_output.read_text(encoding="utf-8") == "stored=true\n"

    RECORDING = str(SHARED_FOLDER / "recording" / "recording.py")
    NO_READS = {"paths": [], "existence_reads": [], "directory_memberships": [], "lookup_directories": [], "module_lookups": {}}

    def job_with_additions(self, run: int, job: str, passed: list[tuple[str, str]], changes: list[dict]) -> tuple[list[dict], Path]:
        """One job of a run whose check reported ``changes``, in the shape check writes them: its additions artifact under
        the run's additions folder, and its passed list as ``keep`` leaves it."""
        artifact = self.root.parent / f"additions-{run}" / f"recording-additions-{job}"
        artifact.mkdir(parents=True, exist_ok=True)
        document = artifact / "additions.json"
        document.write_text(json.dumps({"kind": "additions", "suite_list": "suites.json", "suites": changes}), encoding="utf-8")
        listed = self.root.parent / f"passed-{run}-{job}.json"
        listed.write_text(json.dumps([{"suite": suite, "entry": entry} for suite, entry in passed]), encoding="utf-8")
        completed = subprocess.run([sys.executable, self.RECORDING, "keep", "--additions", str(document), str(listed)], capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(listed.read_text(encoding="utf-8")), artifact.parent

    def app_commit(self, *documents: Path) -> str:
        """The App's additions commit: the additions applied to the suite list, and no other file."""
        before = git(self.root, "rev-parse", "HEAD")
        completed = subprocess.run([sys.executable, self.RECORDING, "apply", "--root", str(self.root), "--suites", str(self.root / "suites.json"),
                                    *map(str, documents)], capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)
        app = self.commit("chore: apply the recorded additions")
        self.assertEqual("suites.json", git(self.root, "diff", "--name-only", before, app))
        return app

    @staticmethod
    def runs(outputs: dict[str, str]) -> list[str]:
        return sorted(name for name, result in json.loads(outputs["suites"]).items() if result == "run")

    @staticmethod
    def running(outputs: dict[str, str]) -> list[list[str]]:
        return sorted(sorted(unit["entries"]) for unit in json.loads(outputs["units"]).values())

    def make_both_read_the_suite_list(self) -> None:
        """Make `both` read the suite list, as the entry files of this repository that read tests/suites.json do."""
        suites = json.loads(json.dumps(SUITES))
        suites["suites"][1]["paths"].append("suites.json")
        git(self.root, "checkout", "-q", self.base)
        self.base = self.commit("a suite that reads the suite list", {"suites.json": json.dumps(suites, indent=2) + "\n"})

    def first_run_with_additions(self, run: int) -> tuple[str, Path]:
        """A first head whose run passes every unit, with additions for the entry file `tests/test_one.py` of the suite
        selected per entry file and for the pooled suite `pr-only`; it returns the head and the additions folder."""
        first = self.head({"src/feature.py": "feature v2\n"})
        outputs = self.select_head(first)
        reads = self.NO_READS | {"paths": ["README.md"]}
        kept, additions = self.job_with_additions(run, "shard-1", self.ALL, [
            {"suite": "per-entry", "create": False, "recorded_entries": ["tests/test_one.py"], "entries": {"tests/test_one.py": reads}},
            {"suite": "pr-only", "create": False, "recorded_entries": ["tests/test_pr.py"], "reads": reads}])
        self.assertEqual([("per-entry", "tests/test_one.py"), ("pr-only", "tests/test_pr.py")],
                         sorted((item["suite"], item["entry"]) for item in kept if item.get("additions")), "keep marks the entry files with additions")
        self.assertTrue(self.store(outputs, {"suite-results-shard-1": kept}, run, additions=additions))
        return first, additions

    ALL = [("pr-only", "tests/test_pr.py"), ("both", "tests/test_both.py"), ("per-entry", "tests/test_one.py"), ("windows", "tests/test_win.py")]

    def first_head_passes(self) -> str:
        first = self.head({"src/feature.py": "feature v2\n"})
        outputs = self.select_head(first)
        self.assertEqual(json.loads(outputs["reused"]), {})
        self.assertEqual(sorted(unit["suite"] for unit in json.loads(outputs["units"]).values()), ["both", "per-entry", "pr-only", "windows"])
        self.assertTrue(self.store(outputs, self.ALL, 9001))
        return first

    def test_a_later_head_reuses_the_passed_results_whose_key_matches_exactly(self) -> None:
        self.first_head_passes()
        later = self.head({"README.md": "a change no suite reads\n"})
        outputs = self.select_head(later)
        self.assertEqual(json.loads(outputs["suites"]), {name: "skip" for name in ("pr-only", "both", "main-only", "per-entry", "windows")})
        self.assertEqual(sorted(json.loads(outputs["reused"]).values()), [9001] * 4)
        self.assertEqual((outputs["shards"], outputs["windows"], json.loads(outputs["units"])), ("[]", "", {}))
        _, printed, _ = self.select("--base", self.base, "--head", later, "--compare", "merge-base", "--results", str(self.record), *self.LABELS)
        self.assertIn("pr-only: skip (reuses the passed result of run 9001)", printed)

    def test_a_change_to_any_single_part_of_the_key_makes_the_unit_run(self) -> None:
        first = self.first_head_passes()
        runs = lambda outputs: sorted(name for name, result in json.loads(outputs["suites"]).items() if result == "run")
        git(self.root, "checkout", "-q", first)
        self.assertEqual(runs(self.select_head(self.head({"tests/test_pr.py": "a changed read\n"}))), ["pr-only"], "the content of a read")
        git(self.root, "checkout", "-q", first)
        changed_command = json.loads(json.dumps(SUITES))
        changed_command["suites"][1]["command"] = ["python3", "-m", "unittest", "-v"]
        self.assertEqual(runs(self.select_head(self.head({"suites.json": json.dumps(changed_command)}))), ["both"], "the command")
        git(self.root, "checkout", "-q", first)
        self.assertEqual(runs(self.select_head(first, labels=self.LABELS[:4] + ("--setup-version", "python=3.14"))),
                         ["both", "per-entry", "pr-only", "windows"], "the version a setup pins")
        self.assertEqual(runs(self.select_head(first, labels=self.LABELS[:3] + ("windows=windows-2026",) + self.LABELS[4:])),
                         ["windows"], "the runner label")

    def test_a_moved_pin_of_a_pinned_tool_makes_only_the_units_that_state_it_run(self) -> None:
        pins = self.root.parent / "pins.json"

        def select_pinned(head: str, version: str) -> dict[str, str]:
            pins.write_text(json.dumps({"node": {"version": version}}), encoding="utf-8")
            return self.select_head(head, "--pinned-tool", "node", "--pins", str(pins))

        first = self.head({"src/feature.py": "feature v2\n", "src/other.py": "other v2\n"})
        outputs = select_pinned(first, "24.9.0")
        self.assertTrue(self.store(outputs, self.ALL + [("per-entry", "tests/test_two.py")], 9001))
        later = self.head({"README.md": "a change no suite reads\n"})
        same = select_pinned(later, "24.9.0")
        self.assertEqual((same["shards"], same["windows"]), ("[]", ""), "the same pin reuses every passed result")
        moved = select_pinned(later, "24.10.0")
        self.assertEqual([unit["entries"] for unit in json.loads(moved["units"]).values()], [["tests/test_two.py"]],
                         "only the entry file that states node runs again")
        self.assertEqual(sorted(json.loads(moved["reused"]).values()), [9001] * 4)
        self.assertEqual(json.loads(moved["shards"])[0]["tools"], {"node": "24.10.0"})

    def test_an_entry_file_of_a_suite_selected_per_entry_file_is_a_unit_of_its_own(self) -> None:
        self.first_head_passes()
        later = self.head({"src/other.py": "other v2\n"})
        outputs = self.select_head(later)
        self.assertEqual(json.loads(outputs["entries"])["per-entry"], ["tests/test_two.py"])
        self.assertEqual([unit["entries"] for unit in json.loads(outputs["units"]).values()], [["tests/test_two.py"]])

    def test_a_changed_step_of_the_calling_workflow_counts_as_a_read_of_its_suite(self) -> None:
        workflow = ("name: Validate\non: [pull_request]\njobs:\n  select:\n    runs-on: ubuntu-latest\n    steps:\n"
                    "      - run: python suites.py\n        id: select\n"
                    "      - run: python -m unittest tests/test_main.py\n        if: steps.select.outputs.main-only == 'run'\n")
        write(self.root, ".github/workflows/validate.yml", workflow)
        self.base = self.commit("the workflow")
        arguments = ("--workflow", ".github/workflows/validate.yml")
        first = self.head({".github/workflows/validate.yml": workflow.replace("unittest tests", "unittest -v tests")})
        outputs = self.select_head(first, *arguments)
        self.assertEqual(json.loads(outputs["suites"])["main-only"], "run")
        self.assertTrue(self.store(outputs, [("main-only", "tests/test_main.py")], 9002))
        self.assertEqual(json.loads(self.select_head(self.head({"README.md": "unread\n"}), *arguments)["suites"])["main-only"], "skip")
        second = self.head({".github/workflows/validate.yml": workflow.replace("unittest tests", "unittest -v -b tests")})
        self.assertEqual(json.loads(self.select_head(second, *arguments)["suites"])["main-only"], "run")

    def test_the_key_holds_the_tracked_gitignore_files_and_the_tracked_paths_that_they_match(self) -> None:
        runs = lambda outputs: sorted(name for name, result in json.loads(outputs["suites"]).items() if result == "run")
        every = ["both", "main-only", "per-entry", "pr-only", "windows"]
        for label, first, later in (
                ("a tracked .gitignore file", {".gitignore": "*.tmp\n"}, {".gitignore": "*.tmp\n*.out\n"}),
                ("a tracked path that the rules match", {".gitignore": "*.tmp\n", "notes.tmp": "first\n"}, {"notes.tmp": "second\n"})):
            with self.subTest(part=label):
                git(self.root, "checkout", "-q", self.base)
                self.record.unlink(missing_ok=True)
                force_add(self.root, first)
                outputs = self.select_head(self.head({}))
                self.assertEqual(every, runs(outputs))
                self.assertTrue(self.store(outputs, self.ALL + [("main-only", "tests/test_main.py")], 9010))
                outputs = self.select_head(self.head(later))
                self.assertEqual(every, runs(outputs), "the change still runs every unit")
                self.assertEqual({}, json.loads(outputs["reused"]), "and reuses no result")

    def test_the_key_holds_the_names_that_carry_a_module_lookup_in_each_lookup_directory(self) -> None:
        suites = json.loads(json.dumps(SUITES))
        suites["suites"][0]["lookup_directories"] = ["src"]
        suites["module_lookups"] = {"src": ["helper"]}
        git(self.root, "checkout", "-q", self.base)
        self.base = self.commit("module lookups", {"suites.json": json.dumps(suites)})
        self.first_head_passes()
        first = git(self.root, "rev-parse", "HEAD")
        reused = lambda outputs: sorted(unit for unit in json.loads(outputs["suites"]) if json.loads(outputs["suites"])[unit] == "skip")
        self.assertIn("pr-only", reused(self.select_head(self.head({"src/notes.txt": "no module name\n"}))))
        git(self.root, "checkout", "-q", first)
        outputs = self.select_head(self.head({"src/helper.py": "a module that the suite looks up\n"}))
        self.assertEqual("run", json.loads(outputs["suites"])["pr-only"], "a new module under a lookup name reuses no result")

    def test_a_re_run_reuses_no_earlier_result(self) -> None:
        self.first_head_passes()
        later = self.head({"README.md": "unread\n"})
        outputs = self.select_head(later, "--run-attempt", "2")
        self.assertEqual(json.loads(outputs["reused"]), {})
        self.assertEqual(sorted(name for name, result in json.loads(outputs["suites"]).items() if result == "run"),
                         ["both", "per-entry", "pr-only", "windows"])

    def test_the_store_reads_the_selection_from_the_job_that_the_caller_names(self) -> None:
        outputs = self.select_head(self.head({"src/feature.py": "feature v2\n"}))
        self.assertTrue(self.store(outputs, self.ALL, 9020, job="suite-selection"), "the pipeline's selection job carries the part's name")
        self.assertEqual(len(json.loads(self.record.read_text(encoding="utf-8"))["passed"]), 4)
        self.record.unlink()
        needs = self.root.parent / "needs.json"
        needs.write_text(json.dumps({"select": {"result": "success", "outputs": outputs}}), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            suite_selection.main(["store", "--needs", str(needs), "--selection-job", "suite-selection", "--passed", str(self.root.parent / "passed-9020"),
                                  "--run-id", "9021", "--output", str(self.record)])
        self.assertFalse(self.record.exists(), "a needs context without the named job stores nothing")

    def test_only_passed_units_are_stored_and_a_failed_selection_stores_nothing(self) -> None:
        first = self.head({"src/feature.py": "feature v2\n"})
        outputs = self.select_head(first)
        self.assertTrue(self.store(outputs, [("pr-only", "tests/test_pr.py"), ("per-entry", "tests/test_one.py")], 9003))
        stored = json.loads(self.record.read_text(encoding="utf-8"))["passed"]
        units = json.loads(outputs["units"])
        self.assertEqual(sorted(units[key]["suite"] for key in stored), ["per-entry", "pr-only"])
        later = self.select_head(self.head({"README.md": "unread\n"}))
        self.assertEqual(sorted(name for name, result in json.loads(later["suites"]).items() if result == "run"), ["both", "windows"])
        self.assertTrue(self.store(later, [("both", "tests/test_both.py")], 9004))
        carried = json.loads(self.record.read_text(encoding="utf-8"))["passed"]
        self.assertEqual(sorted(carried.values()), [9003, 9003, 9004], "the reused results stay with the run they passed in")
        self.record.unlink()
        self.assertFalse(self.store(outputs, self.ALL, 9005, result="failure"))
        self.assertFalse(self.record.exists())

    def test_a_failed_plan_job_with_a_written_plan_and_passed_selection_stores_pull_request_results(self) -> None:
        first = self.head({"src/feature.py": "feature v2\n"})
        outputs = self.select_head(first)
        self.assertTrue(self.store(outputs, [("pr-only", "tests/test_pr.py")], 9070))
        later = self.select_head(self.head({"README.md": "unread\n"}))
        with_plan = {**later, "plan": '{"parts":{}}', "selection": "success"}
        self.record.unlink()
        self.assertTrue(self.store(with_plan, [("both", "tests/test_both.py")], 9071, result="failure", job="plan"))
        carried = json.loads(self.record.read_text(encoding="utf-8"))["passed"]
        self.assertEqual([9070, 9071], sorted(carried.values()), "keeps both reused and newly passed units after a failed light check")
        for bad_outputs, label in (
            ({**later, "plan": "", "selection": "success"}, "an empty plan output stores nothing"),
            ({**later, "plan": '{"parts":{}}', "selection": "failure"}, "a failed selection stores nothing"),
            ({**later, "plan": '{"parts":{}}'}, "a missing selection outcome stores nothing"),
        ):
            with self.subTest(case=label):
                self.record.unlink(missing_ok=True)
                self.assertFalse(self.store(bad_outputs, [("both", "tests/test_both.py")], 9072, result="failure", job="plan"), label)
                self.assertFalse(self.record.exists(), label)

    def test_a_later_head_that_does_not_apply_the_additions_reuses_the_unmarked_passes_and_runs_the_entry_files_with_additions(self) -> None:
        self.first_run_with_additions(9007)
        later = self.select_head(self.head({"README.md": "a change no suite reads\n"}))
        self.assertEqual(["per-entry", "pr-only"], self.runs(later))
        self.assertEqual([9007, 9007], sorted(json.loads(later["reused"]).values()))
        self.assertEqual([["tests/test_one.py"], ["tests/test_pr.py"]], self.running(later), "a pass with additions counts only under the applied lists")

    def test_a_later_head_that_only_applies_the_additions_reuses_the_passes_of_the_units_with_additions(self) -> None:
        self.make_both_read_the_suite_list()
        _, additions = self.first_run_with_additions(9008)
        app = self.app_commit(*additions.rglob("additions.json"))
        later = self.select_head(app)
        self.assertEqual(["both"], self.runs(later), "only the reader of the suite list runs")
        self.assertEqual([["tests/test_both.py"]], self.running(later))
        self.assertEqual([9008] * 3, sorted(json.loads(later["reused"]).values()))
        _, printed, _ = self.select("--base", self.base, "--head", app, "--compare", "merge-base", "--results", str(self.record), *self.LABELS)
        for line in ("pr-only: skip (reuses the passed result of run 9008)", "per-entry: skip (reuses the passed result of run 9008)",
                     "windows: skip (reuses the passed result of run 9008)"):
            self.assertIn(line, printed)

    def test_a_pooled_suite_split_over_jobs_with_additions_in_each_reuses_its_pass_on_the_head_that_applies_them(self) -> None:
        pooled = json.loads(json.dumps(SUITES))
        pooled["suites"][0]["entries"] = ["tests/test_pr.py", "tests/test_main.py"]
        git(self.root, "checkout", "-q", self.base)
        self.base = self.commit("a pooled suite of two entry files", {"suites.json": json.dumps(pooled, indent=2) + "\n"})

        def run(number: int, shard_2_kept: bool) -> tuple[str, list[Path]]:
            git(self.root, "checkout", "-q", self.base)
            self.record.unlink(missing_ok=True)
            first = self.head({"src/feature.py": f"feature v{number}\n"})
            outputs = self.select_head(first)
            one, additions = self.job_with_additions(number, "shard-1", [("pr-only", "tests/test_pr.py")], [
                {"suite": "pr-only", "create": False, "recorded_entries": ["tests/test_pr.py"], "reads": self.NO_READS | {"paths": ["README.md"]}}])
            two, _ = self.job_with_additions(number, "shard-2", [("pr-only", "tests/test_main.py")], [
                {"suite": "pr-only", "create": False, "recorded_entries": ["tests/test_main.py"], "reads": self.NO_READS | {"paths": ["src/other.py"]}}])
            lists = {"suite-results-shard-1": one} | ({"suite-results-shard-2": two} if shard_2_kept else {})
            self.assertTrue(self.store(outputs, lists, number, additions=additions))
            return first, sorted(additions.rglob("additions.json"))

        _, documents = run(9030, shard_2_kept=True)
        later = self.select_head(self.app_commit(*documents))
        self.assertNotIn("pr-only", self.runs(later), "the pooled suite reuses its pass under the additions of both jobs")
        self.assertIn(9030, json.loads(later["reused"]).values())
        _, documents = run(9031, shard_2_kept=False)
        later = self.select_head(self.app_commit(*documents))
        self.assertIn(["tests/test_main.py", "tests/test_pr.py"], self.running(later), "a job that uploaded no passed list stores no pass of the suite")

    def test_a_head_that_applies_the_additions_and_changes_a_file_a_unit_read_runs_that_unit(self) -> None:
        _, additions = self.first_run_with_additions(9009)
        self.app_commit(*additions.rglob("additions.json"))
        later = self.select_head(self.head({"README.md": "a changed read of the entry files with additions\n"}))
        self.assertEqual(["per-entry", "pr-only"], self.runs(later))
        self.assertEqual([["tests/test_one.py"], ["tests/test_pr.py"]], self.running(later))

    def test_a_head_whose_stored_list_differs_from_the_list_with_the_additions_applied_runs_the_unit(self) -> None:
        first, _ = self.first_run_with_additions(9011)
        reads = self.NO_READS | {"paths": ["README.md"]}
        for label, changes, expected in (
                ("only part of the additions", [{"suite": "per-entry", "create": False, "recorded_entries": ["tests/test_one.py"],
                                                 "entries": {"tests/test_one.py": reads}}], [["tests/test_pr.py"]]),
                ("a read beyond the additions", [{"suite": "per-entry", "create": False, "recorded_entries": ["tests/test_one.py"],
                                                  "entries": {"tests/test_one.py": self.NO_READS | {"paths": ["README.md", "src/moved.py"]}}},
                                                 {"suite": "pr-only", "create": False, "recorded_entries": ["tests/test_pr.py"], "reads": reads}],
                 [["tests/test_one.py"]])):
            with self.subTest(head=label):
                other = self.root.parent / f"other-{len(changes)}.json"
                other.write_text(json.dumps({"kind": "additions", "suite_list": "suites.json", "suites": changes}), encoding="utf-8")
                later = self.select_head(self.app_commit(other))
                self.assertEqual(expected, self.running(later))
                git(self.root, "checkout", "-q", first)

    def test_a_unit_whose_stored_patterns_match_the_suite_list_runs_on_the_head_that_applies_the_additions(self) -> None:
        self.make_both_read_the_suite_list()
        _, additions = self.first_run_with_additions(9012)
        later = self.select_head(self.app_commit(*additions.rglob("additions.json")))
        self.assertEqual(["both"], self.runs(later))
        self.assertEqual(["both"], [unit["suite"] for unit in json.loads(later["units"]).values()], "its pass under the old list is not reused")

    def test_a_job_with_a_recording_error_or_a_failed_keep_stores_none_of_its_passes(self) -> None:
        first = self.head({"src/feature.py": "feature v2\n"})
        outputs = self.select_head(first)
        reads = self.NO_READS | {"paths": ["README.md"]}
        _, additions = self.job_with_additions(9013, "shard-1", self.ALL, [
            {"suite": "per-entry", "create": False, "recorded_entries": ["tests/test_one.py"], "entries": {"tests/test_one.py": reads}}])
        # The job uploaded no passed list: its check failed without additions, or its keep failed.
        self.assertTrue(self.store(outputs, {}, 9013, additions=additions))
        self.assertEqual({}, json.loads(self.record.read_text(encoding="utf-8"))["passed"])
        later = self.select_head(self.app_commit(*additions.rglob("additions.json")))
        self.assertEqual({}, json.loads(later["reused"]))
        self.assertEqual(["both", "per-entry", "pr-only", "windows"], self.runs(later))

    def test_a_marked_pass_whose_additions_artifact_is_missing_is_not_stored_under_the_applied_lists(self) -> None:
        first = self.head({"src/feature.py": "feature v2\n"})
        outputs = self.select_head(first)
        reads = self.NO_READS | {"paths": ["README.md"]}
        # Shard 1 marked tests/test_one.py, but the upload of its additions artifact failed: only shard 2's document is there.
        kept, lost = self.job_with_additions(9016, "shard-1", self.ALL, [
            {"suite": "per-entry", "create": False, "recorded_entries": ["tests/test_one.py"], "entries": {"tests/test_one.py": reads}}])
        temporary_support.remove_tree(lost / "recording-additions-shard-1")
        _, additions = self.job_with_additions(9016, "shard-2", [("main-only", "tests/test_main.py")], [
            {"suite": "pr-only", "create": False, "recorded_entries": ["tests/test_pr.py"], "reads": reads}])
        self.assertTrue(self.store(outputs, {"suite-results-shard-1": kept}, 9016, additions=additions))
        units = json.loads(outputs["units"])
        stored = json.loads(self.record.read_text(encoding="utf-8"))["passed"]
        self.assertEqual(4, len(stored), "the unmarked passes, and pr-only also under the applied lists")
        self.assertNotIn(["tests/test_one.py"], [units[key]["entries"] for key in stored if key in units])
        self.assertEqual(9016, stored.get(next(key for key, unit in units.items() if unit["suite"] == "pr-only")))

    def test_a_pass_with_additions_is_never_stored_under_the_key_of_the_lists_without_them(self) -> None:
        first = self.head({"src/feature.py": "feature v2\n"})
        outputs = self.select_head(first)
        kept, _ = self.job_with_additions(9014, "shard-1", self.ALL, [
            {"suite": "pr-only", "create": False, "recorded_entries": ["tests/test_pr.py"], "reads": self.NO_READS | {"paths": ["README.md"]}}])
        self.assertTrue(self.store(outputs, {"suite-results-shard-1": kept}, 9014))
        units = json.loads(outputs["units"])
        self.assertEqual(["both", "per-entry", "windows"], sorted(units[key]["suite"] for key in json.loads(self.record.read_text(encoding="utf-8"))["passed"]),
                         "without the additions folder the marked pass is not stored")

    def test_a_re_run_of_the_head_that_applies_the_additions_reuses_nothing(self) -> None:
        _, additions = self.first_run_with_additions(9015)
        app = self.app_commit(*additions.rglob("additions.json"))
        later = self.select_head(app, "--run-attempt", "2")
        self.assertEqual({}, json.loads(later["reused"]))
        self.assertEqual(["both", "per-entry", "pr-only", "windows"], self.runs(later))

    def test_a_suite_split_over_jobs_is_stored_only_when_every_entry_file_passed(self) -> None:
        both = json.loads(json.dumps(SUITES))
        both["suites"][0]["entries"] = ["tests/test_pr.py", "tests/test_main.py"]
        first = self.head({"suites.json": json.dumps(both), "src/feature.py": "feature v2\n"})
        outputs = self.select_head(first)
        self.assertTrue(self.store(outputs, [("pr-only", "tests/test_pr.py")], 9006))
        units = json.loads(outputs["units"])
        self.assertNotIn("pr-only", [units[key]["suite"] for key in json.loads(self.record.read_text(encoding="utf-8"))["passed"]])

    def test_a_pass_of_a_new_suite_is_stored_under_its_applied_key_and_reused_after_additions(self) -> None:
        with_new = json.loads(json.dumps(SUITES))
        with_new["suites"].append({
            "name": "brand-new",
            "entries": ["tests/test_new.py"],
            "command": ["python3", "-m", "unittest"],
            "setups": ["python"],
        })
        first = self.head({"suites.json": json.dumps(with_new, indent=2) + "\n", "tests/test_new.py": "new test\n"})
        outputs = self.select_head(first)
        self.assertIn("brand-new", self.runs(outputs))
        self.assertIn("brand-new", [unit["suite"] for unit in json.loads(outputs["units"]).values()],
                      "a new suite without paths is emitted in units so store --additions can key it")
        # Without additions, a pass of an unlisted suite is never stored under its sentinel key.
        self.assertTrue(self.store(outputs, [("brand-new", "tests/test_new.py")], 9040))
        self.assertEqual({}, json.loads(self.record.read_text(encoding="utf-8"))["passed"])
        self.record.unlink()

        reads = self.NO_READS | {"paths": ["tests/test_new.py", "src/feature.py"]}
        kept, additions = self.job_with_additions(9041, "shard-1", [("brand-new", "tests/test_new.py")], [
            {"suite": "brand-new", "create": True, "recorded_entries": ["tests/test_new.py"], "reads": reads}])
        self.assertTrue(self.store(outputs, {"suite-results-shard-1": kept}, 9041, additions=additions))

        # Before additions are applied, a later head still runs the new suite.
        unapplied = self.select_head(self.head({"README.md": "unread\n"}))
        self.assertIn("brand-new", self.runs(unapplied))
        git(self.root, "checkout", "-q", first)

        # Once additions are applied, the next run takes over the pass from run 9041.
        app = self.app_commit(*additions.rglob("additions.json"))
        later = self.select_head(app)
        self.assertNotIn("brand-new", self.runs(later))
        self.assertIn(9041, json.loads(later["reused"]).values())

        # Changing one of its recorded reads makes it run again.
        changed = self.select_head(self.head({"src/feature.py": "feature changed\n"}))
        self.assertIn("brand-new", self.runs(changed))

    def test_a_pass_of_a_new_entry_file_is_stored_under_its_applied_key_and_reused_after_additions(self) -> None:
        with_entry = json.loads(json.dumps(SUITES))
        with_entry["suites"][3]["entries"].append("tests/test_three.py")
        first = self.head({"suites.json": json.dumps(with_entry, indent=2) + "\n", "tests/test_three.py": "third test\n"})
        outputs = self.select_head(first)
        self.assertEqual(["tests/test_three.py"], json.loads(outputs["entries"])["per-entry"])
        self.assertIn(["tests/test_three.py"], self.running(outputs),
                      "a new entry file without a stored list is emitted in units so store --additions can key it")

        reads = self.NO_READS | {"paths": ["tests/test_three.py", "src/other.py"]}
        kept, additions = self.job_with_additions(9042, "shard-1", [("per-entry", "tests/test_three.py")], [
            {"suite": "per-entry", "create": False, "recorded_entries": ["tests/test_three.py"],
             "entries": {"tests/test_three.py": reads}}])
        self.assertTrue(self.store(outputs, {"suite-results-shard-1": kept}, 9042, additions=additions))

        # Before additions are applied, a later head still runs the new entry file.
        unapplied = self.select_head(self.head({"README.md": "unread\n"}))
        self.assertEqual(["tests/test_three.py"], json.loads(unapplied["entries"])["per-entry"])
        git(self.root, "checkout", "-q", first)

        # Once additions are applied, the next run takes over the pass from run 9042.
        app = self.app_commit(*additions.rglob("additions.json"))
        later = self.select_head(app)
        self.assertNotIn("per-entry", self.runs(later))
        self.assertIn(9042, json.loads(later["reused"]).values())

        # Changing one of its recorded reads makes it run again.
        changed = self.select_head(self.head({"src/other.py": "other changed\n"}))
        self.assertIn("tests/test_three.py", json.loads(changed["entries"])["per-entry"])

    def test_a_unit_reused_in_an_additions_run_is_rekeyed_under_the_applied_lists(self) -> None:
        suites = json.loads(json.dumps(SUITES))
        suites["suites"][0]["lookup_directories"] = ["src"]
        suites["module_lookups"] = {"src": ["alpha"]}
        git(self.root, "checkout", "-q", self.base)
        self.base = self.commit("legacy module lookup", {"suites.json": json.dumps(suites, indent=2) + "\n"})

        # Run 9050 passes pr-only (and the other suites touched by src/feature.py).
        first = self.head({"src/feature.py": "feature v2\n"})
        first_outputs = self.select_head(first)
        self.assertTrue(self.store(first_outputs, self.ALL, 9050))

        # Run 9051 touches src/other.py: pr-only is reused from 9050, while test_two.py runs and reports
        # a new module lookup in src/, which changes pr-only's key under the applied suite list.
        second = self.head({"src/other.py": "other v2\n"})
        second_outputs = self.select_head(second)
        self.assertNotIn("pr-only", self.runs(second_outputs))
        self.assertIn(9050, json.loads(second_outputs["reused"]).values())
        reads = self.NO_READS | {"lookup_directories": ["src"], "module_lookups": {"src": ["beta"]}}
        kept, additions = self.job_with_additions(9051, "shard-1", [("per-entry", "tests/test_two.py")], [
            {"suite": "per-entry", "create": False, "recorded_entries": ["tests/test_two.py"],
             "entries": {"tests/test_two.py": reads}}])
        self.assertTrue(self.store(second_outputs, {"suite-results-shard-1": kept}, 9051, additions=additions))

        # After applying additions, pr-only must still be reused from run 9050 (re-keyed under applied lists).
        app = self.app_commit(*additions.rglob("additions.json"))
        later = self.select_head(app)
        self.assertEqual([], self.runs(later), "both the running unit with additions and the reused unit take over their passes")
        self.assertEqual([9050, 9050, 9050, 9050, 9051], sorted(json.loads(later["reused"]).values()))

    def test_adding_a_module_lookup_name_leaves_the_keys_of_units_that_did_not_look_it_up_unchanged(self) -> None:
        suites = json.loads(json.dumps(SUITES))
        suites["suites"][0]["lookup_directories"] = ["src"]
        suites["suites"][0]["module_lookups"] = {"src": ["alpha"]}
        suites["suites"][3]["lookup_directories"] = ["src"]
        suites["suites"][3]["entry_lookup_directories"] = {"tests/test_one.py": ["src"], "tests/test_two.py": ["src"]}
        suites["suites"][3]["module_lookups"] = {"src": ["alpha"]}
        suites["suites"][3]["entry_module_lookups"] = {"tests/test_one.py": {"src": ["alpha"]}, "tests/test_two.py": {}}
        suites["module_lookups"] = {"src": ["alpha"]}
        git(self.root, "checkout", "-q", self.base)
        self.base = self.commit("per-unit module lookups", {"suites.json": json.dumps(suites, indent=2) + "\n"})

        first = self.head({"src/feature.py": "feature v2\n", "src/other.py": "other v2\n"})
        first_outputs = self.select_head(first)
        self.assertTrue(self.store(first_outputs, self.ALL + [("per-entry", "tests/test_two.py")], 9060))

        # Add "beta" to top-level module_lookups["src"] and only to tests/test_two.py's entry_module_lookups.
        updated = json.loads(json.dumps(suites))
        updated["module_lookups"]["src"] = ["alpha", "beta"]
        updated["suites"][3]["module_lookups"]["src"] = ["alpha", "beta"]
        updated["suites"][3]["entry_module_lookups"]["tests/test_two.py"] = {"src": ["beta"]}
        later = self.head({"suites.json": json.dumps(updated, indent=2) + "\n"})
        later_outputs = self.select_head(later)
        self.assertEqual(["per-entry"], self.runs(later_outputs), "units that did not look up beta keep their reuse keys")
        self.assertEqual(["tests/test_two.py"], json.loads(later_outputs["entries"])["per-entry"])
        self.assertEqual([9060] * 4, sorted(json.loads(later_outputs["reused"]).values()))

        # Adding src/beta.py affects only tests/test_two.py, not pr-only or tests/test_one.py.
        with_beta = self.head({"src/beta.py": "beta module\n"})
        beta_outputs = self.select_head(with_beta)
        self.assertEqual(["per-entry"], self.runs(beta_outputs))
        self.assertEqual(["tests/test_two.py"], json.loads(beta_outputs["entries"])["per-entry"])


if __name__ == "__main__":
    unittest.main()
