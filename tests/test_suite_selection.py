"""Suite selection and verdict commands on isolated repositories."""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from tests import temporary_support
from tests.suite_selection_fixtures import (
    DELTA_REASON, FIXTURE_SUITES, FixtureTemplateTests, fixture_repository, git, write,
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
            ({"suites": []}, "the keys always and suites, and optionally entry_durations_version, jobs, setups and module_lookups"),
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
            ({"entry_extra_paths": {"x.py": ["docs/**"]}}, "(a): entry_extra_paths needs entry_selection"),
            ({"entry_selection": True, "entry_paths": {"x.py": [], "checks/*.py": []}, "entry_setups": {"y.py": ["node"]}}, "names y.py, which the entries lack"),
            ({"entry_selection": True, "entry_paths": {"x.py": [], "checks/*.py": []}, "entry_setups": {"x.py": "node"}}, "entry_setups (x.py) must be a list"),
            ({"entry_durations": ["x.py", 3]}, "(a): entry_durations must be an object"),
            ({"entry_durations": {"x.py": -1}}, "entry_durations (x.py) must be a number of seconds, zero or more"),
            ({"entry_durations": {"x.py": "3"}}, "entry_durations (x.py) must be a number of seconds"),
            ({"entry_durations": {"x.py": True}}, "entry_durations (x.py) must be a number of seconds"),
            ({"os": "macos"}, "(a): os must be one of linux, windows"),
        ):
            with self.subTest(message=message), temporary_support.Directory() as directory:
                suites = Path(directory) / "suites.json"
                suites.write_text(json.dumps({"always": [], "suites": [{**base, **extra}]}), encoding="utf-8")
                error = io.StringIO()
                with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", error):
                    code = suite_selection.main(["select", "--root", directory, "--suites", str(suites), "--changed-file", "x.py"])
                self.assertEqual(code, 2)
                self.assertIn(message, error.getvalue())
        with temporary_support.Directory() as directory:
            suites = Path(directory) / "suites.json"
            suites.write_text(json.dumps({"always": [], "suites": [{**base, "entry_durations": {"x.py": float("nan")}}]}), encoding="utf-8")
            with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(suite_selection.main(["select", "--root", directory, "--suites", str(suites), "--changed-file", "x.py"]), 2)

    def test_select_rejects_the_language_python_runner_and_every_change_fields(self) -> None:
        for field, value in (("language", "python"), ("python_runner", "unittest"),
                             ("every_change", {"sites": [{"file": "x.py", "expression": "ROOT / name"}]})):
            with self.subTest(field=field), temporary_support.Directory() as directory:
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

    def glob_suite(self, **fields) -> dict:
        """A suite list whose one suite names its entry files through a pattern and stores a list per matched file."""
        return {"always": [], "suites": [{"name": "unit", "entries": ["tests/test_*.py"], "entry_selection": True, "paths": ["lib/a.py", "lib/b.py"],
                                         "entry_paths": {"tests/test_a.py": ["lib/a.py"], "tests/test_b.py": ["lib/b.py"]}, **fields}]}

    def test_a_glob_entry_takes_per_entry_fields_keyed_by_the_files_it_matches(self) -> None:
        text = json.dumps(self.glob_suite(), indent=2) + "\n"
        parsed = suite_selection.parse_suite_list(text)
        self.assertEqual(dict(parsed.suites[0].entry_paths), {"tests/test_a.py": ("lib/a.py",), "tests/test_b.py": ("lib/b.py",)})
        self.assertEqual(suite_selection.render_suite_list(parsed), text, "the pattern and the keys of the matched files render back the same")
        for field, value in (("entry_extra_paths", {"tests/test_b.py": ["docs/**"]}),
                             ("entry_directory_memberships", {"tests/test_b.py": ["lib"]}),
                             ("entry_existence_reads", {"tests/test_b.py": [{"path": "probe", "kind": "any"}]}),
                             ("entry_lookup_directories", {"tests/test_b.py": ["tests"]}),
                             ("entry_setups", {"tests/test_b.py": ["node"]}),
                             ("entry_durations", {"tests/test_b.py": 2.5})):
            with self.subTest(field=field):
                document = self.glob_suite(**{field: value})
                rendered = suite_selection.render_suite_list(suite_selection.parse_suite_list(json.dumps(document)))
                self.assertEqual(json.loads(rendered), document)

    def path_suite(self, **fields) -> dict:
        """The suite of ``glob_suite`` with each entry file named by path."""
        document = self.glob_suite(**fields)
        document["suites"][0]["entries"] = ["tests/test_a.py", "tests/test_b.py"]
        return document

    def namings(self) -> tuple:
        """The suite of ``glob_suite`` and of ``path_suite``, each with the name of how it names its entry files."""
        return (("glob", self.glob_suite), ("path", self.path_suite))

    def test_a_recorded_field_may_hold_the_key_of_a_file_that_no_entry_names_any_more(self) -> None:
        gone = "old/test_gone.py"
        for field, value in (("entry_paths", ["lib/gone.py"]), ("entry_directory_memberships", ["lib"]),
                             ("entry_existence_reads", [{"path": "probe", "kind": "any"}]), ("entry_lookup_directories", ["tests"]),
                             ("entry_durations", 2.5)):
            for naming, build in self.namings():
                with self.subTest(field=field, naming=naming):
                    document = build()
                    document["suites"][0][field] = {**document["suites"][0].get(field, {}), gone: value}
                    parsed = suite_selection.parse_suite_list(json.dumps(document))
                    self.assertEqual(json.loads(suite_selection.render_suite_list(parsed)), document, "the key renders back until apply removes it")
                    resolved = suite_selection.resolve_entries(parsed, {"tests/test_a.py", "tests/test_b.py"}).suites[0]
                    self.assertNotIn(gone, dict(getattr(resolved, field)), "nothing that selects reads the key")

    def test_a_field_that_the_repository_writes_rejects_a_key_that_is_no_entry_path_and_that_no_pattern_matches(self) -> None:
        for field in ("entry_extra_paths", "entry_setups"):
            for naming, build in self.namings():
                with self.subTest(field=field, naming=naming):
                    document = build(**{field: {"old/test_gone.py": ["x"]}})
                    with self.assertRaisesRegex(suite_selection.SuiteListError, rf"\(unit\): {field} names old/test_gone.py, which the entries lack"):
                        suite_selection.parse_suite_list(json.dumps(document))

    def whole_suite(self, build, **fields) -> dict:
        """The suite of ``build`` selected as a whole, without ``entry_selection`` and ``entry_paths``, with ``fields`` added."""
        document = build()
        for key in ("entry_selection", "entry_paths"):
            del document["suites"][0][key]
        document["suites"][0].update(fields)
        return document

    def test_a_field_that_the_repository_writes_needs_entry_selection_when_it_names_an_entry_file(self) -> None:
        for field in ("entry_extra_paths", "entry_setups"):
            for naming, build in self.namings():
                with self.subTest(field=field, naming=naming):
                    document = self.whole_suite(build, **{field: {"tests/test_b.py": ["x"]}})
                    with self.assertRaisesRegex(suite_selection.SuiteListError, rf"\(unit\): {field} needs entry_selection"):
                        suite_selection.parse_suite_list(json.dumps(document))

    def test_an_empty_field_that_the_repository_writes_parses_without_entry_selection_and_renders_as_absent(self) -> None:
        for field in ("entry_extra_paths", "entry_setups"):
            for naming, build in self.namings():
                with self.subTest(field=field, naming=naming):
                    rendered = suite_selection.render_suite_list(suite_selection.parse_suite_list(json.dumps(self.whole_suite(build, **{field: {}}))))
                    self.assertEqual(json.loads(rendered), self.whole_suite(build))

    def test_select_passes_on_a_stale_key_of_a_suite_with_only_path_entries_and_selects_nothing_on_it(self) -> None:
        document = self.path_suite(entry_durations={"old/test_gone.py": 3.0})
        document["suites"][0]["entry_paths"]["old/test_gone.py"] = ["gone/**"]
        with temporary_support.Directory() as directory:
            for path in ("suites.json", "tests/test_a.py", "tests/test_b.py"):
                (Path(directory) / path).parent.mkdir(parents=True, exist_ok=True)
                (Path(directory) / path).write_text("", encoding="utf-8")
            suites = Path(directory) / "suites.json"
            suites.write_text(json.dumps(document), encoding="utf-8")
            report = io.StringIO()
            with redirect_stdout(report), mock.patch("sys.stderr", io.StringIO()):
                code = suite_selection.main(["select", "--root", directory, "--suites", str(suites), "--changed-file", "gone/x.py"])
        self.assertEqual(code, 0)
        self.assertIn("unit: skip", report.getvalue())

    def test_an_entry_file_named_by_path_may_lack_its_stored_list(self) -> None:
        for naming, build in self.namings():
            with self.subTest(naming=naming):
                document = build()
                document["suites"][0]["entries"].append("checks/x.py")
                text = json.dumps(document, indent=2) + "\n"
                parsed = suite_selection.parse_suite_list(text)
                self.assertEqual(suite_selection.render_suite_list(parsed), text)
                unit = parsed.suites[0]
                self.assertTrue(unit.lacks_list("checks/x.py"))
                self.assertFalse(unit.lacks_list("tests/test_a.py"))


class SelectionTests(FixtureTemplateTests):
    """The selection command on a fixture Git repository with commits."""

    @classmethod
    def populate(cls, root: Path) -> None:
        write(root, "checks/suites.json", json.dumps(FIXTURE_SUITES, indent=2) + "\n")

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._suite_list = suite_selection.load_suite_list(cls._template / "checks" / "suites.json")

    def setUp(self) -> None:
        super().setUp()
        self.suite_list = self._suite_list

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


class StoredListSelectionTests(FixtureTemplateTests):
    """Selection from the stored lists alone, on a fixture Git repository."""

    SUITES = {
        "always": ["ci/**"],
        "suites": [
            {"name": "alpha", "entries": ["checks/test_app.py"]},
            {"name": "gamma", "entries": ["checks/*.ps1"], "extra_paths": ["guides/**"]},
            {"name": "stored", "entries": ["checks/beta.py"], "paths": ["src/old.txt"], "extra_paths": ["stray.txt"]},
        ],
    }

    @classmethod
    def populate(cls, root: Path) -> None:
        write(root, "checks/suites.json", json.dumps(cls.SUITES, indent=2) + "\n")

    def setUp(self) -> None:
        super().setUp()
        self.scratch = Path(self.temporary.name)
        self.suites = json.loads(json.dumps(self.SUITES))

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


class GlobEntrySelectionTests(FixtureTemplateTests):
    """The selection command on a suite that names its entry files through a pattern and stores a list per matched file."""

    SUITES = {"always": [], "suites": [
        {"name": "unit", "entries": ["tests/test_*.py"], "entry_selection": True, "paths": ["lib/a.py", "lib/b.py"],
         "entry_paths": {"tests/test_a.py": ["lib/a.py"], "tests/test_b.py": ["lib/b.py"]}}]}

    @classmethod
    def populate(cls, root: Path) -> None:
        write(root, "checks/suites.json", json.dumps(cls.SUITES, indent=2) + "\n")
        for path in ("tests/test_a.py", "tests/test_b.py", "lib/a.py", "lib/b.py"):
            write(root, path)

    def setUp(self) -> None:
        super().setUp()
        self.outputs = Path(self.temporary.name) / "outputs.txt"

    def commit(self, message: str) -> None:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", message)

    def select(self, changed: str) -> tuple[str, str]:
        """The line of the suite in the report and its selected entry files, for one changed file."""
        self.outputs.write_text("", encoding="utf-8")
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), mock.patch("sys.stderr", error):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "checks" / "suites.json"),
                                         "--changed-file", changed, "--github-output", str(self.outputs)])
        self.assertEqual(code, 0, error.getvalue())
        outputs = dict(line.split("=", 1) for line in self.outputs.read_text(encoding="utf-8").splitlines())
        return next(line for line in output.getvalue().splitlines() if line.startswith("unit: ")), outputs["unit-entries"]

    def test_a_change_that_reaches_the_stored_list_of_one_matched_file_runs_that_entry_file_alone(self) -> None:
        line, entries = self.select("lib/a.py")
        self.assertEqual(entries, "tests/test_a.py")
        self.assertIn("`lib/a.py` matches `lib/a.py` of `tests/test_a.py`", line)
        self.assertEqual(self.select("stray.txt"), ("unit: skip (no changed file matches the path list of any of its entry files)", ""))

    def test_a_matched_file_without_a_stored_list_runs_on_every_change_beside_the_entry_files_selected_on_their_lists(self) -> None:
        write(self.root, "tests/test_new.py")
        self.commit("a new test file that the pattern matches")
        line, entries = self.select("stray.txt")
        self.assertEqual(entries, "tests/test_new.py", "a change that no stored list reads runs the file without a list alone")
        self.assertIn("unit: run (`tests/test_new.py` runs on every change: no stored path list;", line)
        line, entries = self.select("lib/b.py")
        self.assertEqual(entries, "tests/test_b.py tests/test_new.py")
        self.assertIn("`lib/b.py` matches `lib/b.py` of `tests/test_b.py` and 1 more", line)

    def test_no_passed_result_of_a_matched_file_without_a_stored_list_is_reused(self) -> None:
        import reuse

        files = {"tests/test_a.py": "a", "tests/test_b.py": "b", "tests/test_new.py": "new", "lib/a.py": "a", "lib/b.py": "b"}
        suite_list = suite_selection.resolve_entries(suite_selection.parse_suite_list(json.dumps(self.SUITES)), set(files))
        selection = suite_selection.every_suite(suite_list, "test")
        self.assertEqual(selection.decisions[0].entries, ("tests/test_a.py", "tests/test_b.py", "tests/test_new.py"))
        units = suite_selection.reuse_units(selection, suite_list, reuse.Tree(files, suite_selection.matches), {}, {}, ignore_content={})
        self.assertEqual([(unit.entries, unit.unlisted) for unit in units],
                         [(("tests/test_a.py",), False), (("tests/test_b.py",), False), (("tests/test_new.py",), True)],
                         "the file without a list is emitted as unlisted so store --additions can key it after it passes")
        after, running, reused = suite_selection.apply_reuse(selection, units, {unit.key: 100 for unit in units})
        self.assertEqual(after.decisions[0].entries, ("tests/test_new.py",),
                         "an unlisted file is never looked up or reused even if its sentinel key were present in the record")
        self.assertEqual(len(reused), 2)
        self.assertEqual([unit["entries"] for unit in running.values()], [["tests/test_new.py"]])

    def test_the_estimate_of_a_file_that_no_entry_names_any_more_is_dropped_and_unmeasured_entries_stay_none(self) -> None:
        document = json.loads(json.dumps(self.SUITES))
        document["suites"][0]["entry_durations"] = {"tests/test_a.py": 2.0, "old/test_gone.py": 98.0}
        tracked = {"tests/test_a.py", "tests/test_b.py"}
        suite_list = suite_selection.resolve_entries(suite_selection.parse_suite_list(json.dumps(document)), tracked)
        units = suite_selection.selected_units(suite_selection.every_suite(suite_list, "test"), suite_list, "linux")
        self.assertEqual([(entry, estimate) for _, entry, estimate in units], [("tests/test_a.py", 2.0), ("tests/test_b.py", None)])

    def test_a_list_keyed_by_the_pattern_itself_is_the_list_of_no_matched_file(self) -> None:
        # Such a hand-written key loads; no recording writes it. Every matched file then has no list of its own.
        document = json.loads(json.dumps(self.SUITES))
        document["suites"][0]["entry_paths"] = {"tests/test_*.py": ["lib/a.py"]}
        suite_list = suite_selection.resolve_entries(suite_selection.parse_suite_list(json.dumps(document)), {"tests/test_a.py", "tests/test_b.py"})
        decision = suite_selection.select(suite_list, ["stray.txt"]).decisions[0]
        self.assertEqual(decision.entries, ("tests/test_a.py", "tests/test_b.py"))
        self.assertIn("`tests/test_a.py` runs on every change: no stored path list", decision.reason)

    def test_a_stored_key_of_a_deleted_file_is_ignored(self) -> None:
        (self.root / "tests" / "test_b.py").unlink()
        self.commit("delete a matched file")
        self.assertEqual(self.select("lib/b.py"), ("unit: skip (no changed file matches the path list of any of its entry files)", ""))
        self.assertEqual(self.select("lib/a.py")[1], "tests/test_a.py")


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


class EntrySelectionTests(unittest.TestCase):
    """A suite selected per entry file: each entry runs when a changed file matches its own list; the step runs
    the selected modules; a suite without the mark behaves as before."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.root = temporary_support.make_directory(cls.addClassCleanup, prefix="entry-selection-") / "repo"
        cls.root.mkdir()
        fixture_repository(cls.root)

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

    def test_an_entry_file_named_by_path_without_a_stored_list_runs_on_every_change_and_the_others_on_their_own_lists(self) -> None:
        document = json.loads(json.dumps(PAIR_SUITES))
        del document["suites"][0]["entry_paths"]["checks/beta.py"]
        suite_list = self.suite_list(document)
        pair = suite_selection.select(suite_list, ["stray.txt"], root=self.root).decisions[0]
        self.assertEqual((pair.run, pair.entries, pair.reason), (True, ("checks/beta.py",), "`checks/beta.py` runs on every change: no stored path list"))
        both = suite_selection.select(suite_list, ["guides/intro.md"], root=self.root).decisions[0]
        self.assertEqual((both.run, both.entries), (True, ("checks/test_app.py", "checks/beta.py")))
        self.assertIn("`guides/intro.md` matches `guides/**` of `checks/test_app.py`", both.reason)

    def test_the_suite_list_rejects_entry_lists_that_do_not_fit_the_entries(self) -> None:
        base = PAIR_SUITES["suites"][0]
        for change, message in (
            ({"entry_selection": False}, "entry_paths needs entry_selection"),
            ({"entry_extra_paths": {"checks/other.py": ["x"]}}, "names checks/other.py, which the entries lack"),
            ({"entry_setups": {"checks/other.py": ["x"]}}, "names checks/other.py, which the entries lack"),
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


class WholeTreeReasonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temporary_support.make_directory(self.addCleanup, prefix="whole-tree-reasons-")
        for path in ("checks/test_a.py", "checks/test_b.py", "src/app.py"):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("", encoding="utf-8")

    def write_suites(self, document: dict) -> Path:
        path = self.root / "suites.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def run_select(self, suites_path: Path, *extra: str) -> tuple[int, str]:
        stderr = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(suites_path), *extra])
        return code, stderr.getvalue()

    def test_invalid_whole_tree_reasons_fail_with_a_message_naming_the_problem(self) -> None:
        cases = (
            ({"name": "unit", "entries": ["checks/test_a.py"], "paths": ["checks/test_a.py"],
              "whole_tree_reasons": {"content": ""}}, "whole_tree_reasons \\(content\\): reason must be a non-empty string"),
            ({"name": "unit", "entries": ["checks/test_a.py"], "paths": ["checks/test_a.py"],
              "whole_tree_reasons": {"membership": "   "}}, "whole_tree_reasons \\(membership\\): reason must be a non-empty string"),
            ({"name": "unit", "entries": ["checks/test_a.py"], "paths": ["checks/test_a.py"],
              "whole_tree_reasons": {}}, "whole_tree_reasons must be a non-empty object"),
            ({"name": "unit", "entries": ["checks/test_a.py"], "paths": ["checks/test_a.py"],
              "whole_tree_reasons": {"other": "reason"}}, "unknown keys other"),
            ({"name": "unit", "entries": ["checks/test_a.py"], "entry_selection": True, "paths": ["checks/test_a.py"],
              "entry_paths": {"checks/test_a.py": ["checks/test_a.py"]},
              "whole_tree_reasons": {"content": "reason"}}, "whole_tree_reasons goes with a suite selected as a whole"),
            ({"name": "unit", "entries": ["checks/test_a.py"], "paths": ["checks/test_a.py"],
              "entry_whole_tree_reasons": {"checks/test_a.py": {"content": "reason"}}}, "entry_whole_tree_reasons needs entry_selection"),
            ({"name": "unit", "entries": ["checks/test_a.py"], "entry_selection": True, "paths": ["checks/test_a.py"],
              "entry_paths": {"checks/test_a.py": ["checks/test_a.py"]},
              "entry_whole_tree_reasons": {"checks/test_missing.py": {"content": "reason"}}}, "checks/test_missing.py"),
        )
        for item, pattern in cases:
            with self.subTest(item=item):
                with self.assertRaisesRegex(suite_selection.SuiteListError, pattern):
                    suite_selection.parse_suite_list(json.dumps({"always": [], "suites": [item]}))
        # Glob entry that matches a non-existent file is rejected by resolve_entries.
        glob_doc = {"always": [], "suites": [
            {"name": "unit", "entries": ["checks/test_*.py"], "entry_selection": True, "paths": ["checks/test_a.py"],
             "entry_paths": {"checks/test_a.py": ["checks/test_a.py"]},
             "entry_whole_tree_reasons": {"checks/test_ghost.py": {"content": "reason"}}}]}
        parsed = suite_selection.parse_suite_list(json.dumps(glob_doc))
        with self.assertRaisesRegex(suite_selection.SuiteListError, "checks/test_ghost.py"):
            suite_selection.resolve_entries(parsed, {"checks/test_a.py", "checks/test_b.py"})

    def test_pull_request_select_fails_on_stored_whole_tree_without_reason_and_push_or_full_still_works(self) -> None:
        suites_path = self.write_suites({"always": [], "suites": [
            {"name": "per", "entries": ["checks/test_a.py", "checks/test_b.py"], "entry_selection": True,
             "paths": ["**", "checks/test_a.py", "checks/test_b.py"],
             "entry_paths": {"checks/test_a.py": ["**", "checks/test_a.py"], "checks/test_b.py": ["checks/test_b.py"]},
             "directory_memberships": ["**"],
             "entry_directory_memberships": {"checks/test_b.py": ["**"]}},
            {"name": "whole", "entries": ["checks/test_a.py"], "paths": ["**", "checks/test_a.py"]}]})
        pr_code, pr_err = self.run_select(suites_path, "--changed-file", "src/app.py", "--event", "pull_request")
        self.assertEqual(2, pr_code)
        self.assertIn("per: checks/test_a.py (content)", pr_err)
        self.assertIn("per: checks/test_b.py (membership)", pr_err)
        self.assertIn("whole (content)", pr_err)
        # Push and full-recording keep working on the same suite list.
        push_code, push_err = self.run_select(suites_path, "--changed-file", "src/app.py", "--event", "push")
        self.assertEqual(0, push_code, push_err)
        full_code, full_err = self.run_select(suites_path, "--full")
        self.assertEqual(0, full_code, full_err)

    def test_pull_request_select_passes_when_matching_reasons_are_recorded_and_fails_on_wrong_kind_or_unit(self) -> None:
        suites_path = self.write_suites({"always": [], "suites": [
            {"name": "per", "entries": ["checks/test_a.py", "checks/test_b.py"], "entry_selection": True,
             "paths": ["**", "checks/test_a.py", "checks/test_b.py"],
             "entry_paths": {"checks/test_a.py": ["**", "checks/test_a.py"], "checks/test_b.py": ["checks/test_b.py"]},
             "entry_whole_tree_reasons": {"checks/test_a.py": {"content": "Audits every tracked file"}}},
            {"name": "whole", "entries": ["checks/test_a.py"], "directory_memberships": ["**"],
             "paths": ["checks/test_a.py"],
             "whole_tree_reasons": {"membership": "Checks every tracked path name"}}]})
        ok_code, ok_err = self.run_select(suites_path, "--changed-file", "src/app.py", "--event", "pull_request")
        self.assertEqual(0, ok_code, ok_err)
        # Wrong kind (membership reason for content **) still fails.
        wrong_kind = self.write_suites({"always": [], "suites": [
            {"name": "whole", "entries": ["checks/test_a.py"], "paths": ["**", "checks/test_a.py"],
             "whole_tree_reasons": {"membership": "Only explains membership"}}]})
        bad_kind_code, bad_kind_err = self.run_select(wrong_kind, "--changed-file", "src/app.py", "--event", "pull_request")
        self.assertEqual(2, bad_kind_code)
        self.assertIn("whole (content)", bad_kind_err)
        # Wrong unit (reason on test_b for test_a's **) still fails.
        wrong_unit = self.write_suites({"always": [], "suites": [
            {"name": "per", "entries": ["checks/test_a.py", "checks/test_b.py"], "entry_selection": True,
             "paths": ["**", "checks/test_a.py", "checks/test_b.py"],
             "entry_paths": {"checks/test_a.py": ["**", "checks/test_a.py"], "checks/test_b.py": ["checks/test_b.py"]},
             "entry_whole_tree_reasons": {"checks/test_b.py": {"content": "Only explains test_b"}}}]})
        bad_unit_code, bad_unit_err = self.run_select(wrong_unit, "--changed-file", "src/app.py", "--event", "pull_request")
        self.assertEqual(2, bad_unit_code)
        self.assertIn("per: checks/test_a.py (content)", bad_unit_err)


if __name__ == "__main__":
    unittest.main()
