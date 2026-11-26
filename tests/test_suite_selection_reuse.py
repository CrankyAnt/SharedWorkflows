"""Reuse keys, default-branch records, shared CI digests and dispatch commits of select."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import subprocess
import sys
import unittest
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager, redirect_stdout
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import ModuleType
from unittest import mock

from tests import temporary_support
from tests.suite_selection_fixtures import git, write
from tests.suite_selection_reuse_support import (
    SHARED_FOLDER,
    FixtureRepository,
    reuse,
    suite_selection,
)

SHADOW_MODULES = (
    "ci_definition",
    "dependabot_coverage",
    "hygiene",
    "link_check",
    "plan",
    "promote_v1",
    "recording",
    "recording_modules",
    "reuse",
    "revisions",
    "suite_list",
    "suite_selection",
    "unrecorded",
)


@contextmanager
def isolated_imports(search_paths: Sequence[Path]) -> Iterator[Callable[[Path, str | None], ModuleType]]:
    """Temporarily prepend ``search_paths`` to ``sys.path`` and isolate ``sys.modules`` for loaded shared scripts."""
    resolved_roots = tuple(path.resolve() for path in search_paths)
    saved_path = list(sys.path)
    saved_modules = {name: sys.modules.pop(name) for name in SHADOW_MODULES if name in sys.modules}
    loaded_names: list[str] = []
    try:
        sys.path[:0] = [str(path) for path in search_paths]

        def load(script_path: Path, name: str | None = None) -> ModuleType:
            target_name = name or f"_isolated_{script_path.stem}"
            spec = importlib.util.spec_from_file_location(target_name, script_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot load module from {script_path}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[target_name] = module
            loaded_names.append(target_name)
            spec.loader.exec_module(module)
            return module

        yield load
    finally:
        for name, module in list(sys.modules.items()):
            if name in loaded_names or name in SHADOW_MODULES:
                sys.modules.pop(name, None)
                continue
            module_file = getattr(module, "__file__", None)
            if module_file:
                resolved = Path(module_file).resolve()
                if any(resolved.is_relative_to(root) for root in resolved_roots):
                    sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path


class DefaultBranchRecordTests(FixtureRepository):
    """Passed default-branch runs store a structured record that pull requests restore for exact-key takeover."""

    LABELS = ("--runner-label", "linux=ubuntu-26.04", "--runner-label", "windows=windows-2025", "--setup-version", "python=3.13")
    DIGESTS = {"language:python": "3" * 64, "platform:linux": "a" * 64, "platform:windows": "1" * 64, "tool:gitleaks": "2" * 64}
    ALL_PASSED = [
        ("pr-only", "tests/test_pr.py"),
        ("both", "tests/test_both.py"),
        ("main-only", "tests/test_main.py"),
        ("per-entry", "tests/test_one.py"),
        ("per-entry", "tests/test_two.py"),
        ("windows", "tests/test_win.py"),
    ]

    def setUp(self) -> None:
        super().setUp()
        self.default_record = self.root.parent / "default-record.json"
        self.pr_record = self.root.parent / "pr-record.json"

    def write_shared_lookup(self, digests: dict[str, str], previous: dict[str, str] | None = None, run: int | None = None) -> Path:
        path = self.root.parent / "shared-lookup.json"
        payload = {"digests": digests, "previous": previous, "reason": "fixture", "run": run}
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def store_default(self, outputs: dict[str, str], passed: list[tuple[str, str]], run: int, commit: str,
                      digests: dict[str, str] | None = None, *, extra_jobs: dict[str, dict] | None = None) -> bool:
        needs = self.root.parent / "default-needs.json"
        jobs = {"plan": {"result": "success", "outputs": outputs}, "suite-selection-jobs": {"result": "success", "outputs": {}}}
        if extra_jobs:
            jobs.update(extra_jobs)
        needs.write_text(json.dumps(jobs), encoding="utf-8")
        directory = self.root.parent / f"default-passed-{run}"
        if directory.exists():
            temporary_support.remove_tree(directory)
        (directory / "suite-results-shard-1").mkdir(parents=True)
        (directory / "suite-results-shard-1" / "passed.json").write_text(
            json.dumps([{"suite": s, "entry": e} for s, e in passed]), encoding="utf-8")
        tree = git(self.root, "rev-parse", f"{commit}^{{tree}}")
        github_output = self.root.parent / "default-store-output.txt"
        github_output.unlink(missing_ok=True)
        digests_arg = json.dumps(digests, sort_keys=True, separators=(",", ":")) if digests is not None else ""
        with redirect_stdout(io.StringIO()):
            code = suite_selection.main([
                "store", "--default-branch", "--needs", str(needs), "--selection-job", "plan",
                "--passed", str(directory), "--run-id", str(run), "--commit", commit, "--tree", tree,
                "--digests", digests_arg, "--output", str(self.default_record), "--github-output", str(github_output),
            ])
        self.assertEqual(code, 0)
        return github_output.read_text(encoding="utf-8") == "stored=true\n"

    def test_a_passed_default_branch_run_stores_passed_keys_tested_tree_and_digests(self) -> None:
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps({"case": "own", "head": self.base, "landing_base": None, "reason": "first push"}), encoding="utf-8")
        shared = self.write_shared_lookup(self.DIGESTS, previous=None)
        outputs, _, _ = self.select("--lookup", str(lookup), "--shared-lookup", str(shared),
                                    "--default-record", str(self.default_record), *self.LABELS)
        self.assertTrue(self.store_default(outputs, self.ALL_PASSED, 7001, self.base, self.DIGESTS))
        stored = json.loads(self.default_record.read_text(encoding="utf-8"))
        self.assertEqual(1, stored["version"])
        self.assertEqual(7001, stored["run_id"])
        self.assertEqual(self.base, stored["commit"])
        self.assertEqual(git(self.root, "rev-parse", f"{self.base}^{{tree}}"), stored["tree"])
        self.assertEqual(self.DIGESTS, stored["digests"])
        self.assertEqual(6, len(stored["passed"]))
        self.assertEqual({7001}, set(stored["passed"].values()))

    def test_a_cancelled_or_failed_run_stores_nothing(self) -> None:
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps({"case": "own", "head": self.base, "landing_base": None, "reason": "first push"}), encoding="utf-8")
        shared = self.write_shared_lookup(self.DIGESTS, previous=None)
        outputs, _, _ = self.select("--lookup", str(lookup), "--shared-lookup", str(shared),
                                    "--default-record", str(self.default_record), *self.LABELS)
        for bad_result in ("cancelled", "failure", "timed_out"):
            with self.subTest(default_branch_job_result=bad_result):
                self.default_record.unlink(missing_ok=True)
                self.assertFalse(self.store_default(outputs, self.ALL_PASSED, 7002, self.base, self.DIGESTS,
                                                    extra_jobs={"repository-hygiene": {"result": bad_result, "outputs": {}}}))
                self.assertFalse(self.default_record.exists())
        # A failed plan job still writes no default-branch record, even with a written plan and successful selection.
        self.default_record.unlink(missing_ok=True)
        self.assertFalse(self.store_default(
            outputs, self.ALL_PASSED, 7004, self.base, self.DIGESTS,
            extra_jobs={"plan": {"result": "failure", "outputs": {**outputs, "plan": '{"parts":{}}', "selection": "success"}}},
        ))
        self.assertFalse(self.default_record.exists())
        # A cancelled pull-request run also stores nothing.
        needs = self.root.parent / "cancelled-pr-needs.json"
        needs.write_text(json.dumps({
            "plan": {"result": "success", "outputs": outputs},
            "suite-selection-jobs": {"result": "cancelled", "outputs": {}},
        }), encoding="utf-8")
        directory = self.root.parent / "cancelled-pr-passed"
        (directory / "suite-results-shard-1").mkdir(parents=True, exist_ok=True)
        (directory / "suite-results-shard-1" / "passed.json").write_text(
            json.dumps([{"suite": "pr-only", "entry": "tests/test_pr.py"}]), encoding="utf-8")
        self.pr_record.unlink(missing_ok=True)
        github_output = self.root.parent / "cancelled-pr-store-output.txt"
        with redirect_stdout(io.StringIO()):
            code = suite_selection.main([
                "store", "--needs", str(needs), "--selection-job", "plan", "--passed", str(directory),
                "--run-id", "7003", "--output", str(self.pr_record), "--github-output", str(github_output),
            ])
        self.assertEqual(0, code)
        self.assertEqual("stored=false\n", github_output.read_text(encoding="utf-8"))
        self.assertFalse(self.pr_record.exists())

    def test_a_pull_request_takes_over_from_the_default_branch_record_only_when_inputs_are_identical(self) -> None:
        # Default branch passes at base under DIGESTS.
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps({"case": "own", "head": self.base, "landing_base": None, "reason": "first push"}), encoding="utf-8")
        shared_first = self.write_shared_lookup(self.DIGESTS, previous=None)
        outputs, _, _ = self.select("--lookup", str(lookup), "--shared-lookup", str(shared_first),
                                    "--default-record", str(self.default_record), *self.LABELS)
        self.assertTrue(self.store_default(outputs, self.ALL_PASSED, 7010, self.base, self.DIGESTS))

        # A PR edits src/feature.py, then reverts it in a second commit on the branch? Even simpler: PR changes
        # an unread file and forces selection via a shared digest move on a different platform, OR PR changes
        # src/feature.py (different inputs -> runs) while a suite whose inputs match main's record is taken over.
        # Here we force every suite to be considered (via previous=None in shared-lookup, simulating a PR that
        # selected suites whose inputs match main's record) and change src/feature.py on the PR:
        # main-only and per-entry:test_two.py have identical inputs to main (taken over from run 7010),
        # while pr-only, both, per-entry:test_one.py, and windows changed src/feature.py (must run!).
        pr_head = self.commit("pr change", {"src/feature.py": "feature v2\n"})
        shared_missing = self.write_shared_lookup(self.DIGESTS, previous=None)
        pr_outputs, printed, _ = self.select(
            "--base", self.base, "--head", pr_head, "--compare", "merge-base",
            "--shared-lookup", str(shared_missing), "--results", str(self.pr_record),
            "--default-record", str(self.default_record), *self.LABELS,
        )
        self.assertEqual({"pr-only": "run", "both": "run", "main-only": "skip", "per-entry": "run", "windows": "run"},
                         json.loads(pr_outputs["suites"]), printed)
        self.assertEqual(["tests/test_one.py"], json.loads(pr_outputs["entries"])["per-entry"])
        self.assertEqual({7010}, set(json.loads(pr_outputs["reused"]).values()))
        self.assertEqual(2, len(json.loads(pr_outputs["reused"])), "only main-only and per-entry:test_two.py match main's record")
        self.assertIn("main-only: skip (reuses the passed result of run 7010)", printed)

        # Changing the digest or runner label invalidates takeover even for main-only.
        moved = dict(self.DIGESTS, **{"platform:linux": "f" * 64})
        shared_moved = self.write_shared_lookup(moved, previous=self.DIGESTS, run=7010)
        moved_outputs, _, _ = self.select(
            "--base", self.base, "--head", pr_head, "--compare", "merge-base",
            "--shared-lookup", str(shared_moved), "--results", str(self.pr_record),
            "--default-record", str(self.default_record), *self.LABELS,
        )
        self.assertEqual("run", json.loads(moved_outputs["suites"])["main-only"])
        self.assertEqual({}, json.loads(moved_outputs["reused"]))

        # A manual re-run (--run-attempt 2) takes over nothing from the default-branch record.
        rerun_outputs, _, _ = self.select(
            "--base", self.base, "--head", pr_head, "--compare", "merge-base",
            "--shared-lookup", str(shared_missing), "--results", str(self.pr_record),
            "--default-record", str(self.default_record), "--run-attempt", "2", *self.LABELS,
        )
        self.assertEqual("run", json.loads(rerun_outputs["suites"])["main-only"])
        self.assertEqual({}, json.loads(rerun_outputs["reused"]))


class WholeSuiteKeyTests(unittest.TestCase):
    """The reuse key of a suite selected as a whole holds each entry file with its content."""

    SUITES = {"always": [], "suites": [{"name": "unit", "entries": ["tests/test_*.py"], "paths": ["lib/a.py", "tests/test_a.py"]}]}

    def key(self, files: dict[str, str]) -> str:
        suite_list = suite_selection.resolve_entries(suite_selection.parse_suite_list(json.dumps(self.SUITES)), set(files))
        units = suite_selection.reuse_units(suite_selection.every_suite(suite_list, "test"), suite_list, reuse.Tree(files, suite_selection.matches),
                                            {}, {}, ignore_content={})
        self.assertEqual([unit.entries for unit in units], [tuple(sorted(file for file in files if file.startswith("tests/")))])
        return units[0].key

    def test_the_key_changes_with_the_content_of_an_entry_file_that_the_stored_lists_do_not_match(self) -> None:
        files = {"lib/a.py": "a", "tests/test_a.py": "a", "tests/test_new.py": "new", "stray.txt": "stray"}
        key = self.key(files)
        self.assertNotEqual(self.key(files | {"tests/test_new.py": "changed"}), key)
        self.assertNotEqual(self.key({path: content for path, content in files.items() if path != "tests/test_new.py"}), key,
                            "a tree without the entry file has another key")
        self.assertEqual(self.key(files | {"stray.txt": "changed"}), key, "a file that is no read and no entry file leaves the key")


class SharedDigestTests(unittest.TestCase):
    """The digest of the shared recording step, taken from a copy of the shared CI's part folders as a job fetches them."""

    def setUp(self) -> None:
        self.temporary = temporary_support.Directory()
        self.addCleanup(self.temporary.cleanup)
        self.shared_folder = Path(self.temporary.name) / "shared-ci"
        for part in ("recording", "suite-selection", "python-language-step", "repository-hygiene"):
            shutil.copytree(SHARED_FOLDER / part, self.shared_folder / part, ignore=shutil.ignore_patterns("__pycache__"))
        with isolated_imports((self.shared_folder / "suite-selection", self.shared_folder / "recording")) as load:
            self.recording = load(self.shared_folder / "recording" / "recording.py", "recording")

    def digest(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(self.shared_folder / "recording" / "recording.py"), "digest", *arguments],
                              capture_output=True, text=True)

    def run_digest_main(self, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), mock.patch.object(sys, "stderr", stderr):
            code = self.recording.main(["digest", *arguments])
        return code, stdout.getvalue(), stderr.getvalue()

    def taken(self) -> dict[str, str]:
        digests = self.recording.shared_digests()
        self.assertIsInstance(digests, dict)
        for name, value in digests.items():
            self.assertRegex(value, r"^[0-9a-f]{64}$", f"{name} must be a SHA-256 hex digest")
        return digests

    def test_the_digests_separate_platforms_tools_language_steps_and_exclude_selection_code(self) -> None:
        first = self.taken()
        self.assertEqual(first, self.taken(), "the same files give the same digests")
        completed = self.digest()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        (raw,) = re.findall(r"^shared CI digests (\{[^\r\n]+\})$", completed.stdout, re.MULTILINE)
        self.assertEqual(first, json.loads(raw))
        self.assertLessEqual({"platform:linux", "platform:windows", "tool:node", "tool:gitleaks", "language:python"}, set(first))

        # Windows-only recording files change only platform:windows.
        for changed in ("recording/etw-consumer/Program.cs", "recording/etw-consumer/EtwConsumer.csproj",
                        "recording/etw-consumer/global.json", "recording/etw-consumer/packages.lock.json",
                        "recording/etw-consumer/build/EtwConsumer.dll", "recording/etw_consumer_build.py"):
            with self.subTest(windows_only=changed):
                path = self.shared_folder / changed
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                after = self.taken()
                path.write_bytes(original)
                self.assertNotEqual(first["platform:windows"], after["platform:windows"])
                self.assertEqual({k: v for k, v in first.items() if k != "platform:windows"},
                                 {k: v for k, v in after.items() if k != "platform:windows"})

        # Common recording files change both platform:linux and platform:windows, and no tool or language-step digest.
        for changed in ("recording/action.yml", "recording/recording.py", "suite-selection/suite_list.py",
                        "suite-selection/recording_modules.py"):
            with self.subTest(common_recording=changed):
                path = self.shared_folder / changed
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                after = self.taken()
                path.write_bytes(original)
                self.assertNotEqual(first["platform:linux"], after["platform:linux"])
                self.assertNotEqual(first["platform:windows"], after["platform:windows"])
                self.assertEqual({k: v for k, v in first.items() if not k.startswith("platform:")},
                                 {k: v for k, v in after.items() if not k.startswith("platform:")})

        # Selection code changes no digest at all.
        for changed in ("suite-selection/suite_selection.py", "suite-selection/ci_definition.py",
                        "suite-selection/revisions.py", "suite-selection/reuse.py", "suite-selection/unrecorded.py",
                        "suite-selection/action.yml"):
            with self.subTest(selection_or_unrelated=changed):
                path = self.shared_folder / changed
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                after = self.taken()
                path.write_bytes(original)
                self.assertEqual(first, after)

        # A change to the Python language step changes only language:python.
        for changed in ("python-language-step/python_language_step.py", "python-language-step/action.yml"):
            with self.subTest(language_step=changed):
                path = self.shared_folder / changed
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                after = self.taken()
                path.write_bytes(original)
                self.assertNotEqual(first["language:python"], after["language:python"])
                self.assertEqual({k: v for k, v in first.items() if k != "language:python"},
                                 {k: v for k, v in after.items() if k != "language:python"})

        # Changing one pinned tool in pins.json changes only that tool's digest (and language:python for ruff/mypy).
        pins_path = self.shared_folder / "repository-hygiene" / "pins.json"
        original_pins = pins_path.read_text(encoding="utf-8")
        pins = json.loads(original_pins)
        pins["tools"]["gitleaks"]["version"] = "9.9.9"
        pins_path.write_text(json.dumps(pins), encoding="utf-8")
        after_gitleaks = self.taken()
        self.assertNotEqual(first["tool:gitleaks"], after_gitleaks["tool:gitleaks"])
        self.assertEqual({k: v for k, v in first.items() if k != "tool:gitleaks"},
                         {k: v for k, v in after_gitleaks.items() if k != "tool:gitleaks"})

        pins = json.loads(original_pins)
        pins["node"]["version"] = "99.0.0"
        pins_path.write_text(json.dumps(pins), encoding="utf-8")
        after_node = self.taken()
        self.assertNotEqual(first["tool:node"], after_node["tool:node"])
        self.assertEqual({k: v for k, v in first.items() if k != "tool:node"},
                         {k: v for k, v in after_node.items() if k != "tool:node"})

        pins = json.loads(original_pins)
        pins["python"]["ruff"]["version"] = "9.9.9"
        pins_path.write_text(json.dumps(pins), encoding="utf-8")
        after_ruff = self.taken()
        self.assertNotEqual(first["tool:ruff"], after_ruff["tool:ruff"])
        self.assertNotEqual(first["language:python"], after_ruff["language:python"])
        self.assertEqual({k: v for k, v in first.items() if k not in ("tool:ruff", "language:python")},
                         {k: v for k, v in after_ruff.items() if k not in ("tool:ruff", "language:python")})
        pins_path.write_text(original_pins, encoding="utf-8")

        # New file in recording/ enters both platform digests; __pycache__ is ignored.
        write(self.shared_folder, "recording/toolchain-rules.json", "{}\n")
        added = self.taken()
        self.assertNotEqual(first["platform:linux"], added["platform:linux"])
        self.assertNotEqual(first["platform:windows"], added["platform:windows"])
        write(self.shared_folder, "recording/__pycache__/recording.cpython-313.pyc", "cache\n")
        self.assertEqual(added, self.taken(), "a bytecode cache is not part of any digest")

    def test_the_digest_follows_the_shared_workflow_of_the_suite_jobs(self) -> None:
        without = self.taken()
        workflow = self.shared_folder.parent / ".github" / "workflows" / "suite-jobs.yml"
        write(workflow.parent, workflow.name, "name: Suite jobs\n")
        first = self.taken()
        self.assertNotEqual(without["platform:linux"], first["platform:linux"], "the workflow that runs a consumer's entry files is part of the platform digests")
        self.assertNotEqual(without["platform:windows"], first["platform:windows"])
        write(workflow.parent, workflow.name, "name: Suite jobs\njobs: {}\n")
        after = self.taken()
        self.assertNotEqual(first["platform:linux"], after["platform:linux"])
        self.assertNotEqual(first["platform:windows"], after["platform:windows"])
        write(workflow.parent, "other.yml", "name: Other\n")
        write(workflow.parent, workflow.name, "name: Suite jobs\n")
        self.assertEqual(first, self.taken(), "another workflow is not part of it")

    PIPELINE = ("name: Pipeline\non: workflow_call\njobs:\n  plan:\n    runs-on: ubuntu-26.04\n    steps:\n"
                "      - name: Dependabot coverage of tracked package files\n        id: coverage\n        run: coverage\n"
                "      - name: Relative links in tracked Markdown\n        id: links\n        run: links\n\n"
                "  custom:\n    needs: plan\n    steps:\n      - run: bash -- \"$SCRIPT\"\n\n"
                "  dotnet-test:\n    needs: plan\n    steps:\n      - uses: $/actions/dotnet-test\n\n"
                "  node-test:\n    needs: plan\n    steps:\n      - uses: $/actions/node-test\n\n"
                "  python-test:\n    needs: plan\n    steps:\n      - uses: $/actions/python-tests\n\n"
                "  python-verify:\n    needs: plan\n    steps:\n      - uses: $/actions/python-tests\n\n"
                "  repository-hygiene:\n    needs: plan\n    steps:\n      - run: hygiene\n")
    PARTS = ("part:custom", "part:dotnet-test", "part:node-test", "part:python-test", "part:python-verify")
    HYGIENE = ("hygiene:actionlint", "hygiene:markdown", "hygiene:packages", "hygiene:reuse", "hygiene:zizmor", "hygiene:zizmor-policy")

    def assert_moves_only(self, before: dict[str, str], after: dict[str, str], moved: set[str]) -> None:
        self.assertEqual(moved, {name for name in set(before) | set(after) if before.get(name) != after.get(name)})

    def test_part_digests_track_each_pipeline_part_s_shared_code_independently(self) -> None:
        for part in ("pipeline", "dotnet-test", "node-test", "python-tests"):
            shutil.copytree(SHARED_FOLDER / part, self.shared_folder / part, ignore=shutil.ignore_patterns("__pycache__"))
        workflow = self.shared_folder.parent / ".github" / "workflows" / "pipeline.yml"
        write(workflow.parent, workflow.name, self.PIPELINE)
        first = self.taken()
        for key in self.PARTS:
            self.assertIn(key, first)
        # Changing dotnet-test changes only part:dotnet-test.
        dotnet_script = self.shared_folder / "dotnet-test" / "dotnet_test.py"
        original = dotnet_script.read_bytes()
        dotnet_script.write_bytes(original + b"\n")
        self.assert_moves_only(first, self.taken(), {"part:dotnet-test"})
        dotnet_script.write_bytes(original)
        # Changing the job of one part in pipeline.yml changes only that part's digest.
        write(workflow.parent, workflow.name, self.PIPELINE.replace("uses: $/actions/node-test", "uses: $/actions/node-test\n        with: {a: b}"))
        self.assert_moves_only(first, self.taken(), {"part:node-test"})
        # Changing another job, or the blank lines between jobs, changes no part digest.
        write(workflow.parent, workflow.name, self.PIPELINE.replace("runs-on: ubuntu-26.04\n", "runs-on: ubuntu-26.04\n    timeout-minutes: 5\n\n\n"))
        self.assert_moves_only(first, self.taken(), set())
        # Changing the lines before jobs: changes every part and pipeline-backed hygiene digest and no platform, tool or language digest.
        write(workflow.parent, workflow.name, self.PIPELINE.replace("name: Pipeline\n", "name: Pipeline v2\n"))
        self.assert_moves_only(first, self.taken(), {*self.PARTS, *(k for k in self.HYGIENE if k != "hygiene:zizmor-policy")})
        write(workflow.parent, workflow.name, self.PIPELINE)

    def test_hygiene_digests_track_each_hygiene_check_s_scripts_pins_and_pipeline_steps(self) -> None:
        for part in ("pipeline", "dotnet-test", "node-test", "python-tests"):
            shutil.copytree(SHARED_FOLDER / part, self.shared_folder / part, ignore=shutil.ignore_patterns("__pycache__"))
        workflow = self.shared_folder.parent / ".github" / "workflows" / "pipeline.yml"
        write(workflow.parent, workflow.name, self.PIPELINE)
        first = self.taken()
        for key in self.HYGIENE:
            self.assertIn(key, first)
        # Check-specific scripts move only their own digest.
        for relative, expected in (
            ("repository-hygiene/dependabot_coverage.py", {"hygiene:packages"}),
            ("repository-hygiene/package-ecosystems.json", {"hygiene:packages"}),
            ("repository-hygiene/link_check.py", {"hygiene:markdown"}),
            ("repository-hygiene/zizmor-policy.yml", {"hygiene:zizmor-policy"}),
            ("repository-hygiene/hygiene.py", {"hygiene:actionlint", "hygiene:zizmor", "hygiene:packages", "hygiene:markdown", "hygiene:reuse"}),
            ("repository-hygiene/promote_v1.py", set()),
        ):
            with self.subTest(file=relative):
                path = self.shared_folder / relative
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                self.assert_moves_only(first, self.taken(), expected)
                path.write_bytes(original)
        # Tool pins move only the checks that use them (including ShellCheck for actionlint).
        pins_path = self.shared_folder / "repository-hygiene" / "pins.json"
        original_pins = pins_path.read_bytes()
        for section, tool, expected in (
            ("tools", "actionlint", {"tool:actionlint", "hygiene:actionlint"}),
            ("tools", "shellcheck", {"tool:shellcheck", "hygiene:actionlint"}),
            ("tools", "zizmor", {"tool:zizmor", "hygiene:zizmor"}),
            ("python", "reuse", {"tool:reuse", "hygiene:reuse"}),
        ):
            with self.subTest(pin=f"{section}/{tool}"):
                pins = json.loads(original_pins.decode("utf-8"))
                pins[section][tool]["version"] = "9.9.9"
                pins_path.write_bytes(json.dumps(pins).encode("utf-8"))
                self.assert_moves_only(first, self.taken(), expected)
                pins_path.write_bytes(original_pins)
        # Pipeline workflow job / light-check step changes move only the checks they run.
        write(workflow.parent, workflow.name, self.PIPELINE.replace("run: hygiene\n", "run: hygiene --strict\n"))
        self.assert_moves_only(first, self.taken(), {"hygiene:actionlint", "hygiene:zizmor", "hygiene:reuse"})
        write(workflow.parent, workflow.name, self.PIPELINE.replace("run: coverage\n", "run: coverage --all\n"))
        self.assert_moves_only(first, self.taken(), {"hygiene:packages"})
        write(workflow.parent, workflow.name, self.PIPELINE.replace("run: links\n", "run: links --all\n"))
        self.assert_moves_only(first, self.taken(), {"hygiene:markdown"})
        write(workflow.parent, workflow.name, self.PIPELINE)

    def test_a_part_job_that_the_pipeline_workflow_does_not_hold_as_expected_fails_the_digest(self) -> None:
        for part in ("pipeline", "dotnet-test", "node-test", "python-tests"):
            shutil.copytree(SHARED_FOLDER / part, self.shared_folder / part, ignore=shutil.ignore_patterns("__pycache__"))
        workflow = self.shared_folder.parent / ".github" / "workflows" / "pipeline.yml"
        for name, text in (("missing", self.PIPELINE.replace("  node-test:\n", "  node-tests:\n")),
                           ("indented", self.PIPELINE.replace("  node-test:\n", "    node-test:\n"))):
            with self.subTest(case=name):
                write(workflow.parent, workflow.name, text)
                code, stdout, stderr = self.run_digest_main()
                self.assertNotEqual(0, code)
                self.assertIn("the job of the part node-test cannot be found in ../.github/workflows/pipeline.yml", stdout + stderr)

    def test_a_part_digest_fails_loudly_when_its_part_folder_or_pipeline_workflow_is_missing(self) -> None:
        for part in ("pipeline", "dotnet-test", "node-test", "python-tests"):
            shutil.copytree(SHARED_FOLDER / part, self.shared_folder / part, ignore=shutil.ignore_patterns("__pycache__"))
        workflow = self.shared_folder.parent / ".github" / "workflows" / "pipeline.yml"
        write(workflow.parent, workflow.name, self.PIPELINE)
        # Missing part folder while pipeline.yml is present fails loudly.
        temporary_support.remove_tree(self.shared_folder / "dotnet-test")
        code, stdout, stderr = self.run_digest_main()
        self.assertNotEqual(0, code)
        self.assertIn("dotnet-test", stdout + stderr)
        shutil.copytree(SHARED_FOLDER / "dotnet-test", self.shared_folder / "dotnet-test", ignore=shutil.ignore_patterns("__pycache__"))
        # Missing pipeline.yml while part folders are present fails loudly.
        workflow.unlink()
        code, stdout, stderr = self.run_digest_main()
        self.assertNotEqual(0, code)
        self.assertIn("pipeline.yml", stdout + stderr)
        write(workflow.parent, workflow.name, self.PIPELINE)
        # Missing or unreadable timeouts.json while part digests are taken fails loudly.
        timeouts_path = self.shared_folder / "pipeline" / "timeouts.json"
        original_timeouts = timeouts_path.read_bytes()
        for label, action in (
            ("missing", lambda: timeouts_path.unlink()),
            ("invalid json", lambda: timeouts_path.write_text("{bad", encoding="utf-8")),
            ("wrong type", lambda: timeouts_path.write_text('{"dotnet-test": "15"}\n', encoding="utf-8")),
        ):
            with self.subTest(timeouts=label):
                action()
                code, stdout, stderr = self.run_digest_main()
                self.assertNotEqual(0, code)
                self.assertIn("timeouts.json", stdout + stderr)
                timeouts_path.write_bytes(original_timeouts)

    def test_part_digests_include_time_limits_and_custom_setups(self) -> None:
        for part in ("pipeline", "dotnet-test", "node-test", "python-tests"):
            shutil.copytree(SHARED_FOLDER / part, self.shared_folder / part, ignore=shutil.ignore_patterns("__pycache__"))
        workflow = self.shared_folder.parent / ".github" / "workflows" / "pipeline.yml"
        write(workflow.parent, workflow.name, self.PIPELINE)
        first = self.taken()
        # Changing setups.json moves only part:custom.
        setups_path = self.shared_folder / "pipeline" / "schema" / "v1" / "setups.json"
        original_setups = setups_path.read_bytes()
        setups_doc = json.loads(original_setups.decode("utf-8"))
        setups_doc["python-3.14"]["version"] = "3.14.1"
        setups_path.write_bytes(json.dumps(setups_doc).encode("utf-8"))
        self.assert_moves_only(first, self.taken(), {"part:custom"})
        setups_path.write_bytes(original_setups)
        # Changing one part's timeout in timeouts.json moves only that part's digest.
        timeouts_path = self.shared_folder / "pipeline" / "timeouts.json"
        original_timeouts = timeouts_path.read_bytes()
        timeouts_doc = json.loads(original_timeouts.decode("utf-8"))
        timeouts_doc["dotnet-test"] = 8
        timeouts_path.write_bytes(json.dumps(timeouts_doc).encode("utf-8"))
        self.assert_moves_only(first, self.taken(), {"part:dotnet-test"})
        # Changing plan or verdict timeout moves no part digest.
        timeouts_doc = json.loads(original_timeouts.decode("utf-8"))
        timeouts_doc["plan"] = 4
        timeouts_doc["verdict"] = 4
        timeouts_path.write_bytes(json.dumps(timeouts_doc).encode("utf-8"))
        self.assert_moves_only(first, self.taken(), set())
        timeouts_path.write_bytes(original_timeouts)

    def test_part_digests_leave_out_what_no_part_job_runs(self) -> None:
        for part in ("pipeline", "dotnet-test", "node-test", "python-tests"):
            shutil.copytree(SHARED_FOLDER / part, self.shared_folder / part, ignore=shutil.ignore_patterns("__pycache__"))
        workflow = self.shared_folder.parent / ".github" / "workflows" / "pipeline.yml"
        write(workflow.parent, workflow.name, self.PIPELINE)
        first = self.taken()
        for relative in ("pipeline/consumer-kit/README.md", "pipeline/schema/README.md", "pipeline/release.py", "pipeline/plan.py",
                         "dotnet-test/consumer-kit/README.md",
                         "python-language-step/consumer-kit/README.md", "python-language-step/consumer-kit/python-language-step.yml"):
            with self.subTest(file=relative):
                path = self.shared_folder / relative
                original = path.read_bytes() if path.is_file() else None
                write(self.shared_folder, relative, "changed\n")
                self.assert_moves_only(first, self.taken(), set())
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)

    def test_the_digest_is_the_same_whichever_order_a_platform_sorts_the_shared_files_in(self) -> None:
        # Windows orders paths case-insensitively and Linux by code point, so the same revision lists its shared
        # files in two orders: recording/etw-consumer/EtwConsumer.csproj comes after build/ on Windows, before it on Linux.
        files = self.recording.platform_files("windows")
        orders = []
        digests = []
        for flavour in (PurePosixPath, PureWindowsPath):
            order = sorted(files, key=lambda path: flavour(self.recording.shared_name(path)))
            orders.append(" ".join(self.recording.shared_name(path) for path in order))
            digests.append(self.recording.files_digest(order))
        linux_order, windows_order = orders
        linux_digest, windows_digest = digests
        self.assertNotEqual(linux_order, windows_order, "the shared files sort differently on the two platforms")
        self.assertEqual(linux_digest, windows_digest)
        self.assertEqual(self.taken()["platform:windows"], linux_digest)

    def test_expect_fails_on_a_digest_that_differs_from_the_one_select_took(self) -> None:
        digests = self.taken()
        expected = json.dumps(digests, sort_keys=True, separators=(",", ":"))
        output = Path(self.temporary.name) / "output.txt"
        self.assertEqual(self.digest("--expect", expected, "--github-output", str(output)).returncode, 0)
        self.assertEqual(f"shared-digests={expected}\n", output.read_text(encoding="utf-8"))
        moved_map = dict(digests, **{"platform:linux": "0" * 64})
        moved = self.digest("--expect", json.dumps(moved_map, sort_keys=True, separators=(",", ":")))
        self.assertEqual(moved.returncode, 1)
        self.assertIn("platform:linux", moved.stderr)
        self.assertIn("v1 moved during the run", moved.stderr)
        bad = self.digest("--expect", "not-json")
        self.assertEqual(bad.returncode, 1)


class SharedDigestScopeTests(unittest.TestCase):
    """The digests that recording.py takes in a job and those that promote_v1.py takes from a commit name the same
    digests over the same files, on a committed copy of the shared CI's part folders."""

    PARTS = ("recording", "suite-selection", "python-language-step", "repository-hygiene", "pipeline", "dotnet-test", "node-test", "python-tests")

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._template_temporary = temporary_support.Directory()
        cls.addClassCleanup(cls._template_temporary.cleanup)
        cls._template_repo = Path(cls._template_temporary.name) / "repo"
        for part in cls.PARTS:
            shutil.copytree(SHARED_FOLDER / part, cls._template_repo / "actions" / part, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("pipeline.yml", "suite-jobs.yml"):
            target = cls._template_repo / ".github" / "workflows" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SHARED_FOLDER.parent / ".github" / "workflows" / name, target)
        git(cls._template_repo, "init", "-q")
        git(cls._template_repo, "add", "-A", "-f", ".")
        git(cls._template_repo, "commit", "-q", "--allow-empty", "-m", "fixture")

    def setUp(self) -> None:
        self.temporary = temporary_support.Directory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        shutil.copytree(self._template_repo, self.repo)

    def _load_recording_and_promote(self, repo: Path):
        actions = repo / "actions"
        with isolated_imports((actions / "recording", actions / "suite-selection", actions / "repository-hygiene")) as load:
            recording_mod = load(actions / "recording" / "recording.py", "recording")
            promote_mod = load(actions / "repository-hygiene" / "promote_v1.py", "promote_v1")
        return recording_mod, promote_mod

    def _load_promote(self, repo: Path):
        hygiene_dir = repo / "actions" / "repository-hygiene"
        with isolated_imports((hygiene_dir,)) as load:
            return load(hygiene_dir / "promote_v1.py", "promote_v1")

    def scopes(self) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """The scopes of recording.py on the copy's files and of promote_v1.py on its commit, both by repository path."""
        if git(self.repo, "status", "--porcelain"):
            git(self.repo, "add", "-A", "-f", ".")
            git(self.repo, "commit", "-q", "--allow-empty", "-m", "fixture")
        recording_mod, promote_mod = self._load_recording_and_promote(self.repo)

        def by_path(name: str) -> str:
            return name[3:] if name.startswith("../") else name if name.startswith("pins:") else "actions/" + name

        recorded = {name: sorted(by_path(entry) for entry in entries) for name, entries in recording_mod.shared_digest_scopes().items()}
        promoted = promote_mod.commit_digest_scopes(self.repo, "HEAD")
        return recorded, promoted

    def test_both_take_the_same_digests_over_the_same_files(self) -> None:
        recorded, promoted = self.scopes()
        self.assertEqual(sorted(recorded), sorted(promoted))
        for name in recorded:
            with self.subTest(digest=name):
                self.assertEqual(recorded[name], promoted[name])
        for name in ("platform:linux", "platform:windows", "tool:gitleaks", "language:python", "part:custom", "part:dotnet-test",
                     "hygiene:actionlint", "hygiene:zizmor", "hygiene:zizmor-policy", "hygiene:packages", "hygiene:markdown", "hygiene:reuse"):
            self.assertIn(name, recorded)
        self.assertFalse([entry for entries in recorded.values() for entry in entries if "/consumer-kit/" in entry],
                         "no digest covers a consumer kit, which no job runs")

    def test_a_consumer_kit_file_moves_no_digest_of_a_commit(self) -> None:
        before = git(self.repo, "rev-parse", "HEAD").strip()
        for relative in ("actions/python-language-step/consumer-kit/README.md", "actions/pipeline/consumer-kit/README.md"):
            write(self.repo, relative, "changed\n")
        git(self.repo, "add", "-A", "-f", ".")
        git(self.repo, "commit", "-q", "-m", "kit")
        promote_mod = self._load_promote(self.repo)
        self.assertEqual([], promote_mod.moved_digests(self.repo, before, "HEAD"))

    def part_job_texts(self, workflow: str, jobs: list[str]) -> tuple[dict, dict]:
        recording_mod, promote_mod = self._load_recording_and_promote(self.repo)
        return ({job: recording_mod.part_job_text(workflow, job) for job in jobs},
                {job: promote_mod.part_job_text(workflow, job) for job in jobs})

    def test_both_read_the_same_job_text_and_a_comment_inside_a_job_keeps_the_rest_of_it(self) -> None:
        pipeline = (self.repo / ".github" / "workflows" / "pipeline.yml").read_text(encoding="utf-8")
        parts = ["custom", "dotnet-test", "node-test", "python-test", "python-verify"]
        recorded, promoted = self.part_job_texts(pipeline, parts)
        for part in parts:
            with self.subTest(part=part):
                self.assertIsNotNone(recorded[part])
                self.assertEqual(recorded[part], promoted[part])
        workflow = ("name: Pipeline\njobs:\n  plan:\n    runs-on: ubuntu-26.04\n\n"
                    "  node-test:\n    needs: plan\n  # a comment at the job's indentation\n    steps:\n      - uses: $/actions/node-test\n\n"
                    "  # a comment between jobs\n\n  python-test:\n    needs: plan\n")
        recorded, promoted = self.part_job_texts(workflow, ["node-test", "python-test"])
        self.assertEqual(recorded, promoted)
        self.assertEqual("name: Pipeline\n  node-test:\n    needs: plan\n  # a comment at the job's indentation\n    steps:\n"
                         "      - uses: $/actions/node-test\n", recorded["node-test"])
        self.assertEqual("name: Pipeline\n  python-test:\n    needs: plan\n", recorded["python-test"])

    def test_promote_v1_fails_on_a_commit_whose_pipeline_workflow_does_not_hold_a_part_job_as_expected(self) -> None:
        workflow = self.repo / ".github" / "workflows" / "pipeline.yml"
        workflow.write_text(workflow.read_text(encoding="utf-8").replace("\n  dotnet-test:\n", "\n   dotnet-test:\n", 1), encoding="utf-8")
        git(self.repo, "add", "-A", "-f", ".")
        git(self.repo, "commit", "-q", "-m", "fixture")
        promote_mod = self._load_promote(self.repo)
        with self.assertRaises(promote_mod.DigestScopeError) as context:
            promote_mod.commit_digests(self.repo, "HEAD")
        self.assertIn("the job of the part dotnet-test cannot be found in .github/workflows/pipeline.yml", str(context.exception))

    def test_promote_v1_fails_loudly_when_a_part_folder_or_pipeline_workflow_is_missing(self) -> None:
        for case, remove in (("missing folder", self.repo / "actions" / "dotnet-test"),
                             ("missing workflow", self.repo / ".github" / "workflows" / "pipeline.yml"),
                             ("missing timeouts", self.repo / "actions" / "pipeline" / "timeouts.json")):
            with self.subTest(case=case):
                copy = Path(self.temporary.name) / case.replace(" ", "-")
                shutil.copytree(self.repo, copy)
                target = copy / remove.relative_to(self.repo)
                if target.is_dir():
                    temporary_support.remove_tree(target)
                else:
                    target.unlink()
                git(copy, "add", "-A", "-f", ".")
                git(copy, "commit", "-q", "-m", "fixture")
                promote_mod = self._load_promote(copy)
                with self.assertRaises(promote_mod.DigestScopeError) as context:
                    promote_mod.commit_digests(copy, "HEAD")
                expected_name = "dotnet-test" if "folder" in case else "timeouts.json" if "timeouts" in case else "pipeline.yml"
                self.assertIn(expected_name, str(context.exception))

    def test_promote_v1_fails_loudly_on_unreadable_timeouts_and_handles_historical_base_commits(self) -> None:
        # Unreadable/malformed timeouts.json fails loudly in promote_v1.commit_digests.
        for label, bad_content in (("invalid json", "{bad"), ("wrong type", '{"dotnet-test": true}\n')):
            with self.subTest(timeouts=label):
                copy = Path(self.temporary.name) / f"bad-timeouts-{label.replace(' ', '-')}"
                shutil.copytree(self.repo, copy)
                (copy / "actions" / "pipeline" / "timeouts.json").write_text(bad_content, encoding="utf-8")
                git(copy, "add", "-A", "-f", ".")
                git(copy, "commit", "-q", "-m", "fixture")
                promote_mod = self._load_promote(copy)
                with self.assertRaises(promote_mod.DigestScopeError) as context:
                    promote_mod.commit_digests(copy, "HEAD")
                self.assertIn("timeouts.json", str(context.exception))
        # Historical old_commit predating timeouts.json and plan light-check steps still compares in moved_digests.
        hist = Path(self.temporary.name) / "historical-base"
        shutil.copytree(self.repo, hist)
        timeouts_saved = (hist / "actions" / "pipeline" / "timeouts.json").read_bytes()
        workflow_saved = (hist / ".github" / "workflows" / "pipeline.yml").read_bytes()
        (hist / "actions" / "pipeline" / "timeouts.json").unlink()
        (hist / ".github" / "workflows" / "pipeline.yml").write_text(
            workflow_saved.decode("utf-8").replace("        id: coverage\n", "").replace("        id: links\n", ""),
            encoding="utf-8",
        )
        git(hist, "add", "-A", "-f", ".")
        git(hist, "commit", "-q", "-m", "historical v1")
        old_sha = git(hist, "rev-parse", "HEAD").strip()
        (hist / "actions" / "pipeline" / "timeouts.json").write_bytes(timeouts_saved)
        (hist / ".github" / "workflows" / "pipeline.yml").write_bytes(workflow_saved)
        git(hist, "add", "-A", "-f", ".")
        git(hist, "commit", "-q", "-m", "new main")
        promote_mod = self._load_promote(hist)
        moved = set(promote_mod.moved_digests(hist, old_sha, "HEAD"))
        self.assertLessEqual({"part:custom", "part:dotnet-test", "hygiene:packages", "hygiene:markdown"}, moved)

    def test_hygiene_check_digests_and_shared_digest_helpers_stay_in_step_across_part_folders(self) -> None:
        recorded, promoted = self.scopes()
        actions = self.repo / "actions"
        with isolated_imports((actions / "repository-hygiene", actions / "pipeline", actions / "suite-selection")) as load:
            hygiene_mod = load(actions / "repository-hygiene" / "hygiene.py", "hygiene")
            plan_mod = load(actions / "pipeline" / "plan.py", "plan")
            reuse_mod = load(actions / "suite-selection" / "reuse.py", "reuse")
        check_keys = sorted(hygiene_mod.CHECK_DIGESTS)
        checks = sorted(c for c in hygiene_mod.CHECKS if c != "workflows")
        digest_names = sorted({name for names in hygiene_mod.CHECK_DIGESTS.values() for name in names})
        good = {"a": "0" * 64, "b": "1" * 64}
        other = {"a": "0" * 64, "c": "2" * 64}
        for sample in (good, {}, None, "bad", {"": "0" * 64}, {"a": "not-hex"}):
            vals = [reuse_mod.parse_digests(sample), plan_mod.parse_digests(sample), hygiene_mod.parse_digests(sample)]
            self.assertEqual(vals[0], vals[1], sample)
            self.assertEqual(vals[1], vals[2], sample)
        self.assertEqual(reuse_mod.moved_digest_names(good, other), {"b", "c"})
        self.assertEqual(plan_mod.moved_digest_names(good, other), {"b", "c"})
        self.assertEqual(hygiene_mod.moved_digest_names(good, other), {"b", "c"})
        self.assertEqual(checks, check_keys)
        recorded_hygiene = sorted(name for name in recorded if name.startswith("hygiene:"))
        promoted_hygiene = sorted(name for name in promoted if name.startswith("hygiene:"))
        self.assertEqual(recorded_hygiene, digest_names)
        self.assertEqual(promoted_hygiene, digest_names)

    def test_a_folder_added_to_one_side_only_breaks_the_tie(self) -> None:
        recording_script = self.repo / "actions" / "recording" / "recording.py"
        text = recording_script.read_text(encoding="utf-8")
        recording_script.write_text(text.replace('"custom": (),', '"custom": (SHARED_FOLDER / "pipeline",),', 1), encoding="utf-8")
        recorded, promoted = self.scopes()
        self.assertNotEqual(recorded["part:custom"], promoted["part:custom"])
        promote_script = self.repo / "actions" / "repository-hygiene" / "promote_v1.py"
        text = promote_script.read_text(encoding="utf-8")
        promote_script.write_text(text.replace('LANGUAGE_STEP_FOLDERS = {"python": "actions/python-language-step"}',
                                               'LANGUAGE_STEP_FOLDERS = {"python": "actions/python-language-step", "node": "actions/node-test"}', 1),
                                  encoding="utf-8")
        recorded, promoted = self.scopes()
        self.assertIn("language:node", promoted)
        self.assertNotIn("language:node", recorded)


class DispatchCommitTests(FixtureRepository):
    """A dependabot-merged run stands in for a push on the default branch only with a payload commit on that branch."""

    def setUp(self) -> None:
        super().setUp()
        self.earlier = self.commit("earlier", {"src/feature.py": "feature v2\n"})
        self.tip = self.commit("tip", {"src/other.py": "other v2\n"})
        git(self.root, "checkout", "-q", "-b", "side", self.earlier)
        self.side = self.commit("side", {"src/moved.py": "moved v2\n"})
        git(self.root, "checkout", "-q", "main")
        git(self.root, "update-ref", "refs/remotes/origin/main", self.tip)

    def check(self, payload: str) -> tuple[int, str, str]:
        printed, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(printed), mock.patch.object(sys, "stderr", errors):
            code = suite_selection.main(["check-dispatch", "--root", str(self.root), "--sha", payload, "--branch", "main"])
        return code, printed.getvalue(), errors.getvalue()

    def test_a_payload_commit_on_the_default_branch_passes_at_its_tip_and_before_it(self) -> None:
        for commit in (self.tip, self.earlier, self.base):
            with self.subTest(commit=commit):
                code, printed, errors = self.check(commit)
                self.assertEqual((code, errors), (0, ""))
                self.assertEqual(f"client_payload.sha {commit} is on `origin/main`, the default branch.\n", printed)

    def test_a_payload_commit_off_the_default_branch_fails_with_the_commit_and_the_reason(self) -> None:
        code, printed, errors = self.check(self.side)
        self.assertEqual((code, printed), (1, ""))
        self.assertEqual(f"client_payload.sha {self.side} is not on the default branch: `origin/main` does not reach it.\n", errors)

    def test_a_malformed_payload_fails_with_the_value(self) -> None:
        for payload in (self.tip[:7], self.tip.upper(), self.tip + "0", "main", "HEAD", f"{self.tip}^", ""):
            with self.subTest(payload=payload):
                code, printed, errors = self.check(payload)
                self.assertEqual((code, printed), (1, ""))
                self.assertEqual(f"client_payload.sha '{payload}' is malformed: it is not a full 40-character commit ID in lowercase hexadecimal.\n", errors)

    def test_a_payload_absent_from_the_checkout_fails_with_the_value(self) -> None:
        tree = git(self.root, "rev-parse", f"{self.tip}^{{tree}}")
        for payload in ("0123456789abcdef0123456789abcdef01234567", tree):
            with self.subTest(payload=payload):
                code, printed, errors = self.check(payload)
                self.assertEqual((code, printed), (1, ""))
                self.assertEqual(f"client_payload.sha {payload} is absent from this job's checkout: it names no commit there.\n", errors)

    def test_a_default_branch_that_the_job_did_not_fetch_fails(self) -> None:
        git(self.root, "update-ref", "-d", "refs/remotes/origin/main")
        code, printed, errors = self.check(self.tip)
        self.assertEqual((code, printed), (1, ""))
        self.assertEqual(f"client_payload.sha {self.tip} cannot be checked: `origin/main`, the default branch, is not in this job's checkout.\n", errors)


if __name__ == "__main__":
    unittest.main()
