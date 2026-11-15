"""The Python, .NET and Node parts in the real schema of the configuration file: the projects each part takes with their
values, the files the schema rejects, and the values that reach the part's jobs through the plan."""

from __future__ import annotations

import json
import sys
import unittest

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
# The settings of one project of each part, and the ones a project must state.
SETTINGS = {
    PYTHON_VERIFY: (["format-check", "install", "python-version", "working-directory"], ["python-version"]),
    PYTHON_TEST: (["install", "python-version", "runner-label", "working-directory"], ["python-version"]),
    DOTNET_TEST: (["project", "runner-label", "sdk-version"], ["project", "sdk-version"]),
    NODE_TEST: (["runner-label", "working-directory"], ["working-directory"]),
}
# A project of each part in its smallest form.
SMALLEST = {
    PYTHON_VERIFY: {"python-version": "3.13"},
    PYTHON_TEST: {"python-version": "3.13"},
    DOTNET_TEST: {"sdk-version": "10.0.x", "project": "Shop.sln"},
    NODE_TEST: {"working-directory": "web"},
}


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
        for part, (settings, required) in SETTINGS.items():
            with self.subTest(part=part):
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
        for part in SETTINGS:
            text = (SCHEMAS / "v1" / "parts" / f"{part}.json").read_text(encoding="utf-8")
            for own in ("tests/fixtures", "python_tests", "dotnet_test", "node_test", "CrankyAnt"):
                with self.subTest(part=part, own=own):
                    self.assertNotIn(own, text)


class ProjectListTests(RealSchemaFixture):
    def test_one_project_is_the_smallest_form_of_each_part_and_the_part_runs_with_it(self) -> None:
        for part, project in SMALLEST.items():
            with self.subTest(part=part):
                outcome = self.assert_accepted(configuration(part, project))
                self.assertEqual({"run": True, "reason": "switched on", "config": {"projects": [project]}}, outcome.plan["parts"][part])
                for other in SMALLEST:
                    if other != part:
                        self.assertIs(False, outcome.plan["parts"][other]["run"])

    def test_several_projects_reach_the_plan_in_the_file_s_order_each_with_its_own_settings(self) -> None:
        for part, projects in (
            (PYTHON_VERIFY, [{"python-version": "3.13", "working-directory": "backend", "install": "dev", "format-check": True},
                             {"python-version": "3.12.4", "working-directory": "tools"}]),
            (PYTHON_TEST, [{"python-version": "3.14", "working-directory": "services/api", "install": "requirements-dev.txt", "runner-label": "windows-2025"},
                           {"python-version": "3.14", "working-directory": "services/api", "install": "requirements-dev.txt"},
                           {"python-version": "3.11"}]),
            (DOTNET_TEST, [{"sdk-version": "10.0.x", "project": "tests/Shop.Tests/Shop.Tests.csproj", "runner-label": "windows-2025"},
                           {"sdk-version": "9.0.305", "project": "tests/Legacy.Tests/Legacy.Tests.csproj"}]),
            (NODE_TEST, [{"working-directory": "web", "runner-label": "windows-2025"}, {"working-directory": "."}]),
        ):
            with self.subTest(part=part):
                outcome = self.assert_accepted(configuration(part, *projects, paths=["src/"]))
                self.assertEqual({"projects": projects, "paths": ["src/"]}, outcome.plan["parts"][part]["config"])

    def test_a_part_without_a_project_or_with_one_project_twice_or_in_the_form_of_one_project_is_rejected(self) -> None:
        for part, project in SMALLEST.items():
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


class RejectedProjectTests(RealSchemaFixture):
    def assert_each_rejected(self, part: str, cases: tuple) -> None:
        for project, message in cases:
            with self.subTest(project=project):
                self.assert_rejected(configuration(part, SMALLEST[part], project), f"parts.{part}.projects[1].{message}")

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
        ))

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
        self.assert_rejected(configuration(NODE_TEST, SMALLEST[NODE_TEST], scripts=["lint"]), f"parts.{NODE_TEST}.scripts: is not a setting of the part {NODE_TEST}")

    def test_the_sdk_version_takes_the_forms_that_name_one_sdk_or_its_newest_patch(self) -> None:
        for version in ("10.0.403", "10.0.x", "10.x", "10.0.4xx"):
            with self.subTest(version=version):
                self.assert_accepted(configuration(DOTNET_TEST, {"sdk-version": version, "project": "Shop.sln"}))


class RunnerLabelTests(RealSchemaFixture):
    """A project's job runs on a GitHub-hosted runner only: no configuration sends a change to a self-hosted runner."""

    def test_a_label_of_the_hosted_runner_families_is_accepted(self) -> None:
        for label in ("ubuntu-26.04", "ubuntu-latest", "ubuntu-slim", "ubuntu-24.04-arm", "windows-2025", "windows-11-arm", "macos-26", "macos-15-intel"):
            for part in (PYTHON_TEST, DOTNET_TEST, NODE_TEST):
                with self.subTest(part=part, label=label):
                    self.assert_accepted(configuration(part, {**SMALLEST[part], "runner-label": label}))

    def test_any_other_label_is_rejected(self) -> None:
        for label in ("self-hosted", "linux", "x64", "gpu", "build-box", "ubuntu", "windows-", "my-ubuntu-26.04", "Ubuntu-26.04", "ubuntu-26.04 self-hosted",
                      "[self-hosted, linux]", "${{ vars.RUNNER }}", ""):
            for part in (PYTHON_TEST, DOTNET_TEST, NODE_TEST):
                with self.subTest(part=part, label=label):
                    self.assert_rejected(configuration(part, {**SMALLEST[part], "runner-label": label}),
                                         f"parts.{part}.projects[0].runner-label: does not match the pattern")

    def test_a_list_of_labels_is_rejected(self) -> None:
        for part in (PYTHON_TEST, DOTNET_TEST, NODE_TEST):
            with self.subTest(part=part):
                self.assert_rejected(configuration(part, {**SMALLEST[part], "runner-label": ["self-hosted", "ubuntu-26.04"]}),
                                     f"parts.{part}.projects[0].runner-label: expected a string, found a list")


class OwnConfigurationTests(unittest.TestCase):
    """This repository's own configuration file switches the four parts on, each on its fixture project."""

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

    def test_the_dotnet_part_runs_on_its_fixture_project(self) -> None:
        (project,) = self.parts[DOTNET_TEST]["projects"]
        self.assertTrue(project["project"].startswith("tests/fixtures/dotnet_test/"))
        self.assertTrue((REPOSITORY_ROOT / project["project"]).is_file())

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
        common = [".github/workflows/ci.yml", ".github/workflows/pipeline.yml", "actions/pipeline/"]
        for part, own in ((PYTHON_VERIFY, ["tests/fixtures/python_tests/", "actions/python-language-step/", "actions/python-tests/", "actions/repository-hygiene/"]),
                          (PYTHON_TEST, ["tests/fixtures/python_tests/", "actions/python-tests/"]),
                          (DOTNET_TEST, ["tests/fixtures/dotnet_test/", "actions/dotnet-test/"]),
                          (NODE_TEST, ["tests/fixtures/node_test/", "actions/node-test/"])):
            with self.subTest(part=part):
                self.assertEqual(sorted([*common, *own]), sorted(self.parts[part]["paths"]))


class InvocationTests(unittest.TestCase):
    """The pipeline's invocations of the parts' scripts, parsed with the scripts' own parsers."""

    def test_the_python_parts_install_and_test_from_the_working_directory(self) -> None:
        invocations = script_invocations(WORKFLOW, "python_tests.py")
        self.assertEqual(["install", "test", "install"], [tokens[0] for tokens in invocations], "python-test installs and tests, python-verify installs")
        for tokens in invocations:
            with self.subTest(command=tokens[0]):
                options = python_tests.parse_arguments(tokens)
                self.assertEqual("x", options.root)
        for tokens in (invocations[0], invocations[2]):
            self.assertEqual("x", python_tests.parse_arguments(tokens).install, "the value reaches --install with an equals sign, so a leading - stays a value")

    def test_the_dotnet_step_hands_the_project_to_its_script(self) -> None:
        action = REPOSITORY_ROOT / "actions" / "dotnet-test" / "action.yml"
        (tokens,) = script_invocations(action, "dotnet_test.py")
        self.assertEqual("x", dotnet_test.build_parser().parse_args(tokens).project)

    def test_the_node_step_hands_the_command_and_the_working_directory_to_its_script(self) -> None:
        action = REPOSITORY_ROOT / "actions" / "node-test" / "action.yml"
        check, test = script_invocations(action, "node_test.py")
        for tokens, command in ((check, "check"), (test, "test")):
            with self.subTest(command=command):
                options = node_test.build_parser().parse_args(tokens)
                self.assertEqual((command, "x"), (options.command, options.working_directory))


if __name__ == "__main__":
    unittest.main()
