from __future__ import annotations

import hashlib
import json
import unittest

from tests.publication_spec_module import REPOSITORY_ROOT, update_spec


class PublicationSpecFixtureTests(unittest.TestCase):
    def test_committed_fixture_records_node_results_for_the_vendored_specification(self) -> None:
        specification = json.loads((REPOSITORY_ROOT / "actions" / "publication-checks" / "spec.json").read_text(encoding="utf-8"))
        metadata = json.loads((REPOSITORY_ROOT / "actions" / "publication-checks" / "spec-metadata.json").read_text(encoding="utf-8"))
        fixture = json.loads((REPOSITORY_ROOT / "tests" / "fixtures" / "publication_checks" / "specification-cases.json").read_text(encoding="utf-8"))
        raw_specification = (REPOSITORY_ROOT / "actions" / "publication-checks" / "spec.json").read_bytes()

        self.assertEqual(specification["version"], metadata["version"])
        self.assertEqual(specification["version"], fixture["version"])
        self.assertEqual(hashlib.sha256(raw_specification).hexdigest(), metadata["sha256"])
        self.assertEqual(specification["grammar"]["regex"], fixture["official_regex"])
        branches = tuple(case["branch"] for case in fixture["cases"])
        self.assertEqual(update_spec._compile_and_match(specification["grammar"]["regex"], branches), [case["matches"] for case in fixture["cases"]])


if __name__ == "__main__":
    unittest.main()
