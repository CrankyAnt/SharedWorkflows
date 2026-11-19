"""The shard steps that run the entry files and list those that passed, the suite-job definition and the passed record, on
the shared suite jobs `suite-jobs.yml` that replaced the validation workflow template of the suite selection's kit.

The template's shape checks (its steps against this repository's `validate.yml`, its placeholders and its inputs) left
with the template. The properties of the shared workflow are checked by its own contract tests, not here."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tests import temporary_support
from tests.ci_policy_support import flow_items, read_workflow

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SUITE_JOBS = REPOSITORY_ROOT / ".github" / "workflows" / "suite-jobs.yml"
ENTRIES_STEP = "Run the shard's entry files under the recording, two at a time"
LISTING_STEP = "List the entry files that passed"

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
    """The shard job's step that runs the entry files, and its step that lists those that passed, run as GitHub runs them,
    on the suite jobs of the shared workflow for every run, a full recording included."""

    def script(self, step: str) -> str:
        steps = {item.get("name"): item for item in read_workflow(SUITE_JOBS)[1]["jobs"]["linux"]["steps"]}
        return steps[step]["run"]

    def run_step(self, script: str, environment: dict[str, str], directory: Path) -> subprocess.CompletedProcess:
        path = directory / "step.sh"
        path.write_text(script, encoding="utf-8")
        return subprocess.run(["bash", "--noprofile", "--norc", "-e", "-o", "pipefail", str(path)], cwd=directory,
                              capture_output=True, text=True, env=environment)

    def test_each_entry_file_of_a_shard_keeps_its_own_status_and_log_group_and_the_passed_list_names_the_passes(self) -> None:
        entries, listing = self.script(ENTRIES_STEP), self.script(LISTING_STEP)
        with temporary_support.Directory() as temporary:
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
            listed = self.run_step(listing, environment, directory)
            self.assertEqual(0, listed.returncode, listed.stdout + listed.stderr)
            self.assertEqual([{"suite": "one", "entry": "a_b/c.py"}, {"suite": "one", "entry": "x.py"}],
                             json.loads((directory / "runner-temp" / "suite-results" / "passed.json").read_text(encoding="utf-8")))


class SuiteJobDefinitionTest(unittest.TestCase):
    """Each job of the shared suite jobs is a suite job in the sense of `actions/suite-selection/unrecorded.py`, since each
    uses the recording step. Which units a job runs is not checked here: `job_units` reads the `shards` and `windows`
    outputs of `select` only, while the shared workflow reads them as its inputs, so it counts every unit for each job."""

    def test_each_job_of_the_shared_suite_jobs_is_a_suite_job(self) -> None:
        selection_tool = str(REPOSITORY_ROOT / "actions" / "suite-selection")
        sys.path.insert(0, selection_tool)
        self.addCleanup(sys.path.remove, selection_tool)
        import unrecorded

        jobs = unrecorded.ci_definition.read_yaml(SUITE_JOBS.read_text(encoding="utf-8"), "suite-jobs.yml")["jobs"]
        self.assertEqual(["linux", "windows"], list(jobs))
        for name, job in jobs.items():
            with self.subTest(job=name):
                self.assertTrue(unrecorded.is_suite_job(job))


class PassedRecordTest(unittest.TestCase):
    def test_a_quoted_value_with_a_comma_in_the_jq_object_program_is_returned_whole(self) -> None:
        script = """jq -n --arg a "$suite" '{entry: $a, note: "x,y", braces: "{a, b}"}' >> passed.json"""
        self.assertEqual({"entry": "$suite", "note": "x,y", "braces": "{a, b}"}, passed_record(script))


def passed_record(script: str) -> dict[str, str]:
    """The fields of the record a script writes for an entry file that passed: a jq object built with ``--arg``, where a
    quoted value is a literal and a ``$name`` value the shell variable it names."""
    for line in script.splitlines():
        lexer = shlex.shlex(line, posix=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
        if tokens[:1] == ["jq"] and "--arg" in tokens:
            arguments = {tokens[index + 1]: tokens[index + 2] for index, token in enumerate(tokens) if token == "--arg"}
            program = next(token for token in tokens if token.startswith("{") and token.endswith("}"))
            fields = {}
            for item in flow_items(program[1:-1]):
                key, value = (part.strip() for part in item.split(":", 1))
                fields[key] = arguments[value[1:]] if value.startswith("$") else value.strip('"')
            return fields
    raise AssertionError("the script writes no passed record")


if __name__ == "__main__":
    unittest.main()
