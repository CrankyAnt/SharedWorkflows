"""Text contract of the fixed callers: each template, and each previous version that hygiene still accepts, starts only
on the events of its trigger class, calls its one shared workflow at v1 and passes secrets by name; and a caller of the
pipeline, the previous version included, lets every job of the pipeline start."""

from __future__ import annotations

import json
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, ci_definition, read_workflow, tracked_files, workflow_triggers

KIT = "actions/pipeline/consumer-kit"
RECORD = REPOSITORY_ROOT / KIT / "fixed-callers.json"
PIPELINE = REPOSITORY_ROOT / ".github" / "workflows" / "pipeline.yml"
SHARED = "CrankyAnt/SharedWorkflows/.github/workflows/"
# The trigger classes of the spec: per fixed caller its events and the shared workflow it calls. The read class gets
# read permissions only.
CLASSES = {
    "ci.yml": {"class": "read", "events": {"pull_request", "push"}, "calls": "pipeline.yml"},
    "automation.yml": {"class": "write", "events": {"workflow_run", "schedule", "workflow_dispatch", "issues"}, "calls": "automation.yml"},
    "release.yml": {"class": "release", "events": {"push", "workflow_dispatch"}, "calls": "release.yml"},
}
LEVELS = {"none": 0, "read": 1, "write": 2}


def caller_problems(name: str, text: str) -> list[str]:
    """Each way the text of a fixed caller breaks the rules of its trigger class."""
    rules = CLASSES[name]
    problems = []
    if "pull_request_target" in text:
        problems.append(f"{name} names pull_request_target")
    document = ci_definition().read_yaml(text, name)
    jobs = document.get("jobs") if isinstance(document.get("jobs"), dict) else {}
    outside = sorted(workflow_triggers(text, name) - rules["events"])
    problems += [f"{name} starts on {event}, which is outside the {rules['class']} class" for event in outside]
    missing = sorted(rules["events"] - workflow_triggers(text, name))
    problems += [f"{name} does not start on {event}" for event in missing]
    if document.get("permissions") != "{}":
        problems.append(f"{name} does not set permissions: {{}} at the top")
    job_id = name.removesuffix(".yml")
    if list(jobs) != [job_id]:
        problems.append(f"{name} does not hold the one job {job_id}")
    for job_name, job in jobs.items():
        if job.get("uses") != f"{SHARED}{rules['calls']}@v1":
            problems.append(f"{name}: the job {job_name} does not call {rules['calls']} at v1")
        if "steps" in job or "runs-on" in job:
            problems.append(f"{name}: the job {job_name} runs steps of its own")
        if job.get("secrets") == "inherit":
            problems.append(f"{name}: the job {job_name} inherits every secret")
        elif isinstance(job.get("secrets"), dict):
            problems += [f"{name}: the secret {secret} is not passed under its own name" for secret, value in job["secrets"].items()
                         if value != f"${{{{ secrets.{secret} }}}}"]
        if not isinstance(job.get("permissions"), dict):
            problems.append(f"{name}: the job {job_name} states no permissions")
        elif rules["class"] == "read":
            problems += [f"{name}: the job {job_name} holds {scope}: {level} in the read class"
                         for scope, level in job["permissions"].items() if level != "read"]
    return problems


def reach_problems(caller: dict, pipeline: dict) -> list[str]:
    """Each way a caller of the pipeline keeps a job of the pipeline from starting: an event it does not run on, or a
    permission of a job that the caller's job does not grant."""
    on = caller.get("on") if isinstance(caller.get("on"), dict) else {}
    problems = [f"the caller does not run on {event}" for event in ("pull_request", "push") if event not in on]
    callers = [job for job in caller.get("jobs", {}).values() if str(job.get("uses", "")).endswith("/pipeline.yml@v1")]
    if len(callers) != 1:
        return [*problems, "the caller does not call the pipeline at v1 in one job"]
    granted = callers[0].get("permissions") or {}
    for name, job in pipeline["jobs"].items():
        for scope, level in (job.get("permissions") or {}).items():
            if LEVELS[str(granted.get(scope, "none"))] < LEVELS[str(level)]:
                problems.append(f"the job {name} needs {scope}: {level}, which the caller does not grant")
    return problems


def previous_versions() -> dict[str, str]:
    """The text of each previous caller version that the record names, by the caller's file name."""
    record = json.loads(RECORD.read_text(encoding="utf-8"))
    return {name: (REPOSITORY_ROOT / KIT / "previous" / name).read_text(encoding="utf-8") for name in record["previous"]}


class TemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.templates = {name: (REPOSITORY_ROOT / KIT / name).read_text(encoding="utf-8") for name in CLASSES}

    def test_the_kit_holds_exactly_the_three_fixed_callers(self) -> None:
        tracked = tracked_files(REPOSITORY_ROOT, [KIT])
        self.assertEqual(sorted(f"{KIT}/{name}" for name in CLASSES), sorted(path for path in tracked if path.endswith(".yml") and "/previous/" not in path))

    def test_every_template_follows_the_rules_of_its_trigger_class(self) -> None:
        for name, text in self.templates.items():
            with self.subTest(template=name):
                self.assertEqual([], caller_problems(name, text))

    def test_every_previous_version_follows_the_rules_of_its_trigger_class(self) -> None:
        for name, text in previous_versions().items():
            with self.subTest(previous=name):
                self.assertEqual([], caller_problems(name, text))

    def test_the_record_names_only_fixed_callers_whose_previous_version_is_tracked_and_differs_from_the_template(self) -> None:
        record = json.loads(RECORD.read_text(encoding="utf-8"))
        self.assertEqual({"previous", "sync-branch"}, set(record))
        tracked = tracked_files(REPOSITORY_ROOT, [f"{KIT}/previous"])
        self.assertEqual(sorted(f"{KIT}/previous/{name}" for name in record["previous"]), sorted(tracked))
        for name, promoted in record["previous"].items():
            with self.subTest(previous=name):
                self.assertIn(name, CLASSES)
                self.assertRegex(promoted, r"^\d{4}-\d{2}-\d{2}$")
                self.assertNotEqual(self.templates[name], previous_versions()[name])

    def test_the_read_caller_runs_on_pull_requests_and_pushes_to_main(self) -> None:
        _, caller = read_workflow(REPOSITORY_ROOT / KIT / "ci.yml")
        self.assertEqual({"pull_request": None, "push": {"branches": "[main]"}}, caller["on"])
        self.assertEqual({"actions": "read", "contents": "read", "issues": "read", "pull-requests": "read"}, caller["jobs"]["ci"]["permissions"])

    def test_the_write_caller_runs_on_the_automation_events_and_only_on_opened_issues(self) -> None:
        text, caller = read_workflow(REPOSITORY_ROOT / KIT / "automation.yml")
        self.assertEqual({"workflows": "[CI]", "types": "[completed]"}, caller["on"]["workflow_run"])
        self.assertEqual({"types": "[opened]"}, caller["on"]["issues"])
        self.assertIsNone(caller["on"]["workflow_dispatch"])
        self.assertEqual(1, text.count("- cron: "))

    def test_the_release_caller_runs_on_tag_pushes_only_and_by_hand(self) -> None:
        _, caller = read_workflow(REPOSITORY_ROOT / KIT / "release.yml")
        self.assertEqual({"push": {"tags": '["**"]'}, "workflow_dispatch": None}, caller["on"])

    def test_a_caller_that_breaks_a_rule_is_named(self) -> None:
        ci, automation = self.templates["ci.yml"], self.templates["automation.yml"]
        cases = (
            ("ci.yml", ci.replace("  pull_request:\n", "  pull_request:\n  pull_request_target:\n"),
             ["ci.yml names pull_request_target", "ci.yml starts on pull_request_target, which is outside the read class"]),
            ("ci.yml", ci.replace("  push:\n", "  workflow_dispatch:\n  push:\n"), ["ci.yml starts on workflow_dispatch, which is outside the read class"]),
            ("ci.yml", ci.replace("      issues: read\n", "      issues: write\n"), ["ci.yml: the job ci holds issues: write in the read class"]),
            ("ci.yml", ci.replace("@v1", "@main"), ["ci.yml: the job ci does not call pipeline.yml at v1"]),
            ("ci.yml", ci.replace("permissions: {}\n", "permissions: read-all\n"), ["ci.yml does not set permissions: {} at the top"]),
            ("automation.yml", automation.replace("  workflow_dispatch:\n", "  workflow_dispatch:\n  pull_request:\n"),
             ["automation.yml starts on pull_request, which is outside the write class"]),
            ("automation.yml", automation.replace("  workflow_dispatch:\n", ""), ["automation.yml does not start on workflow_dispatch"]),
            ("release.yml", self.templates["release.yml"].replace("  workflow_dispatch:\n", "  workflow_dispatch:\n  schedule:\n    - cron: \"0 0 * * *\"\n"),
             ["release.yml starts on schedule, which is outside the release class"]),
        )
        for name, text, problems in cases:
            with self.subTest(problems=problems):
                self.assertEqual(problems, caller_problems(name, text))

    def test_a_caller_that_inherits_secrets_or_renames_one_is_named(self) -> None:
        automation = self.templates["automation.yml"]
        start = automation.index("    secrets:\n")
        end = automation.index("    uses:", start)
        inherited = automation[:start] + "    secrets: inherit\n" + automation[end:]
        self.assertEqual(["automation.yml: the job automation inherits every secret"], caller_problems("automation.yml", inherited))
        renamed = automation[:start] + "    secrets:\n      TOKEN: ${{ secrets.OTHER }}\n" + automation[end:]
        self.assertEqual(["automation.yml: the secret TOKEN is not passed under its own name"], caller_problems("automation.yml", renamed))


class ReachTests(unittest.TestCase):
    """A consumer whose caller equals the template, or the previous version, still starts every job of the pipeline."""

    def setUp(self) -> None:
        self.pipeline = read_workflow(PIPELINE)[1]
        self.template = (REPOSITORY_ROOT / KIT / "ci.yml").read_text(encoding="utf-8")

    def test_the_template_and_the_previous_version_of_the_read_caller_let_every_job_of_the_pipeline_start(self) -> None:
        versions = {"template": self.template, **{f"previous {name}": text for name, text in previous_versions().items() if name == "ci.yml"}}
        for label, text in versions.items():
            with self.subTest(version=label):
                self.assertEqual([], reach_problems(ci_definition().read_yaml(text, label), self.pipeline))

    def test_this_repository_s_own_caller_lets_every_job_of_the_pipeline_start(self) -> None:
        _, caller = read_workflow(REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml")
        granted = caller["jobs"]["ci"]["permissions"]
        _, template = read_workflow(REPOSITORY_ROOT / KIT / "ci.yml")
        self.assertEqual(template["jobs"]["ci"]["permissions"], granted)

    def test_a_previous_version_that_keeps_a_job_of_the_pipeline_from_starting_is_named(self) -> None:
        older = ci_definition().read_yaml(self.template.replace("      issues: read\n", ""), "an invented older version")
        self.assertEqual(["the job repository-hygiene needs issues: read, which the caller does not grant"], reach_problems(older, self.pipeline))
        without_push = ci_definition().read_yaml(self.template.replace("  push:\n    branches: [main]\n", ""), "an invented older version")
        self.assertEqual(["the caller does not run on push"], reach_problems(without_push, self.pipeline))
        elsewhere = ci_definition().read_yaml(self.template.replace("pipeline.yml@v1", "pipeline.yml@v0"), "an invented older version")
        self.assertEqual(["the caller does not call the pipeline at v1 in one job"], reach_problems(elsewhere, self.pipeline))


if __name__ == "__main__":
    unittest.main()
