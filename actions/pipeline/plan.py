#!/usr/bin/env python3
"""Plan the pipeline from a repository's configuration file, and give its verdict.

    python3 plan.py plan    [--root DIR] [--schemas DIR] [--shared-workflow FILE] [--base SHA --head SHA [--compare merge-base|direct]]
                            [--workflow NAME] [--event NAME] [--pull-request NUMBER] [--run-id ID] [--github-output FILE] [--summary FILE]
    python3 plan.py verdict --needs FILE [--summary FILE]
    python3 plan.py schema  [--schemas DIR] [--check]
    python3 plan.py setting --part NAME --setting NAME [--root DIR] [--schemas DIR]

``plan`` reads ``.github/ci-config.yml``, checks it against the schema of its version and decides per part whether it
runs. The schema folder holds one folder per schema version, ``v<N>``, with the ``base.json`` of the file and one
fragment per part, ``parts/<part>.json``: its ``description``, the schema of its ``configuration``, or both, and
``"always": true`` for a part that runs in every pipeline run. The configuration file can state a part that always runs
only when its fragment has a configuration, and then only to give its settings; it cannot switch it off. A fragment
with ``"workflow": "<file>.yml"`` is a part whose job is in that shared workflow and not in the pipeline: the
configuration file states it under ``parts`` like any other part, and the plan and the verdict leave it out; with ``--shared-workflow`` set to that file,
``plan`` decides on the parts of that workflow alone. Beside them,
``setups.json`` holds the known setups by name, each with the ``tool`` and the ``version`` that a job installs; the
definition ``setup`` of the base accepts those names only. The highest version is the current one; the plan reads it and
the one before.

A part that always runs, runs. Any other part runs when the configuration file states it under ``parts`` and, where its
configuration names ``paths``, a changed path matches them, its own configuration differs from the base, or the changed
files are unknown. The plan is one JSON document, the output ``plan``: per part ``run``, ``reason`` and ``config``, and
for a stated part with the setting ``setups`` the tool and the version of each setup it names, in the listed order, the
parts switched off and the local workflows registered compared with the base, and whether that comparison was made.

``verdict`` reads the ``needs`` context of the job that needs ``plan`` and every part, as ``toJSON(needs)`` renders it.
It fails when ``plan`` did not succeed, its plan output is missing or invalid, or a planned check failed, ended skipped,
timed out or never started; a check the plan did not select counts as not run. ``schema`` writes, or with ``--check``
compares, the assembled schema ``ci-config.schema.json`` of each version.

``setting`` prints the items of one list setting of one part, one per line, as the checked configuration file states
them; it prints nothing when the file does not state the setting. When the configuration file cannot be used, it prints
nothing and says so on standard error, and passes: the job that reads the setting then runs without it, and the plan
reports the file.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep

PART_FOLDER = Path(__file__).resolve().parent
sys.path.insert(0, str(PART_FOLDER.parent / "repository-hygiene"))
from dependabot_coverage import YamlError, parse_yaml  # noqa: E402

CONFIGURATION = ".github/ci-config.yml"
SCHEMAS = PART_FOLDER / "schema"
ASSEMBLED = "ci-config.schema.json"
SETUPS = "setups.json"
PIPELINE = "pipeline.yml"
AUTOMATION = "automation.yml"
FULL_RECORDING_JOB = "full-recording"
PLAN_JOB = "plan"
HYGIENE_JOB = "repository-hygiene"
VERDICT_JOB = "verdict"
WAIT_FOR_CODE_JOB = "wait-for-code"
EDIT_PLAN_JOB = "plan (edited pull request)"
SUITE_SELECTION_JOB = "suite-selection"
SUITE_SELECTION_JOBS = "suite-selection-jobs"
SUITE_SELECTION_LINUX = "suite-selection-linux"
SUITE_SELECTION_WINDOWS = "suite-selection-windows"
SUITE_SELECTION_RESULTS = "suite-selection-results"
DEFAULT_JOB_TIMEOUT_MINUTES = 10
TIMEOUTS_FILE = PART_FOLDER / "timeouts.json"


def load_timeouts(path: Path = TIMEOUTS_FILE) -> dict[str, int]:
    """Read and validate the job timeout-minutes map; fails loudly when missing, unreadable or malformed."""
    document = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(document, dict)
            or not all(isinstance(k, str) and k and isinstance(v, int) and not isinstance(v, bool) and v > 0
                       for k, v in document.items())):
        raise ValueError(f"{path} must map job names to positive integer minute limits")
    return document


JOB_TIMEOUT_MINUTES: dict[str, int] = load_timeouts()
PLAN_CHANGES_STEP = "Decide which checks the changed files can affect"
RECORD_TREE_STEP = "Record the checked-out commit and tree"
TITLE_STEP = "Check the pull request title"
REFERENCES_STEP = "Check the references in the pull request title and description"
TEXT_ONLY_FAILURE_STEP = "Record that only the pull request text checks failed"
API_TIMEOUT = 15
POLL_SECONDS = 10
DISCOVERY_SECONDS = 30
FAILING_RESULTS = ("failure", "cancelled", "timed_out")
INPUTS_UNKNOWN_REASON = "inputs unknown"
# Each pipeline part's own shared code and pin files in SharedWorkflows, which select the part where this repository
# calls the pipeline by local path. The pipeline's own folder and workflow are not among them: each part's job in the
# workflow and the files it runs make up its part digest (``PART_DIGESTS``). A folder's consumer kit
# (``PART_SHARED_EXCLUDED``), which no job runs, selects no part, as it moves no part digest.
PART_SHARED_INPUTS: dict[str, tuple[str, ...]] = {
    "python-verify": (
        "actions/python-language-step/",
        "actions/python-tests/",
        "actions/repository-hygiene/pins.json",
        "actions/repository-hygiene/install_tools.py",
    ),
    "python-test": (
        "actions/python-tests/",
        "actions/python-language-step/",
        "actions/repository-hygiene/pins.json",
        "actions/repository-hygiene/install_tools.py",
    ),
    "dotnet-test": ("actions/dotnet-test/",),
    "node-test": ("actions/node-test/",),
}
PART_SHARED_EXCLUDED = ("consumer-kit",)


def shared_input_changed(name: str, changed: list[str]) -> str | None:
    """The first changed file of a part's own shared code that its job runs: under one of its ``PART_SHARED_INPUTS``
    and, under a folder, outside the folder's consumer kit; None when there is none."""
    for path in changed:
        for pattern in PART_SHARED_INPUTS.get(name, ()):
            if pattern.endswith("/"):
                if path.startswith(pattern) and path.removeprefix(pattern).split("/")[0] not in PART_SHARED_EXCLUDED:
                    return path
            elif path == pattern:
                return path
    return None


# Shared CI digests (from shared-lookup.json) that select a pipeline part when moved.
PART_DIGESTS: dict[str, tuple[str, ...]] = {
    "custom": ("part:custom",),
    "dotnet-test": ("part:dotnet-test",),
    "node-test": ("part:node-test",),
    "python-verify": ("part:python-verify", "language:python"),
    "python-test": ("part:python-test", "language:python"),
}
ALL_ZERO = re.compile(r"^0+$")
VERSION_FOLDER = re.compile(r"^v([1-9][0-9]*)$")
WORKFLOW_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.yml$")
# The schema keywords that say nothing about a value, and those the check reads; any other keyword fails the check.
ANNOTATIONS = {"$schema", "$id", "$defs", "title", "description", "examples", "default"}
KEYWORDS = {"type", "const", "enum", "pattern", "minLength", "minimum", "minItems", "maxItems", "uniqueItems", "items", "properties", "required",
            "additionalProperties", "oneOf", "$ref"}
TYPES = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool, "null": type(None)}
TYPE_NAMES = {"object": "a mapping", "array": "a list", "string": "a string", "integer": "a whole number", "boolean": "true or false", "null": "nothing"}


class PlanError(Exception):
    """The plan could not be made; the message says why."""


class SchemaError(Exception):
    """A schema folder holds something this program does not read."""


# --- the schema folder ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaVersion:
    version: int
    schema: dict
    always: frozenset[str]
    setups: dict = field(default_factory=dict)
    workflows: dict = field(default_factory=dict)

    def parts_of(self, workflow: str) -> list[str]:
        """The parts whose job runs in a shared workflow, by name; a part without a stated workflow runs in the pipeline."""
        return [name for name in self.parts if self.workflows.get(name, PIPELINE) == workflow]

    @property
    def parts(self) -> list[str]:
        """Every part by name: those the configuration file switches on and those that always run."""
        return sorted({*self.schema["properties"]["parts"]["properties"], *self.always})

    @property
    def pipeline_parts(self) -> list[str]:
        """The parts whose job is in the pipeline: every part but those whose fragment names another shared workflow."""
        return [name for name in self.parts if name not in self.workflows]


def read_json(path: Path) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SchemaError(f"{path}: {error}") from error
    if not isinstance(document, dict):
        raise SchemaError(f"{path}: expected a JSON object")
    return document


def check_keywords(schema: object, source: str) -> None:
    """Fail on a keyword that the check of a configuration file does not read, so that no rule of a schema is skipped."""
    if not isinstance(schema, dict):
        raise SchemaError(f"{source}: a schema is a JSON object")
    unknown = sorted(set(schema) - ANNOTATIONS - KEYWORDS)
    if unknown:
        raise SchemaError(f"{source}: the keyword {unknown[0]} is not supported")
    for key in ("properties", "$defs"):
        for name, inner in schema.get(key, {}).items():
            check_keywords(inner, f"{source}, {name}")
    for inner in schema.get("oneOf", []):
        check_keywords(inner, source)
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            check_keywords(schema[key], source)


def known_setups(folder: Path) -> dict:
    """The setups of one version by name, each with the tool and the version that a job installs."""
    path = folder / SETUPS
    setups = read_json(path) if path.is_file() else {}
    for name, setup in setups.items():
        if not isinstance(setup, dict) or set(setup) != {"tool", "version"} or not all(isinstance(value, str) and value for value in setup.values()):
            raise SchemaError(f"{path}: the setup {name} holds a tool and a version")
    return setups


def assemble(folder: Path, version: int) -> SchemaVersion:
    """The schema of one version: its base with the configuration of each part's fragment under ``parts``."""
    schema = read_json(folder / "base.json")
    check_keywords(schema, str(folder / "base.json"))
    try:
        parts = schema["properties"]["parts"]["properties"]
    except (KeyError, TypeError) as error:
        raise SchemaError(f"{folder / 'base.json'}: properties.parts.properties is missing") from error
    setups = known_setups(folder)
    if "setup" in schema.get("$defs", {}):
        if not setups:
            raise SchemaError(f"{folder / SETUPS}: the base has the definition setup, so this file names at least one setup")
        schema["$defs"]["setup"]["enum"] = list(setups)
    always = set()
    workflows = {}
    for path in sorted((folder / "parts").glob("*.json")):
        fragment = read_json(path)
        unknown = sorted(set(fragment) - {"description", "always", "configuration", "workflow"})
        stated = fragment.get("always") or "configuration" in fragment
        if unknown or not isinstance(fragment.get("description"), str) or not isinstance(fragment.get("always", False), bool) or not stated:
            raise SchemaError(f"{path}: a fragment holds a description and a configuration, always, or both")
        if "workflow" in fragment:
            if fragment.get("always"):
                raise SchemaError(f"{path}: a part that always runs is a part of the pipeline and names no workflow")
            if not isinstance(fragment["workflow"], str) or not WORKFLOW_FILE.match(fragment["workflow"]):
                raise SchemaError(f"{path}: workflow names a workflow file, such as release.yml")
            workflows[path.stem] = fragment["workflow"]
        if fragment.get("always"):
            always.add(path.stem)
            if "configuration" not in fragment:
                continue
        configuration = fragment.get("configuration")
        check_keywords(configuration, str(path))
        if configuration.get("type") != "object" or configuration.get("additionalProperties") is not False:
            raise SchemaError(f"{path}: the configuration of a part is an object without additional properties")
        parts[path.stem] = {"description": fragment["description"], **configuration}
    without_settings = sorted(always - set(parts))
    if without_settings:
        schema["properties"]["parts"]["description"] += f" Not stated here, because it always runs and has no settings: {and_list(without_settings)}."
    return SchemaVersion(version, schema, frozenset(always), setups, workflows)


def schema_versions(folder: Path) -> dict[int, SchemaVersion]:
    """The current schema version and the one before it, from the ``v<N>`` folders of the schema folder."""
    numbers = sorted(int(match.group(1)) for item in (folder.iterdir() if folder.is_dir() else []) if item.is_dir() and (match := VERSION_FOLDER.match(item.name)))
    if not numbers:
        raise SchemaError(f"{folder} holds no schema version")
    current = numbers[-1]
    return {number: assemble(folder / f"v{number}", number) for number in (current, current - 1) if number in numbers}


# --- the check of a value against a schema ------------------------------------------------------------------------


@dataclass(frozen=True)
class Problem:
    path: tuple
    message: str
    kind: str = "value"


def type_name(value: object) -> str:
    for name, kind in TYPES.items():
        if isinstance(value, kind) and not (kind is int and isinstance(value, bool)):
            return TYPE_NAMES[name]
    return "a value this file cannot hold"


def has_type(value: object, name: str) -> bool:
    return isinstance(value, TYPES[name]) and not (name == "integer" and isinstance(value, bool))


def choice(values: list) -> str:
    quoted = [repr(value) if isinstance(value, str) else json.dumps(value) for value in values]
    return quoted[0] if len(quoted) == 1 else ", ".join(quoted[:-1]) + " or " + quoted[-1]


def problems(value: object, schema: dict, root: dict, path: tuple = ()) -> list[Problem]:
    """Each way the value does not match the schema, with the place of the value in the file."""
    if "$ref" in schema:
        target: object = root
        for step in schema["$ref"].removeprefix("#/").split("/"):
            target = target[step]  # type: ignore[index]
        return problems(value, target, root, path)  # type: ignore[arg-type]
    if "oneOf" in schema:
        attempts = [problems(value, alternative, root, path) for alternative in schema["oneOf"]]
        if any(not attempt for attempt in attempts):
            return []
        fitting = [attempt for attempt in attempts if all(problem.kind == "value" for problem in attempt)]
        if len(fitting) == 1:
            return fitting[0]
        titles = [alternative.get("title", "an alternative") for alternative in schema["oneOf"]]
        return [Problem(path, "is neither " + " nor ".join(titles))]
    found: list[Problem] = []
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(has_type(value, name) for name in names):
            return [Problem(path, f"expected {' or '.join(TYPE_NAMES[name] for name in names)}, found {type_name(value)}", "type")]
    if "const" in schema and (value != schema["const"] or type(value) is not type(schema["const"])):
        found.append(Problem(path, f"must be {choice([schema['const']])}, found {choice([value])}"))
    if "enum" in schema and value not in schema["enum"]:
        found.append(Problem(path, f"must be {choice(schema['enum'])}, found {choice([value])}"))
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            found.append(Problem(path, f"does not match the pattern {schema['pattern']}"))
        if len(value) < schema.get("minLength", 0):
            found.append(Problem(path, "is empty" if not value else f"needs at least {schema['minLength']} characters"))
    if has_type(value, "integer") and "minimum" in schema and value < schema["minimum"]:  # type: ignore[operator]
        found.append(Problem(path, f"must be {schema['minimum']} or more"))
    if isinstance(value, list):
        minimum = schema.get("minItems", 0)
        if len(value) < minimum:
            found.append(Problem(path, f"needs at least {minimum} item{'' if minimum == 1 else 's'}"))
        maximum = schema.get("maxItems")
        if maximum is not None and len(value) > maximum:
            found.append(Problem(path, f"holds at most {maximum} item{'' if maximum == 1 else 's'}"))
        if schema.get("uniqueItems"):
            seen: list = []
            for item in value:
                if item in seen:
                    found.append(Problem(path, f"holds {choice([item])} twice"))
                seen.append(item)
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                found += problems(item, schema["items"], root, (*path, index))
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        found += [Problem((*path, key), "is required", "structure") for key in schema.get("required", []) if key not in value]
        for key, item in value.items():
            if key in properties:
                found += problems(item, properties[key], root, (*path, key))
            elif schema.get("additionalProperties") is False:
                found.append(Problem((*path, key), "", "structure"))
            elif isinstance(schema.get("additionalProperties"), dict):
                found += problems(item, schema["additionalProperties"], root, (*path, key))
    return found


def place(path: tuple) -> str:
    text = ""
    for step in path:
        text += f"[{step}]" if isinstance(step, int) else ("." if text else "") + str(step)
    return text


def and_list(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def problem_text(problem: Problem, version: SchemaVersion) -> str:
    message = problem.message
    if not message:  # a key that the schema does not state
        parents = problem.path[:-1]
        if not parents:
            message = "is not a setting of the configuration file"
        elif parents == ("parts",) and problem.path[-1] in version.always:
            message = "always runs and has no settings; the configuration file cannot state it"
        elif parents == ("parts",):
            message = f"is not a part of this pipeline; its parts are {and_list(sorted(version.parts))}"
        elif len(parents) == 2 and parents[0] == "parts":
            message = f"is not a setting of the part {parents[1]}"
        else:
            message = "is not a setting here"
    where = place(problem.path)
    return f"{where}: {message}" if where else f"the configuration file: {message}"


# --- the configuration file -----------------------------------------------------------------------------------------


def read_configuration(text: str) -> dict:
    """The document of a configuration file, with a part that is stated without a value as a part without values."""
    try:
        document = parse_yaml(text)
    except YamlError as error:
        raise PlanError(f"the file is not YAML that the pipeline reads: {error}") from error
    if not isinstance(document, dict):
        raise PlanError(f"the configuration file: expected a mapping, found {type_name(document)}")
    parts = document.get("parts")
    if isinstance(parts, dict):
        document["parts"] = {name: {} if value is None else value for name, value in parts.items()}
    return document


def checked_configuration(text: str, versions: dict[int, SchemaVersion]) -> tuple[dict, SchemaVersion]:
    """The configuration and the schema version it matches; a PlanError names every mismatch."""
    document = read_configuration(text)
    current = versions[max(versions)]
    stated = document.get("version")
    if "version" in document and not (has_type(stated, "integer") and stated in versions):
        reads = f"the version{'s' if len(versions) > 1 else ''} {and_list([str(number) for number in sorted(versions, reverse=True)])}"
        raise PlanError(f"version: {stated} is not a version this pipeline reads; it reads {reads}")
    version = versions[stated] if "version" in document else current
    found = problems(document, version.schema, version.schema)
    if found:
        raise PlanError("\n".join(problem_text(problem, version) for problem in found))
    return document, version


# --- the changed files and the base ---------------------------------------------------------------------------------


def git(root: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True, encoding="utf-8")


def comparison_start(root: Path, base: str | None, head: str | None, compare: str) -> str | None:
    """The commit that the head is compared with: the base, or for a pull request its merge base with the head; None
    when it cannot be determined."""
    if not base or not head or ALL_ZERO.match(base):
        return None
    for commit in (base, head):
        if git(root, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}").returncode != 0:
            return None
    if compare == "merge-base":
        merge_base = git(root, "merge-base", base, head)
        return merge_base.stdout.strip() if merge_base.returncode == 0 else None
    return base


def changed_files(root: Path, start: str, head: str) -> list[str] | None:
    """The paths that differ between the commits, a renamed file under its old and its new path; None when Git fails."""
    listing = git(root, "diff", "--name-only", "--no-renames", "-z", start, head)
    if listing.returncode != 0:
        return None
    return sorted(path for path in listing.stdout.split("\0") if path)


def pattern_expression(pattern: str) -> re.Pattern:
    """The paths that a pattern of ``paths`` matches, as the schema describes them."""
    if pattern.endswith("/"):
        return re.compile(re.escape(pattern) + r".*")
    expression, index = "", 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            expression, index = expression + r"(?:.*/)?", index + 3
        elif pattern.startswith("**", index):
            expression, index = expression + r".*", index + 2
        elif pattern[index] == "*":
            expression, index = expression + r"[^/]*", index + 1
        elif pattern[index] == "?":
            expression, index = expression + r"[^/]", index + 1
        else:
            expression, index = expression + re.escape(pattern[index]), index + 1
    return re.compile(expression)


def first_match(patterns: list[str], changed: list[str]) -> str | None:
    expressions = [pattern_expression(pattern) for pattern in patterns]
    return next((path for path in changed if any(expression.fullmatch(path) for expression in expressions)), None)


def base_configuration(root: Path, start: str | None) -> dict | None:
    """The configuration file at the start of the comparison, read without the schema; None when it has none to read."""
    if start is None:
        return None
    shown = git(root, "show", f"{start}:{CONFIGURATION}")
    if shown.returncode != 0:
        return None
    try:
        return read_configuration(shown.stdout)
    except PlanError:
        return None


def stated_parts(configuration: dict | None) -> dict:
    parts = configuration.get("parts") if configuration else None
    return parts if isinstance(parts, dict) else {}


def registered_workflows(configuration: dict | None) -> list[str]:
    entries = configuration.get("local-workflows") if configuration else None
    return [entry["workflow"] for entry in entries if isinstance(entry, dict) and isinstance(entry.get("workflow"), str)] if isinstance(entries, list) else []


LOOKUP_CASES = ("identical", "behind", "pull-request", "own")
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")


def parse_digests(value: object) -> dict[str, str] | None:
    """Validate a shared CI digests mapping (non-empty dict of non-empty name to 64-hex SHA-256); None when invalid."""
    if (not isinstance(value, dict) or not value
            or not all(isinstance(k, str) and k and isinstance(v, str) and HEX_DIGEST.fullmatch(v) for k, v in value.items())):
        return None
    return {k: value[k] for k in sorted(value)}


def moved_digest_names(current: dict[str, str], other: dict[str, str]) -> set[str]:
    """Digest names whose value differs between ``current`` and ``other``; a name absent on either side counts as moved."""
    return {name for name in set(current) | set(other) if current.get(name) != other.get(name)}


@dataclass(frozen=True)
class ResolvedChange:
    """The resolved change range, changed paths and push-lookup context of a run."""
    start: str | None
    changed: list[str] | None
    identical_reason: str | None = None
    tested_base: str | None = None
    lookup_case: str | None = None
    tested_digests: object = None


def read_push_lookup(path: str | None) -> dict | None:
    """The push lookup document written by ``suite_selection.py lookup``, or None when absent or unreadable."""
    if not path:
        return None
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("case") not in LOOKUP_CASES or not isinstance(raw.get("head"), str) or not raw["head"]:
        return None
    if raw["case"] == "behind" and not (isinstance(raw.get("tested_base"), str) and isinstance(raw.get("landing_base"), str)):
        return None
    return raw


def resolve_change(root: Path, base: str | None, head: str | None, compare: str,
                   lookup: str | None) -> ResolvedChange:
    """Resolve the comparison range, changed paths and push-lookup metadata for the run's change."""
    if lookup is not None:
        found = read_push_lookup(lookup)
        if found is None:
            return ResolvedChange(None, None)
        case, target, landing = found["case"], found["head"], found.get("landing_base")
        start = comparison_start(root, landing, target, "direct")
        if case == "identical":
            changed = changed_files(root, start, target) if start else None
            pull = found.get("pull_request")
            reason = f"the tree equals the tested tree that passed on pull request #{pull}" if isinstance(pull, int) else "the tree equals a passed tested tree"
            return ResolvedChange(start, changed, identical_reason=reason, lookup_case=case, tested_digests=found.get("tested_digests"))
        if case == "behind":
            tested_base = comparison_start(root, found.get("tested_base"), landing, "direct")
            pr_changed = changed_files(root, start, target) if start else None
            base_changed = changed_files(root, tested_base, landing) if tested_base and landing else None
            changed = sorted({*pr_changed, *base_changed}) if pr_changed is not None and base_changed is not None else None
            return ResolvedChange(start, changed, tested_base=tested_base, lookup_case=case, tested_digests=found.get("tested_digests"))
        changed = changed_files(root, start, target) if start else None
        return ResolvedChange(start, changed, lookup_case=case)
    start = comparison_start(root, base, head, compare)
    changed = changed_files(root, start, head) if start and head else None
    return ResolvedChange(start, changed)


# --- the plan -------------------------------------------------------------------------------------------------------


def supports_paths(name: str, version: SchemaVersion) -> bool:
    """Whether the schema of part ``name`` declares a ``paths`` property."""
    part_schema = version.schema["properties"]["parts"]["properties"].get(name)
    return isinstance(part_schema, dict) and "paths" in part_schema.get("properties", {})


def declared_input_patterns(name: str, config: dict) -> list[str]:
    """Input path patterns implied by a part's declared project, working directory, install file, or script."""
    patterns: list[str] = []
    script = config.get("script")
    if isinstance(script, str) and script:
        patterns.append(script)
    projects = config.get("projects")
    if isinstance(projects, list):
        for project in projects:
            if not isinstance(project, dict):
                continue
            proj = project.get("project")
            if isinstance(proj, str) and proj:
                normalized = proj.rstrip("/")
                if normalized:
                    patterns.extend((normalized, normalized + "/"))
            wd = project.get("working-directory")
            cleaned_wd = wd.strip("/") if isinstance(wd, str) else ""
            if cleaned_wd and cleaned_wd != ".":
                patterns.append(cleaned_wd + "/")
            elif name == "node-test":
                patterns.extend(("package.json", "package-lock.json", ".nvmrc"))
            install = project.get("install")
            if isinstance(install, str) and install:
                prefix = "" if not cleaned_wd or cleaned_wd == "." else cleaned_wd + "/"
                if install.endswith(".txt"):
                    patterns.append(prefix + install.lstrip("./"))
                else:
                    patterns.extend(prefix + manifest for manifest in ("pyproject.toml", "setup.py", "setup.cfg"))
    return list(dict.fromkeys(patterns))


def tracked_files(root: Path) -> set[str] | None:
    """The tracked files of the checkout at ``root``, or None when Git cannot list them."""
    listing = git(root, "ls-files", "-z")
    if listing.returncode != 0:
        return None
    return {path for path in listing.stdout.split("\0") if path}


def unmatched_paths(parts: dict, workflow_parts: list[str], tracked: set[str] | None) -> dict[str, list[str]]:
    """Per switched-on part, each pattern in its ``paths`` that matches no tracked file."""
    if tracked is None:
        return {}
    unmatched: dict[str, list[str]] = {}
    for name in workflow_parts:
        patterns = parts.get(name, {}).get("paths")
        if not isinstance(patterns, list):
            continue
        missing = [pattern for pattern in patterns if not any(pattern_expression(pattern).fullmatch(path) for path in tracked)]
        if missing:
            unmatched[name] = missing
    return unmatched


# The plan's own fallbacks to running more, by the reason of a part's decision: the counted reason that the plan adds
# to the fallbacks of the suite selection, as the suite selection counts its own (``reuse.fallback_notice``).
DIGEST_FALLBACK_SUFFIX = "digest could not be compared"
TESTED_DIGEST_FALLBACK_SUFFIX = "digest could not be compared with the tested digests"
CHANGED_FILES_UNKNOWN = "the changed files are unknown"
PLAN_FALLBACKS = {
    TESTED_DIGEST_FALLBACK_SUFFIX: "unreadable-tested-digests",
    DIGEST_FALLBACK_SUFFIX: "unreadable-shared-digests",
    CHANGED_FILES_UNKNOWN: "changed-files-unknown",
}


def moved_part_digests(path: str | None, *, lookup_case: str | None = None,
                       tested_digests: object = None) -> tuple[set[str] | None, str]:
    """The digest names that moved compared with ``tested_digests`` on ``identical``/``behind`` pushes or with
    ``previous`` in ``shared-lookup.json`` otherwise, paired with the fallback suffix to use when the comparison fails.
    A digest present on either side and missing on the other counts as moved. Returns an empty set when ``path`` is None."""
    if not path:
        return set(), DIGEST_FALLBACK_SUFFIX
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, DIGEST_FALLBACK_SUFFIX
    digests = parse_digests(raw.get("digests")) if isinstance(raw, dict) else None
    if digests is None:
        return None, DIGEST_FALLBACK_SUFFIX
    if lookup_case in ("identical", "behind"):
        tested = parse_digests(tested_digests)
        if tested is None:
            return None, TESTED_DIGEST_FALLBACK_SUFFIX
        return moved_digest_names(digests, tested), TESTED_DIGEST_FALLBACK_SUFFIX
    previous = parse_digests(raw.get("previous"))
    if previous is None:
        return None, DIGEST_FALLBACK_SUFFIX
    return moved_digest_names(digests, previous), DIGEST_FALLBACK_SUFFIX


def decide(name: str, version: SchemaVersion, parts: dict, base_parts: dict, changed: list[str] | None,
           identical_reason: str | None = None, tested_base_parts: dict | None = None,
           moved_digests: set[str] | None = None,
           digest_fallback_suffix: str = DIGEST_FALLBACK_SUFFIX) -> tuple[bool, str, bool]:
    """Whether a part runs, with the reason and whether a passed result was taken over."""
    if name in version.always:
        return True, "always runs", False
    if name not in parts:
        return False, "switched off", False
    part_digests = PART_DIGESTS.get(name, ())
    if part_digests:
        if moved_digests is None:
            return True, f"`{part_digests[0]}` {digest_fallback_suffix}", False
        moved = next((digest for digest in part_digests if digest in moved_digests), None)
        if moved is not None:
            return True, f"`{moved}` digest moved", False
    if identical_reason is not None:
        return False, identical_reason, True
    patterns = parts[name].get("paths")
    if patterns is None:
        return (True, INPUTS_UNKNOWN_REASON, False) if supports_paths(name, version) else (True, "switched on", False)
    if changed is None:
        return True, CHANGED_FILES_UNKNOWN, False
    if base_parts.get(name) != parts[name] or (tested_base_parts is not None and tested_base_parts.get(name) != parts[name]):
        return True, "its configuration changed", False
    all_patterns = [*patterns, *declared_input_patterns(name, parts[name])]
    matched = first_match(all_patterns, changed) or shared_input_changed(name, changed)
    return (True, f"{matched} changed", False) if matched else (False, "no changed path matches its paths", False)


def own_fallbacks(decisions: dict) -> list[tuple[str, str]]:
    """The plan's own fallbacks, one per running part whose decision fell back to running it: the counted reason and
    the detail of its notice, in the order of the parts."""
    found = []
    for name, entry in decisions.items():
        reason = entry.get("reason") if isinstance(entry, dict) and entry.get("run") is True else None
        if not isinstance(reason, str):
            continue
        key = next((counted for suffix, counted in PLAN_FALLBACKS.items() if reason.endswith(suffix)), None)
        if key is not None:
            found.append((key, f"`{name}` runs: {reason}"))
    return found


def read_hygiene_fallbacks(path: str | None) -> dict[str, int]:
    """The hygiene fallbacks that ``hygiene.py changes --fallbacks`` counted by reason; none when the file is absent
    or unreadable."""
    if not path:
        return {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {name: count for name, count in raw.items()
            if isinstance(name, str) and isinstance(count, int) and not isinstance(count, bool) and count > 0}


def fallback_notice(reason: str, detail: str) -> str:
    """The notice of one fallback, in the form of the suite selection's ``reuse.fallback_notice``."""
    return f"::notice title=ci-fallback::{reason}: {detail}"


def read_unit_decisions(path: str | None) -> dict | None:
    """The per-unit selection decisions written by ``suite_selection.py select --unit-decisions``, or None."""
    if not path:
        return None
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("run"), bool) or not isinstance(raw.get("units"), dict):
        return None
    return raw


def combine_python_projects(decisions: dict) -> None:
    """Fold matching python-verify projects into python-test so each Python project installs once per run."""
    verify, test = decisions.get("python-verify"), decisions.get("python-test")
    if not (verify and verify.get("run") and test and test.get("run")):
        return
    verify_projects = verify.get("config", {}).get("projects")
    test_projects = test.get("config", {}).get("projects")
    if not isinstance(verify_projects, list) or not isinstance(test_projects, list):
        return
    updated_test = [dict(project) for project in test_projects]
    remaining_verify = []
    for verify_project in verify_projects:
        verify_dir = verify_project.get("working-directory", ".")
        verify_version = verify_project.get("python-version")
        verify_install = verify_project.get("install", "")
        matched = False
        for test_project in updated_test:
            if (
                not test_project.get("verify")
                and test_project.get("runner-label", "ubuntu-26.04") == "ubuntu-26.04"
                and test_project.get("python-version") == verify_version
                and test_project.get("working-directory", ".") == verify_dir
                and verify_install in ("", test_project.get("install", ""))
            ):
                test_project["verify"] = True
                test_project["format-check"] = bool(verify_project.get("format-check", False))
                matched = True
                break
        if not matched:
            remaining_verify.append(verify_project)
    if len(remaining_verify) != len(verify_projects):
        test["config"] = {**test["config"], "projects": updated_test}
        verify["config"] = {**verify["config"], "projects": remaining_verify}
        if not remaining_verify:
            verify["run"] = False
            verify["reason"] = "combined into python-test"


def github_get(api_url: str, repository: str, token: str | None, path: str, timeout: float = API_TIMEOUT) -> object:
    """Read one JSON endpoint under ``repos/<repository>/<path>``."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{api_url.rstrip('/')}/repos/{repository}/{path}", headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 - the API host of the run
        return json.loads(response.read().decode("utf-8"))


def short_job_name(job: dict) -> str:
    return str(job.get("name", "")).rsplit(" / ", 1)[-1]


def code_plan_name(pull_request: int) -> str:
    return f"plan (PR #{pull_request})"


def code_run_identity(jobs: list[dict], pull_request: int) -> bool | None:
    """True for a code plan, false for an edit or a different PR, None before identification."""
    for job in jobs:
        name = short_job_name(job)
        if name == EDIT_PLAN_JOB:
            return False
        if name.startswith("plan (PR #"):
            return name == code_plan_name(pull_request)
        if name == PLAN_JOB:
            steps = {step.get("name"): step.get("conclusion") for step in (job.get("steps") or [])
                     if isinstance(step, dict)}
            if steps.get(RECORD_TREE_STEP) == "success" or steps.get(PLAN_CHANGES_STEP) == "success":
                return True
            if job.get("conclusion") == "success" and steps.get(PLAN_CHANGES_STEP) == "skipped":
                return False
            if steps.get(RECORD_TREE_STEP) == "skipped" and steps.get("Check the configuration file and decide which parts run") == "success":
                return False
    return None


def code_failures_of_run(run: dict, jobs: list[dict], verdict: dict, head: str, pull_request: int) -> list[str]:
    """Every non-title, non-reference failure in a completed code run of ``head``."""
    run_id = run.get("id")
    failures: list[str] = []
    title_or_ref_failed = False
    for job in jobs:
        job_name = str(job.get("name", ""))
        short_name = short_job_name(job)
        if short_name == code_plan_name(pull_request):
            short_name = PLAN_JOB
        if short_name == VERDICT_JOB:
            continue
        conclusion = job.get("conclusion")
        if short_name in (PLAN_JOB, HYGIENE_JOB):
            if conclusion in ("success", "skipped"):
                continue
            allowed = REFERENCES_STEP if short_name == PLAN_JOB else TITLE_STEP
            bad_steps = [step.get("name") for step in (job.get("steps") or [])
                         if isinstance(step, dict) and step.get("conclusion") in (*FAILING_RESULTS, "action_required")]
            if conclusion == "failure" and bad_steps and all(step == allowed for step in bad_steps):
                title_or_ref_failed = True
            else:
                failed_checks = [step for step in bad_steps if isinstance(step, str) and step != allowed]
                if failed_checks:
                    failures.extend(f"`{step}` in `{job_name}` failed in run {run_id}" for step in failed_checks)
                else:
                    failures.append(f"`{job_name}` ended {conclusion} in run {run_id}")
        elif conclusion not in ("success", "skipped"):
            failures.append(f"`{job_name}` ended {conclusion} in run {run_id}")
    if not failures and (verdict.get("conclusion") != "success" or run.get("conclusion") != "success"):
        only_text_failed = title_or_ref_failed and any(
            isinstance(step, dict) and step.get("name") == TEXT_ONLY_FAILURE_STEP and step.get("conclusion") == "success"
            for step in (verdict.get("steps") or [])
        )
        if not only_text_failed:
            failures.append(f"code run {run_id} for `{head}` did not pass its verdict or establish that only pull request text checks failed")
    return failures


def wait_for_code_results(head: str, pull_request: int, workflow_ref: str, run_id: str) -> dict:
    """Wait for the latest eligible run and its verdict, without mistaking an unclassified run for code."""
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    token = os.environ.get("GITHUB_TOKEN") or None
    if not head or not repository or pull_request < 1:
        return {"run_id": None, "passed": False, "failures": ["the code results of the head could not be looked up: missing repository, head or pull request"]}
    workflow_file = workflow_ref.split("@", 1)[0].rsplit("/", 1)[-1] if workflow_ref else "ci.yml"
    path = f"actions/workflows/{urllib.parse.quote(workflow_file)}/runs?head_sha={urllib.parse.quote(head)}&event=pull_request&per_page=100"
    start = monotonic()
    deadline = start + 60 * code_wait_minutes()
    no_code = f"no eligible code run found for pull request #{pull_request} at `{head}`"
    detail = no_code
    candidate = None
    try:
        while monotonic() < deadline:
            try:
                result, detail, candidate = read_code_result(api_url, repository, token, path, deadline, head, pull_request, run_id)
            except urllib.error.HTTPError as error:
                if error.code not in (408, 429) and error.code < 500:
                    raise
                detail = f"the Actions API answered HTTP {error.code}"
            except (urllib.error.URLError, OSError) as error:
                detail = f"the Actions API could not be read: {error}"
            else:
                if result is not None and monotonic() < deadline:
                    return result
                if not detail and monotonic() >= start + DISCOVERY_SECONDS:
                    return {"run_id": None, "passed": False, "failures": [no_code]}
            remaining = deadline - monotonic()
            if remaining > 0:
                sleep(min(POLL_SECONDS, remaining))
    except (urllib.error.HTTPError, ValueError) as error:
        return {"run_id": candidate, "passed": False, "failures": [f"the code results of `{head}` could not be looked up: {error}"]}
    return {"run_id": candidate, "passed": False, "failures": [f"the wait for code results of `{head}` expired after {code_wait_minutes()} minutes: {detail or no_code}"]}


def read_code_result(api_url: str, repository: str, token: str | None, path: str, deadline: float, head: str,
                     pull_request: int, own_run_id: str) -> tuple[dict | None, str, int | None]:
    def get(endpoint: str) -> object:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise TimeoutError
        return github_get(api_url, repository, token, endpoint, min(API_TIMEOUT, remaining))

    def items(endpoint: str, key: str) -> Iterator[dict]:
        seen = 0
        for page in range(1, 11):
            doc = get(endpoint if page == 1 else f"{endpoint}&page={page}")
            if not isinstance(doc, dict):
                raise ValueError(f"the Actions API returned no object for {endpoint}")
            rows = doc.get(key)
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError(f"the Actions API returned invalid {key} for {endpoint}")
            yield from rows
            seen += len(rows)
            total = doc.get("total_count")
            if len(rows) < 100 or (isinstance(total, int) and seen >= total):
                return
        raise ValueError(f"the Actions API returned too many {key} for {endpoint}")

    for run in items(path, "workflow_runs"):
        run_id = run.get("id")
        if (not isinstance(run_id, int) or isinstance(run_id, bool) or str(run_id) == own_run_id
                or run.get("head_sha") != head or run.get("conclusion") == "cancelled"):
            continue
        associated = {pr.get("number") for pr in (run.get("pull_requests") or []) if isinstance(pr, dict)}
        if associated and pull_request not in associated:
            continue
        jobs = list(items(f"actions/runs/{run_id}/jobs?per_page=100", "jobs"))
        identity = code_run_identity(jobs, pull_request)
        if identity is False:
            continue
        if identity is None:
            detail = f"run {run_id} cannot be identified as a code run"
            if run.get("status") != "completed":
                return None, detail, None
            return {"run_id": None, "passed": False, "failures": [detail]}, detail, None
        if run.get("status") != "completed":
            return None, f"code run {run_id} is still {run.get('status')}", run_id
        verdict = next((job for job in jobs if short_job_name(job) == VERDICT_JOB), None)
        if verdict is None or verdict.get("status") != "completed":
            return None, f"code run {run_id} has no completed `ci / verdict`", run_id
        if not associated and not any(short_job_name(job) == code_plan_name(pull_request) for job in jobs):
            failure = verify_legacy_code_run(api_url, repository, token, run_id, pull_request, head)
            if failure:
                return {"run_id": run_id, "passed": False, "failures": [failure]}, failure, run_id
            if monotonic() >= deadline:
                return None, f"could not verify the pull request of code run {run_id} within the wait", run_id
        failures = code_failures_of_run(run, jobs, verdict, head, pull_request)
        if verdict.get("conclusion") not in ("success", "failure"):
            failures.append(f"`ci / verdict` ended {verdict.get('conclusion')} in code run {run_id}")
        return {"run_id": run_id, "passed": not failures, "failures": failures}, "", run_id
    return None, "", None


def verify_legacy_code_run(api_url: str, repository: str, token: str | None,
                           run_id: int, pull_request: int, head: str) -> str:
    """Runs from before the plan name carried the PR number can prove it with their tested state."""
    sys.path.insert(0, str(PART_FOLDER.parent / "suite-selection"))
    try:
        from reuse import Api, ApiError, read_tested_state_artifact

        try:
            state, _, detail = read_tested_state_artifact(Api(api_url, repository, token), run_id, pull_request, head)
            if state is not None and state.get("run_id") != run_id:
                detail = f"the tested state of code run {run_id} names a different run"
        except ApiError as error:
            detail = f"the tested state of code run {run_id} could not be read: {error}"
        return detail
    finally:
        sys.path.pop(0)


def code_wait_minutes() -> int:
    """Bound the longest dependency chain of a code run, with time for runner handoffs."""
    timeouts = JOB_TIMEOUT_MINUTES
    suites = max(timeouts[SUITE_SELECTION_LINUX], timeouts[SUITE_SELECTION_WINDOWS]) + timeouts[SUITE_SELECTION_RESULTS]
    parts = max(DEFAULT_JOB_TIMEOUT_MINUTES, *(minutes for name, minutes in timeouts.items()
                                               if name not in (PLAN_JOB, VERDICT_JOB, WAIT_FOR_CODE_JOB)))
    return timeouts[PLAN_JOB] + max(suites, parts) + timeouts[VERDICT_JOB] + 5


def job_timeouts(version: SchemaVersion, workflow: str) -> dict[str, int]:
    """The timeout-minutes limit for every job of ``workflow``."""
    timeouts: dict[str, int] = {PLAN_JOB: JOB_TIMEOUT_MINUTES[PLAN_JOB]}
    for name in version.parts_of(workflow):
        if workflow == PIPELINE and name == SUITE_SELECTION_JOB:
            timeouts[SUITE_SELECTION_LINUX] = JOB_TIMEOUT_MINUTES[SUITE_SELECTION_LINUX]
            timeouts[SUITE_SELECTION_WINDOWS] = JOB_TIMEOUT_MINUTES[SUITE_SELECTION_WINDOWS]
            timeouts[SUITE_SELECTION_RESULTS] = JOB_TIMEOUT_MINUTES[SUITE_SELECTION_RESULTS]
        else:
            timeouts[name] = JOB_TIMEOUT_MINUTES.get(name, DEFAULT_JOB_TIMEOUT_MINUTES)
    if workflow == PIPELINE:
        timeouts[VERDICT_JOB] = JOB_TIMEOUT_MINUTES[VERDICT_JOB]
    return timeouts


def make_plan(root: Path, versions: dict[int, SchemaVersion], base: str | None, head: str | None, compare: str,
              workflow: str = PIPELINE, lookup: str | None = None, unit_decisions: str | None = None,
              event: str = "", event_action: str = "",
              iso_week: int | None = None, shared_lookup: str | None = None, hygiene_fallbacks: str | None = None) -> dict:
    path = root / CONFIGURATION
    if not path.is_file():
        raise PlanError(f"{CONFIGURATION} does not exist. The smallest valid file is the line `version: {max(versions)}`.")
    configuration, version = checked_configuration(path.read_text(encoding="utf-8"), versions)
    change = resolve_change(root, base, head, compare, lookup)
    before = base_configuration(root, change.start) if change.changed is not None else None
    tested_before = base_configuration(root, change.tested_base) if change.changed is not None and change.tested_base is not None else None
    parts, base_parts = stated_parts(configuration), stated_parts(before)
    tested_base_parts = stated_parts(tested_before) if change.tested_base is not None else None
    workflow_parts = version.parts_of(workflow)
    moved_digests, digest_fallback_suffix = moved_part_digests(shared_lookup, lookup_case=change.lookup_case, tested_digests=change.tested_digests)
    departures: list[dict] = [
        {"part": name, "reason": INPUTS_UNKNOWN_REASON}
        for name in workflow_parts
        if name in parts and supports_paths(name, version) and parts[name].get("paths") is None
    ]
    unmatched = unmatched_paths(parts, workflow_parts, tracked_files(root))
    decisions = {}
    edited = event == "pull_request" and event_action == "edited" and workflow == PIPELINE
    if edited:
        for name in workflow_parts:
            if name == HYGIENE_JOB:
                entry: dict = {"run": True, "reason": "title and description checks", "config": parts.get(name, {})}
            elif name not in parts:
                entry = {"run": False, "reason": "switched off", "config": {}}
            else:
                entry = {"run": False, "reason": "edited pull request", "config": parts.get(name, {})}
            if name in parts and "setups" in version.schema["properties"]["parts"]["properties"][name].get("properties", {}):
                entry["setups"] = [version.setups[setup] for setup in parts[name].get("setups", [])]
            decisions[name] = entry
    else:
        for name in workflow_parts:
            run, reason, reused = decide(name, version, parts, base_parts, change.changed, change.identical_reason, tested_base_parts,
                                         moved_digests, digest_fallback_suffix)
            entry = {"run": run, "reason": reason, "config": parts.get(name, {})}
            if reused:
                entry["reused"] = True
            if name in parts and "setups" in version.schema["properties"]["parts"]["properties"][name].get("properties", {}):
                entry["setups"] = [version.setups[setup] for setup in parts[name].get("setups", [])]
            decisions[name] = entry
        combine_python_projects(decisions)
        if workflow == AUTOMATION and event == "schedule" and FULL_RECORDING_JOB in decisions and decisions[FULL_RECORDING_JOB]["run"]:
            week = datetime.now(timezone.utc).isocalendar().week if iso_week is None else iso_week
            if week % 2 != 0:
                decisions[FULL_RECORDING_JOB]["run"] = False
                decisions[FULL_RECORDING_JOB]["reason"] = "odd ISO week"
        selected_units = read_unit_decisions(unit_decisions)
        if selected_units is not None and SUITE_SELECTION_JOB in decisions and SUITE_SELECTION_JOB in parts:
            suite_entry = decisions[SUITE_SELECTION_JOB]
            suite_entry["run"] = bool(selected_units["run"])
            if isinstance(selected_units.get("reason"), str) and selected_units["reason"]:
                suite_entry["reason"] = selected_units["reason"]
            if not suite_entry["run"] and selected_units.get("reused") is True:
                suite_entry["reused"] = True
            else:
                suite_entry.pop("reused", None)
    known = registered_workflows(before)
    result: dict = {
        "version": version.version,
        "parts": decisions,
        "timeouts": job_timeouts(version, workflow),
        "departures": departures,
        "unmatched-paths": unmatched,
        "compared": change.changed is not None,
        "switched-off": sorted(name for name in base_parts if name not in parts and name not in version.always),
        "local-workflows-added": [workflow for workflow in registered_workflows(configuration) if workflow not in known],
    }
    fallbacks: dict[str, int] = {}
    if not edited and (selected_units := read_unit_decisions(unit_decisions)) is not None:
        result["units"] = selected_units["units"]
        if isinstance(selected_units.get("shards"), list):
            shards = [
                {"shard": item["shard"], "estimate": float(item["estimate"])}
                for item in selected_units["shards"]
                if isinstance(item, dict)
                and isinstance(item.get("shard"), int)
                and not isinstance(item.get("shard"), bool)
                and isinstance(item.get("estimate"), (int, float))
                and not isinstance(item.get("estimate"), bool)
            ]
            if shards:
                result["shards"] = shards
        if isinstance(selected_units.get("fallbacks"), dict):
            fallbacks.update({name: count for name, count in selected_units["fallbacks"].items()
                              if isinstance(name, str) and isinstance(count, int) and count > 0})
    for reason, _ in own_fallbacks(decisions):
        fallbacks[reason] = fallbacks.get(reason, 0) + 1
    for reason, count in read_hygiene_fallbacks(hygiene_fallbacks).items():
        fallbacks[reason] = fallbacks.get(reason, 0) + count
    if fallbacks:
        result["fallbacks"] = dict(sorted(fallbacks.items()))
    if edited:
        result["wait-for-code"] = True
        result["timeouts"][WAIT_FOR_CODE_JOB] = JOB_TIMEOUT_MINUTES[WAIT_FOR_CODE_JOB]
    return result


def comparison_lines(plan: dict) -> list[str]:
    """What the configuration file weakens compared with the base: the parts switched off and the local workflows added."""
    if not plan.get("compared"):
        return ["The configuration file could not be compared with the base: the parts switched off and the local workflows registered by this change are unknown."]
    lines = []
    for title, items, none in (("Parts switched off compared with the base", plan.get("switched-off", []), "No part is switched off compared with the base."),
                               ("Local workflows registered compared with the base", plan.get("local-workflows-added", []),
                                "No local workflow is registered compared with the base.")):
        lines += [f"{title}:", "", *[f"- `{item}`" for item in items], ""] if items else [none, ""]
    return lines[:-1]


def plan_markdown(plan: dict) -> str:
    lines = ["## Pipeline plan", "", f"`{CONFIGURATION}` matches schema version {plan['version']}.", "", "| Part | Runs | Reason |", "| --- | --- | --- |"]
    for name, part in plan["parts"].items():
        status = "yes" if part["run"] else ("no (taken over)" if part.get("reused") is True else "no")
        lines.append(f"| `{name}` | {status} | {part['reason']} |")
    units = plan.get("units")
    if isinstance(units, dict) and units:
        lines += ["", "### Units", "", "| Unit | Runs | Reason |", "| --- | --- | --- |"]
        for unit_name, unit in units.items():
            status = "yes" if unit.get("run") else ("no (taken over)" if unit.get("reused") is True else "no")
            lines.append(f"| `{unit_name}` | {status} | {unit.get('reason', '')} |")
    departures = plan.get("departures")
    if isinstance(departures, list) and departures:
        lines += ["", "### Departures", ""]
        lines += [f"- `{item['part']}`: {item['reason']}" for item in departures if isinstance(item, dict)]
    unmatched = plan.get("unmatched-paths")
    if isinstance(unmatched, dict) and unmatched:
        lines += ["", "### Unmatched paths", ""]
        for part_name, patterns in unmatched.items():
            for pattern in patterns:
                lines.append(f"- `{part_name}`: `{pattern}` matches no tracked file")
    if plan.get("wait-for-code"):
        lines += ["", "The verdict waits for the code results of this head."]
    fallbacks = plan.get("fallbacks")
    if isinstance(fallbacks, dict) and fallbacks:
        counts = ", ".join(f"`{name}` ({count})" for name, count in sorted(fallbacks.items()))
        lines += ["", f"Fallbacks: {counts}."]
    return "\n".join([*lines, "", *comparison_lines(plan)]) + "\n"


def append(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(text)


def run_plan(options: argparse.Namespace) -> int:
    try:
        versions = schema_versions(Path(options.schemas))
        plan = make_plan(Path(options.root), versions, options.base, options.head, options.compare,
                         options.shared_workflow, options.lookup, options.unit_decisions,
                         options.event, options.event_action, options.iso_week,
                         options.shared_lookup, options.hygiene_fallbacks)
    except (PlanError, SchemaError) as error:
        lines = str(error).splitlines()
        print(f"The plan failed: {CONFIGURATION} cannot be used.")
        print("\n".join(lines))
        append(options.summary, "\n".join(["## Pipeline plan", "", f"**Failed.** `{CONFIGURATION}` cannot be used:", "", *[f"- {line}" for line in lines]]) + "\n")
        return 1
    for name, part in plan["parts"].items():
        print(f"{name}: {'runs' if part['run'] else 'does not run'} ({part['reason']})")
    for departure in plan.get("departures", []):
        print(f"departure: {departure['part']} ({departure['reason']})")
    for reason, detail in own_fallbacks(plan["parts"]):
        print(fallback_notice(reason, detail))
    for part_name, patterns in plan.get("unmatched-paths", {}).items():
        for pattern in patterns:
            print(f"unmatched path pattern: {part_name}: {pattern}")
    append(options.github_output, f"plan={json.dumps(plan, separators=(',', ':'))}\n")
    if options.output:
        destination = Path(options.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(plan, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
    append(options.summary, plan_markdown(plan))
    return 0


def run_wait_for_code(options: argparse.Namespace) -> int:
    result = wait_for_code_results(options.head, options.pull_request, options.workflow_ref, options.run_id)
    append(options.github_output, f"code-results={json.dumps(result, separators=(',', ':'))}\n")
    message = (f"Code results of the head: passed in run {result['run_id']}." if result["passed"]
               else "Code results of the head: " + "; ".join(result["failures"]) + ".")
    print(message)
    append(options.summary, message + "\n")
    return 0


# --- the verdict ----------------------------------------------------------------------------------------------------


def parse_job_plan(needs: dict) -> tuple[dict | None, str | None]:
    """The validated plan in the outputs of the ``plan`` job, or None with the reason when missing or invalid."""
    plan_job = needs.get(PLAN_JOB)
    outputs = plan_job.get("outputs") if isinstance(plan_job, dict) else None
    raw = outputs.get("plan") if isinstance(outputs, dict) else None
    if not isinstance(raw, str) or not raw:
        return None, f"the `{PLAN_JOB}` job gave no plan output"
    try:
        plan = json.loads(raw)
    except ValueError:
        return None, f"the `{PLAN_JOB}` job gave an invalid plan output"
    if not isinstance(plan, dict) or not isinstance(plan.get("parts"), dict):
        return None, f"the `{PLAN_JOB}` job gave an invalid plan output"
    for name, part in plan["parts"].items():
        if (not isinstance(name, str) or not isinstance(part, dict) or not isinstance(part.get("run"), bool)
                or ("reused" in part and not isinstance(part["reused"], bool))):
            return None, f"the `{PLAN_JOB}` job gave an invalid plan output"
    return plan, None


def job_plan(needs: dict) -> dict | None:
    """The validated plan in the outputs of the ``plan`` job, when that job gave one."""
    return parse_job_plan(needs)[0]


@dataclass(frozen=True)
class CheckOutcome:
    name: str
    outcome: str
    result: str
    reason: str


def planned_check_outcome(name: str, reason: str, needs: dict) -> tuple[CheckOutcome, str | None]:
    """The outcome row and optional failure reason of a check the plan selected to run."""
    job = needs.get(name)
    result = job.get("result") if isinstance(job, dict) else None
    if not isinstance(result, str) or not result:
        return CheckOutcome(name, "skipped", "never started", reason), f"`{name}` never started"
    if result == "success":
        return CheckOutcome(name, "ran", "success", reason), None
    if result == "skipped":
        return CheckOutcome(name, "skipped", "skipped", reason), f"`{name}` ended skipped"
    return CheckOutcome(name, "ran", result, reason), f"`{name}` ended {result}"


def decode_json_output(outputs: dict, key: str, expected_type: type) -> tuple[object | None, bool]:
    """Decode one optional JSON string output of a job; returns (value, ok)."""
    if key not in outputs or outputs[key] == "":
        return None, True
    raw = outputs[key]
    if not isinstance(raw, str):
        return None, False
    try:
        value = json.loads(raw)
    except ValueError:
        return None, False
    return (value, True) if isinstance(value, expected_type) else (None, False)


def suite_selection_followups(needs: dict, selection_job: str = SUITE_SELECTION_JOB) -> tuple[list[CheckOutcome], list[str]]:
    """Evaluate the follow-up jobs of a planned ``suite-selection`` (either its own job or inside ``plan``)."""
    job_entry = needs.get(selection_job, {})
    outputs = job_entry.get("outputs") if isinstance(job_entry, dict) else None
    if not isinstance(outputs, dict):
        outputs = {}
    shards, shards_ok = decode_json_output(outputs, "shards", list)
    units, units_ok = decode_json_output(outputs, "units", dict)
    reused, reused_ok = decode_json_output(outputs, "reused", dict)
    windows = outputs.get("windows", "")
    if not (shards_ok and units_ok and reused_ok and isinstance(windows, str)):
        return [], [f"`{selection_job}` gave invalid selection output"]
    identical = outputs.get("identical") == "true"
    jobs_planned = bool(shards) or bool(windows) or bool(units)
    if not jobs_planned and "shards" not in outputs and "windows" not in outputs and not identical:
        if SUITE_SELECTION_JOBS in needs:
            return [], [f"`{selection_job}` gave no selection output"]
        return [], []
    reasons: list[str] = []
    if jobs_planned:
        parts = []
        if shards:
            parts.append(f"{len(shards)} Linux shard{'' if len(shards) == 1 else 's'}")  # type: ignore[arg-type]
        if windows:
            parts.append("Windows")
        reason = " and ".join(parts) if parts else "selected units"
        if reused:
            count = len(reused)  # type: ignore[arg-type]
            reason += f"; {count} passed unit{'' if count == 1 else 's'} taken over"
        row, failure = planned_check_outcome(SUITE_SELECTION_JOBS, reason, needs)
        rows = [row]
        if failure:
            reasons.append(failure)
    elif identical:
        rows = [CheckOutcome(SUITE_SELECTION_JOBS, "taken over", "not run", "the tree equals a passed tested tree")]
    elif reused:
        count = len(reused)  # type: ignore[arg-type]
        rows = [CheckOutcome(SUITE_SELECTION_JOBS, "taken over", "not run", f"{count} passed unit{'' if count == 1 else 's'} taken over")]
    else:
        rows = [CheckOutcome(SUITE_SELECTION_JOBS, "skipped", "not run", "no suites selected")]
    if SUITE_SELECTION_RESULTS in needs and isinstance(needs[SUITE_SELECTION_RESULTS], dict):
        store_result = needs[SUITE_SELECTION_RESULTS].get("result")
        if isinstance(store_result, str) and store_result and store_result not in ("success", "skipped"):
            reasons.append(f"`{SUITE_SELECTION_RESULTS}` ended {store_result}")
    return rows, reasons


# The outputs of the ``plan`` job that hold the outcome of each light hygiene check that runs inside it.
LIGHT_CHECKS = {"light-branch": "the pull request branch", "light-references": "the references to repositories of the owner",
                "light-pr-references": "the references in the pull request title and description",
                "light-consumer-checks": "the consumer checks", "light-coverage": "Dependabot coverage",
                "light-links": "relative links in tracked Markdown"}


def light_check_failures(needs: dict) -> list[str]:
    """The light hygiene checks inside ``plan`` whose step failed, as the plan job's outputs name them."""
    plan_job = needs.get(PLAN_JOB)
    outputs = plan_job.get("outputs") if isinstance(plan_job, dict) else None
    if not isinstance(outputs, dict):
        return []
    return [f"the light check of {label} in `{PLAN_JOB}` failed" for output, label in LIGHT_CHECKS.items() if outputs.get(output) == "failure"]


def edit_code_result(needs: dict) -> tuple[int | None, list[str]]:
    job = needs.get(WAIT_FOR_CODE_JOB, {})
    if job.get("result") != "success":
        return None, [f"the wait for code results did not succeed: `{WAIT_FOR_CODE_JOB}` ended {job.get('result') or 'missing'}"]
    try:
        result = json.loads(job.get("outputs", {}).get("code-results", ""))
    except (TypeError, ValueError):
        result = None
    if isinstance(result, dict):
        if result.get("passed") is True and type(result.get("run_id")) is int and result.get("failures") == []:
            return result["run_id"], []
        failures = result.get("failures")
        if result.get("passed") is False and isinstance(failures, list) and failures and all(isinstance(item, str) for item in failures):
            return None, failures
    return None, ["the wait job gave no usable code result"]


def evaluate_verdict(needs: dict) -> tuple[list[str], dict | None, list[CheckOutcome]]:
    """Evaluate the needs context against the plan; returns (failure reasons, validated plan, check outcomes). A failed
    ``plan`` job that wrote its plan, such as one whose light hygiene check failed, fails the verdict with the failed
    check named, and its planned checks are judged as after a passed ``plan``."""
    reasons: list[str] = []
    if PLAN_JOB not in needs:
        reasons.append(f"the needs context has no `{PLAN_JOB}` job")
    else:
        result = needs[PLAN_JOB].get("result")
        if result != "success":
            reasons.append(f"`{PLAN_JOB}` never started" if not isinstance(result, str) or not result else f"`{PLAN_JOB}` ended {result}")
            reasons += light_check_failures(needs)
        plan, error = parse_job_plan(needs) if result in ("success", "failure") else (None, None)
        if plan is None:
            if result == "success":
                reasons.append(error or f"the `{PLAN_JOB}` job gave no plan output")
        else:
            checks: list[CheckOutcome] = []
            code_run = None
            if plan.get("wait-for-code") is True:
                code_run, code_failures = edit_code_result(needs)
                reasons.extend(code_failures)
                hygiene_outputs = needs.get(HYGIENE_JOB, {}).get("outputs", {})
                for output, label in (("title", "the pull request title"), ("references", "the references in the pull request title and description")):
                    if hygiene_outputs.get(output) == "failure":
                        reasons.append(f"the check of {label} failed")
            for name, part in plan["parts"].items():
                reason = str(part.get("reason", ""))
                if part["run"]:
                    if name == SUITE_SELECTION_JOB and SUITE_SELECTION_JOB not in needs:
                        followup_rows, followup_reasons = suite_selection_followups(needs, selection_job=PLAN_JOB)
                        checks += followup_rows
                        reasons += followup_reasons
                    else:
                        row, failure = planned_check_outcome(name, reason, needs)
                        checks.append(row)
                        if failure:
                            reasons.append(failure)
                        elif name == SUITE_SELECTION_JOB:
                            followup_rows, followup_reasons = suite_selection_followups(needs)
                            checks += followup_rows
                            reasons += followup_reasons
                else:
                    outcome = "taken over" if part.get("reused") is True else "skipped"
                    if code_run is not None and reason == "edited pull request":
                        outcome, reason = "taken over", f"code result from run {code_run}"
                    checks.append(CheckOutcome(name, outcome, "not run", reason))
            return reasons, plan, checks
    if WAIT_FOR_CODE_JOB in needs and needs[WAIT_FOR_CODE_JOB].get("result") != "skipped":
        reasons.extend(edit_code_result(needs)[1])
    reasons += [f"`{name}` ended {job.get('result')}" for name, job in needs.items()
                if name not in (PLAN_JOB, WAIT_FOR_CODE_JOB) and job.get("result") in FAILING_RESULTS]
    return reasons, None, []


def verdict_reasons(needs: dict) -> list[str]:
    """Why the verdict fails: ``plan`` did not succeed, its plan output is missing or invalid, or a planned check did not succeed."""
    return evaluate_verdict(needs)[0]


def verdict_markdown(needs: dict, reasons: list[str], plan: dict | None = None, checks: list[CheckOutcome] | None = None) -> str:
    if plan is None and checks is None:
        _, plan, checks = evaluate_verdict(needs)
    lines = ["## Verdict", ""]
    lines.append("**Failed.** " + "; ".join(reasons) + "." if reasons else "**Passed.** The plan succeeded and every planned check passed or took over a pass.")
    if plan is None:
        lines += ["", f"The `{PLAN_JOB}` job gave no plan: the parts switched off and the local workflows registered by this change are unknown."]
    else:
        lines += ["", *comparison_lines(plan)]
        lines += ["", "| Check | Outcome | Result | Reason |", "| --- | --- | --- | --- |"]
        lines += [f"| `{check.name}` | {check.outcome} | {check.result} | {check.reason} |" for check in (checks or [])]
    lines += ["", "| Job | Result |", "| --- | --- |"]
    lines += [f"| `{name}` | {job.get('result')} |" for name, job in needs.items()]
    return "\n".join(lines) + "\n"


def only_pr_text_failed(needs: dict, plan: dict | None, reasons: list[str]) -> bool:
    """A failed code verdict can be inherited only when every reason is accounted for by the PR text checks."""
    if plan is None or plan.get("wait-for-code") or not reasons:
        return False
    allowed = set()
    if needs[PLAN_JOB].get("outputs", {}).get("light-pr-references") == "failure":
        allowed.update({f"`{PLAN_JOB}` ended failure",
                        f"the light check of {LIGHT_CHECKS['light-pr-references']} in `{PLAN_JOB}` failed"})
    if needs.get(HYGIENE_JOB, {}).get("outputs", {}).get("title") == "failure":
        allowed.add(f"`{HYGIENE_JOB}` ended failure")
    return set(reasons) <= allowed


def run_verdict(options: argparse.Namespace) -> int:
    try:
        needs = json.loads(Path(options.needs).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"The needs context {options.needs} cannot be read: {error}", file=sys.stderr)
        return 2
    if not isinstance(needs, dict) or not all(isinstance(job, dict) for job in needs.values()):
        print(f"The needs context {options.needs} is no object of jobs", file=sys.stderr)
        return 2
    reasons, plan, checks = evaluate_verdict(needs)
    print("Verdict: " + ("failed: " + "; ".join(reasons) if reasons else "passed"))
    for name, job in needs.items():
        print(f"job {name}: {job.get('result')}")
    append(options.summary, verdict_markdown(needs, reasons, plan, checks))
    if options.github_output and only_pr_text_failed(needs, plan, reasons):
        append(options.github_output, "text-only-failure=true\n")
    return 1 if reasons else 0


# --- the assembled schema -------------------------------------------------------------------------------------------


def assembled_text(version: SchemaVersion) -> str:
    return json.dumps(version.schema, indent=2, ensure_ascii=False) + "\n"


def run_schema(options: argparse.Namespace) -> int:
    folder = Path(options.schemas)
    try:
        versions = schema_versions(folder)
    except SchemaError as error:
        print(error, file=sys.stderr)
        return 2
    stale = []
    for number, version in sorted(versions.items()):
        path = folder / f"v{number}" / ASSEMBLED
        text = assembled_text(version)
        if options.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                stale.append(path)
        else:
            path.write_text(text, encoding="utf-8", newline="\n")
            print(f"wrote {path}")
    for path in stale:
        print(f"{path} differs from the schema assembled from base.json and parts/; run `plan.py schema` to write it")
    return 1 if stale else 0


# --- one setting of the configuration file -------------------------------------------------------------------------


def run_setting(options: argparse.Namespace) -> int:
    try:
        versions = schema_versions(Path(options.schemas))
    except SchemaError as error:
        print(error, file=sys.stderr)
        return 2
    path = Path(options.root) / CONFIGURATION
    try:
        if not path.is_file():
            raise PlanError(f"{CONFIGURATION} does not exist")
        configuration, _ = checked_configuration(path.read_text(encoding="utf-8"), versions)
    except PlanError as error:
        print(f"{CONFIGURATION} cannot be used, so the setting {options.part}.{options.setting} is taken as not stated; the plan reports why: "
              + "; ".join(str(error).splitlines()), file=sys.stderr)
        return 0
    value = stated_parts(configuration).get(options.part, {}).get(options.setting, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        print(f"{options.part}.{options.setting} is no list of strings", file=sys.stderr)
        return 2
    for item in value:
        print(item)
    return 0


# --- the command line -----------------------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    main_parser = argparse.ArgumentParser(description="Plan the pipeline from the configuration file and give its verdict.")
    commands = main_parser.add_subparsers(dest="command", required=True)
    planning = commands.add_parser("plan", help="check the configuration file and decide per part whether it runs")
    planning.add_argument("--root", default=".", help="the repository's checkout")
    planning.add_argument("--schemas", default=str(SCHEMAS), help="the schema folder")
    planning.add_argument("--shared-workflow", default=PIPELINE, help="the file name of the shared workflow whose parts the plan decides on")
    planning.add_argument("--base", help="the commit the head is compared with")
    planning.add_argument("--head", help="the commit under test")
    planning.add_argument("--compare", choices=("merge-base", "direct"), default="direct", help="compare the head with its merge base with the base, or with the base itself")
    planning.add_argument("--lookup", help="the push lookup JSON file written by suite_selection.py lookup")
    planning.add_argument("--shared-lookup", help="the shared lookup JSON file written by suite_selection.py shared-lookup")
    planning.add_argument("--unit-decisions", help="the per-unit decisions JSON file written by suite_selection.py select")
    planning.add_argument("--hygiene-fallbacks", help="the hygiene fallbacks JSON file written by hygiene.py changes --fallbacks")
    planning.add_argument("--workflow", default="pipeline", help="the name of the running workflow, for the concurrency groups")
    planning.add_argument("--event", default="", help="the name of the event of the run")
    planning.add_argument("--event-action", default="", help="the action of the event, such as edited on pull_request")
    planning.add_argument("--pull-request", default="", help="the number of the pull request of the run, empty on another event")
    planning.add_argument("--run-id", default="0", help="the id of the run")
    planning.add_argument("--iso-week", type=int, default=None, help="override the UTC ISO week number used for scheduled cadence checks")
    planning.add_argument("--github-output", help="the file that receives the output plan")
    planning.add_argument("--output", help="the file that receives the JSON plan document")
    planning.add_argument("--summary", help="the file that receives the Markdown summary")
    deciding = commands.add_parser("verdict", help="pass or fail on the results of the plan and the parts")
    deciding.add_argument("--needs", required=True, help="a file with toJSON(needs) of the verdict job")
    deciding.add_argument("--summary", help="the file that receives the Markdown summary")
    deciding.add_argument("--github-output", help="the file that receives the verdict's classification of text-check failures")
    waiting = commands.add_parser(WAIT_FOR_CODE_JOB, help="wait for the code verdict of an edited pull request's head")
    waiting.add_argument("--head", required=True)
    waiting.add_argument("--pull-request", type=int, required=True)
    waiting.add_argument("--workflow-ref", required=True)
    waiting.add_argument("--run-id", required=True)
    waiting.add_argument("--github-output", required=True)
    waiting.add_argument("--summary")
    assembling = commands.add_parser("schema", help="write or check the assembled schema of each version")
    assembling.add_argument("--schemas", default=str(SCHEMAS), help="the schema folder")
    assembling.add_argument("--check", action="store_true", help="fail when an assembled schema differs instead of writing it")
    reading = commands.add_parser("setting", help="print the items of one list setting of one part, one per line")
    reading.add_argument("--root", default=".", help="the repository's checkout")
    reading.add_argument("--schemas", default=str(SCHEMAS), help="the schema folder")
    reading.add_argument("--part", required=True, help="the name of the part")
    reading.add_argument("--setting", required=True, help="the name of the setting, a list of strings")
    return main_parser


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    return {"plan": run_plan, "verdict": run_verdict, WAIT_FOR_CODE_JOB: run_wait_for_code,
            "schema": run_schema, "setting": run_setting}[options.command](options)


if __name__ == "__main__":
    sys.exit(main())
