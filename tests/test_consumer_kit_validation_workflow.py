"""The validation workflow template: suite jobs that run their setups and entry files under the recording step."""

from __future__ import annotations

import re
import shlex
import sys
import unittest
from pathlib import Path
from tests.ci_policy_support import ci_definition, passed_list_steps, read_workflow

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SELECTION_KIT = REPOSITORY_ROOT / "actions" / "suite-selection" / "consumer-kit"
RECORDING = "CrankyAnt/SharedWorkflows/actions/recording@v1"
PYTHON_LANGUAGE_STEP = "CrankyAnt/SharedWorkflows/actions/python-language-step@v1"
RECORDING_PATH = "${{ steps.recording.outputs.path }}"
SUITE_GATE = "fromJSON(inputs.suites).<suite> == 'run'"
DISPATCH_REF = "${{ github.event_name == 'repository_dispatch' && github.event.client_payload.sha || '' }}"
NOT_CANCELLED = "!cancelled()"
PULL_REQUEST = "github.event_name == 'pull_request'"
CHECK_PASSED_OR_ADDITIONS = "(steps.check.outcome == 'success' || steps.check.outputs.additions != '')"
ADDITIONS = "steps.check.outputs.additions != ''"
CHECK_PASSED_OR_KEPT = "(steps.check.outcome == 'success' || steps.keep.outcome == 'success')"
STARTED = "steps.start.outcome == 'success'"
PYTHON_TERM = "fromJSON(inputs.language-steps).python == 'run'"
PLACEHOLDER = re.compile(r"<[a-z][a-z-]*>")
JOBS = (("<job>", "<suite>", "<setup>", None), ("<windows-job>", "<windows-suite>", "<windows-setup>", "pwsh"))


def suite_gate(suite: str) -> str:
    return f"fromJSON(inputs.suites).{suite} == 'run'"


def gate_terms(condition: str) -> list[str]:
    return [term.strip() for term in condition.split("||")]


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
    """What a step does, read from what it calls or uses rather than from its name."""
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
    raise AssertionError(f"a step of unknown role: {step}")


class ValidationWorkflowTemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(SELECTION_KIT / "validation-workflow.yml")
        self.jobs = self.workflow["jobs"]

    def roles(self, job: str) -> list[str]:
        return [role(step) for step in self.jobs[job]["steps"]]

    def step(self, job: str, name: str) -> dict:
        (found,) = [step for step in self.jobs[job]["steps"] if role(step) == name]
        return found

    def test_only_the_validation_caller_starts_it_with_the_selection_as_its_inputs(self) -> None:
        self.assertEqual({"workflow_call": {"inputs": {"suites": {"type": "string", "required": "true"},
                                                       "entries": {"type": "string", "required": "false"},
                                                       "language-steps": {"type": "string", "required": "false"},
                                                       "shared-digest": {"type": "string", "required": "true"}}}}, self.workflow["on"])
        self.assertEqual("{}", self.workflow["permissions"])
        self.assertEqual(["<job>", "<windows-job>"], list(self.jobs))

    def test_each_suite_job_carries_its_suite_s_gate_and_reads_the_contents_only(self) -> None:
        for job, suite, _, _ in JOBS:
            with self.subTest(job=job):
                self.assertIn(suite_gate(suite), gate_terms(self.jobs[job]["if"]))
                self.assertEqual({"contents": "read"}, self.jobs[job]["permissions"])
                self.assertRegex(self.jobs[job]["runs-on"], PLACEHOLDER)

    def test_the_job_of_the_python_language_step_also_runs_whenever_the_step_is_selected(self) -> None:
        self.assertEqual([suite_gate("<suite>"), PYTHON_TERM], gate_terms(self.jobs["<job>"]["if"]))
        self.assertNotIn(PYTHON_TERM, gate_terms(self.jobs["<windows-job>"]["if"]))
        for name in ("start", "setup", "entries"):
            with self.subTest(step=name):
                self.assertEqual([suite_gate("<suite>")], gate_terms(self.step("<job>", name)["if"]))

    def test_the_ci_definition_reader_attributes_each_job_to_its_suite_alone(self) -> None:
        reader = ci_definition()
        concrete = PLACEHOLDER.sub(lambda match: match.group(0)[1:-1], self.text)
        for job, before, after in (
            ("job", "-- setup-command", "-- setup-command --changed"),
            ("job", "actions/python-language-step@v1", "actions/python-language-step@v2"),
            ("windows-job", "-- windows-setup-command", "-- windows-setup-command --changed"),
        ):
            with self.subTest(job=job, change=after):
                self.assertEqual(1, concrete.count(before))
                gate = reader.changed_step_gate(concrete, concrete.replace(before, after))
                self.assertEqual({"job": {"suite"}, "windows-job": {"windows-suite"}}[job], set(gate.suites))

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

    def test_the_checkout_validates_the_commit_of_a_dependabot_merge(self) -> None:
        for job in self.jobs:
            with self.subTest(job=job):
                self.assertEqual({"ref": DISPATCH_REF, "persist-credentials": "false"}, self.step(job, "checkout")["with"])

    def test_the_steps_run_in_order_with_the_shared_actions_at_v1(self) -> None:
        recorded = ["checkout", "recording path", "start", "setup", "entries", "check"]
        python = ["checkout", "recording path", "python setup", "start", "python provide", "setup", "entries", "check"]
        self.assertEqual(python + ["python language step", "passed list", "keep", "upload"], self.roles("<job>"))
        self.assertEqual(recorded + ["keep", "upload"], self.roles("<windows-job>"))
        for job in self.jobs:
            with self.subTest(job=job):
                self.assertEqual({"id": "recording", "uses": RECORDING, "with": {"digest": "${{ inputs.shared-digest }}"}},
                                 self.step(job, "recording path"), "the first step after the checkout fails on a digest that select did not take")

    def test_the_setups_and_the_entry_files_run_under_the_recording_after_its_start(self) -> None:
        for job, suite, setup, shell in JOBS:
            with self.subTest(job=job):
                start, setup_step, entries = (self.step(job, name) for name in ("start", "setup", "entries"))
                self.assertEqual("start", start["id"])
                setup_call, provide_call = recording_calls(setup_step["run"])
                self.assertEqual(["exec", "--setup", setup, "--"], setup_call[:4])
                self.assertRegex(" ".join(setup_call[4:]), PLACEHOLDER, "the setup's own command follows")
                self.assertEqual(["provide", "--setup", setup], provide_call[:3])
                self.assertRegex(" ".join(provide_call[3:]), PLACEHOLDER, "what the setup provides to the entry files that state it follows")
                (entry_call,) = recording_calls(entries["run"])
                self.assertEqual(["exec", "--entry", "$entry", "--suites", ".github/suites.json", "--suite", suite, "--"], entry_call[:8])
                self.assertEqual("$entry", entry_call[-1], "the entry file follows the suite's command")
                self.assertRegex(" ".join(entry_call[8:-1]), PLACEHOLDER, "the suite's command runs the entry file")
                self.assertRegex(entries["env"]["ENTRIES"], PLACEHOLDER)
                for step in (start, setup_step, entries):
                    self.assertEqual(RECORDING_PATH, step["env"]["RECORDING"])
                    self.assertEqual(shell, step.get("shell"))

    def test_the_set_up_python_gets_the_lookup_report_in_its_site_directory(self) -> None:
        python, provide = self.step("<job>", "python setup"), self.step("<job>", "python provide")
        self.assertEqual({"python-version": "<python-version>", "update-environment": "false"}, python["with"])
        self.assertEqual(SUITE_GATE, python["if"])
        self.assertEqual(SUITE_GATE, provide["if"])
        self.assertEqual({"RECORDING": RECORDING_PATH, "PYTHON_PATH": f"${{{{ steps.{python['id']}.outputs.python-path }}}}"}, provide["env"])
        (call,) = recording_calls(provide["run"])
        self.assertEqual(["provide", "--setup", "python", "--path", "$(dirname $PYTHON_PATH)", "--python", "$PYTHON_PATH"], call)

    def test_check_compares_the_recording_with_the_suite_list_whenever_the_recording_started(self) -> None:
        for job in self.jobs:
            with self.subTest(job=job):
                check = self.step(job, "check")
                self.assertEqual("check", check["id"])
                self.assertEqual({NOT_CANCELLED, STARTED}, conjuncts(check["if"]))
                self.assertEqual({"check": "true", "suites": ".github/suites.json", "artifact-name": f"recording-additions-{job}"}, check["with"])

    def test_the_python_language_step_runs_after_check_outside_the_recording_when_the_selection_runs_it(self) -> None:
        step = self.step("<job>", "python language step")
        self.assertEqual({NOT_CANCELLED, PYTHON_TERM}, conjuncts(step["if"]))
        self.assertNotIn("env", step)

    def test_a_pull_request_uploads_the_entry_files_that_passed_for_the_reuse_of_later_heads(self) -> None:
        self.assertTrue(writes(self.step("<job>", "entries")["run"], "passed.jsonl"))
        listing = self.step("<job>", "passed list")
        self.assertEqual({NOT_CANCELLED, PULL_REQUEST, CHECK_PASSED_OR_ADDITIONS}, conjuncts(listing["if"]))
        self.assertTrue(writes(listing["run"], "passed.jsonl") and writes(listing["run"], "passed.json"))
        self.assertTrue(writes(self.step("<windows-job>", "entries")["run"], "passed.json"))
        for job, suite, _, _ in JOBS:
            with self.subTest(job=job):
                self.assertEqual({"suite": suite, "entry": "$entry"}, passed_record(self.step(job, "entries")["run"]))
                upload = self.step(job, "upload")
                self.assertEqual({NOT_CANCELLED, PULL_REQUEST, CHECK_PASSED_OR_KEPT}, conjuncts(upload["if"]))
                self.assertEqual({"name": f"suite-results-{job}", "path": "${{ runner.temp }}/suite-results/passed.json",
                                  "retention-days": "1", "if-no-files-found": "error"}, upload["with"])

    def test_a_check_with_additions_still_uploads_the_passes_of_the_entry_files_that_its_additions_do_not_name(self) -> None:
        for job, _, _, shell in JOBS:
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
        listed = {"<job>": ["List the entry files that passed"], "<windows-job>": []}
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


class SuiteJobDefinitionTest(unittest.TestCase):
    """A suite job is a job whose gate names suites or entry files; the selection of unrecorded inputs recognises it as a
    job that uses the recording step. In the template the two agree, and each such job runs the units of its gate."""

    def test_the_jobs_that_use_the_recording_step_are_those_whose_gate_names_suites(self) -> None:
        selection_tool = str(REPOSITORY_ROOT / "actions" / "suite-selection")
        sys.path.insert(0, selection_tool)
        self.addCleanup(sys.path.remove, selection_tool)
        import unrecorded

        text = PLACEHOLDER.sub(lambda match: match.group(0)[1:-1], (SELECTION_KIT / "validation-workflow.yml").read_text(encoding="utf-8"))
        jobs = unrecorded.ci_definition.read_yaml(text, "validation-workflow.yml")["jobs"]
        units = [unrecorded.EntryUnit(unrecorded.EntryFile(suite, f"tests/test_{suite}.py"), system, ())
                 for suite, system in (("suite", "linux"), ("windows-suite", "windows"))]
        self.assertTrue(jobs)
        for name, job in jobs.items():
            with self.subTest(job=name):
                gate = unrecorded.ci_definition.condition_gate(job.get("if"))
                self.assertEqual(bool(gate.suites), unrecorded.is_suite_job(job))
                self.assertEqual([unit for unit in units if unit.file.suite in gate.suites], unrecorded.job_units(job, units))


if __name__ == "__main__":
    unittest.main()
