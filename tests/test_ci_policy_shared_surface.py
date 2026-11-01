"""The shared CI surface records every tracked resource shared workflows load."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from tests.ci_policy_support import REPOSITORY_ROOT, is_tracked, shared_workflows, tracked_files, tracked_workflows


SURFACE = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "shared-surface.json"
CATALOGUE = REPOSITORY_ROOT / "catalogue.json"
PARTS = ("publication-checks", "repository-hygiene", "dependabot-auto-merge", "suite-selection", "recording", "python-language-step")
ENTRY_TEXTS = ("name", "purpose", "needs", "applies")
# The header of the pin-source table of a consumer kit's README, which consumers follow and the pin-source test reads.
PIN_SOURCE_HEADER = "| Template | Pin source |"


def on_surface(path: str, surface_paths: list[str]) -> bool:
    return any(path == item or path.startswith(item + "/") for item in surface_paths)


def shared_workflow_problems(workflows: dict[str, str], surface_paths: list[str]) -> list[str]:
    """Each way the workflows, from path to text, are out of step with the shared surface."""
    shared = shared_workflows(workflows)
    problems = [f"{path} has a workflow_call trigger but the shared surface lacks it" for path in shared if path not in surface_paths]
    problems += [f"{path} is on the shared surface without a workflow_call trigger" for path in surface_paths
                 if path.startswith(".github/workflows/") and path not in shared]
    problems += [f"{path} loads {reference} outside the shared surface" for path in shared
                 for reference in re.findall(r"uses: \$/([^\s]+)", workflows[path]) if reference not in surface_paths]
    return problems


def led_to(readme: Path) -> set[str]:
    """The repository paths that the relative links of a consumer kit's README lead to."""
    targets = re.findall(r"\]\(([^)\s#]+)(?:#[^)]*)?\)", readme.read_text(encoding="utf-8"))
    return {(readme.parent / target).resolve().relative_to(REPOSITORY_ROOT).as_posix()
            for target in targets if "://" not in target and (readme.parent / target).resolve().is_relative_to(REPOSITORY_ROOT)}


def pin_sources(readme: str) -> dict[str, list[str]]:
    """The pin sources that the pin-source table of a consumer kit's README names, from template link target to workflow paths."""
    lines = readme.splitlines()
    if PIN_SOURCE_HEADER not in lines:
        return {}
    sources: dict[str, list[str]] = {}
    for line in lines[lines.index(PIN_SOURCE_HEADER) + 2:]:
        if not line.startswith("|"):
            break
        template_cell, _, source_cell = line.strip("| ").partition(" | ")
        for template in re.findall(r"\]\(([^)#\s]+)\)", template_cell):
            sources.setdefault(template, []).extend(re.findall(r"`([^`]+)`", source_cell))
    return sources


def kit_pin_problems(readme: str, templates: dict[str, str], workflows: dict[str, str]) -> list[str]:
    """Each way the pin source that the README names for a template, from file name to text, fails to pin an action that
    the template references with `@<pin>`; ``workflows`` maps the path of each tracked workflow to its text."""
    sources = pin_sources(readme)
    problems = []
    for template, text in sorted(templates.items()):
        actions = sorted(set(re.findall(r"""uses:\s*['"]?([^\s@'"]+)@<pin>""", text)))
        if not actions:
            continue
        named = sources.get(template, [])
        if len(named) != 1:
            problems.append(f"{template} carries @<pin> but the README names {len(named) or 'no'} pin sources for it, not one")
            continue
        source = named[0]
        if not source.startswith(".github/workflows/") or source not in workflows:
            problems.append(f"{template}: its pin source {source} is no tracked workflow under .github/workflows/")
            continue
        for action in actions:
            references = re.findall(rf"(?m)uses:\s*{re.escape(action)}@(.*)$", workflows[source])
            if not references:
                problems.append(f"{template}: its pin source {source} does not reference {action}")
            elif not all(re.fullmatch(r"[0-9a-f]{40} # v\d+(?:\.\d+)*[ \t]*", reference) for reference in references):
                problems.append(f"{template}: its pin source {source} references {action} without a 40-character commit SHA and a version comment")
    return problems


def catalogue_problems(catalogue: dict, surface_paths: list[str], tracked: set[str]) -> list[str]:
    """Each way the catalogue is out of step with the shared surface and the tracked files."""
    problems = []
    named = set()
    for entry in catalogue["parts"]:
        name = entry.get("name", "<unnamed>")
        problems += [f"{name}: {key} is empty" for key in ENTRY_TEXTS if not entry.get(key)]
        if not str(entry.get("purpose", "")).endswith(".") or ". " in str(entry.get("purpose", "")):
            problems.append(f"{name}: the purpose is not one sentence")
        problems += [f"{name}: {key} is missing" for key in ("workflows", "steps", "required_checks") if not isinstance(entry.get(key), list)]
        for workflow in entry.get("workflows", []):
            named.add(workflow)
            if not workflow.startswith(".github/workflows/") or workflow not in surface_paths or workflow not in tracked:
                problems.append(f"{name}: the workflow {workflow} is no shared workflow")
        for step in entry.get("steps", []):
            named.add(step)
            if not step.startswith("actions/") or step not in surface_paths or f"{step}/action.yml" not in tracked:
                problems.append(f"{name}: the step {step} is no shared step")
        if "consumer_kit" not in entry:
            problems.append(f"{name}: consumer_kit is missing; null says that the part has no consumer kit yet")
        kit = entry.get("consumer_kit")
        if kit is not None and (not on_surface(kit, surface_paths) or f"{kit}/README.md" not in tracked):
            problems.append(f"{name}: the consumer kit {kit} does not exist on the shared surface")
    # A folder under actions/ without a step, such as the ruleset template, is reached through a consumer kit that leads to it.
    kits = [entry["consumer_kit"] for entry in catalogue["parts"] if entry.get("consumer_kit") and f"{entry['consumer_kit']}/README.md" in tracked]
    reached = set().union(*(led_to(REPOSITORY_ROOT / kit / "README.md") for kit in kits))
    for path in surface_paths:
        if not path.startswith((".github/workflows/", "actions/")) or path in named:
            continue
        if path.startswith("actions/") and f"{path}/action.yml" not in tracked:
            if not any(on_surface(target, [path]) for target in reached):
                problems.append(f"{path} holds no step and no consumer kit leads to it")
        else:
            problems.append(f"{path} belongs to no catalogue entry")
    return problems


class SharedSurfaceTests(unittest.TestCase):
    def test_the_shared_surface_covers_every_resource_a_shared_workflow_loads_and_names_tracked_paths_only(self) -> None:
        surface = json.loads(SURFACE.read_text(encoding="utf-8"))
        workflows = tracked_workflows(REPOSITORY_ROOT)
        self.assertTrue(shared_workflows(workflows), "no tracked workflow has a workflow_call trigger")
        self.assertEqual([], shared_workflow_problems(workflows, surface["paths"]))
        surface_workflows = [path for path in surface["paths"] if path.startswith(".github/workflows/")]
        self.assertEqual(surface_workflows + ["catalogue.json"] + [f"actions/{part}" for part in PARTS] + ["actions/repository-ruleset"], surface["paths"])
        tracked = tracked_files(REPOSITORY_ROOT, [surface["pins"], *surface["paths"]])
        self.assertIn(surface["pins"], tracked)
        for path in surface["paths"]:
            with self.subTest(path=path):
                self.assertTrue(is_tracked(path, tracked), f"{path} is in the shared surface but Git tracks nothing there")


class SharedWorkflowProblemTests(unittest.TestCase):
    SURFACE_PATHS = [".github/workflows/shared.yml", "catalogue.json", "actions/part"]

    def test_a_workflow_with_a_workflow_call_trigger_that_the_shared_surface_lacks_fails_and_is_named(self) -> None:
        workflows = {".github/workflows/shared.yml": "on: workflow_call\njobs: {}\n",
                     ".github/workflows/new.yml": "on: workflow_call\njobs:\n  unit:\n    runs-on: ubuntu-latest\n"}
        self.assertEqual([".github/workflows/new.yml has a workflow_call trigger but the shared surface lacks it"],
                         shared_workflow_problems(workflows, self.SURFACE_PATHS))

    def test_a_workflow_call_trigger_in_a_list_or_a_mapping_that_the_shared_surface_lacks_fails_and_is_named(self) -> None:
        for form, on in (("flow list", "on: [push, workflow_call]\n"), ("quoted flow list", "on: ['push', \"workflow_call\"]\n"),
                         ("block list", "on:\n  - push\n  - workflow_call\n"),
                         ("mapping", "on:\n  push:\n    branches: [main]\n  workflow_call:\n    inputs:\n      name:\n        type: string\n"),
                         ("empty mapping entry", "on:\n  workflow_call:\n"),
                         ("flow mapping", "on: {push: {branches: [main, 'a,b']}, workflow_call: {}}\n")):
            with self.subTest(form=form):
                workflows = {".github/workflows/shared.yml": "on: workflow_call\n", ".github/workflows/new.yml": on + "jobs:\n  unit:\n    runs-on: ubuntu-latest\n"}
                self.assertEqual([".github/workflows/new.yml has a workflow_call trigger but the shared surface lacks it"],
                                 shared_workflow_problems(workflows, self.SURFACE_PATHS))

    def test_a_workflow_without_a_workflow_call_trigger_off_the_shared_surface_passes(self) -> None:
        for form, on in (("string", "on: push\n"), ("flow list", "on: [push, workflow_dispatch]\n"), ("block list", "on:\n  - push\n"),
                         ("mapping", "on:\n  push:\n  workflow_run:\n    workflows: [workflow_call]\n")):
            with self.subTest(form=form):
                workflows = {".github/workflows/shared.yml": "on: workflow_call\n", ".github/workflows/caller.yml": on}
                self.assertEqual([], shared_workflow_problems(workflows, self.SURFACE_PATHS))

    def test_a_workflow_on_the_shared_surface_without_a_workflow_call_trigger_fails_and_is_named(self) -> None:
        for form, on in (("string", "on: push\n"), ("flow list", "on: [push, workflow_dispatch]\n"), ("mapping", "on:\n  push:\n")):
            with self.subTest(form=form):
                workflows = {".github/workflows/shared.yml": on}
                self.assertEqual([".github/workflows/shared.yml is on the shared surface without a workflow_call trigger"],
                                 shared_workflow_problems(workflows, self.SURFACE_PATHS))

    def test_a_workflow_with_a_workflow_call_trigger_that_loads_a_path_outside_the_shared_surface_fails_and_is_named(self) -> None:
        steps = "jobs:\n  unit:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: $/actions/part\n      - uses: $/actions/elsewhere\n"
        surface_paths = [*self.SURFACE_PATHS, ".github/workflows/second.yml"]
        for form, on in (("string", "on: workflow_call\n"), ("flow list", "on: [push, workflow_call]\n"), ("mapping", "on:\n  workflow_call:\n")):
            with self.subTest(form=form):
                workflows = {".github/workflows/shared.yml": "on: workflow_call\n", ".github/workflows/second.yml": on + steps}
                self.assertEqual([".github/workflows/second.yml loads actions/elsewhere outside the shared surface"],
                                 shared_workflow_problems(workflows, surface_paths))

    def test_a_workflow_without_a_workflow_call_trigger_may_load_a_path_outside_the_shared_surface(self) -> None:
        workflows = {".github/workflows/shared.yml": "on: workflow_call\n",
                     ".github/workflows/caller.yml": "on: push\njobs:\n  unit:\n    uses: $/.github/workflows/elsewhere.yml\n"}
        self.assertEqual([], shared_workflow_problems(workflows, self.SURFACE_PATHS))


class CatalogueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.surface = json.loads(SURFACE.read_text(encoding="utf-8"))["paths"]
        self.catalogue = json.loads(CATALOGUE.read_text(encoding="utf-8"))
        kits = [entry["consumer_kit"] for entry in self.catalogue["parts"] if entry.get("consumer_kit")]
        self.tracked = tracked_files(REPOSITORY_ROOT, [*self.surface, *kits])

    def test_the_catalogue_names_every_shared_workflow_and_step_and_only_existing_ones(self) -> None:
        self.assertEqual([], catalogue_problems(self.catalogue, self.surface, self.tracked))

    def test_the_catalogue_and_every_consumer_kit_are_on_the_shared_surface(self) -> None:
        self.assertTrue(on_surface(CATALOGUE.relative_to(REPOSITORY_ROOT).as_posix(), self.surface))
        kits = [entry["consumer_kit"] for entry in self.catalogue["parts"] if entry["consumer_kit"] is not None]
        self.assertTrue(kits)
        for kit in kits:
            with self.subTest(kit=kit):
                self.assertTrue(on_surface(kit, self.surface))

    def test_a_shared_workflow_or_step_without_an_entry_fails(self) -> None:
        for path in (".github/workflows/suite-verdict.yml", "actions/recording"):
            with self.subTest(path=path):
                catalogue = {"parts": [{**entry, "workflows": [item for item in entry["workflows"] if item != path],
                                        "steps": [item for item in entry["steps"] if item != path]} for entry in self.catalogue["parts"]]}
                self.assertEqual([f"{path} belongs to no catalogue entry"], catalogue_problems(catalogue, self.surface, self.tracked))

    def test_an_entry_that_names_a_missing_workflow_step_or_consumer_kit_fails(self) -> None:
        first = self.catalogue["parts"][0]
        # Without the consumer kit of repository hygiene, no consumer kit leads to the ruleset template.
        unreached = ["actions/repository-ruleset holds no step and no consumer kit leads to it"]
        for key, value, problem, more in (
            ("workflows", [*first["workflows"], ".github/workflows/missing.yml"], "the workflow .github/workflows/missing.yml is no shared workflow", []),
            ("steps", [*first["steps"], "actions/missing"], "the step actions/missing is no shared step", []),
            ("consumer_kit", "actions/repository-hygiene/missing-kit", "the consumer kit actions/repository-hygiene/missing-kit does not exist on the shared surface", unreached),
        ):
            with self.subTest(key=key):
                catalogue = {"parts": [{**first, key: value}, *self.catalogue["parts"][1:]]}
                self.assertEqual([f"{first['name']}: {problem}", *more], catalogue_problems(catalogue, self.surface, self.tracked))

    def test_the_suite_selection_names_its_consumer_kit(self) -> None:
        entries = {entry["name"]: entry for entry in self.catalogue["parts"]}
        self.assertEqual("actions/suite-selection/consumer-kit", entries["Suite selection"]["consumer_kit"])

    def test_repository_hygiene_names_its_consumer_kit_and_an_entry_without_one_says_so(self) -> None:
        entries = {entry["name"]: entry for entry in self.catalogue["parts"]}
        self.assertEqual("actions/repository-hygiene/consumer-kit", entries["Repository hygiene"]["consumer_kit"])
        self.assertIn("consumer_kit", entries["Python language step"])
        without = {**entries["Python language step"]}
        del without["consumer_kit"]
        self.assertIn("Python language step: consumer_kit is missing; null says that the part has no consumer kit yet",
                      catalogue_problems({"parts": [*self.catalogue["parts"], without]}, self.surface, self.tracked))


class HygieneConsumerKitRulesetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.readme = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "consumer-kit" / "README.md"

    def test_the_hygiene_consumer_kit_leads_to_the_ruleset_template_on_the_shared_surface(self) -> None:
        template = "actions/repository-ruleset/ruleset-main-template.json"
        self.assertIn(template, led_to(self.readme))
        self.assertIn("actions/repository-ruleset/README.md", led_to(self.readme))
        self.assertTrue((REPOSITORY_ROOT / template).is_file())
        self.assertTrue(on_surface(template, json.loads(SURFACE.read_text(encoding="utf-8"))["paths"]))


class AutoMergeConsumerKitTests(unittest.TestCase):
    def setUp(self) -> None:
        entries = {entry["name"]: entry for entry in json.loads(CATALOGUE.read_text(encoding="utf-8"))["parts"]}
        self.entry = entries["Dependabot auto-merge"]
        self.kit = REPOSITORY_ROOT / self.entry["consumer_kit"]
        self.readme = (self.kit / "README.md").read_text(encoding="utf-8")

    def test_the_catalogue_entry_names_the_consumer_kit_beside_the_part(self) -> None:
        self.assertEqual("actions/dependabot-auto-merge/consumer-kit", self.entry["consumer_kit"])
        self.assertTrue((self.kit / "dependabot-auto-merge.yml").is_file())
        self.assertTrue((self.kit / "dependabot.yml").is_file())

    def test_the_readme_names_what_a_consumer_installs_needs_and_when_it_applies(self) -> None:
        for text in ("dependabot-auto-merge.yml", "dependabot.yml", ".github/workflows/dependabot-auto-merge.yml",
                     ".github/dependabot.yml", "<required-check-workflows>", "contents: write", "pull-requests: write",
                     "CrankyAnt/SharedWorkflows/.github/workflows/dependabot-auto-merge.yml@v1", "patch", "minor", "actions/*",
                     "ruleset", "Dependabot alerts", "security updates", "When a repository adopts it"):
            with self.subTest(text=text):
                self.assertIn(text, self.readme)

    def test_the_readme_names_nothing_that_exists_only_in_shared_workflows(self) -> None:
        for text in ("dependabot-auto-merge-caller", "crankyant-publication-checks", "etw-consumer", "tests/", "docs/agents",
                     "maintaining.md", "$/.github", "in SharedWorkflows only"):
            with self.subTest(text=text):
                self.assertNotIn(text, self.readme)
        for template in ("dependabot-auto-merge.yml", "dependabot.yml"):
            self.assertNotIn("$/actions", (self.kit / template).read_text(encoding="utf-8"))

    def test_the_readme_states_the_rights_and_the_ignore_entry_that_the_templates_hold(self) -> None:
        caller = (self.kit / "dependabot-auto-merge.yml").read_text(encoding="utf-8")
        self.assertIn("contents: write", caller)
        self.assertIn("pull-requests: write", caller)
        self.assertIn("dependency-name: CrankyAnt/SharedWorkflows", (self.kit / "dependabot.yml").read_text(encoding="utf-8"))
        self.assertIn("CrankyAnt/SharedWorkflows", self.readme)


class SelectionConsumerKitPinSourceTests(unittest.TestCase):
    KIT = REPOSITORY_ROOT / "actions" / "suite-selection" / "consumer-kit"
    README = ("Replace each `@<pin>`:\n\n| Template | Pin source |\n| --- | --- |\n"
              "| [kit.yml](kit.yml) | `.github/workflows/source.yml` |\n\nThe installed line then reads ...\n")
    TEMPLATE = {"kit.yml": "jobs:\n  unit:\n    steps:\n      - uses: actions/checkout@<pin>\n        with:\n          persist-credentials: false\n"}
    PINNED = "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"

    def test_the_readme_names_a_pin_source_for_each_template_that_pins_every_action_the_template_references(self) -> None:
        readme = (self.KIT / "README.md").read_text(encoding="utf-8")
        kit = self.KIT.relative_to(REPOSITORY_ROOT).as_posix()
        templates = {Path(path).name: (REPOSITORY_ROOT / path).read_text(encoding="utf-8")
                     for path in sorted(tracked_files(REPOSITORY_ROOT, [kit])) if re.fullmatch(rf"{re.escape(kit)}/[^/]+\.ya?ml", path)}
        self.assertEqual({"validation.yml", "validation-workflow.yml", "full-recording.yml", "apply-additions.yml"}, set(templates))
        self.assertEqual({"validation-workflow.yml": [".github/workflows/validate.yml"],
                          "full-recording.yml": [".github/workflows/full-recording.yml"],
                          "apply-additions.yml": [".github/workflows/full-recording.yml"]}, pin_sources(readme))
        self.assertEqual([], kit_pin_problems(readme, templates, tracked_workflows(REPOSITORY_ROOT)))

    def test_a_pin_source_that_pins_every_action_of_its_template_passes(self) -> None:
        self.assertEqual([], kit_pin_problems(self.README, self.TEMPLATE, {".github/workflows/source.yml": self.PINNED}))

    def test_a_quoted_action_reference_of_a_template_is_checked_by_its_action(self) -> None:
        for quote in ("'", '"'):
            with self.subTest(quote=quote):
                template = {"kit.yml": f"      - uses: {quote}actions/setup-python@<pin>{quote}\n"}
                self.assertEqual(["kit.yml: its pin source .github/workflows/source.yml does not reference actions/setup-python"],
                                 kit_pin_problems(self.README, template, {".github/workflows/source.yml": self.PINNED}))

    def test_an_action_that_the_pin_source_does_not_reference_fails_and_names_the_template_the_action_and_the_pin_source(self) -> None:
        workflows = {".github/workflows/source.yml": "      - uses: actions/checkout-extra@3d3c42e5aac5ba805825da76410c181273ba90b1 # v1.0.0\n"}
        self.assertEqual(["kit.yml: its pin source .github/workflows/source.yml does not reference actions/checkout"],
                         kit_pin_problems(self.README, self.TEMPLATE, workflows))

    def test_an_action_that_the_pin_source_references_without_a_commit_sha_and_a_version_comment_fails(self) -> None:
        for form, reference in (("tag", "v7"), ("short sha", "3d3c42e # v7.0.1"), ("no version comment", "3d3c42e5aac5ba805825da76410c181273ba90b1"),
                                ("one of two references", "3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n      - uses: actions/checkout@v7")):
            with self.subTest(form=form):
                workflows = {".github/workflows/source.yml": f"      - uses: actions/checkout@{reference}\n"}
                self.assertEqual(["kit.yml: its pin source .github/workflows/source.yml references actions/checkout"
                                  " without a 40-character commit SHA and a version comment"],
                                 kit_pin_problems(self.README, self.TEMPLATE, workflows))

    def test_a_pin_source_that_is_no_tracked_workflow_fails_and_names_the_template_and_the_pin_source(self) -> None:
        renamed = {".github/workflows/renamed.yml": self.PINNED}
        self.assertEqual(["kit.yml: its pin source .github/workflows/source.yml is no tracked workflow under .github/workflows/"],
                         kit_pin_problems(self.README, self.TEMPLATE, renamed))
        elsewhere = self.README.replace(".github/workflows/source.yml", "actions/source.yml")
        self.assertEqual(["kit.yml: its pin source actions/source.yml is no tracked workflow under .github/workflows/"],
                         kit_pin_problems(elsewhere, self.TEMPLATE, {"actions/source.yml": self.PINNED}))

    def test_a_template_with_a_pin_but_no_single_pin_source_fails_and_is_named(self) -> None:
        workflows = {".github/workflows/source.yml": self.PINNED, ".github/workflows/other.yml": self.PINNED}
        two_sources = self.README.replace("`.github/workflows/source.yml` |", "`.github/workflows/source.yml`, `.github/workflows/other.yml` |")
        two_rows = self.README.replace("| [kit.yml](kit.yml) |", "| [kit.yml](kit.yml) | `.github/workflows/other.yml` |\n| [kit.yml](kit.yml) |")
        for form, readme, count in (("no row", self.README.replace("[kit.yml](kit.yml)", "[other.yml](other.yml)"), "no"),
                                    ("no table", "Replace each `@<pin>` with the pin of the same action.\n", "no"),
                                    ("two sources in a row", two_sources, "2"), ("two rows", two_rows, "2")):
            with self.subTest(form=form):
                self.assertEqual([f"kit.yml carries @<pin> but the README names {count} pin sources for it, not one"],
                                 kit_pin_problems(readme, self.TEMPLATE, workflows))

    def test_a_template_without_a_pin_needs_no_pin_source(self) -> None:
        template = {"caller.yml": "jobs:\n  select:\n    uses: CrankyAnt/SharedWorkflows/.github/workflows/suite-selection.yml@v1\n"}
        self.assertEqual([], kit_pin_problems("No table.\n", template, {}))
