"""Shared standard-library helpers for the CI policy entry modules."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def action_pin_pattern(action: str) -> str:
    """Match one `uses: <action>@<40-hex SHA> # v<version>` line."""
    return rf"(?m)^[ \t]*- uses: {re.escape(action)}@[0-9a-f]{{40}} # v\d+(?:\.\d+)*[ \t]*$"


def tracked_files(root: Path, pathspecs: list[str]) -> set[str]:
    """The tracked files that ``pathspecs`` name; at least one pathspec, so that the listing is not every tracked path."""
    assert pathspecs, "a listing of every tracked path needs no helper"
    listing = subprocess.run(["git", "ls-files", "-z", "--", *pathspecs], cwd=root, capture_output=True, text=True, encoding="utf-8", check=True)
    return {item for item in listing.stdout.split("\0") if item}


def entry_pathspecs(suites: Path) -> list[str]:
    """The pathspecs of the entries of a suite list: a path as itself, a glob pattern as a ``:(glob)`` pathspec."""
    entries = [entry for suite in json.loads(suites.read_text(encoding="utf-8"))["suites"] for entry in suite["entries"]]
    return [f":(glob){entry}" if any(character in entry for character in "*?[") else entry for entry in dict.fromkeys(entries)]


def is_tracked(path: str, tracked: set[str]) -> bool:
    return path in tracked or any(item.startswith(path + "/") for item in tracked)


def ci_definition():
    """The CI-definition reader of the suite selection, loaded once by its path."""
    import importlib.util
    import sys

    module = sys.modules.get("ci_policy_ci_definition")
    if module is None:
        spec = importlib.util.spec_from_file_location("ci_policy_ci_definition", REPOSITORY_ROOT / "actions" / "suite-selection" / "ci_definition.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


def verdict_runs(condition: str, select_result: str, identical: str) -> bool:
    """Whether a job whose ``if`` is ``condition``, an expression of always(), !, &&, || and == or != between
    needs.select.result or needs.select.outputs.identical and a quoted string, runs after a select job with this result
    and identical output."""
    names = {"needs.select.result": repr(select_result), "needs.select.outputs.identical": repr(identical), "always()": "True"}
    expression = re.sub(r"needs\.select\.result|needs\.select\.outputs\.identical|always\(\)", lambda found: names[found[0]], condition)
    expression = expression.replace("&&", " and ").replace("||", " or ").replace("!=", "<>").replace("!", " not ").replace("<>", "!=")
    assert re.fullmatch(r"[\sA-Za-z()'=!-]*", expression), condition
    return bool(eval(expression, {"__builtins__": {}}, {}))


def read_workflow(path: Path) -> tuple[str, dict]:
    """The text of a workflow and its document, read with the YAML subset reader of the suite selection."""
    text = path.read_text(encoding="utf-8")
    document = ci_definition().read_yaml(text, str(path))
    assert isinstance(document, dict), path
    return text, document
