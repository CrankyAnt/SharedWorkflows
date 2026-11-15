"""The plan of the pipeline on fixture consumers: which configuration files it accepts, which parts run on which
changes, and what it reports as switched off or newly registered compared with the base."""

from __future__ import annotations

import json
import unittest

from tests.pipeline_fixtures import CONFIGURATION, PipelineFixture, git


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


class ConcurrencyTests(PipelineFixture):
    def concurrency(self, *arguments: str) -> dict:
        return json.loads(self.plan("--workflow", "CI", *arguments).outputs["concurrency"])

    def test_a_pull_request_run_shares_a_group_per_part_with_the_other_runs_of_its_pull_request_and_is_cancelled_by_a_newer_one(self) -> None:
        self.commit({CONFIGURATION: "version: 1\n"})
        groups = self.concurrency("--event", "pull_request", "--pull-request", "12", "--run-id", "700")
        self.assertEqual({"fixture-build": {"group": "CI-fixture-build-pr-12", "cancel-in-progress": True},
                          "fixture-floor": {"group": "CI-fixture-floor-pr-12", "cancel-in-progress": True},
                          "fixture-test": {"group": "CI-fixture-test-pr-12", "cancel-in-progress": True}}, groups)
        self.assertEqual(groups, self.concurrency("--event", "pull_request", "--pull-request", "12", "--run-id", "701"))

    def test_a_push_run_has_groups_of_its_own_and_is_never_cancelled(self) -> None:
        self.commit({CONFIGURATION: "version: 1\n"})
        groups = self.concurrency("--event", "push", "--pull-request", "", "--run-id", "700")
        self.assertEqual({"group": "CI-fixture-floor-run-700", "cancel-in-progress": False}, groups["fixture-floor"])
        self.assertNotEqual(groups, self.concurrency("--event", "push", "--pull-request", "", "--run-id", "701"))

    def test_a_plan_that_fails_on_the_configuration_file_still_gives_the_concurrency_of_every_part(self) -> None:
        self.commit({CONFIGURATION: "version: 9\n"})
        outcome = self.plan("--workflow", "CI", "--event", "pull_request", "--pull-request", "3", "--run-id", "5")
        self.assertEqual(1, outcome.status)
        self.assertNotIn("plan", outcome.outputs)
        self.assertEqual({"group": "CI-fixture-floor-pr-3", "cancel-in-progress": True}, json.loads(outcome.outputs["concurrency"])["fixture-floor"])


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


if __name__ == "__main__":
    unittest.main()
