"""The suite list states each suite's command and setups, and the validation workflow runs every stated setup in the job
of the suite's operating system, the pinned tools of the hygiene suite among them."""

from __future__ import annotations

import json
import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, read_workflow

VALIDATE = REPOSITORY_ROOT / ".github" / "workflows" / "validate.yml"
SUITES = REPOSITORY_ROOT / "tests" / "suites.json"
SETUP_GATES = {"linux": r"contains\(matrix\.shard\.setups, '([a-z0-9-]+)'\)", "windows": r"contains\(fromJSON\(env\.SETUPS\), '([a-z0-9-]+)'\)"}


def suites() -> list[dict]:
    return json.loads(SUITES.read_text(encoding="utf-8"))["suites"]


def stated_setups(suite: dict) -> set[str]:
    setups = set(suite["setups"])
    for own in suite.get("entry_setups", {}).values():
        setups |= set(own)
    return setups


class SuiteSetupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, workflow = read_workflow(VALIDATE)
        self.jobs = {"linux": workflow["jobs"]["linux"], "windows": workflow["jobs"]["windows"]}

    def gated_setups(self, system: str) -> set[str]:
        found: set[str] = set()
        for step in self.jobs[system]["steps"]:
            found |= set(re.findall(SETUP_GATES[system], str(step.get("if", ""))))
        return found

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

    def test_every_stated_setup_has_a_step_in_the_job_of_the_suite_s_operating_system(self) -> None:
        for suite in suites():
            system = suite.get("os", "linux")
            with self.subTest(suite=suite["name"]):
                self.assertLessEqual(stated_setups(suite), self.gated_setups(system))

    def test_every_setup_step_serves_a_stated_setup(self) -> None:
        for system in ("linux", "windows"):
            stated = set().union(*(stated_setups(suite) for suite in suites() if suite.get("os", "linux") == system))
            with self.subTest(job=system):
                self.assertEqual(stated, self.gated_setups(system))

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
        self.assertIn('--destination "$RUNNER_TEMP/setups/$setup"', self.text)
        self.assertIn('provide --setup "$setup" --path "$RUNNER_TEMP/setups/$setup"', self.text)
        self.assertNotIn("HYGIENE_TOOLS", self.text)

    def test_node_reads_its_pinned_version_under_the_recording(self) -> None:
        self.assertEqual(1, self.text.count("exec --setup node -- "))
        self.assertEqual(1, self.text.count("node-version: ${{ steps.node.outputs.version }}"))


if __name__ == "__main__":
    unittest.main()
