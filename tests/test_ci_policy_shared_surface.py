"""The shared CI surface records every tracked resource shared workflows load."""

from __future__ import annotations

import json
import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, is_tracked, tracked_files


WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
SURFACE = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "shared-surface.json"
CATALOGUE = REPOSITORY_ROOT / "catalogue.json"
SHARED_WORKFLOWS = ("repository-hygiene.yml", "dependabot-auto-merge.yml", "suite-selection.yml", "suite-verdict.yml")
PARTS = ("publication-checks", "repository-hygiene", "dependabot-auto-merge", "suite-selection", "recording", "python-language-step")
ENTRY_TEXTS = ("name", "purpose", "needs", "applies")


def on_surface(path: str, surface_paths: list[str]) -> bool:
    return any(path == item or path.startswith(item + "/") for item in surface_paths)


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
    problems += [f"{path} belongs to no catalogue entry" for path in surface_paths
                 if path.startswith((".github/workflows/", "actions/")) and path not in named]
    return problems


class SharedSurfaceTests(unittest.TestCase):
    def test_the_shared_surface_covers_every_resource_a_shared_workflow_loads_and_names_tracked_paths_only(self) -> None:
        surface = json.loads(SURFACE.read_text(encoding="utf-8"))
        self.assertEqual([f".github/workflows/{workflow}" for workflow in SHARED_WORKFLOWS] + ["catalogue.json"] + [f"actions/{part}" for part in PARTS], surface["paths"])
        tracked = tracked_files(REPOSITORY_ROOT, [surface["pins"], *surface["paths"]])
        self.assertIn(surface["pins"], tracked)
        for path in surface["paths"]:
            with self.subTest(path=path):
                self.assertTrue(is_tracked(path, tracked), f"{path} is in the shared surface but Git tracks nothing there")
        for workflow in SHARED_WORKFLOWS:
            text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
            self.assertIn(f".github/workflows/{workflow}", surface["paths"])
            for reference in re.findall(r"uses: \$/([^\s]+)", text):
                self.assertIn(reference, surface["paths"], f"{workflow} loads {reference} outside the shared surface")


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
        for key, value, problem in (
            ("workflows", [*first["workflows"], ".github/workflows/missing.yml"], "the workflow .github/workflows/missing.yml is no shared workflow"),
            ("steps", [*first["steps"], "actions/missing"], "the step actions/missing is no shared step"),
            ("consumer_kit", "actions/repository-hygiene/missing-kit", "the consumer kit actions/repository-hygiene/missing-kit does not exist on the shared surface"),
        ):
            with self.subTest(key=key):
                catalogue = {"parts": [{**first, key: value}, *self.catalogue["parts"][1:]]}
                self.assertEqual([f"{first['name']}: {problem}"], catalogue_problems(catalogue, self.surface, self.tracked))

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
