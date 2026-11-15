"""The free job, the part ``custom``, in the real schema: which setups ``plan`` knows, what it gives the job for the
named setups, and that the job's command ends with the exit status of the repository's script."""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest

from tests.ci_policy_support import read_workflow
from tests.pipeline_fixtures import CONFIGURATION, REPOSITORY_ROOT, SCHEMAS, PipelineFixture

CUSTOM = "custom"
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "pipeline.yml"
FIXTURE_SCRIPTS = "tests/fixtures/custom"


def configuration(setups: str, script: str = "ci/check.sh") -> str:
    """A configuration file with the free job; ``setups`` is the list as the file states it, such as ``[dotnet-8]``."""
    return f"version: 1\nparts:\n  custom:\n    setups: {setups}\n    script: {script}\n"


class SetupTests(PipelineFixture):
    def setUp(self) -> None:
        super().setUp()
        self.schemas = SCHEMAS

    def test_a_started_plan_with_a_setup_name_that_this_repository_does_not_know_fails_with_the_list_of_known_setups(self) -> None:
        self.commit({CONFIGURATION: configuration("[python-3.14, ruby-3.4]")})
        outcome = self.start_program("plan")
        self.assertEqual(1, outcome.status)
        self.assertNotIn("plan", outcome.outputs)
        known = "'python-3.11', 'python-3.12', 'python-3.13', 'python-3.14', 'dotnet-8' or 'dotnet-10'"
        for text in (outcome.summary, outcome.stdout):
            self.assertIn(f"parts.custom.setups[1]: must be {known}, found 'ruby-3.4'", text)

    def test_the_plan_gives_the_job_the_tool_and_the_version_of_each_named_setup_in_the_listed_order(self) -> None:
        python, dotnet = {"tool": "python", "version": "3.14"}, {"tool": "dotnet", "version": "10.0.x"}
        every = "[python-3.11, python-3.12, python-3.13, python-3.14, dotnet-8, dotnet-10]"
        for setups, names, expected in (
            ("[python-3.14]", ["python-3.14"], [python]),
            ("[dotnet-10, python-3.14]", ["dotnet-10", "python-3.14"], [dotnet, python]),
            ("[python-3.14, dotnet-10]", ["python-3.14", "dotnet-10"], [python, dotnet]),
            ("[python-3.14, python-3.11]", ["python-3.14", "python-3.11"], [python, {"tool": "python", "version": "3.11"}]),
            (every, every[1:-1].split(", "), [{"tool": "python", "version": "3.11"}, {"tool": "python", "version": "3.12"}, {"tool": "python", "version": "3.13"},
                                              python, {"tool": "dotnet", "version": "8.0.x"}, dotnet]),
            ("[]", [], []),
        ):
            with self.subTest(setups=setups):
                self.commit({CONFIGURATION: configuration(setups)})
                outcome = self.plan()
                self.assertEqual(0, outcome.status, outcome.summary)
                part = outcome.plan["parts"][CUSTOM]
                self.assertEqual((True, {"setups": names, "script": "ci/check.sh"}), (part["run"], part["config"]))
                self.assertEqual(expected, part["setups"])

    def test_the_free_job_may_name_its_request_and_what_it_waits_for_as_a_temporary_entry_does(self) -> None:
        fragment = json.loads((SCHEMAS / "v1" / "parts" / "custom.json").read_text(encoding="utf-8"))["configuration"]
        self.assertEqual(["script"], fragment["required"], "a free job without a request states neither setting")
        self.assertEqual(("#/$defs/request", "#/$defs/waits-for"), (fragment["properties"]["request"]["$ref"], fragment["properties"]["waits-for"]["$ref"]),
                         "the definitions of a temporary entry of the register")
        for waits_for in ("python-licences", "python-test.coverage"):
            with self.subTest(waits_for=waits_for):
                self.commit({CONFIGURATION: f"version: 1\nparts:\n  custom:\n    script: ci/check.sh\n    request: 12\n    waits-for: {waits_for}\n"})
                outcome = self.plan()
                self.assertEqual(0, outcome.status, outcome.summary)
                part = outcome.plan["parts"][CUSTOM]
                self.assertEqual((True, {"script": "ci/check.sh", "request": 12, "waits-for": waits_for}), (part["run"], part["config"]))
        for settings, problem in (("    request: twelve\n    waits-for: python-licences\n", "parts.custom.request"), ("    request: 12\n    waits-for: Python Licences\n", "parts.custom.waits-for")):
            with self.subTest(problem=problem):
                self.commit({CONFIGURATION: f"version: 1\nparts:\n  custom:\n    script: ci/check.sh\n{settings}"})
                outcome = self.plan()
                self.assertEqual(1, outcome.status)
                self.assertIn(problem, outcome.summary)

    def test_the_free_job_may_name_the_github_hosted_runner_of_its_job(self) -> None:
        for label in ("windows-2025", "macos-15", "ubuntu-26.04"):
            with self.subTest(label=label):
                self.commit({CONFIGURATION: configuration("[python-3.14]") + f"    runner-label: {label}\n"})
                outcome = self.plan()
                self.assertEqual(0, outcome.status, outcome.summary)
                part = outcome.plan["parts"][CUSTOM]
                self.assertEqual((True, {"setups": ["python-3.14"], "script": "ci/check.sh", "runner-label": label}), (part["run"], part["config"]))

    def test_the_runner_of_the_free_job_is_one_github_hosted_label_and_no_other(self) -> None:
        for value in ("self-hosted", "windows", "linux-arm64", "[windows-2025]", "[self-hosted, windows]", "''"):
            with self.subTest(value=value):
                self.commit({CONFIGURATION: configuration("[python-3.14]") + f"    runner-label: {value}\n"})
                outcome = self.plan()
                self.assertEqual(1, outcome.status, outcome.summary)
                self.assertNotIn("plan", outcome.outputs)
                self.assertIn("parts.custom.runner-label", outcome.summary)

    def test_a_free_job_without_setups_runs_its_script_with_no_setup(self) -> None:
        self.commit({CONFIGURATION: "version: 1\nparts:\n  custom:\n    script: ci/check.sh\n"})
        outcome = self.plan()
        self.assertEqual(0, outcome.status, outcome.summary)
        part = outcome.plan["parts"][CUSTOM]
        self.assertEqual((True, {"script": "ci/check.sh"}, []), (part["run"], part["config"], part["setups"]))

    def test_the_free_job_needs_a_script_and_takes_no_other_setting_than_setups_and_paths(self) -> None:
        for text, expected in (("version: 1\nparts:\n  custom: {}\n", ["parts.custom.script: is required"]),
                               (configuration("[python-3.14]") + "    secret: TOKEN\n", ["parts.custom.secret: is not a setting of the part custom"]),
                               (configuration("[python-3.14]") + "    setup: python-3.14\n", ["parts.custom.setup: is not a setting of the part custom"]),
                               (configuration("[python-3.14]", "''"), ["parts.custom.script: is empty"]),
                               (configuration("[python-3.14]", "/opt/check.sh"), ["parts.custom.script: does not match the pattern"]),
                               (configuration("[python-3.14]", "../outside.sh"), ["parts.custom.script: does not match the pattern"]),
                               (configuration("[python-3.14]", "ci/../../outside.sh"), ["parts.custom.script: does not match the pattern"]),
                               (configuration("[python-3.14, dotnet-8, python-3.14]"), ["parts.custom.setups: holds 'python-3.14' twice"]),
                               (configuration("python-3.14"), ["parts.custom.setups: expected a list, found a string"])):
            with self.subTest(configuration=text):
                self.commit({CONFIGURATION: text})
                outcome = self.plan()
                self.assertEqual(1, outcome.status)
                for line in expected:
                    self.assertIn(line, outcome.summary)

    def test_the_free_job_that_names_paths_runs_only_on_a_change_under_them(self) -> None:
        base = self.commit({CONFIGURATION: configuration("[dotnet-10]") + "    paths: [src/]\n"})
        other = self.commit({"README.md": "A consumer.\n"})
        self.assertFalse(self.plan("--base", base, "--head", other).plan["parts"][CUSTOM]["run"])
        source = self.commit({"src/Program.cs": "// changed\n"})
        self.assertTrue(self.plan("--base", other, "--head", source).plan["parts"][CUSTOM]["run"])

    def test_a_repository_that_does_not_state_the_free_job_does_not_run_it(self) -> None:
        self.commit({CONFIGURATION: "version: 1\n"})
        part = self.plan().plan["parts"][CUSTOM]
        self.assertEqual((False, "switched off", {}), (part["run"], part["reason"], part["config"]))
        self.assertNotIn("setups", part)


class AssembledSetupTests(unittest.TestCase):
    def test_the_assembled_schema_names_the_known_setups_for_a_reader(self) -> None:
        schema = json.loads((SCHEMAS / "v1" / "ci-config.schema.json").read_text(encoding="utf-8"))
        setups = json.loads((SCHEMAS / "v1" / "setups.json").read_text(encoding="utf-8"))
        self.assertEqual(list(setups), schema["$defs"]["setup"]["enum"])
        custom = schema["properties"]["parts"]["properties"][CUSTOM]
        self.assertEqual({"$ref": "#/$defs/setup"}, schema["$defs"]["setups"]["items"])
        self.assertEqual({"$ref": "#/$defs/setups"}, custom["properties"]["setups"])
        self.assertEqual({"$ref": "#/$defs/script"}, custom["properties"]["script"])
        self.assertEqual(["script"], custom["required"])

    def test_the_assembled_schema_states_the_runner_of_the_free_job_and_its_default(self) -> None:
        schema = json.loads((SCHEMAS / "v1" / "ci-config.schema.json").read_text(encoding="utf-8"))
        runner = schema["properties"]["parts"]["properties"][CUSTOM]["properties"]["runner-label"]
        self.assertEqual("#/$defs/runner-label", runner["$ref"])
        self.assertIn("ubuntu-26.04", runner["description"], "the default for a reader of the schema")


@unittest.skipUnless(shutil.which("bash"), "the job's command runs in Bash")
class ScriptCommandTests(unittest.TestCase):
    """The command of the job's last step, run as the job runs it, on the fixture scripts of this repository's own CI."""

    def run_script(self, script: str) -> subprocess.CompletedProcess:
        step = read_workflow(WORKFLOW)[1]["jobs"][CUSTOM]["steps"][-1]
        self.assertEqual(["SCRIPT"], list(step["env"]))
        return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]], cwd=REPOSITORY_ROOT, capture_output=True, text=True, encoding="utf-8",
                              env={**__import__("os").environ, "SCRIPT": script})

    def test_a_script_that_fails_ends_the_command_with_its_exit_status(self) -> None:
        completed = self.run_script(f"{FIXTURE_SCRIPTS}/fail.sh")
        self.assertEqual(3, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn("The failing fixture script of the free job ran.", completed.stdout)

    def test_a_script_that_does_not_exist_fails_the_command(self) -> None:
        self.assertNotEqual(0, self.run_script(f"{FIXTURE_SCRIPTS}/absent.sh").returncode)

    def test_a_script_whose_name_starts_with_a_dash_runs_as_a_script_and_not_as_an_option(self) -> None:
        completed = self.run_script("-c")
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("-c", completed.stderr)


if __name__ == "__main__":
    unittest.main()
