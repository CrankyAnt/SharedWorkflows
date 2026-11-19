"""The schema of the configuration file as this repository holds it: assembled from one fragment per shared part, with
repository hygiene a part that always runs and takes two exclusions, and checked by rules that the plan program reads in
full."""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tests import temporary_support
from tests.pipeline_fixtures import CONFIGURATION, SCHEMAS, PipelineFixture, copy_schemas, plan_program


def run_schema(*arguments: str) -> tuple[int, str]:
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        status = plan_program.main(["schema", *arguments])
    return status, output.getvalue()


class AssembledSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = temporary_support.Directory()
        self.addCleanup(directory.cleanup)
        self.copy = copy_schemas(Path(directory.name) / "schema")

    def test_the_committed_schema_of_every_version_equals_the_one_assembled_from_its_base_and_fragments(self) -> None:
        self.assertEqual((0, ""), run_schema("--check"))
        versions = sorted(path.name for path in SCHEMAS.iterdir() if path.is_dir())
        self.assertEqual(["v1"], versions)
        self.assertTrue((SCHEMAS / "v1" / "ci-config.schema.json").is_file())

    def test_a_new_fragment_makes_the_committed_schema_stale_until_it_is_written_again(self) -> None:
        fragment = {"description": "An invented part.", "configuration": {"type": "object", "additionalProperties": False,
                                                                           "properties": {"paths": {"$ref": "#/$defs/paths"}}}}
        (self.copy / "v1" / "parts" / "invented-test.json").write_text(json.dumps(fragment), encoding="utf-8")
        status, output = run_schema("--schemas", str(self.copy), "--check")
        self.assertEqual(1, status)
        self.assertIn("ci-config.schema.json differs from the schema assembled", output)
        self.assertEqual(0, run_schema("--schemas", str(self.copy))[0])
        self.assertEqual((0, ""), run_schema("--schemas", str(self.copy), "--check"))
        assembled = json.loads((self.copy / "v1" / "ci-config.schema.json").read_text(encoding="utf-8"))
        self.assertEqual({"description": "An invented part.", **fragment["configuration"]}, assembled["properties"]["parts"]["properties"]["invented-test"])

    def test_a_fragment_with_a_rule_that_the_plan_does_not_read_fails_instead_of_being_skipped(self) -> None:
        for fragment, message in (
            ({"description": "x", "configuration": {"type": "object", "additionalProperties": False, "properties": {"a": {"maxLength": 3}}}}, "the keyword maxLength is not supported"),
            ({"description": "x", "configuration": {"type": "object", "properties": {}}}, "an object without additional properties"),
            ({"description": "x", "always": True, "configuration": {"type": "object", "properties": {}}}, "an object without additional properties"),
            ({"description": "x", "enabled": True, "configuration": {"type": "object", "additionalProperties": False}}, "a fragment holds a description and a configuration, always, or both"),
            ({"description": "x"}, "a fragment holds a description and a configuration, always, or both"),
            ({"description": "x", "always": False}, "a fragment holds a description and a configuration, always, or both"),
        ):
            with self.subTest(message=message):
                (self.copy / "v1" / "parts" / "invented-test.json").write_text(json.dumps(fragment), encoding="utf-8")
                status, output = run_schema("--schemas", str(self.copy), "--check")
                self.assertEqual(2, status)
                self.assertIn(message, output)


class RepositoryHygieneTests(PipelineFixture):
    """Repository hygiene in the real schema: it runs whatever the configuration file says, and the file may state it
only with its two exclusions."""

    def setUp(self) -> None:
        super().setUp()
        self.schemas = SCHEMAS

    def test_repository_hygiene_runs_for_every_configuration_file_that_the_schema_accepts(self) -> None:
        for configuration in ("version: 1\n", "version: 1\nparts: {}\n", "version: 1\nparts: {}\nlocal-workflows: []\n"):
            with self.subTest(configuration=configuration):
                self.commit({CONFIGURATION: configuration})
                outcome = self.plan()
                self.assertEqual(0, outcome.status, outcome.summary)
                self.assertEqual({"run": True, "reason": "always runs", "config": {}}, outcome.plan["parts"]["repository-hygiene"])

    def test_a_configuration_file_may_state_repository_hygiene_with_its_two_exclusions_and_it_still_runs(self) -> None:
        for configuration, config in (
            ("version: 1\nparts:\n  repository-hygiene:\n    coverage-exclude:\n      - tests/fixtures/package.json\n    link-exclude:\n      - vendor/**/*.md\n",
             {"coverage-exclude": ["tests/fixtures/package.json"], "link-exclude": ["vendor/**/*.md"]}),
            ("version: 1\nparts:\n  repository-hygiene:\n    coverage-exclude: [tests/fixtures, tools/fixture/pyproject.toml]\n",
             {"coverage-exclude": ["tests/fixtures", "tools/fixture/pyproject.toml"]}),
            ("version: 1\nparts:\n  repository-hygiene:\n    link-exclude: [vendor/, docs/third-party/*.md]\n", {"link-exclude": ["vendor/", "docs/third-party/*.md"]}),
            ("version: 1\nparts:\n  repository-hygiene: {}\n", {}),
            ("version: 1\nparts:\n  repository-hygiene:\n", {}),
            ("version: 1\n", {}),
        ):
            with self.subTest(configuration=configuration):
                self.commit({CONFIGURATION: configuration})
                outcome = self.plan()
                self.assertEqual(0, outcome.status, outcome.summary)
                self.assertEqual({"run": True, "reason": "always runs", "config": config}, outcome.plan["parts"]["repository-hygiene"])
                self.assertIn("| `repository-hygiene` | yes | always runs |", outcome.summary)
                self.assertNotIn("repository-hygiene", outcome.plan["switched-off"])

    def test_removing_the_part_from_the_configuration_file_switches_nothing_off(self) -> None:
        base = self.commit({CONFIGURATION: "version: 1\nparts:\n  repository-hygiene:\n    link-exclude: [vendor/]\n"})
        head = self.commit({CONFIGURATION: "version: 1\n"})
        outcome = self.plan("--base", base, "--head", head)
        self.assertEqual(0, outcome.status, outcome.summary)
        self.assertEqual([], outcome.plan["switched-off"])
        self.assertEqual((True, "always runs"), (outcome.plan["parts"]["repository-hygiene"]["run"], outcome.plan["parts"]["repository-hygiene"]["reason"]))

    def test_another_key_or_a_setting_that_is_no_non_empty_list_of_strings_fails_the_plan_and_names_the_key(self) -> None:
        for value, message in (
            ("\n    enabled: false", "parts.repository-hygiene.enabled: is not a setting of the part repository-hygiene"),
            ("\n    paths: [src/]", "parts.repository-hygiene.paths: is not a setting of the part repository-hygiene"),
            ("\n    coverage-exclude: tests/fixtures", "parts.repository-hygiene.coverage-exclude: expected a list, found a string"),
            ("\n    coverage-exclude: []", "parts.repository-hygiene.coverage-exclude: needs at least 1 item"),
            ("\n    coverage-exclude: [3]", "parts.repository-hygiene.coverage-exclude[0]: expected a string, found a whole number"),
            ("\n    coverage-exclude: ['']", "parts.repository-hygiene.coverage-exclude[0]: is empty"),
            ("\n    link-exclude: vendor/**/*.md", "parts.repository-hygiene.link-exclude: expected a list, found a string"),
            ("\n    link-exclude: []", "parts.repository-hygiene.link-exclude: needs at least 1 item"),
            ("\n    link-exclude: [true]", "parts.repository-hygiene.link-exclude[0]: expected a string, found true or false"),
            ("\n    link-exclude:\n      tree: vendor", "parts.repository-hygiene.link-exclude: expected a list, found a mapping"),
            (" false", "parts.repository-hygiene: expected a mapping, found true or false"),
        ):
            with self.subTest(value=value):
                self.commit({CONFIGURATION: f"version: 1\nparts:\n  repository-hygiene:{value}\n"})
                outcome = self.plan()
                self.assertEqual(1, outcome.status, outcome.summary)
                self.assertIn(message, outcome.summary)

    def test_repository_hygiene_is_no_setting_outside_parts(self) -> None:
        for configuration in ("version: 1\nrepository-hygiene: false\n", "version: 1\nhygiene: false\n"):
            with self.subTest(configuration=configuration):
                self.commit({CONFIGURATION: configuration})
                outcome = self.plan()
                self.assertEqual(1, outcome.status)
                self.assertIn("is not a setting of the configuration file", outcome.summary)

    def test_the_assembled_schema_describes_the_two_settings_under_parts_repository_hygiene(self) -> None:
        schema = json.loads((SCHEMAS / "v1" / "ci-config.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(["dependabot", "local-workflows", "parts", "version"], sorted(schema["properties"]))
        self.assertIs(False, schema["additionalProperties"])
        part = schema["properties"]["parts"]["properties"]["repository-hygiene"]
        self.assertIs(False, part["additionalProperties"])
        self.assertEqual(["coverage-exclude", "link-exclude"], sorted(part["properties"]))
        self.assertIn("always runs and cannot be switched off", part["description"])
        for name in ("coverage-exclude", "link-exclude"):
            with self.subTest(setting=name):
                setting = part["properties"][name]
                self.assertEqual(("array", 1), (setting["type"], setting["minItems"]))
                self.assertTrue(setting["description"])
        self.assertIn("Dependabot coverage check", part["properties"]["coverage-exclude"]["description"])
        self.assertIn("still checks a link from another file into one", part["properties"]["link-exclude"]["description"])
        self.assertNotIn("has no settings", schema["properties"]["parts"]["description"])

    def test_this_repository_s_own_configuration_file_matches_the_schema(self) -> None:
        from tests.pipeline_fixtures import REPOSITORY_ROOT

        versions = plan_program.schema_versions(SCHEMAS)
        document, version = plan_program.checked_configuration((REPOSITORY_ROOT / CONFIGURATION).read_text(encoding="utf-8"), versions)
        self.assertEqual(1, version.version)
        self.assertEqual(1, document["version"])


class PinnedToolsSettingTests(PipelineFixture):
    """The setting ``pinned-tools`` of the suite selection in the real schema."""

    PART = "version: 1\nparts:\n  suite-selection:\n    suites: ci/suites.json\n"

    def setUp(self) -> None:
        super().setUp()
        self.schemas = SCHEMAS

    def test_the_part_takes_the_pinned_tools_node_and_gitleaks_and_runs_without_the_setting(self) -> None:
        for setting, config in (("    pinned-tools: [node, gitleaks]\n", {"pinned-tools": ["node", "gitleaks"]}),
                                ("    pinned-tools:\n      - gitleaks\n", {"pinned-tools": ["gitleaks"]}),
                                ("    pinned-tools: []\n", {"pinned-tools": []}), ("", {})):
            with self.subTest(setting=setting):
                self.commit({CONFIGURATION: self.PART + setting})
                outcome = self.plan()
                self.assertEqual(0, outcome.status, outcome.summary)
                self.assertEqual({"suites": "ci/suites.json"} | config, outcome.plan["parts"]["suite-selection"]["config"])

    def test_a_name_outside_the_pinned_tools_a_repeated_name_or_another_form_fails_the_plan(self) -> None:
        for setting in ("[actionlint]", "[node, zizmor]", "[gitleaks, gitleaks]", "[node, gitleaks, node]", "gitleaks", "[1]"):
            with self.subTest(setting=setting):
                self.commit({CONFIGURATION: self.PART + f"    pinned-tools: {setting}\n"})
                outcome = self.plan()
                self.assertNotEqual(0, outcome.status, outcome.summary)
                self.assertIn("parts.suite-selection.pinned-tools", outcome.summary)

    def test_the_setting_command_prints_the_pinned_tools_that_the_full_recording_reads(self) -> None:
        self.commit({CONFIGURATION: self.PART + "    pinned-tools: [node, gitleaks]\n"})
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = plan_program.main(["setting", "--root", str(self.repository), "--schemas", str(self.schemas), "--part", "suite-selection",
                                        "--setting", "pinned-tools"])
        self.assertEqual((0, "node\ngitleaks\n", ""), (status, stdout.getvalue(), stderr.getvalue()))

    def test_the_fragment_knows_the_setting_under_the_name_that_a_consumer_waits_for(self) -> None:
        fragment = json.loads((SCHEMAS / "v1" / "parts" / "suite-selection.json").read_text(encoding="utf-8"))
        self.assertEqual({"type": "array", "uniqueItems": True, "items": {"type": "string", "enum": ["node", "gitleaks"]}},
                         {key: value for key, value in fragment["configuration"]["properties"]["pinned-tools"].items() if key != "description"})


class SettingCommandTests(PipelineFixture):
    """``plan.py setting``, with which the pipeline's hygiene job reads an exclusion from the configuration file itself."""

    def setUp(self) -> None:
        super().setUp()
        self.schemas = SCHEMAS

    def setting(self, name: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = plan_program.main(["setting", "--root", str(self.repository), "--schemas", str(self.schemas), "--part", "repository-hygiene", "--setting", name])
        return status, stdout.getvalue(), stderr.getvalue()

    def test_it_prints_each_item_of_the_stated_setting_on_a_line_of_its_own(self) -> None:
        self.commit({CONFIGURATION: "version: 1\nparts:\n  repository-hygiene:\n    coverage-exclude: [tests/fixtures, tools/package.json]\n"
                                    "    link-exclude:\n      - vendor/**/*.md\n"})
        self.assertEqual((0, "tests/fixtures\ntools/package.json\n", ""), self.setting("coverage-exclude"))
        self.assertEqual((0, "vendor/**/*.md\n", ""), self.setting("link-exclude"))

    def test_it_prints_nothing_when_the_file_does_not_state_the_setting(self) -> None:
        for configuration in ("version: 1\n", "version: 1\nparts:\n  repository-hygiene: {}\n", "version: 1\nparts:\n  repository-hygiene:\n    link-exclude: [vendor/]\n"):
            with self.subTest(configuration=configuration):
                self.commit({CONFIGURATION: configuration})
                self.assertEqual((0, "", ""), self.setting("coverage-exclude"))

    def test_a_configuration_file_that_cannot_be_used_leaves_nothing_out_and_says_that_the_plan_reports_it(self) -> None:
        for configuration, problem in (
            ("version: 1\nparts:\n  repository-hygiene:\n    coverage-exclude: [tests/fixtures]\n    extra: 1\n", "parts.repository-hygiene.extra: is not a setting of the part repository-hygiene"),
            ("version: 1\nparts:\n  repository-hygiene:\n    coverage-exclude: [tests/fixtures]\n  invented: {}\n", "parts.invented: is not a part of this pipeline"),
            ("version: 9\nparts:\n  repository-hygiene:\n    coverage-exclude: [tests/fixtures]\n", "version: 9 is not a version this pipeline reads"),
            ("version: 1\nparts: [\n", "the file is not YAML that the pipeline reads"),
        ):
            with self.subTest(configuration=configuration):
                self.commit({CONFIGURATION: configuration})
                status, stdout, stderr = self.setting("coverage-exclude")
                self.assertEqual((0, ""), (status, stdout))
                self.assertIn(".github/ci-config.yml cannot be used, so the setting repository-hygiene.coverage-exclude is taken as not stated; the plan reports why", stderr)
                self.assertIn(problem, stderr)
                self.assertEqual(1, self.plan().status, "the plan fails on the same file")

    def test_a_missing_configuration_file_leaves_nothing_out(self) -> None:
        self.commit({"README.md": "A consumer.\n"})
        status, stdout, stderr = self.setting("link-exclude")
        self.assertEqual((0, ""), (status, stdout))
        self.assertIn(".github/ci-config.yml does not exist", stderr)


if __name__ == "__main__":
    unittest.main()
