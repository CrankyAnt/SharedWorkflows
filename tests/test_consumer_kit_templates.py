"""The consumer kit of the suite selection holds no workflow template, its README names the switch and the suite list of
the part, and the section of every consumer kit for a repository that adopted its part earlier."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path
from tests.ci_policy_support import BARE_DISPATCH_CONTEXT, expression_value, read_workflow

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SELECTION_KIT = REPOSITORY_ROOT / "actions" / "suite-selection" / "consumer-kit"
# What only this repository has: its suite list and tests, its own validation files, a local-path call of a shared
# workflow or step, and a resolved action pin in place of `<pin>`. A consumer reads the README at `v1`. The `tests/` in the
# address of a source of the criteria is not this repository's folder.
THIS_REPOSITORY_ONLY = re.compile(r"(?<!docs\.datadoghq\.com/)tests/|validate\.yml|maintaining\.md|\$/|@[0-9a-f]{40}")
EARLIER_ADOPTION_HEADING = "## When a repository adopted it earlier"


def readme_section(readme: str, heading: str) -> str:
    """The section under ``heading`` up to the next level-two heading, or to the end of the README."""
    start = readme.index(heading + "\n")
    end = readme.find("\n## ", start + 1)
    return readme[start:] if end == -1 else readme[start:end]


class SelectionConsumerKitTest(unittest.TestCase):
    """The kit is a README alone: the suite selection is a part that the repository switches on, and the shared workflows
    it calls are not templates of the kit."""

    def setUp(self) -> None:
        self.readme = (SELECTION_KIT / "README.md").read_text(encoding="utf-8")

    def test_the_kit_holds_no_workflow_template(self) -> None:
        self.assertEqual([], sorted(path.name for path in SELECTION_KIT.iterdir() if path.suffix in (".yml", ".yaml")))
        self.assertTrue((SELECTION_KIT / "README.md").is_file())

    def test_the_readme_names_the_part_and_the_suite_list_keys_of_the_switch(self) -> None:
        self.assertIn("\n  suite-selection:\n    suites: .github/suites.json\n    language-steps: [python]\n", self.readme)
        self.assertIn("`suite-selection`", self.readme)
        for key in ("`jobs`", "`setups`"):
            with self.subTest(key=key):
                self.assertIn(key, readme_section(self.readme, "## The suite list"))
        self.assertIn('"jobs": {', self.readme)
        self.assertIn('"setups": {', self.readme)

    def test_the_readme_names_the_checks_and_the_one_required_check(self) -> None:
        for check in ("`ci / suite-selection`", "`ci / suite-selection-jobs / shard <n>`", "`ci / suite-selection-jobs / windows`",
                      "`ci / suite-selection-results`", "`ci / verdict`"):
            with self.subTest(check=check):
                self.assertIn(check, self.readme)
        self.assertIn("the one required check: it fails", self.readme)

    def test_the_readme_names_nothing_that_exists_only_in_this_repository(self) -> None:
        self.assertEqual([], THIS_REPOSITORY_ONLY.findall(self.readme))


class SuiteListCriteriaDocumentationTest(unittest.TestCase):
    """The criteria of the suite list: how a suite names its entry files, its unit of selection, and the moves between
    the choices."""

    HEADING = "### Naming entry files and the unit of selection"
    MOVES = ("Path to glob", "Glob to path", "Whole to per entry file", "Per entry file to whole")
    SOURCES = (
        "https://docs.pytest.org/en/stable/explanation/goodpractices.html",
        "https://docs.python.org/3/library/unittest.html#test-discovery",
        "https://pkg.go.dev/cmd/go#hdr-Test_packages",
        "https://jestjs.io/docs/configuration",
        "https://maven.apache.org/surefire/maven-surefire-plugin/examples/inclusion-exclusion.html",
        "https://doc.rust-lang.org/cargo/reference/cargo-targets.html",
        "https://bazel.build/build/style-guide",
        "https://bazel.build/reference/be/functions#glob",
        "https://arxiv.org/abs/1810.05286",
        "https://docs.develocity.ai/predictive-test-selection/",
        "https://docs.datadoghq.com/tests/test_impact_analysis/",
        "https://mir.cs.illinois.edu/~marinov/publications/LegunsenETAL16StaticRTS.pdf",
        "https://users.ece.utexas.edu/~gligoric/papers/Gligoric15PhD.pdf",
        "https://docs.develocity.ai/test-distribution/",
    )

    def setUp(self) -> None:
        self.readme = (SELECTION_KIT / "README.md").read_text(encoding="utf-8")
        suite_list = readme_section(self.readme, "## The suite list")
        self.assertIn(self.HEADING + "\n", suite_list, "the criteria are a section of the suite list")
        self.section = suite_list[suite_list.index(self.HEADING + "\n"):]

    def paragraph(self, opening: str) -> str:
        """The one paragraph or list item of the section that starts with ``opening``."""
        (found,) = [block for block in re.split(r"\n(?=- )|\n\n", self.section) if block.startswith(opening)]
        return found

    def test_naming_by_glob_and_by_path_each_rest_on_the_listing_of_the_tracked_files(self) -> None:
        self.assertIn("`git ls-files`", self.paragraph("**Naming entry files.**"))
        by_glob, by_path = self.paragraph("- By glob"), self.paragraph("- By path")
        self.assertIn("one pattern matches every entry file of the suite and no other tracked file", by_glob)
        self.assertIn("a directory and a file-name rule with an extension", by_glob)
        self.assertIn("`**` only for a directory tree that holds nothing but that suite's tests and their reserved helper places", by_glob)
        for fact in ("share no such rule", "a helper, fixture or data file would match it", "one entry file"):
            with self.subTest(fact=fact):
                self.assertIn(fact, by_path)

    def test_a_new_test_file_runs_from_its_push_under_a_glob_and_only_once_named_under_a_list_of_paths(self) -> None:
        new = self.paragraph("A new test file")
        self.assertIn("runs from the push that adds it", new)
        self.assertIn("runs only once the list names it", new)

    def test_the_unit_of_selection_rests_on_the_count_the_sum_of_the_estimates_and_the_equality_of_the_lists(self) -> None:
        per_entry, whole = self.paragraph("- Per entry file"), self.paragraph("- As a whole")
        self.assertIn("two or more entry files", per_entry)
        self.assertIn("the sum of its `entry_durations` is one minute or more", per_entry)
        for fact in ("one entry file", "the sum is under one minute", "stored lists are equal"):
            with self.subTest(fact=fact):
                self.assertIn(fact, whole)
        start = self.paragraph("One minute is the starting value")
        self.assertIn("(https://users.ece.utexas.edu/~gligoric/papers/Gligoric15PhD.pdf)", start)
        self.assertIn("measured runs of the repository decide", start)
        self.assertIn("(../../pipeline/consumer-kit/docs/agents/ci.md)", start)

    def test_each_of_the_four_moves_states_its_sign_its_edit_and_what_the_next_run_runs_and_records(self) -> None:
        for move in self.MOVES:
            with self.subTest(move=move):
                text = self.paragraph(f"- **{move}.**")
                positions = [text.find(label) for label in ("Sign:", "Edit:", "Next run:")]
                self.assertEqual(sorted(positions), positions)
                self.assertNotIn(-1, positions)
                self.assertRegex(text[positions[2]:], r"\bruns?\b")
                self.assertRegex(text[positions[2]:], r"\brecords?\b")

    def test_the_section_links_every_source_that_a_criterion_rests_on(self) -> None:
        for source in self.SOURCES:
            with self.subTest(source=source):
                self.assertIn(f"]({source})", self.section)

    def test_the_guidance_for_the_first_suite_list_points_to_the_criteria(self) -> None:
        self.assertNotIn("where the repository selects per entry file", self.readme)
        (first,) = [block for block in self.readme.split("\n\n") if block.startswith("Propose the first suite list")]
        self.assertIn("(#naming-entry-files-and-the-unit-of-selection)", first)


class ShardedValidationWorkflowDocumentationTest(unittest.TestCase):
    """The README describes the suite jobs of the shared workflow `suite-jobs.yml` and their checks; the earlier form of
    the validation workflow is named nowhere but in the section for a repository that adopted it earlier."""

    def setUp(self) -> None:
        self.readme = (SELECTION_KIT / "README.md").read_text(encoding="utf-8")
        self.section = readme_section(self.readme, "## The suite jobs")

    def test_the_readme_describes_the_suite_jobs_of_the_shared_workflow_only(self) -> None:
        for earlier in ("<suite>", "<job>", "<entry-files>", "<windows-suite>", "passed.jsonl", "fromJSON(inputs.suites)",
                        "fromJSON(inputs.entries)", "suite-results-<job>", "one per validation workflow", "`validation.yml` caller"):
            with self.subTest(absent=earlier):
                self.assertNotIn(earlier, self.readme.split(EARLIER_ADOPTION_HEADING)[0])

    def test_the_suite_jobs_run_their_steps_in_the_order_of_the_shared_workflow(self) -> None:
        steps = ["the checkout", "the recording step", "the tools that the job's setups name", "the setups with `recorded: false`",
                 "the start of the recording", "the other setups", "the entry files", "the check of the recording",
                 "on Linux shard 1, the Python language step", "on a pull request, the list of the entry files that passed"]
        positions = [self.section.index(step) for step in steps]
        self.assertEqual(sorted(positions), positions)
        for text in ("two at a time", "one after another", "log group", "`suite-jobs.yml`", "`shard <n>`"):
            with self.subTest(text=text):
                self.assertIn(text, self.section)

    def test_the_jobs_and_checks_table_names_the_check_of_each_job(self) -> None:
        jobs_and_checks = readme_section(self.readme, "## Jobs and checks")
        self.assertIn("| `ci / suite-selection` |", jobs_and_checks)
        self.assertIn("| `ci / verdict` |", jobs_and_checks)
        self.assertIn("a skipped job is a suite that the selection did not run", jobs_and_checks)

    def test_a_change_to_the_shared_suite_jobs_runs_every_suite_through_the_shared_digest(self) -> None:
        selection = readme_section(self.readme, "## What selects a suite")
        self.assertIn("a change of the shared suite jobs or of the shared recording step", selection)
        self.assertIn("every suite runs once", selection)
        self.assertIn("a changed setup definition", selection)
        self.assertIn("a changed `jobs` entry runs the entry files of its system", selection)

    def test_an_adr_records_the_shards_on_one_linux_runner_label_and_that_a_step_change_runs_every_suite(self) -> None:
        records = [path for path in (REPOSITORY_ROOT / "docs" / "adr").glob("*.md") if "timed shards" in path.read_text(encoding="utf-8")]
        self.assertEqual(1, len(records))
        text = records[0].read_text(encoding="utf-8")
        self.assertIn("one runner label", text)
        self.assertIn("therefore runs every suite", text)

    def test_the_checkout_takes_the_suite_list_s_fetch_depth_and_no_other_option_of_the_repository(self) -> None:
        jobs = readme_section(self.readme, "## The suite list")
        self.assertIn("the `fetch-depth` of its checkout", jobs)
        self.assertIn("Use `0` for the whole history", jobs)


class SetupsDocumentationTest(unittest.TestCase):
    def test_setups_name_the_environment_a_script_gets_and_the_provision_lines(self) -> None:
        section = readme_section((SELECTION_KIT / "README.md").read_text(encoding="utf-8"), "## Setups")
        for text in ("`SETUP_NAME`", "`SETUP_DIRECTORY`", "`SETUP_PROVIDES`", "`SETUP_TOOL`", "`path=<directory>`",
                     "`variable=<name>=<value>`", "`python=<interpreter>`", "`$GITHUB_PATH`", "`$GITHUB_ENV`"):
            with self.subTest(text=text):
                self.assertIn(text, section)

    def test_variables_reach_an_entry_file_only_through_the_runner_context_or_a_variable_line(self) -> None:
        # The suite jobs have no `env` of the repository's own; a `runner` value that an entry file needs under its own
        # name is the job's default environment variable, and no other value reaches it but a setup's `variable=` line.
        section = readme_section((SELECTION_KIT / "README.md").read_text(encoding="utf-8"), "## Setups")
        self.assertIn("no `env` of the repository's own", section)
        self.assertIn("`RUNNER_TEMP`", section)
        self.assertIn("`variable=`", section)
        self.assertIn("no other value replaces them", section)
        self.assertIn("`exec --entry` fails when the job did not install a setup that the entry file states", section)


class EarlierAdoptionSectionTest(unittest.TestCase):
    """Every consumer kit leads a repository that adopted the part earlier onto the kit's current templates; a kit without
    templates leads it onto its switch and links no template."""

    def setUp(self) -> None:
        catalogue = json.loads((REPOSITORY_ROOT / "catalogue.json").read_text(encoding="utf-8"))
        self.kits = [REPOSITORY_ROOT / part["consumer_kit"] for part in catalogue["parts"] if part["consumer_kit"]]

    def test_every_kit_readme_has_the_section_once(self) -> None:
        self.assertTrue(self.kits)
        for kit in self.kits:
            with self.subTest(kit=kit.parent.name):
                readme = (kit / "README.md").read_text(encoding="utf-8")
                self.assertEqual(1, readme.count(EARLIER_ADOPTION_HEADING + "\n"))

    def test_the_section_links_every_template_of_its_kit_and_a_kit_without_templates_links_none(self) -> None:
        for kit in self.kits:
            templates = sorted(path.name for path in kit.glob("*.yml"))
            section = readme_section((kit / "README.md").read_text(encoding="utf-8"), EARLIER_ADOPTION_HEADING)
            if not templates:
                with self.subTest(kit=kit.parent.name):
                    self.assertEqual([], re.findall(r"\]\(([^)]*\.ya?ml)\)", section))
            for template in templates:
                with self.subTest(kit=kit.parent.name, template=template):
                    self.assertIn(f"]({template})", section)

    def test_the_suite_selection_section_lets_an_earlier_caller_drop_the_v1_moved_trigger(self) -> None:
        readme = (SELECTION_KIT / "README.md").read_text(encoding="utf-8")
        section = readme_section(readme, EARLIER_ADOPTION_HEADING)
        self.assertIn("removes `v1-moved` from the `types` of its `repository_dispatch` trigger", section)
        self.assertIn("(#when-v1-moves)", section)
        self.assertEqual(1, readme.count("`v1-moved`"), "only the earlier adoption still names the event")
        self.assertIn("No move of `v1` sends the repository an event", readme_section(readme, "## When `v1` moves"))

    def test_the_earlier_adoption_registers_both_files_or_switches_and_names_the_required_check(self) -> None:
        section = readme_section((SELECTION_KIT / "README.md").read_text(encoding="utf-8"), EARLIER_ADOPTION_HEADING)
        for text in ("`validation.yml`", "`local-workflows`", "../../pipeline/consumer-kit/README.md#the-configuration-file",
                     "`suite-selection`", "`setups`", "`jobs`", "`validation / validation`", "`ci / verdict`",
                     "a question to the user", "it removes `validation.yml` and its validation workflow"):
            with self.subTest(text=text):
                self.assertIn(text, section)
        self.assertNotIn("](validation.yml)", section, "the kit holds no template of the earlier form")

    def test_the_earlier_adoption_keeps_the_full_recording_of_an_earlier_kit_on_the_switch(self) -> None:
        section = readme_section((SELECTION_KIT / "README.md").read_text(encoding="utf-8"), EARLIER_ADOPTION_HEADING)
        self.assertIn("`full-recording.yml` or `apply-additions.yml`", section)
        self.assertIn("shared steps that it calls stay at `v1`", section)


class BareDispatchRunTest(unittest.TestCase):
    """The shared suite jobs run on a ``repository_dispatch`` without a ``client_payload``."""

    def test_every_checkout_takes_the_default_branch_s_tip(self) -> None:
        _, suite_jobs = read_workflow(REPOSITORY_ROOT / ".github" / "workflows" / "suite-jobs.yml")
        checkouts = [step for job in suite_jobs["jobs"].values() for step in job.get("steps", []) if str(step.get("uses", "")).startswith("actions/checkout@")]
        self.assertGreaterEqual(len(checkouts), 2)
        for step in checkouts:
            with self.subTest(job=step.get("name")):
                self.assertEqual("", expression_value(step["with"]["ref"], BARE_DISPATCH_CONTEXT), "an empty ref checks out github.sha, the default branch's tip")

    def test_the_expression_value_returns_operands_as_github_does(self) -> None:
        context = {"github": {"event_name": "repository_dispatch", "event": {"client_payload": {"sha": "abc"}}, "sha": "tip"}}
        self.assertEqual("abc", expression_value("${{ github.event_name == 'repository_dispatch' && github.event.client_payload.sha || '' }}", context))
        self.assertEqual("tip", expression_value("${{ github.event_name == 'push' && github.event.client_payload.sha || github.sha }}", context))
        self.assertTrue(expression_value("!(github.event_name == 'push')", context))


if __name__ == "__main__":
    unittest.main()
