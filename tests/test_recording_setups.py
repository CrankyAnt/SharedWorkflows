"""The setups that a suite list states as data, run by the recording step's ``setups`` command as their own processes on a
fixture repository: the order of the setups before the recording and under it, what each script gets, and what an entry
file sees of what they provide."""

from __future__ import annotations

import json
import sys
import textwrap
import unittest
from pathlib import Path

from tests.recording_fixtures import RecordingFixture, requires_etw, suite

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

    def setups(self, phase: str, *names: str, tools: tuple[str, ...] = ("python=/tools/python3",)):
        options = [option for name in names or ("first", "traced", "last") for option in ("--setup", name)]
        options += [option for tool in tools for option in ("--tool", tool)]
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
        self.assertIn("the setup traced states `folder=/x`; a line is path=<directory>, variable=<name>=<value> or python=<interpreter>", failed.stderr)


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


if __name__ == "__main__":
    unittest.main()
