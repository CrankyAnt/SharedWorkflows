"""The validation workflow template: Linux shard jobs and one Windows job that run their setups and selected entry files
under the recording step, in the form of this repository's own `validate.yml`."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import NamedTuple
from tests.ci_policy_support import ci_definition, passed_list_steps, read_workflow

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SELECTION_KIT = REPOSITORY_ROOT / "actions" / "suite-selection" / "consumer-kit"
OWN_VALIDATE = REPOSITORY_ROOT / ".github" / "workflows" / "validate.yml"
RECORDING = "CrankyAnt/SharedWorkflows/actions/recording@v1"
PYTHON_LANGUAGE_STEP = "CrankyAnt/SharedWorkflows/actions/python-language-step@v1"
RECORDING_PATH = "${{ steps.recording.outputs.path }}"
DISPATCH_REF = "${{ github.event_name == 'repository_dispatch' && github.event.client_payload.sha || '' }}"
NOT_CANCELLED = "!cancelled()"
PULL_REQUEST = "github.event_name == 'pull_request'"
CHECK_PASSED_OR_ADDITIONS = "(steps.check.outcome == 'success' || steps.check.outputs.additions != '')"
ADDITIONS = "steps.check.outputs.additions != ''"
CHECK_PASSED_OR_KEPT = "(steps.check.outcome == 'success' || steps.keep.outcome == 'success')"
STARTED = "steps.start.outcome == 'success'"
PYTHON_TERM = "fromJSON(inputs.language-steps).python == 'run'"
PLACEHOLDER = re.compile(r"<[a-z][a-z-]*>")


class Job(NamedTuple):
    """A job of the template with what the template gives it."""

    name: str
    setup: str
    setup_gate: str
    shell: str | None
    additions: str
    passed: str


JOBS = (Job("linux", "<setup>", "contains(matrix.shard.setups, '<setup>')", None,
            "recording-additions-shard-${{ matrix.shard.shard }}", "suite-results-shard-${{ matrix.shard.shard }}"),
        Job("<windows-job>", "<windows-setup>", "contains(fromJSON(env.SETUPS), '<windows-setup>')", "pwsh",
            "recording-additions-windows", "suite-results-windows"))
SHARD_ONE = "matrix.shard.shard == 1"


def as_input(node: object) -> object:
    """A part of validate.yml with the outputs of its select job read as the inputs that the caller passes instead."""
    if isinstance(node, dict):
        return {key: as_input(value) for key, value in node.items()}
    if isinstance(node, list):
        return [as_input(item) for item in node]
    return node.replace("needs.select.outputs.", "inputs.") if isinstance(node, str) else node


def conjuncts(condition: str) -> set[str]:
    """The terms that a condition joins with ``&&``, without its ``${{ }}`` wrapper."""
    text = condition.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    return {term.strip() for term in text.split("&&")}


def recording_calls(script: str) -> list[list[str]]:
    """The arguments after recording.py of every call of it in a bash or pwsh script, up to the end of its command."""
    calls = []
    for line in script.splitlines():
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";")
        lexer.whitespace_split = True
        lexer.escape = ""
        tokens = list(lexer)
        for index, token in enumerate(tokens):
            if re.search(r"[/\\]recording\.py$", token):
                rest = tokens[index + 1:]
                calls.append(rest[:rest.index(";")] if ";" in rest else rest)
    return calls


def passed_record(script: str) -> dict[str, str]:
    """The fields of the record a script writes for an entry file that passed: a jq object built with ``--arg`` in bash, or
    an ``[ordered]@{...}`` table in pwsh. A quoted value is a literal, a ``$name`` value the shell variable it names."""
    for line in script.splitlines():
        table = re.search(r"\[ordered\]@\{(.*)\}", line)
        if table:
            pairs = [item.split("=", 1) for item in table.group(1).split(";")]
            return {key.strip(): value.strip().strip("'\"") for key, value in pairs}
        lexer = shlex.shlex(line, posix=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
        if tokens[:1] == ["jq"] and "--arg" in tokens:
            arguments = {tokens[index + 1]: tokens[index + 2] for index, token in enumerate(tokens) if token == "--arg"}
            program = next(token for token in tokens if token.startswith("{") and token.endswith("}"))
            fields = {}
            for item in program[1:-1].split(","):
                key, value = (part.strip() for part in item.split(":", 1))
                fields[key] = arguments[value[1:]] if value.startswith("$") else value.strip('"')
            return fields
    raise AssertionError("the script writes no passed record")


def writes(script: str, name: str) -> bool:
    """Whether the script names the file ``name`` under ``$RUNNER_TEMP/suite-results``."""
    return re.search(r"RUNNER_TEMP[/\\]suite-results[/\\]" + re.escape(name) + r"(?![\w.])", script) is not None


def role(step: dict) -> str:
    """What a step of the template does, read from what it calls or uses rather than from its name."""
    found = known_role(step)
    if found is None:
        raise AssertionError(f"a step of unknown role: {step}")
    return found


def known_role(step: dict) -> str | None:
    """What a step does, read from what it calls or uses rather than from its name, or None for any other step."""
    uses = str(step.get("uses", ""))
    if uses.startswith("actions/checkout@"):
        return "checkout"
    if uses.startswith("actions/upload-artifact@"):
        return "upload"
    if uses.startswith("actions/setup-python@"):
        return "python setup"
    if uses == PYTHON_LANGUAGE_STEP:
        return "python language step"
    if uses == RECORDING:
        return "check" if step.get("with", {}).get("check") == "true" else "recording path"
    calls = recording_calls(str(step.get("run", "")))
    if calls == [["start"]]:
        return "start"
    if len(calls) == 1 and calls[0][:3] == ["provide", "--setup", "python"]:
        return "python provide"
    if calls and all(call[:2] in (["exec", "--setup"], ["provide", "--setup"]) for call in calls):
        return "setup"
    if calls and all(call[:2] == ["exec", "--entry"] for call in calls):
        return "entries"
    if len(calls) == 1 and calls[0][:1] == ["keep"]:
        return "keep"
    if not calls and writes(str(step.get("run", "")), "passed.json"):
        return "passed list"
    return None


def own_role(step: dict) -> str | None:
    """What a step of validate.yml does, with its local calls of the shared steps read as the template's calls at v1;
    None for a setup of this repository's own, which the template's setup placeholders stand for."""
    local = {"$/actions/recording": RECORDING, "$/actions/python-language-step": PYTHON_LANGUAGE_STEP}
    return known_role(step | {"uses": local.get(step["uses"], step["uses"])} if "uses" in step else step)


class ValidationWorkflowTemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(SELECTION_KIT / "validation-workflow.yml")
        self.jobs = self.workflow["jobs"]
        _, own = read_workflow(OWN_VALIDATE)
        self.own = own["jobs"]

    def roles(self, job: str) -> list[str]:
        return [role(step) for step in self.jobs[job]["steps"]]

    def step(self, job: str, name: str) -> dict:
        (found,) = [step for step in self.jobs[job]["steps"] if role(step) == name]
        return found

    def own_step(self, job: str, name: str) -> dict:
        """A step of validate.yml's job, found by what it does."""
        (found,) = [step for step in self.own[job]["steps"] if own_role(step) == name]
        return found

    def own_entries(self, job: str) -> str:
        """The script of validate.yml's step that runs the entry files, on the consumer's suite list."""
        return self.own_step(job, "entries")["run"].replace("tests/suites.json", ".github/suites.json")

    def test_only_the_validation_caller_starts_it_with_the_shards_the_windows_job_and_the_language_steps(self) -> None:
        self.assertEqual({"workflow_call": {"inputs": {"shards": {"type": "string", "required": "false"},
                                                       "windows": {"type": "string", "required": "false"},
                                                       "language-steps": {"type": "string", "required": "false"},
                                                       "shared-digest": {"type": "string", "required": "true"}}}}, self.workflow["on"])
        self.assertEqual("{}", self.workflow["permissions"])
        self.assertEqual(["linux", "<windows-job>"], list(self.jobs))

    def test_a_shard_job_runs_per_shard_on_the_one_linux_runner_label_and_only_when_shards_holds_one(self) -> None:
        linux = self.jobs["linux"]
        self.assertEqual("inputs.shards != '' && inputs.shards != '[]'", linux["if"])
        self.assertEqual("shard ${{ matrix.shard.shard }}", linux["name"])
        self.assertEqual({"fail-fast": "false", "matrix": {"shard": "${{ fromJSON(inputs.shards) }}"}}, linux["strategy"])
        self.assertEqual({"SHARD": "${{ toJSON(matrix.shard) }}"}, linux["env"])
        self.assertEqual("<linux-runner>", linux["runs-on"])
        for key in ("if", "name", "strategy", "env"):
            with self.subTest(key=key):
                self.assertEqual(as_input(self.own["linux"][key]), linux[key], "the shard job of validate.yml")

    def test_the_windows_job_runs_only_when_a_windows_entry_file_is_selected_on_the_windows_runner_label(self) -> None:
        windows = self.jobs["<windows-job>"]
        self.assertEqual("inputs.windows != ''", windows["if"])
        self.assertEqual("<windows-runner>", windows["runs-on"])
        self.assertNotIn("strategy", windows)
        for key in ("if", "env"):
            with self.subTest(key=key):
                self.assertEqual(as_input(self.own["windows"][key]), windows[key], "the Windows job of validate.yml")

    def test_every_job_reads_the_contents_only(self) -> None:
        for job in self.jobs.values():
            self.assertEqual({"contents": "read"}, job["permissions"])

    def test_each_setup_is_gated_on_the_setups_of_its_job_and_no_job_or_step_on_a_suite(self) -> None:
        reader = ci_definition()
        for job in JOBS:
            with self.subTest(job=job.name):
                self.assertEqual(job.setup_gate, self.step(job.name, "setup")["if"])
                self.assertEqual(frozenset(), reader.condition_gate(self.jobs[job.name]["if"]).suites)
                for step in self.jobs[job.name]["steps"]:
                    self.assertEqual(frozenset(), reader.condition_gate(step.get("if")).suites, step)
        python = "contains(matrix.shard.setups, 'python')"
        self.assertEqual(python, self.step("linux", "python setup")["if"])
        self.assertEqual(python, self.step("linux", "python provide")["if"])
        self.assertNotIn("if", self.step("linux", "start"), "every shard records")
        self.assertNotIn("inputs.suites", self.text)
        self.assertNotIn("inputs.entries", self.text)

    def test_each_action_reference_names_its_action_with_the_pin_placeholder(self) -> None:
        actions = {"checkout": "actions/checkout", "python setup": "actions/setup-python", "upload": "actions/upload-artifact"}
        for job in self.jobs:
            for name, action in actions.items():
                if name in self.roles(job):
                    with self.subTest(job=job, step=name):
                        self.assertEqual(f"{action}@<pin>", self.step(job, name)["uses"])
        references = {step["uses"] for job in self.jobs.values() for step in job["steps"]
                      if "uses" in step and not step["uses"].startswith("CrankyAnt/SharedWorkflows/")}
        self.assertEqual({f"{action}@<pin>" for action in actions.values()}, references)
        self.assertEqual([], re.findall(r"@[0-9a-f]{40}", self.text), "the template carries no commit pin")
        self.assertEqual([], re.findall(r"(?m)^ +(?:- )?uses: \S+ +#.*$", self.text), "the template carries no version comment")

    def test_every_checkout_takes_the_dispatched_commit_and_keeps_no_credentials(self) -> None:
        for job in self.jobs:
            with self.subTest(job=job):
                self.assertEqual({"ref": DISPATCH_REF, "persist-credentials": "false"}, self.step(job, "checkout")["with"])

    def test_the_steps_run_in_order_with_the_shared_actions_at_v1(self) -> None:
        self.assertEqual(["checkout", "recording path", "python setup", "start", "python provide", "setup", "entries", "check",
                          "python language step", "passed list", "keep", "upload"], self.roles("linux"))
        self.assertEqual(["checkout", "recording path", "start", "setup", "entries", "check", "keep", "upload"], self.roles("<windows-job>"))
        for job in self.jobs:
            with self.subTest(job=job):
                self.assertEqual({"id": "recording", "uses": RECORDING, "with": {"digest": "${{ inputs.shared-digest }}"}},
                                 self.step(job, "recording path"), "the first step after the checkout fails on a digest that select did not take")

    def test_the_setups_run_under_the_recording_after_its_start(self) -> None:
        for job, setup, shell in ((spec.name, spec.setup, spec.shell) for spec in JOBS):
            with self.subTest(job=job):
                start, setup_step = self.step(job, "start"), self.step(job, "setup")
                self.assertEqual("start", start["id"])
                setup_call, provide_call = recording_calls(setup_step["run"])
                self.assertEqual(["exec", "--setup", setup, "--"], setup_call[:4])
                self.assertRegex(" ".join(setup_call[4:]), PLACEHOLDER, "the setup's own command follows")
                self.assertEqual(["provide", "--setup", setup], provide_call[:3])
                self.assertRegex(" ".join(provide_call[3:]), PLACEHOLDER, "what the setup provides to the entry files that state it follows")
                for step in (start, setup_step):
                    self.assertEqual(RECORDING_PATH, step["env"]["RECORDING"])
                    self.assertEqual(shell, step.get("shell"))

    def test_the_set_up_python_gets_the_lookup_report_as_validate_yml_gives_it(self) -> None:
        python, provide = self.step("linux", "python setup"), self.step("linux", "python provide")
        self.assertEqual({"python-version": "<python-version>", "update-environment": "false"}, python["with"])
        self.assertEqual({"RECORDING": RECORDING_PATH, "PYTHON_PATH": f"${{{{ steps.{python['id']}.outputs.python-path }}}}"}, provide["env"])
        self.assertEqual(self.own_step("linux", "python provide")["run"], provide["run"])

    def test_a_shard_runs_its_entry_files_two_at_a_time_each_with_its_suite_and_command_and_its_own_log_group(self) -> None:
        entries = self.step("linux", "entries")
        self.assertEqual(self.own_entries("linux"), entries["run"])
        self.assertEqual({"RECORDING": RECORDING_PATH}, entries["env"])
        (call,) = recording_calls(entries["run"])
        self.assertEqual(["exec", "--entry", "$entry", "--suites", ".github/suites.json", "--suite", "$suite", "--", "${command[@]}", "$entry"],
                         call[:10])
        script = entries["run"]
        self.assertIn("<<<\"$SHARD\" | xargs -d '\\n' -r -n 1 -P 2 ", script, "two entry files at a time")
        self.assertIn('echo "::group::$entry ($suite)"', script)
        self.assertIn('echo "::endgroup::"', script)
        self.assertIn("set -uo pipefail", script, "a failed entry file stops none of the others")
        self.assertTrue(script.rstrip().endswith('exit "$failed"'), "the shard fails after every entry file ran")

    def test_the_windows_job_runs_every_selected_windows_entry_file_one_after_another(self) -> None:
        entries = self.step("<windows-job>", "entries")
        self.assertEqual(self.own_entries("windows"), entries["run"])
        self.assertEqual("pwsh", entries["shell"])
        self.assertEqual({"RECORDING": RECORDING_PATH}, entries["env"])
        (call,) = recording_calls(entries["run"])
        self.assertEqual(["exec", "--entry", "$item.entry", "--suites", ".github/suites.json", "--suite", "$item.suite", "--", "@command",
                          "$item.entry"], call)
        self.assertIn("foreach ($item in $job.entries)", entries["run"])

    def test_check_compares_the_recording_with_the_suite_list_whenever_the_recording_started(self) -> None:
        for job, additions in ((spec.name, spec.additions) for spec in JOBS):
            with self.subTest(job=job):
                check = self.step(job, "check")
                self.assertEqual("check", check["id"])
                self.assertEqual({NOT_CANCELLED, STARTED}, conjuncts(check["if"]))
                self.assertEqual({"check": "true", "suites": ".github/suites.json", "artifact-name": additions}, check["with"])

    def test_shard_1_alone_runs_the_python_language_step_after_check_outside_the_recording(self) -> None:
        step = self.step("linux", "python language step")
        self.assertEqual({NOT_CANCELLED, SHARD_ONE, PYTHON_TERM}, conjuncts(step["if"]))
        self.assertEqual(as_input(self.own_step("linux", "python language step")["if"]), step["if"])
        self.assertNotIn("env", step)
        self.assertNotIn("python language step", self.roles("<windows-job>"))

    def test_a_pull_request_uploads_the_entry_files_that_passed_for_the_reuse_of_later_heads(self) -> None:
        listing = self.step("linux", "passed list")
        self.assertEqual({NOT_CANCELLED, PULL_REQUEST, CHECK_PASSED_OR_ADDITIONS}, conjuncts(listing["if"]))
        self.assertEqual(self.own_step("linux", "passed list")["run"], listing["run"])
        self.assertEqual({"suite": "$suite", "entry": "$entry"}, passed_record(listing["run"]))
        windows_entries = self.step("<windows-job>", "entries")["run"]
        self.assertTrue(writes(windows_entries, "passed.json"))
        self.assertEqual({"suite": "$item.suite", "entry": "$item.entry"}, passed_record(windows_entries))
        for job, name in ((spec.name, spec.passed) for spec in JOBS):
            with self.subTest(job=job):
                upload = self.step(job, "upload")
                self.assertEqual({NOT_CANCELLED, PULL_REQUEST, CHECK_PASSED_OR_KEPT}, conjuncts(upload["if"]))
                self.assertEqual({"name": name, "path": "${{ runner.temp }}/suite-results/passed.json",
                                  "retention-days": "1", "if-no-files-found": "error"}, upload["with"])
                self.assertTrue(name.startswith("suite-results-"), "the verdict collects suite-results-*")

    def test_a_check_with_additions_still_uploads_the_passes_of_the_entry_files_that_its_additions_do_not_name(self) -> None:
        for job, shell in ((spec.name, spec.shell) for spec in JOBS):
            with self.subTest(job=job):
                keep = self.step(job, "keep")
                self.assertEqual("keep", keep["id"])
                self.assertEqual({NOT_CANCELLED, PULL_REQUEST, ADDITIONS}, conjuncts(keep["if"]))
                self.assertEqual({"RECORDING": RECORDING_PATH, "ADDITIONS": "${{ steps.check.outputs.additions }}"}, keep["env"])
                self.assertEqual(shell, keep.get("shell"))
                (call,) = recording_calls(keep["run"])
                self.assertEqual(["keep", "--additions", "$env:ADDITIONS" if shell else "$ADDITIONS"], call[:3])
                self.assertTrue(writes(keep["run"], "passed.json"), "keep rewrites the passed list that the upload sends")
                if shell:
                    self.assertIn("if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }", keep["run"])

    def test_a_check_that_passed_uploads_every_pass_one_with_additions_the_kept_passes_and_a_recording_error_none(self) -> None:
        listed = {"linux": ["List the entry files that passed"], "<windows-job>": []}
        upload = "Upload the entry files that passed"
        for job in self.jobs:
            for case, (outcome, additions, keep), expected in (
                    ("the check passed", ("success", "", "success"), [upload]),
                    ("additions", ("failure", "additions_json", "success"), ["keep", upload]),
                    ("additions whose keep failed", ("failure", "additions_json", "failure"), ["keep"]),
                    ("a recording error", ("failure", "", "success"), []),
                    ("no check", ("skipped", "", "success"), [])):
                with self.subTest(job=job, case=case):
                    ran = passed_list_steps(self.jobs[job]["steps"], outcome, additions, keep)
                    self.assertEqual((listed[job] if expected else []) + expected, ran)


# A stand-in for recording.py: `exec --entry ... -- <command> <entry>` runs the command and exits with its status.
RECORDING_STAND_IN = """\
import subprocess
import sys

arguments = sys.argv[1:]
assert arguments[:2] == ["exec", "--entry"], arguments
sys.exit(subprocess.call(arguments[arguments.index("--") + 1:]))
"""
# Two paths that flatten to the same name, and one entry file that runs for two suites; each exits with the status its
# command names and prints its own marker.
SHARD = {"shard": 1, "setups": [], "entries": [
    {"suite": "one", "entry": "a/b_c.py", "command": ["sh", "-c", "echo marker-one-a-b_c; exit 1"]},
    {"suite": "one", "entry": "a_b/c.py", "command": ["sh", "-c", "echo marker-one-a_b-c; exit 0"]},
    {"suite": "one", "entry": "x.py", "command": ["sh", "-c", "echo marker-one-x; exit 0"]},
    {"suite": "two", "entry": "x.py", "command": ["sh", "-c", "echo marker-two-x; exit 1"]},
]}


@unittest.skipUnless(sys.platform == "linux" and shutil.which("jq") and shutil.which("bash"), "the shard scripts need Linux for GNU xargs, bash and jq")
class ShardEntryRunTest(unittest.TestCase):
    """The shard job's step that runs the entry files, and in a validation run its step that lists those that passed,
    run as GitHub runs them: for the validation workflow template, validate.yml, the full-recording template and this
    repository's full recording."""

    ENTRIES_STEP = "Run the shard's entry files under the recording, two at a time"
    LISTING_STEP = "List the entry files that passed"

    def scripts(self) -> dict[str, tuple[str, str | None]]:
        """The script of each source's entry step and of its listing step, None for a full recording, which lists none."""
        sources = {"validation-workflow.yml template": (SELECTION_KIT / "validation-workflow.yml", self.LISTING_STEP),
                   "validate.yml": (OWN_VALIDATE, self.LISTING_STEP),
                   "full-recording.yml template": (SELECTION_KIT / "full-recording.yml", None),
                   "full-recording.yml": (REPOSITORY_ROOT / ".github" / "workflows" / "full-recording.yml", None)}
        scripts = {}
        for source, (path, listing) in sources.items():
            steps = {step.get("name"): step for step in read_workflow(path)[1]["jobs"]["linux"]["steps"]}
            scripts[source] = (steps[self.ENTRIES_STEP]["run"], steps[listing]["run"] if listing else None)
        return scripts

    def run_step(self, script: str, environment: dict[str, str], directory: Path) -> subprocess.CompletedProcess:
        path = directory / "step.sh"
        path.write_text(script, encoding="utf-8")
        return subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", str(path)], cwd=directory,
                              capture_output=True, text=True, env=environment)

    def test_each_entry_file_of_a_shard_keeps_its_own_status_and_log_group(self) -> None:
        for source, (entries, listing) in self.scripts().items():
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                (directory / "recording").mkdir()
                (directory / "recording" / "recording.py").write_text(RECORDING_STAND_IN, encoding="utf-8")
                (directory / "runner-temp").mkdir()
                environment = {**os.environ, "RECORDING": str(directory / "recording"), "RUNNER_TEMP": str(directory / "runner-temp"),
                               "SHARD": json.dumps(SHARD)}
                ran = self.run_step(entries, environment, directory)
                self.assertEqual(1, ran.returncode, ran.stdout + ran.stderr)
                groups = re.findall(r"::group::(.*)\n(.*)\n::endgroup::", ran.stdout)
                self.assertEqual([("a/b_c.py (one)", "marker-one-a-b_c"), ("a_b/c.py (one)", "marker-one-a_b-c"),
                                  ("x.py (one)", "marker-one-x"), ("x.py (two)", "marker-two-x")], groups)
                self.assertEqual(["a/b_c.py (one)", "x.py (two)"], re.findall(r"::error::(.*) failed", ran.stdout),
                                 "each entry file fails on its own status")
                if listing is None:
                    continue
                listed = self.run_step(listing, environment, directory)
                self.assertEqual(0, listed.returncode, listed.stdout + listed.stderr)
                self.assertEqual([{"suite": "one", "entry": "a_b/c.py"}, {"suite": "one", "entry": "x.py"}],
                                 json.loads((directory / "runner-temp" / "suite-results" / "passed.json").read_text(encoding="utf-8")))


class SuiteJobDefinitionTest(unittest.TestCase):
    """The selection of unrecorded inputs recognises each job of the template as a suite job, since it uses the recording
    step. Neither job gates on a suite, so the parts of either job belong to every entry file: a change to a step of the
    validation workflow runs every suite."""

    def test_each_job_is_a_suite_job_that_runs_every_unit(self) -> None:
        selection_tool = str(REPOSITORY_ROOT / "actions" / "suite-selection")
        sys.path.insert(0, selection_tool)
        self.addCleanup(sys.path.remove, selection_tool)
        import unrecorded

        text = PLACEHOLDER.sub(lambda match: match.group(0)[1:-1], (SELECTION_KIT / "validation-workflow.yml").read_text(encoding="utf-8"))
        jobs = unrecorded.ci_definition.read_yaml(text, "validation-workflow.yml")["jobs"]
        units = [unrecorded.EntryUnit(unrecorded.EntryFile(suite, f"tests/test_{suite}.py"), system, ())
                 for suite, system in (("one", "linux"), ("two", "linux"), ("three", "windows"))]
        self.assertEqual(["linux", "windows-job"], list(jobs))
        for name, job in jobs.items():
            with self.subTest(job=name):
                self.assertTrue(unrecorded.is_suite_job(job))
                self.assertEqual(units, unrecorded.job_units(job, units))


if __name__ == "__main__":
    unittest.main()
