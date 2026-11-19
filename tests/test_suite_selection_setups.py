"""The setups and the jobs that a suite list states as data: what the suite list accepts, what select gives the suite jobs
and what a change to a setup selects, on fixture repositories."""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tests import temporary_support
from tests.suite_selection_fixtures import git, write

TOOL = Path(__file__).resolve().parents[1] / "actions/suite-selection"
sys.path.insert(0, str(TOOL))

import suite_selection

SETUPS = {
    "python": {"script": "ci/setups/python.py", "recorded": False, "tool": "python", "version": "3.13"},
    "node": {"script": "ci/setups/node.py", "recorded": False, "tool": "node", "version-file": "ci/pins.json#/node/version"},
    "linter": {"script": "ci/setups/linter.sh"},
}
SUITES = [
    {"name": "unit", "entries": ["checks/test_a.py", "checks/test_b.py"], "command": ["python3"], "setups": ["python"],
     "entry_setups": {"checks/test_b.py": ["linter"]}, "entry_selection": True, "paths": ["src/**"], "entry_paths": {"checks/test_a.py": ["src/**"], "checks/test_b.py": ["src/**"]}},
    {"name": "web", "entries": ["checks/test_c.py"], "command": ["node"], "setups": ["node"], "paths": ["web/**"]},
    {"name": "desktop", "entries": ["checks/test_win.py"], "os": "windows", "command": ["python"], "setups": ["python"], "paths": ["src/**"]},
]


def document(**keys) -> dict:
    return {"always": [], "setups": SETUPS, "suites": SUITES} | keys


def parse(raw: dict) -> suite_selection.SuiteList:
    return suite_selection.parse_suite_list(json.dumps(raw))


class SetupDataTests(unittest.TestCase):
    def test_setups_and_jobs_parse_and_render_back_the_same(self) -> None:
        raw = {"always": [], "jobs": {"linux": {"runner": "ubuntu-24.04", "fetch-depth": 0}, "windows": {"runner": "windows-2022"}},
               "setups": SETUPS, "suites": SUITES}
        text = json.dumps(raw, indent=2) + "\n"
        parsed = suite_selection.parse_suite_list(text)
        self.assertEqual(suite_selection.render_suite_list(parsed), text)
        self.assertEqual([setup.name for setup in parsed.setups], ["python", "node", "linter"], "the setups keep the order the suite list states")
        python, node, linter = parsed.setups
        self.assertEqual((python.script, python.recorded, python.tool, python.version), ("ci/setups/python.py", False, "python", "3.13"))
        self.assertEqual((node.tool, node.version, node.version_file), ("node", None, "ci/pins.json#/node/version"))
        self.assertEqual((linter.recorded, linter.tool), (True, None), "a setup runs under the recording unless it states otherwise")

    def test_a_suite_list_without_setups_and_jobs_states_none_and_gets_the_default_jobs(self) -> None:
        parsed = parse({"always": [], "suites": [{"name": "a", "entries": ["x.py"], "setups": ["anything"]}]})
        self.assertIsNone(parsed.setups, "a suite list without the key keeps its setups in its own workflow")
        self.assertEqual(parsed.job("linux"), {"runner": "ubuntu-26.04", "fetch-depth": 1})
        self.assertEqual(parsed.job("windows"), {"runner": "windows-2025", "fetch-depth": 1})

    def test_a_stated_job_keeps_the_defaults_of_what_it_leaves_out(self) -> None:
        parsed = parse(document(jobs={"linux": {"fetch-depth": 0}}))
        self.assertEqual(parsed.job("linux"), {"runner": "ubuntu-26.04", "fetch-depth": 0})
        self.assertEqual(parsed.job("windows"), {"runner": "windows-2025", "fetch-depth": 1})

    def test_a_runner_label_is_one_of_the_github_hosted_families(self) -> None:
        for label in ("ubuntu-26.04", "ubuntu-24.04-arm", "ubuntu-slim", "windows-2025", "windows-11-arm", "macos-26", "macos-15-intel"):
            with self.subTest(accepted=label):
                self.assertEqual(label, parse(document(jobs={"linux": {"runner": label}})).job("linux")["runner"])
        message = "runner must be a label of a GitHub-hosted runner of the ubuntu-, windows- or macos- family, such as ubuntu-26.04"
        for label in ("self-hosted", "linux", "ubuntu", "ubuntu-", "Ubuntu-26.04", "my-ubuntu-26.04", "ubuntu_26.04", "ubuntu-26.04 self-hosted", "ubuntu-26.04\nself-hosted", "ubuntu-26.04\n",
                      "${{ inputs.runner }}", "gpu-runner", ["ubuntu-26.04"], 26):
            for system in ("linux", "windows"):
                with self.subTest(refused=label, system=system):
                    with self.assertRaises(suite_selection.SuiteListError) as raised:
                        parse(document(jobs={system: {"runner": label}}))
                    self.assertIn(f"jobs ({system}): {message}", str(raised.exception))

    def test_invalid_setups_and_jobs_fail_and_name_the_setup(self) -> None:
        def setups(**changed) -> dict:
            return document(setups=SETUPS | changed)

        for broken, message in (
            (document(setups=[]), "setups must be an object"),
            (setups(Bad={"script": "a.sh"}), "setups: the name Bad must be lowercase letters, digits and hyphens"),
            (setups(linter={}), "setups (linter): script must be a repository path that ends in .py, .sh or .ps1"),
            (setups(linter={"script": "ci/setups/linter.exe"}), "setups (linter): script must be a repository path that ends in .py, .sh or .ps1"),
            (setups(linter={"script": "a.sh", "extra": 1}), "setups (linter): unknown keys extra"),
            (setups(linter={"script": "a.sh", "recorded": "no"}), "setups (linter): recorded must be true or false"),
            (setups(linter={"script": "a.sh", "tool": "lua", "version": "5.4"}), "setups (linter): tool must be one of python, node, dotnet"),
            (setups(linter={"script": "a.sh", "tool": "python"}), "setups (linter): a tool needs either version or version-file"),
            (setups(linter={"script": "a.sh", "tool": "python", "version": "3.13", "version-file": "a.json#/b"}),
             "setups (linter): a tool needs either version or version-file"),
            (setups(linter={"script": "a.sh", "version": "1"}), "setups (linter): version and version-file go with tool"),
            (setups(linter={"script": "a.sh", "tool": "node", "version-file": "pins.json"}),
             "setups (linter): version-file must be <JSON file>#/<key>/<key>"),
            (setups(linter={"script": "a.sh", "tool": "python", "version": "3.12"}),
             "setups (linter): the tool python has the version 3.12, but the setup python gives it 3.13; a suite list holds one version per tool"),
            (document(setups={"python": SETUPS["python"], "node": SETUPS["node"]}),
             "suites[1] (unit): entry_setups (checks/test_b.py) names the setup linter, which setups lacks"),
            (document(jobs={"macos": {}}), "jobs: macos must be one of linux, windows"),
            (document(jobs={"linux": {"image": "x"}}), "jobs (linux): unknown keys image"),
            (document(jobs={"linux": {"runner": ""}}), "jobs (linux): runner must be a label of a GitHub-hosted runner of the ubuntu-, windows- or macos- family, such as ubuntu-26.04"),
            (document(jobs={"linux": {"fetch-depth": -1}}), "jobs (linux): fetch-depth must be a whole number of 0 or more"),
        ):
            with self.subTest(message=message):
                with self.assertRaises(suite_selection.SuiteListError) as raised:
                    parse(broken)
                self.assertIn(message, str(raised.exception))


class FixtureRepository(unittest.TestCase):
    """A repository whose suite list states its setups, with a commit to change from."""

    def setUp(self) -> None:
        self.temporary = temporary_support.Directory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        git(self.root, "init", "-q", "-b", "main")
        for name in ("a", "b", "c", "win"):
            write(self.root, f"checks/test_{name}.py")
        for name in ("python.py", "node.py", "linter.sh"):
            write(self.root, f"ci/setups/{name}")
        write(self.root, "ci/pins.json", json.dumps({"node": {"version": "24.1.0"}}))
        write(self.root, "src/app.py")
        self.write_suites(document())
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "base")
        self.base = git(self.root, "rev-parse", "HEAD")

    def write_suites(self, raw: dict) -> None:
        write(self.root, "suites.json", json.dumps(raw, indent=2) + "\n")

    def commit(self) -> str:
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "change")
        return git(self.root, "rev-parse", "HEAD")

    def select(self, *arguments: str) -> tuple[int, dict[str, str], str]:
        output = self.root / "outputs.txt"
        output.unlink(missing_ok=True)
        stream = io.StringIO()
        with redirect_stdout(stream), redirect_stderr(stream):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "suites.json"),
                                         "--json-output", str(output), *arguments])
        lines = output.read_text(encoding="utf-8").splitlines() if output.is_file() else []
        return code, dict(line.split("=", 1) for line in lines), stream.getvalue()

    def outputs(self, *arguments: str) -> dict[str, str]:
        code, outputs, text = self.select(*arguments)
        self.assertEqual(code, 0, text)
        return outputs

    def running(self, *arguments: str) -> list[str]:
        """The entry files that a selection runs, Linux and Windows."""
        outputs = self.outputs(*arguments)
        entries = [item["entry"] for shard in json.loads(outputs["shards"]) for item in shard["entries"]]
        return sorted(entries + [item["entry"] for item in json.loads(outputs["windows"] or '{"entries": []}')["entries"]])


class SelectOutputTests(FixtureRepository):
    def test_each_job_gets_the_tools_of_its_setups_with_their_versions(self) -> None:
        outputs = self.outputs("--full")
        shards = json.loads(outputs["shards"])
        self.assertEqual(len(shards), 1)
        self.assertEqual(shards[0]["setups"], ["python", "linter", "node"])
        self.assertEqual(shards[0]["tools"], {"python": "3.13", "node": "24.1.0"}, "node takes its version from the file that the setup names")
        self.assertEqual(json.loads(outputs["windows"])["tools"], {"python": "3.13"})

    def test_select_gives_the_jobs_with_their_runner_and_fetch_depth(self) -> None:
        self.assertEqual(json.loads(self.outputs("--full")["jobs"]),
                         {"linux": {"runner": "ubuntu-26.04", "fetch-depth": 1}, "windows": {"runner": "windows-2025", "fetch-depth": 1}})
        self.write_suites(document(jobs={"linux": {"runner": "ubuntu-24.04", "fetch-depth": 0}}))
        self.assertEqual(json.loads(self.outputs("--full")["jobs"])["linux"], {"runner": "ubuntu-24.04", "fetch-depth": 0})

    def test_a_suite_list_without_setups_gives_no_tools(self) -> None:
        self.write_suites({"always": [], "suites": SUITES})
        self.assertNotIn("tools", json.loads(self.outputs("--full")["shards"])[0])

    def test_a_setup_script_or_version_file_outside_the_tracked_files_fails_select(self) -> None:
        (self.root / "ci/setups/linter.sh").unlink()
        git(self.root, "add", "-A")
        code, _, text = self.select("--full")
        self.assertEqual(code, 2)
        self.assertIn("setups (linter): the script ci/setups/linter.sh is no tracked file", text)

    def test_a_version_file_without_the_named_key_fails_select(self) -> None:
        write(self.root, "ci/pins.json", json.dumps({"node": {}}))
        code, _, text = self.select("--full")
        self.assertEqual(code, 2)
        self.assertIn("setups (node): ci/pins.json holds no text at /node/version", text)


class SetupChangeTests(FixtureRepository):
    def changed(self) -> list[str]:
        return self.running("--base", self.base, "--head", self.commit(), "--compare", "direct")

    def test_a_changed_setup_script_runs_the_entry_files_that_state_the_setup(self) -> None:
        write(self.root, "ci/setups/linter.sh", "echo changed\n")
        self.assertEqual(self.changed(), ["checks/test_b.py"])

    def test_a_changed_script_of_a_setup_outside_the_recording_runs_its_entry_files_on_both_systems(self) -> None:
        write(self.root, "ci/setups/python.py", "print('changed')\n")
        self.assertEqual(self.changed(), ["checks/test_a.py", "checks/test_b.py", "checks/test_win.py"])

    def test_a_changed_version_file_runs_the_entry_files_that_state_its_setup(self) -> None:
        write(self.root, "ci/pins.json", json.dumps({"node": {"version": "24.2.0"}}))
        self.assertEqual(self.changed(), ["checks/test_c.py"])

    def test_a_changed_setup_definition_runs_the_entry_files_that_state_the_setup(self) -> None:
        self.write_suites(document(setups=SETUPS | {"python": SETUPS["python"] | {"version": "3.14"}}))
        self.assertEqual(self.changed(), ["checks/test_a.py", "checks/test_b.py", "checks/test_win.py"])

    def test_a_changed_job_runs_the_entry_files_of_its_system(self) -> None:
        self.write_suites(document(jobs={"windows": {"runner": "windows-2022"}}))
        self.assertEqual(self.changed(), ["checks/test_win.py"])

    def test_a_change_to_another_file_runs_nothing(self) -> None:
        write(self.root, "notes.txt", "x\n")
        self.assertEqual(self.changed(), [])


PINS = {"node": {"version": "24.9.0"},
        "tools": {"gitleaks": {"repository": "gitleaks/gitleaks", "version": "8.30.1",
                               "platforms": {"linux-x64": {"sha256": "a" * 64}, "windows-x64": {"sha256": "b" * 64}}}}}
PINNED_SETUPS = {"python": SETUPS["python"], "linter": SETUPS["linter"]}
PINNED_SUITES = [
    {"name": "unit", "entries": ["checks/test_a.py", "checks/test_b.py"], "command": ["python3"], "setups": ["python"],
     "entry_setups": {"checks/test_b.py": ["linter", "gitleaks"]}, "entry_selection": True, "paths": ["src/**"],
     "entry_paths": {"checks/test_a.py": ["src/**"], "checks/test_b.py": ["src/**"]}},
    {"name": "web", "entries": ["checks/test_c.py"], "command": ["node"], "setups": ["node"], "paths": ["web/**"]},
    {"name": "desktop", "entries": ["checks/test_win.py"], "os": "windows", "command": ["python"], "setups": ["python", "gitleaks"], "paths": ["src/**"]},
]
BOTH = ("--pinned-tool", "node", "--pinned-tool", "gitleaks")


class PinnedToolTests(FixtureRepository):
    """The pinned tools that the configuration file names: setups that the suite jobs give at the version of the pins file."""

    def setUp(self) -> None:
        super().setUp()
        self.pins = self.root.parent / f"{self.root.name}-pins.json"
        self.addCleanup(lambda: self.pins.unlink(missing_ok=True))
        self.write_pins(PINS)
        self.write_suites(self.pinned())
        self.base = self.commit()

    @staticmethod
    def pinned(setups: dict | None = None, suites: list | None = None) -> dict:
        return {"always": [], "setups": PINNED_SETUPS if setups is None else setups, "suites": PINNED_SUITES if suites is None else suites}

    def write_pins(self, pins: dict) -> None:
        self.pins.write_text(json.dumps(pins), encoding="utf-8")

    def select(self, *arguments: str) -> tuple[int, dict[str, str], str]:
        return super().select("--pins", str(self.pins), *arguments)

    def refused(self, *arguments: str) -> str:
        code, outputs, text = self.select("--full", *arguments)
        self.assertEqual((code, outputs), (2, {}), text)
        return text

    def test_each_job_gets_the_pinned_tools_that_its_entry_files_state(self) -> None:
        outputs = self.outputs("--full", *BOTH)
        shards = json.loads(outputs["shards"])
        self.assertEqual(len(shards), 1)
        self.assertEqual(shards[0]["setups"], ["python", "linter", "gitleaks", "node"])
        self.assertEqual(shards[0]["pinned"], ["node", "gitleaks"])
        self.assertEqual(shards[0]["tools"], {"python": "3.13", "node": "24.9.0"}, "the job installs Node.js at node.version of the pins file")
        windows = json.loads(outputs["windows"])
        self.assertEqual((windows["pinned"], windows["tools"]), (["gitleaks"], {"python": "3.13"}),
                         "a job installs only the pinned tools that one of its entry files states")

    def test_a_job_whose_entry_files_state_no_pinned_tool_gets_none(self) -> None:
        write(self.root, "web/page.js", "x\n")
        outputs = self.outputs("--base", self.base, "--head", self.commit(), "--compare", "direct", *BOTH)
        (shard,) = json.loads(outputs["shards"])
        self.assertEqual((shard["setups"], shard["pinned"], shard["tools"]), (["node"], ["node"], {"node": "24.9.0"}))
        self.write_suites(self.pinned(suites=[PINNED_SUITES[0] | {"entry_setups": {}}]))
        git(self.root, "add", "-A")
        (shard,) = json.loads(self.outputs("--full", *BOTH)["shards"])
        self.assertNotIn("pinned", shard)
        self.assertEqual(shard["tools"], {"python": "3.13"})

    def test_without_pinned_tools_the_outputs_hold_no_trace_of_them(self) -> None:
        self.write_suites(document())
        git(self.root, "add", "-A")
        named, plain = self.outputs("--full", "--pinned-tool", "gitleaks"), self.outputs("--full")
        self.assertEqual(named, plain, "a pinned tool that no entry file states changes no output")
        self.assertEqual(set(json.loads(plain["shards"])[0]), {"shard", "estimate", "setups", "tools", "entries"})
        self.assertEqual(set(json.loads(plain["windows"])), {"estimate", "setups", "tools", "entries"})

    def test_a_setup_of_the_suite_list_named_after_a_pinned_tool_fails_select(self) -> None:
        self.write_suites(self.pinned(setups=PINNED_SETUPS | {"gitleaks": {"script": "ci/setups/linter.sh"}}))
        git(self.root, "add", "-A")
        self.assertIn("setups (gitleaks): pinned-tools names gitleaks, so the suite jobs give this setup; remove it from setups", self.refused(*BOTH))
        self.assertEqual(self.select("--full", "--pinned-tool", "node")[0], 0, "without the name in pinned-tools the setup is the suite list's own")

    def test_a_setup_that_installs_node_beside_the_pinned_node_fails_select(self) -> None:
        self.write_suites(self.pinned(setups=PINNED_SETUPS | {"packages": {"script": "ci/setups/linter.sh", "tool": "node", "version": "24.1.0"}}))
        git(self.root, "add", "-A")
        self.assertIn("setups (packages): the tool node stands beside node in pinned-tools; a suite list holds one version per tool", self.refused(*BOTH))

    def test_a_stated_pinned_tool_that_nothing_names_fails_select_as_an_undefined_setup(self) -> None:
        self.assertIn("suites[1] (unit): entry_setups (checks/test_b.py) names the setup gitleaks, which setups lacks", self.refused("--pinned-tool", "node"))
        self.assertIn("suites[2] (web): setups names the setup node, which setups lacks", self.refused("--pinned-tool", "gitleaks"))
        self.assertIn("names the setup gitleaks, which setups lacks", self.refused())

    def test_a_name_outside_the_pinned_tools_is_refused(self) -> None:
        stream = io.StringIO()
        with redirect_stderr(stream), self.assertRaises(SystemExit):
            suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "suites.json"), "--full", "--pinned-tool", "actionlint"])
        self.assertIn("invalid choice: 'actionlint'", stream.getvalue())

    def test_a_pins_file_without_the_version_fails_select(self) -> None:
        self.write_pins({"node": {"version": "24.9.0"}, "tools": {}})
        self.assertIn("holds no version of gitleaks", self.refused(*BOTH))
        code, _, text = FixtureRepository.select(self, "--full", *BOTH)
        self.assertEqual(code, 2)
        self.assertIn("select needs --pins, the pins file, with --pinned-tool", text)

    def test_a_moved_pin_alone_selects_no_unit(self) -> None:
        write(self.root, "notes.txt", "x\n")
        head = self.commit()
        before = self.outputs("--base", self.base, "--head", head, "--compare", "direct", *BOTH)
        self.write_pins({"node": {"version": "25.0.0"}, "tools": {"gitleaks": PINS["tools"]["gitleaks"] | {"version": "9.0.0"}}})
        after = self.outputs("--base", self.base, "--head", head, "--compare", "direct", *BOTH)
        self.assertEqual((before["shards"], before["windows"]), ("[]", ""))
        self.assertEqual(after, before)

    def test_a_setup_that_becomes_a_pinned_tool_runs_the_entry_files_that_state_it(self) -> None:
        self.write_suites(self.pinned(setups=PINNED_SETUPS | {"gitleaks": {"script": "ci/setups/linter.sh"}}))
        self.base = self.commit()
        self.write_suites(self.pinned())
        self.assertEqual(self.running("--base", self.base, "--head", self.commit(), "--compare", "direct", *BOTH),
                         ["checks/test_b.py", "checks/test_win.py"])


if __name__ == "__main__":
    unittest.main()
