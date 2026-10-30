"""The recording step's composite action runs check, uploads its additions as an artifact and then fails the job."""

from __future__ import annotations

import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, action_pin_pattern

ACTION = REPOSITORY_ROOT / "actions" / "recording" / "action.yml"


def steps(text: str) -> list[str]:
    """The text of each step under ``runs.steps``, in order."""
    body = text.split("\n  steps:\n", 1)[1]
    return [chunk for chunk in re.split(r"\n(?=    - )", "\n" + body) if chunk.strip()]


class RecordingActionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = ACTION.read_text(encoding="utf-8")
        self.steps = steps(self.text)

    def test_it_is_a_composite_action_that_exposes_the_recording_command(self) -> None:
        self.assertIn("\nruns:\n  using: composite\n", self.text)
        self.assertIn("value: ${{ steps.resources.outputs.path }}", self.text)
        self.assertIn('printf \'path=%s\\n\' "$RESOURCE_PATH" >> "$GITHUB_OUTPUT"', self.steps[0])

    def test_a_digest_that_differs_from_the_one_select_took_fails_the_job_before_anything_runs(self) -> None:
        self.assertRegex(self.text, r'(?m)^  digest:\n    description: .*\n    required: false\n    default: ""$')
        digest = self.steps[1]
        self.assertIn("id: digest", digest)
        self.assertIn("if: inputs.digest != ''", digest)
        self.assertIn("DIGEST: ${{ inputs.digest }}", digest)
        self.assertIn("PYTHON: ${{ runner.os == 'Windows' && 'python' || 'python3' }}", digest)
        self.assertIn('PYTHONPYCACHEPREFIX="$RUNNER_TEMP/repo-pycache" "$PYTHON" "$RESOURCE_PATH/recording.py" digest --expect "$DIGEST"', digest)

    def test_check_records_its_status_without_stopping_the_action(self) -> None:
        check = self.steps[2]
        self.assertIn("id: check", check)
        self.assertIn("if: inputs.check == 'true'", check)
        self.assertIn('"$PYTHON" "$RESOURCE_PATH/recording.py" check --suites "$SUITES" --summary "$GITHUB_STEP_SUMMARY" '
                      '--github-output "$GITHUB_OUTPUT" || status=$?', check)
        self.assertIn("PYTHON: ${{ runner.os == 'Windows' && 'python' || 'python3' }}", check)
        self.assertIn("set -uo pipefail", check)
        self.assertIn("printf 'status=%s\\n' \"$status\" >> \"$GITHUB_OUTPUT\"", check)

    def test_the_additions_are_uploaded_as_an_artifact_before_the_job_fails(self) -> None:
        upload, fail = self.steps[3], self.steps[4]
        self.assertRegex(upload, action_pin_pattern("actions/upload-artifact").replace(r"^[ \t]*- uses: ", r"^[ \t]*uses: "))
        self.assertIn("if: inputs.check == 'true' && steps.check.outputs.file != ''", upload)
        self.assertIn("path: ${{ steps.check.outputs.file }}", upload)
        self.assertIn("name: ${{ inputs.artifact-name }}", upload)
        self.assertIn("if: inputs.check == 'true' && steps.check.outputs.status != '0'", fail)
        self.assertIn('exit "$STATUS"', fail)
        self.assertEqual(7, len(self.steps))

    def test_the_first_call_writes_the_job_s_environment_before_any_setup_with_the_job_s_own_interpreter(self) -> None:
        linux, windows = self.steps[5], self.steps[6]
        self.assertIn("if: inputs.check != 'true' && runner.os != 'Windows'", linux)
        self.assertIn('run: python3 "$RESOURCE_PATH/recording.py" base', linux)
        self.assertIn("if: inputs.check != 'true' && runner.os == 'Windows'", windows)
        self.assertIn("shell: pwsh", windows, "a Git Bash step would hand its own variables to every entry file")
        self.assertIn('run: python "$env:RESOURCE_PATH\\recording.py" base', windows)


if __name__ == "__main__":
    unittest.main()
