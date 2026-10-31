"""Suite selection and verdict commands on isolated repositories."""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.suite_selection_fixtures import (
    DELTA_REASON, FIXTURE_SUITES, FIXTURE_UNIT, FixtureRepositoryTests, force_add, git, write,
)

TOOL = Path(__file__).resolve().parents[1] / "actions/suite-selection"
sys.path.insert(0, str(TOOL))

import ci_definition
import suite_selection


class PatternTests(unittest.TestCase):
    def test_double_star_crosses_separators_and_single_star_and_question_mark_do_not(self) -> None:
        matches = suite_selection.matches
        self.assertTrue(matches("src/**", "src/deep/module.py"))
        self.assertTrue(matches("src/**", "src/module.py"))
        self.assertFalse(matches("src/**", "source/module.py"))
        self.assertTrue(matches("**", "a/b/c.yml"))
        self.assertTrue(matches("checks/*.py", "checks/test_x.py"))
        self.assertFalse(matches("checks/*.py", "checks/deep/test_x.py"))
        self.assertTrue(matches("checks/test_?.py", "checks/test_x.py"))
        self.assertFalse(matches("checks/test_?.py", "checks/test_xy.py"))
        self.assertTrue(matches("**/manifest.json", "settings/codex/manifest.json"))
        self.assertTrue(matches("**/manifest.json", "manifest.json"))
        self.assertTrue(matches("a.b", "a.b"))
        self.assertFalse(matches("a.b", "axb"))


class SuiteListTests(unittest.TestCase):
    def test_a_valid_suite_list_parses_and_renders_back_the_same(self) -> None:
        text = json.dumps(FIXTURE_SUITES, indent=2) + "\n"
        parsed = suite_selection.parse_suite_list(text)
        self.assertEqual([suite.name for suite in parsed.suites], ["alpha", "beta", "gamma", "delta"])
        self.assertIsNone(parsed.suites[3].paths)
        self.assertEqual(suite_selection.render_suite_list(parsed), text)

    def test_an_invalid_suite_list_fails_and_names_the_entry(self) -> None:
        for broken, message in (
            ({"always": [], "suites": [{"name": "Alpha", "entries": ["x.py"], "paths": []}]}, "suites[1]: name"),
            ({"always": [], "suites": [{"name": "a", "entries": [], "paths": []}]}, "(a): entries"),
            ({"always": [], "suites": [{"name": "a", "entries": ["x.py"], "paths": [], "extra": 1}]}, "unknown keys extra"),
            ({"always": [], "suites": [{"name": "a", "entries": ["x.py"], "extra_paths": "guides/**"}]}, "(a): extra_paths"),
            ({"always": [], "suites": [{"name": "a", "entries": ["x.py"],
                                        "extra_directory_memberships": [""]}]}, "(a): extra_directory_memberships[1]"),
            ({"always": [], "suites": [{"name": "a", "entries": ["x.py"], "paths": []}] * 2}, "duplicate suite name a"),
            ({"suites": []}, "the keys always and suites, and optionally module_lookups"),
            ({"always": [], "suites": [], "module_lookups": {"tests": "helper"}}, "module_lookups (tests)"),
            ({"always": [], "suites": [{"name": "a", "entries": ["x.py"], "entry_lookup_directories": {"x.py": ["tests"]}}]},
             "entry_lookup_directories needs entry_selection"),
        ):
            with self.subTest(message=message):
                with self.assertRaises(suite_selection.SuiteListError) as caught:
                    suite_selection.parse_suite_list(json.dumps(broken))
                self.assertIn(message, str(caught.exception))
        with self.assertRaises(suite_selection.SuiteListError):
            suite_selection.parse_suite_list("{not json")

    def test_the_help_usage_and_errors_name_the_suite_list(self) -> None:
        printed = io.StringIO()
        with redirect_stdout(printed), self.assertRaises(SystemExit):
            suite_selection.main(["select", "--help"])
        with self.assertRaises(suite_selection.SuiteListError) as caught:
            suite_selection.parse_suite_list(json.dumps({"suites": []}))
        for label, text in (("help", printed.getvalue()), ("usage", suite_selection.__doc__), ("error", str(caught.exception))):
            with self.subTest(text=label):
                self.assertIn("suite list", text)
                self.assertNotIn("suite file", text)

    def test_a_command_setups_entry_setups_durations_and_os_round_trip(self) -> None:
        text = json.dumps({"always": [], "suites": [
            {"name": "unit", "entries": ["checks/test_a.py", "checks/test_b.py"], "os": "windows",
             "command": ["python", "-m", "unittest"], "setups": ["python"], "entry_setups": {"checks/test_b.py": ["node"]},
             "entry_durations": {"checks/test_a.py": 12.5, "checks/test_b.py": 0}, "entry_selection": True,
             "paths": ["checks/**"], "entry_paths": {"checks/test_a.py": ["checks/test_a.py"], "checks/test_b.py": ["checks/test_b.py"]}},
            {"name": "glob", "entries": ["specs/*.lua"], "command": ["busted"], "setups": [],
             "entry_durations": {"specs/a.lua": 3}},
        ]}, indent=2) + "\n"
        parsed = suite_selection.parse_suite_list(text)
        unit, glob = parsed.suites
        self.assertEqual(unit.command, ("python", "-m", "unittest"))
        self.assertEqual(unit.os, "windows")
        self.assertEqual(unit.setups_of("checks/test_a.py"), ("python",))
        self.assertEqual(unit.setups_of("checks/test_b.py"), ("python", "node"))
        self.assertEqual(dict(unit.entry_durations), {"checks/test_a.py": 12.5, "checks/test_b.py": 0})
        self.assertEqual(glob.os, "linux")
        self.assertEqual(glob.setups, (), "an empty setups list is stated")
        self.assertEqual(suite_selection.render_suite_list(parsed), text, "every new key renders back the same")

    def test_a_malformed_command_setup_duration_or_os_fails_select_with_exit_2(self) -> None:
        base = {"name": "a", "entries": ["x.py", "checks/*.py"], "paths": ["x.py"]}
        for extra, message in (
            ({"command": "python x.py"}, "(a): command must be a list of non-empty strings"),
            ({"command": []}, "(a): command must name at least the program"),
            ({"command": ["python", ""]}, "(a): command must be a list of non-empty strings"),
            ({"setups": "python"}, "(a): setups must be a list of non-empty strings"),
            ({"setups": [7]}, "(a): setups must be a list of non-empty strings"),
            ({"entry_setups": {"x.py": ["node"]}}, "(a): entry_setups needs entry_selection"),
            ({"entry_selection": True, "entry_paths": {"x.py": [], "checks/*.py": []}, "entry_setups": {"y.py": ["node"]}}, "names y.py, which the entries lack"),
            ({"entry_selection": True, "entry_paths": {"x.py": [], "checks/*.py": []}, "entry_setups": {"x.py": "node"}}, "entry_setups (x.py) must be a list"),
            ({"entry_durations": ["x.py", 3]}, "(a): entry_durations must be an object"),
            ({"entry_durations": {"x.py": -1}}, "entry_durations (x.py) must be a number of seconds, zero or more"),
            ({"entry_durations": {"x.py": "3"}}, "entry_durations (x.py) must be a number of seconds"),
            ({"entry_durations": {"x.py": True}}, "entry_durations (x.py) must be a number of seconds"),
            ({"entry_durations": {"checks/deep/y.py": 1}}, "names checks/deep/y.py, which the entries lack"),
            ({"os": "macos"}, "(a): os must be one of linux, windows"),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                suites = Path(directory) / "suites.json"
                suites.write_text(json.dumps({"always": [], "suites": [{**base, **extra}]}), encoding="utf-8")
                error = io.StringIO()
                with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", error):
                    code = suite_selection.main(["select", "--root", directory, "--suites", str(suites), "--changed-file", "x.py"])
                self.assertEqual(code, 2)
                self.assertIn(message, error.getvalue())
        with tempfile.TemporaryDirectory() as directory:
            suites = Path(directory) / "suites.json"
            suites.write_text(json.dumps({"always": [], "suites": [{**base, "entry_durations": {"x.py": float("nan")}}]}), encoding="utf-8")
            with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(suite_selection.main(["select", "--root", directory, "--suites", str(suites), "--changed-file", "x.py"]), 2)

    def test_select_rejects_the_language_python_runner_and_every_change_fields(self) -> None:
        for field, value in (("language", "python"), ("python_runner", "unittest"),
                             ("every_change", {"sites": [{"file": "x.py", "expression": "ROOT / name"}]})):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                suites = Path(directory) / "suites.json"
                suites.write_text(json.dumps({"always": [], "suites": [{"name": "a", "entries": ["x.py"], field: value}]}), encoding="utf-8")
                (Path(directory) / "x.py").write_text("", encoding="utf-8")
                error = io.StringIO()
                with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", error):
                    code = suite_selection.main(["select", "--root", directory, "--suites", str(suites), "--changed-file", "x.py"])
                self.assertEqual(code, 2)
                self.assertIn(f"suites[1] (a): unknown keys {field}", error.getvalue())

    def test_a_suite_without_paths_has_no_stored_list_and_keeps_its_extra_paths(self) -> None:
        text = json.dumps({"always": [], "suites": [
            {"name": "a", "entries": ["x.py"]},
            {"name": "b", "entries": ["checks/*.lua"], "extra_paths": ["data/**"]},
            {"name": "c", "entries": ["x.py"], "paths": ["x.py"], "extra_paths": ["guides/**"]},
        ]}, indent=2) + "\n"
        parsed = suite_selection.parse_suite_list(text)
        self.assertIsNone(parsed.suites[0].paths)
        self.assertIsNone(parsed.suites[1].paths)
        self.assertEqual(parsed.suites[1].extra_paths, ("data/**",))
        self.assertEqual(parsed.suites[2].paths, ("x.py",))
        self.assertEqual(parsed.suites[2].extra_paths, ("guides/**",))
        self.assertEqual(suite_selection.render_suite_list(parsed), text, "entries without paths and extra_paths render back the same")


class SelectionTests(FixtureRepositoryTests):
    """The selection command on a fixture Git repository with commits."""

    def setUp(self) -> None:
        super().setUp()
        write(self.root, "checks/suites.json", json.dumps(FIXTURE_SUITES, indent=2) + "\n")
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "core.autocrlf", "false")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        self.base = git(self.root, "rev-parse", "HEAD")
        self.suite_list = suite_selection.load_suite_list(self.root / "checks" / "suites.json")

    def commit(self, message: str) -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def select(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "checks" / "suites.json"), *arguments])
        return code, output.getvalue()

    def test_a_matching_changed_file_runs_its_suite_and_an_unmatched_one_selects_no_suite(self) -> None:
        write(self.root, "guides/guide.md", "changed\n")
        write(self.root, "stray.txt", "changed\n")
        head = self.commit("docs and stray")
        changed, undetermined = suite_selection.changed_files(self.root, self.base, head, "direct")
        self.assertIsNone(undetermined)
        self.assertEqual(changed, ["guides/guide.md", "stray.txt"])
        selection = suite_selection.select(self.suite_list, changed)
        by_name = {decision.suite: decision for decision in selection.decisions}
        self.assertTrue(by_name["alpha"].run)
        self.assertEqual(by_name["alpha"].reason, "`guides/guide.md` matches `guides/**`")
        self.assertFalse(by_name["beta"].run)
        self.assertEqual(by_name["beta"].reason, "no changed file matches its path list")
        self.assertFalse(by_name["gamma"].run)
        self.assertTrue(by_name["delta"].run)
        self.assertEqual(by_name["delta"].reason, DELTA_REASON)
        self.assertEqual(selection.unread, ("stray.txt",), "the file no suite reads is named")

    def test_a_changed_file_matching_an_always_run_pattern_runs_every_suite_with_that_reason(self) -> None:
        write(self.root, "ci/pipeline.yml", "changed\n")
        head = self.commit("ci")
        changed, _ = suite_selection.changed_files(self.root, self.base, head, "direct")
        selection = suite_selection.select(self.suite_list, changed)
        self.assertTrue(all(decision.run for decision in selection.decisions))
        self.assertEqual({decision.reason for decision in selection.decisions},
                         {"every suite runs: `ci/pipeline.yml` matches the always-run pattern `ci/**`"})
        self.assertEqual(selection.unread, ())

    def test_when_every_changed_file_is_read_by_no_suite_no_listed_suite_runs(self) -> None:
        write(self.root, "stray.txt", "changed\n")
        head = self.commit("stray")
        changed, _ = suite_selection.changed_files(self.root, self.base, head, "direct")
        selection = suite_selection.select(self.suite_list, changed)
        self.assertEqual([decision.suite for decision in selection.decisions if decision.run], ["delta"], "only the every-change suite")
        self.assertEqual(selection.unread, ("stray.txt",))

    def test_a_renamed_file_counts_under_its_old_path_and_a_deleted_file_under_its_path(self) -> None:
        git(self.root, "mv", "src/old.txt", "src/new.txt")
        (self.root / "gone.txt").unlink()
        head = self.commit("rename and delete")
        changed, _ = suite_selection.changed_files(self.root, self.base, head, "direct")
        self.assertEqual(changed, ["gone.txt", "src/new.txt", "src/old.txt"])
        selection = suite_selection.select(self.suite_list, changed)
        by_name = {decision.suite: decision for decision in selection.decisions}
        self.assertTrue(by_name["beta"].run, "the suite whose path list covers only the old path runs")
        self.assertEqual(by_name["beta"].reason, "`src/old.txt` matches `src/old.txt`")
        self.assertTrue(by_name["gamma"].run, "the suite whose path list covers the deleted path runs")
        self.assertEqual(by_name["gamma"].reason, "`gone.txt` matches `gone.txt`")

    def test_a_pull_request_compares_from_the_merge_base_so_the_base_branch_s_own_changes_do_not_count(self) -> None:
        git(self.root, "switch", "-q", "-c", "feature")
        write(self.root, "src/old.txt", "feature\n")
        head = self.commit("feature change")
        git(self.root, "switch", "-q", "main")
        write(self.root, "guides/guide.md", "main moved on\n")
        base = self.commit("main change")
        merge_base_changed, _ = suite_selection.changed_files(self.root, base, head, "merge-base")
        self.assertEqual(merge_base_changed, ["src/old.txt"])
        direct_changed, _ = suite_selection.changed_files(self.root, base, head, "direct")
        self.assertEqual(direct_changed, ["guides/guide.md", "src/old.txt"], "a direct comparison would count the base branch's change")

    def test_a_dependabot_merged_dispatch_selects_what_a_push_from_the_merge_s_first_parent_selects(self) -> None:
        git(self.root, "switch", "-q", "-c", "dependabot")
        write(self.root, "src/old.txt", "updated\n")
        self.commit("dependency update")
        git(self.root, "switch", "-q", "main")
        write(self.root, "guides/guide.md", "main moved on\n")
        parent = self.commit("main change")
        git(self.root, "merge", "-q", "--no-ff", "-m", "merge", "dependabot")
        merged = git(self.root, "rev-parse", "HEAD")
        runs = []
        for base in (parent, f"{merged}^1"):
            outputs = self.root / f"outputs-{len(runs)}.txt"
            code, printed = self.select("--base", base, "--head", merged, "--compare", "direct", "--github-output", str(outputs))
            self.assertEqual(code, 0, printed)
            runs.append((outputs.read_text(encoding="utf-8"), printed))
        self.assertEqual(runs[0], runs[1])
        self.assertEqual(runs[0][0], "alpha=skip\nbeta=run\ngamma=skip\ndelta=run\n", "only the merged change counts, not main's earlier one")

    def test_a_dependabot_merged_dispatch_without_a_sha_has_an_empty_base_and_runs_every_suite(self) -> None:
        outputs = self.root / "outputs.txt"
        code, printed = self.select("--base", "", "--head", self.base, "--compare", "direct", "--github-output", str(outputs))
        self.assertEqual(code, 0, printed)
        self.assertEqual(outputs.read_text(encoding="utf-8"), "alpha=run\nbeta=run\ngamma=run\ndelta=run\n")

    def test_an_absent_or_all_zero_previous_head_cannot_determine_the_changed_files_and_runs_every_suite(self) -> None:
        for base, expected in (("0" * 40, "all-zero commit"), ("1234567890abcdef1234567890abcdef12345678", "absent from the checkout")):
            with self.subTest(base=base):
                changed, undetermined = suite_selection.changed_files(self.root, base, self.base, "direct")
                self.assertIsNone(changed)
                self.assertIn(expected, undetermined)
                selection = suite_selection.select(self.suite_list, changed, undetermined)
                self.assertTrue(all(decision.run for decision in selection.decisions))
                self.assertIn("the changed files could not be determined", selection.decisions[0].reason)
                self.assertIn(expected, selection.decisions[0].reason)
                summary = suite_selection.summary_markdown(selection, changed)
                self.assertIn("The changed files could not be determined", summary)
                self.assertIn("Every suite runs.", summary)

    def test_the_command_writes_step_outputs_and_the_summary_and_lists_every_suite_once(self) -> None:
        write(self.root, "guides/guide.md", "changed\n")
        write(self.root, "stray.txt", "changed\n")
        head = self.commit("docs and stray")
        outputs = self.root / "outputs.txt"
        summary = self.root / "summary.md"
        outputs.write_text("earlier=kept\n", encoding="utf-8")
        code, printed = self.select("--base", self.base, "--head", head, "--compare", "direct",
                                    "--github-output", str(outputs), "--summary", str(summary))
        self.assertEqual(code, 0, printed)
        self.assertEqual(outputs.read_text(encoding="utf-8"), "earlier=kept\nalpha=run\nbeta=skip\ngamma=skip\ndelta=run\n")
        markdown = summary.read_text(encoding="utf-8")
        self.assertIn("| Suite | Result | Reason |", markdown)
        self.assertIn("| `alpha` | run | `guides/guide.md` matches `guides/**` |", markdown)
        self.assertIn("| `beta` | skip | no changed file matches its path list |", markdown)
        self.assertIn(f"| `delta` | run | {DELTA_REASON} |", markdown)
        self.assertIn("Read by no suite: `stray.txt`.", markdown)
        for name in ("alpha", "beta", "gamma", "delta"):
            self.assertEqual(markdown.count(f"| `{name}` |"), 1, "every suite appears once")
        self.assertIn("alpha: run (`guides/guide.md` matches `guides/**`)", printed)
        self.assertIn("Read by no suite: stray.txt", printed)

    def test_the_command_accepts_changed_files_directly_and_selects_by_the_fixture_s_own_lists(self) -> None:
        code, printed = self.select("--changed-file", "assets/one.txt", "--changed-file", "src/old.txt")
        self.assertEqual(code, 0)
        self.assertIn("gamma: run (`assets/one.txt` matches `assets/*.txt`)", printed)
        self.assertIn("beta: run (`src/old.txt` matches `src/old.txt`)", printed)
        self.assertIn("alpha: skip", printed)
        listing = self.root / "changed.txt"
        listing.write_text("checks/suites.json\n", encoding="utf-8")
        code, printed = self.select("--changed-list", str(listing))
        self.assertEqual(code, 0)
        self.assertIn("alpha: run (every suite runs: `checks/suites.json` matches the always-run pattern `checks/suites.json`)", printed)

    def test_an_unreadable_changed_list_fails_the_command_with_exit_2(self) -> None:
        error = io.StringIO()
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", error):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "checks" / "suites.json"),
                                         "--changed-list", str(self.root / "missing.txt")])
        self.assertEqual(code, 2)
        self.assertIn("missing.txt", error.getvalue())

    def test_an_invalid_suite_list_fails_the_command(self) -> None:
        write(self.root, "checks/suites.json", json.dumps({"always": [], "suites": [{"name": "a"}]}))
        error = io.StringIO()
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", error):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "checks" / "suites.json"), "--changed-file", "x"])
        self.assertEqual(code, 2)
        self.assertIn("suites[1] (a): entries", error.getvalue())

class StoredListSelectionTests(FixtureRepositoryTests):
    """Selection from the stored lists alone, on a fixture Git repository."""

    SUITES = {
        "always": ["ci/**"],
        "suites": [
            {"name": "alpha", "entries": ["checks/test_app.py"]},
            {"name": "gamma", "entries": ["checks/*.ps1"], "extra_paths": ["guides/**"]},
            {"name": "stored", "entries": ["checks/beta.py"], "paths": ["src/old.txt"], "extra_paths": ["stray.txt"]},
        ],
    }

    def setUp(self) -> None:
        super().setUp()
        self.scratch = Path(self.temporary.name)
        self.suites = json.loads(json.dumps(self.SUITES))
        self.write_suites()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "core.autocrlf", "false")
        self.base = self.commit("base")

    def write_suites(self) -> None:
        write(self.root, "checks/suites.json", json.dumps(self.suites, indent=2) + "\n")

    def commit(self, message: str) -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def select(self, *arguments: str) -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), mock.patch("sys.stderr", error):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "checks" / "suites.json"), *arguments])
        return code, output.getvalue(), error.getvalue()

    def test_a_suite_without_a_stored_path_list_runs_on_every_change(self) -> None:
        summary = self.scratch / "summary.md"
        code, printed, error = self.select("--changed-file", "stray.txt", "--summary", str(summary))
        self.assertEqual(code, 0, error)
        write(self.root, "guides/guide.md", "changed\n")
        head = self.commit("a change no list reads")
        code, committed, error = self.select("--base", self.base, "--head", head, "--compare", "direct")
        self.assertEqual(code, 0, error)
        for report in (printed, committed):
            self.assertIn("alpha: run (runs on every change: no stored path list)", report)
        self.assertIn("gamma: run (runs on every change: no stored path list)", printed)
        self.assertIn("stored: run (`stray.txt` matches `stray.txt`)", printed)
        self.assertIn("stored: skip", committed)
        self.assertNotIn("Derived path list", summary.read_text(encoding="utf-8"))

    def test_a_suite_with_paths_is_selected_on_its_stored_list_and_extra_paths(self) -> None:
        code, printed, _ = self.select("--changed-file", "src/old.txt")
        self.assertEqual(code, 0, printed)
        self.assertIn("stored: run (`src/old.txt` matches `src/old.txt`)", printed)
        code, printed, _ = self.select("--changed-file", "stray.txt")
        self.assertIn("stored: run (`stray.txt` matches `stray.txt`)", printed, "an extra path of a stored suite")
        code, printed, _ = self.select("--changed-file", "data/table.csv")
        self.assertIn("stored: skip", printed)

    def test_a_glob_entry_names_the_tracked_files_it_matches(self) -> None:
        tracked = suite_selection.tracked_files(self.root)
        suite_list = suite_selection.resolve_entries(suite_selection.load_suite_list(self.root / "checks" / "suites.json"), tracked)
        self.assertEqual(suite_list.suites[1].entries, ("checks/suite.ps1",))
        write(self.root, "checks/untracked.ps1", "")
        suite_list = suite_selection.resolve_entries(suite_selection.load_suite_list(self.root / "checks" / "suites.json"), suite_selection.tracked_files(self.root))
        self.assertEqual(suite_list.suites[1].entries, ("checks/suite.ps1",), "an untracked file matches no entry pattern")
        self.suites["suites"][1]["entries"] = ["checks/*.ps1", "checks/suite.ps1", "scripts/*.ps1"]
        self.write_suites()
        suite_list = suite_selection.resolve_entries(suite_selection.load_suite_list(self.root / "checks" / "suites.json"), tracked)
        self.assertEqual(suite_list.suites[1].entries, ("checks/suite.ps1", "scripts/helper.ps1"), "each file once, in entry order")

    def test_an_entry_or_glob_pattern_that_matches_no_tracked_file_exits_2_naming_the_entry(self) -> None:
        for entry in ("checks/*.lua", "checks/absent.py"):
            with self.subTest(entry=entry):
                self.suites["suites"][0]["entries"] = [entry]
                self.write_suites()
                code, _, error = self.select("--changed-file", "stray.txt")
                self.assertEqual(code, 2)
                self.assertIn(f"suites[1] (alpha): the entry {entry} matches no tracked file", error)

    def test_a_stored_list_selects_on_its_patterns_and_extra_paths(self) -> None:
        self.suites["suites"].append({"name": "lua", "entries": ["checks/beta.py"],
                                      "paths": ["checks/beta.py", "scripts/*.lua"], "extra_paths": ["config/lua.json"]})
        self.write_suites()
        summary = self.scratch / "summary.md"
        code, printed, _ = self.select("--changed-file", "stray.txt", "--summary", str(summary))
        self.assertEqual(code, 0, printed)
        self.assertIn("lua: skip", printed)
        self.assertIn("| `lua` | skip | no changed file matches its path list |", summary.read_text(encoding="utf-8"))
        for path in ("checks/beta.py", "scripts/check.lua", "config/lua.json"):
            with self.subTest(path=path):
                code, printed, error = self.select("--changed-file", path)
                self.assertEqual(code, 0, error)
                self.assertIn("lua: run", printed)

    def test_a_change_that_deletes_a_listed_file_selects_from_the_stored_list_without_extracting_a_revision(self) -> None:
        (self.root / "src" / "old.txt").unlink()
        head = self.commit("delete a listed file")
        with mock.patch.object(suite_selection.subprocess, "run", wraps=suite_selection.subprocess.run) as run:
            code, printed, error = self.select("--base", self.base, "--head", head, "--compare", "direct")
        self.assertEqual(code, 0, error)
        self.assertIn("stored: run (`src/old.txt` matches `src/old.txt`)", printed)
        self.assertFalse(any("archive" in call.args[0] for call in run.call_args_list), "no revision is extracted")


class DirectoryMembershipSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="membership-selection-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "core.autocrlf", "false")
        for name, folder in (("reader", "guides"), ("other", "assets")):
            write(self.root, f"checks/{name}.py", f'from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\nlist((ROOT / "{folder}").iterdir())\n')
        write(self.root, "guides/existing/page.md", "prose\n")
        write(self.root, "guides/direct.md", "prose\n")
        write(self.root, "assets/one.txt")

    def configure(self, stored: bool, membership: str = "guides") -> str:
        entries = ["checks/reader.py", "checks/other.py"]
        suite = {"name": "listing", "entries": entries, "entry_selection": True}
        if stored:
            suite.update(paths=entries, entry_paths={entry: [entry] for entry in entries},
                         directory_memberships=[membership, "assets"],
                         entry_directory_memberships={entries[0]: [membership], entries[1]: ["assets"]})
        write(self.root, "checks/suites.json", json.dumps({"always": [], "suites": [suite]}))
        return self.commit()

    def commit(self) -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", "fixture")
        return git(self.root, "rev-parse", "HEAD")

    def select(self, *arguments: str) -> tuple[int, str, str]:
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), mock.patch("sys.stderr", error):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "checks/suites.json"), *arguments])
        return code, output.getvalue(), error.getvalue()

    def test_committed_child_addition_and_removal_select_only_the_reader(self) -> None:
        for stored in (True,):
            for child in ("new.md", "new/page.md"):
                with self.subTest(stored=stored, child=child):
                    base = self.configure(stored)
                    path = write(self.root, f"guides/{child}")
                    head = self.commit()
                    outputs = self.root / "outputs"
                    code, printed, error = self.select("--base", base, "--head", head, "--compare", "direct", "--github-output", str(outputs))
                    self.assertEqual(code, 0, error)
                    self.assertIn("listing=run\nlisting-entries=checks/reader.py\n", outputs.read_text())
                    outputs.unlink()
                    path.unlink()
                    removed = self.commit()
                    code, printed, error = self.select("--base", head, "--head", removed, "--compare", "direct", "--github-output", str(outputs))
                    self.assertEqual(code, 0, error)
                    self.assertIn("listing=run\nlisting-entries=checks/reader.py\n", outputs.read_text())
                    outputs.unlink()

    def test_content_edits_and_files_below_existing_children_skip(self) -> None:
        for stored in (True,):
            with self.subTest(stored=stored):
                base = self.configure(stored)
                for path in ("guides/direct.md", "guides/existing/page.md", "guides/existing/new.md"):
                    write(self.root, path, f"changed {stored}\n")
                head = self.commit()
                code, printed, error = self.select("--base", base, "--head", head, "--compare", "direct")
                self.assertEqual(code, 0, error)
                self.assertIn("listing: skip", printed)
                code, printed, error = self.select("--changed-file", "guides/direct.md")
                self.assertEqual(code, 0, error)
                self.assertIn("listing: skip", printed)

    def test_stored_memberships_read_each_git_tree_once_without_extracting_contents(self) -> None:
        base = self.configure(True)
        (self.root / "guides/direct.md").unlink()
        head = self.commit()
        with mock.patch.object(suite_selection, "revision_files", wraps=suite_selection.revision_files) as listing:
            code, printed, error = self.select("--base", base, "--head", head, "--compare", "direct")
        self.assertEqual(code, 0, error)
        self.assertIn("listing: run", printed)
        self.assertEqual(listing.call_args_list, [mock.call(self.root, base), mock.call(self.root, head)])

    def test_removing_one_nested_file_keeps_its_existing_child_membership(self) -> None:
        write(self.root, "guides/existing/retained.md")
        base = self.configure(True)
        (self.root / "guides/existing/page.md").unlink()
        head = self.commit()
        code, printed, error = self.select("--base", base, "--head", head, "--compare", "direct")
        self.assertEqual(code, 0, error)
        self.assertIn("listing: skip", printed)

    def test_membership_uses_the_requested_head_and_merge_base(self) -> None:
        self.configure(True)
        git(self.root, "switch", "-q", "-c", "feature")
        write(self.root, "guides/new/page.md")
        head = self.commit()
        git(self.root, "switch", "-q", "main")
        write(self.root, "assets/main-only.txt")
        base = self.commit()
        output = self.root / "outputs"
        code, printed, error = self.select("--base", base, "--head", head, "--compare", "merge-base", "--github-output", str(output))
        self.assertEqual(code, 0, error)
        self.assertIn("listing=run\nlisting-entries=checks/reader.py\n", output.read_text())

    def test_root_listing_uses_dot_and_detects_only_top_level_membership(self) -> None:
        write(self.root, "checks/reader.py", 'from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\nlist(ROOT.iterdir())\n')
        document = {"always": [], "suites": [{"name": "root", "entries": ["checks/reader.py"], "paths": ["checks/reader.py"],
                                              "directory_memberships": ["."]}]}
        self.assertEqual(suite_selection.parse_suite_list(json.dumps(document)).suites[0].directory_memberships, (".",))
        for stored in (True,):
            with self.subTest(stored=stored):
                base = self.configure(stored, ".")
                added = write(self.root, "new-root.txt")
                head = self.commit()
                code, printed, error = self.select("--base", base, "--head", head, "--compare", "direct")
                self.assertEqual(code, 0, error)
                self.assertIn("listing: run", printed)
                added.unlink()
                self.commit()



class ModuleLookupSelectionTests(unittest.TestCase):
    """A change that adds a file or directory in a directory in which entry files look up modules, under a
    standard-library name or a stored module lookup of that directory."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="lookup-selection-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "core.autocrlf", "false")
        for entry in ("tests/test_lookup.py", "tests/test_other.py"):
            write(self.root, entry, "import helper\n")
        write(self.root, "lib/helper.py", "VALUE = 1\n")
        write(self.root, "tests/pkg/module.py", "")
        entries = ["tests/test_lookup.py", "tests/test_other.py"]
        suite = {"name": "lookups", "entries": entries, "entry_selection": True, "paths": entries,
                 "entry_paths": {entry: [entry, "lib/helper.py"] for entry in entries},
                 "lookup_directories": ["tests", "lib"], "entry_lookup_directories": {entries[0]: ["tests"], entries[1]: ["lib"]}}
        write(self.root, "checks/suites.json", json.dumps({"always": [], "suites": [suite], "module_lookups": {"tests": ["helper", "pkg"]}}))
        self.base = self.commit()

    def commit(self) -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", "fixture")
        return git(self.root, "rev-parse", "HEAD")

    def selected(self, files: list[str]) -> tuple[str, str]:
        """The running entry files and the printed report of a commit that adds ``files`` to the base commit."""
        git(self.root, "checkout", "-q", "--detach", self.base)
        for file in files:
            write(self.root, file, "added\n")
        head = self.commit()
        outputs = self.root.parent / "lookup-outputs"
        outputs.unlink(missing_ok=True)
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), mock.patch("sys.stderr", error):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "checks/suites.json"), "--base", self.base,
                                         "--head", head, "--compare", "direct", "--github-output", str(outputs)])
        self.assertEqual(code, 0, error.getvalue())
        entries = re.search(r"^lookups-entries=(.*)$", outputs.read_text(encoding="utf-8"), re.MULTILINE)
        return (entries.group(1) if entries else ""), output.getvalue()

    def test_an_added_file_or_directory_with_a_stored_lookup_or_a_standard_library_name_selects_the_entry_files_that_look_up_there(self) -> None:
        for added in (["tests/helper.py"], ["tests/json.py"], ["tests/helper/__init__.py"], ["tests/json/data.txt"], ["tests/helper.cpython-313-x86_64-linux-gnu.so"]):
            with self.subTest(added=added):
                entries, printed = self.selected(added)
                self.assertEqual("tests/test_lookup.py", entries, printed)
                self.assertIn(f"`{added[0]}` matches `module lookup `{added[0].split('/')[1].split('.')[0]}` in `tests`` of `tests/test_lookup.py`", printed)

    def test_a_lookup_counts_only_in_its_own_directory(self) -> None:
        # helper is a stored lookup of tests/ alone; in lib/ only the standard-library names count.
        self.assertEqual("", self.selected(["lib/helper/__init__.py"])[0])
        self.assertEqual("tests/test_other.py", self.selected(["lib/json.py"])[0])

    def test_other_names_and_files_inside_an_existing_directory_select_nothing(self) -> None:
        for added in (["tests/notes.py"], ["tests/pkg/other.py"], ["docs/json.py"]):
            with self.subTest(added=added):
                self.assertEqual("", self.selected(added)[0])

    def test_an_init_file_in_an_existing_directory_of_a_lookup_name_selects(self) -> None:
        # tests/pkg was a namespace portion; an __init__ file makes it a package that precedes later portions.
        self.assertEqual("tests/test_lookup.py", self.selected(["tests/pkg/__init__.py"])[0])

    def test_module_lookups_roundtrip_in_the_suite_list(self) -> None:
        text = (self.root / "checks/suites.json").read_text(encoding="utf-8")
        parsed = suite_selection.parse_suite_list(text)
        self.assertEqual((("tests", ("helper", "pkg")),), parsed.module_lookups)
        self.assertEqual(("tests",), parsed.suites[0].lookup_directories_of("tests/test_lookup.py"))
        self.assertEqual(json.loads(text), json.loads(suite_selection.render_suite_list(parsed)))

    def test_the_standard_library_names_are_the_union_over_the_supported_python_versions(self) -> None:
        names = suite_selection.standard_library_names()
        self.assertLessEqual(set(sys.stdlib_module_names), names)
        self.assertLessEqual({"json", "aifc", "telnetlib", "annotationlib", "compression"}, names)
        # The script that renews the file writes the union over the interpreters it is given, with their versions.
        written = self.root / "names.json"
        script = suite_selection.STANDARD_LIBRARY.parent / "stdlib_names.py"
        subprocess.run([sys.executable, str(script), "--output", str(written), sys.executable, sys.executable], check=True)
        self.assertEqual({"versions": ["%d.%d" % sys.version_info[:2]], "names": sorted(sys.stdlib_module_names)},
                         json.loads(written.read_text(encoding="utf-8")))


class GlobMembershipModeTests(unittest.TestCase):
    """A glob membership pattern ending in /** selects on the same changes with --changed-file as between two commits."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "core.autocrlf", "false")
        write(self.root, ".gitignore", "__pycache__/\n")
        write(self.root, "checks/test_tools.py")
        write(self.root, "tools/alpha/TOOL.md", "alpha\n")
        write(self.root, "tools/alpha/run.py", "run\n")
        write(self.root, "tools/beta/TOOL.md", "beta\n")
        write(self.root, "checks/suites.json", json.dumps({"always": [], "suites": [
            {"name": "tools", "entries": ["checks/test_tools.py"], "paths": ["checks/test_tools.py"],
             "directory_memberships": ["tools/*/**"]}]}))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        self.base = git(self.root, "rev-parse", "HEAD")
        write(self.root, "tools/alpha/__pycache__/run.cpython-313.pyc", "untracked\n")

    def select(self, *arguments: str) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "checks/suites.json"), *arguments])
        self.assertEqual(code, 0)
        return "run" if "tools: run" in output.getvalue() else "skip"

    def decisions(self, change, changed: list[str]) -> tuple[str, str]:
        """The decision with --changed-file on the uncommitted change, then between two commits after committing it."""
        change()
        listed = self.select(*[argument for file in changed for argument in ("--changed-file", file)])
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "change")
        committed = self.select("--base", self.base, "--head", git(self.root, "rev-parse", "HEAD"), "--compare", "direct")
        return listed, committed

    def test_a_content_change_under_a_matching_directory_selects_in_neither_mode(self) -> None:
        for path in ("tools/alpha/run.py", "tools/beta/TOOL.md"):
            with self.subTest(path=path):
                self.assertEqual(self.decisions(lambda: write(self.root, path, "changed\n"), [path]), ("skip", "skip"))
                self.base = git(self.root, "rev-parse", "HEAD")

    def test_an_added_removed_or_renamed_matching_path_selects_in_both_modes(self) -> None:
        def rename() -> None:
            (self.root / "tools/alpha/run.py").rename(self.root / "tools/alpha/start.py")

        for name, change, changed in (
            ("added", lambda: write(self.root, "tools/beta/new.py", "new\n"), ["tools/beta/new.py"]),
            ("removed", lambda: (self.root / "tools/beta/TOOL.md").unlink(), ["tools/beta/TOOL.md"]),
            ("renamed", rename, ["tools/alpha/run.py", "tools/alpha/start.py"]),
        ):
            with self.subTest(change=name):
                self.assertEqual(self.decisions(change, changed), ("run", "run"))
                self.base = git(self.root, "rev-parse", "HEAD")


class ShardTests(unittest.TestCase):
    """The shard matrix, the Windows entry files and the language step in the JSON outputs of select, on fixture suite lists."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q", "-b", "main")
        for name in "abcdefgh":
            write(self.root, f"checks/test_{name}.py")
        write(self.root, "checks/test_win.py")
        write(self.root, "src/app.py")
        write(self.root, "docs/guide.md")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")

    def suite(self, name: str, durations: dict[str, float], **extra) -> dict:
        entries = [f"checks/test_{entry}.py" for entry in durations] if "entries" not in extra else extra.pop("entries")
        suite = {"name": name, "entries": entries, "paths": ["src/**"],
                 "command": ["python", "-m", "unittest"], "setups": ["python"]}
        estimates = {f"checks/test_{entry}.py": value for entry, value in durations.items() if value is not None}
        if estimates:
            suite["entry_durations"] = estimates
        suite.update(extra)
        return suite

    def outputs(self, suites: list[dict], *arguments: str) -> dict[str, str]:
        write(self.root, "suites.json", json.dumps({"always": [], "suites": suites}))
        output = self.root / "outputs.txt"
        output.unlink(missing_ok=True)
        with redirect_stdout(io.StringIO()):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "suites.json"),
                                         "--json-output", str(output), *arguments])
        self.assertEqual(code, 0)
        return dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())

    def shards(self, suites: list[dict], *arguments: str) -> list[dict]:
        return json.loads(self.outputs(suites, *(arguments or ("--changed-file", "src/app.py")))["shards"])

    @staticmethod
    def layout(shards: list[dict]) -> list[tuple[float, list[str]]]:
        return [(shard["estimate"], [item["entry"].removeprefix("checks/test_").removesuffix(".py") for item in shard["entries"]]) for shard in shards]

    def test_the_fewest_shards_keep_each_shard_s_two_worker_time_under_120_s_assigned_greedily_by_estimate(self) -> None:
        # One shard would take 180 s on its two workers (100 + 50 + 30 against 70 + 60 + 40 after greedy assignment).
        # Greedy by estimate over two shards: 100, 70, 60 to the second, 50 to the first, 40 to the second, 30 to the first.
        shards = self.shards([self.suite("unit", {"a": 100, "b": 70, "c": 60, "d": 50, "e": 40, "f": 30})])
        self.assertEqual(self.layout(shards), [(100, ["a", "d", "f"]), (100, ["b", "c", "e"])])
        self.assertEqual([shard["shard"] for shard in shards], [1, 2])
        self.assertEqual(shards[0]["entries"][0], {"suite": "unit", "entry": "checks/test_a.py", "command": ["python", "-m", "unittest"]})
        self.assertEqual(shards[0]["setups"], ["python"])

    def test_work_that_fits_two_workers_under_the_bound_stays_in_one_shard(self) -> None:
        shards = self.shards([self.suite("unit", {"a": 60, "b": 50, "c": 50, "d": 59})])
        self.assertEqual(self.layout(shards), [(110, ["a", "d", "b", "c"])])

    def test_an_entry_file_over_the_bound_gets_a_shard_of_its_own(self) -> None:
        shards = self.shards([self.suite("unit", {"a": 130, "b": 50, "c": 40})])
        self.assertEqual(self.layout(shards), [(130, ["a"]), (50, ["b", "c"])])

    def test_an_entry_file_without_an_estimate_counts_with_the_mean_of_the_stored_estimates(self) -> None:
        # With b at the mean of 100, one shard would take 200 s; with b at 0 it would fit in one shard. On a tie the
        # first shard takes the entry file.
        shards = self.shards([self.suite("unit", {"a": 100, "b": None}), self.suite("other", {"c": 100})])
        self.assertEqual(self.layout(shards), [(100, ["a", "c"]), (100, ["b"])])

    def test_an_entry_file_without_an_estimate_counts_with_the_mean_of_its_operating_system(self) -> None:
        # Linux stores 100 and 50 s, Windows 300 s: the Linux mean is 75 s, where the mean over both would be 150 s.
        windows = self.suite("windows", {}, entries=["checks/test_a.py", "checks/test_b.py"], os="windows", paths=["docs/**"],
                             entry_durations={"checks/test_a.py": 300})
        outputs = self.outputs([self.suite("unit", {"a": 100, "b": 50, "c": None}, paths=["docs/**"]), windows],
                               "--changed-file", "docs/guide.md")
        # c at 75 s fits beside b; at 150 s it would take a shard of its own.
        self.assertEqual(self.layout(json.loads(outputs["shards"])), [(100, ["a"]), (75, ["c", "b"])])
        self.assertEqual(json.loads(outputs["windows"])["estimate"], 600, "checks/test_b.py counts at the Windows mean of 300 s")

    def test_without_any_stored_estimate_every_selected_entry_file_shares_one_shard(self) -> None:
        shards = self.shards([self.suite("unit", {"a": None, "b": None, "c": None})])
        self.assertEqual(self.layout(shards), [(0, ["a", "b", "c"])])

    def test_only_selected_entry_files_are_sharded_with_the_setups_their_suites_and_entries_need(self) -> None:
        per_entry = self.suite("per-entry", {"a": 10, "b": 10}, entry_selection=True, setups=["python"],
                               entry_paths={"checks/test_a.py": ["src/**"], "checks/test_b.py": ["docs/**"]},
                               entry_setups={"checks/test_a.py": ["node"]})
        skipped = self.suite("skipped", {"c": 10}, paths=["docs/**"], setups=["dotnet"])
        shards = self.shards([per_entry, skipped])
        self.assertEqual(self.layout(shards), [(10, ["a"])])
        self.assertEqual(shards[0]["setups"], ["python", "node"])

    def test_the_selected_windows_entry_files_are_listed_for_the_windows_job_and_not_sharded(self) -> None:
        windows = self.suite("windows", {}, entries=["checks/test_win.py"], os="windows", paths=["docs/**"], setups=["python"],
                             entry_durations={"checks/test_win.py": 80})
        linux = self.suite("unit", {"a": 10})
        outputs = self.outputs([linux, windows], "--changed-file", "docs/guide.md")
        self.assertEqual(json.loads(outputs["windows"]), {"estimate": 80, "setups": ["python"], "entries": [
            {"suite": "windows", "entry": "checks/test_win.py", "command": ["python", "-m", "unittest"]}]})
        self.assertEqual(json.loads(outputs["shards"]), [], "no Linux suite is selected")
        outputs = self.outputs([linux, windows], "--changed-file", "src/app.py")
        self.assertEqual(outputs["windows"], "", "no Windows entry file is selected, so the Windows job has nothing to start")
        self.assertEqual(self.layout(json.loads(outputs["shards"])), [(10, ["a"])])

    def test_the_windows_estimate_is_the_sum_of_its_entry_files_as_the_job_runs_them_one_after_another(self) -> None:
        # On two workers 80, 50 and 30 s would take 80 s; the Windows job runs them one after another.
        windows = self.suite("windows", {}, entries=["checks/test_a.py", "checks/test_b.py", "checks/test_c.py"], os="windows",
                             paths=["docs/**"], entry_durations={"checks/test_a.py": 80, "checks/test_b.py": 50, "checks/test_c.py": 30})
        outputs = self.outputs([windows], "--changed-file", "docs/guide.md")
        self.assertEqual(json.loads(outputs["windows"])["estimate"], 160)

    def test_a_selected_python_language_step_without_a_linux_suite_gets_one_shard_without_suites(self) -> None:
        suites = [self.suite("unit", {"a": 10}, paths=["src/**"])]
        outputs = self.outputs(suites, "--changed-file", "tools/lint.py", "--language-step", "python")
        self.assertEqual(json.loads(outputs["language-steps"]), {"python": "run"})
        self.assertEqual(json.loads(outputs["shards"]), [{"shard": 1, "estimate": 0, "setups": [], "entries": []}])
        outputs = self.outputs(suites, "--changed-file", "pyproject.toml", "--language-step", "python")
        self.assertEqual(json.loads(outputs["language-steps"]), {"python": "run"}, "Python configuration selects the step")
        outputs = self.outputs(suites, "--changed-file", "docs/guide.md", "--language-step", "python")
        self.assertEqual(json.loads(outputs["language-steps"]), {"python": "skip"})
        self.assertEqual(json.loads(outputs["shards"]), [])
        outputs = self.outputs(suites, "--changed-file", "tools/lint.py")
        self.assertEqual(json.loads(outputs["shards"]), [], "a caller that does not call the step gets no shard for it")
        self.assertNotIn("language-steps", outputs)
        outputs = self.outputs(suites, "--changed-file", "src/app.py", "--language-step", "python")
        self.assertEqual(self.layout(json.loads(outputs["shards"])), [(10, ["a"])], "the step runs in the first shard of the selected suites")

    def test_the_full_recording_runs_every_entry_file_in_shards_under_300_s_of_wide_traced_time(self) -> None:
        # Stored estimates are mid-set seconds; the full recording counts them at 1.65 times under the wide set.
        # 100, 80, 60 and 50 s become 165, 132, 99 and 82.5 s: 247.5 s on two workers, so one shard, where a
        # pull-request run of the same entry files needs two shards under its 120 s bound.
        suites = [self.suite("unit", {"a": 100, "b": 80}, paths=["docs/**"]), self.suite("other", {"c": 60, "d": 50}, paths=["docs/**"])]
        outputs = self.outputs(suites, "--full")
        self.assertEqual(self.layout(json.loads(outputs["shards"])), [(247.5, ["a", "b", "c", "d"])])
        self.assertEqual(json.loads(outputs["suites"]), {"unit": "run", "other": "run"})
        self.assertEqual(len(self.shards(suites, "--changed-file", "docs/guide.md")), 2)

    def test_the_full_recording_splits_work_over_300_s_of_wide_traced_time(self) -> None:
        # 150, 100 and 90 s become 247.5, 165 and 148.5 s: one shard would take 313.5 s on its two workers.
        shards = json.loads(self.outputs([self.suite("unit", {"a": 150, "b": 100, "c": 90})], "--full")["shards"])
        self.assertEqual(self.layout(shards), [(247.5, ["a"]), (165, ["b", "c"])])

    def test_the_full_recording_lists_every_windows_entry_file(self) -> None:
        windows = self.suite("windows", {}, entries=["checks/test_win.py"], os="windows", paths=["docs/**"],
                             entry_durations={"checks/test_win.py": 80})
        per_entry = self.suite("per-entry", {"a": 10, "b": 10}, entry_selection=True,
                               entry_paths={"checks/test_a.py": ["docs/**"], "checks/test_b.py": ["docs/**"]})
        outputs = self.outputs([per_entry, windows], "--full")
        self.assertEqual([item["entry"] for item in json.loads(outputs["windows"])["entries"]], ["checks/test_win.py"])
        self.assertEqual(json.loads(outputs["windows"])["estimate"], 80, "the Windows job records under its one ETW session")
        self.assertEqual(self.layout(json.loads(outputs["shards"])), [(16.5, ["a", "b"])])

    def test_the_full_recording_takes_no_changed_files(self) -> None:
        write(self.root, "suites.json", json.dumps({"always": [], "suites": [self.suite("unit", {"a": 10})]}))
        for extra in (["--changed-file", "src/app.py"], ["--base", "HEAD", "--head", "HEAD"]):
            with self.subTest(extra=extra), redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()), \
                    self.assertRaises(SystemExit) as caught:
                suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "suites.json"), "--full", *extra])
            self.assertEqual(caught.exception.code, 2)

    def test_an_unknown_language_step_fails_select_with_exit_2(self) -> None:
        write(self.root, "suites.json", json.dumps({"always": [], "suites": [self.suite("unit", {"a": 10})]}))
        error = io.StringIO()
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", error), self.assertRaises(SystemExit) as caught:
            suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "suites.json"),
                                  "--changed-file", "src/app.py", "--language-step", "cobol"])
        self.assertEqual(caught.exception.code, 2)


FIXTURE_CALLER = """\
name: Validation

on:
  pull_request:
  push:
    branches: [main]

permissions: {}

jobs:
  select:
    permissions:
      contents: read
    uses: ./.github/workflows/selection.yml
    with:
      suites: checks/suites.json
  unit:
    needs: select
    uses: ./.github/workflows/unit.yml  # the validation workflow
    with:
      suites: ${{ needs.select.outputs.suites }}
  validation:
    needs: [select, unit]
    if: always()
    uses: ./.github/workflows/verdict.yml
    with:
      needs: ${{ toJSON(needs) }}
"""


class CiDefinitionTests(FixtureRepositoryTests):
    """The CI-definition rule on fixture workflows in a fixture Git repository."""

    def setUp(self) -> None:
        super().setUp()
        write(self.root, ".github/workflows/validation.yml", FIXTURE_CALLER)
        write(self.root, ".github/workflows/unit.yml", FIXTURE_UNIT)
        write(self.root, ".github/workflows/selection.yml", "on:\n  workflow_call:\njobs:\n  select:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: $/kit/selection\n")
        write(self.root, ".github/workflows/verdict.yml", "on:\n  workflow_call:\njobs:\n  validation:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n")
        write(self.root, "tooling/setup/action.yml", "runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: cat tooling/defaults.json\n")
        write(self.root, "tooling/defaults.json", "{}\n")
        write(self.root, "kit/selection/action.yml", "runs:\n  using: composite\n  steps:\n    - shell: bash\n      env:\n        RESOURCE_PATH: ${{ github.action_path }}\n      run: python \"$RESOURCE_PATH/select.py\"\n")
        write(self.root, "kit/selection/select.py", "")
        write(self.root, "kit/selection/notes.txt", "a file of the action's folder that no step names\n")
        write(self.root, "checks/suites.json", json.dumps(FIXTURE_SUITES, indent=2) + "\n")
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "core.autocrlf", "false")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        self.tracked = suite_selection.tracked_files(self.root)

    def definition(self) -> list:
        return ci_definition.ci_definition(self.root, ".github/workflows/validation.yml", "checks/suites.json", self.tracked)

    def test_the_definition_holds_the_caller_the_used_workflows_and_actions_the_named_files_and_the_suite_list_in_order(self) -> None:
        items = self.definition()
        self.assertEqual([(item.pattern, item.role) for item in items], [
            (".github/workflows/validation.yml", "the calling workflow"),
            (".github/workflows/selection.yml", "a workflow it uses"),
            ("checks/suites.json", "a file that a step of `.github/workflows/validation.yml` names"),
            (".github/workflows/unit.yml", "a workflow it uses"),
            (".github/workflows/verdict.yml", "a workflow it uses"),
            ("kit/selection/action.yml", "a local action it uses"),
            ("tooling/setup/action.yml", "a local action it uses"),
            ("config/settings.toml", "a file that a step of `.github/workflows/unit.yml` names"),
            ("checks/**", "a directory that a step of `.github/workflows/unit.yml` names"),
            ("ci/pipeline.yml", "a file that a step of `.github/workflows/unit.yml` names"),
            ("kit/selection/select.py", "a file that a step of `kit/selection/action.yml` names"),
            ("tooling/defaults.json", "a file that a step of `tooling/setup/action.yml` names"),
        ])

    def test_a_local_action_contributes_its_action_file_and_the_files_its_steps_name_not_its_folder(self) -> None:
        """The action file and the file its step runs through an environment variable set to github.action_path are
        in the definition; another file of the action's folder is not. Without the step that runs it, the script is
        out as well, which is what makes this case go red under the old whole-folder rule."""
        patterns = [item.pattern for item in self.definition()]
        self.assertIn("kit/selection/action.yml", patterns)
        self.assertIn("kit/selection/select.py", patterns)
        self.assertNotIn("kit/selection/notes.txt", patterns)
        self.assertNotIn("kit/selection/**", patterns)
        suite_list = suite_selection.load_suite_list(self.root / "checks" / "suites.json")
        selection = suite_selection.select(suite_list, ["kit/selection/notes.txt"], definition=self.definition())
        self.assertEqual([decision.suite for decision in selection.decisions if decision.run], ["delta"], "a file of the folder that no step names changes nothing")
        write(self.root, "kit/selection/action.yml", "runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: echo ok\n")
        patterns = [item.pattern for item in self.definition()]
        self.assertIn("kit/selection/action.yml", patterns)
        self.assertNotIn("kit/selection/select.py", patterns, "a script the action no longer runs leaves the definition")
        write(self.root, "kit/selection/action.yml", "runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: cat ${{ github.action_path }}/select.py\n")
        self.assertIn("kit/selection/select.py", [item.pattern for item in self.definition()], "github.action_path itself resolves to the folder")

    def test_a_change_to_the_selection_definition_does_not_run_product_suites(self) -> None:
        suite_list = suite_selection.load_suite_list(self.root / "checks" / "suites.json")
        for changed in (
            ".github/workflows/validation.yml",
            ".github/workflows/unit.yml",
            "kit/selection/action.yml",
            "kit/selection/select.py",
        ):
            with self.subTest(changed=changed):
                selection = suite_selection.select(suite_list, ["stray.txt", changed], definition=self.definition())
                self.assertEqual([decision.suite for decision in selection.decisions if decision.run], ["delta"],
                                 "an ungated workflow or selection-tool read is not a reason to run product suites")
        selection = suite_selection.select(suite_list, ["stray.txt"], definition=self.definition())
        self.assertEqual([decision.suite for decision in selection.decisions if decision.run], ["delta"], "a file outside the definition changes nothing")

    def test_no_word_of_a_comment_in_a_run_script_counts_as_a_path_the_step_names(self) -> None:
        """A shell comment in a run script may mention a directory or a file; neither joins the definition. The same
        words outside a comment do, which is what makes this case go red without the comment rule."""
        script = "          # The fixtures under the ci folder are copied; see tooling/defaults.json\n          echo ok  # ci again\n"
        write(self.root, ".github/workflows/unit.yml", FIXTURE_UNIT.replace("          python -m unittest\n", script))
        patterns = [item.pattern for item in self.definition()]
        self.assertNotIn("ci/**", patterns, "a directory named in a comment is no part of the definition")
        self.assertNotIn("tooling/defaults.json", patterns[:patterns.index("tooling/setup/action.yml")], "a file named in a comment is no part of the definition")
        self.assertIn("ci/pipeline.yml", patterns, "the cp line before the comment still names its file")
        write(self.root, ".github/workflows/unit.yml", FIXTURE_UNIT.replace("          python -m unittest\n", script.replace("# ", "")))
        patterns = [item.pattern for item in self.definition()]
        self.assertIn("ci/**", patterns, "the same words outside a comment name the directory")
        self.assertEqual(ci_definition.without_comments('echo "a # not a comment" x#y # tail\n#whole\nkeep'), 'echo "a # not a comment" x#y \n\nkeep')
        self.assertEqual(ci_definition.without_comments('python -c "x = 1 # kit/keep.py\nprint(x)" # ci/drop.yml\n'), 'python -c "x = 1 # kit/keep.py\nprint(x)" \n',
                         "a quote spanning lines keeps its text; the comment after it goes")
        self.assertEqual(ci_definition.without_comments("echo \\# not a comment\n<# a ci/drop.yml\nblock #> echo ok\n"), "echo \\# not a comment\n  echo ok\n",
                         "an escaped hash and a PowerShell block comment")

    def test_a_change_to_the_calling_workflow_is_named_before_any_other_part(self) -> None:
        suite_list = suite_selection.load_suite_list(self.root / "checks" / "suites.json")
        changed = [".github/workflows/selection.yml", ".github/workflows/validation.yml"]
        selection = suite_selection.select(suite_list, changed, definition=self.definition())
        self.assertFalse(selection.decisions[0].run)
        self.assertEqual(selection.decisions[-1].reason, DELTA_REASON)

    def test_the_command_takes_the_workflow_as_a_path_or_as_the_workflow_ref_and_names_the_suite_list(self) -> None:
        for workflow in (".github/workflows/validation.yml", "owner/repo/.github/workflows/validation.yml@refs/pull/3/merge"):
            with self.subTest(workflow=workflow):
                output = io.StringIO()
                with redirect_stdout(output):
                    code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "checks" / "suites.json"),
                                                 "--workflow", workflow, "--changed-file", "checks/suites.json"])
                self.assertEqual(code, 0)
                self.assertIn("alpha: run (every suite runs: `checks/suites.json` matches the always-run pattern `checks/suites.json`)", output.getvalue())
        self.assertEqual(suite_selection.workflow_path("owner/repo/.github/workflows/validation.yml@refs/heads/main"), ".github/workflows/validation.yml")

    def test_an_unreadable_calling_workflow_fails_the_command_without_selecting_suites(self) -> None:
        write(self.root, ".github/workflows/validation.yml", "jobs:\n  a:\n    broken line\n")
        output = io.StringIO()
        with redirect_stdout(output):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "checks" / "suites.json"),
                                         "--workflow", ".github/workflows/validation.yml", "--changed-file", "stray.txt"])
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")


PAIR_SUITES = {
    "always": ["ci/**", "checks/suites.json"],
    "suites": [
        {"name": "pair", "entries": ["checks/test_app.py", "checks/beta.py"], "entry_selection": True,
         "paths": ["checks/beta.py", "checks/test_app.py", "guides/**", "src/old.txt"],
         "entry_paths": {"checks/test_app.py": ["checks/test_app.py", "guides/**"], "checks/beta.py": ["checks/beta.py", "src/old.txt"]},
         "entry_extra_paths": {"checks/beta.py": ["assets/*.txt"]}},
        {"name": "whole", "entries": ["checks/unlisted.py"], "paths": ["checks/unlisted.py", "gone.txt"]},
    ],
}


GATED_CALLER = """\
name: Validate

on:
  pull_request:
  push:
    branches: [main]

jobs:
  validate:
    runs-on: ubuntu-latest
    outputs:
      gamma: ${{ steps.select.outputs.gamma }}
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000 # v1.0.0
      - name: Reconcile
        if: steps.select.outputs.alpha == 'run'
        run: cat tooling/defaults.json checks/test_app.py
      - uses: ./tooling/setup
      - id: select
        run: python kit/select.py --suites checks/suites.json
      - name: Install the tools of two suites
        if: steps.select.outputs.alpha == 'run' || steps.select.outputs.beta == 'run'
        run: python tooling/install.py --pins tooling/pins.json
      - if: steps.select.outputs.alpha == 'run'
        run: python -m unittest checks.test_app
      - if: steps.select.outputs.beta == 'run'
        run: cat tooling/pins.json && python -m unittest checks.beta
      - if: steps.select.outputs.delta == 'run'
        run: cat tooling/other-form.txt

  other:
    needs: validate
    if: needs.validate.outputs.gamma == 'run'
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000 # v1.0.0
      - run: cat tooling/platform.json
      - run: pwsh checks/suite.ps1
"""


class GatedStepTests(FixtureRepositoryTests):
    """A file that only steps gated on particular suites name runs those suites, not every suite; a file an ungated
    step names runs every suite, whatever gated steps name it too."""

    def setUp(self) -> None:
        super().setUp()
        write(self.root, ".github/workflows/validation.yml", GATED_CALLER)
        write(self.root, "tooling/setup/action.yml", "runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: cat tooling/defaults.json\n")
        for name in ("tooling/defaults.json", "tooling/pins.json", "tooling/platform.json", "tooling/install.py", "tooling/other-form.txt", "kit/select.py"):
            write(self.root, name, "")
        write(self.root, "checks/suites.json", json.dumps(FIXTURE_SUITES, indent=2) + "\n")
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "core.autocrlf", "false")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        self.suite_list = suite_selection.load_suite_list(self.root / "checks" / "suites.json")
        self.definition = ci_definition.ci_definition(self.root, ".github/workflows/validation.yml", "checks/suites.json", suite_selection.tracked_files(self.root))

    def gates(self) -> dict[str, tuple[str, ...]]:
        return {item.pattern: tuple(sorted(item.suites)) for item in self.definition}

    def test_each_part_carries_the_suites_of_the_gates_of_the_steps_that_name_it(self) -> None:
        gates = self.gates()
        self.assertEqual(gates["tooling/install.py"], ("alpha", "beta"), "one step gated on two suites")
        self.assertEqual(gates["tooling/pins.json"], ("alpha", "beta"), "two gated steps: the union of their gates")
        self.assertEqual(gates["tooling/platform.json"], ("gamma",), "a step of a job gated on one suite")
        self.assertEqual(gates["checks/suite.ps1"], ("gamma",))
        self.assertEqual(gates["tooling/other-form.txt"], ("delta",))
        self.assertEqual(gates["checks/suites.json"], (), "an ungated step")
        self.assertEqual(gates["kit/select.py"], ())
        self.assertEqual(gates["tooling/setup/action.yml"], ())

    def test_an_ungated_step_that_names_a_file_wins_over_the_gated_steps_that_name_it(self) -> None:
        """The first step, gated on alpha, names the defaults file before the ungated setup action does; the action's
        reference makes it a file that runs every suite, with the action as its role."""
        items = {item.pattern: item for item in self.definition}
        self.assertEqual(items["tooling/defaults.json"].suites, frozenset())
        self.assertEqual(items["tooling/defaults.json"].role, "a file that a step of `tooling/setup/action.yml` names")

    def test_a_file_only_gated_steps_name_runs_the_suites_of_those_gates_with_every_entry_file(self) -> None:
        selection = suite_selection.select(self.suite_list, ["tooling/platform.json"], definition=self.definition)
        by_name = {decision.suite: decision for decision in selection.decisions}
        self.assertTrue(by_name["gamma"].run)
        self.assertEqual(by_name["gamma"].reason, "`tooling/platform.json` belongs to the CI definition (a file that a step of `.github/workflows/validation.yml` names) and only steps gated on this suite name it")
        self.assertEqual(by_name["gamma"].entries, ("checks/suite.ps1",))
        self.assertFalse(by_name["alpha"].run, by_name["alpha"].reason)
        self.assertFalse(by_name["beta"].run, by_name["beta"].reason)
        self.assertEqual(selection.unread, (), "the gated steps read the file")
        self.assertIn("| `gamma` | run | `tooling/platform.json` belongs to the CI definition", suite_selection.summary_markdown(selection, ["tooling/platform.json"]))
        selection = suite_selection.select(self.suite_list, ["tooling/install.py"], definition=self.definition)
        self.assertEqual([decision.suite for decision in selection.decisions if decision.run], ["alpha", "beta", "delta"], "delta runs on every change")
        selection = suite_selection.select(self.suite_list, ["tooling/other-form.txt"], definition=self.definition)
        self.assertEqual([decision.suite for decision in selection.decisions if decision.run], ["delta"])

    def test_an_action_used_by_a_gated_and_an_ungated_step_contributes_its_files_without_a_gate(self) -> None:
        """A used action is read under the gate of each use that widens it: an ungated use after a gated one makes the
        files its steps name run every suite, and a second gated use adds its suites to theirs."""
        write(self.root, "tooling/shared/action.yml", "runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: python tooling/shared/run.py\n")
        write(self.root, "tooling/shared/run.py", "")
        caller = GATED_CALLER.replace("      - uses: ./tooling/setup\n",
                                      "      - if: steps.select.outputs.alpha == 'run'\n        uses: ./tooling/shared\n      - uses: ./tooling/setup\n      - uses: ./tooling/shared\n")
        write(self.root, ".github/workflows/validation.yml", caller)
        git(self.root, "add", "-A")
        gates = {item.pattern: tuple(sorted(item.suites)) for item in
                 ci_definition.ci_definition(self.root, ".github/workflows/validation.yml", "checks/suites.json", suite_selection.tracked_files(self.root))}
        self.assertEqual(gates["tooling/shared/action.yml"], ())
        self.assertEqual(gates["tooling/shared/run.py"], (), "the ungated use reads the action again without a gate")
        caller = GATED_CALLER.replace("      - uses: ./tooling/setup\n",
                                      "      - if: steps.select.outputs.alpha == 'run'\n        uses: ./tooling/shared\n      - uses: ./tooling/setup\n"
                                      "      - if: steps.select.outputs.beta == 'run'\n        uses: ./tooling/shared\n")
        write(self.root, ".github/workflows/validation.yml", caller)
        gates = {item.pattern: tuple(sorted(item.suites)) for item in
                 ci_definition.ci_definition(self.root, ".github/workflows/validation.yml", "checks/suites.json", suite_selection.tracked_files(self.root))}
        self.assertEqual(gates["tooling/shared/action.yml"], ("alpha", "beta"))
        self.assertEqual(gates["tooling/shared/run.py"], ("alpha", "beta"), "the second gated use reads the action again under both gates")

    def test_a_gate_on_a_name_the_suite_list_lacks_fails_clearly(self) -> None:
        write(self.root, ".github/workflows/validation.yml", GATED_CALLER.replace("if: needs.validate.outputs.gamma == 'run'", "if: needs.validate.outputs.omega == 'run'"))
        definition = ci_definition.ci_definition(self.root, ".github/workflows/validation.yml", "checks/suites.json", suite_selection.tracked_files(self.root))
        self.assertEqual({item.pattern: item.suites for item in definition}["tooling/platform.json"], frozenset({"omega"}))
        with self.assertRaisesRegex(suite_selection.SuiteListError, "gated on `omega`, which the suite list lacks$"):
            suite_selection.select(self.suite_list, ["tooling/platform.json"], definition=definition)
        error = io.StringIO()
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", error):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "checks" / "suites.json"),
                                         "--workflow", ".github/workflows/validation.yml", "--changed-file", "tooling/platform.json"])
        self.assertEqual(code, 2)
        self.assertTrue(error.getvalue().rstrip("\n").endswith("which the suite list lacks"), error.getvalue())

    def test_json_caller_gates_and_unrelated_conditions_keep_scoped_attribution(self) -> None:
        write(self.root, ".github/workflows/validation.yml", GATED_CALLER.replace(
            "if: steps.select.outputs.alpha == 'run'", "if: fromJSON(steps.select.outputs.suites).alpha == 'run'", 1))
        definition = ci_definition.ci_definition(self.root, ".github/workflows/validation.yml", "checks/suites.json", suite_selection.tracked_files(self.root))
        self.assertEqual({item.pattern: item.suites for item in definition}["checks/test_app.py"], frozenset({"alpha"}))
        write(self.root, ".github/workflows/validation.yml", GATED_CALLER.replace(
            "if: steps.select.outputs.alpha == 'run'", "if: github.ref == 'refs/heads/main'", 1))
        definition = ci_definition.ci_definition(self.root, ".github/workflows/validation.yml", "checks/suites.json", suite_selection.tracked_files(self.root))
        self.assertEqual({item.pattern: item.suites for item in definition}["checks/test_app.py"], frozenset())

    def test_a_module_gate_keeps_its_exact_entry_on_the_definition_item(self) -> None:
        write(self.root, ".github/workflows/validation.yml", GATED_CALLER.replace(
            "if: steps.select.outputs.alpha == 'run'", "if: contains(needs.select.outputs.alpha-modules, 'checks.test_app')", 1))
        definition = ci_definition.ci_definition(self.root, ".github/workflows/validation.yml", "checks/suites.json", suite_selection.tracked_files(self.root))
        item = {item.pattern: item for item in definition}["checks/test_app.py"]
        self.assertEqual(item.suites, frozenset({"alpha"}))
        self.assertEqual(item.entries_for("alpha"), frozenset({"checks/test_app.py"}))

    def test_an_entry_gate_on_the_entries_object_keeps_its_exact_entry_on_the_definition_item(self) -> None:
        write(self.root, ".github/workflows/validation.yml", GATED_CALLER.replace(
            "if: steps.select.outputs.alpha == 'run'", "if: contains(fromJSON(inputs.entries).alpha, 'checks/test_app.py')", 1))
        definition = ci_definition.ci_definition(self.root, ".github/workflows/validation.yml", "checks/suites.json", suite_selection.tracked_files(self.root))
        item = {item.pattern: item for item in definition}["checks/test_app.py"]
        self.assertEqual(item.suites, frozenset({"alpha"}))
        self.assertEqual(item.entries_for("alpha"), frozenset({"checks/test_app.py"}))

    def test_a_suite_whose_own_path_list_holds_the_file_keeps_its_own_reason(self) -> None:
        """A file that a suite reads and that a step gated on it names: the suite runs for the read; the definition adds
        nothing and the summary keeps the path-list reason."""
        self.assertEqual(self.gates()["checks/test_app.py"], ("alpha",))
        selection = suite_selection.select(self.suite_list, ["checks/test_app.py"], definition=self.definition)
        by_name = {decision.suite: decision for decision in selection.decisions}
        self.assertTrue(by_name["alpha"].run)
        self.assertEqual(by_name["alpha"].reason, "`checks/test_app.py` matches `checks/test_app.py`")


def dot_caller(command: str = "python ../scripts/build.py", step: str = "", job: str = "", workflow: str = "") -> str:
    """A calling workflow with one step gated on beta that runs ``command``; ``step``, ``job`` and ``workflow`` add
    settings at those levels."""
    return ("name: Validate\non:\n  pull_request:\n" + workflow + "jobs:\n  validate:\n    runs-on: ubuntu-latest\n" + job
            + "    steps:\n      - id: select\n        run: python kit/select.py --suites checks/suites.json\n"
            "      - if: steps.select.outputs.beta == 'run'\n" + step + f"        run: {command}\n")


STEP_DIRECTORY = "        working-directory: app\n"
JOB_DIRECTORY = "    defaults:\n      run:\n        working-directory: app\n"
WORKFLOW_DIRECTORY = "defaults:\n  run:\n    working-directory: app\n"
BUILD_REASON = ("beta: run (`scripts/build.py` belongs to the CI definition (a file that a step of `.github/workflows/validation.yml` "
                "names) and only steps gated on this suite name it)")


class DotSegmentTests(FixtureRepositoryTests):
    """A ``run`` path that starts with ``..`` resolves against the step's working directory, and ``..`` segments in any
    reference resolve, before the reference is matched against tracked paths."""

    def setUp(self) -> None:
        super().setUp()
        for name in ("kit/select.py", "scripts/build.py", "tools/scripts/build.py", "app/lint.py", "outside.py", "tools/shared/x.py"):
            write(self.root, name, "")
        write(self.root, "tools/part/action.yml", "runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: python \"$GITHUB_ACTION_PATH/../shared/x.py\"\n")
        write(self.root, "checks/suites.json", json.dumps(FIXTURE_SUITES, indent=2) + "\n")
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "core.autocrlf", "false")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")

    def use(self, caller: str) -> None:
        write(self.root, ".github/workflows/validation.yml", caller)
        git(self.root, "add", "-A")

    def select(self, changed: str) -> tuple[int, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "checks" / "suites.json"),
                                         "--workflow", ".github/workflows/validation.yml", "--changed-file", changed])
        return code, output.getvalue()

    def patterns(self) -> list[str]:
        definition = ci_definition.ci_definition(self.root, ".github/workflows/validation.yml", "checks/suites.json", suite_selection.tracked_files(self.root))
        return [item.pattern for item in definition]

    def test_a_dot_path_of_a_step_resolves_against_the_working_directory_of_the_step(self) -> None:
        self.use(dot_caller(step=STEP_DIRECTORY))
        code, output = self.select("scripts/build.py")
        self.assertEqual(code, 0, output)
        self.assertIn(BUILD_REASON, output)
        self.assertIn("alpha: skip", output)

    def test_a_dot_path_of_a_step_without_a_working_directory_resolves_against_the_default_of_the_job_or_the_workflow(self) -> None:
        for name, caller in (("job", dot_caller(job=JOB_DIRECTORY)), ("workflow", dot_caller(workflow=WORKFLOW_DIRECTORY))):
            with self.subTest(defaults=name):
                self.use(caller)
                code, output = self.select("scripts/build.py")
                self.assertEqual(code, 0, output)
                self.assertIn(BUILD_REASON, output)

    def test_the_working_directory_of_the_step_wins_over_the_default_of_the_job(self) -> None:
        self.use(dot_caller(step=STEP_DIRECTORY, job=JOB_DIRECTORY.replace("app", "tools/sub")))
        patterns = self.patterns()
        self.assertIn("scripts/build.py", patterns)
        self.assertNotIn("tools/scripts/build.py", patterns)
        self.use(dot_caller(job=JOB_DIRECTORY.replace("app", "tools/sub"), workflow=WORKFLOW_DIRECTORY))
        self.assertIn("tools/scripts/build.py", self.patterns(), "the default of the job wins over that of the workflow")

    def test_dot_segments_resolve_after_the_action_path_of_a_local_action(self) -> None:
        self.use(dot_caller().replace("        run: python ../scripts/build.py\n", "        uses: ./tools/part\n"))
        code, output = self.select("tools/shared/x.py")
        self.assertEqual(code, 0, output)
        self.assertIn("beta: run (`tools/shared/x.py` belongs to the CI definition (a file that a step of `tools/part/action.yml` names)", output)
        for name, step in (("an expression", "      run: python ${{ github.action_path }}/../shared/x.py\n"),
                           ("a variable bound to it", "      env:\n        PART: ${{ github.action_path }}\n      run: python \"$PART/../shared/x.py\"\n")):
            with self.subTest(action_path=name):
                write(self.root, "tools/part/action.yml", "runs:\n  using: composite\n  steps:\n    - shell: bash\n" + step)
                self.assertIn("tools/shared/x.py", self.patterns())

    def test_dot_segments_in_a_with_value_resolve_from_the_repository_root(self) -> None:
        self.use(dot_caller().replace("        run: python ../scripts/build.py\n",
                                      "        uses: ./tools/part\n        with:\n          script: tools/part/../shared/x.py\n"))
        write(self.root, "tools/part/action.yml", "runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: echo ok\n")
        self.assertIn("tools/shared/x.py", self.patterns())

    def test_a_dot_path_above_the_repository_root_names_no_file(self) -> None:
        self.use(dot_caller(command="echo ok", step=STEP_DIRECTORY))
        without = self.patterns()
        for command in ("python ../../outside.py", "python app/../../outside.py"):
            with self.subTest(command=command):
                self.use(dot_caller(command=command, step=STEP_DIRECTORY))
                code, output = self.select("outside.py")
                self.assertEqual(code, 0, output)
                self.assertIn("beta: skip", output)
                self.assertEqual(self.patterns(), without)

    def test_a_dot_path_under_a_working_directory_outside_the_repository_stays_unresolved(self) -> None:
        temporary = "        working-directory: ${{ runner.temp }}\n"
        for name, job in (("without job defaults", ""), ("with job defaults", JOB_DIRECTORY)):
            with self.subTest(name):
                self.use(dot_caller(command="echo ok", step=temporary, job=job))
                without = self.patterns()
                self.use(dot_caller(step=temporary, job=job))
                code, output = self.select("scripts/build.py")
                self.assertEqual(code, 0, output)
                self.assertIn("beta: skip", output)
                self.assertEqual(self.patterns(), without)

    def test_a_run_path_without_dot_segments_keeps_its_reading_from_the_repository_root(self) -> None:
        self.use(dot_caller(command="python scripts/build.py lint.py", step=STEP_DIRECTORY))
        patterns = self.patterns()
        self.assertIn("scripts/build.py", patterns)
        self.assertIn("app/**", patterns, "the working-directory value names its tree")
        self.assertNotIn("app/lint.py", patterns, "a path without .. does not resolve against the working directory")


class EntrySelectionTests(FixtureRepositoryTests):
    """A suite selected per entry file: each entry runs when a changed file matches its own list; the step runs
    the selected modules; a suite without the mark behaves as before."""

    def suite_list(self, document: dict = PAIR_SUITES):
        return suite_selection.parse_suite_list(json.dumps(document), "checks/suites.json")

    def test_a_changed_file_selects_the_entry_files_whose_lists_it_matches_and_the_outputs_name_them(self) -> None:
        suite_list = self.suite_list()
        selection = suite_selection.select(suite_list, ["guides/intro.md"])
        pair = selection.decisions[0]
        self.assertEqual((pair.run, pair.entries, pair.per_entry), (True, ("checks/test_app.py",), True))
        self.assertEqual(pair.reason, "`guides/intro.md` matches `guides/**` of `checks/test_app.py`")
        self.assertEqual(pair.modules, ("checks.test_app",))
        self.assertFalse(selection.decisions[1].run)
        outputs = suite_selection.github_outputs(selection, suite_list)
        self.assertEqual(outputs, "pair=run\npair-entries=checks/test_app.py\npair-modules=checks.test_app\nwhole=skip\n",
                         "a suite without entry_selection gets its run or skip line and nothing else")
        self.assertEqual(suite_selection.entries_json(selection, suite_list), '{"pair":["checks/test_app.py"]}')
        self.assertIn("| `pair` | run | `guides/intro.md` matches `guides/**` of `checks/test_app.py`; entries: `checks/test_app.py` |",
                      suite_selection.summary_markdown(selection, ["guides/intro.md"]))
        both = suite_selection.select(suite_list, ["guides/intro.md", "src/old.txt"]).decisions[0]
        self.assertEqual(both.entries, ("checks/test_app.py", "checks/beta.py"))
        self.assertTrue(both.reason.endswith(" and 1 more"))

    def test_an_entry_s_extra_paths_select_that_entry_alone_and_an_unmatched_change_skips_the_suite(self) -> None:
        suite_list = self.suite_list()
        beta_only = suite_selection.select(suite_list, ["assets/notes.txt"]).decisions[0]
        self.assertEqual((beta_only.run, beta_only.entries), (True, ("checks/beta.py",)))
        skipped = suite_selection.select(suite_list, ["stray.txt"]).decisions[0]
        self.assertEqual((skipped.run, skipped.entries, skipped.reason), (False, (), "no changed file matches the path list of any of its entry files"))
        self.assertIn("pair-entries=\npair-modules=\n", suite_selection.github_outputs(suite_selection.select(suite_list, ["stray.txt"]), suite_list))

    def test_a_definition_module_gate_runs_only_its_selected_entry(self) -> None:
        suite_list = self.suite_list()
        item = ci_definition.Item("tooling/setup.py", "a file that a step names", frozenset({"pair"}),
                                  (("pair", frozenset({"checks/test_app.py"})),))
        decision = suite_selection.select(suite_list, ["tooling/setup.py"], definition=[item]).decisions[0]
        self.assertEqual((decision.run, decision.entries, decision.per_entry), (True, ("checks/test_app.py",), True))

    def test_an_always_run_or_definition_match_runs_every_entry_file(self) -> None:
        suite_list = self.suite_list()
        every = suite_selection.select(suite_list, ["ci/pipeline.yml"]).decisions[0]
        self.assertEqual((every.run, every.entries, every.per_entry), (True, ("checks/test_app.py", "checks/beta.py"), False))
        self.assertEqual(suite_selection.github_outputs(suite_selection.select(suite_list, ["ci/pipeline.yml"]), suite_list).splitlines()[1],
                         "pair-entries=checks/test_app.py checks/beta.py")

    def test_a_suite_selected_per_entry_without_a_stored_list_runs_every_entry_file_on_every_change(self) -> None:
        document = {"always": [], "suites": [{"name": "pair", "entries": ["checks/test_app.py", "checks/beta.py"], "entry_selection": True}]}
        pair = suite_selection.select(self.suite_list(document), ["guides/intro.md"], root=self.root).decisions[0]
        self.assertEqual((pair.run, pair.entries, pair.reason), (True, ("checks/test_app.py", "checks/beta.py"), "runs on every change: no stored path list"))

    def test_the_suite_list_rejects_entry_lists_that_do_not_fit_the_entries(self) -> None:
        base = PAIR_SUITES["suites"][0]
        for change, message in (
            ({"entry_selection": False}, "entry_paths needs entry_selection"),
            ({"entry_paths": {"checks/test_app.py": ["checks/test_app.py"]}}, "must name every entry file"),
            ({"entry_extra_paths": {"checks/other.py": ["x"]}}, "names checks/other.py, which the entries lack"),
            ({"entry_selection": "yes"}, "entry_selection must be true or false"),
        ):
            with self.subTest(change=change):
                with self.assertRaisesRegex(suite_selection.SuiteListError, message):
                    self.suite_list({"always": [], "suites": [{**base, **change}]})
        with self.assertRaisesRegex(suite_selection.SuiteListError, "a stored suite with entry_selection needs entry_paths"):
            self.suite_list({"always": [], "suites": [{key: value for key, value in base.items() if key != "entry_paths"}]})
        plain = {key: value for key, value in base.items() if key not in ("entry_selection", "entry_paths", "entry_extra_paths")}
        suite = self.suite_list({"always": [], "suites": [plain]}).suites[0]
        self.assertFalse(suite.entry_selection)
        self.assertEqual(suite.entry(), plain, "a suite list without the new keys reads and renders as before")
        self.assertEqual(self.suite_list().suites[0].entry(), base, "the new keys render as stated")


VALIDATE_WORKFLOW = """\
name: Validate

on: [push]

jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000 # v1.0.0
      - run: python kit/selection/select.py --suites checks/suites.json
        id: select
      - run: python -m unittest checks.test_app -v
        if: steps.select.outputs.alpha == 'run'
      - run: python -m unittest checks.beta -v
        if: steps.select.outputs.beta == 'run'
  other:
    needs: validate
    if: needs.validate.outputs.gamma == 'run'
    runs-on: ubuntu-latest
    steps:
      - run: pwsh checks/suite.ps1
"""


class WorkflowStepRuleTests(FixtureRepositoryTests):
    """The calling workflow compared per step between the two commits: a change inside the steps of particular
    suites runs those suites and the suites that read the workflow; any other change runs every suite."""

    def setUp(self) -> None:
        super().setUp()
        write(self.root, ".github/workflows/validate.yml", VALIDATE_WORKFLOW)
        write(self.root, "kit/selection/select.py", "")
        write(self.root, "checks/suites.json", json.dumps(FIXTURE_SUITES, indent=2) + "\n")
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "core.autocrlf", "false")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        self.base = git(self.root, "rev-parse", "HEAD")

    def head_after(self, text: str) -> str:
        write(self.root, ".github/workflows/validate.yml", text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "change")
        return git(self.root, "rev-parse", "HEAD")

    def decisions(self, head: str) -> dict[str, str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "checks" / "suites.json"),
                                         "--base", self.base, "--head", head, "--compare", "direct", "--workflow", ".github/workflows/validate.yml"])
        self.assertEqual(code, 0)
        return {line.split(": ", 1)[0]: line.split(": ", 1)[1] for line in output.getvalue().splitlines()[1:]}

    def test_a_change_inside_one_suite_s_gated_step_runs_that_suite_and_not_the_others(self) -> None:
        head = self.head_after(VALIDATE_WORKFLOW.replace("python -m unittest checks.beta -v", "python -m unittest checks.beta -v --locals"))
        decisions = self.decisions(head)
        self.assertEqual(decisions["beta"], "run (its step in `.github/workflows/validate.yml` changed)")
        self.assertTrue(decisions["alpha"].startswith("skip"), decisions["alpha"])
        self.assertTrue(decisions["gamma"].startswith("skip"), decisions["gamma"])
        self.assertTrue(decisions["delta"].startswith("run"), "the every-change suite runs as always")
        self.assertEqual(ci_definition.changed_step_suites(VALIDATE_WORKFLOW, VALIDATE_WORKFLOW.replace("pwsh checks/suite.ps1", "pwsh checks/suite.ps1 -Verbose")),
                         {"gamma"}, "a step of a job gated on one suite belongs to that suite")
        self.assertEqual(ci_definition.changed_step_suites(VALIDATE_WORKFLOW, VALIDATE_WORKFLOW.replace("checks.beta -v", "checks.beta").replace("checks.test_app -v", "checks.test_app")),
                         {"alpha", "beta"})
        shared = VALIDATE_WORKFLOW.replace("if: steps.select.outputs.beta == 'run'", "if: steps.select.outputs.alpha == 'run' || steps.select.outputs.beta == 'run'")
        self.assertEqual(ci_definition.changed_step_suites(shared, shared.replace("checks.beta -v", "checks.beta")), {"alpha", "beta"},
                         "a step gated on two suites with || belongs to both, as in the CI definition")

    def test_workflow_edits_select_only_their_gated_suites_and_never_all_product_suites(self) -> None:
        changed = {
            "selection step": (VALIDATE_WORKFLOW.replace("--suites checks/suites.json", "--suites checks/suites.json --verbose"), set()),
            "gated step added": (VALIDATE_WORKFLOW.replace("  other:\n", "      - run: python -m unittest checks.beta --locals\n        if: steps.select.outputs.beta == 'run'\n  other:\n"), {"beta"}),
            "gated step removed": (VALIDATE_WORKFLOW.replace("      - run: python -m unittest checks.beta -v\n        if: steps.select.outputs.beta == 'run'\n", ""), {"beta"}),
            "gate changed": (VALIDATE_WORKFLOW.replace("if: steps.select.outputs.beta == 'run'", "if: steps.select.outputs.alpha == 'run'"), {"alpha", "beta"}),
            "job gate changed": (VALIDATE_WORKFLOW.replace("if: needs.validate.outputs.gamma == 'run'", "if: needs.validate.outputs.beta == 'run'"), {"beta", "gamma"}),
        }
        for label, (text, expected) in changed.items():
            with self.subTest(change=label):
                self.assertEqual(ci_definition.changed_step_suites(VALIDATE_WORKFLOW, text), expected)
        head = self.head_after(changed["selection step"][0])
        decisions = self.decisions(head)
        self.assertEqual([name for name, result in decisions.items() if result.startswith("run")], ["delta"], decisions)


class IgnoreRuleSelectionTests(unittest.TestCase):
    """A change to the tracked .gitignore files, or an added path that the head commit's ignore rules match, runs every
    unit; only the tracked .gitignore files decide, matched case-sensitively."""

    EVERY = {"reads": "run", "each": "run"}

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        git(self.root.parent, "init", "-q", "-b", "main", str(self.root))
        git(self.root, "config", "core.autocrlf", "false")
        write(self.root, ".gitignore", "*.log\n!keep.log\nbuild/\n/cache\n")
        write(self.root, "docs/.gitignore", "draft-*\n")
        write(self.root, "checks/test_reads.py")
        write(self.root, "checks/test_one.py")
        write(self.root, "checks/test_two.py")
        write(self.root, "src/app.py", "app\n")
        write(self.root, "old.log", "tracked although ignored\n")
        write(self.root, "checks/suites.json", json.dumps({"always": [], "suites": [
            {"name": "reads", "entries": ["checks/test_reads.py"], "paths": ["checks/test_reads.py", "src/**"]},
            {"name": "each", "entries": ["checks/test_one.py", "checks/test_two.py"], "paths": ["checks/**"],
             "entry_selection": True, "entry_paths": {"checks/test_one.py": ["checks/test_one.py"],
                                                      "checks/test_two.py": ["checks/test_two.py"]}}]}))
        git(self.root, "add", "-A")
        git(self.root, "add", "-f", "old.log")
        git(self.root, "commit", "-q", "-m", "base")
        self.base = git(self.root, "rev-parse", "HEAD")

    def select(self, *arguments: str) -> tuple[dict[str, str], str]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "checks/suites.json"), *arguments])
        self.assertEqual(code, 0)
        lines = [line.split(": ", 1) for line in output.getvalue().splitlines()[1:] if not line.startswith("Read by no suite")]
        return {name: result.split(" ", 1)[0] for name, result in lines}, output.getvalue()

    def committed(self) -> tuple[dict[str, str], str]:
        git(self.root, "commit", "-q", "-m", "change")
        return self.select("--base", self.base, "--head", git(self.root, "rev-parse", "HEAD"), "--compare", "direct")

    def test_a_change_that_adds_removes_or_changes_a_tracked_gitignore_file_runs_every_unit(self) -> None:
        for label, change, ignore_file in (
                ("changed", lambda: force_add(self.root, {".gitignore": "*.log\n"}), ".gitignore"),
                ("added", lambda: force_add(self.root, {"src/.gitignore": "*.tmp\n"}), "src/.gitignore"),
                ("removed", lambda: git(self.root, "rm", "-q", "docs/.gitignore"), "docs/.gitignore")):
            with self.subTest(change=label):
                change()
                for mode, (decisions, printed) in (("listed", self.select("--changed-file", ignore_file)), ("committed", self.committed())):
                    self.assertEqual(self.EVERY, decisions, mode)
                    self.assertIn(f"every suite runs: the tracked ignore file `{ignore_file}` changed", printed, mode)
                    self.assertIn("each: run", printed)
                git(self.root, "reset", "-q", "--hard", self.base)

    def test_an_added_path_that_the_ignore_rules_of_the_head_commit_match_runs_every_unit(self) -> None:
        for path in ("notes.log", "deep/trace.log", "build/out.bin", "src/build/out.bin", "cache/state", "docs/draft-1.md"):
            with self.subTest(path=path):
                force_add(self.root, {path: "forced\n"})
                for mode, (decisions, printed) in (("listed", self.select("--changed-file", path)), ("committed", self.committed())):
                    self.assertEqual(self.EVERY, decisions, mode)
                    self.assertIn(f"every suite runs: the added `{path}` is matched by the ignore rules of the head commit", printed, mode)
                git(self.root, "reset", "-q", "--hard", self.base)

    def test_an_added_path_that_no_tracked_rule_matches_selects_as_usual(self) -> None:
        # A negated pattern re-includes keep.log, /cache is anchored to the root, draft-* belongs to docs/ alone, and
        # matching is case-sensitive.
        for path in ("keep.log", "src/cache", "draft-1.md", "NOTES.LOG", "src/Build/out.bin"):
            with self.subTest(path=path):
                force_add(self.root, {path: "added\n"})
                expected = {"reads": "run" if path.startswith("src/") else "skip", "each": "skip"}
                self.assertEqual(expected, self.select("--changed-file", path)[0], "listed")
                self.assertEqual(expected, self.committed()[0], "committed")
                git(self.root, "reset", "-q", "--hard", self.base)

    def test_local_ignore_settings_and_untracked_gitignore_files_have_no_effect(self) -> None:
        git(self.root, "config", "core.ignoreCase", "true")
        (self.root / ".git/info").mkdir(exist_ok=True)
        (self.root / ".git/info/exclude").write_text("*.md\n", encoding="utf-8")
        excludes = self.root.parent / "global-ignore"
        excludes.write_text("*.txt\n", encoding="utf-8")
        git(self.root, "config", "core.excludesFile", str(excludes))
        write(self.root, "src/.gitignore", "*.py\n")
        force_add(self.root, {"README.md": "readme\n", "notes.txt": "notes\n", "src/new.py": "new\n", "NOTES.LOG": "upper case\n"})
        changed = ["README.md", "notes.txt", "src/new.py", "NOTES.LOG"]
        listed = self.select(*[argument for path in changed for argument in ("--changed-file", path)])[0]
        self.assertEqual({"reads": "run", "each": "skip"}, listed)
        self.assertEqual({"reads": "run", "each": "skip"}, self.committed()[0])

    def test_a_list_of_changed_files_counts_an_edited_tracked_gitignore_file_as_changed(self) -> None:
        # The edit that drops *.log is neither staged nor listed: HEAD's rules still match the forced-added notes.log.
        force_add(self.root, {"notes.log": "forced\n"})
        write(self.root, ".gitignore", "build/\n")
        decisions, printed = self.select("--changed-file", "notes.log")
        self.assertEqual(self.EVERY, decisions)
        self.assertIn("every suite runs: the tracked ignore file `.gitignore` changed", printed)
        git(self.root, "checkout", "-q", "--", ".gitignore")
        decisions, printed = self.select("--changed-file", "notes.log")
        self.assertEqual(self.EVERY, decisions)
        self.assertIn("every suite runs: the added `notes.log` is matched by the ignore rules of the head commit", printed)

    def test_a_changed_tracked_path_that_the_rules_match_selects_as_usual_between_commits(self) -> None:
        force_add(self.root, {"old.log": "changed\n"})
        self.assertEqual({"reads": "skip", "each": "skip"}, self.committed()[0])


class VerdictTests(unittest.TestCase):
    """The verdict command on fixture needs JSON."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def verdict(self, needs: object) -> tuple[int, str, str, str]:
        path = self.root / "needs.json"
        path.write_text(needs if isinstance(needs, str) else json.dumps(needs), encoding="utf-8")
        summary = self.root / "summary.md"
        summary.write_text("", encoding="utf-8")
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), mock.patch("sys.stderr", error):
            code = suite_selection.main(["verdict", "--needs", str(path), "--summary", str(summary)])
        return code, output.getvalue(), summary.read_text(encoding="utf-8"), error.getvalue()

    @staticmethod
    def needs(select: str = "success", **jobs: str) -> dict:
        context = {"select": {"result": select, "outputs": {"suites": json.dumps({"unit": "run", "scripts": "skip"})}}}
        context.update({name: {"result": result, "outputs": {}} for name, result in jobs.items()})
        return context

    def test_the_verdict_passes_when_select_succeeded_and_every_other_job_succeeded_or_was_skipped(self) -> None:
        code, printed, markdown, _ = self.verdict(self.needs(unit="success", scripts="skipped"))
        self.assertEqual(code, 0, printed)
        self.assertIn("Verdict: passed", printed)
        self.assertIn("**Passed.**", markdown)
        self.assertIn("| `unit` | run |", markdown)
        self.assertIn("| `scripts` | skip |", markdown)
        self.assertIn("| `select` | success |", markdown)
        self.assertIn("| `scripts` | skipped |", markdown)
        code, _, markdown, _ = self.verdict(self.needs())
        self.assertEqual(code, 0, "a selection that skipped every suite passes")
        self.assertIn("**Passed.**", markdown)

    def test_the_verdict_fails_when_select_did_not_succeed(self) -> None:
        for result in ("failure", "cancelled", "skipped"):
            with self.subTest(result=result):
                code, printed, markdown, _ = self.verdict(self.needs(select=result, unit="skipped"))
                self.assertEqual(code, 1)
                self.assertIn(f"Verdict: failed: `select` ended {result}", printed)
                self.assertIn(f"**Failed.** `select` ended {result}.", markdown)
        code, printed, _, _ = self.verdict({"unit": {"result": "success", "outputs": {}}})
        self.assertEqual(code, 1)
        self.assertIn("the needs context has no `select` job", printed)

    def test_the_verdict_fails_when_a_called_job_ended_failure_or_cancelled(self) -> None:
        code, printed, markdown, _ = self.verdict(self.needs(unit="failure", scripts="cancelled", docs="success"))
        self.assertEqual(code, 1)
        self.assertIn("Verdict: failed: `unit` ended failure; `scripts` ended cancelled", printed)
        self.assertIn("**Failed.** `unit` ended failure; `scripts` ended cancelled.", markdown)
        self.assertIn("| `docs` | success |", markdown)

    def test_a_missing_or_invalid_selection_output_is_noted_without_changing_the_rule(self) -> None:
        needs = self.needs(unit="success")
        needs["select"]["outputs"] = {}
        code, printed, markdown, _ = self.verdict(needs)
        self.assertEqual(code, 0)
        self.assertIn("the `select` job has no `suites` output to list", markdown)
        self.assertIn("the `select` job has no `suites` output to list", printed)

    def test_an_unreadable_needs_context_exits_2(self) -> None:
        for text in ("{not json", "[]", json.dumps({"select": "success"})):
            with self.subTest(text=text):
                code, _, _, error = self.verdict(text)
                self.assertEqual(code, 2)
                self.assertIn("the needs context", error)


RECORDED_VALIDATE = """\
name: Validate

on:
  pull_request:

permissions: {}

concurrency:
  group: validate-${{ github.ref }}
  cancel-in-progress: true

jobs:
  select:
    permissions:
      contents: read
    uses: $/.github/workflows/suite-selection.yml
    with:
      suites: checks/suites.json
      language-steps: python

  linux:
    needs: select
    if: needs.select.outputs.shards != '[]'
    name: shard ${{ matrix.shard.shard }}
    runs-on: ubuntu-latest
    permissions:
      contents: read
    strategy:
      matrix:
        shard: ${{ fromJSON(needs.select.outputs.shards) }}
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000 # v1.0.0
        with:
          fetch-depth: 0
      - if: contains(matrix.shard.setups, 'python')
        uses: actions/setup-python@0000000000000000000000000000000000000000 # v1.0.0
        with:
          python-version: "3.13"
      - name: Install Lua
        if: contains(matrix.shard.setups, 'lua')
        run: sudo apt-get install -y lua5.4
      - id: recording
        uses: $/actions/recording
      - name: Start the recording
        id: start
        run: python3 "$RECORDING/recording.py" start
      - name: Install the pinned tools
        if: contains(matrix.shard.setups, 'actionlint') || contains(matrix.shard.setups, 'node')
        run: python3 "$RECORDING/recording.py" exec --setup tools -- true
      - name: Run the entry files
        run: |
          # two at a time
          jq -c '.entries[]' <<<"$SHARD" | xargs -P 2 -n 1 python3 "$RECORDING/recording.py" exec --entry
      - name: Check the recording
        id: check
        if: ${{ !cancelled() && steps.start.outcome == 'success' }}
        uses: $/actions/recording
        with:
          check: "true"
      - name: Run the Python language step
        if: ${{ !cancelled() && fromJSON(needs.select.outputs.language-steps).python == 'run' }}
        uses: $/actions/python-language-step
      - name: List the entry files that passed
        if: ${{ !cancelled() && steps.check.outcome == 'success' }}
        run: echo passed > "$RUNNER_TEMP/passed.json"

  windows:
    needs: select
    if: needs.select.outputs.windows != ''
    runs-on: windows-latest
    permissions:
      contents: read
    env:
      SETUPS: ${{ toJSON(fromJSON(needs.select.outputs.windows).setups) }}
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000 # v1.0.0
      - if: contains(fromJSON(env.SETUPS), 'dotnet')
        uses: actions/setup-dotnet@0000000000000000000000000000000000000000 # v1.0.0
        with:
          dotnet-version: "10.0"
      - id: recording
        uses: $/actions/recording
      - name: Run the entry files
        shell: pwsh
        run: python "$env:RECORDING\\recording.py" exec --entry checks/test_win.py
      - name: Check the recording
        if: ${{ !cancelled() }}
        uses: $/actions/recording
        with:
          check: "true"

  validation:
    needs: [select, linux, windows]
    if: always()
    permissions:
      contents: read
    uses: $/.github/workflows/suite-verdict.yml
    with:
      needs: ${{ toJSON(needs) }}
"""

RECORDED_SUITES = {
    "always": ["config/everything.txt"],
    "suites": [
        {"name": "lib", "entries": ["checks/test_a.py", "checks/test_b.py"], "command": ["python3", "-m", "unittest", "-v"],
         "setups": ["python"], "entry_setups": {"checks/test_b.py": ["lua"]}, "entry_durations": {"checks/test_a.py": 1.0, "checks/test_b.py": 1.0},
         "entry_selection": True, "paths": ["checks/test_a.py", "checks/test_b.py", "src/a.py", "src/b.py"],
         "entry_paths": {"checks/test_a.py": ["checks/test_a.py", "src/a.py"], "checks/test_b.py": ["checks/test_b.py", "src/b.py"]},
         "entry_extra_paths": {"checks/test_a.py": ["data/a.txt"]}},
        {"name": "docs", "entries": ["checks/test_docs.py"], "command": ["node"], "setups": ["node"], "paths": ["checks/test_docs.py", "docs/**"]},
        {"name": "selection", "entries": ["checks/test_selection.py"], "command": ["python3", "-m", "unittest", "-v"], "setups": ["python"],
         "paths": ["checks/test_selection.py", ".github/workflows/suite-selection.yml", "actions/suite-selection/report.py",
                   "actions/suite-selection/reuse.py"]},
        {"name": "win", "os": "windows", "entries": ["checks/test_win.py"], "command": ["python", "-m", "unittest", "-v"],
         "setups": ["python", "dotnet"], "paths": ["checks/test_win.py"]},
    ],
}

LINUX_UNITS = {"checks/test_a.py", "checks/test_b.py", "checks/test_docs.py", "checks/test_selection.py"}


class UnrecordedFixture(unittest.TestCase):
    """A fixture repository laid out as this one: the shards and the Windows job in the calling workflow, with the
    recording step and the suite selection in the tree."""

    WORKFLOW = ".github/workflows/validate.yml"
    SUITES = "checks/suites.json"

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        self.layout()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "core.autocrlf", "false")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        self.base = git(self.root, "rev-parse", "HEAD")

    def layout(self) -> None:
        write(self.root, self.WORKFLOW, RECORDED_VALIDATE)
        write(self.root, self.SUITES, json.dumps(RECORDED_SUITES, indent=2) + "\n")
        write(self.root, ".github/workflows/suite-selection.yml", "on:\n  workflow_call:\njobs:\n  select:\n    runs-on: ubuntu-latest\n"
              "    steps:\n      - run: python3 actions/suite-selection/suite_selection.py select --root .\n")
        write(self.root, ".github/workflows/suite-verdict.yml", "on:\n  workflow_call:\njobs:\n  validation:\n    runs-on: ubuntu-latest\n"
              "    steps:\n      - run: python3 actions/suite-selection/suite_selection.py verdict\n")
        write(self.root, "actions/recording/action.yml", "runs:\n  using: composite\n  steps:\n    - shell: bash\n"
              "      env:\n        RESOURCE_PATH: ${{ github.action_path }}\n      run: python3 \"$RESOURCE_PATH/recording.py\" check\n")
        write(self.root, "actions/recording/recording.py", "import sys\n\nsys.path.insert(0, 'suite-selection')\n\nimport suite_selection\n\n\n"
              "def apply():\n    import apply_rules\n")
        write(self.root, "actions/suite-selection/apply_rules.py", "import json\n")
        write(self.root, "actions/recording/etw-consumer/Program.cs", "class Program {}\n")
        write(self.root, "actions/recording/etw_consumer_build.py", "print('build')\n")
        write(self.root, "actions/suite-selection/suite_selection.py", "import ci_definition\n\n\ndef reuse_units():\n    import reuse\n")
        write(self.root, "actions/suite-selection/ci_definition.py", "import json\n")
        write(self.root, "actions/suite-selection/reuse.py", "import json\n")
        write(self.root, "actions/suite-selection/report.py", "import json\n")
        write(self.root, "actions/python-language-step/action.yml", "runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: ruff check\n")
        for entry in ("checks/test_a.py", "checks/test_b.py", "checks/test_c.py", "checks/test_docs.py", "checks/test_selection.py",
                      "checks/test_win.py", "src/a.py", "src/b.py", "data/a.txt", "docs/guide.md", "README.md"):
            write(self.root, entry, entry + "\n")

    def head_after(self, files: dict[str, str]) -> str:
        git(self.root, "checkout", "-q", "--detach", self.base)
        for relative, text in files.items():
            write(self.root, relative, text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "change")
        return git(self.root, "rev-parse", "HEAD")

    def workflow_after(self, old: str, new: str, path: str | None = None) -> str:
        path = path or self.WORKFLOW
        text = (self.root / path).read_text(encoding="utf-8") if path != self.WORKFLOW else RECORDED_VALIDATE
        self.assertIn(old, text)
        return self.head_after({path: text.replace(old, new, 1)})

    def suites_after(self, change, stored: dict | None = None, base: str | None = None, indent: int | None = 2) -> str:
        suites = json.loads(json.dumps(stored or RECORDED_SUITES))
        change({suite["name"]: suite for suite in suites["suites"]}, suites)
        return self.commit_on(base or self.base, {self.SUITES: json.dumps(suites, indent=indent) + "\n"})

    def commit_on(self, parent: str, files: dict[str, str]) -> str:
        git(self.root, "checkout", "-q", "--detach", parent)
        for relative, text in files.items():
            write(self.root, relative, text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "change")
        return git(self.root, "rev-parse", "HEAD")

    def running(self, head: str, base: str | None = None) -> tuple[set[str], dict[str, str], str]:
        """The entry files the selection runs from ``base`` (the fixture's first commit) to ``head``, the language steps it
        starts, and its report."""
        output = self.root.parent / "outputs.txt"
        output.unlink(missing_ok=True)
        printed = io.StringIO()
        with redirect_stdout(printed):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / self.SUITES), "--base", base or self.base,
                                         "--head", head, "--compare", "direct", "--workflow", self.WORKFLOW, "--language-step", "python",
                                         "--json-output", str(output)])
        self.assertEqual(code, 0, printed.getvalue())
        values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
        entries = {item["entry"] for shard in json.loads(values["shards"]) for item in shard["entries"]}
        if values["windows"]:
            entries |= {item["entry"] for item in json.loads(values["windows"])["entries"]}
        return entries, json.loads(values["language-steps"]), printed.getvalue()

    def assertRuns(self, head: str, expected: set[str], language: str | None = None, base: str | None = None) -> str:
        entries, steps, printed = self.running(head, base)
        self.assertEqual(entries, expected, printed)
        if language is not None:
            self.assertEqual(steps["python"], language, printed)
        return printed



class UnrecordedInputTests(UnrecordedFixture):
    """What a change to an input that no recording sees selects: the suite list's own fields, the parts of the
    workflow that holds the suite jobs, and the recording step's files."""

    # --- the suite list ------------------------------------------------------------------------------------

    def test_a_changed_command_setups_operating_system_or_selection_runs_every_entry_file_of_the_suite(self) -> None:
        changes = {
            "command": lambda suites, _: suites["lib"].update(command=["python3", "-m", "unittest"]),
            "setups": lambda suites, _: suites["lib"].update(setups=["python", "ruff"]),
            "selection per entry file": lambda suites, _: [suites["lib"].pop(key) for key in ("entry_selection", "entry_paths", "entry_setups", "entry_extra_paths")],
        }
        for label, change in changes.items():
            with self.subTest(change=label):
                printed = self.assertRuns(self.suites_after(change), {"checks/test_a.py", "checks/test_b.py"})
                self.assertIn("lib: run (its command, setups, operating system or selection per entry file in `checks/suites.json` changed", printed)
        self.assertRuns(self.suites_after(lambda suites, _: suites["docs"].update(os="windows")), {"checks/test_docs.py"})

    def test_a_changed_entry_list_or_entry_setups_runs_only_the_entry_files_it_names(self) -> None:
        def add_entry(suites, _):
            suites["lib"]["entries"].append("checks/test_c.py")
            suites["lib"]["entry_paths"]["checks/test_c.py"] = ["checks/test_c.py"]
        printed = self.assertRuns(self.suites_after(add_entry), {"checks/test_c.py"})
        self.assertIn("`checks/test_c.py` is new in `checks/suites.json`", printed)
        self.assertRuns(self.suites_after(lambda suites, _: suites["lib"]["entry_setups"].update({"checks/test_b.py": ["lua", "node"]})),
                        {"checks/test_b.py"})

    def test_a_removed_read_or_extra_read_runs_the_entry_file_it_is_removed_from(self) -> None:
        printed = self.assertRuns(self.suites_after(lambda suites, _: suites["lib"]["entry_paths"]["checks/test_a.py"].remove("src/a.py")),
                                  {"checks/test_a.py"})
        self.assertIn("`checks/suites.json` removes reads from the lists of `checks/test_a.py`", printed)
        self.assertRuns(self.suites_after(lambda suites, _: suites["lib"].pop("entry_extra_paths")), {"checks/test_a.py"})
        self.assertRuns(self.suites_after(lambda suites, _: suites["docs"]["paths"].remove("docs/**")), {"checks/test_docs.py"})

    def test_a_removed_lookup_directory_or_module_lookup_runs_the_entry_files_that_looked_up_there(self) -> None:
        def with_lookups(suites, document):
            suites["lib"]["lookup_directories"] = ["checks", "src"]
            suites["lib"]["entry_lookup_directories"] = {"checks/test_a.py": ["checks"], "checks/test_b.py": ["src"]}
            document["module_lookups"] = {"checks": ["helper"], "src": ["other"]}
        stored = json.loads(json.dumps(RECORDED_SUITES))
        with_lookups({suite["name"]: suite for suite in stored["suites"]}, stored)
        base = self.commit_on(self.base, {self.SUITES: json.dumps(stored, indent=2) + "\n"})

        def removing(change) -> str:
            suites = json.loads(json.dumps(stored))
            change({suite["name"]: suite for suite in suites["suites"]}, suites)
            return self.commit_on(base, {self.SUITES: json.dumps(suites, indent=2) + "\n"})

        printed = self.assertRuns(removing(lambda suites, document: document["module_lookups"].pop("checks")), {"checks/test_a.py"}, base=base)
        self.assertIn("`checks/suites.json` removes reads from the lists of `checks/test_a.py`", printed)
        self.assertRuns(removing(lambda suites, _: suites["lib"]["entry_lookup_directories"].pop("checks/test_b.py")), {"checks/test_b.py"}, base=base)
        self.assertRuns(removing(lambda suites, document: document["module_lookups"]["src"].append("added")), set(), base=base)

    def test_estimates_additions_and_the_every_suite_patterns_select_nothing_by_themselves(self) -> None:
        changes = {
            "estimate": lambda suites, _: suites["lib"]["entry_durations"].update({"checks/test_a.py": 9.0}),
            "addition to a path list": lambda suites, _: suites["lib"]["entry_paths"]["checks/test_a.py"].append("src/new.py"),
            "addition to the extra reads": lambda suites, _: suites["docs"].update(extra_paths=["guides/**"]),
            "every-suite patterns": lambda suites, document: document.update(always=["config/other.txt"]),
        }
        for label, change in changes.items():
            with self.subTest(change=label):
                printed = self.assertRuns(self.suites_after(change), set())
                self.assertIn("Read by no suite: checks/suites.json", printed)

    def read_suite_list(self) -> tuple[dict, str]:
        """A suite list whose stored lists read the suite list itself, as this repository's do, committed on the
        fixture's first commit."""
        stored = json.loads(json.dumps(RECORDED_SUITES))
        suites = {suite["name"]: suite for suite in stored["suites"]}
        suites["lib"]["paths"].append(self.SUITES)
        suites["lib"]["entry_paths"]["checks/test_a.py"].append(self.SUITES)
        suites["docs"]["paths"].append(self.SUITES)
        return stored, self.commit_on(self.base, {self.SUITES: json.dumps(stored, indent=2) + "\n"})

    def test_a_change_of_estimates_alone_selects_no_entry_file_that_reads_the_suite_list(self) -> None:
        stored, base = self.read_suite_list()
        changes = {
            "one estimate": lambda suites, _: suites["lib"]["entry_durations"].update({"checks/test_a.py": 1.5}),
            "every estimate": lambda suites, _: suites["lib"].update(entry_durations={"checks/test_a.py": 3.0, "checks/test_b.py": 0.4}),
            "an added entry file key": lambda suites, _: suites["docs"].update(entry_durations={"checks/test_docs.py": 2.0}),
            "a removed entry file key": lambda suites, _: suites["lib"]["entry_durations"].pop("checks/test_b.py"),
            "every key removed": lambda suites, _: suites["lib"].pop("entry_durations"),
        }
        for label, change in changes.items():
            with self.subTest(change=label):
                printed = self.assertRuns(self.suites_after(change, stored, base), set(), base=base)
                self.assertNotIn(f"matches `{self.SUITES}`", printed)
                self.assertIn(f"Read by no suite: {self.SUITES}", printed)
        reformatted = self.suites_after(lambda suites, _: suites["lib"]["entry_durations"].update({"checks/test_a.py": 7.0}), stored, base,
                                       indent=None)
        self.assertRuns(reformatted, set(), base=base)

    def test_a_change_of_estimates_with_another_field_selects_the_entry_files_that_read_the_suite_list(self) -> None:
        stored, base = self.read_suite_list()
        readers = {"checks/test_a.py", "checks/test_docs.py"}

        def estimate(suites) -> None:
            suites["lib"]["entry_durations"]["checks/test_a.py"] = 1.5

        changes = {
            "an addition to a path list": (lambda suites, _: suites["lib"]["entry_paths"]["checks/test_b.py"].append("src/new.py"), readers),
            "the every-suite patterns": (lambda suites, document: document.update(always=["config/other.txt"]), readers),
            "a command": (lambda suites, _: suites["win"].update(command=["python", "-m", "pytest"]), readers | {"checks/test_win.py"}),
        }
        for label, (change, expected) in changes.items():
            with self.subTest(change=label, estimate=False):
                printed = self.assertRuns(self.suites_after(change, stored, base), expected, base=base)
                self.assertIn(f"docs: run (`{self.SUITES}` matches `{self.SUITES}`", printed)
                self.assertIn(f"`{self.SUITES}` matches `{self.SUITES}` of `checks/test_a.py`", printed)
            with self.subTest(change=label, estimate=True):
                head = self.suites_after(lambda suites, document: (change(suites, document), estimate(suites)), stored, base)
                printed = self.assertRuns(head, expected, base=base)
                self.assertIn(f"docs: run (`{self.SUITES}` matches `{self.SUITES}`", printed)

    def test_a_suite_list_that_is_absent_or_unreadable_at_the_base_counts_as_changed(self) -> None:
        stored, _ = self.read_suite_list()
        for label, before in {"absent": None, "not valid JSON": "{\n", "not a suite list": "{\"suites\": []}\n"}.items():
            with self.subTest(base=label):
                git(self.root, "checkout", "-q", "--detach", self.base)
                if before is None:
                    git(self.root, "rm", "-q", self.SUITES)
                    git(self.root, "commit", "-q", "-m", "remove")
                    base = git(self.root, "rev-parse", "HEAD")
                else:
                    base = self.commit_on(self.base, {self.SUITES: before})
                head = self.commit_on(base, {self.SUITES: json.dumps(stored, indent=2) + "\n"})
                entries, _, printed = self.running(head, base)
                self.assertLessEqual({"checks/test_a.py", "checks/test_docs.py"}, entries, printed)
                self.assertIn(f"docs: run (`{self.SUITES}` matches `{self.SUITES}`", printed)

    def test_two_suite_list_texts_differ_only_in_estimates_when_both_parse_and_differ_in_nothing_else(self) -> None:
        before = json.dumps(RECORDED_SUITES, indent=2) + "\n"
        estimate = json.loads(before)
        estimate["suites"][0]["entry_durations"]["checks/test_a.py"] = 2.0
        after = json.dumps(estimate) + "\n"
        self.assertTrue(suite_selection.differs_in_estimates_only(before, after))
        for label, other in {"indentation": json.dumps(RECORDED_SUITES) + "\n", "line endings": before.replace("\n", "\r\n")}.items():
            with self.subTest(formatting=label):
                self.assertTrue(suite_selection.differs_in_estimates_only(before, other))
        for label, other in {"absent": None, "not valid JSON": "{", "the same text": before}.items():
            with self.subTest(after=label):
                self.assertFalse(suite_selection.differs_in_estimates_only(before, other))
                self.assertFalse(suite_selection.differs_in_estimates_only(other, before))
        estimate["suites"][1]["setups"] = ["node", "lua"]
        self.assertFalse(suite_selection.differs_in_estimates_only(before, json.dumps(estimate)))

    def test_a_change_of_estimates_alone_in_the_working_tree_selects_no_entry_file(self) -> None:
        stored, base = self.read_suite_list()
        git(self.root, "checkout", "-q", "--detach", base)

        def selected() -> str:
            printed = io.StringIO()
            with redirect_stdout(printed):
                code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / self.SUITES),
                                             "--changed-file", self.SUITES])
            self.assertEqual(code, 0, printed.getvalue())
            return printed.getvalue()

        changed = json.loads(json.dumps(stored))
        changed["suites"][0]["entry_durations"]["checks/test_a.py"] = 4.0
        write(self.root, self.SUITES, json.dumps(changed) + "\n")
        printed = selected()
        self.assertNotIn(f"matches `{self.SUITES}`", printed)
        self.assertIn("docs: skip", printed)
        changed["suites"][1]["command"] = ["node", "--test"]
        write(self.root, self.SUITES, json.dumps(changed) + "\n")
        self.assertIn(f"docs: run (`{self.SUITES}` matches `{self.SUITES}`", selected())
        # A checkout that already holds the change, its line endings converted: the comparison sees no difference.
        committed = (json.dumps(changed, indent=2) + "\n").encode()
        (self.root / self.SUITES).write_bytes(committed)
        git(self.root, "commit", "-q", "-am", "the change")
        (self.root / self.SUITES).write_bytes(committed.replace(b"\n", b"\r\n"))
        self.assertIn(f"docs: run (`{self.SUITES}` matches `{self.SUITES}`", selected())

    # --- the workflow ---------------------------------------------------------------------------------------

    def test_a_changed_step_whose_condition_names_setups_runs_the_entry_files_that_state_them_in_both_jobs(self) -> None:
        python = self.workflow_after('python-version: "3.13"', 'python-version: "3.14"')
        printed = self.assertRuns(python, {"checks/test_a.py", "checks/test_b.py", "checks/test_selection.py"})
        self.assertIn("lib: run (its step in `.github/workflows/validate.yml` changed", printed)
        self.assertRuns(self.workflow_after("lua5.4", "lua5.4 luarocks"), {"checks/test_b.py"})
        self.assertRuns(self.workflow_after("exec --setup tools -- true", "exec --setup tools -- true --quiet"), {"checks/test_docs.py"})
        self.assertRuns(self.workflow_after('dotnet-version: "10.0"', 'dotnet-version: "10.0.1"'), {"checks/test_win.py"})

    def test_a_changed_step_whose_condition_names_the_python_language_step_starts_it_and_no_unit(self) -> None:
        head = self.workflow_after("uses: $/actions/python-language-step\n",
                                   "uses: $/actions/python-language-step\n        with:\n          strict: \"true\"\n")
        self.assertRuns(head, set(), language="run")
        changed_action = self.head_after({"actions/python-language-step/action.yml":
                                          "runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: ruff check --fix\n"})
        self.assertRuns(changed_action, set(), language="run")

    def test_a_changed_part_of_a_suite_job_runs_every_entry_file_of_that_job(self) -> None:
        changes = {
            "runner": ("runs-on: ubuntu-latest", "runs-on: ubuntu-24.04"),
            "checkout": ("fetch-depth: 0", "fetch-depth: 1"),
            "job condition": ("if: needs.select.outputs.shards != '[]'", "if: needs.select.outputs.shards != '[]' && github.actor != 'bot'"),
            "time limit": ("    strategy:\n      matrix:\n        shard:", "    timeout-minutes: 20\n    strategy:\n      matrix:\n        shard:"),
            "entry-file step": ("xargs -P 2", "xargs -P 4"),
            "start": ('recording.py" start', 'recording.py" start --verbose'),
            "check step": ("        with:\n          check: \"true\"\n      - name: Run the Python", "        with:\n          check: \"true\"\n          suites: x\n      - name: Run the Python"),
        }
        for label, (old, new) in changes.items():
            with self.subTest(change=label):
                printed = self.assertRuns(self.workflow_after(old, new), LINUX_UNITS)
                self.assertNotIn("win: run", printed)
        self.assertRuns(self.workflow_after("runs-on: windows-latest", "runs-on: windows-2025"), {"checks/test_win.py"})
        self.assertRuns(self.workflow_after("SETUPS: ${{", "LEVEL: one\n      SETUPS: ${{"), {"checks/test_win.py"})

    def test_a_changed_permissions_env_or_defaults_of_the_workflow_runs_every_entry_file_of_its_suite_jobs(self) -> None:
        everything = LINUX_UNITS | {"checks/test_win.py"}
        self.assertRuns(self.workflow_after("permissions: {}\n", "permissions:\n  contents: read\n"), everything)
        self.assertRuns(self.workflow_after("permissions: {}\n", "permissions: {}\n\nenv:\n  LEVEL: one\n"), everything)
        self.assertRuns(self.workflow_after("permissions: {}\n", "permissions: {}\n\ndefaults:\n  run:\n    shell: bash\n"), everything)

    def test_a_changed_recording_step_or_a_module_it_loads_runs_every_entry_file_of_the_jobs_that_use_it(self) -> None:
        everything = LINUX_UNITS | {"checks/test_win.py"}
        for path in ("actions/recording/recording.py", "actions/recording/action.yml",
                     "actions/suite-selection/suite_selection.py", "actions/suite-selection/ci_definition.py",
                     "actions/suite-selection/apply_rules.py"):
            with self.subTest(file=path):
                text = (self.root / path).read_text(encoding="utf-8")
                printed = self.assertRuns(self.head_after({path: text + "# changed\n"}), everything)
                self.assertIn(f"the recording step's file `{path}` changed", printed)
        for path in ("actions/recording/etw-consumer/Program.cs", "actions/recording/etw_consumer_build.py"):
            with self.subTest(file=path):
                self.assertRuns(self.head_after({path: "changed\n"}), {"checks/test_win.py"})

    def test_on_concurrency_names_and_comments_select_nothing(self) -> None:
        changes = {
            "on": ("on:\n  pull_request:\n", "on:\n  pull_request:\n  push:\n    branches: [main]\n"),
            "concurrency": ("cancel-in-progress: true", "cancel-in-progress: false"),
            "workflow name": ("name: Validate\n", "name: Validation\n"),
            "job name": ("name: shard ${{ matrix.shard.shard }}", "name: linux ${{ matrix.shard.shard }}"),
            "step name": ("name: Install Lua", "name: Install the Lua interpreter"),
            "comment in a script": ("# two at a time", "# two entry files at a time"),
            "comment in the workflow": ("  linux:\n", "  # The Linux shards.\n  linux:\n"),
        }
        for label, (old, new) in changes.items():
            with self.subTest(change=label):
                self.assertRuns(self.workflow_after(old, new), set())

    def test_the_selection_and_verdict_machinery_selects_only_the_entry_files_that_read_it(self) -> None:
        changes = {
            "a module the recording step does not load": {"actions/suite-selection/report.py": "import os\n"},
            "a module the suite selection imports inside a function the recording step does not call": {"actions/suite-selection/reuse.py": "import os\n"},
            "the suite-selection workflow": {".github/workflows/suite-selection.yml": (self.root / ".github/workflows/suite-selection.yml").read_text(encoding="utf-8") + "# changed\n"},
            "the select job": {self.WORKFLOW: RECORDED_VALIDATE.replace("language-steps: python", "language-steps: python lua")},
            "the validation job": {self.WORKFLOW: RECORDED_VALIDATE.replace("needs: [select, linux, windows]", "needs: [select, windows, linux]")},
            "a step that stores results": {self.WORKFLOW: RECORDED_VALIDATE.replace("echo passed >", "echo passed >>")},
        }
        readers = {"a module the recording step does not load": {"checks/test_selection.py"}, "the suite-selection workflow": {"checks/test_selection.py"},
                   "a module the suite selection imports inside a function the recording step does not call": {"checks/test_selection.py"}}
        for label, files in changes.items():
            with self.subTest(change=label):
                self.assertRuns(self.head_after(files), readers.get(label, set()))

    def test_a_list_of_changed_files_compares_the_working_tree_with_head(self) -> None:
        git(self.root, "checkout", "-q", "main")
        write(self.root, self.WORKFLOW, RECORDED_VALIDATE.replace("lua5.4", "lua5.4 luarocks"))
        printed = io.StringIO()
        with redirect_stdout(printed):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / self.SUITES),
                                         "--changed-file", self.WORKFLOW, "--workflow", self.WORKFLOW])
        self.assertEqual(code, 0)
        self.assertIn("lib: run (its step in `.github/workflows/validate.yml` changed; entries: `checks/test_b.py`)", printed.getvalue())

    # --- the reuse key --------------------------------------------------------------------------------------

    def reusing(self, head: str, record: Path) -> tuple[dict, dict]:
        """The running units and the reused units of a pull-request selection of ``head`` with the record."""
        git(self.root, "checkout", "-q", head)
        output = self.root.parent / "outputs.txt"
        output.unlink(missing_ok=True)
        with redirect_stdout(io.StringIO()):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / self.SUITES), "--base", self.base,
                                         "--head", head, "--compare", "merge-base", "--workflow", self.WORKFLOW, "--results", str(record),
                                         "--runner-label", "linux=ubuntu-latest", "--runner-label", "windows=windows-latest",
                                         "--json-output", str(output)])
        self.assertEqual(code, 0)
        values = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
        return json.loads(values["units"]), json.loads(values["reused"])

    def store_all(self, units: dict, record: Path, run: int) -> None:
        needs = self.root.parent / "needs.json"
        needs.write_text(json.dumps({"select": {"result": "success", "outputs": {"units": json.dumps(units), "reused": "{}"}}}), encoding="utf-8")
        passed = self.root.parent / f"passed-{run}"
        passed.mkdir()
        (passed / "passed.json").write_text(json.dumps([{"suite": unit["suite"], "entry": entry} for unit in units.values()
                                                        for entry in unit["entries"]]), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            self.assertEqual(suite_selection.main(["store", "--needs", str(needs), "--passed", str(passed), "--run-id", str(run),
                                                   "--output", str(record)]), 0)

    def test_the_reuse_key_holds_each_unrecorded_input_so_a_later_head_that_changes_one_reuses_no_earlier_pass(self) -> None:
        recording = (self.root / "actions/recording/recording.py").read_text(encoding="utf-8")
        inputs = {
            "the recording step": lambda version: {"actions/recording/recording.py": recording + f"# {version}\n"},
            "a module the recording step loads": lambda version: {"actions/suite-selection/ci_definition.py": f"import json  # {version}\n"},
            "a step with a setup condition": lambda version: {self.WORKFLOW: RECORDED_VALIDATE.replace('"3.13"', f'"3.13.{version}"')},
            "a part of a suite job": lambda version: {self.WORKFLOW: RECORDED_VALIDATE.replace("fetch-depth: 0", f"fetch-depth: {version}")},
            "the workflow's environment": lambda version: {self.WORKFLOW: RECORDED_VALIDATE.replace("permissions: {}\n", f"permissions: {{}}\n\nenv:\n  LEVEL: {version}\n", 1)},
            "the setups of an entry file": lambda version: {self.SUITES: json.dumps(dict(RECORDED_SUITES, suites=[
                dict(suite, entry_setups={"checks/test_b.py": ["lua", f"tool{version}"]}) if suite["name"] == "lib" else suite
                for suite in RECORDED_SUITES["suites"]]), indent=2) + "\n"},
        }
        for number, (label, change) in enumerate(inputs.items(), start=1):
            with self.subTest(input=label):
                record = self.root.parent / f"record-{number}.json"
                first = self.head_after(change(1))
                units, reused = self.reusing(first, record)
                self.assertTrue(units)
                self.assertEqual(reused, {})
                self.store_all(units, record, 100 + number)
                unchanged = self.commit_on(first, {"README.md": f"unread {number}\n"})
                again, reused = self.reusing(unchanged, record)
                self.assertEqual((again, sorted(reused)), ({}, sorted(units)), "a head that changes no input reuses every pass")
                changed = self.commit_on(unchanged, change(2))
                running, reused = self.reusing(changed, record)
                self.assertEqual(reused, {}, f"a later change to {label} reuses no earlier pass")
                self.assertEqual(sorted(entry for unit in running.values() for entry in unit["entries"]),
                                 sorted(entry for unit in units.values() for entry in unit["entries"]))


MODULE_PROBE = """\
import json
import runpy
import sys
from pathlib import Path

script, report, *arguments = sys.argv[1:]
sys.argv = [script, *arguments]
code = 0
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as stopped:
    code = stopped.code if isinstance(stopped.code, int) else (0 if stopped.code is None else 1)
shared = Path(script).resolve().parents[1]
files = {Path(module.__file__).resolve() for module in list(sys.modules.values()) if getattr(module, "__file__", None)}
Path(report).write_text(json.dumps(sorted(path.as_posix() for path in files if path.is_relative_to(shared))), encoding="utf-8")
sys.exit(code)
"""


class RecordingModulesTests(unittest.TestCase):
    """The modules of the shared CI that the recording step's commands load stay inside the set that the selection rule
    and the shared digest name (``recording_modules.py``), so that a later import on a recording path cannot leave it."""

    def setUp(self) -> None:
        from tests.recording_fixtures import RECORDING, RecordingFixture, suite

        class ProbedRecording(RecordingFixture):
            def command(probed, *arguments: str) -> list[str]:
                return [sys.executable, "-c", MODULE_PROBE, str(RECORDING), str(probed.base / "loaded.json"), *arguments]

            def loaded(probed) -> set[str]:
                return set(json.loads((probed.base / "loaded.json").read_text(encoding="utf-8")))

        sys.path.insert(0, str(RECORDING.parent))
        self.addCleanup(sys.path.remove, str(RECORDING.parent))
        import recording

        self.allowed = {path.resolve().as_posix() for path in recording.loaded_modules(RECORDING.resolve())}
        self.fixture = ProbedRecording(self.addCleanup, {"tests/a.py": "print('a')\n"}, [suite("a", ["tests/a.py"], paths=["tests/a.py"])])

    def assertLoadsOnlyTheSet(self, ran) -> None:
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        loaded = self.fixture.loaded()
        self.assertIn(next(path for path in self.allowed if path.endswith("/suite_selection.py")), loaded)
        self.assertLessEqual(loaded, self.allowed, "a module outside the set that recording_modules.py names")

    def test_the_set_holds_the_modules_that_recording_py_loads_and_not_those_that_only_select_imports(self) -> None:
        names = {path.rsplit("/", 1)[-1] for path in self.allowed}
        self.assertLessEqual({"recording.py", "suite_selection.py", "ci_definition.py", "revisions.py", "recording_modules.py"}, names)
        self.assertFalse({"reuse.py", "unrecorded.py"} & names, "select, the lookup and the store import them inside their functions")

    def test_digest_loads_only_the_set(self) -> None:
        self.assertLoadsOnlyTheSet(self.fixture.run("digest"))

    @unittest.skipUnless(sys.platform.startswith("linux"), "the recording step records under strace on Linux")
    def test_start_exec_check_and_apply_load_only_the_set(self) -> None:
        self.assertLoadsOnlyTheSet(self.fixture.start())
        self.assertLoadsOnlyTheSet(self.fixture.exec_entry("tests/a.py"))
        self.assertLoadsOnlyTheSet(self.fixture.check())
        self.assertLoadsOnlyTheSet(self.fixture.apply())


CONSUMER_CALLER = """\
name: Validation

on:
  pull_request:

permissions: {}

concurrency:
  group: validation-${{ github.ref }}

jobs:
  select:
    permissions:
      contents: read
    uses: CrankyAnt/SharedWorkflows/.github/workflows/suite-selection.yml@v1
    with:
      suites: .github/suites.json
  tests:
    needs: select
    permissions:
      contents: read
    uses: $/.github/workflows/tests.yml
    with:
      suites: ${{ needs.select.outputs.suites }}
  validation:
    needs: [select, tests]
    if: always()
    uses: CrankyAnt/SharedWorkflows/.github/workflows/suite-verdict.yml@v1
    with:
      needs: ${{ toJSON(needs) }}
"""

CONSUMER_TESTS = """\
name: Tests

on:
  workflow_call:
    inputs:
      suites:
        type: string
        required: true

permissions: {}

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - run: echo build
  unit:
    needs: build
    if: fromJSON(inputs.suites).unit == 'run' || fromJSON(inputs.suites).lint == 'run'
    runs-on: ubuntu-latest
    steps:
      - id: recording
        uses: CrankyAnt/SharedWorkflows/actions/recording@v1
      - name: Run the entry files of unit
        if: fromJSON(inputs.suites).unit == 'run'
        run: python3 "$RECORDING/recording.py" exec --entry checks/test_unit.py --suite unit -- python3 checks/test_unit.py
      - name: Run the entry files of lint
        if: fromJSON(inputs.suites).lint == 'run'
        run: python3 "$RECORDING/recording.py" exec --entry checks/test_lint.py --suite lint -- python3 checks/test_lint.py
      - name: Check the recording
        if: ${{ !cancelled() }}
        uses: CrankyAnt/SharedWorkflows/actions/recording@v1
        with:
          check: "true"
  other:
    if: fromJSON(inputs.suites).other == 'run'
    runs-on: ubuntu-latest
    steps:
      - id: recording
        uses: CrankyAnt/SharedWorkflows/actions/recording@v1
      - name: Run the entry files of other
        run: python3 "$RECORDING/recording.py" exec --entry checks/test_other.py --suite other -- python3 checks/test_other.py
"""

CONSUMER_SUITES = {"always": [], "suites": [
    {"name": name, "entries": [f"checks/test_{name}.py"], "command": ["python3"], "setups": ["python"], "paths": [f"checks/test_{name}.py"]}
    for name in ("unit", "lint", "other")]}


class ConsumerUnrecordedInputTests(UnrecordedFixture):
    """The same rules in a consumer: the validation caller, a job above the suite jobs, and suite jobs gated on suites."""

    WORKFLOW = ".github/workflows/validation.yml"
    SUITES = ".github/suites.json"

    def layout(self) -> None:
        write(self.root, self.WORKFLOW, CONSUMER_CALLER)
        write(self.root, ".github/workflows/tests.yml", CONSUMER_TESTS)
        write(self.root, self.SUITES, json.dumps(CONSUMER_SUITES, indent=2) + "\n")
        for name in ("unit", "lint", "other"):
            write(self.root, f"checks/test_{name}.py", name + "\n")

    def running(self, head: str, base: str | None = None) -> tuple[set[str], dict[str, str], str]:
        entries, _, printed = super().running(head, base)
        return entries, {"python": "skip"}, printed

    def caller_after(self, old: str, new: str) -> str:
        self.assertIn(old, CONSUMER_CALLER)
        return self.head_after({self.WORKFLOW: CONSUMER_CALLER.replace(old, new, 1)})

    def called_after(self, old: str, new: str) -> str:
        self.assertIn(old, CONSUMER_TESTS)
        return self.head_after({".github/workflows/tests.yml": CONSUMER_TESTS.replace(old, new, 1)})

    EVERY = {"checks/test_unit.py", "checks/test_lint.py", "checks/test_other.py"}
    GATED = {"checks/test_unit.py", "checks/test_lint.py"}

    def test_a_changed_part_of_a_suite_job_runs_every_unit_its_gate_names(self) -> None:
        printed = self.assertRuns(self.called_after("    runs-on: ubuntu-latest\n    steps:\n      - id: recording",
                                                   "    runs-on: ubuntu-24.04\n    steps:\n      - id: recording"), self.GATED)
        self.assertIn("unit: run (the job `unit` of `.github/workflows/tests.yml` changed)", printed)
        self.assertRuns(self.called_after("if: ${{ !cancelled() }}", "if: ${{ always() }}"), self.GATED)
        self.assertRuns(self.called_after("--suite unit -- python3 checks/test_unit.py", "--suite unit -- python3 -X dev checks/test_unit.py"),
                        {"checks/test_unit.py"})

    def test_a_changed_job_above_a_suite_job_or_permissions_runs_every_unit_below_it(self) -> None:
        self.assertRuns(self.called_after("run: echo build", "run: echo build --release"), self.GATED)
        self.assertRuns(self.caller_after("suites: ${{ needs.select.outputs.suites }}",
                                          "suites: ${{ needs.select.outputs.suites }}\n      entries: ${{ needs.select.outputs.entries }}"), self.EVERY)
        self.assertRuns(self.caller_after("    permissions:\n      contents: read\n    uses: $/",
                                          "    permissions:\n      contents: read\n      actions: read\n    uses: $/"), self.EVERY)
        self.assertRuns(self.caller_after("permissions: {}\n", "permissions:\n  contents: read\n"), self.EVERY)
        self.assertRuns(self.called_after("permissions: {}\n", "permissions: {}\n\nenv:\n  LEVEL: one\n"), self.EVERY)

    def test_on_concurrency_and_the_select_and_validation_jobs_of_the_caller_select_nothing(self) -> None:
        self.assertRuns(self.caller_after("group: validation-${{ github.ref }}", "group: validation-${{ github.head_ref }}"), set())
        self.assertRuns(self.caller_after("on:\n  pull_request:\n", "on:\n  pull_request:\n  push:\n"), set())
        self.assertRuns(self.caller_after("suites: .github/suites.json", "suites: .github/suites.json\n      language-steps: python"), set())
        self.assertRuns(self.caller_after("    if: always()\n", "    if: ${{ always() }}\n"), set())
        self.assertRuns(self.called_after("required: true", "required: true\n        description: The selection."), set())

    def test_a_job_that_runs_select_in_a_step_or_through_a_local_workflow_is_no_job_above(self) -> None:
        local = CONSUMER_CALLER.replace("uses: CrankyAnt/SharedWorkflows/.github/workflows/suite-selection.yml@v1", "uses: $/.github/workflows/select.yml")
        layouts = {
            "a local workflow that runs select": ({self.WORKFLOW: local, ".github/workflows/select.yml": (
                "on:\n  workflow_call:\njobs:\n  select:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - run: python3 actions/suite-selection/suite_selection.py select --root .\n")},
                ("suites: .github/suites.json", "suites: .github/suites.json\n      language-steps: python")),
            "a step that runs select": ({self.WORKFLOW: CONSUMER_CALLER.replace(
                "    uses: CrankyAnt/SharedWorkflows/.github/workflows/suite-selection.yml@v1\n    with:\n      suites: .github/suites.json\n",
                "    runs-on: ubuntu-latest\n    steps:\n      - run: python3 \"$RESOURCE_PATH/suite_selection.py\" select --root .\n")},
                ("runs-on: ubuntu-latest", "runs-on: ubuntu-24.04")),
        }
        for label, (files, (old, new)) in layouts.items():
            with self.subTest(layout=label):
                laid_out = self.head_after(files)
                changed = self.commit_on(laid_out, {self.WORKFLOW: files[self.WORKFLOW].replace(old, new, 1)})
                self.assertRuns(changed, set(), base=laid_out)


if __name__ == "__main__":
    unittest.main()
