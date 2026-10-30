"""The v1 promotion and pin updater use the named shared hygiene surface."""

from __future__ import annotations

import json
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT


PROMOTER = REPOSITORY_ROOT / ".github" / "workflows" / "promote-v1.yml"
UPDATER = REPOSITORY_ROOT / ".github" / "workflows" / "update-ci-pins.yml"
SURFACE = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "shared-surface.json"


class HygienePromotionSurfaceTests(unittest.TestCase):
    def test_the_promotion_and_the_updater_name_the_pins_file_and_the_updater_branch_of_the_shared_surface(self) -> None:
        surface = json.loads(SURFACE.read_text(encoding="utf-8"))
        self.assertEqual("actions/repository-hygiene/pins.json", surface["pins"])
        promoter = PROMOTER.read_text(encoding="utf-8")
        self.assertIn(f"    paths: [{surface['pins']}]\n", promoter, "only a push that changes the pins file starts the promotion")
        self.assertIn(f'"{surface["pins"]} ") candidate=true ;;', promoter, "the promotion pre-filter names the pins file of the shared surface")
        updater = UPDATER.read_text(encoding="utf-8")
        self.assertIn(f"{surface['pins']}) ;;", updater)
        self.assertIn(f"git add {surface['pins']}", updater)
        self.assertEqual(2, updater.count(f'branch="{surface["updater_branch"]}"'))
