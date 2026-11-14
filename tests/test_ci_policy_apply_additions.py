"""This repository's additions workflow: the additions template of the suite-selection consumer kit, run here over
`Validate` and `tests/suites.json`, kept equal to the template apart from the stated substitutions. Its bound also
recognises the rebuild workflow's commit, and the rebuild workflow's bound its own commit, so that the two automatic
routes make at most one round per push. The run tests of both routes are in `tests/test_consumer_kit_apply_additions.py`."""

from __future__ import annotations

import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, ci_definition, read_workflow

TEMPLATE = REPOSITORY_ROOT / "actions" / "suite-selection" / "consumer-kit" / "apply-additions.yml"
WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
OWN_WORKFLOW = WORKFLOWS / "apply-additions.yml"
REBUILD = WORKFLOWS / "rebuild-etw-consumer.yml"
ADDITIONS_SUBJECT = "chore: apply the recorded additions of run 4100"
REBUILD_SUBJECT = "chore: rebuild the ETW consumer"
TEMPLATE_BOUND = "bound='^chore: apply the recorded additions of run [0-9]+$'"
OWN_BOUND = "bound='^(chore: apply the recorded additions of run [0-9]+|chore: rebuild the ETW consumer)$'"
GITLEAKS_OPTIONS = "--config .gitleaks.toml --baseline-path .gitleaks-baseline.json"
# The stated substitutions, from this repository's text to the template's, applied to every string of the workflow.
SUBSTITUTIONS = (
    ("[Validate]", "[Validation]"),
    ("$/actions/recording", "CrankyAnt/SharedWorkflows/actions/recording@v1"),
    ("$/actions/repository-hygiene", "CrankyAnt/SharedWorkflows/actions/repository-hygiene@v1"),
    ("tests/suites.json", ".github/suites.json"),
    (GITLEAKS_OPTIONS, "<gitleaks-options>"),
    (OWN_BOUND, TEMPLATE_BOUND),
)
PIN = re.compile(r"^([\w.-]+/[\w./-]+)@[0-9a-f]{40}$")


def step(steps: list[dict], key: str) -> dict:
    return next(item for item in steps if key in (item.get("id"), item.get("name")))


def as_template(value: object, key: str = "") -> object:
    """This repository's workflow document with the stated substitutions made and each commit pin of an action as the
    template's `<pin>`."""
    if isinstance(value, dict):
        return {name: as_template(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [as_template(item) for item in value]
    if isinstance(value, str):
        pinned = PIN.fullmatch(value) if key == "uses" else None
        if pinned:
            return f"{pinned[1]}@<pin>"
        for ours, theirs in SUBSTITUTIONS:
            value = value.replace(ours, theirs)
    return value


def template_differences(own_text: str, template_text: str) -> list[str]:
    """Where this repository's workflow differs from the template beyond the stated substitutions, as document paths."""
    read = ci_definition().read_yaml
    differences: list[str] = []

    def compare(ours: object, theirs: object, path: str) -> None:
        if isinstance(ours, dict) and isinstance(theirs, dict):
            for name in [*ours, *(name for name in theirs if name not in ours)]:
                compare(ours.get(name), theirs.get(name), f"{path}.{name}")
            if [name for name in ours if name in theirs] != [name for name in theirs if name in ours]:
                differences.append(f"{path}: the order of the keys")
        elif isinstance(ours, list) and isinstance(theirs, list) and len(ours) == len(theirs):
            for index, (mine, other) in enumerate(zip(ours, theirs)):
                compare(mine, other, f"{path}[{index}]")
        elif ours != theirs:
            differences.append(path)

    compare(as_template(read(own_text, str(OWN_WORKFLOW))), read(template_text, str(TEMPLATE)), "")
    return differences


class ApplyAdditionsWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(OWN_WORKFLOW)
        self.template = TEMPLATE.read_text(encoding="utf-8")

    def test_it_equals_the_template_apart_from_the_stated_substitutions(self) -> None:
        self.assertEqual([], template_differences(self.text, self.template))

    def test_a_difference_beyond_the_substitutions_fails_and_names_its_place(self) -> None:
        for name, changed, place in (
                ("a changed condition", self.text.replace("steps.changes.outputs.changed == 'true' && github.event_name == 'workflow_run'",
                                                          "steps.changes.outputs.changed == 'true'", 1), ".jobs.apply.steps["),
                ("a changed comment", self.text.replace("applying them leaves the suite list unchanged.", "nothing changed.", 1),
                 ".jobs.comment.steps[0].run"),
                ("another bound", self.text.replace(OWN_BOUND, TEMPLATE_BOUND.replace("[0-9]+", "[0-9]*"), 1), ".jobs.apply.steps[0].run"),
                ("a dropped step", re.sub(r"(?ms)^      - name: Check the changed files\n.*?(?=^      - )", "", self.text, count=1), ".jobs.apply.steps"),
                ("another trigger type", self.text.replace("    types: [completed]\n", "    types: [completed, requested]\n", 1), ".on.workflow_run.types"),
                ("a version tag for a pin", re.sub(r"actions/checkout@[0-9a-f]{40}", "actions/checkout@v7", self.text, count=1), ".jobs.apply.steps[1].uses"),
                ("another suite list", self.text.replace("tests/suites.json", "tests/other.json", 1), ".jobs.apply.steps[")):
            with self.subTest(change=name):
                self.assertNotEqual(self.text, changed, "the mutation applies")
                differences = template_differences(changed, self.template)
                self.assertTrue(differences, "the comparison fails")
                self.assertTrue(any(difference.startswith(place) for difference in differences), differences)

    def test_it_runs_over_validate_and_this_repository_s_suite_list_with_the_shared_steps_by_local_path(self) -> None:
        self.assertEqual(f"[{read_workflow(WORKFLOWS / 'validate.yml')[1]['name']}]", self.workflow["on"]["workflow_run"]["workflows"])
        self.assertRegex(self.text, r"(?m)^  workflow_run: # zizmor: ignore\[dangerous-triggers\] \S")
        steps = self.workflow["jobs"]["apply"]["steps"]
        self.assertEqual("$/actions/recording", step(steps, "recording")["uses"])
        self.assertEqual("$/actions/repository-hygiene", step(steps, "hygiene")["uses"])
        self.assertIn('"$RECORDING/recording.py" apply --suites tests/suites.json', step(steps, "apply")["run"])
        self.assertIn(f"gitleaks\" git {GITLEAKS_OPTIONS} --log-opts=", step(steps, "Scan the commit before pushing")["run"])
        for absent in ("CrankyAnt/SharedWorkflows/", "<pin>", "<gitleaks-options>", ".github/suites.json"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, self.text)
        self.assertEqual(self.text.count("uses: actions/"), len(re.findall(r"uses: actions/[\w-]+@[0-9a-f]{40} # v\d+(?:\.\d+)*\n", self.text)))

    def test_the_bound_recognises_both_app_subjects_and_nothing_else(self) -> None:
        bound = re.search(r"bound='(.+)'", step(self.workflow["jobs"]["apply"]["steps"], "run")["run"])[1]
        _, rebuild = read_workflow(REBUILD)
        rebuild_commit = step(rebuild["jobs"]["push"]["steps"], "Commit the rebuild as the GitHub App")["run"]
        self.assertIn(f'commit -m "{REBUILD_SUBJECT}"', rebuild_commit)
        for subject in (ADDITIONS_SUBJECT, REBUILD_SUBJECT):
            self.assertRegex(subject, bound)
        for subject in (f"{REBUILD_SUBJECT} (#12)", "fix: rebuild the ETW consumer", f"{ADDITIONS_SUBJECT} (#12)"):
            self.assertNotRegex(subject, bound)


if __name__ == "__main__":
    unittest.main()
