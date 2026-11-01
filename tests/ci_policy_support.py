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
    return condition_holds(condition, {"needs.select.result": select_result, "needs.select.outputs.identical": identical})


def condition_holds(condition: str, values: dict[str, str]) -> bool:
    """Whether an ``if``, an expression of !, &&, ||, parentheses, always(), cancelled() and == or != between a context
    value that ``values`` names and a quoted string, holds for those values in a run that was not cancelled. A value holds
    only letters, digits, ``_`` and ``-``; a context value that ``values`` does not name fails the evaluation."""
    text = condition.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    assert all(re.fullmatch(r"[\w-]*", value) for value in values.values()), values
    names = {**{name: repr(value) for name, value in values.items()}, "always()": "True", "cancelled()": "False"}
    pattern = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    expression = re.sub(pattern, lambda found: names[found[0]], text)
    expression = expression.replace("&&", " and ").replace("||", " or ").replace("!=", "<>").replace("!", " not ").replace("<>", "!=")
    assert re.fullmatch(r"[\s\w()'=!-]*", expression) and "." not in expression, condition
    return bool(eval(expression, {"__builtins__": {}}, {}))


EXPRESSION_TOKEN = re.compile(r"\s*(?:(\|\||&&|==|!=|\(|\)|!)|'([^']*)'|([A-Za-z_][A-Za-z0-9_.-]*))")


def expression_value(expression: str, context: dict) -> object:
    """The value of a GitHub Actions expression made of context paths, string literals, ``==``, ``!=``, ``!``, ``&&``,
    ``||`` and parentheses; a missing property is null, and ``&&`` and ``||`` return an operand, as GitHub's do."""
    text = expression.strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    tokens, position = [], 0
    while text[position:].strip():
        match = EXPRESSION_TOKEN.match(text, position)
        assert match, f"unexpected expression text: {text[position:]}"
        tokens.append(("op", match.group(1)) if match.group(1) else ("str", match.group(2)) if match.group(2) is not None else ("path", match.group(3)))
        position = match.end()
    index = 0

    def peek() -> str | None:
        return tokens[index][1] if index < len(tokens) and tokens[index][0] == "op" else None

    def primary() -> object:
        nonlocal index
        kind, value = tokens[index]
        index += 1
        if kind == "op" and value == "(":
            result = either()
            index += 1
            return result
        if kind == "op" and value == "!":
            return not primary()
        if kind == "str":
            return value
        current: object = context
        for part in value.split("."):
            current = current.get(part) if isinstance(current, dict) else None
        return current

    def comparison() -> object:
        nonlocal index
        left = primary()
        while peek() in ("==", "!="):
            operator = peek()
            index += 1
            right = primary()
            left = (left == right) if operator == "==" else (left != right)
        return left

    def both() -> object:
        nonlocal index
        left = comparison()
        while peek() == "&&":
            index += 1
            right = comparison()
            left = right if left else left
        return left

    def either() -> object:
        nonlocal index
        left = both()
        while peek() == "||":
            index += 1
            right = both()
            left = left if left else right
        return left

    return either()


# A ``v1-moved`` run: ``repository_dispatch`` without a ``client_payload``, on the default branch's tip.
V1_MOVED_TIP = "c" * 40
V1_MOVED_CONTEXT = {"github": {"event_name": "repository_dispatch", "sha": V1_MOVED_TIP, "ref_name": "main",
                               "event": {"action": "v1-moved", "repository": {"default_branch": "main"}}}}


def passed_list_steps(steps: list[dict], check_outcome: str, additions: str, keep_outcome: str = "success") -> list[str]:
    """The ids, or names, of the steps of a suite job whose condition reads the check's outcome or outputs and that run on a
    pull request after the step ``check`` ended with ``check_outcome`` and the action output ``additions``. The step
    ``keep`` ends with ``keep_outcome`` when it runs and with ``skipped`` otherwise."""
    values = {"github.event_name": "pull_request", "steps.check.outcome": check_outcome, "steps.check.outputs.additions": additions,
              "steps.keep.outcome": "skipped"}
    ran = []
    for step in steps:
        condition = str(step.get("if") or "")
        if "steps.check." not in condition or step.get("id") == "check":
            continue
        if condition_holds(condition, values):
            ran.append(step.get("id") or step["name"])
            if step.get("id") == "keep":
                values["steps.keep.outcome"] = keep_outcome
    return ran


def tracked_workflows(root: Path) -> dict[str, str]:
    """The text of each tracked workflow directly under ``.github/workflows/``, keyed by its repository path."""
    return {path: (root / path).read_text(encoding="utf-8") for path in sorted(tracked_files(root, [".github/workflows"]))
            if re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", path)}


def flow_items(text: str) -> list[str]:
    """The top-level items of the inner text of a flow list, a flow mapping or a jq object program, split at commas outside
    brackets and quotes: the one splitter of such text in the tests."""
    items, depth, quote, start = [], 0, "", 0
    for index, character in enumerate(text):
        if quote:
            quote = "" if character == quote else quote
        elif character in "'\"":
            quote = character
        elif character in "[{":
            depth += 1
        elif character in "]}":
            depth -= 1
        elif character == "," and depth == 0:
            items.append(text[start:index])
            start = index + 1
    return [item.strip() for item in [*items, text[start:]] if item.strip()]


def workflow_triggers(text: str, source: str) -> set[str]:
    """The events in a workflow's ``on``, stated as a string, a list or a mapping. The YAML subset reader returns a flow
    list such as ``[push, workflow_call]`` or a flow mapping such as ``{workflow_call: {}}`` as its text, so its top-level
    items are split here."""
    document = ci_definition().read_yaml(text, source)
    on = document.get("on") if isinstance(document, dict) else None
    if isinstance(on, dict):
        return set(on)
    if isinstance(on, str) and on[:1] + on[-1:] in ("[]", "{}"):
        on = [item.split(":", 1)[0] if on[0] == "{" else item for item in flow_items(on[1:-1])]
    if isinstance(on, list):
        return {str(item).strip().strip("'\"") for item in on}
    return {on} if isinstance(on, str) else set()


def shared_workflows(workflows: dict[str, str]) -> list[str]:
    """The paths of the workflows, from path to text, whose triggers hold ``workflow_call``: the shared workflows."""
    return [path for path, text in workflows.items() if "workflow_call" in workflow_triggers(text, path)]


def uses_values(text: str, source: str) -> list[str]:
    """The ``uses:`` value of each job and then each step of a workflow, read from the document that the YAML subset reader
    returns, so that quotes and a trailing comment are no part of a value."""
    document = ci_definition().read_yaml(text, source)
    job_map = document.get("jobs") if isinstance(document, dict) else None
    jobs = [job for job in job_map.values() if isinstance(job, dict)] if isinstance(job_map, dict) else []
    steps = [step for job in jobs if isinstance(job.get("steps"), list) for step in job["steps"] if isinstance(step, dict)]
    return [str(item["uses"]) for item in [*jobs, *steps] if item.get("uses")]


def read_workflow(path: Path) -> tuple[str, dict]:
    """The text of a workflow and its document, read with the YAML subset reader of the suite selection."""
    text = path.read_text(encoding="utf-8")
    document = ci_definition().read_yaml(text, str(path))
    assert isinstance(document, dict), path
    return text, document
