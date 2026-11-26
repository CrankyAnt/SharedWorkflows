"""The project parts of the pipeline in the real schema of the configuration file: the projects each part takes with their
values, the files the schema rejects, and the values that reach the part's jobs through the plan. Each project part is one
entry of ``PARTS``, and every check that applies to each part iterates over it."""

from __future__ import annotations

import json
import sys
import unittest
from dataclasses import dataclass

from tests.pipeline_fixtures import CONFIGURATION, REPOSITORY_ROOT, SCHEMAS, PipelineFixture, plan_program
from tests.workflow_commands import script_invocations

WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "pipeline.yml"

sys.path.insert(0, str(REPOSITORY_ROOT / "actions" / "python-tests"))
sys.path.insert(0, str(REPOSITORY_ROOT / "actions" / "dotnet-test"))
sys.path.insert(0, str(REPOSITORY_ROOT / "actions" / "node-test"))
import dotnet_test  # noqa: E402
import node_test  # noqa: E402
import python_tests  # noqa: E402

PYTHON_VERIFY = "python-verify"
PYTHON_TEST = "python-test"
DOTNET_TEST = "dotnet-test"
NODE_TEST = "node-test"


@dataclass(frozen=True)
class Part:
    """What the checks of this file need of one project part."""

    settings: list[str]
    """The settings of one project, sorted."""
    required: list[str]
    """The settings that a project must state, sorted."""
    smallest: dict
    """One project in its smallest form."""
    several: list[dict]
    """Several projects with their own settings, in the order that the plan keeps."""
    runner_label: bool
    """Whether a project takes runner-label."""
    fixture: str
    """The name of the part's fixture folder under tests/fixtures, which no fragment names."""
    paths: list[str]
    """The paths that the part names in this repository's configuration file."""


# The project parts: adding a part to the checks of this file is one entry here.
PARTS = {
    PYTHON_VERIFY: Part(
        settings=["format-check", "install", "python-version", "working-directory"], required=["python-version"], smallest={"python-version": "3.13"},
        several=[{"python-version": "3.13", "working-directory": "backend", "install": "dev", "format-check": True},
                 {"python-version": "3.12.4", "working-directory": "tools"}],
        runner_label=False, fixture="python_tests",
        paths=["tests/fixtures/python_tests/", "actions/python-language-step/", "actions/python-tests/"]),
    PYTHON_TEST: Part(
        settings=["install", "python-version", "runner-label", "working-directory"], required=["python-version"], smallest={"python-version": "3.13"},
        several=[{"python-version": "3.14", "working-directory": "services/api", "install": "requirements-dev.txt", "runner-label": "windows-2025"},
                 {"python-version": "3.14", "working-directory": "services/api", "install": "requirements-dev.txt"},
                 {"python-version": "3.11"}],
        runner_label=True, fixture="python_tests",
        paths=["tests/fixtures/python_tests/", "actions/python-language-step/", "actions/python-tests/"]),
    DOTNET_TEST: Part(
        settings=["console-runner", "project", "runner-label", "sdk-version"], required=["project", "sdk-version"], smallest={"sdk-version": "10.0.x", "project": "Shop.sln"},
        several=[{"sdk-version": "10.0.x", "project": "tests/Shop.Tests/Shop.Tests.csproj", "runner-label": "windows-2025"},
                 {"sdk-version": "10.0.x", "project": "tests/Shop.Checks/Shop.Checks.csproj", "console-runner": True},
                 {"sdk-version": "9.0.305", "project": "tests/Legacy.Tests/Legacy.Tests.csproj", "console-runner": False}],
        runner_label=True, fixture="dotnet_test", paths=["tests/fixtures/dotnet_test/", "tests/fixtures/dotnet_console/", "actions/dotnet-test/"]),
    NODE_TEST: Part(
        settings=["runner-label", "working-directory"], required=["working-directory"], smallest={"working-directory": "web"},
        several=[{"working-directory": "web", "runner-label": "windows-2025"}, {"working-directory": "."}],
        runner_label=True, fixture="node_test", paths=["tests/fixtures/node_test/", "actions/node-test/"]),
}
RUNNER_LABEL_PARTS = [name for name, part in PARTS.items() if part.runner_label]


def configuration(part: str, *projects: dict, **settings: object) -> str:
    """A configuration file that switches one part on with these projects, written as JSON, which is YAML."""
    return f"version: 1\nparts:\n  {part}: {json.dumps({'projects': list(projects), **settings})}\n"


class RealSchemaFixture(PipelineFixture):
    def setUp(self) -> None:
        super().setUp()
        self.schemas = SCHEMAS

    def planned(self, text: str):
        self.commit({CONFIGURATION: text})
        return self.plan()

    def assert_accepted(self, text: str):
        outcome = self.planned(text)
        self.assertEqual(0, outcome.status, outcome.summary)
        return outcome

    def assert_rejected(self, text: str, message: str) -> None:
        outcome = self.planned(text)
        self.assertEqual(1, outcome.status, text)
        self.assertIn(message, outcome.summary)


class PartSettingsTests(unittest.TestCase):
    def test_each_part_takes_a_list_of_projects_with_their_settings_and_paths_for_the_whole_part(self) -> None:
        schema = json.loads((SCHEMAS / "v1" / "ci-config.schema.json").read_text(encoding="utf-8"))
        parts = schema["properties"]["parts"]["properties"]
        self.assertEqual(sorted(PARTS), sorted(name for name, part in parts.items() if "projects" in part.get("properties", {})),
                         "every part that takes projects is in the register")
        for part, entry in PARTS.items():
            settings, required = entry.settings, entry.required
            with self.subTest(part=part):
                self.assertEqual(entry.runner_label, "runner-label" in settings)
                self.assertEqual(["paths", "projects"], sorted(parts[part]["properties"]))
                self.assertEqual(["projects"], parts[part]["required"])
                self.assertEqual({"$ref": "#/$defs/paths"}, parts[part]["properties"]["paths"])
                self.assertTrue(parts[part]["description"])
                projects = parts[part]["properties"]["projects"]
                self.assertEqual(("array", 1), (projects["type"], projects["minItems"]))
                project = projects["items"]
                self.assertIs(False, project["additionalProperties"])
                self.assertEqual(settings, sorted(project["properties"]))
                self.assertEqual(required, sorted(project["required"]))
                for name, setting in project["properties"].items():
                    self.assertTrue(setting["description"], f"{part}.{name} has no description")

    def test_the_examples_of_the_fragments_are_invented(self) -> None:
        owns = ["tests/fixtures", *dict.fromkeys(entry.fixture for entry in PARTS.values()), "CrankyAnt"]
        for part in PARTS:
            text = (SCHEMAS / "v1" / "parts" / f"{part}.json").read_text(encoding="utf-8")
            for own in owns:
                with self.subTest(part=part, own=own):
                    self.assertNotIn(own, text)


class ProjectListTests(RealSchemaFixture):
    def test_one_project_is_the_smallest_form_of_each_part_and_the_part_runs_with_it(self) -> None:
        for part, entry in PARTS.items():
            project = entry.smallest
            with self.subTest(part=part):
                outcome = self.assert_accepted(configuration(part, project))
                self.assertEqual({"run": True, "reason": "inputs unknown", "config": {"projects": [project]}}, outcome.plan["parts"][part])
                self.assertEqual([{"part": part, "reason": "inputs unknown"}], outcome.plan["departures"])
                for other in PARTS:
                    if other != part:
                        self.assertIs(False, outcome.plan["parts"][other]["run"])

    def test_several_projects_reach_the_plan_in_the_file_s_order_each_with_its_own_settings(self) -> None:
        for part, entry in PARTS.items():
            projects = entry.several
            with self.subTest(part=part):
                outcome = self.assert_accepted(configuration(part, *projects, paths=["src/"]))
                self.assertEqual({"projects": projects, "paths": ["src/"]}, outcome.plan["parts"][part]["config"])

    def test_a_part_without_a_project_or_with_one_project_twice_or_in_the_form_of_one_project_is_rejected(self) -> None:
        for part, entry in PARTS.items():
            project = entry.smallest
            with self.subTest(part=part):
                self.assert_rejected(f"version: 1\nparts:\n  {part}: {{}}\n", f"parts.{part}.projects: is required")
                self.assert_rejected(configuration(part), f"parts.{part}.projects: needs at least 1 item")
                self.assert_rejected(configuration(part, project, project), f"parts.{part}.projects: holds")
                setting = next(iter(project))
                self.assert_rejected(f"version: 1\nparts:\n  {part}: {json.dumps(project)}\n", f"parts.{part}.{setting}: is not a setting of the part {part}")

    def test_the_part_runs_only_on_a_change_under_its_paths(self) -> None:
        text = configuration(PYTHON_TEST, {"python-version": "3.13"}, paths=["src/", "pyproject.toml"])
        base = self.commit({CONFIGURATION: text, "src/app.py": "", "README.md": ""})
        for change, runs in (({"README.md": "changed\n"}, False), ({"src/app.py": "changed = True\n"}, True)):
            with self.subTest(change=change):
                head = self.commit(change)
                outcome = self.plan("--base", base, "--head", head)
                self.assertEqual(0, outcome.status, outcome.summary)
                self.assertIs(runs, outcome.plan["parts"][PYTHON_TEST]["run"])
                base = head

    def test_matching_python_verify_and_python_test_projects_combine_into_one_install(self) -> None:
        text = (
            "version: 1\nparts:\n"
            f"  {PYTHON_VERIFY}: {json.dumps({'projects': [{'python-version': '3.14', 'working-directory': 'backend', 'install': 'dev', 'format-check': True}]})}\n"
            f"  {PYTHON_TEST}: {json.dumps({'projects': [{'python-version': '3.14', 'working-directory': 'backend', 'install': 'dev'}]})}\n"
        )
        outcome = self.assert_accepted(text)
        verify = outcome.plan["parts"][PYTHON_VERIFY]
        test = outcome.plan["parts"][PYTHON_TEST]
        self.assertIs(False, verify["run"])
        self.assertIn("python-test", verify["reason"])
        self.assertEqual([], verify["config"]["projects"])
        self.assertIs(True, test["run"])
        self.assertEqual([{"python-version": "3.14", "working-directory": "backend", "install": "dev", "verify": True, "format-check": True}],
                         test["config"]["projects"])

    def test_python_verify_without_install_combines_into_matching_python_test(self) -> None:
        text = (
            "version: 1\nparts:\n"
            f"  {PYTHON_VERIFY}: {json.dumps({'projects': [{'python-version': '3.14', 'working-directory': 'app', 'format-check': False}]})}\n"
            f"  {PYTHON_TEST}: {json.dumps({'projects': [{'python-version': '3.14', 'working-directory': 'app', 'install': 'dev'}]})}\n"
        )
        outcome = self.assert_accepted(text)
        self.assertIs(False, outcome.plan["parts"][PYTHON_VERIFY]["run"])
        self.assertEqual([{"python-version": "3.14", "working-directory": "app", "install": "dev", "verify": True, "format-check": False}],
                         outcome.plan["parts"][PYTHON_TEST]["config"]["projects"])

    def test_unmatched_python_verify_projects_stay_in_python_verify(self) -> None:
        text = (
            "version: 1\nparts:\n"
            f"  {PYTHON_VERIFY}: {json.dumps({'projects': [{'python-version': '3.14', 'working-directory': 'backend', 'install': 'dev'}, {'python-version': '3.13', 'working-directory': 'cli'}]})}\n"
            f"  {PYTHON_TEST}: {json.dumps({'projects': [{'python-version': '3.14', 'working-directory': 'backend', 'install': 'dev', 'runner-label': 'windows-2025'}, {'python-version': '3.14', 'working-directory': 'backend', 'install': 'dev'}]})}\n"
        )
        outcome = self.assert_accepted(text)
        verify = outcome.plan["parts"][PYTHON_VERIFY]
        test = outcome.plan["parts"][PYTHON_TEST]
        self.assertIs(True, verify["run"])
        self.assertEqual([{"python-version": "3.13", "working-directory": "cli"}], verify["config"]["projects"])
        self.assertEqual([
            {"python-version": "3.14", "working-directory": "backend", "install": "dev", "runner-label": "windows-2025"},
            {"python-version": "3.14", "working-directory": "backend", "install": "dev", "verify": True, "format-check": False},
        ], test["config"]["projects"])

    def test_python_verify_does_not_combine_with_windows_only_python_test(self) -> None:
        text = (
            "version: 1\nparts:\n"
            f"  {PYTHON_VERIFY}: {json.dumps({'projects': [{'python-version': '3.14', 'working-directory': 'backend', 'install': 'dev'}]})}\n"
            f"  {PYTHON_TEST}: {json.dumps({'projects': [{'python-version': '3.14', 'working-directory': 'backend', 'install': 'dev', 'runner-label': 'windows-2025'}]})}\n"
        )
        outcome = self.assert_accepted(text)
        self.assertIs(True, outcome.plan["parts"][PYTHON_VERIFY]["run"])
        self.assertEqual([{"python-version": "3.14", "working-directory": "backend", "install": "dev"}],
                         outcome.plan["parts"][PYTHON_VERIFY]["config"]["projects"])
        self.assertNotIn("verify", outcome.plan["parts"][PYTHON_TEST]["config"]["projects"][0])


class RejectedProjectTests(RealSchemaFixture):
    def assert_each_rejected(self, part: str, cases: tuple) -> None:
        for project, message in cases:
            with self.subTest(project=project):
                self.assert_rejected(configuration(part, PARTS[part].smallest, project), f"parts.{part}.projects[1].{message}")

    def test_a_python_verify_project_that_the_job_cannot_run_with_is_rejected_with_the_place_of_the_value(self) -> None:
        self.assert_each_rejected(PYTHON_VERIFY, (
            ({}, "python-version: is required"),
            ({"python-version": "3.10"}, "python-version: does not match the pattern"),
            ({"python-version": "3"}, "python-version: does not match the pattern"),
            ({"python-version": 3}, "python-version: expected a string, found a whole number"),
            ({"python-version": "3.13", "format-check": "yes"}, "format-check: expected true or false, found a string"),
            ({"python-version": "3.13", "working-directory": ""}, "working-directory: is empty"),
            ({"python-version": "3.13", "working-directory": "/srv/app"}, "working-directory: does not match the pattern"),
            ({"python-version": "3.13", "working-directory": "../app"}, "working-directory: does not match the pattern"),
            ({"python-version": "3.13", "install": "-e ."}, "install: does not match the pattern"),
            ({"python-version": "3.13", "runner-label": "windows-2025"}, "runner-label: is not a setting here"),
        ))

    def test_a_python_test_project_that_the_job_cannot_run_with_is_rejected_with_the_place_of_the_value(self) -> None:
        self.assert_each_rejected(PYTHON_TEST, (
            ({"install": "dev"}, "python-version: is required"),
            ({"python-version": "2.7"}, "python-version: does not match the pattern"),
            ({"python-version": "3.13", "install": "dev extra"}, "install: does not match the pattern"),
            ({"python-version": "3.13", "format-check": True}, "format-check: is not a setting here"),
            ({"python-version": "3.13", "paths": ["src/"]}, "paths: is not a setting here"),
        ))

    def test_a_dotnet_test_project_that_the_job_cannot_run_with_is_rejected_with_the_place_of_the_value(self) -> None:
        self.assert_each_rejected(DOTNET_TEST, (
            ({"project": "Shop.sln"}, "sdk-version: is required"),
            ({"sdk-version": "10.0.x"}, "project: is required"),
            ({"sdk-version": 10, "project": "Shop.sln"}, "sdk-version: expected a string, found a whole number"),
            ({"sdk-version": "latest", "project": "Shop.sln"}, "sdk-version: does not match the pattern"),
            ({"sdk-version": "10.0", "project": "Shop.sln"}, "sdk-version: does not match the pattern"),
            ({"sdk-version": "10.0.x", "project": ""}, "project: is empty"),
            ({"sdk-version": "10.0.x", "project": "../Shop.sln"}, "project: does not match the pattern"),
            ({"sdk-version": "10.0.x", "project": "--version"}, "project: does not match the pattern"),
            ({"sdk-version": "10.0.x", "project": "Shop.sln", "configuration": "Release"}, "configuration: is not a setting here"),
            ({"sdk-version": "10.0.x", "project": "Shop.Checks.csproj", "console-runner": "true"}, "console-runner: expected true or false, found a string"),
            ({"sdk-version": "10.0.x", "project": "Shop.Checks.csproj", "console-runner": 1}, "console-runner: expected true or false, found a whole number"),
        ))

    def test_console_runner_is_a_setting_of_a_dotnet_test_project_and_not_of_the_part(self) -> None:
        project = {"sdk-version": "10.0.x", "project": "Shop.Checks.csproj"}
        for value in (True, False):
            with self.subTest(value=value):
                outcome = self.assert_accepted(configuration(DOTNET_TEST, {**project, "console-runner": value}))
                self.assertEqual([{**project, "console-runner": value}], outcome.plan["parts"][DOTNET_TEST]["config"]["projects"], "the value reaches the job")
        self.assert_rejected(configuration(DOTNET_TEST, project, **{"console-runner": True}),
                             f"parts.{DOTNET_TEST}.console-runner: is not a setting of the part {DOTNET_TEST}")

    def test_a_node_test_project_that_the_job_cannot_run_with_is_rejected_with_the_place_of_the_value(self) -> None:
        self.assert_each_rejected(NODE_TEST, (
            ({}, "working-directory: is required"),
            ({"runner-label": "ubuntu-26.04"}, "working-directory: is required"),
            ({"working-directory": ""}, "working-directory: is empty"),
            ({"working-directory": "/srv/web"}, "working-directory: does not match the pattern"),
            ({"working-directory": "../web"}, "working-directory: does not match the pattern"),
            ({"working-directory": "--prefix"}, "working-directory: does not match the pattern"),
            ({"working-directory": 1}, "working-directory: expected a string, found a whole number"),
            ({"working-directory": "web", "node-version": "24"}, "node-version: is not a setting here"),
            ({"working-directory": "web", "package-manager": "pnpm"}, "package-manager: is not a setting here"),
        ))

    def test_a_node_test_setting_outside_the_fragment_is_rejected(self) -> None:
        self.assert_rejected(configuration(NODE_TEST, PARTS[NODE_TEST].smallest, scripts=["lint"]), f"parts.{NODE_TEST}.scripts: is not a setting of the part {NODE_TEST}")

    def test_the_sdk_version_takes_the_forms_that_name_one_sdk_or_its_newest_patch(self) -> None:
        for version in ("10.0.403", "10.0.x", "10.x", "10.0.4xx"):
            with self.subTest(version=version):
                self.assert_accepted(configuration(DOTNET_TEST, {"sdk-version": version, "project": "Shop.sln"}))


class RunnerLabelTests(RealSchemaFixture):
    """A project's job runs on a GitHub-hosted runner only: no configuration sends a change to a self-hosted runner."""

    def test_a_label_of_the_hosted_runner_families_is_accepted(self) -> None:
        for label in ("ubuntu-26.04", "ubuntu-latest", "ubuntu-slim", "ubuntu-24.04-arm", "windows-2025", "windows-11-arm", "macos-26", "macos-15-intel"):
            for part in RUNNER_LABEL_PARTS:
                with self.subTest(part=part, label=label):
                    self.assert_accepted(configuration(part, {**PARTS[part].smallest, "runner-label": label}))

    def test_any_other_label_is_rejected(self) -> None:
        for label in ("self-hosted", "linux", "x64", "gpu", "build-box", "ubuntu", "windows-", "my-ubuntu-26.04", "Ubuntu-26.04", "ubuntu-26.04 self-hosted",
                      "[self-hosted, linux]", "${{ vars.RUNNER }}", ""):
            for part in RUNNER_LABEL_PARTS:
                with self.subTest(part=part, label=label):
                    self.assert_rejected(configuration(part, {**PARTS[part].smallest, "runner-label": label}),
                                         f"parts.{part}.projects[0].runner-label: does not match the pattern")

    def test_a_list_of_labels_is_rejected(self) -> None:
        for part in RUNNER_LABEL_PARTS:
            with self.subTest(part=part):
                self.assert_rejected(configuration(part, {**PARTS[part].smallest, "runner-label": ["self-hosted", "ubuntu-26.04"]}),
                                     f"parts.{part}.projects[0].runner-label: expected a string, found a list")


class OwnConfigurationTests(unittest.TestCase):
    """This repository's own configuration file switches each project part on, on its fixture project."""

    def setUp(self) -> None:
        versions = plan_program.schema_versions(SCHEMAS)
        self.parts = plan_program.checked_configuration((REPOSITORY_ROOT / CONFIGURATION).read_text(encoding="utf-8"), versions)[0]["parts"]

    def test_the_python_parts_run_on_the_fixture_project_of_python_tests(self) -> None:
        for part in (PYTHON_VERIFY, PYTHON_TEST):
            with self.subTest(part=part):
                (project,) = self.parts[part]["projects"]
                self.assertEqual("tests/fixtures/python_tests", project["working-directory"])
                self.assertTrue((REPOSITORY_ROOT / "tests/fixtures/python_tests/pyproject.toml").is_file())
        self.assertEqual("dev", self.parts[PYTHON_TEST]["projects"][0]["install"], "the dev extra of the fixture holds pytest")

    def test_the_dotnet_part_runs_on_its_fixture_test_project_and_on_its_fixture_console_test_program(self) -> None:
        tests, console = self.parts[DOTNET_TEST]["projects"]
        self.assertTrue(tests["project"].startswith("tests/fixtures/dotnet_test/"))
        self.assertNotIn("console-runner", tests)
        self.assertTrue(console["project"].startswith("tests/fixtures/dotnet_console/"))
        self.assertIs(True, console["console-runner"])
        for project in (tests, console):
            self.assertTrue((REPOSITORY_ROOT / project["project"]).is_file())
        program = (REPOSITORY_ROOT / console["project"]).read_text(encoding="utf-8")
        self.assertIn("<OutputType>Exe</OutputType>", program)
        self.assertNotIn("PackageReference", program, "a console test program has no test framework")

    def test_the_node_part_runs_on_its_fixture_project_with_a_lockfile_a_build_and_a_test(self) -> None:
        (project,) = self.parts[NODE_TEST]["projects"]
        directory = REPOSITORY_ROOT / project["working-directory"]
        self.assertEqual("tests/fixtures/node_test", project["working-directory"])
        for name in ("package.json", "package-lock.json", ".nvmrc"):
            self.assertTrue((directory / name).is_file(), name)
        manifest = json.loads((directory / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(["build", "test"], sorted(manifest["scripts"]))
        self.assertRegex(manifest["packageManager"], r"^npm@[0-9]+\.[0-9]+\.[0-9]+$")
        self.assertNotIn("dependencies", manifest)
        self.assertNotIn("devDependencies", manifest)
        self.assertEqual(3, json.loads((directory / "package-lock.json").read_text(encoding="utf-8"))["lockfileVersion"])
        checked = node_test.read_project(REPOSITORY_ROOT, project["working-directory"])
        self.assertEqual((manifest["packageManager"].removeprefix("npm@"), True), (checked.npm_version, checked.build))
        self.assertRegex((directory / ".nvmrc").read_text(encoding="utf-8"), r"^[0-9]+\.[0-9]+\.[0-9]+\n$", "an exact Node version, which the job compares")
        self.assertEqual((directory / ".nvmrc").read_text(encoding="utf-8").strip(), checked.node_version)

    def test_each_part_s_paths_name_its_fixture_and_what_its_job_runs(self) -> None:
        for part, entry in PARTS.items():
            with self.subTest(part=part):
                self.assertIn(f"tests/fixtures/{entry.fixture}/", entry.paths)
                self.assertEqual(sorted(entry.paths), sorted(self.parts[part]["paths"]))


class InvocationTests(unittest.TestCase):
    """The pipeline's invocations of the parts' scripts, parsed with the scripts' own parsers."""

    def test_the_python_parts_install_and_test_from_the_working_directory(self) -> None:
        invocations = script_invocations(WORKFLOW, "python_tests.py")
        self.assertEqual(["cache-key", "install", "test", "cache-key", "install"], [tokens[0] for tokens in invocations],
                         "python-test keys, installs and tests; python-verify keys and installs")
        for tokens in invocations:
            with self.subTest(command=tokens[0]):
                options = python_tests.parse_arguments(tokens)
                self.assertEqual("x", options.root)
        for tokens in (invocations[0], invocations[1], invocations[3], invocations[4]):
            self.assertEqual("x", python_tests.parse_arguments(tokens).install, "the value reaches --install with an equals sign, so a leading - stays a value")

    def test_the_dotnet_step_hands_the_project_to_its_script_and_the_console_runner_only_for_a_console_test_program(self) -> None:
        action = REPOSITORY_ROOT / "actions" / "dotnet-test" / "action.yml"
        tests, console = script_invocations(action, "dotnet_test.py")
        for tokens, console_runner in ((tests, False), (console, True)):
            with self.subTest(console_runner=console_runner):
                options = dotnet_test.build_parser().parse_args(tokens)
                self.assertEqual(("x", console_runner), (options.project, options.console_runner))

    def test_the_node_step_hands_the_command_and_the_working_directory_to_its_script(self) -> None:
        action = REPOSITORY_ROOT / "actions" / "node-test" / "action.yml"
        check, test = script_invocations(action, "node_test.py")
        for tokens, command in ((check, "check"), (test, "test")):
            with self.subTest(command=command):
                options = node_test.build_parser().parse_args(tokens)
                self.assertEqual((command, "x"), (options.command, options.working_directory))


if __name__ == "__main__":
    unittest.main()
