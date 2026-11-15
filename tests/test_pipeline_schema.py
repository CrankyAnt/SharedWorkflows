"""The schema of the configuration file as this repository holds it: assembled from one fragment per shared part, with
repository hygiene outside the configuration, and checked by rules that the plan program reads in full."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tests.pipeline_fixtures import CONFIGURATION, SCHEMAS, PipelineFixture, copy_schemas, plan_program


def run_schema(*arguments: str) -> tuple[int, str]:
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        status = plan_program.main(["schema", *arguments])
    return status, output.getvalue()


class AssembledSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
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
            ({"description": "x", "always": True, "configuration": {"type": "object", "additionalProperties": False}}, "a part that always runs has no configuration"),
            ({"description": "x", "enabled": True, "configuration": {"type": "object", "additionalProperties": False}}, "a fragment holds a description and either a configuration or always"),
        ):
            with self.subTest(message=message):
                (self.copy / "v1" / "parts" / "invented-test.json").write_text(json.dumps(fragment), encoding="utf-8")
                status, output = run_schema("--schemas", str(self.copy), "--check")
                self.assertEqual(2, status)
                self.assertIn(message, output)


class RepositoryHygieneTests(PipelineFixture):
    """Repository hygiene in the real schema: it runs whatever the configuration file says and the file cannot state it."""

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

    def test_the_schema_has_no_setting_for_repository_hygiene(self) -> None:
        schema = json.loads((SCHEMAS / "v1" / "ci-config.schema.json").read_text(encoding="utf-8"))
        self.assertNotIn("hygiene", json.dumps({key: value for key, value in schema.items() if key != "properties"}))
        self.assertEqual(["local-workflows", "parts", "version"], sorted(schema["properties"]))
        self.assertNotIn("repository-hygiene", schema["properties"]["parts"]["properties"])
        self.assertIs(False, schema["properties"]["parts"]["additionalProperties"])
        self.assertIs(False, schema["additionalProperties"])
        for configuration in ("version: 1\nparts:\n  repository-hygiene: {}\n", "version: 1\nparts:\n  repository-hygiene:\n    enabled: false\n",
                              "version: 1\nparts:\n  repository-hygiene: false\n", "version: 1\nrepository-hygiene: false\n", "version: 1\nhygiene: false\n"):
            with self.subTest(configuration=configuration):
                self.commit({CONFIGURATION: configuration})
                outcome = self.plan()
                self.assertEqual(1, outcome.status)
                self.assertRegex(outcome.summary, "always runs and has no settings|is not a setting of the configuration file")

    def test_this_repository_s_own_configuration_file_matches_the_schema(self) -> None:
        from tests.pipeline_fixtures import REPOSITORY_ROOT

        versions = plan_program.schema_versions(SCHEMAS)
        document, version = plan_program.checked_configuration((REPOSITORY_ROOT / CONFIGURATION).read_text(encoding="utf-8"), versions)
        self.assertEqual(1, version.version)
        self.assertEqual(1, document["version"])


if __name__ == "__main__":
    unittest.main()
