"""Text contract of the shared workflow of the suite jobs, and of the jobs that reach it: the pipeline's part
`suite-selection` and the full recording of the automation workflow. The suite jobs run the setups that the suite list
states as data and the selected entry files under the recording step, and hold no setup of any repository."""

from __future__ import annotations

import json
import re
import sys
import unittest

from tests.ci_policy_support import (REPOSITORY_ROOT, action_pin_pattern, condition_holds, entry_pathspecs, expression_value, flow_items,
                                     passed_list_steps, read_workflow, tracked_files)

WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
SUITE_JOBS = WORKFLOWS / "suite-jobs.yml"
PIPELINE = WORKFLOWS / "pipeline.yml"
AUTOMATION = WORKFLOWS / "automation.yml"
SELECTION = WORKFLOWS / "suite-selection.yml"
VERDICT = WORKFLOWS / "suite-verdict.yml"
SUITES = REPOSITORY_ROOT / "tests" / "suites.json"
LOCAL_CALL = "$/.github/workflows/suite-jobs.yml"
DISPATCHED_REF = "${{ github.event_name == 'repository_dispatch' && github.event.client_payload.sha || '' }}"
PART = "suite-selection"
PLANNED = f"fromJSON(needs.plan.outputs.plan).parts['{PART}']"
# The tools that a setup of a suite list may name, each with the action that installs it and the input that takes its version.
TOOLS = {"python": ("actions/setup-python", "python-version"), "node": ("actions/setup-node", "node-version"),
         "dotnet": ("actions/setup-dotnet", "dotnet-version")}
# Per job: how a step reads the version of a tool, the recording command's interpreter, and the tools' own directory.
JOBS = {
    "linux": {"tool": "matrix.shard.tools.{tool}", "command": 'python3 "$RECORDING/recording.py"', "suites": '--suites "$SUITES"',
              "dotnet": "${{ runner.temp }}/tools/dotnet"},
    "windows": {"tool": "fromJSON(env.TOOLS).{tool}", "command": 'python "$env:RECORDING\\recording.py"', "suites": "--suites $env:SUITES",
                "dotnet": "${{ runner.temp }}\\tools\\dotnet"},
}
SELECT_STEP = "Select the suites the changed files touch"


def needed(job: dict) -> list[str]:
    needs = str(job.get("needs", ""))
    return flow_items(needs[1:-1]) if needs.startswith("[") else [needs] if needs else []


def step_key(step: dict) -> str:
    return str(step.get("name") or str(step["uses"]).split("@", 1)[0])


class SuiteJobsWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(SUITE_JOBS)
        self.jobs = self.workflow["jobs"]

    def test_it_is_only_called_with_the_selection_s_outputs_the_suite_list_and_the_mode(self) -> None:
        self.assertEqual(["workflow_call"], list(self.workflow["on"]))
        inputs = self.workflow["on"]["workflow_call"]["inputs"]
        self.assertEqual({"suites": ("string", "true"), "shards": ("string", "false"), "windows": ("string", "false"), "jobs": ("string", "true"),
                          "language-steps": ("string", "false"), "shared-digest": ("string", "false"), "full": ("boolean", "false")},
                         {name: (value["type"], value["required"]) for name, value in inputs.items()})
        self.assertEqual("false", inputs["full"]["default"])
        self.assertNotIn("secrets", self.text)
        self.assertNotIn("pull_request_target", self.text)

    def test_the_jobs_are_the_linux_shards_and_the_one_windows_job(self) -> None:
        self.assertEqual(["linux", "windows"], list(self.jobs))
        linux, windows = self.jobs["linux"], self.jobs["windows"]
        self.assertEqual("inputs.shards != '' && inputs.shards != '[]'", linux["if"])
        self.assertEqual({"fail-fast": "false", "matrix": {"shard": "${{ fromJSON(inputs.shards) }}"}}, linux["strategy"])
        self.assertEqual("shard ${{ matrix.shard.shard }}", linux["name"])
        self.assertEqual("inputs.windows != ''", windows["if"])
        self.assertNotIn("strategy", windows, "the Windows job has no shards")
        self.assertNotIn("needs", linux)
        self.assertNotIn("needs", windows)

    def test_the_runner_label_and_the_fetch_depth_of_each_job_come_from_the_suite_list(self) -> None:
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                self.assertEqual(f"${{{{ fromJSON(inputs.jobs).{name}.runner }}}}", job["runs-on"])
                self.assertEqual(f"${{{{ fromJSON(inputs.jobs).{name}['fetch-depth'] }}}}", job["steps"][0]["with"]["fetch-depth"])
        self.assertIsNone(re.search(r"runs-on: (?!\$\{\{)", self.text), "no job names a runner label of its own")

    def test_each_job_has_read_rights_only_takes_no_token_and_keeps_no_checkout_credentials(self) -> None:
        self.assertIn("\npermissions: {}\n\n", self.text)
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                self.assertEqual({"contents": "read"}, job["permissions"])
                checkouts = [step for step in job["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")]
                self.assertEqual(1, len(checkouts))
                self.assertEqual("false", checkouts[0]["with"]["persist-credentials"])
                self.assertEqual(DISPATCHED_REF, checkouts[0]["with"]["ref"], "a dependabot-merged run checks out its SHA")
        for word in ("github.token", "GITHUB_TOKEN", "GH_TOKEN", "git commit", "git push"):
            self.assertNotIn(word, self.text)

    def test_the_jobs_use_pinned_actions_and_the_shared_steps_of_their_own_revision(self) -> None:
        external = sorted({uses.split("@")[0] for uses in re.findall(r"(?m)^\s*(?:- )?uses: ([^$\s]\S*)", self.text)})
        self.assertEqual(["actions/checkout", "actions/setup-dotnet", "actions/setup-node", "actions/setup-python", "actions/upload-artifact"], external)
        for action in external:
            pinned = len(re.findall(action_pin_pattern(action).replace(r"^[ \t]*- uses: ", r"^[ \t]*(?:- )?uses: "), self.text))
            self.assertEqual(self.text.count(f"uses: {action}@"), pinned, f"every use of {action} is pinned to a SHA with its version")
        self.assertEqual(4, self.text.count("uses: $/actions/recording\n"))
        self.assertEqual(1, self.text.count("uses: $/actions/python-language-step\n"))
        self.assertEqual([], re.findall(r"uses: \./", self.text))
        self.assertNotIn("@v1", self.text)

    def test_no_job_restores_a_cache(self) -> None:
        self.assertNotIn("actions/cache", self.text)
        for job in self.jobs.values():
            for step in job["steps"]:
                self.assertNotIn("cache: ", str(step.get("with", {})).replace("package-manager-cache", ""))

    def test_the_workflow_holds_no_setup_no_version_and_no_path_of_a_repository(self) -> None:
        stated = json.loads(SUITES.read_text(encoding="utf-8"))
        own = [name for name, setup in stated["setups"].items() if "tool" not in setup or name not in TOOLS]
        self.assertIn("actionlint", own)
        for name in own:
            with self.subTest(setup=name):
                self.assertIsNone(re.search(rf"\b{re.escape(name)}\b", self.text), "a setup is data of the suite list, with its one script")
        for own_text in ("tests/", "pins.json", "global.json", "install_tools", "npm ci", "3.13", "ubuntu-", "windows-20", "--path", "--variable"):
            with self.subTest(text=own_text):
                self.assertNotIn(own_text, self.text)
        for action, version in TOOLS.values():
            self.assertEqual(2, len(re.findall(rf"uses: {action}@[0-9a-f]{{40}} # v[\d.]+\n(?:        .*\n)*?          {version}: \$\{{\{{ ", self.text)),
                             f"{action} takes its version from the job's tools in both jobs")


class RecordedJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, workflow = read_workflow(SUITE_JOBS)
        self.jobs = workflow["jobs"]

    def index(self, job: str, found) -> int:
        (index,) = [index for index, step in enumerate(self.jobs[job]["steps"]) if found(step)]
        return index

    def order(self, job: str) -> dict[str, int]:
        """The place of each part of a suite job's order, by what its step does."""
        run = lambda text: lambda step: text in str(step.get("run", ""))  # noqa: E731
        return {
            "checkout": self.index(job, lambda step: str(step.get("uses", "")).startswith("actions/checkout@")),
            "recording": self.index(job, lambda step: step.get("id") == "recording"),
            **{tool: self.index(job, lambda step, action=action: str(step.get("uses", "")).startswith(f"{action}@")) for tool, (action, _) in TOOLS.items()},
            "before": self.index(job, run("--phase before")),
            "start": self.index(job, run("recording.py\" start")),
            "recorded": self.index(job, run("--phase recorded")),
            "entries": self.index(job, run("exec --entry")),
            "check": self.index(job, lambda step: step.get("uses") == "$/actions/recording" and "with" in step and step["with"].get("check") == "true"),
        }

    def test_each_job_runs_the_tools_the_setups_before_the_recording_the_start_the_setups_under_it_the_entry_files_and_the_check_in_that_order(self) -> None:
        for job in JOBS:
            with self.subTest(job=job):
                order = self.order(job)
                self.assertEqual(list(order.values()), sorted(order.values()), order)
                self.assertEqual(0, order["checkout"])
                self.assertEqual(1, order["recording"], "the recording step writes the job's environment before every tool and setup")
                self.assertEqual(order["before"] + 1, order["start"])
                self.assertEqual(order["start"] + 1, order["recorded"])
                self.assertEqual(order["recorded"] + 1, order["entries"], "nothing runs between the setups and the entry files")

    def test_the_recording_step_checks_the_digest_that_select_took(self) -> None:
        for job in JOBS:
            step = self.jobs[job]["steps"][self.order(job)["recording"]]
            self.assertEqual({"id": "recording", "uses": "$/actions/recording", "with": {"digest": "${{ inputs.shared-digest }}"}}, step, job)

    def test_each_tool_is_installed_only_for_a_job_whose_setups_name_it_with_the_version_of_the_suite_list_outside_the_recording(self) -> None:
        for job, forms in JOBS.items():
            steps = self.jobs[job]["steps"]
            order = self.order(job)
            for tool, (_, version) in TOOLS.items():
                with self.subTest(job=job, tool=tool):
                    step = steps[order[tool]]
                    named = forms["tool"].format(tool=tool)
                    self.assertEqual(named, step["if"])
                    self.assertEqual(f"${{{{ {named} }}}}", step["with"][version])
                    self.assertLess(order[tool], order["start"], "an action's reads are no reads of the recording")
            self.assertEqual("false", steps[order["python"]]["with"]["update-environment"], "the job's own Python stays the recording's interpreter")
            self.assertEqual("python", steps[order["python"]]["id"])
            self.assertEqual("false", steps[order["node"]]["with"]["package-manager-cache"])
            self.assertEqual(forms["dotnet"], steps[order["dotnet"]]["env"]["DOTNET_INSTALL_DIR"], "a tool installs into a directory of its own")

    def test_the_setups_come_from_the_suite_list_and_get_the_tools_that_the_job_installed(self) -> None:
        for job, forms in JOBS.items():
            with self.subTest(job=job):
                steps = self.jobs[job]["steps"]
                order = self.order(job)
                before, recorded = steps[order["before"]], steps[order["recorded"]]
                self.assertEqual("Run the setups that start before the recording", before["name"])
                self.assertEqual("Run the setups under the recording and provide what every setup gives", recorded["name"])
                for step, phase in ((before, "before"), (recorded, "recorded")):
                    self.assertIn(f"{forms['command']} setups {forms['suites']} --phase {phase} ", step["run"])
                    self.assertNotIn("if", step, "the command runs the setups that the job names, also when it names none")
                    self.assertIn("setup-options.txt", step["run"], "both phases take the same setups and tools")
                self.assertEqual("${{ steps.python.outputs.python-path }}", before["env"]["PYTHON_PATH"])
                for tool in TOOLS:
                    self.assertRegex(before["run"], rf"--tool['\"]?,? ['\"]?{tool}=")
                self.assertNotIn("--tool", recorded["run"], "the second phase reads the options that the first wrote")
        self.assertEqual("${{ join(matrix.shard.setups, ' ') }}", self.jobs["linux"]["steps"][self.order("linux")["before"]]["env"]["SETUPS"])
        self.assertIn("foreach ($setup in @($job.setups))", self.jobs["windows"]["steps"][self.order("windows")["before"]]["run"])

    def test_no_step_changes_the_job_s_environment_or_installs_into_the_system(self) -> None:
        for forbidden in ("GITHUB_PATH", "GITHUB_ENV", "apt-get", "sudo "):
            self.assertNotIn(forbidden, self.text)

    def test_the_start_takes_the_full_recording_mode_under_the_job_s_own_name(self) -> None:
        linux = self.jobs["linux"]["steps"][self.order("linux")["start"]]
        windows = self.jobs["windows"]["steps"][self.order("windows")["start"]]
        for step in (linux, windows):
            self.assertEqual("start", step["id"])
            self.assertEqual("${{ inputs.full }}", step["env"]["FULL"])
        self.assertEqual("shard-${{ matrix.shard.shard }}", linux["env"]["JOB"])
        self.assertIn('if [ "$FULL" = true ]; then set -- --full --job "$JOB"; else set --; fi', linux["run"])
        self.assertIn('python3 "$RECORDING/recording.py" start "$@"', linux["run"])
        self.assertIn("if ($env:FULL -eq 'true') { $mode = @('--full', '--job', 'windows') }", windows["run"])
        self.assertIn('python "$env:RECORDING\\recording.py" start @mode', windows["run"])

    def test_a_shard_runs_its_entry_files_two_at_a_time_each_traced_and_the_windows_job_one_after_another(self) -> None:
        linux = self.jobs["linux"]["steps"][self.order("linux")["entries"]]["run"]
        self.assertIn("jq -c '.entries | to_entries[] | .value + {index: .key}' <<<\"$SHARD\" | xargs -d '\\n' -r -n 1 -P 2 bash -c 'run_entry \"$1\"' _", linux)
        self.assertIn('exec --entry "$entry" --suites "$SUITES" --suite "$suite" -- "${command[@]}" "$entry"', linux)
        windows = self.jobs["windows"]["steps"][self.order("windows")["entries"]]["run"]
        self.assertIn("foreach ($item in $job.entries)", windows)
        self.assertIn("exec --entry $item.entry --suites $env:SUITES --suite $item.suite -- @command $item.entry", windows)
        self.assertEqual({"SHARD": "${{ toJSON(matrix.shard) }}", "SUITES": "${{ inputs.suites }}"}, self.jobs["linux"]["env"])
        self.assertEqual({"WINDOWS": "${{ inputs.windows }}", "SUITES": "${{ inputs.suites }}",
                          "TOOLS": "${{ inputs.windows != '' && toJSON(fromJSON(inputs.windows).tools) || '{}' }}"}, self.jobs["windows"]["env"])

    def test_the_step_that_runs_the_entry_files_sets_no_variable_for_them(self) -> None:
        # The entry files get the job's environment before its setups; a variable of this step reaches only exec.
        for job in JOBS:
            self.assertEqual({"RECORDING"}, set(self.jobs[job]["steps"][self.order(job)["entries"]]["env"]), job)

    def test_every_recording_command_runs_on_the_job_s_python(self) -> None:
        for job, forms in JOBS.items():
            for step in self.jobs[job]["steps"]:
                text = str(step.get("run", ""))
                if "recording.py" in text:
                    self.assertEqual(text.count("recording.py"), text.count(forms["command"]), f"{job}: {step.get('name')}")

    def test_check_runs_after_the_entry_files_with_a_unique_artifact_of_the_mode_unless_the_run_was_cancelled(self) -> None:
        names = []
        for job in JOBS:
            step = self.jobs[job]["steps"][self.order(job)["check"]]
            self.assertEqual("${{ !cancelled() && steps.start.outcome == 'success' }}", step["if"])
            self.assertEqual("${{ inputs.suites }}", step["with"]["suites"])
            names.append(step["with"]["artifact-name"])
        mode = "${{ inputs.full && 'full-recording' || 'recording-additions' }}"
        self.assertEqual([f"{mode}-shard-${{{{ matrix.shard.shard }}}}", f"{mode}-windows"], names)
        for full, name in ((True, "full-recording"), (False, "recording-additions")):
            self.assertEqual(name, expression_value(mode, {"inputs": {"full": full}}))

    def test_the_first_shard_runs_the_python_language_step_after_check_outside_the_recording_and_never_in_the_full_recording(self) -> None:
        steps = self.jobs["linux"]["steps"]
        calls = [index for index, step in enumerate(steps) if step.get("uses") == "$/actions/python-language-step"]
        self.assertEqual([self.order("linux")["check"] + 1], calls, "the step follows the recording's check")
        step = steps[calls[0]]
        self.assertEqual("${{ !cancelled() && !inputs.full && matrix.shard.shard == 1 && inputs.language-steps != '' "
                         "&& fromJSON(inputs.language-steps).python == 'run' }}", step["if"])
        self.assertNotIn("with", step, "the rule sets come from the repository's own configuration; no format check")
        self.assertNotIn("python-language-step", str(self.jobs["windows"]["steps"]))

    def test_a_pull_request_run_uploads_the_entry_files_that_passed_and_the_full_recording_none(self) -> None:
        for job, artifact in (("linux", "suite-results-shard-${{ matrix.shard.shard }}"), ("windows", "suite-results-windows")):
            with self.subTest(job=job):
                steps = self.jobs[job]["steps"]
                after = [step for step in steps[self.order(job)["check"] + 1:] if "steps.check." in str(step.get("if", ""))]
                self.assertEqual(["keep", "Upload the entry files that passed"], [step.get("id") or step["name"] for step in after][-2:])
                for step in after:
                    self.assertIn("!inputs.full", step["if"], "the full recording stores no passed result")
                    self.assertIn("github.event_name == 'pull_request'", step["if"])
                upload = after[-1]
                self.assertEqual({"name": artifact, "path": "${{ runner.temp }}/suite-results/passed.json", "retention-days": "1", "if-no-files-found": "error"},
                                 upload["with"])
                plain = [{**step, "if": str(step["if"]).replace("!inputs.full && ", "")} for step in after]
                names = [step.get("id") or step["name"] for step in after]
                self.assertEqual([name for name in names if name != "keep"], passed_list_steps(plain, "success", ""), "a passed check uploads every pass")
                self.assertEqual(names, passed_list_steps(plain, "failure", "additions"), "a check with additions uploads the kept passes")
                self.assertEqual([], passed_list_steps(plain, "failure", ""), "after a recording error the job uploads no passed list")


class UnrecordedInputRolesTests(unittest.TestCase):
    """The jobs and conditions of the suite jobs as the selection of unrecorded inputs reads them, in this repository,
    which reaches the workflow by local path."""

    def setUp(self) -> None:
        sys.path.insert(0, str(REPOSITORY_ROOT / "actions/suite-selection"))
        self.addCleanup(sys.path.remove, str(REPOSITORY_ROOT / "actions/suite-selection"))
        import suite_selection
        import unrecorded

        self.unrecorded = unrecorded
        self.workflow = unrecorded.ci_definition.read_yaml(SUITE_JOBS.read_text(encoding="utf-8"), str(SUITE_JOBS))
        self.tracked = tracked_files(REPOSITORY_ROOT, [*entry_pathspecs(SUITES), "tests/setups", "actions/recording/etw-consumer/global.json",
                                                      "actions/repository-hygiene/pins.json"])
        self.suite_list = suite_selection.resolve_entries(suite_selection.load_suite_list(SUITES), self.tracked)
        self.units = unrecorded.entry_units(self.suite_list)

    def test_both_jobs_are_suite_jobs_and_a_change_to_either_runs_every_unit(self) -> None:
        jobs = self.workflow["jobs"]
        self.assertEqual(["linux", "windows"], [name for name, job in jobs.items() if self.unrecorded.is_suite_job(job)])
        for name in ("linux", "windows"):
            with self.subTest(job=name):
                self.assertEqual(self.units, self.unrecorded.job_units(jobs[name], self.units), "the jobs read the shards and the Windows job as inputs")

    def test_each_step_belongs_to_the_job_to_the_language_step_or_to_the_results(self) -> None:
        for name in ("linux", "windows"):
            job = self.workflow["jobs"][name]
            roles = self.unrecorded.suite_job_roles(job)
            for step, (role, value) in zip(job["steps"], roles):
                with self.subTest(job=name, step=step.get("name") or step.get("uses")):
                    if "language-steps" in str(step.get("if") or ""):
                        self.assertEqual(("language", frozenset({"python"})), (role, value))
                    elif role == "result":
                        self.assertTrue("suite-results" in str(step.get("run", "")) or "upload-artifact" in str(step.get("uses", "")),
                                        "a step after the entry files that is no recording step only stores or reports results")
                    else:
                        self.assertEqual("job", role, "no step is gated on a setup: the suite list selects by a setup's files and definition")
            self.assertIn("result", [role for role, _ in roles], f"{name} stores its passed entry files after the entry files")

    def test_the_walk_from_this_repository_s_caller_reaches_the_suite_jobs_through_the_pipeline(self) -> None:
        side = self.unrecorded.Side(REPOSITORY_ROOT, None, frozenset(tracked_files(REPOSITORY_ROOT, [".github/workflows", "actions"])))
        parts = self.unrecorded.parts(side, ".github/workflows/ci.yml", self.units)
        labels = set().union(*(set(found) for found in parts.entries.values()))
        for label in ("the job `linux` of `.github/workflows/suite-jobs.yml`", "the job `windows` of `.github/workflows/suite-jobs.yml`",
                      "the job `suite-selection-jobs` of `.github/workflows/pipeline.yml`", "the job `ci` of `.github/workflows/ci.yml`",
                      "its step in `.github/workflows/suite-jobs.yml`"):
            with self.subTest(label=label):
                self.assertIn(label, labels)
        self.assertNotIn(f"the job `{PART}` of `.github/workflows/pipeline.yml`", labels, "the job that runs select shapes no unit's run")
        self.assertIn("python", parts.languages, "the language step of the first shard is found")


class PipelinePartTests(unittest.TestCase):
    """The part `suite-selection` of the pipeline: the selection, the call of the suite jobs and the store of the results."""

    def setUp(self) -> None:
        self.text, workflow = read_workflow(PIPELINE)
        self.jobs = workflow["jobs"]
        self.inputs = read_workflow(SUITE_JOBS)[1]["on"]["workflow_call"]["inputs"]

    def test_the_selection_job_runs_the_steps_of_the_shared_selection_workflow_on_the_configured_suite_list(self) -> None:
        shared = read_workflow(SELECTION)[1]["jobs"]["select"]
        ours = self.jobs[PART]
        self.assertEqual([step_key(step) for step in shared["steps"]], [step_key(step) for step in ours["steps"]])
        for theirs, step in zip(shared["steps"], ours["steps"]):
            with self.subTest(step=step_key(step)):
                if step_key(step) == SELECT_STEP:
                    self.assertEqual(f"${{{{ {PLANNED}.config.suites }}}}", step["env"]["SUITES"])
                    self.assertEqual(f"${{{{ join({PLANNED}.config['language-steps'] || fromJSON('[]'), ' ') }}}}", step["env"]["LANGUAGE_STEPS"])
                    self.assertEqual(set(theirs["env"]) - {"RUNNER_LABELS", "SETUP_VERSIONS"}, set(step["env"]),
                                     "the runner labels and the setups are data of the suite list")
                    without = re.sub(r"\n +for (?:label|version) in [^\n]+", "", theirs["run"])
                    self.assertEqual(re.sub(r"\s+", " ", without).replace("pull_request) set", "pull_request) set").strip(),
                                     re.sub(r"\s+", " ", step["run"]).replace(" ;; *)", " ;; *)").strip())
                else:
                    self.assertEqual(theirs, step, "a step of the selection is equal in both workflows")
        self.assertEqual(shared["permissions"], ours["permissions"])
        self.assertEqual(shared["runs-on"], ours["runs-on"])

    def test_the_selection_job_gives_the_outputs_of_select_and_the_jobs_of_the_suite_list(self) -> None:
        shared = read_workflow(SELECTION)[1]["jobs"]["select"]["outputs"]
        self.assertEqual({**shared, "jobs": "${{ steps.select.outputs.jobs }}"}, self.jobs[PART]["outputs"])

    def test_the_suite_jobs_run_when_the_selection_selects_an_entry_file_or_a_language_step(self) -> None:
        job = self.jobs[f"{PART}-jobs"]
        self.assertEqual(LOCAL_CALL, job["uses"])
        self.assertEqual(["plan", PART], needed(job))
        self.assertEqual({"contents": "read"}, job["permissions"])
        self.assertNotIn("secrets", job)
        for shards, windows, runs in (("", "", False), ("[]", "", False), ('[{"shard":1}]', "", True), ("", '{"entries":[]}', True), ("[]", "x", True)):
            with self.subTest(shards=shards, windows=windows):
                context = {"needs": {PART: {"outputs": {"shards": shards, "windows": windows}}}}
                self.assertEqual(runs, bool(expression_value(job["if"].replace(f"needs.{PART}", "needs.selection"), {"needs": {"selection": context["needs"][PART]}})))

    def test_the_suite_jobs_get_exactly_the_inputs_that_the_workflow_declares_from_the_selection_and_the_configuration(self) -> None:
        passed = self.jobs[f"{PART}-jobs"]["with"]
        self.assertEqual(set(self.inputs) - {"full"}, set(passed), "the mode stays the default: no full recording")
        self.assertEqual(f"${{{{ {PLANNED}.config.suites }}}}", passed["suites"])
        for name in ("shards", "windows", "jobs", "language-steps", "shared-digest"):
            self.assertEqual(f"${{{{ needs.{PART}.outputs.{name} }}}}", passed[name])

    def test_a_newer_head_of_a_pull_request_cancels_the_suite_jobs_in_a_group_of_their_own(self) -> None:
        selection, jobs = self.jobs[PART]["concurrency"], self.jobs[f"{PART}-jobs"]["concurrency"]
        self.assertEqual(f"{selection['group']}-jobs", jobs["group"], "two jobs of one run never share a group")
        self.assertEqual(selection["cancel-in-progress"], jobs["cancel-in-progress"])

    def test_the_results_job_stores_the_passed_results_of_a_pull_request_run_that_was_not_cancelled(self) -> None:
        job = self.jobs[f"{PART}-results"]
        self.assertEqual([PART, f"{PART}-jobs"], needed(job))
        self.assertEqual({"contents": "read"}, job["permissions"])
        self.assertEqual("ubuntu-slim", job["runs-on"])
        condition = job["if"].replace(f"needs.{PART}.result", "needs.select.result")
        for event, result, runs in (("pull_request", "success", True), ("push", "success", False), ("pull_request", "failure", False),
                                    ("pull_request", "skipped", False), ("repository_dispatch", "success", False)):
            with self.subTest(event=event, result=result):
                self.assertEqual(runs, condition_holds(condition, {"github.event_name": event, "needs.select.result": result}))
        self.assertIn("!cancelled()", job["if"], "the passes of a run with a failed suite job are stored; those of a cancelled run are not")
        shared = {step_key(step): step for step in read_workflow(VERDICT)[1]["jobs"]["validation"]["steps"]}
        names = [step_key(step) for step in job["steps"]]
        self.assertEqual([name for name in shared if name != "Decide the verdict from the selection and the results of the called jobs"], names)
        store = job["steps"][names.index("Write the record of the passed results")]
        self.assertEqual("${{ toJSON(needs) }}", store["env"]["NEEDS"])
        self.assertIn(f"--selection-job {PART} ", store["run"])
        self.assertEqual(shared["Store the passed results in the pull request's cache"]["with"], job["steps"][-1]["with"])
        self.assertEqual("steps.store.outputs.stored == 'true'", job["steps"][-1]["if"])

    def test_the_verdict_fails_on_a_failed_selection_suite_job_or_store(self) -> None:
        self.assertLessEqual({PART, f"{PART}-jobs", f"{PART}-results"}, set(needed(self.jobs["verdict"])))


class FullRecordingCallTests(unittest.TestCase):
    """The full recording of the automation workflow runs the same suite jobs in the full-recording mode."""

    def setUp(self) -> None:
        self.jobs = read_workflow(AUTOMATION)[1]["jobs"]
        self.inputs = read_workflow(SUITE_JOBS)[1]["on"]["workflow_call"]["inputs"]

    def test_the_full_recording_calls_the_suite_jobs_in_the_full_mode_with_the_shards_and_jobs_of_select(self) -> None:
        job = self.jobs["full-recording-jobs"]
        self.assertEqual(LOCAL_CALL, job["uses"])
        self.assertEqual("full-recording", job["needs"])
        self.assertEqual("needs.full-recording.outputs.run == 'true'", job["if"])
        self.assertEqual({"contents": "read"}, job["permissions"])
        self.assertNotIn("secrets", job, "the jobs that run the repository's code get no key")
        self.assertEqual({"suites": "${{ needs.full-recording.outputs.suites }}", "shards": "${{ needs.full-recording.outputs.shards }}",
                          "windows": "${{ needs.full-recording.outputs.windows }}", "jobs": "${{ needs.full-recording.outputs.jobs }}", "full": "true"},
                         job["with"])
        self.assertLessEqual(set(job["with"]), set(self.inputs))
        self.assertEqual("${{ steps.select.outputs.jobs }}", self.jobs["full-recording"]["outputs"]["jobs"])

    def test_no_job_of_the_workflow_installs_a_setup_or_names_a_runner_label_for_the_suites(self) -> None:
        text = AUTOMATION.read_text(encoding="utf-8")
        for own in ("actions/setup-", "install_tools.py --pins actions/", "exec --setup", "provide --setup", "windows-2025"):
            with self.subTest(text=own):
                self.assertNotIn(own, text)


class FormerLayoutTests(unittest.TestCase):
    def test_no_workflow_holds_the_former_validate_job_layout(self) -> None:
        for workflow in sorted(WORKFLOWS.glob("*.yml")):
            with self.subTest(workflow=workflow.name):
                text = workflow.read_text(encoding="utf-8")
                self.assertIsNone(re.search(r"(?m)^  validate:\n", text), "no job named validate")
                self.assertIsNone(re.search(r"outputs\.[a-z0-9-]+-(?:modules|entries)\b", text), "no per-suite module or entry outputs")
                self.assertIsNone(re.search(r"suite_selection\.py\"? select [^\n]*--github-output", text), "no step-output selection")

    def test_this_repository_holds_no_validation_workflow_of_its_own(self) -> None:
        callers = {path.name: path.read_text(encoding="utf-8") for path in sorted(WORKFLOWS.glob("*.yml"))}
        self.assertNotIn("validate.yml", callers)
        using = sorted(name for name, text in callers.items() if "exec --entry" in text or "suite-selection.yml" in text.replace("name: Suite selection", ""))
        self.assertEqual(["suite-jobs.yml"], [name for name in using if "exec --entry" in callers[name]], "only the shared suite jobs run entry files")
        self.assertEqual([], [name for name in using if "/suite-selection.yml" in callers[name]], "no workflow of this repository calls the selection workflow")
        self.assertEqual([".github/workflows/pipeline.yml", ".github/workflows/automation.yml"],
                         [f".github/workflows/{name}" for name in ("pipeline.yml", "automation.yml") if LOCAL_CALL in callers[name]])


if __name__ == "__main__":
    unittest.main()
