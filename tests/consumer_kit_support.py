"""YAML parsing and assertion helpers for the tests of the consumer-kit templates."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "setup_dependabot_coverage",
    REPOSITORY_ROOT / "actions" / "repository-hygiene" / "dependabot_coverage.py",
)
assert SPEC and SPEC.loader
coverage = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = coverage
SPEC.loader.exec_module(coverage)


def read_yaml_text(text: str) -> tuple[str, dict]:
    document = coverage.parse_yaml(text)
    assert isinstance(document, dict), text[:40]
    return text, document

def read_yaml(path: Path) -> tuple[str, dict]:
    return read_yaml_text(path.read_text(encoding="utf-8"))

def assert_caller_passes_no_secrets_and_runs_no_step(test: unittest.TestCase, text: str) -> None:
    test.assertNotIn("secrets", text)
    test.assertNotRegex(text, r"(?m)^\s*(?:- )?run:")

# The one group of an entry of several directories: each dependency across them, every dependency, every update type.
PER_DEPENDENCY_GROUP = {"per-dependency": {"group-by": "dependency-name", "patterns": ["*"]}}


def assert_update_entry_follows_the_template(test: unittest.TestCase, entry: dict) -> None:
    """An entry of one directory has the group of minor and patch updates; an entry of several directories has the
    group that combines each dependency across them."""
    test.assertEqual({"interval": "weekly"}, entry["schedule"])
    test.assertEqual({"default-days": 7}, entry["cooldown"])
    if "directories" in entry:
        test.assertNotIn("directory", entry)
        test.assertGreater(len(entry["directories"]), 1)
        test.assertEqual(PER_DEPENDENCY_GROUP, entry["groups"], "without update-types the group takes every update type")
    else:
        test.assertEqual({"minor-and-patch": {"patterns": ["*"], "update-types": ["minor", "patch"]}}, entry["groups"])
