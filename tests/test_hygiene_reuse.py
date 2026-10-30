from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.hygiene_fixtures import fixture_repository, tools_available
from tests.hygiene_process import RESOURCES, hygiene

ASSIGNMENT = 'version = 1\n\n[[annotations]]\npath = ["REUSE.toml", "src/**"]\nSPDX-FileCopyrightText = "2026 Fixture"\nSPDX-License-Identifier = "MIT"\n'


@unittest.skipUnless(tools_available("reuse"), "the pinned reuse is neither in HYGIENE_TOOLS nor on PATH")
class ReuseLintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_a_repository_whose_tracked_files_all_have_an_assignment_passes(self) -> None:
        repo = fixture_repository(self.root / "compliant", {"REUSE.toml": ASSIGNMENT, "LICENSES/MIT.txt": "MIT License\n", "src/app.py": "print(1)\n"})
        result = hygiene("reuse", repo)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("+ ", result.stdout, "the command is shown")
        self.assertIn("lint", result.stdout)

    def test_a_tracked_file_without_a_licence_assignment_fails_with_the_report(self) -> None:
        repo = fixture_repository(self.root / "missing", {"REUSE.toml": ASSIGNMENT, "LICENSES/MIT.txt": "MIT License\n", "src/app.py": "print(1)\n",
                                                          "docs/unassigned.txt": "no licence\n"})
        result = hygiene("reuse", repo)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"* {Path('docs/unassigned.txt')}", result.stdout, "reuse prints the path in the form of the platform")
        self.assertIn("MISSING COPYRIGHT AND LICENSING INFORMATION", result.stdout)
        self.assertIn("not compliant", result.stdout)

    def test_a_missing_reuse_is_a_failure(self) -> None:
        repo = fixture_repository(self.root / "tools", {"REUSE.toml": ASSIGNMENT})
        empty = self.root / "empty-tools"
        empty.mkdir()
        result = subprocess.run([sys.executable, str(RESOURCES / "hygiene.py"), "reuse", "--root", str(repo), "--tools", str(empty)],
                                capture_output=True, text=True, check=False)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("reuse is missing from", result.stderr)


if __name__ == "__main__":
    unittest.main()
