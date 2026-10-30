"""The shared Python language step's composite action and the rule that no shared part installs a Python interpreter."""

from __future__ import annotations

import json
import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, read_workflow

ACTION = REPOSITORY_ROOT / "actions" / "python-language-step" / "action.yml"
SURFACE = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "shared-surface.json"
PINS = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "pins.json"
CATALOGUE = REPOSITORY_ROOT / "catalogue.json"
KIT = REPOSITORY_ROOT / "actions" / "python-language-step" / "consumer-kit"


class PythonLanguageStepActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.action = read_workflow(ACTION)

    def test_it_runs_the_step_on_the_job_s_python_with_the_shared_pins(self) -> None:
        self.assertEqual("composite", self.action["runs"]["using"])
        steps = self.action["runs"]["steps"]
        self.assertEqual(1, len(steps))
        run = steps[0]
        self.assertEqual("bash", run["shell"])
        self.assertEqual("${{ runner.os == 'Windows' && 'python' || 'python3' }}", run["env"]["PYTHON"])
        self.assertEqual("${{ runner.temp }}/python-language-step-tools", run["env"]["TOOLS"])
        self.assertIn('"$PYTHON" "$RESOURCE_PATH/python_language_step.py" --root . --tools "$TOOLS" '
                      '--install "$RESOURCE_PATH/../repository-hygiene/pins.json" "${options[@]}"', run["run"])

    def test_the_format_check_is_an_optional_input_that_is_off_by_default(self) -> None:
        self.assertEqual({"format-check"}, set(self.action["inputs"]))
        self.assertEqual("false", self.action["inputs"]["format-check"]["default"])
        run = self.action["runs"]["steps"][0]
        self.assertEqual("${{ inputs.format-check }}", run["env"]["FORMAT_CHECK"])
        self.assertIn('if [[ "$FORMAT_CHECK" == true ]]; then options+=(--format-check); fi', run["run"])

    def test_ruff_and_mypy_are_shared_hash_locked_pins(self) -> None:
        pins = json.loads(PINS.read_text(encoding="utf-8"))
        for tool in ("ruff", "mypy"):
            with self.subTest(tool=tool):
                self.assertEqual(tool, pins["python"][tool]["requirement"])
                self.assertEqual(pins["python"][tool]["version"], pins["python"][tool]["packages"][tool]["version"])

    def test_the_step_is_part_of_the_shared_surface(self) -> None:
        self.assertIn("actions/python-language-step", json.loads(SURFACE.read_text(encoding="utf-8"))["paths"])


class PythonLanguageStepConsumerKitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.template_text, self.template = read_workflow(KIT / "python-language-step.yml")
        self.readme = (KIT / "README.md").read_text(encoding="utf-8")

    def test_the_catalogue_entry_names_the_kit(self) -> None:
        entries = {entry["name"]: entry for entry in json.loads(CATALOGUE.read_text(encoding="utf-8"))["parts"]}
        self.assertEqual("actions/python-language-step/consumer-kit", entries["Python language step"]["consumer_kit"])

    def test_the_template_is_one_step_that_calls_the_shared_step_at_v1_for_a_selected_language_step(self) -> None:
        self.assertEqual(["steps"], list(self.template))
        (step,) = self.template["steps"]
        self.assertEqual("CrankyAnt/SharedWorkflows/actions/python-language-step@v1", step["uses"])
        self.assertEqual("${{ !cancelled() && fromJSON(inputs.language-steps).python == 'run' }}", step["if"])
        self.assertNotIn("with", step, "the format check stays off unless the repository turns it on")
        self.assertNotIn("run", step)

    def test_the_readme_explains_what_runs_what_turns_each_check_on_and_how_a_workflow_calls_it(self) -> None:
        for text in ("ruff check", "mypy.ini", ".mypy.ini", "[tool.mypy]", "setup.cfg", "[mypy]", "ruff format --check",
                     'format-check: "true"', "python-language-step.yml", "language-steps", "installs no interpreter"):
            with self.subTest(text=text):
                self.assertIn(text, self.readme)

    def test_the_kit_names_nothing_that_exists_only_in_this_repository(self) -> None:
        for name, text in (("template", self.template_text), ("README", self.readme)):
            for forbidden in ("tests/", "validate.yml", "F and E9", "maintaining.md", "pins.json", "$/", "docs/agents"):
                with self.subTest(file=name, forbidden=forbidden):
                    self.assertNotIn(forbidden, text)


class NoInterpreterInstallTests(unittest.TestCase):
    def test_no_shared_part_installs_a_python_interpreter(self) -> None:
        surface = json.loads(SURFACE.read_text(encoding="utf-8"))["paths"]
        # A consumer kit's templates become the consumer's own workflows, which set up the Python its suites test on.
        files = [path for path in (REPOSITORY_ROOT / "actions").rglob("*")
                 if path.suffix in (".yml", ".py", ".sh", ".ps1") and "consumer-kit" not in path.parts]
        files += [path for path in (REPOSITORY_ROOT / ".github" / "workflows").glob("*.yml") if f".github/workflows/{path.name}" in surface]
        self.assertTrue(any(path.name == "suite-selection.yml" for path in files))
        install = re.compile(r"actions/setup-python|python-version:|\bpyenv\b|\buv python\b|deadsnakes|apt(?:-get)? install[^\n]*\bpython3?(?:\.\d+)?\b")
        for path in files:
            with self.subTest(path=path.relative_to(REPOSITORY_ROOT).as_posix()):
                self.assertIsNone(install.search(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
