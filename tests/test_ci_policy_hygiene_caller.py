"""This repository's standalone hygiene and publication-checks callers are gone; hygiene runs in the pipeline."""

from __future__ import annotations

import unittest

from tests.ci_policy_support import REPOSITORY_ROOT


WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"


class HygieneCallerTests(unittest.TestCase):
    def test_the_standalone_hygiene_and_publication_checks_callers_are_gone(self) -> None:
        for name in ("repository-hygiene-caller.yml", "publication-checks.yml", "publication-checks-caller.yml"):
            with self.subTest(workflow=name):
                self.assertFalse((WORKFLOWS / name).exists())


if __name__ == "__main__":
    unittest.main()
