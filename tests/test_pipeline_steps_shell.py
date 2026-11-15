"""The shell of the pipeline's steps with logic of their own, run in Bash as `pipeline.yml` holds them, on a fixture
consumer: a temporary Git repository that holds no file of this repository. The part folders lie outside it, copied
from this checkout, and each step reaches them only through the variable that its `$/` step set, as on a runner. Where a
step would download or install, a stand-in answers: a local release folder with stand-in tools for the tool
installation and a stand-in `pip` module for the dependencies.

The steps: the plan step with its base and head, the tool installation, the Gitleaks step with and without
`.gitleaks.toml`, and the install and test steps of `python-verify` and `python-test`. A step that reads a file of this
repository through the working directory fails here, because the working directory is the consumer."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from tests.ci_policy_support import expression_value, read_workflow
from tests.pipeline_fixtures import git, write

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "pipeline.yml"
# The part folders that the steps under test load; the plan program imports from its sibling repository-hygiene.
PART_FOLDERS = ("pipeline", "repository-hygiene", "python-tests")
PLATFORM = "linux-x64"
PULL_REQUEST = "17"

requires_bash = unittest.skipUnless(sys.platform != "win32" and shutil.which("bash"),
                                    "the steps run in Bash off Windows, as on the hosted Linux runner")

# A stand-in for a pinned tool: it records its name, its arguments and its directory; a `--config` or `--baseline-path`
# that names no file there fails it, as the real Gitleaks fails.
STAND_IN_TOOL = """#!/usr/bin/env bash
printf '%s\\n' "$(basename "$0") $* @ $PWD" >> "$TOOL_LOG"
while [ "$#" -gt 0 ]; do
  case "$1" in --config|--baseline-path) [ -f "$2" ] || { echo "$1 $2: no such file" >&2; exit 3; } ;; esac
  shift
done
"""
# A stand-in for `python -m pip`: it records its arguments and directory, and fails for a requirements file that does not
# exist there, as pip does.
STAND_IN_PIP = """import json, os, sys
with open(os.environ["PIP_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\\n")
if "-r" in sys.argv and not os.path.isfile(sys.argv[sys.argv.index("-r") + 1]):
    sys.exit("ERROR: Could not open requirements file")
"""
# A passing test that names its place in the output, so that a case can tell which directory's tests ran.
PASSING_TEST = "import unittest\n\n\nclass Passing(unittest.TestCase):\n    def test_passes(self):\n        print('the tests of %s ran')\n"
CONFIGURATION = ("version: 1\nparts:\n  python-test:\n    projects:\n      - python-version: \"3.14\"\n        working-directory: app\n"
                 "    paths: [app/]\n")


def expression(text: str) -> str:
    """An expression of the workflow in the form that the expression reader of the tests follows: `a['b']` as `a.b`."""
    return re.sub(r"\['([\w-]+)'\]", r".\1", text)


def tool_archive(member: str, asset: str) -> bytes:
    """A release asset that holds the stand-in tool as ``member``, packed as the asset's name says."""
    data = STAND_IN_TOOL.encode("utf-8")
    buffer = io.BytesIO()
    mode = "w:xz" if asset.endswith(".tar.xz") else "w:gz"
    with tarfile.open(fileobj=buffer, mode=mode) as bundle:
        info = tarfile.TarInfo(member)
        info.size, info.mode = len(data), 0o755
        bundle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@requires_bash
class PipelineStepShellTests(unittest.TestCase):
    def setUp(self) -> None:
        self.jobs = read_workflow(WORKFLOW)[1]["jobs"]
        self.base = Path(tempfile.mkdtemp(prefix="pipeline-steps-"))
        self.addCleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.parts = self.base / "shared"
        for folder in PART_FOLDERS:
            shutil.copytree(REPOSITORY_ROOT / "actions" / folder, self.parts / folder,
                            ignore=shutil.ignore_patterns("__pycache__", "consumer-kit"))
        self.consumer = self.base / "consumer"
        self.consumer.mkdir()
        git(self.consumer, "init", "--quiet", "--initial-branch=main")
        self.runner_temp = self.base / "runner-temp"
        self.runner_temp.mkdir()
        self.summary = self.base / "summary.md"
        self.summary.touch()
        self.tool_log = self.base / "tools.log"
        self.pip_log = self.base / "pip.log"
        write(self.base / "stand-ins", "pip/__init__.py", "")
        write(self.base / "stand-ins", "pip/__main__.py", STAND_IN_PIP)
        self.releases = self.base / "releases"
        self.publish_stand_in_tools()

    def publish_stand_in_tools(self) -> None:
        """Place a stand-in for every pinned release tool in the local release folder, and give the copied pins file their
        checksums, so that the real installation downloads, verifies and extracts each stand-in."""
        pins_path = self.parts / "repository-hygiene" / "pins.json"
        pins = json.loads(pins_path.read_text(encoding="utf-8"))
        for tool in pins["tools"].values():
            entry = tool["platforms"][PLATFORM]
            asset = entry["asset"].replace("{version}", tool["version"])
            archive = tool_archive(entry["binary"].replace("{version}", tool["version"]), asset)
            target = self.releases / tool["repository"] / "releases" / "download" / f"v{tool['version']}" / asset
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive)
            entry["sha256"] = hashlib.sha256(archive).hexdigest()
        pins_path.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")

    def commit(self, files: dict[str, str], message: str = "change") -> str:
        for relative, text in files.items():
            write(self.consumer, relative, text)
        git(self.consumer, "add", "-A")
        git(self.consumer, "commit", "--quiet", "-m", message)
        return git(self.consumer, "rev-parse", "HEAD")

    def assert_holds_no_file_of_this_repository(self) -> None:
        tracked = set(git(self.consumer, "ls-files").splitlines())
        for folder in ("actions/", ".github/workflows/", "tests/"):
            self.assertEqual([], [path for path in tracked if path.startswith(folder)], folder)

    def run_step(self, job: str, name: str, context: dict) -> tuple[subprocess.CompletedProcess[str], dict[str, str]]:
        """Run one step's shell as the runner does, in the consumer, with its `env` resolved from ``context``."""
        step = next(item for item in self.jobs[job]["steps"] if name in (item.get("id"), item.get("name")))
        output = self.base / f"output-{len(list(self.base.glob('output-*')))}"
        output.touch()
        values = {"runner": {"os": "Linux", "temp": str(self.runner_temp)}, "steps": {}, "matrix": {"project": {}}, **context}
        github = values.setdefault("github", {})
        github.setdefault("event", {})

        def resolve(text: str) -> str:
            def value(found: re.Match[str]) -> str:
                result = expression_value(expression(found[1]), values)
                return "" if result is None else str(result)
            return re.sub(r"\$\{\{\s*(.+?)\s*\}\}", value, text)

        environment = {key: item for key, item in os.environ.items() if not key.startswith(("PYTHON", "GITHUB_", "RUNNER_"))}
        environment.update({"RUNNER_TEMP": str(self.runner_temp), "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(self.summary),
                            "TOOL_LOG": str(self.tool_log), "PIP_LOG": str(self.pip_log), "PYTHONPATH": str(self.base / "stand-ins"),
                            "HYGIENE_RELEASE_BASE_URL": self.releases.as_uri(),
                            **{key: resolve(str(item)) for key, item in step.get("env", {}).items()}})
        self.assert_holds_no_file_of_this_repository()
        completed = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]], cwd=self.consumer, env=environment, capture_output=True,
                             text=True, encoding="utf-8")
        outputs = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines() if "=" in line)
        return completed, outputs

    def loaded(self, folder: str) -> dict:
        """The outputs of a `$/actions/<folder>` step: the path of the part folder outside the consumer."""
        return {"outputs": {"path": str(self.parts / folder)}}

    # --- the plan step --------------------------------------------------------------------------------------------

    def plan(self, event: str, *, before: str = "", sha: str = "", pull_request: dict | None = None, payload: str = "") -> dict:
        context = {"steps": {"resources": self.loaded("pipeline")},
                   "github": {"event_name": event, "sha": sha, "workflow": "CI", "run_id": "9",
                              "event": {"before": before, "pull_request": pull_request or {}, "client_payload": {"sha": payload} if payload else {}}}}
        ran, produced = self.run_step("plan", "plan", context)
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertIn("concurrency", produced)
        return json.loads(produced["plan"])

    def given_history(self) -> tuple[str, str, str]:
        first = self.commit({".github/ci-config.yml": CONFIGURATION, "README.md": "A consumer.\n"})
        source = self.commit({"app/module.py": "VALUE = 1\n"})
        readme = self.commit({"README.md": "A consumer of the pipeline.\n"})
        return first, source, readme

    def test_the_plan_step_compares_a_pull_request_from_the_merge_base_of_its_base_and_head(self) -> None:
        first, source, readme = self.given_history()
        for base, head, runs in ((first, source, True), (source, readme, False)):
            with self.subTest(base=base, head=head):
                plan = self.plan("pull_request", pull_request={"number": PULL_REQUEST, "base": {"sha": base}, "head": {"sha": head}})
                self.assertTrue(plan["compared"])
                self.assertEqual(runs, plan["parts"]["python-test"]["run"], plan["parts"]["python-test"]["reason"])

    def test_the_plan_step_compares_a_push_from_the_previous_head(self) -> None:
        first, source, readme = self.given_history()
        self.assertTrue(self.plan("push", before=first, sha=source)["parts"]["python-test"]["run"])
        self.assertFalse(self.plan("push", before=source, sha=readme)["parts"]["python-test"]["run"])

    def test_the_plan_step_compares_a_dispatched_commit_with_its_first_parent(self) -> None:
        _, source, readme = self.given_history()
        plan = self.plan("repository_dispatch", sha=source, payload=readme)
        self.assertTrue(plan["compared"])
        self.assertFalse(plan["parts"]["python-test"]["run"], "the dispatched commit changes only the readme against its parent")

    # --- the tool installation and the Gitleaks step --------------------------------------------------------------

    def install(self, **changes: str) -> subprocess.CompletedProcess[str]:
        outputs = {"actionlint": "false", "zizmor": "false", "reuse": "false", **changes}
        ran, _ = self.run_step("repository-hygiene", "install", {"steps": {"resources": self.loaded("repository-hygiene"), "changes": {"outputs": outputs}}})
        return ran

    def installed(self) -> list[str]:
        return sorted(path.name for path in (self.runner_temp / "hygiene-tools").iterdir())

    def test_the_tool_installation_installs_gitleaks_alone_when_no_other_check_runs(self) -> None:
        self.commit({"README.md": "A consumer.\n"})
        ran = self.install()
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertEqual(["gitleaks"], self.installed())

    def test_the_tool_installation_adds_the_tools_of_each_check_that_runs(self) -> None:
        self.commit({"README.md": "A consumer.\n"})
        ran = self.install(actionlint="true", zizmor="true")
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertEqual(["actionlint", "gitleaks", "shellcheck", "zizmor"], self.installed())

    def gitleaks(self) -> list[str]:
        ran = self.install()
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        context = {"steps": {"resources": self.loaded("repository-hygiene")}}
        ran, _ = self.run_step("repository-hygiene", "Gitleaks over all reachable history", context)
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        return self.tool_log.read_text(encoding="utf-8").splitlines()

    def test_the_gitleaks_step_passes_the_consumer_s_configuration_and_baseline_when_it_holds_them(self) -> None:
        self.commit({".gitleaks.toml": "[extend]\nuseDefault = true\n", ".gitleaks-baseline.json": "[]\n", "README.md": "A consumer.\n"})
        self.assertEqual([f"gitleaks git --log-opts=--all --redact --no-banner --no-color --config .gitleaks.toml "
                          f"--baseline-path .gitleaks-baseline.json . @ {self.consumer.resolve()}"], self.gitleaks())

    def test_the_gitleaks_step_passes_no_configuration_when_the_consumer_holds_none(self) -> None:
        self.commit({"README.md": "A consumer.\n"})
        self.assertEqual([f"gitleaks git --log-opts=--all --redact --no-banner --no-color . @ {self.consumer.resolve()}"], self.gitleaks())

    # --- the install and test steps of python-verify and python-test ----------------------------------------------

    def pip_calls(self) -> list[dict]:
        return [json.loads(line) for line in self.pip_log.read_text(encoding="utf-8").splitlines()] if self.pip_log.exists() else []

    def test_the_install_step_of_each_python_part_installs_the_requirements_file_from_the_working_directory(self) -> None:
        self.commit({"app/requirements-dev.txt": "pytest\n"})
        for job in ("python-verify", "python-test"):
            with self.subTest(job=job):
                self.pip_log.unlink(missing_ok=True)
                project = {"python-version": "3.14", "working-directory": "app", "install": "requirements-dev.txt"}
                ran, _ = self.run_step(job, "Install the dependencies", {"steps": {"resources": self.loaded("python-tests")}, "matrix": {"project": project}})
                self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
                self.assertEqual([{"argv": ["install", "--disable-pip-version-check", "-r", "requirements-dev.txt"], "cwd": str(self.consumer.resolve() / "app")}],
                                 self.pip_calls())

    def test_the_install_step_of_each_python_part_installs_nothing_at_the_root_without_an_install_value(self) -> None:
        self.commit({"README.md": "A consumer.\n"})
        for job in ("python-verify", "python-test"):
            with self.subTest(job=job):
                ran, _ = self.run_step(job, "Install the dependencies", {"steps": {"resources": self.loaded("python-tests")},
                                                                         "matrix": {"project": {"python-version": "3.14"}}})
                self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
                self.assertIn("nothing is installed", ran.stdout)
                self.assertEqual([], self.pip_calls())

    def test_the_test_step_of_python_test_runs_the_tests_of_the_working_directory_or_of_the_root(self) -> None:
        self.commit({"app/test_app.py": PASSING_TEST % "app", "test_root.py": PASSING_TEST % "the root"})
        for project, place in (({"python-version": "3.14", "working-directory": "app"}, "app"), ({"python-version": "3.14"}, "the root")):
            with self.subTest(project=project):
                ran, _ = self.run_step("python-test", "Run the tests", {"steps": {"resources": self.loaded("python-tests")}, "matrix": {"project": project}})
                self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
                self.assertIn("-m unittest discover", ran.stdout)
                self.assertIn(f"the tests of {place} ran", ran.stdout)
                self.assertNotIn(f"the tests of {'the root' if place == 'app' else 'app'} ran", ran.stdout)


if __name__ == "__main__":
    unittest.main()
