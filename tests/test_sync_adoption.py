"""The sync's adoption command: in the local mode it makes a consumer's adoption pull request with the fixed callers and
a configuration file derived from the consumer's workflows and Dependabot configuration, and reports what it could not
derive."""

from __future__ import annotations

import unittest

from tests.consumer_checks_fixtures import CALLERS, SHARED, WORKFLOWS, template
from tests.pipeline_fixtures import CONFIGURATION, SCHEMAS, plan_program
from tests.sync_fixtures import CONSUMER, DEPENDABOT, OWNER_TOKEN, CI_GUIDE, CI_GUIDE_TEXT, REPOSITORY_ROOT, SyncFixture, sync_program
from tests.test_pipeline_dependabot import GENERATED

BRANCH = "chore/adopt-shared-pipeline"
FLOOR = f"{WORKFLOWS}/hygiene.yml"
TESTS = f"{WORKFLOWS}/tests.yml"
NIGHTLY = f"{WORKFLOWS}/nightly-report.yml"


def caller(shared_workflow: str, inputs: str = "", job: str = "check") -> str:
    """An invented caller of an earlier shared workflow, as a consumer holds it before adoption."""
    return f"name: {job}\non:\n  pull_request:\npermissions: {{}}\njobs:\n  {job}:\n    uses: {SHARED}/.github/workflows/{shared_workflow}@v1\n{inputs}"


BEFORE = {
    FLOOR: caller("fixture-floor.yml"),
    TESTS: caller("fixture-test.yml", "    with:\n      paths: [src/, tests/]\n      level: high\n"),
    DEPENDABOT: GENERATED.replace(f"    ignore:\n      - dependency-name: {SHARED}\n", ""),
    "src/code.txt": "the repository's own work\n",
}


class AdoptionFixture(SyncFixture):
    def before(self, files: dict[str, str] | None = None):
        """A repository that has not adopted the pipeline: no fixed caller, no CI guide, no configuration file."""
        return self.api.repository(CONSUMER, dict(BEFORE if files is None else files), installed=False)

    def adopt(self):
        return self.local(command="adopt")


class AdoptedChoicesTests(AdoptionFixture):
    def test_the_choices_that_a_repository_recorded_in_its_ci_guide_stay_below_the_fixed_part(self) -> None:
        template = CI_GUIDE_TEXT + "\n## Repository choices\n"
        (self.kit / CI_GUIDE).write_text(template, encoding="utf-8", newline="\n")
        guide = "# CI\n\n## Principles\n\nThe guide before the adoption.\n\n## Repository choices\n\nThe ruleset keeps the merge queue.\n"
        consumer = self.before({**BEFORE, CI_GUIDE: guide})
        adopted = self.adopt()
        self.assertEqual(0, adopted.status, adopted.stdout + adopted.stderr)
        self.assertEqual(template + "\nThe ruleset keeps the merge queue.\n", consumer.files(BRANCH)[CI_GUIDE])


class AdoptionTests(AdoptionFixture):
    def test_the_adoption_pull_request_holds_the_fixed_callers_and_the_derived_configuration_file(self) -> None:
        consumer = self.before()
        adopted = self.start("adopt", "--repository", CONSUMER, token=OWNER_TOKEN)
        self.assertEqual(0, adopted.status, adopted.stdout + adopted.stderr)
        self.assertEqual(1, len(consumer.pulls))
        pull = consumer.pulls[0]
        self.assertEqual((BRANCH, "main"), (pull["head"]["ref"], pull["base"]["ref"]))
        self.assertIsNone(pull["auto_merge"], "the owner merges an adoption")
        self.assertRegex(pull["title"], r"^chore: \S")
        branch = consumer.files(BRANCH)
        for name in CALLERS:
            self.assertEqual(template(name), branch[f"{WORKFLOWS}/{name}"])
        self.assertEqual(CI_GUIDE_TEXT, branch[CI_GUIDE])
        self.assertEqual("version: 1\nparts:\n  fixture-test:\n    paths: [src/, tests/]\n    level: high\ndependabot:\n  github-actions: [/]\n  pip: [/, /tools/report]\n",
                         branch[CONFIGURATION])
        self.assertEqual(GENERATED, branch[DEPENDABOT])
        self.assertNotIn(FLOOR, branch)
        self.assertNotIn(TESTS, branch)
        self.assertEqual("the repository's own work\n", branch["src/code.txt"])
        self.assertIn(pull["html_url"], adopted.summary)
        self.assertIn("Nothing: every workflow and every Dependabot entry is carried over.", adopted.summary)
        self.assertIn("## Not derived", pull["body"])
        self.assertTrue(all(item.token == OWNER_TOKEN for item in self.api.writes))

    def test_the_adopted_repository_matches_the_sync_and_the_plan_reads_its_configuration_file(self) -> None:
        consumer = self.before()
        self.adopt()
        consumer.refs["heads/main"] = consumer.refs[f"heads/{BRANCH}"]
        writes = len(self.api.writes)
        synced = self.local()
        self.assertEqual(0, synced.status, synced.stderr)
        self.assertEqual([], self.api.writes[writes:])
        plan_program.checked_configuration(consumer.files()[CONFIGURATION], plan_program.schema_versions(self.schemas))

    def test_a_workflow_that_no_part_replaces_stays_and_is_reported(self) -> None:
        own = "name: Nightly report\non:\n  schedule:\n    - cron: \"0 3 * * *\"\njobs:\n  report:\n    runs-on: ubuntu-latest\n    steps:\n      - run: ./report.sh\n"
        later = caller("fixture-later.yml")
        consumer = self.before({NIGHTLY: own, f"{WORKFLOWS}/later.yaml": later})
        adopted = self.adopt()
        self.assertEqual(0, adopted.status, adopted.stderr)
        branch = consumer.files(BRANCH)
        self.assertEqual(own, branch[NIGHTLY])
        self.assertEqual(later, branch[f"{WORKFLOWS}/later.yaml"])
        self.assertEqual("version: 1\n", branch[CONFIGURATION])
        for text in (adopted.summary, adopted.stdout, consumer.pulls[0]["body"]):
            self.assertIn(f"`{NIGHTLY}` stays, and hygiene fails on it until `local-workflows` registers it or a part or the free job takes its work: "
                          "its job `report` calls no shared workflow.", text)
            self.assertIn(f"`{WORKFLOWS}/later.yaml` stays", text)
            self.assertIn("no part of the pipeline replaces `fixture-later.yml` yet", text)

    def test_a_workflow_of_the_repository_under_the_name_of_a_fixed_caller_is_replaced_and_reported(self) -> None:
        consumer = self.before({f"{WORKFLOWS}/ci.yml": "name: CI\non: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n"})
        adopted = self.adopt()
        self.assertEqual(template("ci.yml"), consumer.files(BRANCH)[f"{WORKFLOWS}/ci.yml"])
        self.assertIn(f"`{WORKFLOWS}/ci.yml` is replaced by the fixed caller of that name and its work is not carried over: its job `build` calls no shared workflow.",
                      adopted.summary)

    def test_an_input_that_is_no_setting_of_its_part_is_reported(self) -> None:
        inputs = "    with:\n      level: low\n      runner: large\n      paths: ${{ vars.PATHS }}\n"
        consumer = self.before({TESTS: caller("fixture-test.yml", inputs), FLOOR: caller("fixture-floor.yml", "    with:\n      strict: true\n")})
        adopted = self.adopt()
        self.assertEqual("version: 1\nparts:\n  fixture-test:\n    level: low\n", consumer.files(BRANCH)[CONFIGURATION])
        self.assertIn(f"`{TESTS}`: the input `runner` is not carried over; it is no setting of the part `fixture-test`.", adopted.summary)
        self.assertIn(f"`{TESTS}`: the input `paths` is not carried over; its value is no plain value.", adopted.summary)
        self.assertIn(f"`{FLOOR}`: the input `strict` is not carried over; the part `fixture-floor` always runs and has no settings.", adopted.summary)

    def test_a_workflow_with_a_job_of_its_own_beside_a_shared_call_stays(self) -> None:
        mixed = caller("fixture-test.yml") + "  extra:\n    runs-on: ubuntu-latest\n    steps:\n      - run: make\n"
        consumer = self.before({TESTS: mixed})
        adopted = self.adopt()
        self.assertEqual(mixed, consumer.files(BRANCH)[TESTS])
        self.assertEqual("version: 1\n", consumer.files(BRANCH)[CONFIGURATION])
        self.assertIn("its job `extra` calls no shared workflow", adopted.summary)

    def test_a_derived_value_that_the_schema_refuses_stops_the_adoption(self) -> None:
        consumer = self.before({TESTS: caller("fixture-test.yml", "    with:\n      level: medium\n")})
        adopted = self.adopt()
        self.assertEqual(1, adopted.status)
        self.assertIn("the derived configuration file does not match its schema", adopted.stderr)
        self.assertEqual([], consumer.pulls)
        self.assertEqual([], self.api.writes)


class DependabotAdoptionTests(AdoptionFixture):
    def test_what_the_generated_file_does_not_carry_is_reported(self) -> None:
        held = ("version: 2\nregistries:\n  internal:\n    type: npm-registry\nupdates:\n"
                "  - package-ecosystem: pip\n    directories: [/, /tools/*]\n    schedule:\n      interval: daily\n    labels: [dependencies]\n"
                "  - package-ecosystem: npm\n    directory: web\n    schedule:\n      interval: weekly\n")
        consumer = self.before({DEPENDABOT: held})
        adopted = self.adopt()
        branch = consumer.files(BRANCH)
        self.assertEqual("version: 1\ndependabot:\n  pip: [/]\n", branch[CONFIGURATION])
        self.assertEqual(sync_program.consumer_checks.dependabot_text({"pip": ["/"]}, SHARED), branch[DEPENDABOT])
        for line in (f"`{DEPENDABOT}`: the key `registries` is not carried over",
                     f'`{DEPENDABOT}`: the directory `"/tools/*"` of `pip` is not carried over',
                     f'`{DEPENDABOT}`: the directory `"web"` of `npm` is not carried over',
                     f"`{DEPENDABOT}`: `schedule` of the entry of `pip` is not carried over",
                     f"`{DEPENDABOT}`: `labels` of the entry of `pip` is not carried over"):
            self.assertIn(line, adopted.summary)
        self.assertNotIn("`schedule` of the entry of `npm`", adopted.summary)

    def test_a_dependabot_configuration_under_the_other_name_moves_to_the_generated_file(self) -> None:
        consumer = self.before({".github/dependabot.yaml": GENERATED})
        adopted = self.adopt()
        branch = consumer.files(BRANCH)
        self.assertEqual(GENERATED, branch[DEPENDABOT])
        self.assertNotIn(".github/dependabot.yaml", branch)
        self.assertIn("Nothing: every workflow", adopted.summary)

    def test_a_dependabot_configuration_without_a_derivable_entry_leaves_and_is_reported(self) -> None:
        consumer = self.before({DEPENDABOT: "version: 2\nupdates:\n  - package-ecosystem: npm\n    directory: web\n"})
        adopted = self.adopt()
        self.assertNotIn(DEPENDABOT, consumer.files(BRANCH))
        self.assertIn(f"`{DEPENDABOT}` leaves the repository", adopted.summary)

    def test_a_repository_without_a_dependabot_configuration_gets_none(self) -> None:
        consumer = self.before({"README.md": "A repository.\n"})
        self.assertEqual(0, self.adopt().status)
        self.assertEqual("version: 1\n", consumer.files(BRANCH)[CONFIGURATION])
        self.assertNotIn(DEPENDABOT, consumer.files(BRANCH))


class AdoptionBoundsTests(AdoptionFixture):
    def test_a_repository_that_holds_a_configuration_file_already_is_refused(self) -> None:
        consumer = self.consumer()
        adopted = self.adopt()
        self.assertEqual(1, adopted.status)
        self.assertIn("it adopted the pipeline", adopted.stderr)
        self.assertEqual([], consumer.pulls)

    def test_a_second_adoption_keeps_the_open_pull_request(self) -> None:
        consumer = self.before()
        self.adopt()
        self.assertEqual(0, self.adopt().status)
        self.assertEqual(1, len(consumer.pulls))

    def test_adoption_names_one_repository_and_never_the_repository_of_the_shared_parts(self) -> None:
        self.assertEqual(2, self.run_sync("adopt", token=OWNER_TOKEN).status)
        refused = self.run_sync("adopt", "--repository", SHARED, token=OWNER_TOKEN)
        self.assertEqual(1, refused.status)
        self.assertEqual([], self.api.requests)


class RealTableTests(unittest.TestCase):
    def test_the_adoption_table_names_shared_workflows_of_this_repository_and_parts_of_the_schema(self) -> None:
        table = sync_program.read_json(sync_program.ADOPTION, "the adoption table")["shared-workflows"]
        versions = plan_program.schema_versions(SCHEMAS)
        self.assertTrue(table)
        for workflow, part in table.items():
            with self.subTest(workflow=workflow):
                self.assertTrue((REPOSITORY_ROOT / ".github" / "workflows" / workflow).is_file())
                self.assertIn(part, versions[max(versions)].parts)


if __name__ == "__main__":
    unittest.main()
