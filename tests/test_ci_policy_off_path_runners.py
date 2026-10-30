"""The off-path maintenance workflows run on ubuntu-slim."""

from __future__ import annotations

import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT


PROMOTER = REPOSITORY_ROOT / ".github" / "workflows" / "promote-v1.yml"
PINS_UPDATER = REPOSITORY_ROOT / ".github" / "workflows" / "update-ci-pins.yml"
SPEC_UPDATER = REPOSITORY_ROOT / ".github" / "workflows" / "update-publication-spec.yml"


def runners(workflow: str) -> dict[str, str]:
    """Each job of ``workflow`` with its ``runs-on`` label."""
    body = workflow[workflow.index("\njobs:\n"):]
    jobs = re.split(r"(?m)^  ([A-Za-z0-9_-]+):\n", body)[1:]
    return {name: re.search(r"(?m)^    runs-on: (.+)$", text).group(1) for name, text in zip(jobs[::2], jobs[1::2])}  # type: ignore[union-attr]


class OffPathRunnerTests(unittest.TestCase):
    def test_every_job_of_the_off_path_workflows_runs_on_ubuntu_slim(self) -> None:
        expected = {
            "promote-v1.yml": (PROMOTER, {"decide": "ubuntu-slim", "move": "ubuntu-slim"}),
            "update-ci-pins.yml": (PINS_UPDATER, {"plan": "ubuntu-slim", "propose": "ubuntu-slim"}),
            "update-publication-spec.yml": (SPEC_UPDATER, {"plan": "ubuntu-slim", "propose": "ubuntu-slim"}),
        }
        for name, (path, jobs) in expected.items():
            with self.subTest(workflow=name):
                self.assertEqual(jobs, runners(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
