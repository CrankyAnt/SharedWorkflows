"""The Dependabot check of the pipeline's hygiene job: `.github/dependabot.yml` equals the file generated from the
Dependabot ecosystems of the configuration file."""

from __future__ import annotations

import subprocess
import sys
import unittest

from tests.consumer_checks_fixtures import SHARED, ConsumerFixture, consumer_checks
from tests.consumer_kit_support import assert_update_entry_follows_the_template, read_yaml_text
from tests.pipeline_fixtures import CONFIGURATION, REPOSITORY_ROOT, SCHEMAS, git

DEPENDABOT = ".github/dependabot.yml"
STATED = "version: 1\ndependabot:\n  github-actions: [/]\n  pip: [/, /tools/report]\n"
ENTRY_TAIL = """    schedule:
      interval: weekly
    cooldown:
      default-days: 7
    groups:
      minor-and-patch:
        patterns: ["*"]
        update-types: [minor, patch]
"""
# The file that the configuration above generates, written out by hand.
GENERATED = (
    "version: 2\nupdates:\n"
    "  - package-ecosystem: github-actions\n    directory: /\n" + ENTRY_TAIL + f"    ignore:\n      - dependency-name: {SHARED}\n"
    "  - package-ecosystem: pip\n    directory: /\n" + ENTRY_TAIL +
    "  - package-ecosystem: pip\n    directory: /tools/report\n" + ENTRY_TAIL
)


class DependabotCheckTests(ConsumerFixture):
    def test_a_package_file_under_coverage_exclude_needs_no_ecosystem_and_the_generated_file_holds_no_entry_for_it(self) -> None:
        self.schemas = SCHEMAS
        configuration = STATED + "parts:\n  repository-hygiene:\n    coverage-exclude: [tests/fixtures/package.json]\n"
        self.consumer({CONFIGURATION: configuration, DEPENDABOT: GENERATED, "tests/fixtures/package.json": "{}\n"})
        checked = self.start("dependabot")
        self.assertEqual(0, checked.status, checked.stdout + checked.stderr)
        self.assertIn(f"`{DEPENDABOT}` equals the file generated from `dependabot` in `{CONFIGURATION}`", checked.summary)
        self.assertNotIn("npm", GENERATED)
        self.assertNotIn("/tests/fixtures", GENERATED)
        git(self.repository, "add", "--all")
        self.assertEqual(0, self.coverage(), "the exclusion covers the fixture, so the coverage check passes beside the generated file")
        self.consumer({CONFIGURATION: STATED})
        self.assertEqual(1, self.coverage(), "without the exclusion the same fixture needs an ecosystem")

    def coverage(self) -> int:
        """The coverage check of the pipeline's hygiene job: the setting coverage-exclude piped into hygiene.py coverage."""
        excluded = subprocess.run([sys.executable, str(REPOSITORY_ROOT / "actions" / "pipeline" / "plan.py"), "setting", "--root", str(self.repository),
                                   "--part", "repository-hygiene", "--setting", "coverage-exclude"], capture_output=True, text=True, encoding="utf-8", check=True).stdout
        return subprocess.run([sys.executable, str(REPOSITORY_ROOT / "actions" / "repository-hygiene" / "hygiene.py"), "coverage", "--root", str(self.repository),
                               "--exclude-from", "-"], input=excluded, capture_output=True, text=True, encoding="utf-8").returncode

    def test_a_file_that_equals_the_generated_one_passes(self) -> None:
        self.consumer({CONFIGURATION: STATED, DEPENDABOT: GENERATED})
        checked = self.start("dependabot")
        self.assertEqual(0, checked.status, checked.stdout + checked.stderr)
        self.assertIn(f"`{DEPENDABOT}` equals the file generated from `dependabot` in `{CONFIGURATION}`", checked.summary)

    def test_a_file_that_differs_fails_with_the_expected_content_in_the_summary(self) -> None:
        self.consumer({CONFIGURATION: STATED, DEPENDABOT: GENERATED.replace("interval: weekly", "interval: daily", 1)})
        checked = self.start("dependabot")
        self.assertEqual(1, checked.status, checked.stdout + checked.stderr)
        self.assertIn(f"::error file={DEPENDABOT}::", checked.stdout)
        self.assertIn(f"`{DEPENDABOT}` differs from the file generated from `dependabot` in `{CONFIGURATION}`", checked.summary)
        self.assertIn(f"The expected content of `{DEPENDABOT}`:\n\n```yaml\n{GENERATED}```\n", checked.summary)

    def test_a_missing_file_fails_with_the_expected_content(self) -> None:
        self.consumer({CONFIGURATION: STATED})
        checked = self.check("dependabot")
        self.assertEqual(1, checked.status)
        self.assertIn(f"`{DEPENDABOT}` does not exist", checked.summary)
        self.assertIn(f"```yaml\n{GENERATED}```", checked.summary)

    def test_without_ecosystems_the_repository_holds_no_dependabot_configuration(self) -> None:
        checked = self.check("dependabot")
        self.assertEqual(0, checked.status)
        self.assertIn("states no Dependabot ecosystems, and the repository holds no Dependabot configuration", checked.summary)
        for name in (DEPENDABOT, ".github/dependabot.yaml"):
            with self.subTest(file=name):
                self.consumer({name: GENERATED})
                checked = self.check("dependabot")
                self.assertEqual(1, checked.status)
                self.assertIn(f"`{name}` exists, but `{CONFIGURATION}` states no Dependabot ecosystems under `dependabot`", checked.summary)
                self.consumer({name: None})

    def test_the_file_under_its_other_name_fails(self) -> None:
        self.consumer({CONFIGURATION: STATED, DEPENDABOT: GENERATED, ".github/dependabot.yaml": GENERATED})
        checked = self.check("dependabot")
        self.assertEqual(1, checked.status)
        self.assertIn(f"`.github/dependabot.yaml` exists; the generated file is `{DEPENDABOT}`", checked.summary)

    def test_a_dependabot_pull_request_and_a_push_get_a_notice(self) -> None:
        self.consumer({CONFIGURATION: STATED})
        for event, author in (("pull_request", "dependabot[bot]"), ("push", "")):
            with self.subTest(event=event):
                checked = self.check("dependabot", event=event, author=author)
                self.assertEqual(0, checked.status, checked.stdout)
                self.assertIn("**Notice.**", checked.summary)
                self.assertIn(f"```yaml\n{GENERATED}```", checked.summary)

    def test_a_configuration_file_that_cannot_be_used_fails(self) -> None:
        self.consumer({CONFIGURATION: "version: 1\ndependabot:\n  pip: tools\n"})
        checked = self.check("dependabot")
        self.assertEqual(1, checked.status)
        self.assertIn(f"`{CONFIGURATION}` cannot be used", checked.summary)
        self.assertIn("dependabot.pip: expected a list", checked.summary)

    def test_the_repository_of_the_shared_parts_passes_without_a_check(self) -> None:
        self.consumer({DEPENDABOT: "version: 2\n"})
        checked = self.check("dependabot", repository=SHARED)
        self.assertEqual(0, checked.status)
        self.assertIn("This repository holds the shared parts", checked.summary)


class GeneratedFileTests(unittest.TestCase):
    def test_every_generated_entry_follows_the_template_of_an_update_entry(self) -> None:
        _, document = read_yaml_text(GENERATED)
        self.assertEqual(2, document["version"])
        for entry in document["updates"]:
            assert_update_entry_follows_the_template(self, entry)
        self.assertEqual([{"dependency-name": SHARED}], document["updates"][0]["ignore"])
        self.assertNotIn("ignore", document["updates"][1])

    def test_the_schema_of_version_1_states_the_ecosystems_with_their_directories(self) -> None:
        versions = consumer_checks.plan.schema_versions(REPOSITORY_ROOT / "actions" / "pipeline" / "schema")
        for text, problem in ((STATED, None), ("version: 1\ndependabot:\n  pip: []\n", "dependabot.pip: needs at least 1 item"),
                              ("version: 1\ndependabot:\n  pip: [tools]\n", "dependabot.pip[0]: does not match the pattern ^/"),
                              ("version: 1\ndependabot:\n  pip: [/, /]\n", "dependabot.pip: holds '/' twice")):
            with self.subTest(text=text):
                if problem is None:
                    consumer_checks.plan.checked_configuration(text, versions)
                else:
                    with self.assertRaises(consumer_checks.plan.PlanError) as raised:
                        consumer_checks.plan.checked_configuration(text, versions)
                    self.assertEqual(problem, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
