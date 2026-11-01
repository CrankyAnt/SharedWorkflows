"""Dependency detection requires the installed Dependabot files."""

from __future__ import annotations

import subprocess
import unittest
from pathlib import Path
from tests.ci_policy_support import shared_workflows, tracked_workflows, uses_values
from tests.consumer_kit_support import coverage
from tests.consumer_kit_support import read_yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

AUTO_MERGE_KIT = REPOSITORY_ROOT / "actions" / "dependabot-auto-merge" / "consumer-kit"

OWN_WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
OWN_CALLERS = ("dependabot-auto-merge-caller.yml", "repository-hygiene-caller.yml")
OWN_DEPENDABOT = REPOSITORY_ROOT / ".github" / "dependabot.yml"
HYGIENE_RESOURCES = REPOSITORY_ROOT / "actions" / "repository-hygiene"


def template_job(template: str) -> dict:
    _, caller = read_yaml(AUTO_MERGE_KIT / template)
    (job,) = caller["jobs"].values()
    return job


def local_path_callers() -> dict[str, dict]:
    """Each job of this repository's own callers that calls a workflow by local path, keyed by the called path."""
    callers: dict[str, dict] = {}
    for name in OWN_CALLERS:
        path = OWN_WORKFLOWS / name
        _, workflow = read_yaml(path)
        for job in workflow["jobs"].values():
            uses = job.get("uses", "")
            if uses.startswith("$/.github/workflows/"):
                callers[uses.removeprefix("$/")] = job
    return callers


def own_dependencies() -> list[str]:
    """This repository's dependencies of its own as install by need counts them.

    A recognised package file, or a `uses:` reference of its shared workflows outside the repository (`./` and `$/`
    stay inside it) and outside CrankyAnt/SharedWorkflows. This repository names no package file as a test fixture.
    """
    ecosystems = coverage.load_ecosystems(HYGIENE_RESOURCES / "package-ecosystems.json")
    pathspecs = [f":(glob)**/{pattern}" for patterns in ecosystems.values() for pattern in patterns]
    listing = subprocess.run(["git", "ls-files", "-z", "--", *pathspecs], cwd=REPOSITORY_ROOT, capture_output=True, text=True,
                             encoding="utf-8", check=True)
    found = [path for path in filter(None, listing.stdout.split("\0")) if coverage.ecosystem_of(path, ecosystems)]
    workflows = tracked_workflows(REPOSITORY_ROOT)
    for path in shared_workflows(workflows):
        for reference in uses_values(workflows[path], path):
            if not reference.startswith(("./", "$/", "CrankyAnt/SharedWorkflows/")):
                found.append(f"{Path(path).name}: {reference}")
    return found


class InstallByNeedTest(unittest.TestCase):
    def test_this_repository_has_a_dependency_of_its_own_and_carries_both_files_that_come_with_one(self) -> None:
        self.assertTrue(own_dependencies())
        self.assertTrue(OWN_DEPENDABOT.is_file())
        target = template_job("dependabot-auto-merge.yml")["uses"].removeprefix("CrankyAnt/SharedWorkflows/")
        self.assertIn(target.removesuffix("@v1"), local_path_callers())


if __name__ == "__main__":
    unittest.main()
