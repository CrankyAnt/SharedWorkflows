"""Suite selection on unrecorded inputs: suite-list metadata, workflow job changes, and recording module bounds."""

from __future__ import annotations

import io
import json
import shutil
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from tests import temporary_support
from tests.suite_selection_fixtures import git, write

TOOL = Path(__file__).resolve().parents[1] / "actions/suite-selection"
sys.path.insert(0, str(TOOL))

import suite_selection


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

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        if cls is UnrecordedFixture:
            return
        template_dir = temporary_support.make_directory(cls.addClassCleanup, prefix="unrecorded-template-")
        cls._template = template_dir / "repo"
        cls._template.mkdir()
        cls.layout(cls._template)
        git(cls._template, "init", "-q", "-b", "main")
        git(cls._template, "config", "core.autocrlf", "false")
        git(cls._template, "add", "-A")
        git(cls._template, "commit", "-q", "-m", "base")
        cls._base_sha = git(cls._template, "rev-parse", "HEAD")

    def setUp(self) -> None:
        self.temporary = temporary_support.Directory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        shutil.copytree(self._template, self.root)
        self.base = self._base_sha

    @classmethod
    def layout(cls, root: Path) -> None:
        write(root, cls.WORKFLOW, RECORDED_VALIDATE)
        write(root, cls.SUITES, json.dumps(RECORDED_SUITES, indent=2) + "\n")
        write(root, ".github/workflows/suite-selection.yml", "on:\n  workflow_call:\njobs:\n  select:\n    runs-on: ubuntu-latest\n"
              "    steps:\n      - run: python3 actions/suite-selection/suite_selection.py select --root .\n")
        write(root, ".github/workflows/suite-verdict.yml", "on:\n  workflow_call:\njobs:\n  validation:\n    runs-on: ubuntu-latest\n"
              "    steps:\n      - run: python3 actions/suite-selection/suite_selection.py verdict\n")
        write(root, "actions/recording/action.yml", "runs:\n  using: composite\n  steps:\n    - shell: bash\n"
              "      env:\n        RESOURCE_PATH: ${{ github.action_path }}\n      run: python3 \"$RESOURCE_PATH/recording.py\" check\n")
        write(root, "actions/recording/recording.py", "import sys\n\nsys.path.insert(0, 'suite-selection')\n\nimport suite_selection\n\n\n"
              "def apply():\n    import apply_rules\n")
        write(root, "actions/suite-selection/apply_rules.py", "import json\n")
        write(root, "actions/recording/etw-consumer/Program.cs", "class Program {}\n")
        write(root, "actions/recording/etw_consumer_build.py", "print('build')\n")
        write(root, "actions/suite-selection/suite_selection.py", "import ci_definition\n\n\ndef reuse_units():\n    import reuse\n")
        write(root, "actions/suite-selection/ci_definition.py", "import json\n")
        write(root, "actions/suite-selection/reuse.py", "import json\n")
        write(root, "actions/suite-selection/report.py", "import json\n")
        write(root, "actions/python-language-step/action.yml", "runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: ruff check\n")
        for entry in ("checks/test_a.py", "checks/test_b.py", "checks/test_c.py", "checks/test_docs.py", "checks/test_selection.py",
                      "checks/test_win.py", "src/a.py", "src/b.py", "data/a.txt", "docs/guide.md", "README.md"):
            write(root, entry, entry + "\n")

    def head_after(self, files: dict[str, str]) -> str:
        return self.commit_on(self.base, files)

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
        if files:
            git(self.root, "add", "--", *files)
        git(self.root, "commit", "-q", "-m", "change")
        return (self.root / ".git" / "HEAD").read_text(encoding="utf-8").strip()

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
            "addition to the every-suite patterns": lambda suites, document: document["always"].append("config/other.txt"),
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
            "addition to the every-suite patterns": (lambda suites, document: document["always"].append("config/other.txt"), readers),
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
        estimate["entry_durations_version"] = suite_selection.ENTRY_DURATIONS_VERSION
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
        self.assertIn(next(path for path in self.allowed if path.endswith("/suite_list.py")), loaded)
        self.assertLessEqual(loaded, self.allowed, "a module outside the set that recording_modules.py names")

    def test_the_set_holds_the_modules_that_recording_py_loads_and_not_those_that_only_select_imports(self) -> None:
        names = {path.rsplit("/", 1)[-1] for path in self.allowed}
        self.assertLessEqual({"recording.py", "suite_list.py", "recording_modules.py"}, names)
        self.assertFalse({"suite_selection.py", "ci_definition.py", "revisions.py", "reuse.py", "unrecorded.py"} & names,
                         "selection code is not loaded by the recording step")

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

    @classmethod
    def layout(cls, root: Path) -> None:
        write(root, cls.WORKFLOW, CONSUMER_CALLER)
        write(root, ".github/workflows/tests.yml", CONSUMER_TESTS)
        write(root, cls.SUITES, json.dumps(CONSUMER_SUITES, indent=2) + "\n")
        for name in ("unit", "lint", "other"):
            write(root, f"checks/test_{name}.py", name + "\n")

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
