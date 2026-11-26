"""Suite selection sharding, CI definitions, gated steps, relative paths, and workflow step rules."""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests import temporary_support
from tests.suite_selection_fixtures import (
    DELTA_REASON, FIXTURE_SUITES, FIXTURE_UNIT, FixtureTemplateTests, git, write,
)

TOOL = Path(__file__).resolve().parents[1] / "actions/suite-selection"
sys.path.insert(0, str(TOOL))

import ci_definition
import suite_selection


class ShardTests(unittest.TestCase):
    """The shard matrix, the Windows entry files and the language step in the JSON outputs of select, on fixture suite lists."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._shared_root = temporary_support.make_directory(cls.addClassCleanup, prefix="shard-repo-")
        git(cls._shared_root, "init", "-q", "-b", "main")
        for name in "abcdefgh":
            write(cls._shared_root, f"checks/test_{name}.py")
        write(cls._shared_root, "checks/test_win.py")
        write(cls._shared_root, "src/app.py")
        write(cls._shared_root, "docs/guide.md")
        git(cls._shared_root, "add", "-A")
        git(cls._shared_root, "commit", "-q", "-m", "base")

    def setUp(self) -> None:
        self.scratch = temporary_support.make_directory(self.addCleanup, prefix="shard-scratch-")
        self.root = self._shared_root

    def suite(self, name: str, durations: dict[str, float], **extra) -> dict:
        entries = [f"checks/test_{entry}.py" for entry in durations] if "entries" not in extra else extra.pop("entries")
        suite = {"name": name, "entries": entries, "paths": ["src/**"],
                 "command": ["python", "-m", "unittest"], "setups": ["python"]}
        estimates = {f"checks/test_{entry}.py": value for entry, value in durations.items() if value is not None}
        if estimates:
            suite["entry_durations"] = estimates
        suite.update(extra)
        return suite

    def run_select(self, suites: list[dict], *arguments: str, github_actions: bool = False) -> tuple[dict[str, str], str, str]:
        suites_path = write(self.scratch, "suites.json", json.dumps({"always": [], "suites": suites}))
        output = self.scratch / "outputs.txt"
        summary = self.scratch / "summary.md"
        output.unlink(missing_ok=True)
        summary.unlink(missing_ok=True)
        printed = io.StringIO()
        environment = {"GITHUB_ACTIONS": "true"} if github_actions else {}
        with mock.patch.dict(os.environ, environment), redirect_stdout(printed):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(suites_path),
                                         "--json-output", str(output), "--summary", str(summary), *arguments])
        self.assertEqual(code, 0)
        outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
        return outputs, printed.getvalue(), summary.read_text(encoding="utf-8")

    def outputs(self, suites: list[dict], *arguments: str) -> dict[str, str]:
        return self.run_select(suites, *arguments)[0]

    def shards(self, suites: list[dict], *arguments: str) -> list[dict]:
        return json.loads(self.outputs(suites, *(arguments or ("--changed-file", "src/app.py")))["shards"])

    @staticmethod
    def layout(shards: list[dict]) -> list[tuple[float, list[str]]]:
        return [(shard["estimate"], [item["entry"].removeprefix("checks/test_").removesuffix(".py") for item in shard["entries"]]) for shard in shards]

    def test_the_fewest_shards_keep_each_shard_s_two_worker_time_within_the_bound_assigned_longest_first(self) -> None:
        # SHARD_BOUND is 90 s (180 s goal minus 90 s fixed cost).
        # Entries 70, 50, 45, 35, 30, 20: one shard takes 130 s on two workers, two shards fit within 90 s.
        # Greedy longest-first over two shards: 70 -> s1, 50 -> s2, 45 -> s2, 35 -> s1, 30 -> s2, 20 -> s1.
        shards = self.shards([self.suite("unit", {"a": 70, "b": 50, "c": 45, "d": 35, "e": 30, "f": 20})])
        self.assertEqual(self.layout(shards), [(70, ["a", "d", "f"]), (75, ["b", "c", "e"])])
        self.assertEqual([shard["shard"] for shard in shards], [1, 2])
        self.assertEqual(shards[0]["entries"][0], {"suite": "unit", "entry": "checks/test_a.py", "command": ["python", "-m", "unittest"], "timeout": 210})
        self.assertEqual(shards[0]["setups"], ["python"])

    def test_work_that_fits_two_workers_within_the_bound_stays_in_one_shard(self) -> None:
        shards = self.shards([self.suite("unit", {"a": 45, "b": 40, "c": 40, "d": 45})])
        self.assertEqual(self.layout(shards), [(85, ["a", "d", "b", "c"])])

    def test_an_entry_file_over_the_bound_gets_a_shard_of_its_own_and_is_reported_as_a_departure(self) -> None:
        outputs, printed, summary = self.run_select(
            [self.suite("unit", {"a": 130, "b": 50, "c": 40})], "--changed-file", "src/app.py", github_actions=True)
        self.assertEqual(self.layout(json.loads(outputs["shards"])), [(130, ["a"]), (50, ["b", "c"])])
        self.assertIn("Departure: shard 1 exceeds the 90 s bound (130.0 s)", printed)
        self.assertIn("::warning::Departure: shard 1 exceeds the 90 s bound (130.0 s)", printed)
        self.assertIn("### Departures", summary)
        self.assertIn("| Shard | Over-bound departure |", summary)
        self.assertIn("| `shard 1` | exceeds the 90 s bound (130.0 s) |", summary)

    def test_an_entry_file_without_a_measured_duration_packs_under_the_assumed_estimate_without_changing_timeout_or_estimate(self) -> None:
        # Assumed estimate is 60 s (180 s floor / 3): 60, 40 and 30 s take 70 s on two workers, so one shard under 90 s.
        # Reported estimate counts measured entries only (40 s), and the unmeasured entry keeps its 180 s floor timeout.
        shards = self.shards([self.suite("unit", {"a": 40, "b": None}), self.suite("other", {"c": 30})])
        self.assertEqual(self.layout(shards), [(40, ["b", "a", "c"])])
        by_entry = {item["entry"]: item for item in shards[0]["entries"]}
        self.assertEqual(by_entry["checks/test_b.py"]["timeout"], 180)
        self.assertEqual(by_entry["checks/test_a.py"]["timeout"], 180)

    def test_unmeasured_entry_files_pack_under_the_assumed_estimate_while_windows_stays_unsharded(self) -> None:
        windows = self.suite("windows", {}, entries=["checks/test_a.py", "checks/test_b.py"], os="windows", paths=["docs/**"],
                             entry_durations={"checks/test_a.py": 300})
        outputs = self.outputs([self.suite("unit", {"a": 40, "b": None, "c": None}, paths=["docs/**"]), windows],
                               "--changed-file", "docs/guide.md")
        # Packing weights 60, 60 and 40 s take 100 s in one shard (> 90 s), so two shards: [b, a] (60 s) and [c] (60 s).
        self.assertEqual(self.layout(json.loads(outputs["shards"])), [(40, ["b", "a"]), (0, ["c"])])
        self.assertEqual(json.loads(outputs["windows"])["estimate"], 300, "Windows is not sharded and sums its measured estimates")

    def test_unmeasured_entry_files_pack_two_per_shard_on_normal_runs_and_six_on_full_recordings(self) -> None:
        shards = self.shards([self.suite("unit", {"a": None, "b": None, "c": None})])
        self.assertEqual(self.layout(shards), [(0, ["a", "c"]), (0, ["b"])])
        # Full recording uses 100 s assumed estimate (300 s floor / 3) under the 300 s full bound: 6 fit in one shard, 7 split into 2.
        full_shards = json.loads(self.outputs([self.suite("unit", {name: None for name in "abcdefg"})], "--full")["shards"])
        self.assertEqual(self.layout(full_shards), [(0, ["a", "c", "e", "g"]), (0, ["b", "d", "f"])])
        self.assertTrue(all(item["timeout"] == 300 for shard in full_shards for item in shard["entries"]))

    def test_linux_shards_stop_at_the_cap_spread_remaining_entries_and_report_over_bound_shards_as_departures(self) -> None:
        repo = self.scratch / "repo"
        shutil.copytree(self._shared_root, repo)
        self.root = repo
        entries = [f"checks/test_u{index:02d}.py" for index in range(45)]
        for entry in entries:
            write(self.root, entry)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "add entries")
        outputs, printed, summary = self.run_select(
            [self.suite("unit", {}, entries=entries)], "--changed-file", "src/app.py", github_actions=True)
        shards = json.loads(outputs["shards"])
        self.assertEqual(len(shards), suite_selection.MAX_LINUX_SHARDS)
        self.assertEqual(sorted(item["entry"] for shard in shards for item in shard["entries"]), entries)
        sizes = sorted(len(shard["entries"]) for shard in shards)
        self.assertEqual(sizes, [2] * 15 + [3] * 5)
        self.assertEqual(printed.count("::warning::Departure: shard "), 5)
        self.assertIn("### Departures", summary)
        self.assertIn("| Shard | Over-bound departure |", summary)

    def test_a_running_language_step_uses_the_least_loaded_measured_shard_as_shard_1_and_reserves_its_allowance(self) -> None:
        # 80 + 15 s language-step allowance exceeds 90 s, so [80, 10] splits and puts [10] in shard 1.
        shards = self.shards([self.suite("unit", {"a": 80, "b": 10})], "--changed-file", "src/app.py", "--language-step", "python")
        self.assertEqual(self.layout(shards), [(10, ["b"]), (80, ["a"])])
        # With an entry over the bound, the lighter measured shard [50, 40] becomes shard 1 so the language step does not stack onto 130 s.
        shards = self.shards([self.suite("unit", {"a": 130, "b": 50, "c": 40})], "--changed-file", "src/app.py", "--language-step", "python")
        self.assertEqual(self.layout(shards), [(50, ["b", "c"]), (130, ["a"])])
        # A shard with unmeasured entries only ([c, d], weight 60 s) is not ranked ahead of a measured shard ([b, e], weight 65 s).
        shards = self.shards([self.suite("unit", {"a": 70, "b": 65, "c": None, "d": None, "e": None})],
                             "--changed-file", "src/app.py", "--language-step", "python")
        self.assertEqual(self.layout(shards), [(65, ["b", "e"]), (70, ["a"]), (0, ["c", "d"])])
        # When every shard is unmeasured, shard 1 falls back to ranking all shards.
        shards = self.shards([self.suite("unit", {"a": None, "b": None, "c": None})],
                             "--changed-file", "src/app.py", "--language-step", "python")
        self.assertEqual(self.layout(shards), [(0, ["a", "c"]), (0, ["b"])])
        # When splitting cannot reduce the minimum shard below bound - allowance, [80, 80] stays in one shard and reports a departure.
        outputs, printed, summary = self.run_select(
            [self.suite("unit", {"a": 80, "b": 80})], "--changed-file", "src/app.py", "--language-step", "python")
        self.assertEqual(self.layout(json.loads(outputs["shards"])), [(80, ["a", "b"])])
        self.assertIn("Departure: shard 1 exceeds the 90 s bound (80.0 s + 15 s language step = 95.0 s)", printed)
        self.assertIn("| `shard 1` | exceeds the 90 s bound (80.0 s + 15 s language step = 95.0 s) |", summary)
        # When the language step is skipped, the allowance does not apply and [80, 10] stays in one shard.
        shards = self.shards([self.suite("unit", {"a": 80, "b": 10}, paths=["docs/**"])],
                             "--changed-file", "docs/guide.md", "--language-step", "python")
        self.assertEqual(self.layout(shards), [(80, ["a", "b"])])

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
            {"suite": "windows", "entry": "checks/test_win.py", "command": ["python", "-m", "unittest"], "timeout": 240}]})
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

    def test_the_full_recording_runs_every_entry_file_in_shards_under_300_s_of_entry_time(self) -> None:
        # Full recording uses the same entry-time scale: 150, 120, 100 and 80 s take 230 s on two
        # workers, so one shard under the 300 s full-recording bound, where a normal run needs more shards under 90 s.
        suites = [self.suite("unit", {"a": 150, "b": 120}, paths=["docs/**"]), self.suite("other", {"c": 100, "d": 80}, paths=["docs/**"])]
        outputs = self.outputs(suites, "--full")
        self.assertEqual(self.layout(json.loads(outputs["shards"])), [(230, ["a", "b", "c", "d"])])
        self.assertEqual(json.loads(outputs["suites"]), {"unit": "run", "other": "run"})
        self.assertEqual(len(self.shards(suites, "--changed-file", "docs/guide.md")), 4)

    def test_the_full_recording_splits_work_over_300_s_of_wide_traced_time(self) -> None:
        # 250, 180 and 150 s take 330 s on two workers, exceeding the 300 s full-recording bound.
        shards = json.loads(self.outputs([self.suite("unit", {"a": 250, "b": 180, "c": 150})], "--full")["shards"])
        self.assertEqual(self.layout(shards), [(250, ["a"]), (180, ["b", "c"])])

    def test_the_full_recording_lists_every_windows_entry_file(self) -> None:
        windows = self.suite("windows", {}, entries=["checks/test_win.py"], os="windows", paths=["docs/**"],
                             entry_durations={"checks/test_win.py": 80})
        per_entry = self.suite("per-entry", {"a": 10, "b": 10}, entry_selection=True,
                               entry_paths={"checks/test_a.py": ["docs/**"], "checks/test_b.py": ["docs/**"]})
        outputs = self.outputs([per_entry, windows], "--full")
        self.assertEqual([item["entry"] for item in json.loads(outputs["windows"])["entries"]], ["checks/test_win.py"])
        self.assertEqual(json.loads(outputs["windows"])["estimate"], 80, "the Windows job records under its one ETW session")
        self.assertEqual(self.layout(json.loads(outputs["shards"])), [(10, ["a", "b"])])

    def test_the_full_recording_takes_no_changed_files(self) -> None:
        suites_path = write(self.scratch, "suites.json", json.dumps({"always": [], "suites": [self.suite("unit", {"a": 10})]}))
        for extra in (["--changed-file", "src/app.py"], ["--base", "HEAD", "--head", "HEAD"]):
            with self.subTest(extra=extra), redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()), \
                    self.assertRaises(SystemExit) as caught:
                suite_selection.main(["select", "--root", str(self.root), "--suites", str(suites_path), "--full", *extra])
            self.assertEqual(caught.exception.code, 2)

    def test_an_unknown_language_step_fails_select_with_exit_2(self) -> None:
        suites_path = write(self.scratch, "suites.json", json.dumps({"always": [], "suites": [self.suite("unit", {"a": 10})]}))
        error = io.StringIO()
        with redirect_stdout(io.StringIO()), mock.patch("sys.stderr", error), self.assertRaises(SystemExit) as caught:
            suite_selection.main(["select", "--root", str(self.root), "--suites", str(suites_path),
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


class CiDefinitionTests(FixtureTemplateTests):
    """The CI-definition rule on fixture workflows in a fixture Git repository."""

    @classmethod
    def populate(cls, root: Path) -> None:
        write(root, ".github/workflows/validation.yml", FIXTURE_CALLER)
        write(root, ".github/workflows/unit.yml", FIXTURE_UNIT)
        write(root, ".github/workflows/selection.yml", "on:\n  workflow_call:\njobs:\n  select:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: $/kit/selection\n")
        write(root, ".github/workflows/verdict.yml", "on:\n  workflow_call:\njobs:\n  validation:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ok\n")
        write(root, "tooling/setup/action.yml", "runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: cat tooling/defaults.json\n")
        write(root, "tooling/defaults.json", "{}\n")
        write(root, "kit/selection/action.yml", "runs:\n  using: composite\n  steps:\n    - shell: bash\n      env:\n        RESOURCE_PATH: ${{ github.action_path }}\n      run: python \"$RESOURCE_PATH/select.py\"\n")
        write(root, "kit/selection/select.py", "")
        write(root, "kit/selection/notes.txt", "a file of the action's folder that no step names\n")
        write(root, "checks/suites.json", json.dumps(FIXTURE_SUITES, indent=2) + "\n")

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._tracked = suite_selection.tracked_files(cls._template)

    def setUp(self) -> None:
        super().setUp()
        self.tracked = self._tracked

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


class GatedStepTests(FixtureTemplateTests):
    """A file that only steps gated on particular suites name runs those suites, not every suite; a file an ungated
    step names runs every suite, whatever gated steps name it too."""

    @classmethod
    def populate(cls, root: Path) -> None:
        write(root, ".github/workflows/validation.yml", GATED_CALLER)
        write(root, "tooling/setup/action.yml", "runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: cat tooling/defaults.json\n")
        for name in ("tooling/defaults.json", "tooling/pins.json", "tooling/platform.json", "tooling/install.py", "tooling/other-form.txt", "kit/select.py"):
            write(root, name, "")
        write(root, "checks/suites.json", json.dumps(FIXTURE_SUITES, indent=2) + "\n")

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._suite_list = suite_selection.load_suite_list(cls._template / "checks" / "suites.json")
        cls._definition = ci_definition.ci_definition(cls._template, ".github/workflows/validation.yml", "checks/suites.json", suite_selection.tracked_files(cls._template))

    def setUp(self) -> None:
        super().setUp()
        self.suite_list = self._suite_list
        self.definition = self._definition

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


class DotSegmentTests(FixtureTemplateTests):
    """A ``run`` path that starts with ``..`` resolves against the step's working directory, and ``..`` segments in any
    reference resolve, before the reference is matched against tracked paths."""

    @classmethod
    def populate(cls, root: Path) -> None:
        for name in ("kit/select.py", "scripts/build.py", "tools/scripts/build.py", "app/lint.py", "outside.py", "tools/shared/x.py"):
            write(root, name, "")
        write(root, "tools/part/action.yml", "runs:\n  using: composite\n  steps:\n    - shell: bash\n      run: python \"$GITHUB_ACTION_PATH/../shared/x.py\"\n")
        write(root, "checks/suites.json", json.dumps(FIXTURE_SUITES, indent=2) + "\n")

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


class WorkflowStepRuleTests(FixtureTemplateTests):
    """The calling workflow compared per step between the two commits: a change inside the steps of particular
    suites runs those suites and the suites that read the workflow; any other change runs every suite."""

    @classmethod
    def populate(cls, root: Path) -> None:
        write(root, ".github/workflows/validate.yml", VALIDATE_WORKFLOW)
        write(root, "kit/selection/select.py", "")
        write(root, "checks/suites.json", json.dumps(FIXTURE_SUITES, indent=2) + "\n")

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


if __name__ == "__main__":
    unittest.main()
