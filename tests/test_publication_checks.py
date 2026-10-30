from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.publication_fixtures import REPOSITORY_ROOT

RESOURCE_DIRECTORY = REPOSITORY_ROOT / "actions" / "publication-checks"
SPECIFICATION = RESOURCE_DIRECTORY / "spec.json"
SPECIFICATION_METADATA = RESOURCE_DIRECTORY / "spec-metadata.json"


class PublicationChecksTests(unittest.TestCase):
    def test_offline_specification_rejects_content_or_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory)
            for source in [SPECIFICATION, SPECIFICATION_METADATA]:
                (fixture / source.name).write_bytes(source.read_bytes())
            validator = RESOURCE_DIRECTORY / "verify-spec.cjs"
            result = subprocess.run(["node", str(validator), str(fixture)], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            (fixture / "spec.json").write_bytes(SPECIFICATION.read_bytes() + b" ")
            result = subprocess.run(["node", str(validator), str(fixture)], capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("hash mismatch", result.stderr)
            (fixture / "spec.json").write_bytes(SPECIFICATION.read_bytes())
            metadata = json.loads(SPECIFICATION_METADATA.read_text())
            metadata["version"] = "0.0.0"
            (fixture / "spec-metadata.json").write_text(json.dumps(metadata))
            result = subprocess.run(["node", str(validator), str(fixture)], capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("version or grammar", result.stderr)


if __name__ == "__main__":
    unittest.main()
