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
from dataclasses import dataclass, field
from pathlib import Path

PART_FOLDER = Path(__file__).resolve().parent
sys.path.insert(0, str(PART_FOLDER.parent / "repository-hygiene"))
from dependabot_coverage import YamlError, parse_yaml  # noqa: E402

CONFIGURATION = ".github/ci-config.yml"
SCHEMAS = PART_FOLDER / "schema"
ASSEMBLED = "ci-config.schema.json"
SETUPS = "setups.json"
PIPELINE = "pipeline.yml"
PLAN_JOB = "plan"
HYGIENE_JOB = "repository-hygiene"
VERDICT_JOB = "verdict"
SUITE_SELECTION_JOB = "suite-selection"
SUITE_SELECTION_JOBS = "suite-selection-jobs"
SUITE_SELECTION_RESULTS = "suite-selection-results"
PLAN_CHANGES_STEP = "Decide which checks the changed files can affect"
TITLE_STEP = "Check the pull request title"
REFERENCES_STEP = "Check the references to repositories of the owner"
API_TIMEOUT = 15
FAILING_RESULTS = ("failure", "cancelled", "timed_out")
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
                   lookup: str | None) -> tuple[str | None, list[str] | None, str | None, str | None]:
    """Return ``(start, changed, identical_reason, tested_base)`` for the run's change."""
    if lookup is not None:
        found = read_push_lookup(lookup)
        if found is None:
            return None, None, None, None
        case, target, landing = found["case"], found["head"], found.get("landing_base")
        start = comparison_start(root, landing, target, "direct")
        if case == "identical":
            changed = changed_files(root, start, target) if start else None
            pull = found.get("pull_request")
            reason = f"the tree equals the tested tree that passed on pull request #{pull}" if isinstance(pull, int) else "the tree equals a passed tested tree"
            return start, changed, reason, None
        if case == "behind":
            tested_base = comparison_start(root, found.get("tested_base"), landing, "direct")
            pr_changed = changed_files(root, start, target) if start else None
            base_changed = changed_files(root, tested_base, landing) if tested_base and landing else None
            changed = sorted({*pr_changed, *base_changed}) if pr_changed is not None and base_changed is not None else None
            return start, changed, None, tested_base
        changed = changed_files(root, start, target) if start else None
        return start, changed, None, None
    start = comparison_start(root, base, head, compare)
    changed = changed_files(root, start, head) if start and head else None
    return start, changed, None, None


# --- the plan -------------------------------------------------------------------------------------------------------


def decide(name: str, version: SchemaVersion, parts: dict, base_parts: dict, changed: list[str] | None,
           identical_reason: str | None = None, tested_base_parts: dict | None = None) -> tuple[bool, str, bool]:
    """Whether a part runs, with the reason and whether a passed result was taken over."""
    if name in version.always:
        return True, "always runs", False
    if name not in parts:
        return False, "switched off", False
    if identical_reason is not None:
        return False, identical_reason, True
    patterns = parts[name].get("paths")
    if patterns is None:
        return True, "switched on", False
    if changed is None:
        return True, "the changed files are unknown", False
    if base_parts.get(name) != parts[name] or (tested_base_parts is not None and tested_base_parts.get(name) != parts[name]):
        return True, "its configuration changed", False
    matched = first_match(patterns, changed)
    return (True, f"{matched} changed", False) if matched else (False, "no changed path matches its paths", False)


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


def github_get(api_url: str, repository: str, token: str | None, path: str) -> object:
    """Read one JSON endpoint under ``repos/<repository>/<path>``."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(f"{api_url.rstrip('/')}/repos/{repository}/{path}", headers=headers)
    with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:  # noqa: S310 - the API host of the run
        return json.loads(response.read().decode("utf-8"))


def is_edited_run(jobs: list[dict]) -> bool:
    """Whether a run's ``plan`` job skipped change detection, which only an ``edited`` run does."""
    plan_job = next((job for job in jobs if str(job.get("name", "")).rsplit(" / ", 1)[-1] == PLAN_JOB), None)
    if not isinstance(plan_job, dict):
        return False
    return any(step.get("name") == PLAN_CHANGES_STEP and step.get("conclusion") == "skipped"
               for step in (plan_job.get("steps") or []) if isinstance(step, dict))


def code_failures_of_run(run: dict, jobs: list[dict], head: str) -> list[str]:
    """Every non-title, non-reference failure in a completed code run of ``head``."""
    run_id = run.get("id")
    failures: list[str] = []
    title_or_ref_failed = False
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_name = str(job.get("name", ""))
        short_name = job_name.rsplit(" / ", 1)[-1]
        if short_name == VERDICT_JOB:
            continue
        conclusion = job.get("conclusion")
        if short_name in (PLAN_JOB, HYGIENE_JOB):
            if conclusion in ("success", "skipped"):
                continue
            allowed = REFERENCES_STEP if short_name == PLAN_JOB else TITLE_STEP
            bad_steps = [step.get("name") for step in (job.get("steps") or [])
                         if isinstance(step, dict) and step.get("conclusion") in (*FAILING_RESULTS, "action_required")]
            if bad_steps and all(step == allowed for step in bad_steps):
                title_or_ref_failed = True
            else:
                failures.append(f"`{job_name}` ended {conclusion} in run {run_id}")
        elif conclusion in (*FAILING_RESULTS, "action_required", "startup_failure"):
            failures.append(f"`{job_name}` ended {conclusion} in run {run_id}")
    if not failures and not title_or_ref_failed and run.get("conclusion") != "success":
        failures.append(f"code run {run_id} for `{head}` ended {run.get('conclusion')}")
    return failures


def head_code_results(head: str | None, workflow_ref: str, run_id: str) -> dict:
    """Look up the code results of ``head`` from the pull request's latest non-cancelled code run."""
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    api_url = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    token = os.environ.get("GITHUB_TOKEN") or None
    if not head or not repository:
        return {"run_id": None, "passed": False, "failures": ["the code results of the head could not be looked up: missing repository or head"]}
    workflow_file = workflow_ref.split("@", 1)[0].rsplit("/", 1)[-1] if workflow_ref else "ci.yml"
    try:
        runs_doc = github_get(api_url, repository, token,
                              f"actions/workflows/{urllib.parse.quote(workflow_file)}/runs?head_sha={urllib.parse.quote(head)}&event=pull_request&per_page=100")
        runs = runs_doc.get("workflow_runs") if isinstance(runs_doc, dict) else None
        if not isinstance(runs, list):
            return {"run_id": None, "passed": False, "failures": [f"the code results of `{head}` could not be read"]}
        for run in runs:
            if not isinstance(run, dict) or str(run.get("id")) == str(run_id) or run.get("conclusion") == "cancelled":
                continue
            jobs_doc = github_get(api_url, repository, token, f"actions/runs/{run.get('id')}/jobs?per_page=100")
            jobs = jobs_doc.get("jobs") if isinstance(jobs_doc, dict) else None
            if not isinstance(jobs, list):
                return {"run_id": run.get("id"), "passed": False, "failures": [f"the jobs of run {run.get('id')} could not be read"]}
            if is_edited_run(jobs):
                continue
            if run.get("status") != "completed":
                return {"run_id": run.get("id"), "passed": False, "failures": [f"code run {run.get('id')} for `{head}` is still {run.get('status')}"]}
            failures = code_failures_of_run(run, jobs, head)
            return {"run_id": run.get("id"), "passed": not failures, "failures": failures}
    except (urllib.error.URLError, OSError, ValueError) as error:
        return {"run_id": None, "passed": False, "failures": [f"the code results of `{head}` could not be looked up: {error}"]}
    return {"run_id": None, "passed": False, "failures": [f"no completed code run found for `{head}`"]}


def make_plan(root: Path, versions: dict[int, SchemaVersion], base: str | None, head: str | None, compare: str,
              workflow: str = PIPELINE, lookup: str | None = None, unit_decisions: str | None = None,
              event: str = "", event_action: str = "", workflow_ref: str = "", run_id: str = "0") -> dict:
    path = root / CONFIGURATION
    if not path.is_file():
        raise PlanError(f"{CONFIGURATION} does not exist. The smallest valid file is the line `version: {max(versions)}`.")
    configuration, version = checked_configuration(path.read_text(encoding="utf-8"), versions)
    start, changed, identical_reason, tested_base = resolve_change(root, base, head, compare, lookup)
    before = base_configuration(root, start) if changed is not None else None
    tested_before = base_configuration(root, tested_base) if changed is not None and tested_base is not None else None
    parts, base_parts = stated_parts(configuration), stated_parts(before)
    tested_base_parts = stated_parts(tested_before) if tested_base is not None else None
    decisions = {}
    head_results: dict | None = None
    if event == "pull_request" and event_action == "edited" and workflow == PIPELINE:
        head_results = head_code_results(head, workflow_ref, run_id)
        for name in version.parts_of(workflow):
            if name == HYGIENE_JOB:
                entry: dict = {"run": True, "reason": "title and description checks", "config": parts.get(name, {})}
            elif name not in parts:
                entry = {"run": False, "reason": "switched off", "config": {}}
            elif head_results["passed"]:
                entry = {"run": False, "reason": f"took over from run {head_results['run_id']}", "config": parts.get(name, {}), "reused": True}
            else:
                entry = {"run": False, "reason": "edited pull request", "config": parts.get(name, {})}
            if name in parts and "setups" in version.schema["properties"]["parts"]["properties"][name].get("properties", {}):
                entry["setups"] = [version.setups[setup] for setup in parts[name].get("setups", [])]
            decisions[name] = entry
    else:
        for name in version.parts_of(workflow):
            run, reason, reused = decide(name, version, parts, base_parts, changed, identical_reason, tested_base_parts)
            entry = {"run": run, "reason": reason, "config": parts.get(name, {})}
            if reused:
                entry["reused"] = True
            if name in parts and "setups" in version.schema["properties"]["parts"]["properties"][name].get("properties", {}):
                entry["setups"] = [version.setups[setup] for setup in parts[name].get("setups", [])]
            decisions[name] = entry
        combine_python_projects(decisions)
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
        "compared": changed is not None,
        "switched-off": sorted(name for name in base_parts if name not in parts and name not in version.always),
        "local-workflows-added": [workflow for workflow in registered_workflows(configuration) if workflow not in known],
    }
    if head_results is None and (selected_units := read_unit_decisions(unit_decisions)) is not None:
        result["units"] = selected_units["units"]
    if head_results is not None:
        result["head-results"] = head_results
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
    head_results = plan.get("head-results")
    if isinstance(head_results, dict):
        if head_results.get("passed"):
            lines += ["", f"Code results of the head: passed in run {head_results.get('run_id')}."]
        else:
            lines += ["", "Code results of the head: " + "; ".join(head_results.get("failures") or ["not passed"]) + "."]
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
                         options.event, options.event_action, options.workflow_ref, options.run_id)
    except (PlanError, SchemaError) as error:
        lines = str(error).splitlines()
        print(f"The plan failed: {CONFIGURATION} cannot be used.")
        print("\n".join(lines))
        append(options.summary, "\n".join(["## Pipeline plan", "", f"**Failed.** `{CONFIGURATION}` cannot be used:", "", *[f"- {line}" for line in lines]]) + "\n")
        return 1
    for name, part in plan["parts"].items():
        print(f"{name}: {'runs' if part['run'] else 'does not run'} ({part['reason']})")
    append(options.github_output, f"plan={json.dumps(plan, separators=(',', ':'))}\n")
    append(options.summary, plan_markdown(plan))
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


def evaluate_verdict(needs: dict) -> tuple[list[str], dict | None, list[CheckOutcome]]:
    """Evaluate the needs context against the plan; returns (failure reasons, validated plan, check outcomes)."""
    reasons: list[str] = []
    if PLAN_JOB not in needs:
        reasons.append(f"the needs context has no `{PLAN_JOB}` job")
    elif needs[PLAN_JOB].get("result") != "success":
        result = needs[PLAN_JOB].get("result")
        reasons.append(f"`{PLAN_JOB}` never started" if not isinstance(result, str) or not result else f"`{PLAN_JOB}` ended {result}")
    else:
        plan, error = parse_job_plan(needs)
        if plan is None:
            reasons.append(error or f"the `{PLAN_JOB}` job gave no plan output")
        else:
            checks: list[CheckOutcome] = []
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
                    checks.append(CheckOutcome(name, outcome, "not run", reason))
            head_results = plan.get("head-results")
            if isinstance(head_results, dict) and head_results.get("passed") is False:
                reasons.extend(str(item) for item in (head_results.get("failures") or ["the code checks of the head did not pass"]))
            return reasons, plan, checks
    reasons += [f"`{name}` ended {job.get('result')}" for name, job in needs.items()
                if name != PLAN_JOB and job.get("result") in FAILING_RESULTS]
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
    planning.add_argument("--unit-decisions", help="the per-unit decisions JSON file written by suite_selection.py select")
    planning.add_argument("--workflow", default="pipeline", help="the name of the running workflow, for the concurrency groups")
    planning.add_argument("--workflow-ref", default="", help="the workflow_ref of the running workflow, for looking up head runs")
    planning.add_argument("--event", default="", help="the name of the event of the run")
    planning.add_argument("--event-action", default="", help="the action of the event, such as edited on pull_request")
    planning.add_argument("--pull-request", default="", help="the number of the pull request of the run, empty on another event")
    planning.add_argument("--run-id", default="0", help="the id of the run")
    planning.add_argument("--github-output", help="the file that receives the output plan")
    planning.add_argument("--summary", help="the file that receives the Markdown summary")
    deciding = commands.add_parser("verdict", help="pass or fail on the results of the plan and the parts")
    deciding.add_argument("--needs", required=True, help="a file with toJSON(needs) of the verdict job")
    deciding.add_argument("--summary", help="the file that receives the Markdown summary")
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
    return {"plan": run_plan, "verdict": run_verdict, "schema": run_schema, "setting": run_setting}[options.command](options)


if __name__ == "__main__":
    sys.exit(main())
