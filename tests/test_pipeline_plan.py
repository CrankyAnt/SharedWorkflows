"""The plan of the pipeline on fixture consumers: which configuration files it accepts, which parts run on which
changes, and what it reports as switched off or newly registered compared with the base."""

from __future__ import annotations

import json
import unittest

from tests.pipeline_fixtures import CONFIGURATION, FIXTURE_PARTS, PipelineFixture, git


class ConfigurationTests(PipelineFixture):
    def test_a_started_plan_of_a_valid_configuration_passes_and_writes_the_plan_as_its_output(self) -> None:
        self.commit({CONFIGURATION: """\
            version: 1
            parts:
              fixture-build: {}
            """})
        outcome = self.start_program("plan")
        self.assertEqual(0, outcome.status, outcome.stdout + outcome.stderr)
        self.assertEqual(1, outcome.plan["version"])
        self.assertEqual({"fixture-test": False, "fixture-build": True, "fixture-floor": True},
                         {name: part["run"] for name, part in outcome.plan["parts"].items()})
        self.assertIn("fixture-build", outcome.summary)

    def test_a_started_plan_of_a_configuration_that_does_not_match_the_schema_fails_with_the_schema_error_in_the_summary(self) -> None:
        self.commit({CONFIGURATION: """\
            version: 1
            parts:
              fixture-test:
                level: medium
            """})
        outcome = self.start_program("plan")
        self.assertEqual(1, outcome.status)
        self.assertNotIn("plan", outcome.outputs)
        for text in (outcome.summary, outcome.stdout):
            self.assertIn("parts.fixture-test.level", text)
            self.assertIn("'low' or 'high'", text)

    def test_each_mismatch_is_named_with_its_place_in_the_file(self) -> None:
        for configuration, expected in (
            ("parts: {}\n", "version: is required"),
            ("version: 1\nextra: true\n", "extra: is not a setting of the configuration file"),
            ("version: 1\nparts:\n  unknown-part: {}\n", "parts.unknown-part: is not a part of this pipeline; its parts are fixture-build, fixture-floor and fixture-test"),
            ("version: 1\nparts:\n  fixture-test:\n    paths: src\n", "parts.fixture-test.paths: expected a list, found a string"),
            ("version: 1\nparts:\n  fixture-test:\n    paths: []\n", "parts.fixture-test.paths: needs at least 1 item"),
            ("version: 1\nparts:\n  fixture-build:\n    paths: [src/]\n", "parts.fixture-build.paths: is not a setting of the part fixture-build"),
            ("version: 1\nlocal-workflows:\n  - workflow: .github/workflows/nightly.yml\n", "local-workflows[0]: is neither"),
            ("version: 1\nlocal-workflows:\n  - workflow: nightly.yml\n    request: 4\n    waits-for: fixture-deploy\n", "local-workflows[0].workflow: does not match"),
            ("version: 1\nparts: [\n", "line 2"),
            ("- version\n", "expected a mapping, found a list"),
        ):
            with self.subTest(configuration=configuration):
                self.commit({CONFIGURATION: configuration})
                outcome = self.plan()
                self.assertEqual(1, outcome.status)
                self.assertIn(expected, outcome.summary)

    def test_a_repository_without_a_configuration_file_fails_and_is_told_the_smallest_valid_file(self) -> None:
        self.commit({"README.md": "A consumer.\n"})
        outcome = self.plan()
        self.assertEqual(1, outcome.status)
        self.assertIn(".github/ci-config.yml does not exist", outcome.summary)
        self.assertIn("version: 1", outcome.summary)

    def test_a_part_stated_without_a_value_is_switched_on(self) -> None:
        self.commit({CONFIGURATION: "version: 1\nparts:\n  fixture-build:\n"})
        outcome = self.plan()
        self.assertEqual(0, outcome.status, outcome.summary)
        self.assertTrue(outcome.plan["parts"]["fixture-build"]["run"])
        self.assertEqual({}, outcome.plan["parts"]["fixture-build"]["config"])


class VersionTests(PipelineFixture):
    def test_the_current_and_the_previous_schema_version_are_accepted_and_any_other_version_fails(self) -> None:
        self.schemas = self.schema_folder("three-versions", versions=(1, 2, 3))
        for version, accepted in ((3, True), (2, True), (1, False), (4, False), (0, False)):
            with self.subTest(version=version):
                self.commit({CONFIGURATION: f"version: {version}\nparts:\n  fixture-build: {{}}\n"})
                outcome = self.plan()
                self.assertEqual(0 if accepted else 1, outcome.status, outcome.summary)
                if accepted:
                    self.assertEqual(version, outcome.plan["version"])
                else:
                    self.assertIn(f"version: {version} is not a version this pipeline reads; it reads the versions 3 and 2", outcome.summary)

    def test_a_configuration_of_the_previous_version_is_checked_against_the_previous_schema(self) -> None:
        self.schemas = self.schema_folder("two-versions", versions=(1, 2))
        fragment = self.schemas / "v2" / "parts" / "fixture-build.json"
        fragment.write_text(fragment.read_text(encoding="utf-8").replace('"properties": {}', '"properties": {"target": {"type": "string"}}'), encoding="utf-8")
        for version, accepted in ((2, True), (1, False)):
            with self.subTest(version=version):
                self.commit({CONFIGURATION: f"version: {version}\nparts:\n  fixture-build:\n    target: all\n"})
                outcome = self.plan()
                self.assertEqual(0 if accepted else 1, outcome.status, outcome.summary)
        self.assertIn("parts.fixture-build.target: is not a setting of the part fixture-build", outcome.summary)

    def test_with_one_schema_version_only_that_version_is_accepted(self) -> None:
        self.commit({CONFIGURATION: "version: 0\n"})
        outcome = self.plan()
        self.assertEqual(1, outcome.status)
        self.assertIn("version: 0 is not a version this pipeline reads; it reads the version 1", outcome.summary)

    def test_a_version_that_is_no_number_fails_as_a_version(self) -> None:
        self.commit({CONFIGURATION: "version: latest\n"})
        outcome = self.plan()
        self.assertEqual(1, outcome.status)
        self.assertIn("version: latest is not a version this pipeline reads", outcome.summary)


class PartDecisionTests(PipelineFixture):
    CONFIGURATION_WITH_PATHS = 'version: 1\nparts:\n  fixture-test:\n    paths: ["src/", "tests/**/*.py", pyproject.toml, "docs/*.md"]\n'

    def decision(self, outcome, part: str = "fixture-test") -> tuple[bool, str]:
        self.assertEqual(0, outcome.status, outcome.summary)
        return outcome.plan["parts"][part]["run"], outcome.plan["parts"][part]["reason"]

    def test_a_part_that_always_runs_runs_whatever_the_configuration_says(self) -> None:
        for configuration in ("version: 1\n", "version: 1\nparts: {}\n", "version: 1\nparts:\n  fixture-build: {}\n"):
            with self.subTest(configuration=configuration):
                self.commit({CONFIGURATION: configuration})
                self.assertEqual((True, "always runs"), self.decision(self.plan(), "fixture-floor"))

    def test_the_configuration_file_cannot_state_a_part_that_always_runs(self) -> None:
        for value in (" {}", "\n    enabled: false", "\n    paths: [src/]", " false"):
            with self.subTest(value=value):
                self.commit({CONFIGURATION: f"version: 1\nparts:\n  fixture-floor:{value}\n"})
                outcome = self.plan()
                self.assertEqual(1, outcome.status)
                self.assertIn("parts.fixture-floor: always runs and has no settings; the configuration file cannot state it", outcome.summary)

    def test_a_part_that_the_configuration_does_not_state_does_not_run(self) -> None:
        self.commit({CONFIGURATION: "version: 1\n"})
        self.assertEqual((False, "switched off"), self.decision(self.plan()))

    def test_a_switched_on_part_without_paths_runs_on_every_change(self) -> None:
        base = self.commit({CONFIGURATION: "version: 1\nparts:\n  fixture-build: {}\n"})
        head = self.commit({"notes.txt": "a note\n"})
        self.assertEqual((True, "switched on"), self.decision(self.plan("--base", base, "--head", head), "fixture-build"))

    def test_a_part_with_paths_runs_only_when_a_changed_path_matches_them(self) -> None:
        base = self.commit({CONFIGURATION: self.CONFIGURATION_WITH_PATHS, "src/app/main.py": "", "tests/unit/test_main.py": "", "docs/guide/use.md": ""})
        for path, runs in (("src/app/main.py", True), ("src/new.py", True), ("tests/unit/test_main.py", True), ("tests/unit/data.json", False),
                           ("pyproject.toml", True), ("docs/index.md", True), ("docs/guide/use.md", False), ("srcs/other.py", False),
                           ("pyproject.toml.bak", False), ("README.md", False)):
            with self.subTest(path=path):
                git(self.repository, "reset", "--quiet", "--hard", base)
                head = self.commit({path: "changed\n"})
                run, reason = self.decision(self.plan("--base", base, "--head", head))
                self.assertEqual(runs, run, reason)
                self.assertEqual(f"{path} changed" if runs else "no changed path matches its paths", reason)

    def test_a_removed_and_a_renamed_file_count_as_changes_of_their_paths(self) -> None:
        base = self.commit({CONFIGURATION: self.CONFIGURATION_WITH_PATHS, "src/old.py": "one\ntwo\nthree\n", "other/keep.py": "four\nfive\nsix\n"})
        head = self.commit({"src/old.py": None})
        self.assertEqual((True, "src/old.py changed"), self.decision(self.plan("--base", base, "--head", head)))
        git(self.repository, "reset", "--quiet", "--hard", base)
        git(self.repository, "mv", "src/old.py", "other/moved.py")
        head = self.commit({})
        self.assertEqual((True, "src/old.py changed"), self.decision(self.plan("--base", base, "--head", head)))

    def test_a_pull_request_is_compared_with_its_merge_base_and_a_push_with_its_previous_head(self) -> None:
        fork_point = self.commit({CONFIGURATION: self.CONFIGURATION_WITH_PATHS, "README.md": "one\n"})
        git(self.repository, "switch", "--quiet", "-c", "topic")
        head = self.commit({"README.md": "two\n"})
        git(self.repository, "switch", "--quiet", "main")
        base = self.commit({"src/app.py": "moved on\n"})
        self.assertEqual((False, "no changed path matches its paths"), self.decision(self.plan("--base", base, "--head", head, "--compare", "merge-base")))
        self.assertEqual((True, "src/app.py changed"), self.decision(self.plan("--base", fork_point, "--head", base, "--compare", "direct")))

    def test_a_part_with_paths_runs_when_the_changed_files_are_unknown(self) -> None:
        head = self.commit({CONFIGURATION: self.CONFIGURATION_WITH_PATHS})
        for arguments in ((), ("--base", "0" * 40, "--head", head), ("--base", "1" * 40, "--head", head), ("--base", "", "--head", head)):
            with self.subTest(arguments=arguments):
                self.assertEqual((True, "the changed files are unknown"), self.decision(self.plan(*arguments)))

    def test_a_part_with_paths_runs_when_its_own_configuration_changed(self) -> None:
        base = self.commit({CONFIGURATION: "version: 1\n"})
        switched_on = self.commit({CONFIGURATION: self.CONFIGURATION_WITH_PATHS})
        self.assertEqual((True, "its configuration changed"), self.decision(self.plan("--base", base, "--head", switched_on)))
        other_value = self.commit({CONFIGURATION: self.CONFIGURATION_WITH_PATHS + "    level: high\n"})
        self.assertEqual((True, "its configuration changed"), self.decision(self.plan("--base", switched_on, "--head", other_value)))
        other_part = self.commit({CONFIGURATION: self.CONFIGURATION_WITH_PATHS + "    level: high\n  fixture-build: {}\n"})
        self.assertEqual((False, "no changed path matches its paths"), self.decision(self.plan("--base", other_value, "--head", other_part)))

    def test_the_plan_gives_each_part_its_configuration(self) -> None:
        self.commit({CONFIGURATION: "version: 1\nparts:\n  fixture-test:\n    level: high\n"})
        outcome = self.plan()
        self.assertEqual({"fixture-test": {"level": "high"}, "fixture-build": {}, "fixture-floor": {}},
                         {name: part["config"] for name, part in outcome.plan["parts"].items()})


class SharedWorkflowPlanTests(PipelineFixture):
    """The plan of another shared workflow than the pipeline, asked for with ``--shared-workflow``: it decides on the
    parts whose fragment names that workflow, and on those alone."""

    PARTS = {**FIXTURE_PARTS,
             "fixture-merge": {"description": "An invented part of another shared workflow.", "workflow": "automation.yml",
                               "configuration": {"type": "object", "additionalProperties": False, "properties": {"list": {"type": "string"}}}},
             "fixture-label": {"description": "A second invented part of that workflow.", "workflow": "automation.yml",
                               "configuration": {"type": "object", "additionalProperties": False, "properties": {}}}}
    STATED = "version: 1\nparts:\n  fixture-build: {}\n  fixture-merge:\n    list: lists/suites.json\n"

    def setUp(self) -> None:
        super().setUp()
        self.schemas = self.schema_folder("other", versions=(1,), parts=self.PARTS)

    def test_the_pipeline_s_plan_leaves_out_a_part_of_another_workflow_that_the_configuration_states(self) -> None:
        self.commit({CONFIGURATION: self.STATED})
        outcome = self.plan("--workflow", "CI", "--event", "push", "--run-id", "7")
        self.assertEqual(0, outcome.status, outcome.summary)
        self.assertEqual(["fixture-build", "fixture-floor", "fixture-test"], list(outcome.plan["parts"]))
        self.assertNotIn("fixture-merge", outcome.summary)

    def test_the_plan_of_the_named_workflow_decides_on_its_parts_alone(self) -> None:
        self.commit({CONFIGURATION: self.STATED})
        outcome = self.plan("--shared-workflow", "automation.yml", "--workflow", "Automation", "--event", "schedule", "--run-id", "7")
        self.assertEqual(0, outcome.status, outcome.summary)
        self.assertEqual({"fixture-label": {"run": False, "reason": "switched off", "config": {}},
                          "fixture-merge": {"run": True, "reason": "switched on", "config": {"list": "lists/suites.json"}}}, outcome.plan["parts"])
        self.assertNotIn("fixture-floor", outcome.summary, "a part that always runs in the pipeline does not run in another workflow")

    def test_a_setting_that_a_part_of_another_workflow_does_not_have_fails_both_plans(self) -> None:
        self.commit({CONFIGURATION: "version: 1\nparts:\n  fixture-merge:\n    lists: x\n"})
        for arguments in ((), ("--shared-workflow", "automation.yml")):
            with self.subTest(arguments=arguments):
                outcome = self.plan(*arguments)
                self.assertEqual(1, outcome.status)
                self.assertIn("parts.fixture-merge.lists: is not a setting of the part fixture-merge", outcome.summary)

    def test_a_part_of_another_workflow_that_is_switched_off_compared_with_the_base_is_named_by_both_plans(self) -> None:
        base = self.commit({CONFIGURATION: self.STATED})
        head = self.commit({CONFIGURATION: "version: 1\nparts:\n  fixture-build: {}\n"})
        for arguments in ((), ("--shared-workflow", "automation.yml")):
            with self.subTest(arguments=arguments):
                self.assertEqual(["fixture-merge"], self.plan("--base", base, "--head", head, *arguments).plan["switched-off"])

    def test_a_fragment_that_names_no_workflow_file_or_always_runs_in_another_workflow_fails(self) -> None:
        configuration = {"type": "object", "additionalProperties": False, "properties": {}}
        for fragment, message in (({"description": "x", "workflow": "automation", "configuration": configuration}, "workflow names a workflow file, such as release.yml"),
                                  ({"description": "x", "workflow": 3, "configuration": configuration}, "workflow names a workflow file, such as release.yml"),
                                  ({"description": "x", "workflow": "automation.yml", "always": True}, "a part that always runs is a part of the pipeline")):
            with self.subTest(fragment=fragment):
                self.schemas = self.schema_folder(f"broken-{len(str(fragment))}", versions=(1,), parts={"fixture-merge": fragment})
                self.commit({CONFIGURATION: "version: 1\n"})
                outcome = self.plan()
                self.assertEqual(1, outcome.status)
                self.assertIn(message, outcome.summary)


class ComparisonWithTheBaseTests(PipelineFixture):
    def test_the_plan_names_every_part_switched_off_compared_with_the_base(self) -> None:
        base = self.commit({CONFIGURATION: "version: 1\nparts:\n  fixture-test:\n    level: low\n  fixture-build: {}\n"})
        head = self.commit({CONFIGURATION: "version: 1\nparts:\n  fixture-build: {}\n"})
        outcome = self.plan("--base", base, "--head", head)
        self.assertEqual(["fixture-test"], outcome.plan["switched-off"])
        self.assertEqual([], self.plan("--base", head, "--head", head).plan["switched-off"])
        removed = self.commit({CONFIGURATION: "version: 1\n"})
        self.assertEqual(["fixture-build", "fixture-test"], self.plan("--base", base, "--head", removed).plan["switched-off"])

    def test_the_plan_names_every_local_workflow_registered_compared_with_the_base(self) -> None:
        temporary = "  - workflow: .github/workflows/nightly.yml\n    request: 4\n    waits-for: fixture-deploy\n"
        permanent = "  - workflow: .github/workflows/monitor.yaml\n    declined-request: 9\n"
        base = self.commit({CONFIGURATION: "version: 1\nlocal-workflows:\n" + temporary})
        head = self.commit({CONFIGURATION: "version: 1\nlocal-workflows:\n" + temporary + permanent})
        outcome = self.plan("--base", base, "--head", head)
        self.assertEqual(0, outcome.status, outcome.summary)
        self.assertEqual([".github/workflows/monitor.yaml"], outcome.plan["local-workflows-added"])
        first = self.commit({CONFIGURATION: "version: 1\n"})
        both = self.commit({CONFIGURATION: "version: 1\nlocal-workflows:\n" + temporary + permanent})
        self.assertEqual([".github/workflows/nightly.yml", ".github/workflows/monitor.yaml"], self.plan("--base", first, "--head", both).plan["local-workflows-added"])

    def test_a_base_without_a_readable_configuration_file_gives_no_part_switched_off_and_every_registration_as_added(self) -> None:
        registered = "version: 1\nlocal-workflows:\n  - workflow: .github/workflows/monitor.yml\n    declined-request: 9\n"
        for base_files in ({"README.md": "no configuration yet\n"}, {CONFIGURATION: "version: [\n"}):
            with self.subTest(base=base_files):
                base = self.commit(base_files)
                head = self.commit({CONFIGURATION: registered})
                outcome = self.plan("--base", base, "--head", head)
                self.assertEqual(0, outcome.status, outcome.summary)
                self.assertEqual([], outcome.plan["switched-off"])
                self.assertEqual([".github/workflows/monitor.yml"], outcome.plan["local-workflows-added"])

    def test_without_a_known_base_the_plan_says_that_the_comparison_is_missing(self) -> None:
        self.commit({CONFIGURATION: "version: 1\n"})
        outcome = self.plan()
        self.assertEqual(0, outcome.status, outcome.summary)
        self.assertFalse(outcome.plan["compared"])
        self.assertIn("could not be compared with the base", outcome.summary)


class OtherWorkflowTests(PipelineFixture):
    """A part whose fragment names another shared workflow: the configuration file states it, and the pipeline leaves it
    to that workflow."""

    PARTS = {"fixture-build": {"description": "An invented part without settings.",
                               "configuration": {"type": "object", "additionalProperties": False, "properties": {}}},
             "fixture-publish": {"description": "An invented part that another shared workflow runs.", "workflow": "fixture.yml",
                                 "configuration": {"type": "object", "additionalProperties": False, "properties": {"level": {"type": "string"}}}}}

    def setUp(self) -> None:
        super().setUp()
        self.schemas = self.schema_folder("other", versions=(1,), parts=self.PARTS)

    def test_the_configuration_file_states_a_part_of_another_workflow_and_the_pipeline_s_plan_leaves_it_out(self) -> None:
        self.commit({CONFIGURATION: "version: 1\nparts:\n  fixture-publish:\n    level: high\n"})
        outcome = self.plan("--workflow", "CI", "--event", "push", "--run-id", "7")
        self.assertEqual(0, outcome.status, outcome.summary)
        self.assertEqual(["fixture-build"], list(outcome.plan["parts"]))
        self.assertNotIn("fixture-publish", outcome.summary)

    def test_a_setting_that_such_a_part_does_not_have_fails_the_plan(self) -> None:
        self.commit({CONFIGURATION: "version: 1\nparts:\n  fixture-publish:\n    other: 1\n"})
        outcome = self.plan()
        self.assertEqual(1, outcome.status)
        self.assertIn("parts.fixture-publish.other: is not a setting of the part fixture-publish", outcome.summary)

    def test_a_part_of_another_workflow_that_a_change_switches_off_is_named(self) -> None:
        base = self.commit({CONFIGURATION: "version: 1\nparts:\n  fixture-publish: {}\n"})
        head = self.commit({CONFIGURATION: "version: 1\n"})
        self.assertEqual(["fixture-publish"], self.plan("--base", base, "--head", head).plan["switched-off"])

    def test_a_part_that_is_no_part_of_the_schema_is_told_every_part_that_the_file_can_state(self) -> None:
        self.commit({CONFIGURATION: "version: 1\nparts:\n  unknown: {}\n"})
        self.assertIn("parts.unknown: is not a part of this pipeline; its parts are fixture-build and fixture-publish", self.plan().summary)

    def test_a_fragment_names_a_workflow_file_and_a_part_that_always_runs_names_none(self) -> None:
        for number, (fragment, message) in enumerate((
                ({**self.PARTS["fixture-publish"], "workflow": "fixture"}, "workflow names a workflow file, such as release.yml"),
                ({**self.PARTS["fixture-publish"], "workflow": "../fixture.yml"}, "workflow names a workflow file, such as release.yml"),
                ({"description": "x", "always": True, "workflow": "fixture.yml"}, "a part that always runs is a part of the pipeline"))):
            with self.subTest(message=message):
                self.schemas = self.schema_folder(f"broken-{number}", versions=(1,), parts={"fixture-publish": fragment})
                self.commit({CONFIGURATION: "version: 1\n"})
                outcome = self.plan()
                self.assertEqual(1, outcome.status)
                self.assertIn(message, outcome.summary)


if __name__ == "__main__":
    unittest.main()
