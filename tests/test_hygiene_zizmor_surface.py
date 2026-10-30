from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.hygiene_fixtures import REPOSITORY_ROOT, fixture_repository, tools_available
from tests.hygiene_process import hygiene
from tests.zizmor_fixtures import CALLER_HEAD, CHECKOUT_TAG, WORKFLOW_HEAD


@unittest.skipUnless(tools_available("zizmor"), "the pinned tools are neither in HYGIENE_TOOLS nor on PATH")
class SharedPolicySurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_without_repository_configuration_the_shared_policy_allows_v1_callers_and_actions_tags_only(self) -> None:
        surface = json.loads((REPOSITORY_ROOT / "actions" / "repository-hygiene" / "shared-surface.json").read_text(encoding="utf-8"))
        shared_workflows = tuple(path for path in surface["paths"] if path.startswith(".github/workflows/"))
        allowed = fixture_repository(self.root / "allowed", {
            ".github/workflows/caller.yml": CALLER_HEAD + "".join(
                f"  shared_{index}:\n    permissions:\n      contents: read\n    uses: CrankyAnt/SharedWorkflows/{workflow}@v1\n"
                for index, workflow in enumerate(shared_workflows)
            ),
            ".github/workflows/steps.yml": WORKFLOW_HEAD + CHECKOUT_TAG,
        })
        result = hygiene("zizmor", allowed)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("shared policy", result.stdout)
        cases = {
            "third-party-tag": "fixture/action@v1",
            "own-repository-action": "CrankyAnt/SharedWorkflows/action@v1",
            "own-repository-workflow": "CrankyAnt/SharedWorkflows/.github/workflows/unrelated.yml@v1",
        }
        for name, reference in cases.items():
            with self.subTest(reference=reference):
                workflow = (
                    CALLER_HEAD + f"  shared:\n    permissions:\n      contents: read\n    uses: {reference}\n"
                    if name == "own-repository-workflow"
                    else WORKFLOW_HEAD + f"      - uses: {reference}\n"
                )
                rejected = fixture_repository(self.root / name, {".github/workflows/steps.yml": workflow})
                result = hygiene("zizmor", rejected)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("unpinned-uses", result.stdout + result.stderr)
                self.assertIn(reference, result.stdout + result.stderr)
        pinned = fixture_repository(self.root / "pinned", {".github/workflows/steps.yml": WORKFLOW_HEAD + "      - uses: fixture/action@3d3c42e5aac5ba805825da76410c181273ba90b1\n"})
        result = hygiene("zizmor", pinned)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
