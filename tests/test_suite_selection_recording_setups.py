"""The setups that a suite list states as data, run by the recording step's ``setups`` command as their own processes on a
fixture repository: the order of the setups before the recording and under it, what each script gets, and what an entry
file sees of what they provide."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

from tests.ci_policy_support import read_workflow
from tests.recording_fixtures import RecordingFixture, requires_etw, suite

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SUITE_JOBS = REPOSITORY_ROOT / ".github" / "workflows" / "suite-jobs.yml"
BEFORE_STEP = "Run the setups that start before the recording"
START_STEP = "Start the recording"
RECORDED_STEP = "Run the setups under the recording and provide what every setup gives"

ON_WINDOWS = sys.platform == "win32"
# Each script appends its name and what it got to the file that ORDER names, and states what it provides.
SCRIPT = """\
import os
import sys
from pathlib import Path

name = os.environ["SETUP_NAME"]
directory = Path(os.environ["SETUP_DIRECTORY"])
with open(os.environ["ORDER"], "a", encoding="utf-8") as order:
    order.write(f"{name} tool={os.environ.get('SETUP_TOOL', '')} directory={directory.is_dir()} seen={os.environ.get('SEEN', '')}\\n")
(directory / "marker.txt").write_text(name, encoding="utf-8")
with open(os.environ["SETUP_PROVIDES"], "a", encoding="utf-8") as provides:
    provides.write(f"path={directory}\\n")
    provides.write(f"variable=FROM_{name.upper()}={name}\\n")
sys.exit(int(os.environ.get("FAIL_" + name.upper(), "0")))
"""
ENTRY = """\
import os

names = [os.path.basename(path) for path in os.environ["PATH"].split(os.pathsep)[:3]]
print("ENTRY", names, os.environ.get("FROM_FIRST"), os.environ.get("FROM_TRACED"), os.environ.get("FROM_LAST"), os.environ.get("FROM_OTHER"))
"""
SETUPS = {
    "traced": {"script": "ci/setup.py"},
    "first": {"script": "ci/setup.py", "recorded": False, "tool": "python", "version": "3.13"},
    "other": {"script": "ci/setup.py"},
    "last": {"script": "ci/setup.py", "recorded": False},
}


class SetupFixture(RecordingFixture):
    """A fixture repository whose suite list states four setups, two before the recording and two under it, in an order
    that mixes them; the entry file states three of them."""

    def __init__(self, cleanup, setups: dict | None = None) -> None:
        super().__init__(cleanup, {"ci/setup.py": SCRIPT, "ci/setup.sh": "echo \"path=$SETUP_DIRECTORY\" >> \"$SETUP_PROVIDES\"\n",
                                   "ci/setup.ps1": "Add-Content -Path $env:SETUP_PROVIDES -Value \"variable=FROM_PWSH=pwsh\"\n",
                                   "checks/entry.py": ENTRY},
                         [suite("unit", ["checks/entry.py"], command=[Path(sys.executable).name], setups=["last", "traced", "first"])])
        document = self.suites()
        (self.root / "suites.json").write_text(json.dumps({"always": [], "setups": setups or SETUPS, "suites": document["suites"]}, indent=2) + "\n",
                                               encoding="utf-8")
        self.work = self.base / "setups"
        self.order = self.base / "order.txt"

    def environment(self, path: str | None = None) -> dict[str, str]:
        return super().environment(path) | {"ORDER": str(self.order), **self.extra}

    extra: dict[str, str] = {}

    def setups(self, phase: str, *names: str, tools: tuple[str, ...] = ("python=/tools/python3",), pinned: tuple[str, ...] = ()):
        options = [option for name in names or ("first", "traced", "last") for option in ("--setup", name)]
        options += [option for tool in tools for option in ("--tool", tool)]
        options += [option for tool in pinned for option in ("--pinned", tool)]
        return self.run("setups", "--output", str(self.output), "--suites", "suites.json", "--work", str(self.work), "--phase", phase, *options)

    def ordered(self) -> list[str]:
        return self.order.read_text(encoding="utf-8").splitlines() if self.order.is_file() else []

    def run_all(self) -> None:
        for step in (lambda: self.setups("before"), self.start, lambda: self.setups("recorded")):
            done = step()
            assert done.returncode == 0, done.stdout + done.stderr


@unittest.skipIf(ON_WINDOWS, "the fixture's recording runs under the stand-in strace")
class SetupOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = SetupFixture(self.addCleanup)

    def test_the_setups_before_the_recording_run_first_and_outside_it_each_group_in_the_suite_list_s_order(self) -> None:
        fixture = self.fixture
        before = fixture.setups("before")
        self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
        self.assertEqual([line.split()[0] for line in fixture.ordered()], ["first", "last"], "the suite list states first before last")
        self.assertEqual(fixture.stand_in_calls(), [], "a setup before the recording runs outside it")
        self.assertEqual(fixture.start().returncode, 0)
        recorded = fixture.setups("recorded")
        self.assertEqual(recorded.returncode, 0, recorded.stdout + recorded.stderr)
        self.assertEqual([line.split()[0] for line in fixture.ordered()], ["first", "last", "traced"],
                         "the job runs only the setups it names: other stays out")
        traced = [call[call.index("--") + 1:] for call in fixture.stand_in_calls()]
        self.assertEqual(traced, [[sys.executable, "ci/setup.py"]], "the setup under the recording is the one traced command")
        units = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((fixture.output / "units").glob("*.json"))]
        self.assertEqual([(unit["kind"], unit["name"]) for unit in units], [("setup", "traced")])

    def test_a_script_gets_its_name_a_directory_of_its_own_and_the_tool_that_the_job_installed_for_it(self) -> None:
        self.fixture.run_all()
        self.assertEqual(self.fixture.ordered(), ["first tool=/tools/python3 directory=True seen=", "last tool= directory=True seen=",
                                                  "traced tool= directory=True seen="])
        self.assertEqual((self.fixture.work / "first" / "marker.txt").read_text(encoding="utf-8"), "first")

    def test_an_entry_file_gets_what_its_stated_setups_provide_in_its_own_order(self) -> None:
        self.fixture.run_all()
        ran = self.fixture.exec_entry("checks/entry.py", sys.executable, "checks/entry.py")
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("ENTRY ['last', 'traced', 'first'] first traced last None", ran.stdout)

    def test_a_tool_that_the_job_did_not_install_fails_the_setup_that_names_it(self) -> None:
        failed = self.fixture.setups("before", tools=())
        self.assertEqual(failed.returncode, 2)
        self.assertIn("the setup first names the tool python, which this job did not install", failed.stderr)
        self.assertEqual(self.fixture.ordered(), [])

    def test_a_failing_script_fails_the_command_and_stops_the_setups_after_it(self) -> None:
        self.fixture.extra = {"FAIL_FIRST": "7"}
        self.addCleanup(setattr, self.fixture, "extra", {})
        failed = self.fixture.setups("before")
        self.assertEqual(failed.returncode, 7)
        self.assertIn("the setup first failed with exit code 7", failed.stderr)
        self.assertEqual([line.split()[0] for line in self.fixture.ordered()], ["first"])

    def test_a_setup_that_the_suite_list_does_not_define_fails(self) -> None:
        failed = self.fixture.setups("before", "first", "unknown")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("the suite list suites.json defines no setup unknown", failed.stderr)

    def test_the_setups_under_the_recording_need_the_setups_before_it_to_have_run(self) -> None:
        self.assertEqual(self.fixture.start().returncode, 0)
        failed = self.fixture.setups("recorded")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("the setup first runs before the recording starts", failed.stderr)

    def test_a_script_runs_in_bash_or_powershell_by_its_suffix(self) -> None:
        fixture = SetupFixture(self.addCleanup, {"shell": {"script": "ci/setup.sh", "recorded": False}, "traced": {"script": "ci/setup.py"},
                                                 "first": {"script": "ci/setup.py", "recorded": False}, "last": {"script": "ci/setup.ps1"}})
        for step in (lambda: fixture.setups("before", "shell", "last"), fixture.start, lambda: fixture.setups("recorded", "shell", "last")):
            done = step()
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        provided = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in (fixture.output / "setups").glob("*.json")}
        self.assertEqual(provided["shell"], {"paths": [str(fixture.work / "shell")], "variables": {}})
        self.assertEqual(provided["last"], {"paths": [], "variables": {"FROM_PWSH": "pwsh"}})
        self.assertEqual([call[call.index("--") + 1:] for call in fixture.stand_in_calls()],
                         [["pwsh", "-NoProfile", "-NonInteractive", "-File", "ci/setup.ps1"]])

    def test_applying_additions_keeps_the_setups_and_the_jobs_of_the_suite_list(self) -> None:
        fixture = self.fixture
        stated = fixture.suites()
        stated = {"always": [], "jobs": {"linux": {"fetch-depth": 0}}, "setups": stated["setups"], "suites": stated["suites"]}
        (fixture.root / "suites.json").write_text(json.dumps(stated, indent=2) + "\n", encoding="utf-8")
        fixture.run_all()
        self.assertEqual(fixture.exec_entry("checks/entry.py", sys.executable, "checks/entry.py").returncode, 0)
        checked = fixture.check()
        self.assertEqual(checked.returncode, 1, checked.stdout + checked.stderr)
        applied = fixture.apply()
        self.assertEqual(applied.returncode, 0, applied.stdout + applied.stderr)
        after = fixture.suites()
        self.assertEqual(list(after), ["always", "jobs", "setups", "suites"])
        self.assertEqual((after["jobs"], after["setups"]), (stated["jobs"], stated["setups"]))
        self.assertIn("paths", after["suites"][0], "the additions were applied")

    def test_a_line_that_states_no_known_provision_fails(self) -> None:
        (self.fixture.root / "ci/setup.py").write_text(textwrap.dedent("""\
            import os
            open(os.environ["SETUP_PROVIDES"], "a").write("folder=/x\\n")
            """), encoding="utf-8")
        self.assertEqual(self.fixture.setups("before").returncode, 0)
        self.assertEqual(self.fixture.start().returncode, 0)
        failed = self.fixture.setups("recorded")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("the setup traced states `folder=/x`; a line is path=<directory>, variable=<name>=<value>, python=<interpreter> or write=<directory>",
                      failed.stderr)


PATH_ENTRY = """\
import os

print("PATH", [os.path.basename(path) for path in os.environ["PATH"].split(os.pathsep)[:2]])
"""


class PinnedToolFixture(SetupFixture):
    """A fixture repository with two entry files: one states the pinned tool gitleaks after a setup of the suite list, and
    one states that setup alone. The suite list defines no setup gitleaks."""

    def __init__(self, cleanup) -> None:
        RecordingFixture.__init__(self, cleanup, {"ci/setup.py": SCRIPT, "checks/pinned.py": PATH_ENTRY, "checks/plain.py": PATH_ENTRY},
                                  [suite("unit", ["checks/pinned.py"], command=[Path(sys.executable).name], setups=["last", "gitleaks"]),
                                   suite("plain", ["checks/plain.py"], command=[Path(sys.executable).name], setups=["last"])])
        document = self.suites()
        (self.root / "suites.json").write_text(json.dumps({"always": [], "setups": SETUPS, "suites": document["suites"]}, indent=2) + "\n",
                                               encoding="utf-8")
        self.work = self.base / "setups"
        self.order = self.base / "order.txt"
        self.tool = self.base / "installed" / "gitleaks"
        self.tool.mkdir(parents=True)

    def run_job(self, *pinned: str) -> None:
        for step in (lambda: self.setups("before", "last", "gitleaks", pinned=pinned), self.start,
                     lambda: self.setups("recorded", "last", "gitleaks", pinned=pinned)):
            done = step()
            assert done.returncode == 0, done.stdout + done.stderr


@unittest.skipIf(ON_WINDOWS, "the fixture's recording runs under the stand-in strace")
class PinnedToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = PinnedToolFixture(self.addCleanup)

    def test_only_the_entry_file_that_states_a_pinned_tool_gets_its_directory(self) -> None:
        fixture = self.fixture
        fixture.run_job(f"gitleaks={fixture.tool}")
        self.assertEqual([line.split()[0] for line in fixture.ordered()], ["last"], "a pinned tool runs no script")
        self.assertEqual(fixture.stand_in_calls(), [], "and nothing of it runs under the recording")
        pinned = fixture.exec_entry("checks/pinned.py", sys.executable, "checks/pinned.py")
        self.assertEqual(pinned.returncode, 0, pinned.stdout + pinned.stderr)
        self.assertIn("PATH ['last', 'gitleaks']", pinned.stdout)
        plain = fixture.exec_entry("checks/plain.py", sys.executable, "checks/plain.py")
        self.assertEqual(plain.returncode, 0, plain.stdout + plain.stderr)
        self.assertIn("PATH ['last',", plain.stdout)
        self.assertNotIn("gitleaks", plain.stdout)

    def test_a_pinned_tool_that_the_job_did_not_install_is_an_undefined_setup(self) -> None:
        failed = self.fixture.setups("before", "last", "gitleaks")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("the suite list suites.json defines no setup gitleaks", failed.stderr)

    def test_an_entry_file_fails_when_its_job_gave_no_pinned_tool(self) -> None:
        fixture = self.fixture
        for step in (lambda: fixture.setups("before", "last"), fixture.start, lambda: fixture.setups("recorded", "last")):
            self.assertEqual(step().returncode, 0)
        failed = fixture.exec_entry("checks/pinned.py", sys.executable, "checks/pinned.py")
        self.assertEqual(failed.returncode, 2)
        self.assertIn("the setup gitleaks that checks/pinned.py states was not installed in this job", failed.stderr)

    def test_a_missing_directory_or_a_name_that_is_no_pinned_tool_fails(self) -> None:
        fixture = self.fixture
        refused = fixture.setups("before", "last", pinned=("actionlint=/x",))
        self.assertEqual(refused.returncode, 2)
        self.assertIn("--pinned takes <tool>=<directory> with one of node, gitleaks, not actionlint=/x", refused.stderr)
        self.assertEqual(fixture.setups("before", "last", "gitleaks", pinned=(f"gitleaks={fixture.base / 'absent'}",)).returncode, 0)
        self.assertEqual(fixture.start().returncode, 0)
        failed = fixture.setups("recorded", "last", "gitleaks", pinned=(f"gitleaks={fixture.base / 'absent'}",))
        self.assertEqual(failed.returncode, 2)
        self.assertIn("of the setup gitleaks is missing or not absolute", failed.stderr)

    def test_a_setup_of_the_suite_list_is_no_pinned_tool(self) -> None:
        failed = self.fixture.setups("before", "last", pinned=(f"node={self.fixture.tool}",))
        self.assertEqual(failed.returncode, 0, failed.stderr)
        document = self.fixture.suites()
        document["setups"]["gitleaks"] = {"script": "ci/setup.py"}
        (self.fixture.root / "suites.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        failed = self.fixture.setups("before", "last", "gitleaks", pinned=(f"gitleaks={self.fixture.tool}",))
        self.assertEqual(failed.returncode, 2)
        self.assertIn("the suite list suites.json defines the setup gitleaks, so it is no pinned tool of this job", failed.stderr)


WHICH_NODE = """\
import shutil

print("NODE", shutil.which("node"))
"""


class PinnedNodeFixture(RecordingFixture):
    """A fixture repository whose suite list defines no setup: one entry file states the pinned tool node and one states
    nothing. ``installed`` stands for the directory of the programs of the Node.js that the job's action installed."""

    def __init__(self, cleanup, program: str, content: str) -> None:
        command = [Path(sys.executable).name]
        super().__init__(cleanup, {"checks/node.py": WHICH_NODE, "checks/plain.py": WHICH_NODE},
                         [suite("unit", ["checks/node.py"], command=command, setups=["node"]), suite("plain", ["checks/plain.py"], command=command, setups=[])])
        document = self.suites()
        (self.root / "suites.json").write_text(json.dumps({"always": [], "setups": {}, "suites": document["suites"]}, indent=2) + "\n", encoding="utf-8")
        self.installed = self.base / "toolcache" / "node" / "bin"
        self.installed.mkdir(parents=True)
        (self.installed / program).write_text(content, encoding="utf-8")
        (self.installed / program).chmod(0o755)

    def job_environment(self, **step: str) -> dict[str, str]:
        """The environment of a step of the suite job: the runner's temporary directory holds the recording and the setups."""
        return self.environment() | {"RUNNER_TEMP": str(self.base), "RECORDING": str(REPOSITORY_ROOT / "actions" / "recording"), "SUITES": "suites.json"} | step

    def options(self) -> list[str]:
        return (self.base / "setup-options.txt").read_text(encoding="utf-8-sig").splitlines()


def job_step(job: str, name: str) -> str:
    """The commands of one step of a suite job, as the shared workflow states them."""
    (step,) = [step for step in read_workflow(SUITE_JOBS)[1]["jobs"][job]["steps"] if step.get("name") == name]
    return step["run"]


@unittest.skipIf(ON_WINDOWS, "the fixture's recording runs under the stand-in strace")
@unittest.skipUnless(shutil.which("bash"), "the steps of the Linux suite job run in Bash")
class PinnedNodeLinuxJobTests(unittest.TestCase):
    """The steps of the Linux suite job around its setups, run in Bash as Actions runs them, for a shard whose entry file
    states the pinned tool node."""

    def test_the_entry_file_that_states_node_finds_the_node_that_the_job_installed_and_no_other_entry_file_does(self) -> None:
        fixture = PinnedNodeFixture(self.addCleanup, "node", "#!/bin/sh\necho v24.9.0\n")
        based = fixture.run("base", "--file", str(fixture.base / "recording-environment.json"))
        self.assertEqual(based.returncode, 0, based.stdout + based.stderr)
        # The job's action puts Node.js on the PATH of the steps after the recording step, which wrote the job's environment.
        job_path = str(fixture.installed) + os.pathsep + str(fixture.stand_in / "bin") + os.pathsep + os.environ["PATH"]
        for name, step in ((BEFORE_STEP, {"SETUPS": "node", "PINNED": "node", "PYTHON_PATH": "", "NODE": "24.9.0", "DOTNET": ""}),
                           (START_STEP, {"FULL": "false", "JOB": "shard-1"}), (RECORDED_STEP, {})):
            ran = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", job_step("linux", name)], cwd=fixture.root, capture_output=True, text=True,
                                 env=fixture.job_environment(PATH=job_path, **step))
            self.assertEqual(ran.returncode, 0, f"{name}: {ran.stdout}{ran.stderr}")
        self.assertEqual(["--setup", "node", "--tool", f"node={fixture.installed}", "--pinned", f"node={fixture.installed}"], fixture.options())
        stating = fixture.exec_entry("checks/node.py", sys.executable, "checks/node.py")
        self.assertEqual(stating.returncode, 0, stating.stdout + stating.stderr)
        self.assertIn(f"NODE {fixture.installed / 'node'}\n", stating.stdout, "the pinned Node.js is first on the PATH of the entry file that states it")
        plain = fixture.exec_entry("checks/plain.py", sys.executable, "checks/plain.py")
        self.assertEqual(plain.returncode, 0, plain.stdout + plain.stderr)
        self.assertNotIn(str(fixture.installed), plain.stdout, "an entry file that does not state node gets nothing of the pinned tool")

    def test_a_shard_without_a_pinned_tool_writes_no_pinned_option(self) -> None:
        fixture = PinnedNodeFixture(self.addCleanup, "node", "#!/bin/sh\necho v24.9.0\n")
        ran = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", job_step("linux", BEFORE_STEP)], cwd=fixture.root, capture_output=True, text=True,
                             env=fixture.job_environment(SETUPS="", PINNED="", PYTHON_PATH="", NODE="", DOTNET=""))
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual([], fixture.options())


@unittest.skipUnless(ON_WINDOWS and shutil.which("pwsh"), "the step of the Windows suite job runs in PowerShell on Windows")
class PinnedNodeWindowsJobTests(unittest.TestCase):
    """The step of the Windows suite job that names its setups, run in PowerShell as Actions runs it, for a job whose entry
    file states the pinned tool node. The step starts no recording."""

    def before(self, fixture: PinnedNodeFixture, job: dict) -> subprocess.CompletedProcess[str]:
        path = str(fixture.installed) + os.pathsep + os.environ["PATH"]
        environment = fixture.job_environment(PATH=path, WINDOWS=json.dumps(job), TOOLS=json.dumps(job.get("tools", {})), PYTHON_PATH="")
        return subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", job_step("windows", BEFORE_STEP)], cwd=fixture.root,
                              capture_output=True, text=True, env=environment)

    def test_the_job_gives_the_directory_of_the_node_that_its_action_installed_as_the_pinned_tool(self) -> None:
        fixture = PinnedNodeFixture(self.addCleanup, "node.cmd", "@echo v24.9.0\r\n")
        ran = self.before(fixture, {"setups": ["node"], "pinned": ["node"], "tools": {"node": "24.9.0"}, "entries": []})
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual(["--setup", "node", "--tool", f"node={fixture.installed}", "--pinned", f"node={fixture.installed}"], fixture.options())

    def test_a_job_without_a_pinned_tool_writes_no_pinned_option(self) -> None:
        fixture = PinnedNodeFixture(self.addCleanup, "node.cmd", "@echo v24.9.0\r\n")
        ran = self.before(fixture, {"setups": [], "tools": {}, "entries": []})
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertEqual([], fixture.options())


@requires_etw
class WindowsSetupOrderTests(unittest.TestCase):
    """The same order under the live ETW session of the Windows job."""

    def test_the_setups_run_in_order_and_the_entry_file_gets_what_they_provide(self) -> None:
        fixture = SetupFixture(self.addCleanup)
        fixture.run_all()
        self.assertEqual([line.split()[0] for line in fixture.ordered()], ["first", "last", "traced"])
        units = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((fixture.output / "units").glob("*.json"))]
        self.assertEqual([(unit["kind"], unit["name"]) for unit in units], [("setup", "traced")])
        ran = fixture.exec_entry("checks/entry.py", sys.executable, "checks/entry.py")
        self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
        self.assertIn("ENTRY ['last', 'traced', 'first'] first traced last None", ran.stdout)
        fixture.check()  # stops the session


class DotnetSetupTests(unittest.TestCase):
    """The repository's dotnet setup isolates NuGet packages and caches under SETUP_DIRECTORY."""

    def test_dotnet_setup_creates_nuget_directories_exports_variables_and_grants_writes(self) -> None:
        from tests import temporary_support

        work = temporary_support.make_directory(self.addCleanup, prefix="dotnet-setup-")
        setup_dir = work / "dotnet"
        setup_dir.mkdir()
        tool_dir = work / "sdk"
        tool_dir.mkdir()
        provides_file = work / "dotnet.provides"
        provides_file.write_text("", encoding="utf-8")
        env = {
            **os.environ,
            "SETUP_NAME": "dotnet",
            "SETUP_DIRECTORY": str(setup_dir),
            "SETUP_PROVIDES": str(provides_file),
            "SETUP_TOOL": str(tool_dir),
        }
        ran = subprocess.run([sys.executable, str(REPOSITORY_ROOT / "tests" / "setups" / "dotnet.py")],
                             cwd=REPOSITORY_ROOT, capture_output=True, text=True, env=env, check=False)
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        lines = [line.strip() for line in provides_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertIn(f"path={tool_dir}", lines)
        self.assertIn(f"variable=DOTNET_ROOT={tool_dir}", lines)
        variables = dict(line.removeprefix("variable=").split("=", 1) for line in lines if line.startswith("variable="))
        writes = [line.removeprefix("write=") for line in lines if line.startswith("write=")]
        for name in ("DOTNET_CLI_HOME", "NUGET_PACKAGES", "NUGET_HTTP_CACHE_PATH", "NUGET_PLUGINS_CACHE_PATH"):
            self.assertIn(name, variables, f"{name} missing from {lines}")
            target = Path(variables[name])
            self.assertTrue(target.is_dir(), f"{name}={target} was not created")
            self.assertTrue(target.resolve().is_relative_to(setup_dir.resolve()), f"{name}={target} is not under {setup_dir}")
            self.assertIn(str(target), writes, f"{name}={target} was not granted via write=")


if __name__ == "__main__":
    unittest.main()
