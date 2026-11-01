from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.hygiene_fixtures import REPOSITORY_ROOT, fixture_repository, tools_available
from tests.hygiene_process import hygiene
from tests.zizmor_fixtures import CALLER_HEAD, CHECKOUT_BARE_SHA, CHECKOUT_SHA, CHECKOUT_TAG, WORKFLOW_HEAD, caller_job


@unittest.skipUnless(tools_available("zizmor"), "the pinned tools are neither in HYGIENE_TOOLS nor on PATH")
class SharedPolicySurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_without_repository_configuration_the_shared_policy_allows_moving_refs_for_v1_callers_only(self) -> None:
        surface = json.loads((REPOSITORY_ROOT / "actions" / "repository-hygiene" / "shared-surface.json").read_text(encoding="utf-8"))
        shared_workflows = tuple(path for path in surface["paths"] if path.startswith(".github/workflows/"))
        allowed = fixture_repository(self.root / "allowed", {
            ".github/workflows/caller.yml": CALLER_HEAD + "".join(
                caller_job(f"shared_{index}", f"CrankyAnt/SharedWorkflows/{workflow}@v1")
                for index, workflow in enumerate(shared_workflows)
            ),
        })
        result = hygiene("zizmor", allowed)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("shared policy", result.stdout)
        unrelated_workflow = "CrankyAnt/SharedWorkflows/.github/workflows/unrelated.yml@v1"
        cases = {
            "actions/checkout@v7": ("actions-tag", WORKFLOW_HEAD + CHECKOUT_TAG),
            "fixture/action@v1": ("third-party-tag", WORKFLOW_HEAD + "      - uses: fixture/action@v1\n"),
            "CrankyAnt/SharedWorkflows/action@v1": ("own-repository-action", WORKFLOW_HEAD + "      - uses: CrankyAnt/SharedWorkflows/action@v1\n"),
            unrelated_workflow: ("own-repository-workflow", CALLER_HEAD + caller_job("shared", unrelated_workflow)),
        }
        for reference, (name, workflow) in cases.items():
            with self.subTest(reference=reference):
                rejected = fixture_repository(self.root / name, {".github/workflows/steps.yml": workflow})
                result = hygiene("zizmor", rejected)
                self.assertNotEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("unpinned-uses", result.stdout + result.stderr)
                self.assertIn(reference, result.stdout + result.stderr)
        pinned_cases = {
            "pinned-third-party": WORKFLOW_HEAD + "      - uses: fixture/action@3d3c42e5aac5ba805825da76410c181273ba90b1\n",
            "pinned-actions-with-version-comment": WORKFLOW_HEAD + CHECKOUT_SHA,
            "pinned-actions-without-version-comment": WORKFLOW_HEAD + CHECKOUT_BARE_SHA,
        }
        for name, workflow in pinned_cases.items():
            with self.subTest(pinned=name):
                pinned = fixture_repository(self.root / name, {".github/workflows/steps.yml": workflow})
                result = hygiene("zizmor", pinned)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertIn("shared policy", result.stdout)
