"""Policy checks over the repository validation workflow: the shared selection, the recorded Linux shards, the Windows
job and the shared verdict."""

from __future__ import annotations

import re
import sys
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, action_pin_pattern, entry_pathspecs, read_workflow, tracked_files, verdict_runs

WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
VALIDATE = WORKFLOWS / "validate.yml"
DISPATCHED_REF = "${{ github.event_name == 'repository_dispatch' && github.event.client_payload.sha || '' }}"


def installs_outside_the_recording(run: str) -> bool:
    """Whether a setup step's script does more than call exec --setup or only record what a setup provides."""
    if "exec --setup" in run:
        return False
    lines = [line.strip() for line in run.splitlines() if line.strip()]
    return not all(re.search(r"recording\.py\" provide --setup ", line) or line == EXIT_CHECK for line in lines)


EXIT_CHECK = "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }"


class SetupStepGateTests(unittest.TestCase):
    def test_a_setup_step_that_installs_without_exec_fails_the_gate(self) -> None:
        self.assertTrue(installs_outside_the_recording("sudo apt-get install -y lua5.4"))
        self.assertTrue(installs_outside_the_recording('npm ci\npython3 "$RECORDING/recording.py" provide --setup node --path "$DIR"'))
        self.assertFalse(installs_outside_the_recording('python3 "$RECORDING/recording.py" provide --setup node --path "$DIR"'))
        self.assertFalse(installs_outside_the_recording(f'python "$env:RECORDING\\recording.py" provide --setup gitleaks --path "$DIR"\n{EXIT_CHECK}'))
        self.assertFalse(installs_outside_the_recording('mkdir -p "$DIR"\npython3 "$RECORDING/recording.py" exec --setup lua -- true'))


class ValidateWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(VALIDATE)
        self.jobs = self.workflow["jobs"]

    def test_it_runs_on_pull_requests_pushes_to_main_and_dependabot_merged_dispatches(self) -> None:
        self.assertIn("on:\n  pull_request:\n  push:\n    branches: [main]\n  repository_dispatch:\n    types: [dependabot-merged]\n\n", self.text)

    def test_it_calls_the_shared_selection_and_verdict_by_local_path_with_a_consumer_s_inputs(self) -> None:
        self.assertEqual(["select", "linux", "windows", "validation"], list(self.jobs))
        select, verdict = self.jobs["select"], self.jobs["validation"]
        self.assertEqual("$/.github/workflows/suite-selection.yml", select["uses"])
        self.assertEqual({"suites": "tests/suites.json", "language-steps": "python", "runner-labels": "linux=ubuntu-26.04 windows=windows-2025",
                          "setup-versions": "python=3.13"}, select["with"])
        self.assertEqual("$/.github/workflows/suite-verdict.yml", verdict["uses"])
        self.assertEqual({"needs": "${{ toJSON(needs) }}"}, verdict["with"])
        self.assertNotIn("suite_selection.py", self.text, "the workflow runs the Tool only through the shared workflows")

    def test_select_runs_first_then_the_shards_and_the_windows_job_side_by_side_then_the_verdict(self) -> None:
        self.assertNotIn("needs", self.jobs["select"])
        self.assertEqual("select", self.jobs["linux"]["needs"])
        self.assertEqual("select", self.jobs["windows"]["needs"])
        self.assertEqual("[select, linux, windows]", self.jobs["validation"]["needs"])
        condition = self.jobs["validation"]["if"]
        self.assertFalse(verdict_runs(condition, "success", "true"), "a passed select of a tested tree skips the verdict")
        for result, identical in (("success", ""), ("failure", "true"), ("failure", ""), ("cancelled", "true"), ("skipped", "")):
            with self.subTest(select=result, identical=identical):
                self.assertTrue(verdict_runs(condition, result, identical))

    def test_the_linux_shards_come_from_the_selection_s_matrix(self) -> None:
        linux = self.jobs["linux"]
        self.assertEqual("needs.select.outputs.shards != '' && needs.select.outputs.shards != '[]'", linux["if"])
        self.assertEqual({"fail-fast": "false", "matrix": {"shard": "${{ fromJSON(needs.select.outputs.shards) }}"}}, linux["strategy"])

    def test_the_windows_job_runs_only_when_the_selection_selects_a_windows_entry_file(self) -> None:
        windows = self.jobs["windows"]
        self.assertEqual("needs.select.outputs.windows != ''", windows["if"])
        self.assertNotIn("strategy", windows, "the Windows job has no shards")

    def test_runner_labels(self) -> None:
        self.assertEqual("ubuntu-26.04", self.jobs["linux"]["runs-on"])
        self.assertEqual("windows-2025", self.jobs["windows"]["runs-on"])
        selection = (WORKFLOWS / "suite-selection.yml").read_text(encoding="utf-8")
        verdict = (WORKFLOWS / "suite-verdict.yml").read_text(encoding="utf-8")
        self.assertIn("\n  select:\n    runs-on: ubuntu-26.04\n", selection)
        self.assertIn("\n  validation:\n    runs-on: ubuntu-slim\n", verdict)

    def test_only_pull_request_runs_are_cancelled_and_only_by_a_newer_head_of_the_same_pull_request(self) -> None:
        self.assertEqual({
            "group": "${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.event.pull_request.number || github.run_id }}",
            "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
        }, self.workflow["concurrency"])

    def test_each_job_holds_contents_read_and_no_checkout_persists_credentials(self) -> None:
        self.assertIn("\npermissions: {}\n\n", self.text)
        for name, job in self.jobs.items():
            with self.subTest(job=name):
                expected = {"actions": "read", "contents": "read", "pull-requests": "read"} if name == "select" else {"contents": "read"}
                self.assertEqual(expected, job["permissions"], "the push lookup reads runs, job logs and the pull request")
        for name in ("linux", "windows"):
            checkouts = [step for step in self.jobs[name]["steps"] if str(step.get("uses", "")).startswith("actions/checkout@")]
            self.assertEqual(1, len(checkouts))
            self.assertEqual("false", checkouts[0]["with"]["persist-credentials"])
            self.assertEqual(DISPATCHED_REF, checkouts[0]["with"]["ref"], "a dependabot-merged run checks out its SHA")
        self.assertEqual("1", self.jobs["windows"]["steps"][0]["with"]["fetch-depth"], "Windows fixtures do not read this checkout's history")
        self.assertNotIn("git commit", self.text)
        self.assertNotIn("git push", self.text)

    def test_the_jobs_use_pinned_actions_and_the_recording_step_by_local_path(self) -> None:
        external = sorted({uses.split("@")[0] for uses in re.findall(r"(?m)^\s*(?:- )?uses: ([^$\s]\S*)", self.text)})
        self.assertEqual(["actions/checkout", "actions/setup-dotnet", "actions/setup-node", "actions/setup-python", "actions/upload-artifact"], external)
        for action in external:
            pinned = len(re.findall(action_pin_pattern(action).replace(r"^[ \t]*- uses: ", r"^[ \t]*(?:- )?uses: "), self.text))
            self.assertEqual(self.text.count(f"uses: {action}@"), pinned, f"every use of {action} is pinned to a SHA with its version")
        self.assertEqual(4, self.text.count("uses: $/actions/recording\n"))
        self.assertEqual([], re.findall(r"uses: \./", self.text))


class RecordedJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, workflow = read_workflow(VALIDATE)
        self.jobs = workflow["jobs"]

    def recording_steps(self, job: str) -> tuple[list[dict], int, int, int]:
        steps = self.jobs[job]["steps"]
        start = next(index for index, step in enumerate(steps) if "recording.py\" start" in str(step.get("run", "")))
        entries = next(index for index, step in enumerate(steps) if "exec --entry" in str(step.get("run", "")))
        check = next(index for index, step in enumerate(steps) if step.get("uses") == "$/actions/recording" and "with" in step)
        return steps, start, entries, check

    def test_a_shard_runs_its_setups_then_its_entry_files_two_at_a_time_each_traced_then_check(self) -> None:
        steps, start, entries, check = self.recording_steps("linux")
        self.assertLess(start, entries)
        self.assertLess(entries, check)
        for step in steps[start + 1:entries]:
            run = str(step.get("run", ""))
            if run:
                self.assertFalse(installs_outside_the_recording(run), f"the setup step {step.get('name')} runs under the recording")
        script = str(steps[entries]["run"])
        self.assertIn("jq -c '.entries[]' <<<\"$SHARD\" | xargs -d '\\n' -r -n 1 -P 2 bash -c 'run_entry \"$1\"' _", script)
        self.assertIn('exec --entry "$entry" --suites tests/suites.json --suite "$suite" -- "${command[@]}" "$entry"', script)
        self.assertEqual("${{ toJSON(matrix.shard) }}", self.jobs["linux"]["env"]["SHARD"])

    def test_the_windows_job_records_every_entry_file_under_etw(self) -> None:
        steps, start, entries, check = self.recording_steps("windows")
        self.assertLess(start, entries)
        self.assertLess(entries, check)
        for step in steps[start + 1:entries]:
            run = str(step.get("run", ""))
            if run:
                self.assertFalse(installs_outside_the_recording(run), f"the setup step {step.get('name')} runs under the recording")
        script = str(steps[entries]["run"])
        self.assertIn("foreach ($item in $job.entries)", script)
        self.assertIn("exec --entry $item.entry --suites tests/suites.json --suite $item.suite -- @command $item.entry", script)
        self.assertEqual("${{ needs.select.outputs.windows }}", self.jobs["windows"]["env"]["WINDOWS"])

    def test_the_recording_step_writes_the_job_s_environment_before_every_setup(self) -> None:
        for job in ("linux", "windows"):
            steps = self.jobs[job]["steps"]
            recording = next(index for index, step in enumerate(steps) if step.get("id") == "recording")
            setups = [index for index, step in enumerate(steps)
                      if str(step.get("uses", "")).startswith("actions/setup-") or "--setup" in str(step.get("run", ""))]
            self.assertTrue(setups)
            self.assertLess(recording, min(setups), job)

    def test_each_setup_installs_into_a_location_of_its_own_and_states_what_it_provides(self) -> None:
        for job, condition, location in (("linux", r"contains\(matrix\.shard\.setups, '([a-z-]+)'\)", "$RUNNER_TEMP/setups/"),
                                         ("windows", r"contains\(fromJSON\(env\.SETUPS\), '([a-z-]+)'\)", "$env:RUNNER_TEMP\\setups\\")):
            with self.subTest(job=job):
                steps = self.jobs[job]["steps"]
                installed = {name for step in steps for name in re.findall(condition, str(step.get("if", "")))}
                provided = {name for step in steps for name in re.findall(r"provide --setup ([a-z-]+)", str(step.get("run", "")))}
                looped = next((step for step in steps if "for setup in" in str(step.get("run", ""))), None)
                if looped is not None:
                    self.assertIn('provide --setup "$setup"', looped["run"])
                    provided |= set(re.search(r"for setup in ([a-z ]+); do", looped["run"]).group(1).split())
                self.assertEqual(installed, provided, "every setup that the job installs states what it provides")
                for step in steps:
                    text = str(step.get("run", ""))
                    self.assertNotIn("GITHUB_PATH", text, "no setup puts its tools on the job's PATH")
                    self.assertNotIn("GITHUB_ENV", text)
                    self.assertNotIn("apt-get install", text, "no setup installs into the system")
                    for destination in re.findall(r"--destination (\S+)", text):
                        self.assertTrue(destination.strip('"').startswith(location), destination)
                    if str(step.get("uses", "")).startswith("actions/setup-python@"):
                        self.assertEqual("false", step["with"]["update-environment"], "the job's own python3 stays the recording's interpreter")
                    if str(step.get("uses", "")).startswith("actions/setup-dotnet@"):
                        self.assertEqual("${{ runner.temp }}\\setups\\dotnet", step["env"]["DOTNET_INSTALL_DIR"])

    def test_the_set_up_python_gets_the_lookup_report_in_its_site_directory(self) -> None:
        for job in ("linux", "windows"):
            with self.subTest(job=job):
                steps = self.jobs[job]["steps"]
                python = next(step for step in steps if str(step.get("uses", "")).startswith("actions/setup-python@"))
                provides = [step for step in steps if "provide --setup python" in str(step.get("run", ""))]
                self.assertEqual(1, len(provides))
                self.assertEqual(f"${{{{ steps.{python['id']}.outputs.python-path }}}}", provides[0]["env"]["PYTHON_PATH"])
                self.assertRegex(provides[0]["run"], re.compile(r"provide --setup python .* --python (\"\$PYTHON_PATH\"|\$env:PYTHON_PATH)$", re.MULTILINE))
                self.assertEqual(python["if"], provides[0]["if"])

    def test_the_step_that_runs_the_entry_files_sets_no_variable_for_them(self) -> None:
        # The entry files get the job's environment before its setups; a variable of this step reaches only exec.
        for job, allowed in (("linux", {"RECORDING"}), ("windows", {"RECORDING", "TEMP", "TMP"})):
            steps, _, entries, _ = self.recording_steps(job)
            self.assertLessEqual(set(steps[entries].get("env", {})), allowed, job)

    def test_every_recording_command_runs_on_the_job_s_python(self) -> None:
        for job, interpreter in (("linux", 'python3 "$RECORDING/recording.py"'), ("windows", 'python "$env:RECORDING\\recording.py"')):
            with self.subTest(job=job):
                for step in self.jobs[job]["steps"]:
                    text = str(step.get("run", ""))
                    if "recording.py" in text:
                        self.assertEqual(text.count("recording.py"), text.count(interpreter), step.get("name"))

    def test_check_runs_after_the_entry_files_with_a_unique_artifact_and_unless_the_run_was_cancelled(self) -> None:
        names = []
        for job in ("linux", "windows"):
            steps, start, _, check = self.recording_steps(job)
            self.assertEqual("start", steps[start]["id"])
            step = steps[check]
            self.assertEqual("${{ !cancelled() && steps.start.outcome == 'success' }}", step["if"])
            self.assertEqual("true", step["with"]["check"])
            self.assertEqual("tests/suites.json", step["with"]["suites"])
            names.append(step["with"]["artifact-name"])
        self.assertEqual(["recording-additions-shard-${{ matrix.shard.shard }}", "recording-additions-windows"], names)

    def test_the_first_shard_runs_the_python_language_step_after_check_outside_the_recording(self) -> None:
        steps, _, _, check = self.recording_steps("linux")
        calls = [index for index, step in enumerate(steps) if step.get("uses") == "$/actions/python-language-step"]
        self.assertEqual([check + 1], calls, "the step follows the recording's check")
        step = steps[calls[0]]
        self.assertEqual("${{ !cancelled() && matrix.shard.shard == 1 && fromJSON(needs.select.outputs.language-steps).python == 'run' }}", step["if"])
        self.assertNotIn("with", step, "the rule sets come from ruff.toml; no format check")
        self.assertNotIn("recording.py", str(step))
        self.assertNotIn("python-language-step", str(self.jobs["windows"]["steps"]))

    def test_the_shard_installs_ruff_and_mypy_for_the_entry_files_that_state_them(self) -> None:
        steps, start, entries, _ = self.recording_steps("linux")
        install = next(step for step in steps[start + 1:entries] if "install_tools.py" in str(step.get("run", "")))
        self.assertIn("for setup in actionlint zizmor gitleaks reuse ruff mypy; do", install["run"])
        for setup in ("ruff", "mypy"):
            self.assertIn(f"contains(matrix.shard.setups, '{setup}')", install["if"])

    def test_no_recording_job_restores_a_cache(self) -> None:
        self.assertNotIn("actions/cache", self.text)
        for job in ("linux", "windows"):
            for step in self.jobs[job]["steps"]:
                self.assertNotIn("cache: ", str(step.get("with", {})).replace("package-manager-cache", ""))


class UnrecordedInputRolesTests(unittest.TestCase):
    """The jobs, gates and conditions of this workflow as the selection of unrecorded inputs reads them."""

    def setUp(self) -> None:
        sys.path.insert(0, str(REPOSITORY_ROOT / "actions/suite-selection"))
        self.addCleanup(sys.path.remove, str(REPOSITORY_ROOT / "actions/suite-selection"))
        import suite_selection
        import unrecorded

        self.unrecorded = unrecorded
        self.workflow = unrecorded.ci_definition.read_yaml(VALIDATE.read_text(encoding="utf-8"), str(VALIDATE))
        suites = REPOSITORY_ROOT / "tests/suites.json"
        tracked = tracked_files(REPOSITORY_ROOT, entry_pathspecs(suites))
        suite_list = suite_selection.resolve_entries(suite_selection.load_suite_list(suites), tracked)
        self.units = unrecorded.entry_units(suite_list)

    def test_the_shards_and_the_windows_job_are_the_suite_jobs_and_run_every_unit_of_their_system(self) -> None:
        jobs = self.workflow["jobs"]
        self.assertEqual(["linux", "windows"], [name for name, job in jobs.items() if self.unrecorded.is_suite_job(job)])
        self.assertTrue(self.unrecorded.runs_select({"steps": [{"run": 'python3 "$RESOURCE_PATH/suite_selection.py" select --root .'}]}))
        for name, system in (("linux", "linux"), ("windows", "windows")):
            with self.subTest(job=name):
                self.assertEqual([unit for unit in self.units if unit.os == system], self.unrecorded.job_units(jobs[name], self.units))

    def test_each_step_of_a_suite_job_has_the_role_its_condition_states(self) -> None:
        stated = {setup for unit in self.units for setup in unit.setups}
        for name in ("linux", "windows"):
            steps = self.workflow["jobs"][name]["steps"]
            roles = self.unrecorded.suite_job_roles(self.workflow["jobs"][name])
            for step, (role, value) in zip(steps, roles):
                condition = str(step.get("if") or "")
                with self.subTest(job=name, step=step.get("name") or step.get("uses")):
                    if ".setups" in condition or "env.SETUPS" in condition:
                        self.assertEqual("setups", role, "a step with a setup condition selects the entry files that state its setups")
                        self.assertLessEqual(set(value), stated, "every setup a condition names is a setup an entry file states")
                    elif "language-steps" in condition:
                        self.assertEqual(("language", frozenset({"python"})), (role, value))
                    elif role == "result":
                        self.assertTrue("suite-results" in str(step.get("run", "")) or "upload-artifact" in str(step.get("uses", "")),
                                        "a step after the entry files that is no recording step only stores or reports results")
                    else:
                        self.assertEqual("job", role)
            self.assertIn("result", [role for role, _ in roles], f"{name} stores its passed entry files after the entry files")


class FormerLayoutTests(unittest.TestCase):
    def test_no_workflow_holds_the_former_validate_job_layout(self) -> None:
        for workflow in sorted(WORKFLOWS.glob("*.yml")):
            with self.subTest(workflow=workflow.name):
                text = workflow.read_text(encoding="utf-8")
                self.assertIsNone(re.search(r"(?m)^  validate:\n", text), "no job named validate")
                self.assertIsNone(re.search(r"outputs\.[a-z0-9-]+-(?:modules|entries)\b", text), "no per-suite module or entry outputs")
                self.assertIsNone(re.search(r"suite_selection\.py\"? select [^\n]*--github-output", text), "no step-output selection")


if __name__ == "__main__":
    unittest.main()
