"""The management repository kit of the sync: the caller of the shared workflow, the instructions for agents and the
README with the environment's setup and the App's permissions."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from tests.consumer_kit_support import read_yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
KIT = REPOSITORY_ROOT / "actions" / "sync" / "consumer-kit"
SHARED_WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "sync.yml"
SECRET = "SYNC_APP_PRIVATE_KEY"
# What only this repository has, and what a template that a repository fills in would hold.
NOT_FIXED = re.compile(r"\$/|<[a-z-]+>|@[0-9a-f]{40}")


def section(readme: str, heading: str) -> str:
    """The section under a level-two heading, up to the next one or to the end."""
    start = readme.index(f"## {heading}\n")
    end = readme.find("\n## ", start + 1)
    return readme[start:] if end == -1 else readme[start:end]


def runs_per_day(cron: str) -> list[int]:
    """The hours at which a cron line of a fixed minute on every day starts."""
    minute, hours, day, month, weekday = cron.split()
    assert minute.isdigit() and (day, month, weekday) == ("*", "*", "*"), cron
    return sorted(int(hour) for hour in hours.split(","))


class CallerTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.caller = read_yaml(KIT / "sync.yml")

    def test_the_kit_holds_the_caller_the_instructions_and_the_readme_and_nothing_else(self) -> None:
        self.assertEqual(["AGENTS.template.md", "README.md", "sync.yml"], sorted(path.name for path in KIT.iterdir()))

    def test_the_caller_starts_every_twelve_hours_and_by_hand_and_on_nothing_else(self) -> None:
        self.assertEqual({"schedule", "workflow_dispatch"}, set(self.caller["on"]))
        self.assertIsNone(self.caller["on"]["workflow_dispatch"])
        (schedule,) = self.caller["on"]["schedule"]
        hours = runs_per_day(schedule["cron"])
        self.assertEqual(2, len(hours))
        self.assertEqual(12, hours[1] - hours[0])

    def test_the_caller_is_one_job_that_calls_the_shared_workflow_at_v1_and_runs_no_step(self) -> None:
        self.assertEqual({}, self.caller["permissions"])
        self.assertEqual(["sync"], list(self.caller["jobs"]))
        job = self.caller["jobs"]["sync"]
        self.assertEqual("CrankyAnt/SharedWorkflows/.github/workflows/sync.yml@v1", job["uses"])
        self.assertEqual({"issues": "write"}, job["permissions"])
        self.assertEqual({SECRET: f"${{{{ secrets.{SECRET} }}}}"}, job["secrets"], "the key is passed under its own name and nothing is inherited")
        self.assertNotIn("steps", job)
        self.assertNotIn("environment", job, "a job that calls a workflow states no environment; the shared job does")
        self.assertNotRegex(self.text, r"(?m)^\s*(?:- )?run:")

    def test_the_caller_holds_no_placeholder_no_repository_of_a_consumer_and_no_pull_request_target(self) -> None:
        self.assertEqual([], NOT_FIXED.findall(self.text))
        self.assertEqual(["CrankyAnt/SharedWorkflows"], re.findall(r"[A-Za-z0-9-]+/[A-Za-z0-9-]+(?=/\.github)", self.text))
        self.assertNotIn("pull_request", self.text)
        self.assertNotIn("with:", self.text)

    def test_the_caller_grants_what_the_shared_job_holds_and_passes_the_secret_it_declares(self) -> None:
        _, shared = read_yaml(SHARED_WORKFLOW)
        self.assertEqual(shared["jobs"]["sync"]["permissions"], self.caller["jobs"]["sync"]["permissions"])
        self.assertEqual(list(shared["on"]["workflow_call"]["secrets"]), list(self.caller["jobs"]["sync"]["secrets"]))
        self.assertNotIn("inputs", shared["on"]["workflow_call"])


class InstructionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (KIT / "AGENTS.template.md").read_text(encoding="utf-8")

    def test_the_instructions_are_short_and_say_what_the_repository_holds_and_must_not_gain(self) -> None:
        self.assertLessEqual(len(self.text.splitlines()), 12)
        for phrase in ("`.github/workflows/sync.yml`", "environment `sync`", "Add no workflow", "Keep the repository private", "private key",
                       "`actions/sync/consumer-kit/`"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.text)


class ReadmeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.readme = (KIT / "README.md").read_text(encoding="utf-8")

    def test_the_readme_states_the_app_s_permissions(self) -> None:
        app = section(self.readme, "The sync App")
        rows = dict(re.findall(r"(?m)^\| (Contents|Pull requests|Workflows|Issues|Metadata) \| (read|write) \|", app))
        self.assertEqual({"Contents": "write", "Pull requests": "write", "Workflows": "write", "Issues": "read", "Metadata": "read"}, rows)
        self.assertEqual(5, len(re.findall(r"(?m)^\| [A-Z][a-z ]+ \| (?:read|write) \|", app)), "the table grants nothing else")
        self.assertIn("The App holds no Administration permission", app)
        self.assertIn("Never install it on `CrankyAnt/SharedWorkflows`", app)

    def test_the_readme_sets_up_the_environment_limited_to_main_with_the_names_the_shared_workflow_reads(self) -> None:
        environment = section(self.readme, "The environment")
        shared = SHARED_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("Create the environment `sync`", environment)
        self.assertIn("add `main` as the only branch", environment)
        for name, reference in (("SYNC_APP_CLIENT_ID", "vars.SYNC_APP_CLIENT_ID"), (SECRET, f"secrets.{SECRET}")):
            with self.subTest(name=name):
                self.assertIn(f"`{name}`", environment)
                self.assertIn(reference, shared)
        self.assertIn("environment: sync", shared)
        self.assertIn("no repository secret", environment)

    def test_the_readme_names_the_install_target_of_each_template_and_the_caller_s_schedule(self) -> None:
        for row in ("| [sync.yml](sync.yml) | `.github/workflows/sync.yml` |", "| [AGENTS.template.md](AGENTS.template.md) | `AGENTS.md` |"):
            with self.subTest(row=row):
                self.assertIn(row, self.readme)
        _, caller = read_yaml(KIT / "sync.yml")
        minute, hours = caller["on"]["schedule"][0]["cron"].split()[:2]
        for hour in hours.split(","):
            self.assertIn(f"{int(hour):02}:{int(minute):02}", section(self.readme, "The caller"))
        self.assertIn("every twelve hours", self.readme)

    def test_the_readme_names_each_issue_that_the_sync_opens_by_its_title(self) -> None:
        program = (REPOSITORY_ROOT / "actions" / "sync" / "sync.py").read_text(encoding="utf-8")
        issues = section(self.readme, "The issues")
        for title in ("Sync pull request not merged after six hours", "Request for shared CI", "Settings differ", "The sync failed"):
            with self.subTest(title=title):
                self.assertIn(f"| {title} |", issues)
                self.assertIn(f'"{title}', program)

    def test_the_readme_names_the_commands_of_the_local_mode_as_the_program_reads_them(self) -> None:
        import sys
        sys.path.insert(0, str(REPOSITORY_ROOT / "actions" / "sync"))
        import sync as sync_program
        commands = re.findall(r"(?m)^GH_TOKEN=\"\$\(gh auth token\)\" python3 actions/sync/sync\.py (.+)$", section(self.readme, "The local mode"))
        self.assertEqual(2, len(commands))
        parsed = [sync_program.parser().parse_args(command.split()) for command in commands]
        self.assertEqual([("sync", True), ("adopt", False)], [(options.command, bool(getattr(options, "local", False))) for options in parsed])

    def test_the_readme_names_no_repository_but_the_one_of_the_shared_parts(self) -> None:
        self.assertEqual({"CrankyAnt/SharedWorkflows"}, set(re.findall(r"\bCrankyAnt/[\w-]+", self.readme)))
        self.assertNotRegex(self.readme, r"github\.com/")


if __name__ == "__main__":
    unittest.main()
