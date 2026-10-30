"""No consumer kit holds a publication-check caller: the hygiene job runs the convention checks."""

from __future__ import annotations

import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
KITS = REPOSITORY_ROOT / "actions"


class NoPublicationCheckCallerTemplateTest(unittest.TestCase):
    def test_no_template_is_or_calls_a_publication_check_caller(self) -> None:
        self.assertEqual([], sorted(KITS.glob("*/consumer-kit/publication-checks.yml")))
        for template in sorted(KITS.glob("*/consumer-kit/*.yml")):
            with self.subTest(template=template.name):
                self.assertNotIn("publication-checks.yml", template.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
