"""Text contract of the shared workflow of the sync: it is only called, takes its repositories from the App's
installation and names none, pins its actions to full commits, reads the key only in an environment on main, and gives
the App's token to the sync program alone. Beside it, the sync caller that the management repository runs, held against
that workflow; the sync App's permissions, its installation and the environment's names as the maintenance document
and the pipeline's consumer kit state them; and the settings that the sync keeps as the consumer kits state them: the
merge settings and the one required check in the hygiene kit, and no document that sends a difference of either to a
repository's recorded choices."""

from __future__ import annotations

import json
import re
import sys
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, ci_definition, read_workflow, tracked_files, uses_values, workflow_triggers
from tests.workflow_commands import script_invocations

WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "sync.yml"
PART = "actions/sync"
CALLER = f"{PART}/caller.yml"
MAINTAINING = REPOSITORY_ROOT / "docs" / "maintaining.md"
CATALOGUE = REPOSITORY_ROOT / "catalogue.json"
# What only this repository has, and what a template that a repository fills in would hold.
NOT_FIXED = re.compile(r"\$/|<[a-z-]+>|@[0-9a-f]{40}")
SECRET = "SYNC_APP_PRIVATE_KEY"
TOKEN_ACTION = "actions/create-github-app-token"
TOKEN_PERMISSIONS = ("permission-contents", "permission-issues", "permission-pull-requests", "permission-workflows")
SHARED = "CrankyAnt/SharedWorkflows"
# The ways a file names a repository: one of the owner by its name, one in an address of the API or of GitHub, and one
# in a `uses:` value. A placeholder such as <owner>/<repository> or $GITHUB_REPOSITORY names none.
OWNED = re.compile(r"\bCrankyAnt/[\w-]+(?:\.[\w-]+)*")
ADDRESSED = re.compile(r"(?:\brepos/|github\.com/)([A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*)")
USED = re.compile(r"(?m)^\s*(?:- )?uses:\s*([A-Za-z0-9][\w.-]*/[A-Za-z0-9][\w.-]*)")
# The settings that the sync keeps in every consumer, and the documents that state them.
PIPELINE_KIT = REPOSITORY_ROOT / "actions" / "pipeline" / "consumer-kit"
CI_GUIDE = "docs/agents/ci.md"
MERGE_SETTINGS = REPOSITORY_ROOT / PART / "merge-settings.json"
RULESET_TEMPLATE = REPOSITORY_ROOT / "actions" / "repository-ruleset" / "ruleset-main-template.json"
LISTED = re.compile(r"(?m)^\s*repositor(?:y|ies):\s*(\S.*)$")

sys.path.insert(0, str(REPOSITORY_ROOT / PART))
import sync as sync_program  # noqa: E402


def sync_problems(text: str, document: dict) -> list[str]:
    """Each way the shared workflow of the sync breaks its rules."""
    problems = []
    if workflow_triggers(text, "sync.yml") != {"workflow_call"}:
        problems.append("the workflow starts on another event than workflow_call")
    if document.get("permissions") != "{}":
        problems.append("the workflow does not set permissions: {} at the top")
    jobs = document.get("jobs") or {}
    if list(jobs) != ["sync"]:
        problems.append("the workflow does not hold the one job sync")
    for name, job in jobs.items():
        steps = job.get("steps") or []
        reads_key = SECRET in str(job)
        if reads_key and job.get("environment") != "sync":
            problems.append(f"the job {name} reads the key outside the environment sync")
        if reads_key and "github.ref == 'refs/heads/main'" not in str(job.get("if", "")):
            problems.append(f"the job {name} reads the key on another ref than main")
        if job.get("permissions") != {"issues": "write"}:
            problems.append(f"the job {name} holds another permission than issues: write")
        for step in steps:
            uses = str(step.get("uses", ""))
            inputs = step.get("with") or {}
            if uses and not uses.startswith("$/") and not re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", uses):
                problems.append(f"the job {name} uses {uses} without a full commit pin")
            if uses.startswith(TOKEN_ACTION + "@"):
                if "repositories" in inputs:
                    problems.append("the token step names repositories")
                if inputs.get("owner") != "${{ github.repository_owner }}":
                    problems.append("the token step does not take the installation of the repository's owner")
                if any(name.startswith("permission-") and name not in TOKEN_PERMISSIONS for name in inputs) or not all(name in inputs for name in TOKEN_PERMISSIONS):
                    problems.append("the token step does not ask for exactly the permissions of the sync App")
            elif SECRET in str(step):
                problems.append(f"a step of the job {name} other than the token step reads the key")
            if "steps.app-token.outputs.token" in str(step) and "sync.py\" sync " not in str(step.get("run", "")):
                problems.append(f"a step of the job {name} other than the sync gets the App's token")
    return problems


def named_repositories(text: str) -> set[str]:
    """The repositories that a file names, apart from the actions of GitHub that it uses."""
    names = {*OWNED.findall(text), *ADDRESSED.findall(text), *USED.findall(text), *LISTED.findall(text)}
    return {name for name in names if not name.startswith("actions/")}


class SyncWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.document = read_workflow(WORKFLOW)
        self.job = self.document["jobs"]["sync"]

    def test_the_workflow_follows_its_rules(self) -> None:
        self.assertEqual([], sync_problems(self.text, self.document))

    def test_the_key_is_a_secret_of_the_call_that_only_the_token_step_reads_in_the_environment_sync_on_main(self) -> None:
        self.assertEqual([SECRET], list(self.document["on"]["workflow_call"]["secrets"]))
        self.assertEqual("sync", self.job["environment"])
        self.assertEqual("${{ github.ref == 'refs/heads/main' }}", self.job["if"])
        self.assertEqual(1, self.text.count(f"secrets.{SECRET}"))
        token = next(step for step in self.job["steps"] if str(step.get("uses", "")).startswith(TOKEN_ACTION))
        self.assertEqual({"client-id": "${{ vars.SYNC_APP_CLIENT_ID }}", "private-key": f"${{{{ secrets.{SECRET} }}}}", "owner": "${{ github.repository_owner }}",
                          "permission-contents": "write", "permission-issues": "read", "permission-pull-requests": "write", "permission-workflows": "write"},
                         token["with"], "without repositories the token reaches every repository of the installation, with the App's permissions and no other")

    def test_every_action_is_pinned_to_a_full_commit_with_a_version_comment(self) -> None:
        outside = [value for value in uses_values(self.text, "sync.yml") if not value.startswith("$/")]
        self.assertEqual([TOKEN_ACTION], [value.split("@")[0] for value in outside])
        for line in self.text.splitlines():
            if "uses:" in line and "$/" not in line:
                with self.subTest(line=line.strip()):
                    self.assertRegex(line, r"uses: actions/[a-z-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$")

    def test_no_file_of_the_sync_names_a_repository_but_the_one_of_the_shared_parts(self) -> None:
        self.assertEqual(set(), named_repositories(self.text))
        files = sorted(tracked_files(REPOSITORY_ROOT, [PART]))
        self.assertIn(CALLER, files)
        self.assertEqual([], [path for path in files if "consumer-kit" in path], "the sync has no consumer kit: no instructions template and no kit README")
        for path in files:
            with self.subTest(file=path):
                self.assertLessEqual(named_repositories((REPOSITORY_ROOT / path).read_text(encoding="utf-8")), {SHARED})

    def test_the_sync_takes_its_repositories_from_the_installation(self) -> None:
        (arguments,) = script_invocations(WORKFLOW, "sync.py")[:1]
        options = sync_program.parser().parse_args(arguments)
        self.assertEqual("sync", options.command)
        self.assertFalse(options.local)
        self.assertIsNone(options.repository)
        self.assertTrue(options.own_repository)

    def test_only_a_successful_run_closes_its_failure_issue_with_the_token_of_the_run(self) -> None:
        success = self.job["steps"][-2]
        self.assertEqual("${{ success() }}", success["if"])
        self.assertEqual("${{ github.token }}", success["env"]["GITHUB_TOKEN"])
        self.assertNotIn("GH_TOKEN", success["env"])
        options = sync_program.parser().parse_args(script_invocations(WORKFLOW, "sync.py")[-2])
        self.assertEqual("success", options.command)
        self.assertTrue(options.own_repository)

    def test_a_failed_run_opens_its_issue_with_the_token_of_the_run(self) -> None:
        failure = self.job["steps"][-1]
        self.assertEqual("${{ failure() }}", failure["if"])
        self.assertEqual("${{ github.token }}", failure["env"]["GITHUB_TOKEN"])
        self.assertNotIn("GH_TOKEN", failure["env"])
        options = sync_program.parser().parse_args(script_invocations(WORKFLOW, "sync.py")[-1])
        self.assertEqual("failure", options.command)

    def test_the_program_loads_from_the_workflow_s_own_revision_and_one_run_at_a_time(self) -> None:
        self.assertEqual("$/actions/sync", self.job["steps"][0]["uses"])
        self.assertEqual({"group": "sync", "cancel-in-progress": "false"}, self.document["concurrency"])
        self.assertEqual("ubuntu-slim", self.job["runs-on"])

    def test_a_workflow_that_breaks_a_rule_is_named(self) -> None:
        cases = (
            ("    environment: sync\n", "", ["the job sync reads the key outside the environment sync"]),
            ("    if: ${{ github.ref == 'refs/heads/main' }}\n", "", ["the job sync reads the key on another ref than main"]),
            ("      issues: write\n", "      issues: write\n      contents: write\n", ["the job sync holds another permission than issues: write"]),
            ("          permission-contents: write\n", "          permission-contents: write\n          repositories: one-repository\n",
             ["the token step names repositories"]),
            ("          permission-contents: write\n", "          permission-contents: write\n          permission-administration: write\n",
             ["the token step does not ask for exactly the permissions of the sync App"]),
            ("@64ab729a6294000f5aa4a4bc0b73d1f32bc45510 # v3.3.0", "@v3", [f"the job sync uses {TOKEN_ACTION}@v3 without a full commit pin"]),
            ("  workflow_call:\n", "  workflow_dispatch:\n  workflow_call:\n", ["the workflow starts on another event than workflow_call"]),
            ("          GITHUB_TOKEN: ${{ github.token }}\n          RESOURCE_PATH: ${{ steps.resources.outputs.path }}\n        run: PYTHONPYCACHEPREFIX=\"$RUNNER_TEMP/repo-pycache\" python3 \"$RESOURCE_PATH/sync.py\" failure",
             "          GITHUB_TOKEN: ${{ steps.app-token.outputs.token }}\n          RESOURCE_PATH: ${{ steps.resources.outputs.path }}\n        run: PYTHONPYCACHEPREFIX=\"$RUNNER_TEMP/repo-pycache\" python3 \"$RESOURCE_PATH/sync.py\" failure",
             ["a step of the job sync other than the sync gets the App's token"]),
        )
        for old, new, problems in cases:
            with self.subTest(problems=problems):
                self.assertIn(old, self.text)
                changed = self.text.replace(old, new, 1)
                self.assertEqual(problems, sync_problems(changed, ci_definition().read_yaml(changed, "a changed sync.yml")))

    def test_a_named_repository_is_found(self) -> None:
        for line in ("        run: gh api repos/example-owner/consumer/pulls\n", "          repositories: example-owner/consumer\n",
                     "    uses: example-owner/consumer/.github/workflows/ci.yml@v1\n", "See https://github.com/example-owner/consumer for it.\n"):
            with self.subTest(line=line):
                self.assertEqual({"example-owner/consumer"}, named_repositories(line))
        self.assertEqual({"CrankyAnt/Invented"}, named_repositories("The repository CrankyAnt/Invented.\n"))
        self.assertEqual(set(), named_repositories("      - uses: actions/checkout@" + "a" * 40 + " # v1.0.0\n        run: gh api \"repos/$GITHUB_REPOSITORY/issues\"\n"))


def section(text: str, heading: str) -> str:
    """The text below a heading of the README, up to the next heading of the same or a higher level."""
    level = heading.split(" ", 1)[0]
    start = text.index(f"\n{heading}\n") + len(heading) + 2
    following = re.search(rf"(?m)^#{{1,{len(level)}}} ", text[start:])
    return text[start:start + following.start()] if following else text[start:]


def runs_per_day(cron: str) -> list[int]:
    """The hours at which a cron line of a fixed minute on every day starts."""
    minute, hours, day, month, weekday = cron.split()
    assert minute.isdigit() and (day, month, weekday) == ("*", "*", "*"), cron
    return sorted(int(hour) for hour in hours.split(","))


class CallerTests(unittest.TestCase):
    """The sync caller that the management repository holds as ``.github/workflows/sync.yml``."""

    def setUp(self) -> None:
        self.text, self.caller = read_workflow(REPOSITORY_ROOT / CALLER)

    def test_the_caller_starts_every_twelve_hours_and_by_hand_and_on_nothing_else(self) -> None:
        self.assertEqual({"schedule", "workflow_dispatch"}, set(self.caller["on"]))
        self.assertIsNone(self.caller["on"]["workflow_dispatch"])
        (schedule,) = self.caller["on"]["schedule"]
        hours = runs_per_day(schedule["cron"])
        self.assertEqual(2, len(hours))
        self.assertEqual(12, hours[1] - hours[0])

    def test_the_caller_is_one_job_that_calls_the_shared_workflow_at_v1_and_runs_no_step(self) -> None:
        self.assertEqual("{}", self.caller["permissions"])
        self.assertEqual(["sync"], list(self.caller["jobs"]))
        job = self.caller["jobs"]["sync"]
        self.assertEqual(f"{SHARED}/.github/workflows/sync.yml@v1", job["uses"])
        self.assertEqual({"issues": "write"}, job["permissions"])
        self.assertEqual({SECRET: f"${{{{ secrets.{SECRET} }}}}"}, job["secrets"], "the key is passed under its own name and nothing is inherited")
        self.assertNotIn("steps", job)
        self.assertNotIn("environment", job, "a job that calls a workflow states no environment; the shared job does")
        self.assertNotRegex(self.text, r"(?m)^\s*(?:- )?run:")

    def test_the_caller_holds_no_placeholder_no_repository_of_a_consumer_and_no_pull_request_target(self) -> None:
        self.assertEqual([], NOT_FIXED.findall(self.text))
        self.assertEqual({SHARED}, named_repositories(self.text))
        self.assertNotIn("pull_request", self.text)
        self.assertNotIn("with:", self.text)

    def test_the_caller_grants_what_the_shared_job_holds_and_passes_the_secret_it_declares(self) -> None:
        _, shared = read_workflow(WORKFLOW)
        self.assertEqual(shared["jobs"]["sync"]["permissions"], self.caller["jobs"]["sync"]["permissions"])
        self.assertEqual(list(shared["on"]["workflow_call"]["secrets"]), list(self.caller["jobs"]["sync"]["secrets"]))
        self.assertNotIn("inputs", shared["on"]["workflow_call"])

    def test_the_caller_lies_outside_a_consumer_kit_in_the_template_class_and_the_catalogue_names_no_kit(self) -> None:
        surface = json.loads((REPOSITORY_ROOT / "actions" / "repository-hygiene" / "shared-surface.json").read_text(encoding="utf-8"))
        holders = [path for path in surface["classes"] if CALLER == path or CALLER.startswith(path + "/")]
        self.assertEqual("template", surface["classes"][max(holders, key=len)])
        catalogue = json.loads(CATALOGUE.read_text(encoding="utf-8"))
        (entry,) = [item for item in catalogue["parts"] if ".github/workflows/sync.yml" in item["workflows"]]
        self.assertIsNone(entry["configuration"])
        self.assertIsNone(entry["consumer_kit"])
        self.assertEqual("management repository", entry["audience"])
        self.assertIn(CALLER, entry["needs"])
        self.assertNotIn("kit", " ".join(str(value) for value in entry.values()))
        about = catalogue["about"]
        self.assertIn("configuration is the schema fragment", about)
        self.assertIn("or null for an entry that a repository adopts through its consumer kit and not through that key, and for an entry that no consumer adopts;", about)
        self.assertIn("or null when the entry and its fragment say everything, and for an entry that no consumer adopts, which has neither;", about)
        self.assertIn("management repository for an entry that no consumer adopts, that has no configuration and no consumer kit, and that an agent in a consumer skips", about)


class SyncAppDocumentTests(unittest.TestCase):
    """The maintenance document's sections on the sync App, against the shared workflow that uses the App."""

    def setUp(self) -> None:
        self.document = MAINTAINING.read_text(encoding="utf-8")
        self.app = section(self.document, "### Sync App")
        self.environment = section(self.document, "### Set up the sync environment")
        self.rotation = section(self.document, "### Rotate the sync App's private key")
        self.workflow, self.shared = read_workflow(WORKFLOW)

    def test_the_reference_states_the_app_s_permissions_as_the_token_step_asks_for_them(self) -> None:
        rows = dict(re.findall(r"(?m)^\| (Contents|Pull requests|Workflows|Issues|Metadata) \| (read|write) \|", self.app))
        self.assertEqual({"Contents": "write", "Pull requests": "write", "Workflows": "write", "Issues": "read", "Metadata": "read"}, rows)
        self.assertEqual(5, len(re.findall(r"(?m)^\| [A-Z][a-z ]+ \| (?:read|write) \|", self.app)), "the table grants nothing else")
        token = next(step for step in self.shared["jobs"]["sync"]["steps"] if str(step.get("uses", "")).startswith(TOKEN_ACTION))
        asked = {name.removeprefix("permission-").replace("-", " ").capitalize(): level for name, level in token["with"].items() if name.startswith("permission-")}
        self.assertEqual({name: level for name, level in rows.items() if name != "Metadata"}, asked, "GitHub adds the metadata read by itself")
        self.assertIn("The App holds no Administration permission", self.app)

    def test_the_reference_states_where_the_app_is_installed_and_where_never(self) -> None:
        for text in ("The App is installed on selected repositories only", "only the owner adds a repository to the installation",
                     "A consumer outside the installation receives no sync updates", f"Never install it on `{SHARED}`",
                     "The App is not installed on the management repository either",
                     "no App installation and no key reaches the management repository"):
            with self.subTest(text=text):
                self.assertIn(text, self.app)

    def test_the_environment_is_limited_to_main_and_holds_the_names_that_the_shared_workflow_reads(self) -> None:
        self.assertIn("Create the environment `sync`", self.environment)
        self.assertIn("add `main` as the only branch", self.environment)
        self.assertIn("no repository secret", self.environment)
        self.assertEqual("sync", self.shared["jobs"]["sync"]["environment"])
        for name, reference in (("SYNC_APP_CLIENT_ID", "vars.SYNC_APP_CLIENT_ID"), (SECRET, f"secrets.{SECRET}")):
            for place, text in (("the procedure", self.environment), ("the reference", self.app)):
                with self.subTest(name=name, place=place):
                    self.assertIn(f"`{name}`", text)
            self.assertIn(reference, self.workflow)
        self.assertIn("the environment `sync`, which allows `main` as its only branch", self.app)

    def test_the_rotation_is_a_procedure_of_its_own_that_names_the_sync_app_s_secret(self) -> None:
        self.assertIn(f"gh secret set {SECRET} --env sync --repo <management repository>", self.rotation)
        self.assertNotIn("PUBLICATION_CHECKS_APP_PRIVATE_KEY", self.rotation.replace("whose key is `PUBLICATION_CHECKS_APP_PRIVATE_KEY`", ""))
        other = section(self.document, "### Rotate the App's private key")
        self.assertNotIn(SECRET, other, "the other App's procedure does not rotate the sync App's key")
        tasks = section(self.document, "### Recurring tasks")
        self.assertIn("- The sync App: [Set up the sync environment](#set-up-the-sync-environment) and "
                      "[Rotate the sync App's private key](#rotate-the-sync-apps-private-key).", tasks)

    def test_the_promotion_procedure_states_the_scope_and_which_credential_gh_takes(self) -> None:
        promotion = section(self.document, "#### Promote v1 by hand")
        for text in ("needs a token with the `workflow` scope beside `repo`", "`gh` prefers a token in the environment, `GH_TOKEN` and then `GITHUB_TOKEN`, over its stored login",
                     "unset both, or make sure the token is the owner's and holds both scopes"):
            with self.subTest(text=text):
                self.assertIn(text, promotion)

    def test_the_procedures_state_what_a_delivery_reports_and_that_the_sync_has_no_consumer_kit(self) -> None:
        compares = section(self.document, "### Change what the sync compares")
        program = (REPOSITORY_ROOT / PART / "sync.py").read_text(encoding="utf-8")
        self.assertIn("the delivery report names each changed title, finding and name", compares)
        self.assertIn("instructions outside this repository", compares)
        for name in ("`sync`", "`SYNC_APP_CLIENT_ID`", f"`{SECRET}`"):
            self.assertIn(name, compares)
        self.assertNotIn("by hand after the promotion", compares)
        self.assertIn("no file is copied there by hand", compares)
        self.assertIn("The sync has no consumer kit", section(self.document, "### Add a part"))
        issues = section(self.document, "#### Issues in the management repository")
        self.assertEqual(["| Finding | Issue title | One issue |"], [line for line in issues.splitlines() if line.startswith("| Finding")])
        self.assertNotIn("What it asks for", self.document)
        for title in ("Sync pull request not merged after six hours", "Request for shared CI", "Settings differ", "Unwanted workflows", "The sync failed"):
            with self.subTest(title=title):
                self.assertIn(f"`{title}", issues)
                self.assertIn(f'"{title}', program)

    def test_the_documents_link_to_what_is_kept_and_to_no_file_of_the_removed_kit(self) -> None:
        readme = (REPOSITORY_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("- [Sync](docs/maintaining.md#sync):", readme)
        self.assertIn("[caller.yml](../actions/sync/caller.yml)", section(self.document, "#### The sync caller"))
        for name, text in (("README.md", readme), ("docs/maintaining.md", self.document), ("tests/README.md", (REPOSITORY_ROOT / "tests" / "README.md").read_text(encoding="utf-8"))):
            with self.subTest(document=name):
                self.assertNotIn("sync/consumer-kit", text)
                self.assertNotIn("AGENTS.template.md", text)
                self.assertNotIn("management repository kit", text)

    def test_the_pipeline_kit_states_the_consumer_s_side_of_the_sync_app(self) -> None:
        app = section((PIPELINE_KIT / "README.md").read_text(encoding="utf-8"), "## The sync App")
        self.assertIn("Only the owner adds a repository to the sync App.", app)
        self.assertIn("A repository outside the installation receives no sync updates", app)
        self.assertEqual(set(), named_repositories(app))
        self.assertNotIn("maintaining.md", app, "a consumer reads the kit at v1, where it has all it needs")


def as_text(value: object) -> str:
    return json.dumps(value) if isinstance(value, bool) else str(value)


def merge_settings_problems(text: str, settings: dict) -> list[str]:
    """Each field of the merge settings that the kit's list, its PATCH command or its read-back command does not state
    with the value of ``settings``, and each field they name that ``settings`` lacks; every problem names its field."""
    expected = {name: as_text(value) for name, value in settings.items()}
    listed = dict(re.findall(r"(?m)^- `([a-z_]+)` is `([^`]+)`", text))
    patch = next((line for line in text.splitlines() if line.startswith("gh api -X PATCH ")), "")
    passed = dict(re.findall(r" -[Ff] ([a-z_]+)=(\S+)", patch))
    read_back = next((line for line in text.splitlines() if line.startswith("gh api repos/") and "--jq" in line), "")
    braces = re.search(r"--jq '\{([^}]*)\}'", read_back)
    read = {name.strip(): None for name in braces.group(1).split(",")} if braces else {}
    problems = []
    for place, stated in (("the list", listed), ("the PATCH command", passed), ("the read-back command", read)):
        for name in sorted(set(expected) | set(stated)):
            if name not in stated:
                problems.append(f"{name}: {place} does not name it")
            elif name not in expected:
                problems.append(f"{name}: {place} names it, but merge-settings.json has no such field")
            elif stated[name] is not None and stated[name] != expected[name]:
                problems.append(f"{name}: {place} gives {stated[name]}, merge-settings.json {expected[name]}")
    return problems


def required_check_problems(text: str, ruleset: dict) -> list[str]:
    """Whether the ruleset section names the one check that the sync keeps as the required check of a repository that
    holds the fixed callers."""
    rule = next(rule for rule in ruleset["rules"] if rule["type"] == "required_status_checks")
    kept = [check["context"] for check in rule["parameters"]["required_status_checks"]]
    return [f"the section does not name `{check}`, the one required check that the sync keeps" for check in kept if f"`{check}`" not in text]


def recorded_deviations(text: str) -> list[str]:
    """Each sentence that names a deviation of the ruleset or a merge setting as a choice to record: a sentence about
    recording a choice, or about ``## Repository choices``, that names a deviation, the ruleset or a merge setting."""
    sentences = re.split(r"(?<=[.;])\s+", re.sub(r"\s*\n\s*", " ", text))
    return [sentence for sentence in sentences
            if re.search(r"\brecord|Repository choices", sentence) and re.search(r"deviation|ruleset|merge setting", sentence)]


class RepositorySettingsTest(unittest.TestCase):
    """``## Repository settings`` of the kit's README states the merge settings and the required check that the sync
    keeps equal in every consumer."""

    def setUp(self) -> None:
        self.readme = (PIPELINE_KIT / "README.md").read_text(encoding="utf-8")
        self.merge = section(self.readme, "### Merge settings")
        self.settings = json.loads(MERGE_SETTINGS.read_text(encoding="utf-8"))
        self.ruleset_section = section(self.readme, "### Default-branch ruleset")

    def test_the_list_the_patch_command_and_the_read_back_command_state_every_field_of_the_sync_s_merge_settings(self) -> None:
        self.assertEqual(9, len(self.settings))
        self.assertIs(True, self.settings["allow_auto_merge"])
        self.assertIs(True, self.settings["delete_branch_on_merge"])
        self.assertEqual([], merge_settings_problems(self.merge, self.settings))

    def test_a_field_or_a_value_that_the_kit_misses_or_a_field_the_file_lacks_fails_and_names_the_field(self) -> None:
        patch = next(line for line in self.merge.splitlines() if line.startswith("gh api -X PATCH "))
        read_back = next(line for line in self.merge.splitlines() if line.startswith("gh api repos/"))
        for changed, problem in (
            (re.sub(r"(?m)^- `allow_auto_merge` is .*\n", "", self.merge), "allow_auto_merge: the list does not name it"),
            (self.merge.replace("- `allow_auto_merge` is `true`", "- `allow_auto_merge` is `false`"), "allow_auto_merge: the list gives false, merge-settings.json true"),
            (self.merge.replace(patch, patch.replace(" -F allow_auto_merge=true", "")), "allow_auto_merge: the PATCH command does not name it"),
            (self.merge.replace(patch, patch.replace("-f merge_commit_message=BLANK", "-f merge_commit_message=PR_BODY")), "merge_commit_message: the PATCH command gives PR_BODY, merge-settings.json BLANK"),
            (self.merge.replace(read_back, read_back.replace(", allow_auto_merge", "")), "allow_auto_merge: the read-back command does not name it"),
            (re.sub(r"(?m)^- `delete_branch_on_merge` is .*\n", "", self.merge), "delete_branch_on_merge: the list does not name it"),
            (self.merge.replace("- `delete_branch_on_merge` is `true`", "- `delete_branch_on_merge` is `false`"), "delete_branch_on_merge: the list gives false, merge-settings.json true"),
            (self.merge.replace(patch, patch.replace(" -F delete_branch_on_merge=true", "")), "delete_branch_on_merge: the PATCH command does not name it"),
            (self.merge.replace(patch, patch.replace("delete_branch_on_merge=true", "delete_branch_on_merge=false")), "delete_branch_on_merge: the PATCH command gives false, merge-settings.json true"),
            (self.merge.replace(read_back, read_back.replace(", delete_branch_on_merge", "")), "delete_branch_on_merge: the read-back command does not name it"),
            (self.merge.replace(patch, patch + " -F unexpected_setting=true"), "unexpected_setting: the PATCH command names it, but merge-settings.json has no such field"),
        ):
            with self.subTest(problem=problem):
                self.assertIn(problem, merge_settings_problems(changed, self.settings))
        without_field = {name: value for name, value in self.settings.items() if name != "allow_rebase_merge"}
        self.assertIn("allow_rebase_merge: the list names it, but merge-settings.json has no such field", merge_settings_problems(self.merge, without_field))

    def test_the_bullet_for_auto_merge_says_which_pull_requests_merge_themselves(self) -> None:
        bullet = next(line for line in self.merge.splitlines() if line.startswith("- `allow_auto_merge`"))
        self.assertIn("sync pull request", bullet)
        self.assertIn("full recording", bullet)

    def test_the_deletion_bullet_explains_which_branches_are_deleted(self) -> None:
        bullet = next(line for line in self.merge.splitlines() if line.startswith("- `delete_branch_on_merge`"))
        self.assertIn("automatically deletes the pull request's head branch after merge", bullet)

    def test_the_ruleset_section_names_the_check_that_the_sync_keeps_as_the_one_required_check(self) -> None:
        expected = sync_program.expected_ruleset(RULESET_TEMPLATE)
        self.assertEqual([], required_check_problems(self.ruleset_section, expected))
        self.assertEqual(["the section does not name `ci / verdict`, the one required check that the sync keeps"],
                         required_check_problems(self.ruleset_section.replace("`ci / verdict`", "the verdict"), expected))
        self.assertIn("one required check", self.ruleset_section)
        self.assertIn("settings difference", self.ruleset_section)

    def test_the_checks_of_the_earlier_callers_are_no_longer_named(self) -> None:
        for check in ("`hygiene / hygiene`", "`validation / validation`", "`tests / tests`"):
            with self.subTest(check=check):
                self.assertNotIn(check, self.ruleset_section)

    def test_a_merge_setting_or_ruleset_that_differs_is_no_choice_of_the_repository(self) -> None:
        settings = section(self.readme, "## Repository settings")
        self.assertNotIn("deviation", settings)
        self.assertNotIn("Repository choices", settings)
        self.assertIn("a merge setting that differs from [merge-settings.json](../../sync/merge-settings.json)", settings)
        self.assertIn("is not kept. The sync reports it on every run, and its local mode replaces it with the value of that file", settings)

class KeptSettingsTests(unittest.TestCase):
    """No document names a deviation of the ruleset or a merge setting as a choice of a repository to record: the sync
    keeps both equal to their templates."""

    def test_no_document_names_a_deviation_of_the_ruleset_or_a_merge_setting_as_a_choice_to_record(self) -> None:
        documents = {"the CI guide template": (PIPELINE_KIT / CI_GUIDE).read_text(encoding="utf-8"),
                     "the pipeline kit's README": (PIPELINE_KIT / "README.md").read_text(encoding="utf-8"),
                     "the ruleset README": (REPOSITORY_ROOT / "actions" / "repository-ruleset" / "README.md").read_text(encoding="utf-8")}
        for name, text in documents.items():
            with self.subTest(document=name):
                self.assertEqual([], recorded_deviations(text))
        ruleset = documents["the ruleset README"]
        self.assertNotIn("Repository choices", ruleset)
        self.assertIn("Any other difference from the template is not kept: the sync reports a difference from the template on every run, "
                      "and its local mode replaces it with the template's value.", ruleset)

    def test_a_document_that_sends_a_deviation_to_the_repository_choices_fails(self) -> None:
        for text in ("Record each deviation of the ruleset below `## Repository choices`.",
                     "A choice that no file states, such as a deviation of its ruleset from the template, is recorded with its reason below the next heading.",
                     "A merge setting that differs is recorded under `## Repository choices` in the repository's `docs/agents/ci.md`."):
            with self.subTest(text=text):
                self.assertEqual([text], recorded_deviations(f"{text}\n"))


if __name__ == "__main__":
    unittest.main()
