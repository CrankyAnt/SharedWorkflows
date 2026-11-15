"""The suite list states each suite's command and setups, and defines every stated setup as data with one script: the
tools that a pinned action installs, the pinned tools of the hygiene suite and the commitlint toolchain."""

from __future__ import annotations

import json
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, tracked_files

SUITES = REPOSITORY_ROOT / "tests" / "suites.json"
SCRIPTS = "tests/setups"
PINS = "actions/repository-hygiene/pins.json#/node/version"
PINNED_TOOLS = ("actionlint", "zizmor", "gitleaks", "reuse", "ruff", "mypy")


def suite_list() -> dict:
    return json.loads(SUITES.read_text(encoding="utf-8"))


def suites() -> list[dict]:
    return suite_list()["suites"]


def stated_setups(suite: dict) -> set[str]:
    setups = set(suite["setups"])
    for own in suite.get("entry_setups", {}).values():
        setups |= set(own)
    return setups


def script(name: str) -> str:
    return (REPOSITORY_ROOT / SCRIPTS / name).read_text(encoding="utf-8")


class SuiteSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.setups = suite_list()["setups"]

    def test_every_suite_states_its_command_and_its_setups(self) -> None:
        for suite in suites():
            with self.subTest(suite=suite["name"]):
                self.assertIsInstance(suite.get("command"), list)
                self.assertTrue(suite["command"])
                self.assertIsInstance(suite.get("setups"), list)

    def test_a_suite_s_command_runs_on_the_interpreter_of_its_job(self) -> None:
        for suite in suites():
            with self.subTest(suite=suite["name"]):
                program = suite["command"][0]
                if suite.get("os") == "windows":
                    self.assertEqual("python", program, "the Windows job has python, not python3")
                else:
                    self.assertIn(program, ("python3", "pwsh"))
                if program.startswith("python"):
                    self.assertIn("python", stated_setups(suite), "the pinned Python runs the suite")

    def test_every_stated_setup_is_defined_and_every_defined_setup_is_stated(self) -> None:
        stated = set().union(*(stated_setups(suite) for suite in suites()))
        self.assertEqual(stated, set(self.setups))

    def test_every_setup_has_one_tracked_script_in_the_folder_of_the_setups_and_every_script_there_serves_one(self) -> None:
        tracked = {path for path in tracked_files(REPOSITORY_ROOT, [SCRIPTS])}
        self.assertEqual(tracked, {setup["script"] for setup in self.setups.values()})

    def test_the_jobs_take_the_default_runner_labels_and_the_linux_shards_the_whole_history(self) -> None:
        self.assertEqual({"linux": {"fetch-depth": 0}}, suite_list()["jobs"], "entry files of the Linux suites read older commits of this checkout")

    def test_python_node_and_dotnet_are_installed_by_the_job_s_actions_and_provided_outside_the_recording(self) -> None:
        self.assertEqual({"script": f"{SCRIPTS}/python.py", "recorded": False, "tool": "python", "version": "3.13"}, self.setups["python"])
        self.assertEqual({"script": f"{SCRIPTS}/node.py", "recorded": False, "tool": "node", "version-file": PINS}, self.setups["node"])
        self.assertEqual({"script": f"{SCRIPTS}/dotnet.py", "recorded": False, "tool": "dotnet",
                          "version-file": "actions/recording/etw-consumer/global.json#/sdk/version"}, self.setups["dotnet"])
        python = script("python.py")
        self.assertIn('provides.write(f"python={interpreter}\\n")', python, "the set-up Python gets the lookup report")
        self.assertIn('interpreter.parent / "Scripts"', python, "on Windows the entry files also get the interpreter's Scripts directory")
        self.assertIn("variable=DOTNET_ROOT={root}", script("dotnet.py"))
        self.assertEqual(["python", "dotnet", "node"], list(self.setups)[:3], "the setups outside the recording come first in the suite list")

    def test_the_hygiene_entries_get_the_pinned_tools_of_their_setups_on_path(self) -> None:
        hygiene = next(suite for suite in suites() if suite["name"] == "hygiene")
        self.assertEqual({
            "tests/test_hygiene_actionlint.py": ["actionlint"],
            "tests/test_hygiene_gitleaks.py": ["gitleaks"],
            "tests/test_hygiene_reuse.py": ["reuse"],
            "tests/test_hygiene_zizmor.py": ["zizmor"],
            "tests/test_hygiene_zizmor_configuration.py": ["zizmor"],
            "tests/test_hygiene_zizmor_surface.py": ["zizmor"],
        }, hygiene["entry_setups"])
        for name in PINNED_TOOLS:
            self.assertEqual({"script": f"{SCRIPTS}/pinned-tool.sh"}, self.setups[name], "a pinned tool installs under the recording")
        text = script("pinned-tool.sh")
        self.assertIn('tools=(--tool "$SETUP_NAME")', text, "the setup's name is the tool's")
        self.assertIn('if [[ "$SETUP_NAME" == actionlint ]]; then tools+=(--tool shellcheck); fi', text)
        self.assertIn('install_tools.py --pins actions/repository-hygiene/pins.json --platform linux-x64 --destination "$SETUP_DIRECTORY" "${tools[@]}"', text)
        self.assertIn('echo "path=$SETUP_DIRECTORY" >> "$SETUP_PROVIDES"', text)
        self.assertNotIn("HYGIENE_TOOLS", text)

    def test_commitlint_installs_the_locked_toolchain_with_the_npm_of_the_pinned_node_under_the_recording(self) -> None:
        self.assertEqual({"script": f"{SCRIPTS}/commitlint.sh", "tool": "node", "version-file": PINS}, self.setups["commitlint"])
        text = script("commitlint.sh")
        self.assertIn('export PATH="$SETUP_TOOL:$PATH"', text)
        self.assertIn('npm ci --ignore-scripts --no-audit --no-fund --userconfig /dev/null --prefix "$SETUP_DIRECTORY"', text)
        self.assertIn('echo "variable=PUBLICATION_CHECKS_TOOLS=$SETUP_DIRECTORY" >> "$SETUP_PROVIDES"', text)
        publication = next(suite for suite in suites() if suite["name"] == "publication-checks")
        for entry, setups in publication["entry_setups"].items():
            if "commitlint" in setups:
                self.assertIn("node", setups, f"{entry} runs the toolchain on the pinned Node.js")

    def test_no_setup_script_changes_the_job_s_environment_or_installs_into_the_system(self) -> None:
        for path in sorted(tracked_files(REPOSITORY_ROOT, [SCRIPTS])):
            text = (REPOSITORY_ROOT / path).read_text(encoding="utf-8")
            for forbidden in ("GITHUB_PATH", "GITHUB_ENV", "apt-get", "sudo "):
                with self.subTest(script=path, text=forbidden):
                    self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
