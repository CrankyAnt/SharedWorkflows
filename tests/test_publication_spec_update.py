from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.publication_spec_module import MODULE_PATH, update_spec

CURRENT = b'{"spec":"Conventional Branch","version":"1.1.0","grammar":{"regex":"^feat/[a-z]+$"}}\n'

NEWER = (json.dumps({"spec": "Conventional Branch", "version": "1.2.0", "grammar": {"regex": r"^(?:main|feature/[a-z-]+|bugfix/[a-z-]+|ai/[a-z-]+|release/v[0-9]+\.[0-9]+\.[0-9]+)$"}}, separators=(",", ":")).encode("utf-8") + b"\n")

MALFORMED = b'{"spec":"Conventional Branch","version":"1.2","grammar":{"regex":"["}}\n'

INVALID_REGEX = b'{"spec":"Conventional Branch","version":"1.2.0","grammar":{"regex":"["}}\n'


class PublicationSpecUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        base = self.root / "actions" / "publication-checks"
        fixture_directory = self.root / "tests" / "fixtures" / "publication_checks"
        base.mkdir(parents=True)
        fixture_directory.mkdir(parents=True)
        (base / "spec.json").write_bytes(CURRENT)
        (base / "spec-metadata.json").write_text(json.dumps({"version": "1.1.0", "sha256": hashlib.sha256(CURRENT).hexdigest(), "source": "https://conventionalbranch.org/v1.1.0/spec.json"}) + "\n", encoding="utf-8")
        (fixture_directory / "specification-cases.json").write_text('{"version":"1.1.0","official_regex":"^feat/[a-z]+$","cases":[]}\n', encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_newer_specification_replaces_all_three_outputs_and_records_node_results(self) -> None:
        result = update_spec.update(self.root, fetcher=lambda url: NEWER)

        self.assertTrue(result["changed"])
        self.assertEqual(NEWER, self._read("actions/publication-checks/spec.json"))
        metadata = json.loads(self._read("actions/publication-checks/spec-metadata.json"))
        self.assertEqual({"version": "1.2.0", "sha256": hashlib.sha256(NEWER).hexdigest(), "source": "https://conventionalbranch.org/v1.2.0/spec.json"}, metadata)
        fixture = json.loads(self._read("tests/fixtures/publication_checks/specification-cases.json"))
        self.assertEqual("1.2.0", fixture["version"])
        results = {case["branch"]: case["matches"] for case in fixture["cases"]}
        self.assertTrue(results["feature/add-check"])
        self.assertTrue(results["bugfix/repair"])
        self.assertTrue(results["ai/add-check"])
        self.assertFalse(results["research/issue-1-loader-shape"])

    def test_same_or_older_version_does_not_write_outputs(self) -> None:
        before = self._tree()
        same = update_spec.update(self.root, fetcher=lambda url: CURRENT)
        self.assertFalse(same["changed"])
        self.assertEqual(before, self._tree())

        older = CURRENT.replace(b"1.1.0", b"1.0.9")
        result = update_spec.update(self.root, fetcher=lambda url: older)
        self.assertFalse(result["changed"])
        self.assertIn("older", result["message"])
        self.assertEqual(before, self._tree())

    def test_same_version_content_drift_requires_review_without_writing(self) -> None:
        before = self._tree()
        drifted = CURRENT.replace(b"[a-z]+", b"[0-9]+")
        result = update_spec.update(self.root, fetcher=lambda url: drifted)
        self.assertFalse(result["changed"])
        self.assertIn("needs review", result["message"])
        self.assertEqual(before, self._tree())

    def test_network_outage_does_not_write_outputs(self) -> None:
        before = self._tree()

        def outage(url: str) -> bytes:
            raise OSError("network unavailable")

        result = update_spec.update(self.root, fetcher=outage)
        self.assertFalse(result["changed"])
        self.assertIn("network unavailable", result["message"])
        self.assertEqual(before, self._tree())

    def test_malformed_candidate_does_not_write_outputs(self) -> None:
        before = self._tree()
        result = update_spec.update(self.root, fetcher=lambda url: MALFORMED)
        self.assertFalse(result["changed"])
        self.assertIn("invalid", result["message"])
        self.assertEqual(before, self._tree())

    def test_invalid_regex_or_versioned_mismatch_does_not_write_outputs(self) -> None:
        before = self._tree()
        invalid_regex = update_spec.update(self.root, fetcher=lambda url: INVALID_REGEX)
        self.assertFalse(invalid_regex["changed"])
        self.assertIn("does not compile", invalid_regex["message"])
        self.assertEqual(before, self._tree())

        mismatched = update_spec.update(self.root, fetcher=lambda url: NEWER if url == update_spec.LATEST_URL else CURRENT)
        self.assertFalse(mismatched["changed"])
        self.assertIn("differs", mismatched["message"])
        self.assertEqual(before, self._tree())

    def test_cli_writes_github_output_and_keeps_json_on_stdout(self) -> None:
        candidate = self.root / "candidate.json"
        output = self.root / "github-output.txt"
        candidate.write_bytes(NEWER)
        output.write_text("preserved=true\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(MODULE_PATH), "--root", str(self.root), "--candidate", str(candidate), "--output", str(output)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Updated Conventional Branch specification to 1.2.0.", result.stdout)
        self.assertEqual(f"preserved=true\nchanged=true\nversion=1.2.0\nsha256={hashlib.sha256(NEWER).hexdigest()}\n", output.read_text(encoding="utf-8"))

    def _read(self, relative: str) -> bytes:
        return (self.root / relative).read_bytes()

    def _tree(self) -> dict[str, bytes]:
        return {
            relative: self._read(relative)
            for relative in [
                "actions/publication-checks/spec.json",
                "actions/publication-checks/spec-metadata.json",
                "tests/fixtures/publication_checks/specification-cases.json",
            ]
        }


if __name__ == "__main__":
    unittest.main()
