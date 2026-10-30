from __future__ import annotations

import subprocess
from pathlib import Path


class BranchCheckMixin:
    def _run_branch_check(self, validator: Path, branch: str, expected_returncode: int) -> None:
        result = subprocess.run(["node", str(validator), branch], capture_output=True, text=True)
        self.assertEqual(expected_returncode, result.returncode, result.stdout + result.stderr)
