"""Load the auto_merge implementation without importing test cases or repository data."""

import importlib.util
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "actions/dependabot-auto-merge/auto_merge.py"
SPEC = importlib.util.spec_from_file_location("auto_merge", SCRIPT)
assert SPEC and SPEC.loader
auto_merge = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = auto_merge
SPEC.loader.exec_module(auto_merge)
