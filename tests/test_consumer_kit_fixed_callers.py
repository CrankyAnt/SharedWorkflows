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
CI_GUIDE = "docs/agents/ci.md"
AT_V1 = "https://github.com/CrankyAnt/SharedWorkflows/blob/v1/"
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


class CiGuideTemplateTest(unittest.TestCase):
    """The fixed pointer and principles of a consumer's ``docs/agents/ci.md``."""

    def setUp(self) -> None:
        self.template = (KIT / CI_GUIDE).read_text(encoding="utf-8")
        self.readme = (KIT / "README.md").read_text(encoding="utf-8")

    def test_the_template_points_to_the_catalogue_and_the_schema_at_v1_and_names_the_configuration_file(self) -> None:
        self.assertIn(f"]({AT_V1}catalogue.json)", self.template)
        self.assertIn(f"]({AT_V1}actions/pipeline/schema/v1/ci-config.schema.json)", self.template)
        self.assertIn("`.github/ci-config.yml`", self.template)
        self.assertTrue((REPOSITORY_ROOT / "catalogue.json").is_file())
        self.assertTrue((REPOSITORY_ROOT / "actions/pipeline/schema/v1/ci-config.schema.json").is_file())

    def test_every_link_of_the_template_is_absolute_because_the_file_lives_in_another_repository(self) -> None:
        targets = re.findall(r"\]\(([^)]+)\)", self.template)
        self.assertTrue(targets)
        for target in targets:
            with self.subTest(target=target):
                self.assertTrue(target.startswith("https://github.com/CrankyAnt/SharedWorkflows"))
                self.assertNotIn("/main/", target, "a consumer reads the shared parts at v1")

    def test_the_fixed_part_holds_the_pointer_and_principles_without_repository_specific_content(self) -> None:
        self.assertNotIn("holds no rule of its own", self.template)
        self.assertIn("principles", self.template.split("\n\n", 2)[1])
        self.assertEqual(["# CI", "## Principles", "## Repository choices"], re.findall(r"(?m)^#+ .*$", self.template))
        self.assertEqual([], NOT_FIXED.findall(self.template))
        self.assertEqual([], re.findall(r"tests/|maintaining\.md|ci-policy|suites\.json", self.template))

    def test_the_ten_principles_equal_this_repository_s_principles_word_for_word(self) -> None:
        own = (REPOSITORY_ROOT / CI_GUIDE).read_text(encoding="utf-8")
        names = ("Purpose", "Selection", "Sound selection", "Running everything", "Reuse", "One verdict", "Jobs", "Tests", "Changing CI", "Time and scaling")
        sections = [text.split("\n## Principles\n", 1)[1].split("\n## ", 1)[0] for text in (own, self.template)]
        for name in names:
            with self.subTest(principle=name):
                pattern = rf"(?ms)^- \*\*{re.escape(name)}\.\*\* .*?(?=^- \*\*|\Z)"
                source, template = [[principle.rstrip() for principle in re.findall(pattern, section)] for section in sections]
                self.assertEqual(1, len(source), f"{name}: the repository must hold the principle once")
                self.assertEqual(1, len(template), f"{name}: the template must hold the principle once")
                self.assertEqual(source, template, f"{name}: the principle must match word for word")

    def test_the_tests_principle_states_the_layout_conventions_in_at_most_three_sentences_that_name_no_language_or_runner(self) -> None:
        (principle,) = re.findall(r"(?m)^- \*\*Tests\.\*\* (.*)$", self.template)
        opening = "Each suite has one subject and every test a place in one; the entry file is the smallest unit of selection. "
        closing = (" A test starts the real command for what only a real process shows and for each public command's main path; "
                   "where that start is expensive, every other case runs without a new process.")
        self.assertTrue(principle.startswith(opening))
        self.assertTrue(principle.endswith(closing))
        layout = principle[len(opening):-len(closing)]
        self.assertIn(len(re.findall(r"\.(?: |$)", layout)), (1, 2, 3), "the layout conventions take at most three sentences")
        for convention in ("The entry files of a suite share one name rule, of a directory and a file name, that matches no other tracked file",
                           "a suite whose entry files share no such rule, or that has one entry file, names them by path",
                           "Helpers, fixtures and data stand in files or directories that no such rule matches",
                           "a name that the suite's runner loads", "unique where the runner requires it"):
            with self.subTest(convention=convention):
                self.assertIn(convention, layout)
        for text in (self.template, (REPOSITORY_ROOT / CI_GUIDE).read_text(encoding="utf-8")):
            self.assertNotIn("a directory of their own", text, "a suite needs a name rule, not a directory of its own")
        self.assertNotRegex(layout, r"(?i)python|pytest|unittest|\bgo\b|jest|java|maven|surefire|cargo|rust|node|\.net|bazel|\.[a-z]{2,4}\b|[*_/]")

    def test_the_principles_open_with_shared_ci_and_the_consumer_s_own_work(self) -> None:
        paragraph = self.template.split("\n## Principles\n\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("CI work runs in a shared part unless the catalogue's decision rule places it in the repository.", paragraph)
        self.assertIn("principles apply to the CI that the repository builds itself", paragraph)
        self.assertIn("script of the free job", paragraph)
        self.assertIn("registered local workflows", paragraph)

    def test_the_template_ends_with_the_heading_below_which_a_consumer_records_its_choices(self) -> None:
        self.assertTrue(self.template.endswith("\n\n## Repository choices\n"), "the comparison stops at this heading, so nothing fixed follows it")
        self.assertEqual(1, self.template.count("\n## Repository choices\n"))
        self.assertIn("`## Repository choices`", self.readme)
        self.assertIn("A choice of this repository that no file states is recorded with its reason below `## Repository choices`.", self.template)

    def test_the_readme_says_that_a_consumer_holds_the_ci_guide_unchanged(self) -> None:
        self.assertIn(f"[{CI_GUIDE}]({CI_GUIDE})", self.readme)
        self.assertIn("## The CI guide", self.readme)
        self.assertIn("| CI guide |", self.readme)


class RequestReadmeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.readme = (KIT / "README.md").read_text(encoding="utf-8")
        # The section holds a fenced heading of the vocabulary entry, so it ends at the next section by name.
        self.section = self.readme.split("\n## Requests for shared CI\n", 1)[1].split("\n## A corrected title\n", 1)[0]

    def test_the_readme_states_the_vocabulary_entry_for_a_consumer_s_triage_labels(self) -> None:
        self.assertIn("`docs/agents/triage-labels.md`", self.section)
        entry = self.section.split("```markdown\n", 1)[1].split("```", 1)[0]
        self.assertIn("| `shared-ci` |", entry)
        self.assertIn("another workflow's vocabulary", entry)
        self.assertIn("never carry a Type, Triage, Ready, Status or Resolution label", entry)
        self.assertRegex(entry, r"(?m)^## ")

    def test_the_readme_says_where_a_request_goes_and_who_closes_it(self) -> None:
        for text in ("an issue in the repository itself", "`shared-ci`", "general terms", "closes the request", "(../../../catalogue.json)"):
            with self.subTest(text=text):
                self.assertIn(text, self.section)

    def test_the_readme_states_the_switch_over_check(self) -> None:
        row = next(line for line in self.readme.splitlines() if line.startswith("| Switch-over |"))
        for text in ("`waits-for`", "exists", "`<part>.<setting>`", "switch the part on", "remove the workflow", "close the request"):
            with self.subTest(text=text):
                self.assertIn(text, row)


if __name__ == "__main__":
    unittest.main()
