"""The pinned tools that the suite jobs give: this entry file states the pinned tool ``gitleaks`` in a Linux suite and in a
Windows suite, and runs the Gitleaks that its job installed from the pins file. Outside a suite job it has no pinned tool
to run and skips."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

PINS = Path(__file__).resolve().parents[1] / "actions" / "repository-hygiene" / "pins.json"
ON_A_HOSTED_RUNNER = os.environ.get("GITHUB_ACTIONS") == "true" and bool(os.environ.get("RUNNER_TEMP"))


@unittest.skipUnless(ON_A_HOSTED_RUNNER, "only a suite job on a hosted runner gives the pinned tools")
class PinnedGitleaksTests(unittest.TestCase):
    def test_the_job_gives_the_gitleaks_of_the_pins_file_first_on_the_path(self) -> None:
        found = shutil.which("gitleaks")
        self.assertIsNotNone(found, "the entry file states the pinned tool gitleaks, so its job installs it")
        installed = Path(os.environ["RUNNER_TEMP"]) / "setups" / "gitleaks"
        self.assertEqual(installed.resolve(), Path(str(found)).resolve().parent, "the pinned tool comes from the job's own directory for it")
        version = subprocess.run([str(found), "version"], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(json.loads(PINS.read_text(encoding="utf-8"))["tools"]["gitleaks"]["version"], version.removeprefix("v"))


if __name__ == "__main__":
    unittest.main()
