"""The ETW consumer's committed build is checked once, by the Windows suite `etw-consumer-build` of the validation
workflow; its source is a framework-dependent program on TraceEvent, whatever versions its project names; and the
rebuild workflow commits a rebuilt consumer to a Dependabot pull request that changes its source."""

from __future__ import annotations

import json
import re
import unittest
import xml.etree.ElementTree as ElementTree

from tests.ci_policy_support import REPOSITORY_ROOT, read_workflow

WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
REBUILD = WORKFLOWS / "rebuild-etw-consumer.yml"
SOURCE = REPOSITORY_ROOT / "actions" / "recording" / "etw-consumer"
BUILD_COMMAND = "python actions/recording/etw_consumer_build.py build"
TRACE_EVENT = "Microsoft.Diagnostics.Tracing.TraceEvent"
APP_TOKEN = "${{ steps.app-token.outputs.token }}"
CHANGED = "steps.changes.outputs.changed == 'true'"


def consumer_problems(project: str, runtime: str) -> list[str]:
    """Why a project file and its committed runtime configuration are not the consumer's: a program on TraceEvent,
    framework-dependent on the .NET runtime. Versions are the project's own."""
    root = ElementTree.fromstring(project)
    properties = {element.tag: (element.text or "").strip() for group in root.iter("PropertyGroup") for element in group}
    references = [element.get("Include") for element in root.iter("PackageReference")]
    problems = []
    if TRACE_EVENT not in references:
        problems.append(f"the project references no {TRACE_EVENT}")
    if properties.get("SelfContained") != "false":
        problems.append("the project is not framework-dependent")
    if not re.fullmatch(r"net\d+\.\d+", properties.get("TargetFramework", "")):
        problems.append("the project targets no .NET runtime")
    options = json.loads(runtime).get("runtimeOptions", {})
    frameworks = [options["framework"]] if "framework" in options else options.get("frameworks", [])
    if "Microsoft.NETCore.App" not in [framework.get("name") for framework in frameworks]:
        problems.append("the runtime configuration names no Microsoft.NETCore.App")
    return problems


def committed() -> tuple[str, str]:
    return ((SOURCE / "EtwConsumer.csproj").read_text(encoding="utf-8"),
            (SOURCE / "build" / "EtwConsumer.runtimeconfig.json").read_text(encoding="utf-8"))


class EtwConsumerCheckTests(unittest.TestCase):
    def test_the_check_runs_as_a_windows_suite_of_the_validation_workflow(self) -> None:
        suites = json.loads((REPOSITORY_ROOT / "tests" / "suites.json").read_text(encoding="utf-8"))["suites"]
        suite = next(suite for suite in suites if suite["name"] == "etw-consumer-build")
        self.assertEqual(("windows", ["tests/test_etw_consumer_build.py"]), (suite["os"], suite["entries"]))
        self.assertIn("dotnet", suite["setups"])
        validate = (WORKFLOWS / "validate.yml").read_text(encoding="utf-8")
        self.assertIn("global-json-file: actions/recording/etw-consumer/global.json", validate)

    def test_only_the_rebuild_workflow_runs_the_build_command_once_in_its_build_job(self) -> None:
        for workflow in sorted(WORKFLOWS.glob("*.yml")):
            if workflow != REBUILD:
                with self.subTest(workflow=workflow.name):
                    self.assertNotIn("etw_consumer_build.py", workflow.read_text(encoding="utf-8"))
        text, workflow = read_workflow(REBUILD)
        self.assertEqual(1, text.count("etw_consumer_build.py"))
        self.assertIn(BUILD_COMMAND, [step.get("run") for step in workflow["jobs"]["build"]["steps"]])


class EtwConsumerSourceTests(unittest.TestCase):
    def test_the_committed_consumer_is_a_framework_dependent_program_on_traceevent(self) -> None:
        self.assertEqual([], consumer_problems(*committed()))

    def test_the_check_holds_for_a_project_that_names_other_traceevent_and_dotnet_versions(self) -> None:
        project, runtime = committed()
        project = re.sub(rf'(Include="{re.escape(TRACE_EVENT)}" Version=")[^"]+"', r'\g<1>9.8.7"', project)
        project = re.sub(r"<TargetFramework>net[^<]+</TargetFramework>", "<TargetFramework>net12.0</TargetFramework>", project)
        runtime = re.sub(r'("version": ")[^"]+"', r'\g<1>12.0.0"', runtime)
        self.assertIn('Version="9.8.7"', project)
        self.assertIn('"version": "12.0.0"', runtime)
        self.assertEqual([], consumer_problems(project, runtime))

    def test_the_check_fails_without_traceevent_for_a_self_contained_build_or_without_the_shared_runtime(self) -> None:
        project, runtime = committed()
        for changed, expected in ((project.replace(TRACE_EVENT, "Some.Other.Package"), f"the project references no {TRACE_EVENT}"),
                                  (project.replace("<SelfContained>false</SelfContained>", "<SelfContained>true</SelfContained>"), "the project is not framework-dependent")):
            with self.subTest(expected=expected):
                self.assertEqual([expected], consumer_problems(changed, runtime))
        self.assertEqual(["the runtime configuration names no Microsoft.NETCore.App"],
                         consumer_problems(project, runtime.replace("Microsoft.NETCore.App", "Microsoft.WindowsDesktop.App")))


class RebuildWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(REBUILD)
        self.jobs = self.workflow["jobs"]

    def job_text(self, name: str) -> str:
        return re.search(rf"(?ms)^  {name}:\n(.*?)(?=^  \S|\Z)", self.text)[1]  # type: ignore[index]

    def test_it_runs_after_each_completed_validate_run_and_never_cancels_a_run(self) -> None:
        self.assertEqual("Validate", read_workflow(WORKFLOWS / "validate.yml")[1]["name"])
        self.assertEqual({"workflow_run": {"workflows": "[Validate]", "types": "[completed]"}}, self.workflow["on"])
        self.assertEqual({"group": "${{ github.workflow }}-${{ github.event.workflow_run.head_branch }}", "cancel-in-progress": "false"},
                         self.workflow["concurrency"])
        self.assertIn("\npermissions: {}\n\n", self.text)

    def test_it_acts_only_on_a_failed_pull_request_run_of_a_dependabot_branch_of_this_repository(self) -> None:
        self.assertEqual("github.event.workflow_run.event == 'pull_request' && github.event.workflow_run.conclusion == 'failure' && "
                         "github.event.workflow_run.head_repository.full_name == github.repository && "
                         "startsWith(github.event.workflow_run.head_branch, 'dependabot/')", self.jobs["plan"]["if"])
        self.assertEqual("plan", self.jobs["build"]["needs"])
        self.assertEqual("needs.plan.outputs.rebuild == 'true'", self.jobs["build"]["if"])
        self.assertEqual("[plan, build]", self.jobs["push"]["needs"])

    def test_the_plan_acts_only_for_dependabot_s_open_pull_request_at_the_run_s_head_that_changes_the_consumer_s_source(self) -> None:
        (step,) = self.jobs["plan"]["steps"]
        self.assertEqual({"GH_TOKEN": "${{ github.token }}", "HEAD_BRANCH": "${{ github.event.workflow_run.head_branch }}",
                          "HEAD_SHA": "${{ github.event.workflow_run.head_sha }}", "OWNER": "${{ github.repository_owner }}"}, step["env"])
        self.assertIn('select(.user.login == \\"dependabot[bot]\\" and .head.sha == \\"$HEAD_SHA\\")', step["run"])
        self.assertIn("pulls/$number/files", step["run"])
        self.assertIn(r"/^actions\/recording\/etw-consumer\// && !/^actions\/recording\/etw-consumer\/build\//", step["run"])
        self.assertEqual(1, step["run"].count("rebuild=true"))
        self.assertEqual({"rebuild": "${{ steps.pull-request.outputs.rebuild }}"}, self.jobs["plan"]["outputs"])

    def test_jobs_runners_and_rights(self) -> None:
        self.assertEqual(["plan", "build", "push"], list(self.jobs))
        self.assertEqual({"plan": "ubuntu-slim", "build": "windows-2025", "push": "ubuntu-slim"}, {name: job["runs-on"] for name, job in self.jobs.items()})
        self.assertEqual({"contents": "read", "pull-requests": "read"}, self.jobs["plan"]["permissions"])
        self.assertEqual({"contents": "read"}, self.jobs["build"]["permissions"])
        self.assertEqual({"actions": "read", "contents": "read"}, self.jobs["push"]["permissions"])

    def test_the_build_job_builds_the_pull_request_s_head_with_a_read_only_token_and_no_app_token(self) -> None:
        build = self.job_text("build")
        for absent in ("secrets.", "create-github-app-token", "app-token", "GH_TOKEN", "github.token"):
            with self.subTest(absent=absent):
                self.assertNotIn(absent, build)
        steps = self.jobs["build"]["steps"]
        self.assertEqual({"ref": "${{ github.event.workflow_run.head_sha }}", "fetch-depth": "1", "persist-credentials": "false"}, steps[0]["with"])
        self.assertEqual({"global-json-file": "actions/recording/etw-consumer/global.json"}, steps[1]["with"])
        self.assertEqual(BUILD_COMMAND, steps[2]["run"])
        self.assertEqual({"name": "etw-consumer-build", "path": "actions/recording/etw-consumer/build/", "if-no-files-found": "error"}, steps[3]["with"])

    def test_only_the_push_job_mints_the_app_token_with_contents_write_only(self) -> None:
        self.assertEqual(1, self.text.count("create-github-app-token@"))
        self.assertEqual(1, self.text.count("secrets."))
        steps = self.jobs["push"]["steps"]
        mint = next(step for step in steps if str(step.get("uses", "")).startswith("actions/create-github-app-token@"))
        self.assertEqual({"client-id": "${{ vars.PUBLICATION_CHECKS_APP_CLIENT_ID }}", "private-key": "${{ secrets.PUBLICATION_CHECKS_APP_PRIVATE_KEY }}",
                          "permission-contents": "write"}, mint["with"])
        self.assertEqual(CHANGED, mint["if"])

    def test_the_push_job_runs_the_workflow_s_own_tools_on_a_checkout_of_the_head_and_never_its_code(self) -> None:
        steps = self.jobs["push"]["steps"]
        own, head = steps[0], steps[1]
        self.assertEqual({"fetch-depth": "1", "persist-credentials": "false"}, own["with"], "the workflow's own revision")
        self.assertEqual({"ref": "${{ github.event.workflow_run.head_sha }}", "path": "pull-request", "fetch-depth": "1", "persist-credentials": "false"}, head["with"])
        for step in steps:
            run = str(step.get("run", ""))
            with self.subTest(step=step.get("name") or step.get("uses")):
                self.assertNotRegex(run, r"(?:python3|bash|sh|source|\.) +\"?pull-request/", "no file of the head runs")
                self.assertNotRegex(run, r"(?m)^\s*\"?\.?/?pull-request/", "no file of the head runs")
                for invoked in re.findall(r"python3 (\S+\.py)", run):
                    self.assertTrue(invoked.startswith("actions/"), invoked)

    def test_the_push_job_applies_the_run_s_additions_and_replaces_the_build_with_the_rebuild(self) -> None:
        steps = self.jobs["push"]["steps"]
        download = next(step for step in steps if str(step.get("uses", "")).startswith("actions/download-artifact@"))
        self.assertEqual({"name": "etw-consumer-build", "path": "${{ runner.temp }}/etw-consumer-build"}, download["with"])
        additions = next(step for step in steps if step.get("name") == "Download the recording additions of the failed run")
        self.assertEqual("${{ github.event.workflow_run.id }}", additions["env"]["RUN_ID"])
        self.assertIn('select(.name | startswith("recording-additions-"))', additions["run"])
        apply = next(step for step in steps if "recording.py apply" in str(step.get("run", "")))
        self.assertIn('recording.py apply --root pull-request --suites pull-request/tests/suites.json --summary "$GITHUB_STEP_SUMMARY" "${additions[@]}"', apply["run"])
        self.assertIn('rm -rf "$build"\n', apply["run"])
        self.assertTrue(apply["run"].endswith('\ncp -R "$RUNNER_TEMP/etw-consumer-build" "$build"'))

    def test_the_push_job_commits_nothing_without_a_change_and_fails_on_any_file_but_the_build_and_the_suite_list(self) -> None:
        steps = self.jobs["push"]["steps"]
        changes = next(step for step in steps if step.get("id") == "changes")
        self.assertIn('if [ -z "$changed" ]; then', changes["run"])
        self.assertRegex(changes["run"], r'echo "changed=false" >> "\$GITHUB_OUTPUT"\n\s+exit 0\n')
        self.assertRegex(changes["run"], r"\n\s+actions/recording/etw-consumer/build/\*\|tests/suites\.json\) ;;\n"
                                         r'\s+\*\) echo "::error::Unexpected changed file: \$path"; exit 1 ;;\n')
        self.assertIn("git -C pull-request config core.fileMode false", changes["run"])
        after = steps[steps.index(changes) + 1:]
        self.assertGreater(len(after), 3)
        for step in after:
            with self.subTest(step=step.get("name") or step.get("uses")):
                self.assertEqual(CHANGED, step["if"])

    def test_the_push_job_pushes_as_the_app_so_that_the_push_starts_validate_and_never_forces(self) -> None:
        steps = self.jobs["push"]["steps"]
        commit = next(step for step in steps if step.get("name") == "Commit the rebuild as the GitHub App")
        self.assertEqual("${{ steps.app-token.outputs.app-slug }}[bot]", commit["env"]["APP_LOGIN"])
        self.assertIn('git -C pull-request commit -m "chore: rebuild the ETW consumer"', commit["run"])
        scan = next(step for step in steps if step.get("name") == "Scan the commit before pushing")
        self.assertIn('--log-opts="HEAD --not --remotes=origin"', scan["run"])
        self.assertLess(scan["run"].index('= "$HEAD_SHA" ] || exit 0'), scan["run"].index("gitleaks"), "a moved branch is neither scanned nor pushed to")
        push = steps[-1]
        self.assertEqual("Push the rebuild to the pull request's branch", push["name"])
        self.assertEqual(APP_TOKEN, push["env"]["GH_TOKEN"])
        self.assertIn("-c credential.helper= -c 'credential.helper=!gh auth git-credential' push origin \"HEAD:refs/heads/$HEAD_BRANCH\"", push["run"])
        self.assertIn('if [ "$remote" != "$HEAD_SHA" ]; then', push["run"])
        self.assertNotIn("--force", self.text)
        self.assertEqual(1, self.text.count(" push origin "))


if __name__ == "__main__":
    unittest.main()
