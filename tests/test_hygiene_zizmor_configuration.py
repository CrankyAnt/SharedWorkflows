from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.hygiene_fixtures import REPOSITORY_ROOT, fixture_repository, tools_available, tools_directory
from tests.zizmor_fixtures import CALLER_HEAD, WORKFLOW_HEAD, caller_job


HYGIENE = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "hygiene.py"
SHARED_POLICY = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "zizmor-policy.yml"
SHARED_WORKFLOW_V1 = "CrankyAnt/SharedWorkflows/.github/workflows/repository-hygiene.yml@v1"


def configured_hygiene(root: Path, policy: Path) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(HYGIENE), "zizmor", "--root", str(root), "--policy", str(policy)]
    directory = tools_directory()
    if directory is not None:
        command += ["--tools", str(directory)]
    return subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)


@unittest.skipUnless(tools_available("zizmor"), "the pinned tools are neither in HYGIENE_TOOLS nor on PATH")
class ConsumerConfigurationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_a_repository_zizmor_configuration_replaces_the_shared_policy(self) -> None:
        unused_policy = self.root / "unused-policy.yml"
        own = fixture_repository(self.root / "own", {
            ".github/workflows/steps.yml": WORKFLOW_HEAD + "      - uses: fixture/action@v1\n",
            ".github/zizmor.yml": 'rules:\n  unpinned-uses:\n    config:\n      policies:\n        "fixture/*": ref-pin\n        "*": hash-pin\n',
        })
        result = configured_hygiene(own, unused_policy)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("using the repository's zizmor configuration .github/zizmor.yml", result.stdout)
        shared_caller = CALLER_HEAD + caller_job("shared", SHARED_WORKFLOW_V1)
        lenient = fixture_repository(self.root / "lenient", {".github/workflows/caller.yml": shared_caller})
        result = configured_hygiene(lenient, SHARED_POLICY)
        self.assertEqual(0, result.returncode, "the shared policy accepts a shared workflow at v1\n" + result.stdout + result.stderr)
        self.assertIn("using the shared policy", result.stdout)
        strict = fixture_repository(self.root / "strict", {
            ".github/workflows/caller.yml": shared_caller,
            ".github/zizmor.yml": 'rules:\n  unpinned-uses:\n    config:\n      policies:\n        "*": hash-pin\n',
        })
        result = configured_hygiene(strict, SHARED_POLICY)
        self.assertNotEqual(0, result.returncode, "the repository's stricter policy must apply")
        self.assertIn("unpinned-uses", result.stdout + result.stderr)
        self.assertIn(SHARED_WORKFLOW_V1, result.stdout + result.stderr)
        yaml_extension = fixture_repository(self.root / "yaml", {
            ".github/workflows/caller.yml": shared_caller,
            ".github/zizmor.yaml": 'rules:\n  unpinned-uses:\n    config:\n      policies:\n        "*": hash-pin\n',
        })
        result = configured_hygiene(yaml_extension, unused_policy)
        self.assertNotEqual(0, result.returncode, "a .yaml configuration applies as well")
        self.assertIn("using the repository's zizmor configuration .github/zizmor.yaml", result.stdout)
