"""The shared CI surface records every tracked resource shared workflows load."""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from tests.ci_policy_support import REPOSITORY_ROOT, ci_definition, is_tracked, shared_workflows, tracked_files, tracked_workflows, uses_values


SURFACE = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "shared-surface.json"
CATALOGUE = REPOSITORY_ROOT / "catalogue.json"
PARTS = ("publication-checks", "repository-hygiene", "dependabot-auto-merge", "suite-selection", "recording", "python-language-step", "python-tests", "pipeline", "dotnet-test", "node-test", "issue-title", "sync")
ENTRY_TEXTS = ("name", "purpose", "needs", "applies")
# The consumer kits whose README leads to the ruleset template.
LEADING_KITS = ("actions/repository-hygiene/consumer-kit", "actions/pipeline/consumer-kit")
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
                 for reference in (value.removeprefix("$/") for value in uses_values(workflows[path], path) if value.startswith("$/"))
                 if reference not in surface_paths]
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
    # A uses value written plain, double-quoted and single-quoted.
    QUOTES = ("", '"', "'")

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
        surface_paths = [*self.SURFACE_PATHS, ".github/workflows/second.yml"]
        for quote in self.QUOTES:
            steps = (f"jobs:\n  unit:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: {quote}$/actions/part{quote}\n"
                     f"      - name: Elsewhere\n        uses: {quote}$/actions/elsewhere{quote}\n")
            for form, on in (("string", "on: workflow_call\n"), ("flow list", "on: [push, workflow_call]\n"), ("mapping", "on:\n  workflow_call:\n")):
                with self.subTest(quote=quote, form=form):
                    workflows = {".github/workflows/shared.yml": "on: workflow_call\n", ".github/workflows/second.yml": on + steps}
                    self.assertEqual([".github/workflows/second.yml loads actions/elsewhere outside the shared surface"],
                                     shared_workflow_problems(workflows, surface_paths))

    def test_a_workflow_with_a_workflow_call_trigger_that_calls_a_workflow_outside_the_shared_surface_fails_and_is_named(self) -> None:
        for quote in self.QUOTES:
            with self.subTest(quote=quote):
                text = (f"on: workflow_call\njobs:\n  shared:\n    uses: {quote}$/.github/workflows/shared.yml{quote}\n"
                        f"  elsewhere:\n    uses: {quote}$/.github/workflows/elsewhere.yml{quote}\n")
                self.assertEqual([".github/workflows/shared.yml loads .github/workflows/elsewhere.yml outside the shared surface"],
                                 shared_workflow_problems({".github/workflows/shared.yml": text}, self.SURFACE_PATHS))

    def test_a_trailing_comment_after_a_uses_value_is_no_part_of_the_reported_path(self) -> None:
        for quote in self.QUOTES:
            with self.subTest(quote=quote):
                text = (f"on: workflow_call\njobs:\n  caller:\n    uses:   {quote}$/.github/workflows/elsewhere.yml{quote}   # the caller, v2\n"
                        f"  unit:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: {quote}$/actions/elsewhere{quote} # a step, not shared\n"
                        f"      - uses:  {quote}$/actions/part{quote}  # on the surface\n")
                self.assertEqual([".github/workflows/shared.yml loads .github/workflows/elsewhere.yml outside the shared surface",
                                  ".github/workflows/shared.yml loads actions/elsewhere outside the shared surface"],
                                 shared_workflow_problems({".github/workflows/shared.yml": text}, self.SURFACE_PATHS))

    def test_a_workflow_with_a_workflow_call_trigger_that_loads_only_paths_on_the_shared_surface_passes(self) -> None:
        for quote in self.QUOTES:
            with self.subTest(quote=quote):
                text = (f"on: workflow_call\njobs:\n  shared:\n    uses: {quote}$/.github/workflows/shared.yml{quote} # itself\n"
                        f"  unit:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: {quote}$/actions/part{quote}\n"
                        f"      - uses: {quote}actions/checkout@v6{quote}\n      - run: echo 'uses: $/actions/elsewhere'\n")
                self.assertEqual([], shared_workflow_problems({".github/workflows/shared.yml": text}, self.SURFACE_PATHS))

    def test_a_workflow_without_a_workflow_call_trigger_may_load_a_path_outside_the_shared_surface(self) -> None:
        for quote in self.QUOTES:
            with self.subTest(quote=quote):
                workflows = {".github/workflows/shared.yml": "on: workflow_call\n",
                             ".github/workflows/caller.yml": f"on: push\njobs:\n  unit:\n    uses: {quote}$/.github/workflows/elsewhere.yml{quote}\n"}
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
        # Without the consumer kit of repository hygiene, the consumer kit of the pipeline, which several of its parts name
        # too, still leads to the ruleset template.
        unreached: list[str] = []
        for key, value, problem, more in (
            ("workflows", [*first["workflows"], ".github/workflows/missing.yml"], "the workflow .github/workflows/missing.yml is no shared workflow", []),
            ("steps", [*first["steps"], "actions/missing"], "the step actions/missing is no shared step", []),
            ("consumer_kit", "actions/repository-hygiene/missing-kit", "the consumer kit actions/repository-hygiene/missing-kit does not exist on the shared surface", unreached),
        ):
            with self.subTest(key=key):
                catalogue = {"parts": [{**first, key: value}, *self.catalogue["parts"][1:]]}
                self.assertEqual([f"{first['name']}: {problem}", *more], catalogue_problems(catalogue, self.surface, self.tracked))
        without_leading_kits = {"parts": [{**entry, "consumer_kit": None} if entry["consumer_kit"] in LEADING_KITS else entry for entry in self.catalogue["parts"]]}
        self.assertEqual(["actions/repository-ruleset holds no step and no consumer kit leads to it"], catalogue_problems(without_leading_kits, self.surface, self.tracked))

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
        self.assertEqual(["dependabot.yml"], sorted(path.name for path in self.kit.glob("*.yml")), "the part has no caller template: the fixed caller reaches it")

    def test_the_readme_names_what_a_consumer_installs_needs_and_when_it_applies(self) -> None:
        for text in ("`dependabot-auto-merge`", "dependabot.yml", ".github/ci-config.yml", "dependabot-auto-merge: {}",
                     ".github/dependabot.yml", "contents: write", "pull-requests: write", "without a runner",
                     "CrankyAnt/SharedWorkflows/.github/workflows/automation.yml@v1", "patch", "minor", "actions/*",
                     "ruleset", "Dependabot alerts", "security updates", "When a repository adopts it"):
            with self.subTest(text=text):
                self.assertIn(text, self.readme)

    def test_the_readme_names_nothing_that_exists_only_in_shared_workflows(self) -> None:
        for text in ("dependabot-auto-merge-caller", "crankyant-publication-checks", "etw-consumer", "tests/", "docs/agents",
                     "maintaining.md", "$/.github", "in SharedWorkflows only"):
            with self.subTest(text=text):
                self.assertNotIn(text, self.readme)
        self.assertNotIn("$/actions", (self.kit / "dependabot.yml").read_text(encoding="utf-8"))

    def test_the_readme_states_the_rights_of_the_part_s_job_and_the_ignore_entry_that_the_template_holds(self) -> None:
        automation = ci_definition().read_yaml((REPOSITORY_ROOT / ".github" / "workflows" / "automation.yml").read_text(encoding="utf-8"), "automation.yml")
        self.assertEqual({"contents": "write", "pull-requests": "write"}, automation["jobs"]["dependabot-auto-merge"]["permissions"])
        self.assertIn("the part's job takes both", self.readme)
        self.assertIn("an auto-merge that it still has is disabled", self.readme)
        self.assertIn("dependency-name: CrankyAnt/SharedWorkflows", (self.kit / "dependabot.yml").read_text(encoding="utf-8"))
        self.assertIn("CrankyAnt/SharedWorkflows", self.readme)


class SelectionConsumerKitPinSourceTests(unittest.TestCase):
    KIT = REPOSITORY_ROOT / "actions" / "suite-selection" / "consumer-kit"
    README = ("Replace each `@<pin>`:\n\n| Template | Pin source |\n| --- | --- |\n"
              "| [kit.yml](kit.yml) | `.github/workflows/source.yml` |\n\nThe installed line then reads ...\n")
    TEMPLATE = {"kit.yml": "jobs:\n  unit:\n    steps:\n      - uses: actions/checkout@<pin>\n        with:\n          persist-credentials: false\n"}
    PINNED = "      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1\n"

    def test_the_kit_holds_no_template_so_its_readme_names_no_pin_source(self) -> None:
        readme = (self.KIT / "README.md").read_text(encoding="utf-8")
        kit = self.KIT.relative_to(REPOSITORY_ROOT).as_posix()
        templates = {Path(path).name: (REPOSITORY_ROOT / path).read_text(encoding="utf-8")
                     for path in sorted(tracked_files(REPOSITORY_ROOT, [kit])) if re.fullmatch(rf"{re.escape(kit)}/[^/]+\.ya?ml", path)}
        self.assertEqual(set(), set(templates), "the kit holds no template: the suite jobs are a shared workflow with pins of its own")
        self.assertEqual({}, pin_sources(readme))
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


CLASSES = ("read", "write", "template")
WRITE_GRANT = re.compile(r"(?m)^\s*(?:permission-)?[a-z-]+: write\s*(?:#.*)?$")


def class_of(path: str, classes: dict[str, str]) -> str | None:
    """The promotion class of a path: that of the longest recorded path that holds it."""
    holders = [item for item in classes if path == item or path.startswith(item + "/")]
    return classes[max(holders, key=len)] if holders else None


def class_problems(surface_paths: list[str], pairs: list[tuple[str, object]]) -> list[str]:
    """Each way the recorded promotion classes, as the pairs of the record's ``classes``, are out of step with the shared surface."""
    names = [name for name, _ in pairs]
    problems = [f"{path} is on the shared surface without a promotion class" for path in surface_paths if path not in names]
    problems += [f"{name} has more than one promotion class" for name in sorted({name for name in names if names.count(name) > 1})]
    problems += [f"{name} has a promotion class but is not on the shared surface" for name in names if not on_surface(name, surface_paths)]
    problems += [f"{name} has the promotion class {value!r}, which is none of {', '.join(CLASSES)}" for name, value in pairs if value not in CLASSES]
    return problems


def write_class_problems(workflows: dict[str, str], classes: dict[str, str]) -> list[str]:
    """Each shared workflow that grants a write permission, to a job or to an App token, and is in the read class, and
    each recorded path in the read class at or below a part folder that a shared workflow outside the read class loads:
    a path takes the highest class among the workflows that load it, whatever else loads it."""
    shared = shared_workflows(workflows)
    problems = [f"{path} grants a write permission but is in the read class" for path in shared
                if WRITE_GRANT.search(workflows[path]) and class_of(path, classes) == "read"]
    for path in shared:
        loader = class_of(path, classes)
        if loader in (None, "read"):
            continue
        for reference in sorted({value.removeprefix("$/") for value in uses_values(workflows[path], path) if value.startswith("$/")}):
            held = [reference, *sorted(name for name in classes if name.startswith(reference + "/"))]
            problems += [f"{name} is loaded by {path}, which is in the {loader} class, but is in the read class"
                         for name in held if class_of(name, classes) == "read"]
    return list(dict.fromkeys(problems))


class PromotionClassTests(unittest.TestCase):
    def setUp(self) -> None:
        text = SURFACE.read_text(encoding="utf-8")
        self.surface = json.loads(text)
        self.pairs: list[tuple[str, object]] = []

        def keep(pairs: list[tuple[str, object]]) -> dict:
            if any(name == ".github/workflows/pipeline.yml" for name, _ in pairs):
                self.pairs.extend(pairs)
            return dict(pairs)

        json.loads(text, object_pairs_hook=keep)
        self.classes = self.surface["classes"]

    def test_every_path_of_the_shared_surface_has_exactly_one_promotion_class(self) -> None:
        self.assertEqual(sorted(self.classes), sorted(name for name, _ in self.pairs), "the record's classes were read as pairs")
        self.assertEqual([], class_problems(self.surface["paths"], self.pairs))
        tracked = tracked_files(REPOSITORY_ROOT, self.surface["paths"])
        self.assertTrue(tracked)
        for path in sorted(tracked):
            with self.subTest(path=path):
                self.assertIn(class_of(path, self.classes), CLASSES)

    def test_a_path_without_a_class_fails_and_is_named(self) -> None:
        surface_paths = [".github/workflows/shared.yml", "actions/part", "actions/new-part"]
        pairs = [(".github/workflows/shared.yml", "read"), ("actions/part", "read"), ("actions/part/consumer-kit", "template")]
        self.assertEqual(["actions/new-part is on the shared surface without a promotion class"], class_problems(surface_paths, pairs))
        self.assertEqual([], class_problems(surface_paths, [*pairs, ("actions/new-part", "write")]))

    def test_a_second_class_an_unknown_class_and_a_class_off_the_surface_fail_and_are_named(self) -> None:
        surface_paths = [".github/workflows/shared.yml", "actions/part"]
        pairs = [(".github/workflows/shared.yml", "read"), ("actions/part", "read")]
        self.assertEqual(["actions/part has more than one promotion class"], class_problems(surface_paths, [*pairs, ("actions/part", "write")]))
        self.assertEqual(["actions/part/x has the promotion class 'safe', which is none of read, write, template"], class_problems(surface_paths, [*pairs, ("actions/part/x", "safe")]))
        self.assertEqual(["actions/elsewhere has a promotion class but is not on the shared surface"], class_problems(surface_paths, [*pairs, ("actions/elsewhere", "read")]))

    def test_a_path_takes_the_class_of_the_longest_recorded_path_that_holds_it(self) -> None:
        classes = {"actions/part": "read", "actions/part/consumer-kit": "template", "actions/part/write.py": "write"}
        for path, expected in (("actions/part/plan.py", "read"), ("actions/part/consumer-kit/ci.yml", "template"), ("actions/part/write.py", "write"),
                               ("actions/part/write.py.txt", "read"), ("actions/partial/x", None)):
            with self.subTest(path=path):
                self.assertEqual(expected, class_of(path, classes))

    def test_no_shared_workflow_that_writes_and_no_step_that_a_workflow_outside_the_read_class_loads_is_in_the_read_class(self) -> None:
        workflows = tracked_workflows(REPOSITORY_ROOT)
        self.assertEqual([], write_class_problems(workflows, self.classes))
        for path in (".github/workflows/automation.yml", ".github/workflows/release.yml", ".github/workflows/dependabot-auto-merge.yml"):
            with self.subTest(path=path):
                self.assertEqual("write", class_of(path, self.classes))
        # The part folders that automation.yml and release.yml load beside the read workflows.
        # The shared workflow that automation.yml calls beside the pipeline, and the step that it loads, follow it.
        for path in ("actions/pipeline/plan.py", "actions/recording/recording.py", "actions/repository-hygiene/hygiene.py", "actions/suite-selection/suite_selection.py",
                     ".github/workflows/suite-jobs.yml", "actions/python-language-step/python_language_step.py"):
            with self.subTest(path=path):
                self.assertEqual("write", class_of(path, self.classes))

    def test_a_step_that_a_read_workflow_and_a_write_workflow_both_load_fails_in_the_read_class_and_is_named(self) -> None:
        workflows = {".github/workflows/reads.yml": "on: workflow_call\njobs:\n  unit:\n    permissions:\n      contents: read\n    steps:\n      - uses: $/actions/both\n      - uses: $/actions/reading\n",
                     ".github/workflows/writes.yml": "on: workflow_call\njobs:\n  unit:\n    permissions:\n      contents: write\n    steps:\n      - uses: $/actions/both\n      - uses: $/actions/own\n",
                     ".github/workflows/mints.yml": "on: workflow_call\njobs:\n  unit:\n    steps:\n      - uses: actions/create-github-app-token@v3\n        with:\n          permission-contents: write\n"}
        read = dict.fromkeys([*workflows, "actions/both", "actions/own", "actions/reading"], "read")
        self.assertEqual([".github/workflows/writes.yml grants a write permission but is in the read class",
                          ".github/workflows/mints.yml grants a write permission but is in the read class"], write_class_problems(workflows, read))
        classed = {**read, ".github/workflows/writes.yml": "write", ".github/workflows/mints.yml": "template"}
        self.assertEqual(["actions/both is loaded by .github/workflows/writes.yml, which is in the write class, but is in the read class",
                          "actions/own is loaded by .github/workflows/writes.yml, which is in the write class, but is in the read class"], write_class_problems(workflows, classed))
        self.assertEqual([], write_class_problems(workflows, {**classed, "actions/both": "write", "actions/own": "template"}), "a step that only a read workflow loads stays in the read class")

    def test_a_shared_workflow_that_a_write_workflow_calls_fails_in_the_read_class_with_the_steps_it_loads(self) -> None:
        workflows = {".github/workflows/writes.yml": "on: workflow_call\njobs:\n  called:\n    permissions:\n      contents: write\n    uses: $/.github/workflows/jobs.yml\n",
                     ".github/workflows/jobs.yml": "on: workflow_call\njobs:\n  unit:\n    permissions:\n      contents: read\n    steps:\n      - uses: $/actions/step\n"}
        classes = {".github/workflows/writes.yml": "write", ".github/workflows/jobs.yml": "read", "actions/step": "read"}
        self.assertEqual([".github/workflows/jobs.yml is loaded by .github/workflows/writes.yml, which is in the write class, but is in the read class"],
                         write_class_problems(workflows, classes))
        self.assertEqual(["actions/step is loaded by .github/workflows/jobs.yml, which is in the write class, but is in the read class"],
                         write_class_problems(workflows, {**classes, ".github/workflows/jobs.yml": "write"}))
        self.assertEqual([], write_class_problems(workflows, {**classes, ".github/workflows/jobs.yml": "write", "actions/step": "write"}))

    def test_a_read_class_path_inside_a_step_that_a_write_workflow_loads_fails_and_is_named(self) -> None:
        workflows = {".github/workflows/writes.yml": "on: workflow_call\njobs:\n  unit:\n    permissions:\n      contents: write\n    steps:\n      - uses: $/actions/own\n"}
        classes = {".github/workflows/writes.yml": "write", "actions/own": "write", "actions/own/kit": "template", "actions/own/data.json": "read"}
        self.assertEqual(["actions/own/data.json is loaded by .github/workflows/writes.yml, which is in the write class, but is in the read class"],
                         write_class_problems(workflows, classes))

    def test_the_templates_the_sync_and_the_ruleset_template_are_in_the_template_class(self) -> None:
        kits = sorted({path.rsplit("/consumer-kit/", 1)[0] + "/consumer-kit" for path in tracked_files(REPOSITORY_ROOT, ["actions/*/consumer-kit/*"])})
        self.assertIn("actions/pipeline/consumer-kit", kits)
        for path in (*kits, ".github/workflows/sync.yml", "actions/sync/sync.py", "actions/repository-ruleset/ruleset-main-template.json",
                     "actions/pipeline/consumer_checks.py"):
            with self.subTest(path=path):
                self.assertEqual("template", class_of(path, self.classes))

    def test_the_promotion_program_and_the_record_of_the_classes_are_outside_the_read_class(self) -> None:
        for path in ("actions/repository-hygiene/promote_v1.py", "actions/repository-hygiene/shared-surface.json"):
            with self.subTest(path=path):
                self.assertEqual("write", class_of(path, self.classes))
        self.assertEqual("write", class_of(self.surface["pins"], self.classes), "the automation workflow installs the pinned tools, so a pins update is a question")
        self.assertEqual("read", class_of(".github/workflows/pipeline.yml", self.classes))
