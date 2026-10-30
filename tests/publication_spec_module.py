from __future__ import annotations

import importlib.util
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPOSITORY_ROOT / "actions" / "publication-checks" / "update_spec.py"
SPEC = importlib.util.spec_from_file_location("update_spec", MODULE_PATH)
assert SPEC and SPEC.loader
update_spec = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(update_spec)
