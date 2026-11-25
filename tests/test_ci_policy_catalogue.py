"""The catalogue as an agent in a consumer reads it at v1: the decision rule with its four steps, the requests, the
local workflow register, one page per part of the configuration file in one form, and paths that exist. This
repository's issue tracker configuration records the intake rule for the requests."""

from __future__ import annotations

import json
import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, is_tracked, tracked_files

CATALOGUE = REPOSITORY_ROOT / "catalogue.json"
SCHEMAS = REPOSITORY_ROOT / "actions" / "pipeline" / "schema"
ISSUE_TRACKER = REPOSITORY_ROOT / "docs" / "agents" / "issue-tracker.md"
# The keys of every entry of parts, so that a reader finds the same things in each.
ENTRY_KEYS = {"name", "purpose", "configuration", "workflows", "steps", "required_checks", "needs", "applies", "consumer_kit", "audience"}
AUDIENCES = {"consumer", "management repository"}
PATH = re.compile(r"(?<![\w./-])(?:actions|docs)/[A-Za-z0-9_./-]*[A-Za-z0-9_-]")


def current_fragments() -> dict[str, dict]:
    """The schema fragment of each part of the newest schema version, by the part's name."""
    newest = max((folder for folder in SCHEMAS.iterdir() if re.fullmatch(r"v[1-9][0-9]*", folder.name)), key=lambda folder: int(folder.name[1:]))
    return {path.stem: {"path": path.relative_to(REPOSITORY_ROOT).as_posix(), **json.loads(path.read_text(encoding="utf-8"))} for path in sorted((newest / "parts").glob("*.json"))}


def texts(value: object) -> list[str]:
    """Every string in a JSON value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for inner in value.values() for text in texts(inner)]
    if isinstance(value, list):
        return [text for inner in value for text in texts(inner)]
    return []


def named_paths(catalogue: dict) -> list[str]:
    """Every path of this repository that the catalogue names, in its prose and in its path keys."""
    paths = {match for text in texts(catalogue) for match in PATH.findall(text)}
    for entry in catalogue["parts"]:
        paths.update([*entry["workflows"], *entry["steps"], *(item for item in (entry["configuration"], entry["consumer_kit"]) if item)])
    return sorted(paths)


def shared_ci_meanings(kit: str) -> list[str]:
    """The ``Meaning`` cell of each ``shared-ci`` row of a vocabulary table in the pipeline kit's README."""
    return [cells[3].strip() for line in kit.splitlines() if line.startswith("| `shared-ci` |") and len(cells := line.split("|")) == 5]


class CatalogueFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.catalogue = json.loads(CATALOGUE.read_text(encoding="utf-8"))
        self.entries = {entry["name"]: entry for entry in self.catalogue["parts"]}


class DecisionRuleTests(CatalogueFixture):
    def setUp(self) -> None:
        super().setUp()
        self.steps = self.catalogue["decision_rule"]["steps"]

    def test_the_rule_has_four_steps_in_order_each_with_its_condition_and_its_action(self) -> None:
        self.assertEqual([1, 2, 3, 4], [step["step"] for step in self.steps])
        for step in self.steps:
            with self.subTest(step=step["step"]):
                self.assertEqual({"step", "when", "then"}, set(step))
                self.assertTrue(step["when"].strip() and step["then"].strip())

    def test_the_steps_are_those_of_the_spec(self) -> None:
        for number, when, then in (
            (1, ("A part exists", "its inputs suffice"), ("Switch the part on", ".github/ci-config.yml")),
            (2, ("A part exists", "its inputs do not suffice"),
             ("request", "free job", "custom", "the missing input needs write permission, a secret or another trigger", "temporary", "local-workflows")),
            (3, ("No part exists", "read permission suffices"), ("free job", "custom", "request")),
            (4, ("No part exists", "write permission", "a secret", "trigger"), ("request", "temporary", "local-workflows")),
        ):
            step = self.steps[number - 1]
            for text in when:
                with self.subTest(step=number, when=text):
                    self.assertIn(text, step["when"])
            for text in then:
                with self.subTest(step=number, then=text):
                    self.assertIn(text, step["then"])

    def test_the_rule_asks_for_the_first_step_that_holds_and_for_no_judgement_of_other_repositories(self) -> None:
        about = self.catalogue["decision_rule"]["about"]
        self.assertIn("first step whose condition holds", about)
        self.assertIn("another repository", about)

    def test_a_part_without_a_language_is_named_after_the_platform_it_serves(self) -> None:
        about = self.catalogue["decision_rule"]["about"]
        self.assertIn("<ecosystem>-<phase>", about)
        self.assertIn("platform", about)
        self.assertIn("github-release", about)
        self.assertIn("github-release", self.entries)

    def test_the_free_job_that_the_rule_names_has_a_page(self) -> None:
        self.assertIn("free job", self.entries["custom"]["purpose"])
        self.assertIn("decision rule", self.entries["custom"]["applies"])


class RegisterTests(CatalogueFixture):
    def setUp(self) -> None:
        super().setUp()
        self.register = self.catalogue["local_workflows"]

    def test_a_temporary_entry_names_the_request_and_the_part_it_waits_for(self) -> None:
        for text in ("workflow", "request", "waits-for", "the part it waits for", "steps 2 and 4", "<part>.<setting>", "the setting that the part lacks"):
            with self.subTest(text=text):
                self.assertIn(text, self.register["temporary"])

    def test_a_permanent_entry_names_a_closed_request_that_was_declined(self) -> None:
        for text in ("workflow", "declined-request", "closed", "declined"):
            with self.subTest(text=text):
                self.assertIn(text, self.register["permanent"])

    def test_the_register_keys_are_those_of_the_schema(self) -> None:
        schema = json.loads((SCHEMAS / "v1" / "base.json").read_text(encoding="utf-8"))
        temporary, permanent = schema["$defs"]["local-workflow"]["oneOf"]
        for key in temporary["required"]:
            with self.subTest(temporary=key):
                self.assertIn(key, self.register["temporary"])
        for key in permanent["required"]:
            with self.subTest(permanent=key):
                self.assertIn(key, self.register["permanent"])

    def test_the_checks_state_the_named_issue_and_the_switch_over(self) -> None:
        checks = self.register["checks"]
        for text in ("exists at v1", "knows the setting", "whose author is not Dependabot fails", "switch the part on", "remove the temporary workflow", "close the request", "notice"):
            with self.subTest(text=text):
                self.assertIn(text, checks)


class RequestTests(CatalogueFixture):
    def test_a_request_is_an_issue_in_the_consumer_with_the_label_shared_ci(self) -> None:
        requests = self.catalogue["requests"]
        self.assertEqual("shared-ci", requests["label"])
        self.assertIn("own repository", requests["where"])
        self.assertIn("nothing into CrankyAnt/SharedWorkflows", requests["where"])
        self.assertIn("general terms", requests["triage"])
        self.assertIn("docs/agents/triage-labels.md", requests["triage"])
        self.assertIn("closes the request", requests["closing"])

    def test_the_label_s_description_fits_github_s_limit_and_its_colour_matches_the_kit(self) -> None:
        requests = self.catalogue["requests"]
        self.assertLessEqual(len(requests["label_description"]), 100)
        kit = (REPOSITORY_ROOT / "actions/pipeline/consumer-kit/README.md").read_text(encoding="utf-8")
        self.assertIn(f"| `shared-ci` | `{requests['label_colour']}` |", kit)

    def test_the_kit_s_vocabulary_row_holds_the_label_s_description_character_for_character(self) -> None:
        requests = self.catalogue["requests"]
        kit = (REPOSITORY_ROOT / "actions/pipeline/consumer-kit/README.md").read_text(encoding="utf-8")
        self.assertEqual([requests["label_description"]], shared_ci_meanings(kit))
        changed = kit.replace(f"| {requests['label_description']} |", f"| {requests['label_description']}. |")
        self.assertNotEqual([requests["label_description"]], shared_ci_meanings(changed), "a row that differs fails")

    def test_the_triage_states_the_part_s_final_name_and_the_entry_follows_it(self) -> None:
        requests = self.catalogue["requests"]
        self.assertIn("final name", requests["triage"])
        self.assertIn("comment on the request", requests["triage"])
        self.assertIn("final name of the setting", requests["triage"])
        self.assertIn("waits-for equal to", requests["triage"])
        self.assertIn("<part>.<setting>", requests["content"])

    def test_the_triage_closes_a_declined_request_as_not_planned_with_the_decline_in_a_comment(self) -> None:
        closing = self.catalogue["requests"]["closing"]
        for text in ("triage of CrankyAnt/SharedWorkflows", "closes", "not planned", "comment", "permanent entry"):
            with self.subTest(text=text):
                self.assertIn(text, closing)

    def test_a_corrected_title_needs_a_re_run(self) -> None:
        route = self.catalogue["routes"]["corrected_title"]
        self.assertIn("does not start on an edit", route)
        self.assertIn("re-run", route)


class PageTests(CatalogueFixture):
    def setUp(self) -> None:
        super().setUp()
        self.fragments = current_fragments()

    def test_every_entry_has_the_same_keys_and_a_name_of_its_own(self) -> None:
        self.assertEqual(len(self.catalogue["parts"]), len(self.entries), "two entries share a name")
        for name, entry in self.entries.items():
            with self.subTest(entry=name):
                self.assertEqual(ENTRY_KEYS, set(entry))
                self.assertIn(entry["audience"], AUDIENCES)

    def test_every_part_of_the_configuration_file_has_a_page_under_its_name_that_names_its_fragment(self) -> None:
        self.assertTrue(self.fragments)
        for part, fragment in self.fragments.items():
            with self.subTest(part=part):
                self.assertIn(part, self.entries, "the part has no page")
                self.assertEqual(fragment["path"], self.entries[part]["configuration"])

    def test_no_other_entry_states_a_configuration(self) -> None:
        for name, entry in self.entries.items():
            if name not in self.fragments:
                with self.subTest(entry=name):
                    self.assertIsNone(entry["configuration"])

    def test_a_page_names_its_key_and_every_required_setting_of_its_fragment(self) -> None:
        for part, fragment in self.fragments.items():
            needs = self.entries[part]["needs"]
            with self.subTest(part=part):
                if fragment.get("always"):
                    self.assertIn("always runs", needs)
                    if "configuration" not in fragment:
                        continue
                self.assertIn(f"parts.{part}", needs)
                self.assertIn(fragment["path"], needs)
                for setting in fragment["configuration"].get("required", []):
                    with self.subTest(part=part, setting=setting):
                        self.assertRegex(needs, rf"(?<![\w-]){re.escape(setting)}(?![\w-])")

    def test_the_page_of_repository_hygiene_names_both_exclusions_and_no_longer_says_it_takes_no_settings(self) -> None:
        page = self.entries["repository-hygiene"]
        for setting in ("coverage-exclude", "link-exclude"):
            with self.subTest(setting=setting):
                self.assertIn(setting, page["needs"])
                self.assertIn(setting, self.fragments["repository-hygiene"]["configuration"]["properties"])
        for text in (page["purpose"], page["needs"]):
            self.assertNotRegex(text, r"takes no settings|has no settings|nothing under parts")

    def test_the_entry_repository_hygiene_names_enabled_auto_merge_under_needs(self) -> None:
        self.assertIn("whose auto-merge is enabled", self.entries["Repository hygiene"]["needs"])

    def test_a_page_names_the_shared_workflow_of_a_part_outside_the_pipeline(self) -> None:
        for part, fragment in self.fragments.items():
            with self.subTest(part=part):
                expected = [f".github/workflows/{fragment['workflow']}"] if "workflow" in fragment else []
                self.assertEqual(expected, self.entries[part]["workflows"])
                self.assertEqual([], self.entries[part]["required_checks"], "the pipeline's verdict is the one required check")

    def test_a_page_names_nothing_that_exists_only_in_this_repository(self) -> None:
        for part in self.fragments:
            text = json.dumps(self.entries[part])
            for own in ("tests/", "python_tests", "dotnet_test", "dotnet_console", "node_test", "maintaining.md", "validate.yml"):
                with self.subTest(part=part, own=own):
                    self.assertNotIn(own, text)

    def test_an_entry_that_no_consumer_adopts_says_so(self) -> None:
        for name, entry in self.entries.items():
            with self.subTest(entry=name):
                management = any(wf in entry["workflows"] for wf in (".github/workflows/sync.yml", ".github/workflows/measurement.yml"))
                self.assertEqual("management repository" if management else "consumer", entry["audience"])


class LinkTests(CatalogueFixture):
    def test_every_path_that_the_catalogue_names_is_tracked(self) -> None:
        paths = named_paths(self.catalogue)
        self.assertIn("actions/pipeline/schema/v1/ci-config.schema.json", paths)
        self.assertIn("actions/pipeline/schema/v1/setups.json", paths)
        # docs/agents/ci.md and docs/agents/triage-labels.md are files of a consumer; the first has its template in the kit.
        consumer_files = {"docs/agents/ci.md": "actions/pipeline/consumer-kit/docs/agents/ci.md", "docs/agents/triage-labels.md": None}
        looked_up = [consumer_files.get(path, path) for path in paths]
        tracked = tracked_files(REPOSITORY_ROOT, [path for path in looked_up if path])
        for path in looked_up:
            if path:
                with self.subTest(path=path):
                    self.assertTrue(is_tracked(path, tracked), "the catalogue names a path that this repository does not hold")

    def test_a_path_that_does_not_exist_is_found(self) -> None:
        catalogue = {"about": "See actions/pipeline/missing.json.", "parts": []}
        self.assertEqual(["actions/pipeline/missing.json"], named_paths(catalogue))
        self.assertFalse(is_tracked("actions/pipeline/missing.json", tracked_files(REPOSITORY_ROOT, ["actions/pipeline/missing.json"])))


class IntakeRuleTests(unittest.TestCase):
    def test_this_repository_s_triage_collects_the_requests_of_the_owner_s_other_repositories(self) -> None:
        section = ISSUE_TRACKER.read_text(encoding="utf-8").split("\n## Repository vocabularies and intake rules\n", 1)[1].split("\n## ", 1)[0]
        for text in ("`shared-ci`", "open", "the owner's other repositories", "the owner's own login", "general terms", "final name",
                     "closes the request as not planned", "comment that states the decline", "`<part>.<setting>`"):
            with self.subTest(text=text):
                self.assertIn(text, section)
        self.assertNotIn("None.", section)


if __name__ == "__main__":
    unittest.main()
