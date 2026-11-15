"""The consumer kit of the pipeline: the templates of the fixed callers, which a consumer holds unchanged, and what its
README tells a consumer about them."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from tests.consumer_kit_support import assert_caller_passes_no_secrets_and_runs_no_step, read_yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
KIT = REPOSITORY_ROOT / "actions" / "pipeline" / "consumer-kit"
TEMPLATES = ("ci.yml", "automation.yml", "release.yml")
# What only this repository has, and what a template that a repository fills in would hold.
NOT_FIXED = re.compile(r"\$/|<[a-z-]+>|@[0-9a-f]{40}")


class FixedCallerTemplatesTest(unittest.TestCase):
    def test_a_template_holds_no_placeholder_and_nothing_of_this_repository_alone(self) -> None:
        for name in TEMPLATES:
            with self.subTest(template=name):
                self.assertEqual([], NOT_FIXED.findall((KIT / name).read_text(encoding="utf-8")))

    def test_each_template_is_one_job_named_after_its_file_that_calls_its_shared_workflow_at_v1(self) -> None:
        for name, shared in (("ci.yml", "pipeline.yml"), ("automation.yml", "automation.yml"), ("release.yml", "release.yml")):
            with self.subTest(template=name):
                _, caller = read_yaml(KIT / name)
                self.assertEqual({}, caller["permissions"])
                self.assertEqual([name.removesuffix(".yml")], list(caller["jobs"]))
                self.assertEqual(f"CrankyAnt/SharedWorkflows/.github/workflows/{shared}@v1", caller["jobs"][name.removesuffix(".yml")]["uses"])

    def test_the_read_caller_and_the_release_caller_pass_no_secret_and_the_write_caller_passes_its_secret_by_name(self) -> None:
        for name in ("ci.yml", "release.yml"):
            with self.subTest(template=name):
                assert_caller_passes_no_secrets_and_runs_no_step(self, (KIT / name).read_text(encoding="utf-8"))
        text, caller = read_yaml(KIT / "automation.yml")
        self.assertEqual({"PUBLICATION_CHECKS_APP_PRIVATE_KEY": "${{ secrets.PUBLICATION_CHECKS_APP_PRIVATE_KEY }}"}, caller["jobs"]["automation"]["secrets"])
        self.assertNotRegex(text, r"(?m)^\s*(?:- )?run:")

    def test_the_read_caller_gives_the_check_ci_verdict_with_read_rights_only(self) -> None:
        _, caller = read_yaml(KIT / "ci.yml")
        self.assertEqual({"pull_request": None, "push": {"branches": ["main"]}, "repository_dispatch": {"types": ["dependabot-merged"]}}, caller["on"])
        self.assertEqual({"actions": "read", "contents": "read", "issues": "read", "pull-requests": "read"}, caller["jobs"]["ci"]["permissions"])

    def test_this_repository_s_own_caller_differs_from_the_template_only_in_the_local_path(self) -> None:
        own = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        template = (KIT / "ci.yml").read_text(encoding="utf-8")
        self.assertEqual(own, template.replace("CrankyAnt/SharedWorkflows/.github/workflows/pipeline.yml@v1", "$/.github/workflows/pipeline.yml"))


class FixedCallerReadmeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.readme = (KIT / "README.md").read_text(encoding="utf-8")

    def test_the_catalogue_names_the_kit_at_the_pipeline(self) -> None:
        catalogue = json.loads((REPOSITORY_ROOT / "catalogue.json").read_text(encoding="utf-8"))
        entry = next(part for part in catalogue["parts"] if part["name"] == "Pipeline")
        self.assertEqual("actions/pipeline/consumer-kit", entry["consumer_kit"])
        self.assertEqual(["ci / verdict"], entry["required_checks"])

    def test_the_readme_says_that_a_consumer_holds_the_callers_unchanged(self) -> None:
        for template in TEMPLATES:
            with self.subTest(template=template):
                self.assertIn(f"[{template}]({template})", self.readme)
                self.assertIn(f"`.github/workflows/{template}`", self.readme)
        self.assertIn("byte for byte", self.readme)

    def test_the_readme_says_that_a_corrected_title_needs_a_re_run(self) -> None:
        self.assertIn("`ci.yml` does not start on `edited`", self.readme)
        self.assertIn("re-run", self.readme)

    def test_the_readme_names_the_one_required_check_and_the_month_of_the_previous_version(self) -> None:
        self.assertIn("`ci / verdict`", self.readme)
        self.assertIn("one month", self.readme)
        self.assertIn("`dependabot[bot]`", self.readme)
        self.assertIn("(../../repository-ruleset/README.md)", self.readme)

    def test_the_readme_states_each_check_of_a_consumer_s_files(self) -> None:
        for text in ("`local-workflows`", "`dependabot`", "`.github/dependabot.yml`", "`request`", "`declined-request`", "closed"):
            with self.subTest(text=text):
                self.assertIn(text, self.readme)


if __name__ == "__main__":
    unittest.main()
