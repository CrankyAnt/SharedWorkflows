#!/usr/bin/env python3
"""Plan the pipeline from a repository's configuration file, and give its verdict.

    python3 plan.py plan    [--root DIR] [--schemas DIR] [--base SHA --head SHA [--compare merge-base|direct]]
                            [--workflow NAME] [--event NAME] [--pull-request NUMBER] [--run-id ID] [--github-output FILE] [--summary FILE]
    python3 plan.py verdict --needs FILE [--summary FILE]
    python3 plan.py schema  [--schemas DIR] [--check]

``plan`` reads ``.github/ci-config.yml``, checks it against the schema of its version and decides per part whether it
runs. The schema folder holds one folder per schema version, ``v<N>``, with the ``base.json`` of the file and one
fragment per part, ``parts/<part>.json``: its ``description`` and either the schema of its ``configuration`` or
``"always": true`` for a part that runs in every pipeline run, which the configuration file cannot state. The highest
version is the current one; the plan reads it and the one before.

A part that always runs, runs. Any other part runs when the configuration file states it under ``parts`` and, where its
configuration names ``paths``, a changed path matches them, its own configuration differs from the base, or the changed
files are unknown. The plan is one JSON document, the output ``plan``: per part ``run``, ``reason`` and ``config``, the
parts switched off and the local workflows registered compared with the base, and whether that comparison was made.

Before it reads the configuration file, ``plan`` writes the output ``concurrency``: per part the ``group`` and
``cancel-in-progress`` of its job, so that a failed plan still gives them. The group is the workflow's name, the part
and the pull request's number, or the run's id on any other event; only a pull request's run is cancelled by a newer one.

``verdict`` reads the ``needs`` context of the job that needs ``plan`` and every part, as ``toJSON(needs)`` renders it.
It fails when ``plan`` did not succeed or any other job ended failure or cancelled; a skipped job is a part that did not
run. ``schema`` writes, or with ``--check`` compares, the assembled schema ``ci-config.schema.json`` of each version.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PART_FOLDER = Path(__file__).resolve().parent
sys.path.insert(0, str(PART_FOLDER.parent / "repository-hygiene"))
from dependabot_coverage import YamlError, parse_yaml  # noqa: E402

CONFIGURATION = ".github/ci-config.yml"
SCHEMAS = PART_FOLDER / "schema"
ASSEMBLED = "ci-config.schema.json"
PLAN_JOB = "plan"
FAILING_RESULTS = ("failure", "cancelled")
ALL_ZERO = re.compile(r"^0+$")
VERSION_FOLDER = re.compile(r"^v([1-9][0-9]*)$")
# The schema keywords that say nothing about a value, and those the check reads; any other keyword fails the check.
ANNOTATIONS = {"$schema", "$id", "$defs", "title", "description", "examples", "default"}
KEYWORDS = {"type", "const", "enum", "pattern", "minLength", "minimum", "minItems", "uniqueItems", "items", "properties", "required",
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

    @property
    def parts(self) -> list[str]:
        """Every part of the pipeline by name: those the configuration file switches on and those that always run."""
        return sorted({*self.schema["properties"]["parts"]["properties"], *self.always})


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


def assemble(folder: Path, version: int) -> SchemaVersion:
    """The schema of one version: its base with the configuration of each part's fragment under ``parts``."""
    schema = read_json(folder / "base.json")
    check_keywords(schema, str(folder / "base.json"))
    try:
        parts = schema["properties"]["parts"]["properties"]
    except (KeyError, TypeError) as error:
        raise SchemaError(f"{folder / 'base.json'}: properties.parts.properties is missing") from error
    always = set()
    for path in sorted((folder / "parts").glob("*.json")):
        fragment = read_json(path)
        unknown = sorted(set(fragment) - {"description", "always", "configuration"})
        if unknown or not isinstance(fragment.get("description"), str) or not isinstance(fragment.get("always", False), bool):
            raise SchemaError(f"{path}: a fragment holds a description and either a configuration or always")
        if fragment.get("always"):
            if "configuration" in fragment:
                raise SchemaError(f"{path}: a part that always runs has no configuration")
            always.add(path.stem)
            continue
        configuration = fragment.get("configuration")
        check_keywords(configuration, str(path))
        if configuration.get("type") != "object" or configuration.get("additionalProperties") is not False:
            raise SchemaError(f"{path}: the configuration of a part is an object without additional properties")
        parts[path.stem] = {"description": fragment["description"], **configuration}
    if always:
        schema["properties"]["parts"]["description"] += f" Not stated here, because it always runs and has no settings: {and_list(sorted(always))}."
    return SchemaVersion(version, schema, frozenset(always))


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


# --- the plan -------------------------------------------------------------------------------------------------------


def decide(name: str, version: SchemaVersion, parts: dict, base_parts: dict, changed: list[str] | None) -> tuple[bool, str]:
    """Whether a part runs, with the reason."""
    if name in version.always:
        return True, "always runs"
    if name not in parts:
        return False, "switched off"
    patterns = parts[name].get("paths")
    if patterns is None:
        return True, "switched on"
    if changed is None:
        return True, "the changed files are unknown"
    if base_parts.get(name) != parts[name]:
        return True, "its configuration changed"
    matched = first_match(patterns, changed)
    return (True, f"{matched} changed") if matched else (False, "no changed path matches its paths")


def make_plan(root: Path, versions: dict[int, SchemaVersion], base: str | None, head: str | None, compare: str) -> dict:
    path = root / CONFIGURATION
    if not path.is_file():
        raise PlanError(f"{CONFIGURATION} does not exist. The smallest valid file is the line `version: {max(versions)}`.")
    configuration, version = checked_configuration(path.read_text(encoding="utf-8"), versions)
    start = comparison_start(root, base, head, compare)
    changed = changed_files(root, start, head) if start and head else None
    before = base_configuration(root, start) if changed is not None else None
    parts, base_parts = stated_parts(configuration), stated_parts(before)
    decisions = {}
    for name in version.parts:
        run, reason = decide(name, version, parts, base_parts, changed)
        decisions[name] = {"run": run, "reason": reason, "config": parts.get(name, {})}
    known = registered_workflows(before)
    return {
        "version": version.version,
        "parts": decisions,
        "compared": changed is not None,
        "switched-off": sorted(name for name in base_parts if name not in parts and name not in version.always),
        "local-workflows-added": [workflow for workflow in registered_workflows(configuration) if workflow not in known],
    }


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
    lines += [f"| `{name}` | {'yes' if part['run'] else 'no'} | {part['reason']} |" for name, part in plan["parts"].items()]
    return "\n".join([*lines, "", *comparison_lines(plan)]) + "\n"


def append(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(text)


def concurrency(parts: list[str], workflow: str, event: str, pull_request: str, run_id: str) -> dict:
    """The concurrency group and cancel flag of each part's job."""
    on_pull_request = event == "pull_request" and bool(pull_request)
    scope = f"pr-{pull_request}" if on_pull_request else f"run-{run_id}"
    return {name: {"group": f"{workflow}-{name}-{scope}", "cancel-in-progress": on_pull_request} for name in parts}


def run_plan(options: argparse.Namespace) -> int:
    try:
        versions = schema_versions(Path(options.schemas))
        groups = concurrency(versions[max(versions)].parts, options.workflow, options.event, options.pull_request, options.run_id)
        append(options.github_output, f"concurrency={json.dumps(groups, separators=(',', ':'))}\n")
        plan = make_plan(Path(options.root), versions, options.base, options.head, options.compare)
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


def job_plan(needs: dict) -> dict | None:
    """The plan in the outputs of the ``plan`` job, when that job gave one."""
    outputs = needs.get(PLAN_JOB, {}).get("outputs") if isinstance(needs.get(PLAN_JOB), dict) else None
    raw = outputs.get("plan") if isinstance(outputs, dict) else None
    try:
        plan = json.loads(raw) if isinstance(raw, str) and raw else None
    except ValueError:
        return None
    return plan if isinstance(plan, dict) and isinstance(plan.get("parts"), dict) else None


def verdict_reasons(needs: dict) -> list[str]:
    """Why the verdict fails: ``plan`` did not succeed, or another job ended failure or cancelled."""
    reasons = []
    if PLAN_JOB not in needs:
        reasons.append(f"the needs context has no `{PLAN_JOB}` job")
    elif needs[PLAN_JOB].get("result") != "success":
        reasons.append(f"`{PLAN_JOB}` ended {needs[PLAN_JOB].get('result')}")
    reasons += [f"`{name}` ended {job.get('result')}" for name, job in needs.items() if name != PLAN_JOB and job.get("result") in FAILING_RESULTS]
    return reasons


def verdict_markdown(needs: dict, reasons: list[str]) -> str:
    lines = ["## Verdict", ""]
    lines.append("**Failed.** " + "; ".join(reasons) + "." if reasons else "**Passed.** The plan succeeded and no started part ended failure or cancelled.")
    plan = job_plan(needs)
    if plan is None:
        lines += ["", f"The `{PLAN_JOB}` job gave no plan: the parts switched off and the local workflows registered by this change are unknown."]
    else:
        lines += ["", *comparison_lines(plan)]
        lines += ["", "| Part | Planned | Reason |", "| --- | --- | --- |"]
        lines += [f"| `{name}` | {'runs' if part.get('run') else 'does not run'} | {part.get('reason', '')} |" for name, part in plan["parts"].items()]
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
    reasons = verdict_reasons(needs)
    print("Verdict: " + ("failed: " + "; ".join(reasons) if reasons else "passed"))
    for name, job in needs.items():
        print(f"job {name}: {job.get('result')}")
    append(options.summary, verdict_markdown(needs, reasons))
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


# --- the command line -----------------------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    main_parser = argparse.ArgumentParser(description="Plan the pipeline from the configuration file and give its verdict.")
    commands = main_parser.add_subparsers(dest="command", required=True)
    planning = commands.add_parser("plan", help="check the configuration file and decide per part whether it runs")
    planning.add_argument("--root", default=".", help="the repository's checkout")
    planning.add_argument("--schemas", default=str(SCHEMAS), help="the schema folder")
    planning.add_argument("--base", help="the commit the head is compared with")
    planning.add_argument("--head", help="the commit under test")
    planning.add_argument("--compare", choices=("merge-base", "direct"), default="direct", help="compare the head with its merge base with the base, or with the base itself")
    planning.add_argument("--workflow", default="pipeline", help="the name of the running workflow, for the concurrency groups")
    planning.add_argument("--event", default="", help="the name of the event of the run")
    planning.add_argument("--pull-request", default="", help="the number of the pull request of the run, empty on another event")
    planning.add_argument("--run-id", default="0", help="the id of the run")
    planning.add_argument("--github-output", help="the file that receives the outputs concurrency and plan")
    planning.add_argument("--summary", help="the file that receives the Markdown summary")
    deciding = commands.add_parser("verdict", help="pass or fail on the results of the plan and the parts")
    deciding.add_argument("--needs", required=True, help="a file with toJSON(needs) of the verdict job")
    deciding.add_argument("--summary", help="the file that receives the Markdown summary")
    assembling = commands.add_parser("schema", help="write or check the assembled schema of each version")
    assembling.add_argument("--schemas", default=str(SCHEMAS), help="the schema folder")
    assembling.add_argument("--check", action="store_true", help="fail when an assembled schema differs instead of writing it")
    return main_parser


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    return {"plan": run_plan, "verdict": run_verdict, "schema": run_schema}[options.command](options)


if __name__ == "__main__":
    sys.exit(main())
