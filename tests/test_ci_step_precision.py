"""Workflow entry gates, shell assignments and name-only membership selection on fixtures."""

from __future__ import annotations

import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

TOOL = Path(__file__).resolve().parents[1] / "actions" / "suite-selection"
sys.path.insert(0, str(TOOL))
import ci_definition
import suite_selection


WORKFLOW = ".github/workflows/build.yml"


def step(entry: str, command: str = "echo check", suite: str = "checks") -> str:
    return (f"      - run: {command}\n"
            f"        if: contains(steps.select.outputs.{suite}-modules, '{entry}')\n")


def workflow(steps: str, job_settings: str = "    runs-on: ubuntu-latest\n") -> str:
    return "name: Build\njobs:\n  test:\n" + job_settings + "    steps:\n" + steps


def suites():
    return suite_selection.parse_suite_list(json.dumps({"always": [], "suites": [
        {"name": name, "entries": entries, "entry_selection": True,
         "paths": entries, "entry_paths": {entry: [entry] for entry in entries}}
        for name, entries in (("checks", ["checks/one.py", "checks/two.py", "checks/three.py"]),
                              ("other", ["checks/other.py"]))
    ]}), "checks/suites.json")


class WorkflowEntryPrecisionTests(unittest.TestCase):
    def selected(self, before: str, after: str, changed: list[str] | None = None):
        gate = ci_definition.changed_step_gate(before, after)
        self.assertIsNotNone(gate)
        result = suite_selection.select(suites(), changed or [WORKFLOW], workflow_steps=(WORKFLOW, gate))
        return {item.suite: item.entries for item in result.decisions if item.run}

    def test_edit_selects_only_the_entry_of_the_changed_step(self):
        before = workflow(step("checks.one") + step("checks.two") + step("checks.other", suite="other"))
        self.assertEqual(self.selected(before, before.replace("echo check", "echo changed", 1)),
                         {"checks": ("checks/one.py",)})

    def test_insert_and_remove_preserve_unchanged_step_consumers(self):
        before = workflow(step("checks.two") + step("checks.other", suite="other"))
        after = workflow(step("checks.one") + step("checks.two") + step("checks.other", suite="other"))
        for old, new in ((before, after), (after, before)):
            with self.subTest(before=old):
                self.assertEqual(self.selected(old, new), {"checks": ("checks/one.py",)})

    def test_regating_unions_previous_and_current_entries_and_suites(self):
        before = workflow(step("checks.one"))
        self.assertEqual(self.selected(before, workflow(step("checks.two"))),
                         {"checks": ("checks/one.py", "checks/two.py")})
        self.assertEqual(self.selected(before, workflow(step("checks.other", suite="other"))),
                         {"checks": ("checks/one.py",), "other": ("checks/other.py",)})

    def test_step_entries_union_with_entries_selected_by_changed_content(self):
        before = workflow(step("checks.one"))
        self.assertEqual(self.selected(before, before.replace("echo check", "echo changed"),
                                       [WORKFLOW, "checks/two.py"]),
                         {"checks": ("checks/one.py", "checks/two.py")})

    def test_job_setup_change_selects_its_step_consumers(self):
        before = workflow(step("checks.one") + step("checks.other", suite="other"))
        self.assertEqual(self.selected(before, before.replace("ubuntu-latest", "windows-latest")),
                         {"checks": ("checks/one.py",), "other": ("checks/other.py",)})

    def test_entry_path_gates_and_whole_suite_gates_keep_their_scope(self):
        before = workflow(step("checks.one")).replace("checks-modules, 'checks.one'", "checks-entries, 'checks/one.py'")
        self.assertEqual(self.selected(before, before.replace("echo check", "echo changed")),
                         {"checks": ("checks/one.py",)})
        after = before.replace("contains(steps.select.outputs.checks-entries, 'checks/one.py')",
                               "steps.select.outputs.checks == 'run'")
        self.assertEqual(self.selected(before, after), {"checks": ("checks/one.py", "checks/two.py", "checks/three.py")})

    def test_entry_gates_on_the_entries_object_of_a_caller_keep_their_entry(self):
        before = workflow(step("checks.one")).replace("contains(steps.select.outputs.checks-modules, 'checks.one')",
                                                      "contains(fromJSON(inputs.entries).checks, 'checks/one.py')")
        self.assertEqual(self.selected(before, before.replace("echo check", "echo changed")),
                         {"checks": ("checks/one.py",)})
        joined = before.replace("contains(fromJSON(inputs.entries).checks, 'checks/one.py')",
                                "contains(fromJSON(needs.select.outputs.entries).checks, 'checks/one.py')"
                                " || contains(fromJSON(inputs.entries).checks, 'checks/two.py')")
        self.assertEqual(self.selected(joined, joined.replace("echo check", "echo changed")),
                         {"checks": ("checks/one.py", "checks/two.py")})

    def test_unsupported_selection_conjunctions_fail_instead_of_omitting_their_suite(self):
        before = workflow(step("checks.one"))
        for condition in (
            "steps.select.outputs.checks == 'run' && github.ref == 'refs/heads/main'",
            "needs.my-select.outputs.checks=='run' && github.ref == 'refs/heads/main'",
            "${{ 'run' == needs.select.outputs.checks && github.ref == 'refs/heads/main' }}",
            "contains(fromJSON(inputs.entries).checks, 'checks/one.py') && github.ref == 'refs/heads/main'",
            "contains(fromJSON(inputs.suites).checks, 'checks/one.py')",
            "steps.select.outputs.checks == 'run' && fromJSON(needs.select.outputs.language-steps).python == 'run'",
        ):
            with self.subTest(condition=condition):
                after = before.replace("contains(steps.select.outputs.checks-modules, 'checks.one')", condition)
                with self.assertRaisesRegex(ci_definition.WorkflowReadError, "unsupported suite-selection condition"):
                    ci_definition.changed_step_gate(before, after)

    def test_control_output_conditions_remain_ungated(self):
        for condition in ("needs.control.outputs.ready == 'success'", "steps.probe.outputs.changed == 'true'"):
            with self.subTest(condition=condition):
                before = workflow(f"      - run: echo control\n        if: {condition}\n")
                after = before.replace("echo control", "echo changed")
                self.assertEqual(ci_definition.changed_step_gate(before, after), ci_definition.Gate())

    def test_language_step_conditions_name_no_suite_gate(self):
        for condition in ("fromJSON(needs.select.outputs.language-steps).python == 'run'",
                          "${{ !cancelled() && fromJSON(inputs.language-steps).python == 'run' }}",
                          "${{ !cancelled() && matrix.shard.shard == 1 && fromJSON(needs.select.outputs.language-steps).python == 'run' }}",
                          "'run' == fromJSON(steps.select.outputs.language-steps).python && github.ref == 'refs/heads/main'"):
            with self.subTest(condition=condition):
                before = workflow(step("checks.one") + f"      - uses: ./actions/python\n        if: {condition}\n")
                after = before.replace("./actions/python", "./actions/python-next")
                self.assertEqual(ci_definition.changed_step_gate(before, after), ci_definition.Gate())

    def test_a_language_step_term_in_a_job_gate_keeps_the_suites_of_its_other_terms(self):
        for language in ("fromJSON(inputs.language-steps).python == 'run'",
                         "'run' == fromJSON(needs.select.outputs.language-steps).python"):
            with self.subTest(language=language):
                gated = "    if: fromJSON(inputs.suites).other == 'run' || " + language + "\n    runs-on: ubuntu-latest\n"
                before = workflow("      - run: echo check\n", gated)
                self.assertEqual(self.selected(before, before.replace("echo check", "echo changed")),
                                 {"other": ("checks/other.py",)})
                alone = "    if: " + language + "\n    runs-on: ubuntu-latest\n"
                before = workflow("      - run: echo check\n", alone)
                self.assertEqual(ci_definition.changed_step_gate(before, before.replace("echo check", "echo changed")),
                                 ci_definition.Gate())

    def test_command_preserves_entry_gates_in_outputs(self):
        before = workflow(step("checks.one") + step("checks.two"))
        after = before.replace("echo check", "echo changed", 1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / WORKFLOW
            path.parent.mkdir(parents=True)
            path.write_text(after, encoding="utf-8")
            manifest = root / "suites.json"
            manifest.write_text(json.dumps({"always": [], "suites": [suite.entry() for suite in suites().suites]}),
                                encoding="utf-8")
            tracked = {WORKFLOW, "suites.json", *(entry for suite in suites().suites for entry in suite.entries)}
            output = root / "outputs.txt"
            with (mock.patch.object(suite_selection, "tracked_files", return_value=tracked),
                  mock.patch.object(suite_selection, "changed_files", return_value=([WORKFLOW], None)),
                  mock.patch.object(suite_selection, "previous_revision", return_value="base"),
                  mock.patch.object(suite_selection, "revision_text", side_effect=[before, after]),
                  mock.patch.object(suite_selection, "revision_files", return_value=frozenset(tracked)),
                  redirect_stdout(io.StringIO())):
                code = suite_selection.main(["select", "--root", str(root), "--suites", str(manifest),
                                             "--base", "base", "--head", "head", "--workflow", WORKFLOW,
                                             "--github-output", str(output)])
            self.assertEqual(code, 0)
            self.assertEqual(output.read_text(encoding="utf-8"),
                             "checks=run\nchecks-entries=checks/one.py\nchecks-modules=checks.one\n"
                             "other=skip\nother-entries=\nother-modules=\n")

    def test_action_used_by_multiple_entries_keeps_each_consumer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / WORKFLOW
            path.parent.mkdir(parents=True)
            path.write_text(workflow(step("checks.one") + step("checks.two")).replace(
                "run: echo check", "uses: ./actions/setup"), encoding="utf-8")
            action = root / "actions/setup/action.yml"
            action.parent.mkdir(parents=True)
            action.write_text("runs:\n  using: composite\n  steps:\n"
                              "    - run: python scripts/setup.py\n      shell: bash\n", encoding="utf-8")
            items = ci_definition.ci_definition(root, WORKFLOW, "suites.json",
                                               {WORKFLOW, "actions/setup/action.yml", "scripts/setup.py"})
            setup = next(item for item in items if item.pattern == "scripts/setup.py")
            self.assertEqual(setup.entries_for("checks"), frozenset({"checks/one.py", "checks/two.py"}))


class ShellReferencePrecisionTests(unittest.TestCase):
    def test_assignment_names_are_not_paths_but_assignment_values_are(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / WORKFLOW
            path.parent.mkdir(parents=True)
            path.write_text(workflow("      - run: |\n"
                                     "          tools=()\n"
                                     "          tools+=(compiler)\n"
                                     "          assets=payload/config.json\n"
                                     "          cp assets/item.txt output.txt\n"), encoding="utf-8")
            items = ci_definition.ci_definition(root, WORKFLOW, "checks/suites.json",
                                               {WORKFLOW, "tools/unrelated.py", "assets/item.txt", "payload/config.json"})
            patterns = {item.pattern for item in items}
            self.assertNotIn("tools/**", patterns)
            self.assertNotIn("assets/**", patterns)
            self.assertIn("assets/item.txt", patterns)
            self.assertIn("payload/config.json", patterns)


class GlobMembershipSelectionTests(unittest.TestCase):
    def test_directory_only_glob_ignores_files_and_tracks_directory_type_changes(self):
        before = frozenset({"existing/one.txt", "replace"})
        for changed, after, expected in (
            (["LICENSE.txt"], before | {"LICENSE.txt"}, None),
            (["existing/two.txt"], before | {"existing/two.txt"}, None),
            (["added/child.txt"], before | {"added/child.txt"}, "added/child.txt"),
            (["existing/one.txt"], before - {"existing/one.txt"}, "existing/one.txt"),
            (["replace", "replace/child.txt"], before - {"replace"} | {"replace/child.txt"}, "replace"),
        ):
            with self.subTest(changed=changed):
                self.assertEqual(suite_selection.membership_change(None, None, "*/", changed,
                                 previous_tracked=before, current_tracked=after), expected)

    def test_glob_membership_selects_only_name_changes_in_its_pattern(self):
        pattern = "checks/tasks/*.ps1"
        before = frozenset({"checks/tasks/one.ps1", "checks/tasks/readme.md", "checks/tasks/nested/old.ps1"})
        for changed, after, expected in (
            (["checks/tasks/one.ps1"], before, None),
            (["checks/tasks/two.ps1"], before | {"checks/tasks/two.ps1"}, "checks/tasks/two.ps1"),
            (["checks/tasks/one.ps1"], before - {"checks/tasks/one.ps1"}, "checks/tasks/one.ps1"),
            (["checks/tasks/guide.md"], before | {"checks/tasks/guide.md"}, None),
            (["checks/tasks/nested/new.ps1"], before | {"checks/tasks/nested/new.ps1"}, None),
            (["checks/tasks/one.ps1", "checks/tasks/renamed.ps1"],
             before - {"checks/tasks/one.ps1"} | {"checks/tasks/renamed.ps1"}, "checks/tasks/one.ps1"),
        ):
            with self.subTest(changed=changed):
                self.assertEqual(suite_selection.membership_change(None, None, pattern, changed,
                                 previous_tracked=before, current_tracked=after), expected)

    def test_manual_glob_membership_compares_names_with_the_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            folder = root / "checks/tasks"
            folder.mkdir(parents=True)
            (folder / "one.ps1").write_text("changed contents", encoding="utf-8")
            tracked = frozenset({"checks/tasks/one.ps1"})
            self.assertIsNone(suite_selection.membership_change(root, None, "checks/tasks/*.ps1",
                              ["checks/tasks/one.ps1"], current_tracked=tracked))
            (folder / "two.ps1").write_text("new file", encoding="utf-8")
            self.assertEqual(suite_selection.membership_change(root, None, "checks/tasks/*.ps1",
                             ["checks/tasks/two.ps1"], current_tracked=tracked), "checks/tasks/two.ps1")


class ExistenceSelectionTests(unittest.TestCase):
    def document(self, kind: str = "file") -> dict:
        document = json.loads(suite_selection.render_suite_list(suites()))
        read = {"path": "data/required.txt", "kind": kind}
        document["suites"][0]["existence_reads"] = [read]
        document["suites"][0]["entry_existence_reads"] = {"checks/one.py": [read]}
        return document

    def test_existence_metadata_roundtrips_and_old_schema_stays_unchanged(self):
        document = self.document()
        parsed = suite_selection.parse_suite_list(json.dumps(document))
        self.assertEqual(json.loads(suite_selection.render_suite_list(parsed)), document)
        legacy = json.loads(suite_selection.render_suite_list(suites()))
        self.assertNotIn("existence_reads", legacy["suites"][0])
        self.assertEqual(json.loads(suite_selection.render_suite_list(
            suite_selection.parse_suite_list(json.dumps(legacy)))), legacy)

    def test_add_delete_rename_and_type_changes_select_only_the_predicate_reader(self):
        file = "data/required.txt"
        nested = file + "/child.txt"
        for kind, before, after, changed, expected in (
            ("file", {file}, {file}, [file], ()),
            ("file", set(), {file}, [file], ("checks/one.py",)),
            ("file", {file}, set(), [file], ("checks/one.py",)),
            ("file", {file}, {"data/renamed.txt"}, [file, "data/renamed.txt"], ("checks/one.py",)),
            ("file", {file}, {nested}, [file, nested], ("checks/one.py",)),
            ("directory", {nested}, {file}, [nested, file], ("checks/one.py",)),
            ("directory", {nested}, {nested, file + "/new.txt"}, [file + "/new.txt"], ()),
            ("any", {file}, {nested}, [file, nested], ()),
            ("any", set(), {nested}, [nested], ("checks/one.py",)),
        ):
            with self.subTest(kind=kind, before=before, after=after):
                parsed = suite_selection.parse_suite_list(json.dumps(self.document(kind)))
                selected = suite_selection.select(parsed, changed, previous_tracked=frozenset(before),
                                                  current_tracked=frozenset(after))
                self.assertEqual(selected.decisions[0].entries, expected)
                self.assertFalse(selected.decisions[1].run)
                if expected:
                    self.assertIn("existence", selected.decisions[0].reason)
                    self.assertNotIn(changed[0], selected.unread)

    def test_manual_selection_compares_index_presence_with_current_file_type(self):
        parsed = suite_selection.parse_suite_list(json.dumps(self.document()))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "data/required.txt"
            path.parent.mkdir()
            path.write_text("new contents", encoding="utf-8")
            tracked = frozenset({"data/required.txt"})
            self.assertFalse(suite_selection.select(parsed, ["data/required.txt"], root=root,
                                                    current_tracked=tracked).decisions[0].run)
            path.unlink()
            path.mkdir()
            self.assertEqual(suite_selection.select(parsed, ["data/required.txt"], root=root,
                                                   current_tracked=tracked).decisions[0].entries, ("checks/one.py",))

    def test_invalid_existence_records_fail_with_the_field_named(self):
        for records in ([{"path": "data/a", "kind": "contents"}], [{"path": "", "kind": "file"}],
                        [{"path": "data/a"}], "data/a"):
            document = self.document()
            document["suites"][0]["existence_reads"] = records
            with self.subTest(records=records), self.assertRaisesRegex(suite_selection.SuiteListError, "existence_reads"):
                suite_selection.parse_suite_list(json.dumps(document))

    def test_content_extra_paths_still_select_their_readers(self):
        document = self.document()
        document["suites"][0]["entry_extra_paths"] = {"checks/two.py": ["data/required.txt"]}
        selected = suite_selection.select(suite_selection.parse_suite_list(json.dumps(document)), ["data/required.txt"],
                                          previous_tracked=frozenset({"data/required.txt"}),
                                          current_tracked=frozenset({"data/required.txt"}))
        self.assertEqual(selected.decisions[0].entries, ("checks/two.py",))
        document["suites"][0]["extra_paths"] = ["data/required.txt"]
        selected = suite_selection.select(suite_selection.parse_suite_list(json.dumps(document)), ["data/required.txt"],
                                          previous_tracked=frozenset({"data/required.txt"}),
                                          current_tracked=frozenset({"data/required.txt"}))
        self.assertEqual(selected.decisions[0].entries, ("checks/one.py", "checks/two.py", "checks/three.py"))

    def test_whole_suite_existence_reads_select_without_an_entry_map(self):
        document = self.document()
        for key in ("entry_selection", "entry_paths", "entry_existence_reads"):
            document["suites"][0].pop(key)
        parsed = suite_selection.parse_suite_list(json.dumps(document))
        selected = suite_selection.select(parsed, ["data/required.txt"], previous_tracked=frozenset(),
                                          current_tracked=frozenset({"data/required.txt"}))
        self.assertEqual(selected.decisions[0].entries, ("checks/one.py", "checks/two.py", "checks/three.py"))

    def test_entry_existence_metadata_requires_known_entry_and_entry_selection(self):
        document = self.document()
        document["suites"][0]["entry_existence_reads"] = {"checks/missing.py": [{"path": "data/a", "kind": "any"}]}
        with self.assertRaisesRegex(suite_selection.SuiteListError, "which the entries lack"):
            suite_selection.parse_suite_list(json.dumps(document))
        document = self.document()
        document["suites"][0]["entry_selection"] = False
        with self.assertRaisesRegex(suite_selection.SuiteListError, "entry_existence_reads needs entry_selection"):
            suite_selection.parse_suite_list(json.dumps(document))


class ExtraMembershipSelectionTests(unittest.TestCase):
    PATTERN = "data/*/**/*"
    ALL = ("checks/one.py", "checks/two.py", "checks/three.py")
    # Each change: the changed files, the tracked files before and after, and whether it changes the membership.
    CHANGES = (
        ("add", ["data/kit/new.py"], {"data/kit/run.py"}, {"data/kit/run.py", "data/kit/new.py"}, True),
        ("remove", ["data/kit/run.py"], {"data/kit/run.py", "data/kit/other.py"}, {"data/kit/other.py"}, True),
        ("rename", ["data/kit/run.py", "data/kit/moved.py"], {"data/kit/run.py"}, {"data/kit/moved.py"}, True),
        ("content", ["data/kit/run.py"], {"data/kit/run.py"}, {"data/kit/run.py"}, False),
    )

    def variants(self) -> dict:
        stored = json.loads(suite_selection.render_suite_list(suites()))
        stored["suites"][0]["extra_directory_memberships"] = [self.PATTERN]
        whole = json.loads(json.dumps(stored))
        for key in ("entry_selection", "entry_paths"):
            whole["suites"][0].pop(key)
        return {"per entry": stored, "whole suite": whole}

    def test_extra_directory_memberships_roundtrip(self):
        document = self.variants()["per entry"]
        self.assertEqual(json.loads(suite_selection.render_suite_list(suite_selection.parse_suite_list(json.dumps(document)))), document)

    def test_a_matching_path_added_removed_or_renamed_selects_every_entry_and_a_content_change_does_not(self):
        for label, document in self.variants().items():
            parsed = suite_selection.parse_suite_list(json.dumps(document))
            for change, changed, before, after, selects in self.CHANGES:
                for mode in ("between two commits", "changed files"):
                    with self.subTest(suite=label, change=change, mode=mode), tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        for file in [entry for suite in parsed.suites for entry in suite.entries] + sorted(after):
                            (root / file).parent.mkdir(parents=True, exist_ok=True)
                            (root / file).write_text("", encoding="utf-8")
                        if mode == "between two commits":
                            selected = suite_selection.select(parsed, changed, root=root, previous_tracked=frozenset(before),
                                                              current_tracked=frozenset(after))
                        else:
                            # Changed files name the change; the index holds the files before it and the disk those after it.
                            selected = suite_selection.select(parsed, changed, root=root, current_tracked=frozenset(before))
                        self.assertEqual(selected.decisions[0].entries, self.ALL if selects else ())
                        self.assertFalse(selected.decisions[1].run)
                        if selects:
                            self.assertIn(f"directory membership `{self.PATTERN}`", selected.decisions[0].reason)

    def test_an_invalid_extra_directory_membership_fails_naming_the_entry(self):
        for value, message in ((["data/*/**/*", ""], "extra_directory_memberships[2]"), ([7], "extra_directory_memberships[1]"),
                               ("data/*/**/*", "extra_directory_memberships must be a list")):
            document = self.variants()["per entry"]
            document["suites"][0]["extra_directory_memberships"] = value
            with self.subTest(value=value), self.assertRaisesRegex(suite_selection.SuiteListError, re.escape(message)):
                suite_selection.parse_suite_list(json.dumps(document))


class ExtraExistenceSelectionTests(unittest.TestCase):
    PATH = "Directory.Build.props"
    ALL = ("checks/one.py", "checks/two.py", "checks/three.py")
    # Each change: the changed files, the tracked files before and after, and whether it changes the file's existence.
    CHANGES = (
        ("add", [PATH], set(), {PATH}, True),
        ("remove", [PATH], {PATH}, set(), True),
        ("content", [PATH], {PATH}, {PATH}, False),
    )

    def variants(self) -> dict:
        stored = json.loads(suite_selection.render_suite_list(suites()))
        stored["suites"][0]["extra_existence_reads"] = [{"path": self.PATH, "kind": "file"}]
        whole = json.loads(json.dumps(stored))
        for key in ("entry_selection", "entry_paths"):
            whole["suites"][0].pop(key)
        return {"per entry": stored, "whole suite": whole}

    def test_extra_existence_reads_roundtrip(self):
        for label, document in self.variants().items():
            with self.subTest(suite=label):
                rendered = suite_selection.render_suite_list(suite_selection.parse_suite_list(json.dumps(document)))
                self.assertEqual(json.loads(rendered), document)

    def test_an_added_or_removed_file_selects_every_entry_and_a_content_change_does_not(self):
        for label, document in self.variants().items():
            parsed = suite_selection.parse_suite_list(json.dumps(document))
            for change, changed, before, after, selects in self.CHANGES:
                with self.subTest(suite=label, change=change):
                    selected = suite_selection.select(parsed, changed, previous_tracked=frozenset(before),
                                                      current_tracked=frozenset(after))
                    self.assertEqual(selected.decisions[0].entries, self.ALL if selects else ())
                    self.assertFalse(selected.decisions[1].run)
                    if selects:
                        self.assertIn(f"file existence `{self.PATH}`", selected.decisions[0].reason)
                        self.assertNotIn(self.PATH, selected.unread)

    def test_the_reuse_key_holds_the_value_of_an_extra_existence_read(self):
        import reuse

        for label, document in self.variants().items():
            parsed = suite_selection.parse_suite_list(json.dumps(document))
            selection = suite_selection.every_suite(parsed, "test")
            keys = []
            for files in ({"checks/one.py": "a"}, {"checks/one.py": "a", self.PATH: "b"}):
                tree = reuse.Tree(files, suite_selection.matches)
                keys.append([key for key, decision, _ in suite_selection.reuse_units(selection, parsed, tree, {}, {}, ignore_content={})
                             if decision.suite == "checks"])
            with self.subTest(suite=label):
                self.assertTrue(keys[0])
                self.assertTrue(all(before != after for before, after in zip(*keys)))

    def test_an_invalid_extra_existence_read_fails_naming_the_field(self):
        item = r"extra_existence_reads\[1\] must name a non-empty path and kind file, directory or any"
        for value, error in (([{"path": self.PATH, "kind": "contents"}], item), ([{"path": "", "kind": "file"}], item),
                             (self.PATH, r"extra_existence_reads must be a list of path and kind records")):
            document = self.variants()["per entry"]
            document["suites"][0]["extra_existence_reads"] = value
            with self.subTest(value=value), self.assertRaisesRegex(suite_selection.SuiteListError, error):
                suite_selection.parse_suite_list(json.dumps(document))


if __name__ == "__main__":
    unittest.main()
