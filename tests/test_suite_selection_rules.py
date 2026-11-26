"""Suite selection rules: new entry files, directory memberships, existence reads, module lookups, and ignores."""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests import temporary_support
from tests.suite_selection_fixtures import TemplateRepoTests, fixture_repository, force_add, git, write

TOOL = Path(__file__).resolve().parents[1] / "actions/suite-selection"
sys.path.insert(0, str(TOOL))

import suite_selection


class NewEntryFileTests(TemplateRepoTests):
    """The selection command on a change that adds an entry file to a suite with stored lists."""

    LIST = "checks/suites.json"
    STORED = ["lib/a.py", "lib/b.py", "tests/test_a.py", "tests/test_b.py"]
    WHOLE = {"name": "unit", "entries": ["tests/test_*.py"], "paths": STORED}
    BY_PATH = {"name": "unit", "entries": ["tests/test_a.py", "tests/test_b.py"], "paths": STORED}
    PER_ENTRY = {"name": "unit", "entries": ["tests/test_*.py"], "entry_selection": True, "paths": STORED,
                 "entry_paths": {"tests/test_a.py": ["lib/a.py", "tests/test_a.py"], "tests/test_b.py": ["lib/b.py", "tests/test_b.py"]}}
    SKIP = "unit: skip (no changed file matches its path list)"

    @classmethod
    def initialize_template(cls, root: Path) -> str | None:
        fixture_repository(root)
        for path in ("tests/test_a.py", "tests/test_b.py", "lib/a.py", "lib/b.py"):
            write(root, path, path + "\n")
        return None

    def setUp(self) -> None:
        super().setUp()
        self.outputs = Path(self.temporary.name) / "outputs.txt"

    def commit(self, files: dict[str, str] | None = None, suite: dict | None = None) -> str:
        for path, text in (files or {}).items():
            write(self.root, path, text)
        if suite is not None:
            write(self.root, self.LIST, json.dumps({"always": [], "suites": [suite]}, indent=2) + "\n")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "change")
        return git(self.root, "rev-parse", "HEAD")

    def select(self, *arguments: str) -> tuple[str, list[str], dict]:
        """The line of the suite in the report, the entry files that run, and the outputs."""
        self.outputs.write_text("", encoding="utf-8")
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), mock.patch("sys.stderr", error):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / self.LIST),
                                         "--json-output", str(self.outputs), *arguments])
        self.assertEqual(code, 0, error.getvalue())
        values = dict(line.split("=", 1) for line in self.outputs.read_text(encoding="utf-8").splitlines())
        entries = sorted(item["entry"] for shard in json.loads(values["shards"]) for item in shard["entries"])
        return next(line for line in output.getvalue().splitlines() if line.startswith("unit: ")), entries, values

    def between(self, base: str, head: str) -> tuple[str, list[str]]:
        return self.select("--base", base, "--head", head, "--compare", "direct")[:2]

    def test_a_file_that_a_pattern_starts_to_match_runs_a_suite_selected_as_a_whole_with_every_entry_file(self) -> None:
        base = self.commit(suite=self.WHOLE)
        head = self.commit({"tests/test_new.py": "new\n"})
        line, entries = self.between(base, head)
        self.assertEqual(line, "unit: run (`tests/test_new.py` is a new entry file)")
        self.assertEqual(entries, ["tests/test_a.py", "tests/test_b.py", "tests/test_new.py"])

    def test_a_list_of_changed_files_names_an_added_file_that_the_index_holds_and_head_lacks(self) -> None:
        self.commit(suite=self.WHOLE)
        write(self.root, "tests/test_new.py", "new\n")
        git(self.root, "add", "tests/test_new.py")
        line, entries, _ = self.select("--changed-file", "tests/test_new.py")
        self.assertEqual(line, "unit: run (`tests/test_new.py` is a new entry file)")
        self.assertEqual(entries, ["tests/test_a.py", "tests/test_b.py", "tests/test_new.py"])

    def test_a_change_that_adds_no_entry_file_and_touches_no_stored_list_skips_the_suite(self) -> None:
        base = self.commit(suite=self.WHOLE)
        head = self.commit({"stray.txt": "stray\n"})
        self.assertEqual(self.between(base, head), (self.SKIP, []))
        self.assertEqual(self.select("--changed-file", "stray.txt")[:2], (self.SKIP, []))

    def test_an_added_file_that_the_pattern_does_not_match_skips_the_suite(self) -> None:
        base = self.commit(suite=self.WHOLE)
        head = self.commit({"tests/helper.py": "helper\n", "other/test_new.py": "elsewhere\n"})
        self.assertEqual(self.between(base, head), (self.SKIP, []))

    def test_a_changed_entry_file_that_both_sides_hold_is_no_new_entry_file(self) -> None:
        stored = dict(self.WHOLE, paths=["lib/a.py", "lib/b.py"])
        base = self.commit(suite=stored)
        head = self.commit({"tests/test_a.py": "edited\n"})
        self.assertEqual(self.between(base, head), (self.SKIP, []))

    def test_an_entry_file_added_by_path_runs_the_suite_with_the_reason_of_the_suite_list(self) -> None:
        base = self.commit(suite=self.BY_PATH)
        head = self.commit({"tests/test_new.py": "new\n"}, suite=dict(self.BY_PATH, entries=self.BY_PATH["entries"] + ["tests/test_new.py"]))
        line, entries = self.between(base, head)
        self.assertEqual(line, "unit: run (`tests/test_new.py` is new in `checks/suites.json`)")
        self.assertEqual(entries, ["tests/test_a.py", "tests/test_b.py", "tests/test_new.py"])

    def test_a_suite_selected_per_entry_file_runs_the_added_file_alone_with_the_reason_of_the_missing_list(self) -> None:
        base = self.commit(suite=self.PER_ENTRY)
        head = self.commit({"tests/test_new.py": "new\n"})
        line, entries = self.between(base, head)
        self.assertEqual(line, "unit: run (`tests/test_new.py` runs on every change: no stored path list; entries: `tests/test_new.py`)")
        self.assertEqual(entries, ["tests/test_new.py"])

    def test_a_suite_selected_per_entry_file_runs_an_added_file_that_has_a_stored_list_alone(self) -> None:
        stored = dict(self.PER_ENTRY, entry_paths=dict(self.PER_ENTRY["entry_paths"], **{"tests/test_new.py": ["lib/a.py"]}))
        base = self.commit(suite=stored)
        head = self.commit({"tests/test_new.py": "new\n"})
        line, entries = self.between(base, head)
        self.assertEqual(line, "unit: run (`tests/test_new.py` is a new entry file; entries: `tests/test_new.py`)")
        self.assertEqual(entries, ["tests/test_new.py"])

    def test_the_added_entry_files_are_those_that_the_tracked_files_after_the_change_hold_and_those_before_lack(self) -> None:
        import unrecorded

        tracked = frozenset({"tests/test_a.py", "tests/test_b.py", "tests/test_new.py"})
        suite_list = suite_selection.resolve_entries(suite_selection.parse_suite_list(json.dumps({"always": [], "suites": [self.WHOLE]})), set(tracked))
        new = unrecorded.EntryFile("unit", "tests/test_new.py")
        cause = unrecorded.Cause("tests/test_new.py", "`tests/test_new.py` is a new entry file")
        self.assertEqual(unrecorded.added_entry_files(suite_list, tracked - {"tests/test_new.py"}, tracked), {new: cause})
        self.assertEqual(unrecorded.added_entry_files(suite_list, tracked, tracked), {})
        self.assertEqual(unrecorded.added_entry_files(suite_list, tracked - {"tests/test_new.py"}, tracked - {"tests/test_new.py"}), {},
                         "an entry file that the side after the change lacks is not added by it")

    # --- reuse --------------------------------------------------------------------------------------------------

    def reusing(self, base: str, head: str, record: Path) -> tuple[dict, dict]:
        """The running units and the reused units of a pull-request selection of ``head`` with the record."""
        values = self.select("--base", base, "--head", head, "--compare", "merge-base", "--results", str(record))[2]
        return json.loads(values["units"]), json.loads(values["reused"])

    def passed(self, units: dict, record: Path, run: int) -> None:
        record.write_text(json.dumps({"passed": {key: run for key in units}}), encoding="utf-8")

    def test_a_passed_result_of_a_suite_selected_as_a_whole_is_reused_only_with_unchanged_entry_files(self) -> None:
        record = Path(self.temporary.name) / "record.json"
        base = self.commit(suite=self.WHOLE)
        first = self.commit({"lib/a.py": "changed\n"})
        units, reused = self.reusing(base, first, record)
        self.assertEqual(([unit["entries"] for unit in units.values()], reused), ([["tests/test_a.py", "tests/test_b.py"]], {}))
        self.passed(units, record, 101)

        added = self.commit({"tests/test_new.py": "new\n"})
        units, reused = self.reusing(base, added, record)
        self.assertEqual(reused, {}, "a change that only adds an entry file reuses no passed result")
        self.assertEqual([unit["entries"] for unit in units.values()], [["tests/test_a.py", "tests/test_b.py", "tests/test_new.py"]])
        self.passed(units, record, 102)

        edited = self.commit({"tests/test_new.py": "new, and changed\n"})
        running, reused = self.reusing(base, edited, record)
        self.assertEqual(reused, {}, "a change to the content of an entry file outside the stored lists reuses no passed result")
        self.assertEqual([unit["entries"] for unit in running.values()], [["tests/test_a.py", "tests/test_b.py", "tests/test_new.py"]])
        self.assertNotEqual(sorted(running), sorted(units))
        self.passed(running, record, 103)

        unread = self.commit({"stray.txt": "stray\n"})
        again, reused = self.reusing(base, unread, record)
        self.assertEqual((again, sorted(reused)), ({}, sorted(running)), "a head that changes no read and no entry file reuses the pass")


class DirectoryMembershipSelectionTests(TemplateRepoTests):
    prefix = "membership-selection-"

    @classmethod
    def initialize_template(cls, root: Path) -> str | None:
        for name, folder in (("reader", "guides"), ("other", "assets")):
            write(root, f"checks/{name}.py", f'from pathlib import Path\nROOT = Path(__file__).resolve().parents[1]\nlist((ROOT / "{folder}").iterdir())\n')
        write(root, "guides/existing/page.md", "prose\n")
        write(root, "guides/direct.md", "prose\n")
        write(root, "assets/one.txt")
        return None

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


class BackslashExistenceSelectionTests(unittest.TestCase):
    """A backslash in the path of a stored existence read: a character of the name in a Linux suite, a separator in a
    Windows suite."""

    NAME = "D:\\Games\\Example\\game.exe"
    SLASHES = "D:/Games/Example/game.exe"
    ENTRIES = ["checks/one.py", "checks/two.py"]

    def suite_list(self, system: str, read: dict, field: str, per_entry: bool):
        suite = {"name": "probes", "entries": self.ENTRIES, "os": system, "paths": self.ENTRIES, field: [read]}
        if per_entry:
            suite.update(entry_selection=True, entry_paths={entry: [entry] for entry in self.ENTRIES})
            if field == "existence_reads":
                suite["entry_existence_reads"] = {self.ENTRIES[0]: [read]}
        other = {"name": "other", "entries": ["checks/other.py"], "paths": ["checks/other.py"]}
        return suite_selection.parse_suite_list(json.dumps({"always": [], "suites": [suite, other]}))

    def variants(self, system: str, read: dict):
        """Each suite list that stores ``read``, with the entry files that the read selects."""
        for field in ("existence_reads", "extra_existence_reads"):
            for per_entry in (False, True):
                selected = self.ENTRIES[:1] if per_entry and field == "existence_reads" else self.ENTRIES
                yield f"{field}, per entry: {per_entry}", self.suite_list(system, read, field, per_entry), tuple(selected)

    def decide(self, suite_list, changed: list[str], before: set[str], after: set[str]):
        selection = suite_selection.select(suite_list, changed, previous_tracked=frozenset(before), current_tracked=frozenset(after))
        self.assertFalse(selection.decisions[1].run)
        return selection.decisions[0], selection

    def test_a_linux_suite_runs_when_the_change_adds_a_file_with_exactly_the_stored_name(self) -> None:
        tracked = set(self.ENTRIES)
        for label, suite_list, selected in self.variants("linux", {"path": self.NAME, "kind": "any"}):
            with self.subTest(suite=label):
                decision, selection = self.decide(suite_list, [self.NAME], tracked, tracked | {self.NAME})
                self.assertTrue(decision.run)
                self.assertEqual(selected, decision.entries)
                self.assertIn(f"`{self.NAME}` matches `any existence `{self.NAME}``", decision.reason)
                self.assertNotIn(self.NAME, selection.unread)

    def test_a_linux_suite_does_not_run_when_the_change_only_adds_the_path_with_slashes(self) -> None:
        tracked = set(self.ENTRIES)
        for label, suite_list, _ in self.variants("linux", {"path": self.NAME, "kind": "any"}):
            with self.subTest(suite=label):
                decision, selection = self.decide(suite_list, [self.SLASHES], tracked, tracked | {self.SLASHES})
                self.assertFalse(decision.run, decision.reason)
                self.assertIn(self.SLASHES, selection.unread)

    def test_a_linux_suite_runs_on_a_change_beneath_the_stored_name_and_on_one_to_a_directory_above_it(self) -> None:
        tracked = set(self.ENTRIES)
        nested = "data/" + self.NAME
        for kind, path, before, after in (
            ("directory", self.NAME, tracked, tracked | {self.NAME + "/readme.txt"}),
            ("any", nested, tracked | {nested}, tracked | {"data"}),
        ):
            for label, suite_list, selected in self.variants("linux", {"path": path, "kind": kind}):
                with self.subTest(suite=label, path=path):
                    decision, _ = self.decide(suite_list, sorted(before ^ after), before, after)
                    self.assertEqual(selected, decision.entries)
                    self.assertIn(f"{kind} existence `{path}`", decision.reason)

    def test_a_windows_suite_runs_when_the_change_adds_the_first_file_in_a_directory_stated_with_backslashes(self) -> None:
        tracked = set(self.ENTRIES)
        first, second = "tools/cache/a.txt", "tools/cache/b.txt"
        for label, suite_list, selected in self.variants("windows", {"path": "tools\\cache", "kind": "directory"}):
            with self.subTest(suite=label):
                decision, _ = self.decide(suite_list, [first], tracked, tracked | {first})
                self.assertEqual(selected, decision.entries)
                self.assertIn("`tools/cache/a.txt` matches `directory existence `tools\\cache``", decision.reason)
                later, _ = self.decide(suite_list, [second], tracked | {first}, tracked | {first, second})
                self.assertFalse(later.run, later.reason)

    def test_a_windows_suite_does_not_run_when_the_change_adds_a_file_whose_name_holds_the_backslash(self) -> None:
        tracked = set(self.ENTRIES)
        for label, suite_list, _ in self.variants("windows", {"path": "tools\\cache", "kind": "any"}):
            with self.subTest(suite=label):
                decision, _ = self.decide(suite_list, ["tools\\cache"], tracked, tracked | {"tools\\cache"})
                self.assertFalse(decision.run, decision.reason)

    def test_the_reuse_key_holds_the_value_of_the_name_that_the_system_of_the_suite_reads(self) -> None:
        import reuse

        base = {entry: "a" for entry in [*self.ENTRIES, "checks/other.py"]}
        for system, changes in (("linux", {self.NAME: True, self.SLASHES: False}), ("windows", {self.NAME: False, self.SLASHES: True})):
            for label, suite_list, _ in self.variants(system, {"path": self.NAME, "kind": "any"}):
                selection = suite_selection.every_suite(suite_list, "test")

                def keys(files: dict[str, str]) -> list[str]:
                    tree = reuse.Tree(files, suite_selection.matches)
                    return [unit.key for unit in suite_selection.reuse_units(selection, suite_list, tree, {}, {}, ignore_content={})
                            if unit.decision.suite == "probes"]

                for added, changes_key in changes.items():
                    with self.subTest(system=system, suite=label, added=added):
                        before, after = keys(base), keys({**base, added: "b"})
                        self.assertTrue(before)
                        self.assertEqual(changes_key, before != after)

    @unittest.skipIf(sys.platform == "win32", "a Windows file name holds no backslash")
    def test_select_runs_a_linux_suite_on_a_commit_that_adds_a_tracked_file_with_the_stored_name(self) -> None:
        temporary = temporary_support.Directory(prefix="backslash-selection-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        git(root, "init", "-q", "-b", "main")
        suite = {"name": "probes", "entries": ["checks/one.py"], "paths": ["checks/one.py"], "existence_reads": [{"path": self.NAME, "kind": "any"}]}
        write(root, "checks/one.py")
        write(root, "checks/suites.json", json.dumps({"always": [], "suites": [suite]}))

        def commit() -> str:
            git(root, "add", "-A")
            git(root, "commit", "-q", "-m", "fixture")
            return git(root, "rev-parse", "HEAD")

        def select(base: str, head: str) -> str:
            output = io.StringIO()
            with redirect_stdout(output):
                code = suite_selection.main(["select", "--root", str(root), "--suites", str(root / "checks/suites.json"), "--base", base, "--head", head])
            self.assertEqual(0, code, output.getvalue())
            return output.getvalue()

        base = commit()
        write(root, self.SLASHES)
        slashes = commit()
        self.assertIn("probes: skip", select(base, slashes))
        write(root, self.NAME)
        named = commit()
        self.assertIn(f"probes: run (`{self.NAME}` matches `any existence `{self.NAME}``)", select(slashes, named))


class ModuleLookupSelectionTests(TemplateRepoTests):
    """A change that adds a file or directory in a directory in which entry files look up modules, under a
    standard-library name or a stored module lookup of that directory."""

    prefix = "lookup-selection-"

    @classmethod
    def initialize_template(cls, root: Path) -> str | None:
        for entry in ("tests/test_lookup.py", "tests/test_other.py"):
            write(root, entry, "import helper\n")
        write(root, "lib/helper.py", "VALUE = 1\n")
        write(root, "tests/pkg/module.py", "")
        entries = ["tests/test_lookup.py", "tests/test_other.py"]
        suite = {"name": "lookups", "entries": entries, "entry_selection": True, "paths": entries,
                 "entry_paths": {entry: [entry, "lib/helper.py"] for entry in entries},
                 "lookup_directories": ["tests", "lib"], "entry_lookup_directories": {entries[0]: ["tests"], entries[1]: ["lib"]}}
        write(root, "checks/suites.json", json.dumps({"always": [], "suites": [suite], "module_lookups": {"tests": ["helper", "pkg"]}}))
        git(root, "add", "-A")
        git(root, "commit", "-q", "--allow-empty", "-m", "fixture")
        return git(root, "rev-parse", "HEAD")

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


class GlobMembershipModeTests(TemplateRepoTests):
    """A glob membership pattern ending in /** selects on the same changes with --changed-file as between two commits."""

    @classmethod
    def initialize_template(cls, root: Path) -> str | None:
        write(root, ".gitignore", "__pycache__/\n")
        write(root, "checks/test_tools.py")
        write(root, "tools/alpha/TOOL.md", "alpha\n")
        write(root, "tools/alpha/run.py", "run\n")
        write(root, "tools/beta/TOOL.md", "beta\n")
        write(root, "checks/suites.json", json.dumps({"always": [], "suites": [
            {"name": "tools", "entries": ["checks/test_tools.py"], "paths": ["checks/test_tools.py"],
             "directory_memberships": ["tools/*/**"]}]}))
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "base")
        base = git(root, "rev-parse", "HEAD")
        write(root, "tools/alpha/__pycache__/run.cpython-313.pyc", "untracked\n")
        return base

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


class IgnoreRuleSelectionTests(TemplateRepoTests):
    """A change to the tracked .gitignore files, or an added path that the head commit's ignore rules match, runs every
    unit; only the tracked .gitignore files decide, matched case-sensitively."""

    EVERY = {"reads": "run", "each": "run"}

    @classmethod
    def initialize_template(cls, root: Path) -> str | None:
        write(root, ".gitignore", "*.log\n!keep.log\nbuild/\n/cache\n")
        write(root, "docs/.gitignore", "draft-*\n")
        write(root, "checks/test_reads.py")
        write(root, "checks/test_one.py")
        write(root, "checks/test_two.py")
        write(root, "src/app.py", "app\n")
        write(root, "old.log", "tracked although ignored\n")
        write(root, "checks/suites.json", json.dumps({"always": [], "suites": [
            {"name": "reads", "entries": ["checks/test_reads.py"], "paths": ["checks/test_reads.py", "src/**"]},
            {"name": "each", "entries": ["checks/test_one.py", "checks/test_two.py"], "paths": ["checks/**"],
             "entry_selection": True, "entry_paths": {"checks/test_one.py": ["checks/test_one.py"],
                                                      "checks/test_two.py": ["checks/test_two.py"]}}]}))
        git(root, "add", "-A")
        git(root, "add", "-f", "old.log")
        git(root, "commit", "-q", "-m", "base")
        return git(root, "rev-parse", "HEAD")

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


if __name__ == "__main__":
    unittest.main()
