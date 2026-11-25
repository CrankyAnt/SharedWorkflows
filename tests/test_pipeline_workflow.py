"""The pipeline's invocations of the plan program and of the hygiene scripts, parsed with the scripts' own parsers, and
the decision of its hygiene job in a consumer, which holds no pipeline of its own."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest

from tests.ci_policy_support import read_workflow
from tests.pipeline_fixtures import CONFIGURATION, REPOSITORY_ROOT, SCHEMAS, PipelineFixture, git, plan_program, read_outputs
from tests.workflow_commands import script_invocations

WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "pipeline.yml"
HYGIENE_FOLDER = REPOSITORY_ROOT / "actions" / "repository-hygiene"

sys.path.insert(0, str(HYGIENE_FOLDER))
import hygiene  # noqa: E402
import install_tools  # noqa: E402

sys.path.insert(0, str(REPOSITORY_ROOT / "actions" / "pipeline"))
import consumer_checks  # noqa: E402


class PipelineInvocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.invocations = script_invocations(WORKFLOW, "plan.py")

    def test_the_pipeline_starts_the_plan_and_then_the_verdict_with_options_that_the_program_accepts(self) -> None:
        self.assertEqual(["plan", "plan", "setting", "setting", "verdict"], [tokens[0] for tokens in self.invocations])
        for tokens in self.invocations:
            with self.subTest(command=tokens[0]):
                if tokens[0] == "plan":
                    for mode in (["--lookup", "x"], ["--base", "x", "--head", "x", "--compare", "direct"], ["--lookup", "x", "--unit-decisions", "x"]):
                        plan_program.parser().parse_args([tokens[0], *mode, *[token for token in tokens[1:] if token != "$@"]])
                else:
                    plan_program.parser().parse_args(tokens)

    def test_the_hygiene_job_reads_the_two_exclusions_of_repository_hygiene_from_the_checkout(self) -> None:
        settings = [plan_program.parser().parse_args(tokens) for tokens in self.invocations if tokens[0] == "setting"]
        self.assertEqual([("repository-hygiene", "coverage-exclude"), ("repository-hygiene", "link-exclude")], [(options.part, options.setting) for options in settings])
        for options in settings:
            self.assertEqual((".", str(plan_program.SCHEMAS)), (options.root, options.schemas), "the setting is read with the schema of the pipeline's own revision")

    def test_the_plan_gets_the_change_and_the_run_and_writes_its_outputs_and_its_summary(self) -> None:
        final_plan = [tokens for tokens in self.invocations if tokens[0] == "plan"][-1]
        options = plan_program.parser().parse_args([final_plan[0], "--base", "x", "--head", "x", "--compare", "direct",
                                                    *[token for token in final_plan[1:] if token != "$@"]])
        self.assertEqual(".", options.root)
        self.assertEqual(str(plan_program.SCHEMAS), options.schemas, "the pipeline reads the schema of its own revision")
        for name in ("base", "head", "workflow", "workflow_ref", "event", "event_action", "pull_request", "github_output", "summary"):
            self.assertEqual("x", getattr(options, name), name)
        text = WORKFLOW.read_text(encoding="utf-8")
        for binding in ("BASE: ${{ github.event_name == 'pull_request' && github.event.pull_request.base.sha || github.event.before }}",
                        "HEAD: ${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}",
                        "COMPARE: ${{ github.event_name == 'pull_request' && 'merge-base' || 'direct' }}",
                        "WORKFLOW: ${{ github.workflow }}", "EVENT: ${{ github.event_name }}",
                        "PULL_REQUEST: ${{ github.event.pull_request.number }}", "RUN_ID: ${{ github.run_id }}",
                        '--github-output "$GITHUB_OUTPUT" --summary "$GITHUB_STEP_SUMMARY"'):
            self.assertIn(binding, text)

    def test_the_verdict_gets_the_needs_context_and_writes_its_summary(self) -> None:
        options = plan_program.parser().parse_args(self.invocations[-1])
        self.assertEqual(("x/needs.json", "x"), (options.needs, options.summary))

    def test_an_edited_pull_request_runs_only_title_and_description_checks(self) -> None:
        _, workflow = read_workflow(WORKFLOW)
        plan_steps = {step["name"]: step for step in workflow["jobs"]["plan"]["steps"] if "name" in step}
        hygiene_steps = {step["name"]: step for step in workflow["jobs"]["repository-hygiene"]["steps"] if "name" in step}
        for name in ("Decide which checks the changed files can affect", "Take the digest of the shared recording step",
                     "Look up the digest of the last passed run on the base branch", "Restore the passed results of the pull request",
                     "Select the suites the changed files touch", "Check the pull request branch", "Consumer checks"):
            self.assertIn("edited", str(plan_steps[name].get("if", "")), f"plan step {name} must skip on edited")
        self.assertNotIn("edited", str(plan_steps["Check the references to repositories of the owner"].get("if", "")))
        for name in ("Check every pull request commit", "Compute the hygiene tools cache key", "Restore the cached hygiene tools",
                     "Install actionlint and ShellCheck", "Install zizmor", "Install REUSE", "Install Gitleaks",
                     "Restore the default-branch Gitleaks scan marker", "Gitleaks over added commits"):
            self.assertIn("edited", str(hygiene_steps[name].get("if", "")), f"hygiene step {name} must skip on edited")
        self.assertNotIn("edited", str(hygiene_steps["Check the pull request title"].get("if", "")))


@unittest.skipUnless(sys.platform != "win32" and shutil.which("bash") and shutil.which("python3"), "the plan step runs in Bash with python3 off Windows")
class PlanStepRunTests(PipelineFixture):
    """The plan step of the pipeline, run in Bash as Actions runs it, on a fixture consumer."""

    CONFIGURED = "version: 1\nparts:\n  custom:\n    script: ci/check.sh\n    paths: [src/]\n"

    def run_step(self, event: str, head: str, base: str = "") -> dict:
        step = next(step for step in read_workflow(WORKFLOW)[1]["jobs"]["plan"]["steps"] if step.get("id") == "plan")
        outputs = self.directory / "plan-output"
        outputs.unlink(missing_ok=True)
        environment = {**os.environ, "RESOURCE_PATH": str(REPOSITORY_ROOT / "actions" / "pipeline"), "BASE": base, "HEAD": head, "COMPARE": "direct",
                       "WORKFLOW": "CI", "EVENT": event, "PULL_REQUEST": "", "RUN_ID": "7", "RUNNER_TEMP": str(self.directory),
                       "GITHUB_OUTPUT": str(outputs), "GITHUB_STEP_SUMMARY": str(self.directory / "plan-summary.md")}
        ran = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]], cwd=self.repository, env=environment, capture_output=True, text=True)
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        return json.loads(read_outputs(outputs)["plan"])

    def test_a_dependabot_merged_run_plans_the_dispatched_commit_against_its_first_parent_as_a_push_would(self) -> None:
        self.commit({CONFIGURATION: self.CONFIGURED, "src/a.py": "a\n", "docs/guide.md": "guide\n"})
        merged = self.commit({"src/a.py": "b\n"})
        later = self.commit({"docs/guide.md": "more\n"})
        dispatched = self.run_step("repository_dispatch", merged)
        self.assertTrue(dispatched["compared"])
        self.assertEqual({"run": True, "reason": "src/a.py changed"}, {key: dispatched["parts"]["custom"][key] for key in ("run", "reason")})
        self.assertEqual(dispatched["parts"], self.run_step("push", merged, base=f"{merged}^")["parts"])
        unaffected = self.run_step("repository_dispatch", later)
        self.assertEqual((False, "no changed path matches its paths"), (unaffected["parts"]["custom"]["run"], unaffected["parts"]["custom"]["reason"]))

    def test_a_dispatched_commit_without_a_parent_runs_every_part(self) -> None:
        root = self.commit({CONFIGURATION: self.CONFIGURED})
        plan = self.run_step("repository_dispatch", root)
        self.assertFalse(plan["compared"])
        self.assertEqual((True, "the changed files are unknown"), (plan["parts"]["custom"]["run"], plan["parts"]["custom"]["reason"]))


class ConsumerCheckInvocationTests(unittest.TestCase):
    def test_the_hygiene_job_starts_the_consumer_checks_as_one_command_with_options_that_the_program_accepts(self) -> None:
        invocations = script_invocations(WORKFLOW, "consumer_checks.py")
        self.assertEqual(["all"], [tokens[0] for tokens in invocations])
        for tokens in invocations:
            with self.subTest(command=tokens[0]):
                options = consumer_checks.parser().parse_args(tokens)
                self.assertEqual(("x", "x", "x", "."), (options.event, options.author, options.summary, options.root))
                self.assertEqual((str(consumer_checks.KIT), str(plan_program.SCHEMAS)), (options.kit, options.schemas),
                                 "the checks read the templates and the schema of the pipeline's own revision")


class HygieneInvocationTests(PipelineFixture):
    def test_every_hygiene_step_of_the_pipeline_names_a_subcommand_and_options_the_script_accepts(self) -> None:
        invocations = script_invocations(WORKFLOW, "hygiene.py")
        self.assertEqual(["changes", "coverage", "links", "actionlint", "zizmor", "reuse", "gitleaks"], [tokens[0] for tokens in invocations])
        parser = hygiene.build_parser()
        for tokens in invocations:
            if tokens[0] in ("changes", "gitleaks"):
                events = (["--lookup", "x"], ["--base", "x", "--head", "x", "--compare", "direct"])
            elif tokens[0] == "reuse":
                events = ([], ["--record-marker", "x", "--github-output", "x"])
            else:
                events = ([],)
            for event in events:
                with self.subTest(step=tokens[0], event=event):
                    parser.parse_args([tokens[0], *event, *[token for token in tokens[1:] if token != "$@"]])
        install = script_invocations(WORKFLOW, "install_tools.py")
        self.assertEqual(5, len(install))
        key_args = install_tools.build_parser().parse_args(install[0])
        self.assertTrue(key_args.print_cache_key)
        self.assertEqual(("linux-x64", ["gitleaks"]), (key_args.platform, key_args.tools))
        for tokens, tools in zip(install[1:], [["actionlint", "shellcheck"], ["zizmor"], ["reuse"], ["gitleaks"]], strict=True):
            arguments = install_tools.build_parser().parse_args(tokens)
            self.assertEqual(("linux-x64", tools), (arguments.platform, arguments.tools))

    def decide(self, base: str, head: str) -> dict[str, str]:
        """The decision of the pipeline's hygiene job with the definitions that its step `changes` names."""
        tokens = next(tokens for tokens in script_invocations(WORKFLOW, "hygiene.py") if tokens[0] == "changes")
        definitions = []
        skip_next = False
        for token in tokens[1:]:
            if skip_next:
                skip_next = False
                continue
            if token in ("--github-output", "--summary", "--pins", "--reuse-marker"):
                skip_next = True
                continue
            if token != "$@":
                definitions.append(token)
        outputs = self.directory / "hygiene-output"
        outputs.unlink(missing_ok=True)
        completed = subprocess.run([sys.executable, str(HYGIENE_FOLDER / "hygiene.py"), "changes", "--root", str(self.repository), "--base", base, "--head", head,
                                    "--compare", "merge-base", *definitions, "--github-output", str(outputs)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        return read_outputs(outputs)

    def test_the_decision_of_the_hygiene_job_runs_in_a_consumer_that_holds_no_pipeline_of_its_own(self) -> None:
        base = self.commit({CONFIGURATION: "version: 1\n", "README.md": "A consumer.\n", ".github/workflows/ci.yml": "name: CI\non: pull_request\njobs: {}\n"})
        head = self.commit({"docs/guide.md": "A guide.\n"})
        decided = self.decide(base, head)
        self.assertEqual(("true", "false", "false"), (decided["markdown"], decided["actionlint"], decided["packages"]))

    def test_a_change_to_one_exclusion_alone_starts_the_check_that_reads_it(self) -> None:
        stated = "version: 1\nparts:\n  custom:\n    script: ci/check.sh\n  repository-hygiene:\n    coverage-exclude: [tests/fixtures]\n    link-exclude: [vendor/]\n"
        base = self.commit({CONFIGURATION: stated, "README.md": "A consumer.\n", ".github/workflows/ci.yml": "name: CI\non: pull_request\njobs: {}\n"})
        for changed, packages, markdown in (
            (stated.replace("[tests/fixtures]", "[tests/fixtures, tools/fixture]"), "true", "false"),
            (stated.replace("[vendor/]", "[vendor/, docs/old/*.md]"), "false", "true"),
            (stated.replace("ci/check.sh", "ci/other.sh"), "false", "false"),
        ):
            with self.subTest(changed=changed):
                head = self.commit({CONFIGURATION: changed})
                decided = self.decide(base, head)
                self.assertEqual((packages, markdown, "false", "false"), (decided["packages"], decided["markdown"], decided["actionlint"], decided["reuse"]))
                git(self.repository, "reset", "--quiet", "--hard", base)


@unittest.skipUnless(sys.platform != "win32" and shutil.which("bash") and shutil.which("python3"), "the hygiene steps run in Bash with python3 off Windows")
class ExclusionStepRunTests(PipelineFixture):
    """The steps `coverage` and `links` of the pipeline's hygiene job, run in Bash as Actions runs them, on a fixture
    consumer whose configuration file states the exclusions of repository hygiene or does not."""

    def setUp(self) -> None:
        super().setUp()
        self.schemas = SCHEMAS

    DEPENDABOT = "version: 2\nupdates:\n  - package-ecosystem: github-actions\n    directory: /\n    schedule:\n      interval: weekly\n"

    def run_step(self, step_id: str, pipeline_path: str = str(REPOSITORY_ROOT / "actions" / "pipeline")) -> subprocess.CompletedProcess:
        step = next(step for step in read_workflow(WORKFLOW)[1]["jobs"]["plan"]["steps"] if step.get("id") == step_id)
        environment = {**os.environ, "RESOURCE_PATH": str(HYGIENE_FOLDER), "PIPELINE_PATH": pipeline_path,
                       "RUNNER_TEMP": str(self.directory), "GITHUB_STEP_SUMMARY": str(self.directory / "step-summary.md")}
        return subprocess.run(["bash", "-e", "-c", step["run"]], cwd=self.repository, env=environment, capture_output=True, text=True, encoding="utf-8")

    def test_coverage_passes_when_the_only_uncovered_package_file_is_under_coverage_exclude_and_fails_without_it(self) -> None:
        files = {".github/dependabot.yml": self.DEPENDABOT, "tests/fixtures/node/package.json": "{}\n", "README.md": "A consumer.\n"}
        for configuration in ("version: 1\nparts:\n  repository-hygiene:\n    coverage-exclude: [tests/fixtures]\n",
                              "version: 1\nparts:\n  repository-hygiene:\n    coverage-exclude:\n      - tests/fixtures/node/package.json\n"):
            with self.subTest(configuration=configuration):
                self.commit({**files, CONFIGURATION: configuration})
                ran = self.run_step("coverage")
                self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
                self.assertIn("Every tracked package file has a covering Dependabot entry.", ran.stdout)
        for configuration in ("version: 1\n", "version: 1\nparts:\n  repository-hygiene: {}\n", "version: 1\nparts:\n  repository-hygiene:\n    coverage-exclude: [tests/other]\n"):
            with self.subTest(configuration=configuration):
                self.commit({CONFIGURATION: configuration})
                ran = self.run_step("coverage")
                self.assertEqual(1, ran.returncode, ran.stdout + ran.stderr)
                self.assertIn("::error file=tests/fixtures/node/package.json::", ran.stdout)

    def test_links_pass_when_the_only_dead_link_is_in_a_file_under_link_exclude_and_fail_without_it(self) -> None:
        files = {"README.md": "A consumer. See [the guide](docs/guide.md).\n", "docs/guide.md": "# Guide\n", "vendor/lib/notes.md": "See [gone](missing.md).\n"}
        for configuration in ("version: 1\nparts:\n  repository-hygiene:\n    link-exclude: [vendor/**/*.md]\n",
                              "version: 1\nparts:\n  repository-hygiene:\n    link-exclude: [vendor/]\n"):
            with self.subTest(configuration=configuration):
                self.commit({**files, CONFIGURATION: configuration})
                ran = self.run_step("links")
                self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
                self.assertIn("1 excluded", (self.directory / "step-summary.md").read_text(encoding="utf-8"))
        for configuration in ("version: 1\n", "version: 1\nparts:\n  repository-hygiene:\n    link-exclude: [docs/*.md]\n"):
            with self.subTest(configuration=configuration):
                self.commit({CONFIGURATION: configuration})
                ran = self.run_step("links")
                self.assertEqual(1, ran.returncode, ran.stdout + ran.stderr)
                self.assertIn("vendor/lib/notes.md:1: the link target missing.md does not exist", ran.stdout)

    def test_a_link_from_a_checked_file_into_an_excluded_file_is_still_checked(self) -> None:
        self.commit({CONFIGURATION: "version: 1\nparts:\n  repository-hygiene:\n    link-exclude: [vendor/**/*.md]\n",
                     "README.md": "See [the notes](vendor/lib/notes.md#usage) and [the start](vendor/lib/notes.md#start).\n",
                     "vendor/lib/notes.md": "# Start\n\nSee [gone](missing.md).\n"})
        ran = self.run_step("links")
        self.assertEqual(1, ran.returncode, ran.stdout + ran.stderr)
        self.assertIn("README.md:1: the link target vendor/lib/notes.md#usage names no heading of vendor/lib/notes.md", ran.stdout)
        self.assertNotIn("vendor/lib/notes.md#start", ran.stdout)
        self.assertNotIn("missing.md", ran.stdout, "the excluded file's own links stay unchecked")

    def test_a_configuration_file_that_cannot_be_used_leaves_no_file_out_of_either_check(self) -> None:
        self.commit({CONFIGURATION: "version: 1\nparts:\n  repository-hygiene:\n    coverage-exclude: [tests/fixtures]\n    link-exclude: [vendor/]\n    extra: true\n",
                     ".github/dependabot.yml": self.DEPENDABOT, "tests/fixtures/package.json": "{}\n", "README.md": "A consumer.\n",
                     "vendor/notes.md": "See [gone](missing.md).\n"})
        coverage, links = self.run_step("coverage"), self.run_step("links")
        self.assertEqual(1, coverage.returncode, coverage.stdout + coverage.stderr)
        self.assertIn("::error file=tests/fixtures/package.json::", coverage.stdout)
        self.assertEqual(1, links.returncode, links.stdout + links.stderr)
        self.assertIn("vendor/notes.md:1: the link target missing.md does not exist", links.stdout)
        for ran in (coverage, links):
            self.assertIn(".github/ci-config.yml cannot be used", ran.stderr)
            self.assertIn("parts.repository-hygiene.extra: is not a setting of the part repository-hygiene", ran.stderr)
        self.assertEqual(1, self.plan().status, "the plan fails with the file's problem")

    def test_a_setting_that_cannot_be_read_fails_the_step_instead_of_leaving_nothing_out_silently(self) -> None:
        self.commit({CONFIGURATION: "version: 1\n", "README.md": "A consumer.\n"})
        for step_id in ("coverage", "links"):
            with self.subTest(step=step_id):
                ran = self.run_step(step_id, pipeline_path=str(self.directory / "no-pipeline"))
                self.assertNotEqual(0, ran.returncode, "pipefail carries the failure of the reader")


if __name__ == "__main__":
    unittest.main()
