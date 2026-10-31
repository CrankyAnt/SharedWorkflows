"""The CI definition of a calling workflow: the tracked files its gated checks read.

The CI definition is the calling workflow; every workflow and local action it uses, transitively;
every tracked file or directory whose path appears in a ``run``, ``with`` or ``working-directory``
value of their steps, with ``./`` prefixes and backslash separators normalised; and the suite
list. ``ci_definition`` lists it in that order, the calling workflow first, so that the first
item a changed file matches is the most direct reason the selection can give.

A ``run`` path that starts with ``..`` resolves against the step's working directory: the
step's ``working-directory``, otherwise ``defaults.run.working-directory`` of the job, otherwise
that of the workflow, otherwise the repository root. ``..`` segments in any reference resolve
after the action path is substituted, and a reference above the repository root names no file.

Workflow and action files are read with the YAML subset GitHub workflows use: mappings,
sequences, plain and quoted scalars, block scalars and flow collections kept as text. A file
this reader cannot read fails selection clearly. The
reader lives here because the suite selection depends on nothing outside its own directory: a shared
workflow loads the directory on its own through ``$/``, and no YAML library is at hand there.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

WORKFLOW_SUFFIXES = (".yml", ".yaml")
ACTION_FILES = ("action.yml", "action.yaml")
LOCAL_PREFIXES = ("./", "$/")
WORKSPACE_PREFIXES = ("$GITHUB_WORKSPACE/", "${GITHUB_WORKSPACE}/", "${{ github.workspace }}/", "${{github.workspace}}/")
ACTION_PATH = "$GITHUB_ACTION_PATH"
ACTION_PATH_EXPRESSIONS = ("${{ github.action_path }}", "${{github.action_path}}")


def _action_path_aliases(node: object) -> set[str]:
    """The names of environment variables an action file sets to its own path (``RESOURCE_PATH: $GITHUB_ACTION_PATH``)."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "env" and isinstance(value, dict):
                found.update(name for name, text in value.items() if isinstance(text, str) and text.strip() == ACTION_PATH)
            else:
                found |= _action_path_aliases(value)
    elif isinstance(node, list):
        for item in node:
            found |= _action_path_aliases(item)
    return found
TOKEN = re.compile(r"[^\s\"'=,;|()<>&`]+")
KEY = re.compile(r"""^("[^"]*"|'[^']*'|[^\s"'#\-\[\]{}][^:]*?)\s*:(?:\s+(.*))?$""")


class WorkflowReadError(Exception):
    """A workflow or action file could not be read as the YAML subset workflows use."""


@dataclass(frozen=True)
class Item:
    """One part of the CI definition: a file path or a ``<directory>/**`` tree, with its role and, when only steps
    gated on particular suites name it, those suites (empty: no product suite is attributed to it)."""

    pattern: str
    role: str
    suites: frozenset[str] = frozenset()
    entries: tuple[tuple[str, frozenset[str]], ...] = ()  # restricted entries per suite; absent means every entry

    def matches(self, path: str) -> bool:
        if self.pattern.endswith("/**"):
            return path.startswith(self.pattern[:-2])
        return path == self.pattern

    def entries_for(self, suite: str) -> frozenset[str] | None:
        return dict(self.entries).get(suite)


@dataclass(frozen=True)
class Gate:
    suites: frozenset[str] = frozenset()
    entries: tuple[tuple[str, frozenset[str]], ...] = ()

    def merge(self, other: "Gate") -> "Gate":
        suites = self.suites | other.suites
        merged: dict[str, frozenset[str]] = {suite: entries for suite, entries in self.entries}
        for suite, entries in other.entries:
            merged[suite] = (merged[suite] | entries) if suite in merged else entries
        for suite in suites:
            if suite in self.suites and suite not in dict(self.entries) or suite in other.suites and suite not in dict(other.entries):
                merged.pop(suite, None)
        return Gate(suites, tuple(sorted(merged.items())))


# --- the YAML subset reader ------------------------------------------------------------------------------


def _strip_comment(line: str) -> str:
    quote: str | None = None
    for index, character in enumerate(line):
        if quote:
            if character == quote:
                quote = None
        elif character in "'\"":
            quote = character
        elif character == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index].rstrip()
    return line.rstrip()


def _unquote(text: str) -> str:
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "'\"":
        return text[1:-1]
    return text


class _Reader:
    def __init__(self, text: str, source: str) -> None:
        self.lines = text.splitlines()
        self.source = source

    def indent(self, index: int) -> int:
        """The indentation of a line; a line whose start a sequence item consumed is read through ``entry``."""
        line = self.lines[index]
        return len(line) - len(line.lstrip(" "))

    def blank(self, index: int) -> bool:
        stripped = self.lines[index].strip()
        return not stripped or stripped.startswith("#")

    def skip(self, index: int) -> int:
        while index < len(self.lines) and self.blank(index):
            index += 1
        return index

    def error(self, index: int, message: str) -> WorkflowReadError:
        return WorkflowReadError(f"{self.source}, line {index + 1}: {message}")

    def document(self) -> object:
        start = self.skip(0)
        if start < len(self.lines) and self.lines[start].strip() == "---":
            start += 1
        value, index = self.node(start, 0)
        index = self.skip(index)
        if index < len(self.lines):
            raise self.error(index, "unexpected content")
        return value

    def node(self, index: int, minimum: int) -> tuple[object, int]:
        index = self.skip(index)
        if index >= len(self.lines) or self.indent(index) < minimum:
            return None, index
        stripped = _strip_comment(self.lines[index].strip())
        if stripped == "-" or stripped.startswith("- "):
            return self.sequence(index, self.indent(index))
        return self.mapping(index, self.indent(index))

    def entry(self, index: int, first: str | None) -> str:
        """The content of a line without its comment; ``first`` replaces the text of a line whose start an
        enclosing sequence item already consumed (``- key: value`` or ``- - item``)."""
        return first if first is not None else _strip_comment(self.lines[index].strip())

    def mapping(self, index: int, indent: int, first: str | None = None) -> tuple[dict, int]:
        result: dict = {}
        while True:
            if first is None:
                index = self.skip(index)
                if index >= len(self.lines) or self.indent(index) != indent:
                    if index < len(self.lines) and self.indent(index) > indent:
                        raise self.error(index, "unexpected indentation")
                    return result, index
            stripped, first = self.entry(index, first), None
            if stripped == "-" or stripped.startswith("- "):
                return result, index
            match = KEY.match(stripped)
            if match is None:
                raise self.error(index, "expected a key")
            key, rest = _unquote(match.group(1)), (match.group(2) or "").strip()
            result[key], index = self.value(rest, index, indent)

    def sequence(self, index: int, indent: int, first: str | None = None) -> tuple[list, int]:
        items: list = []
        while True:
            if first is None:
                index = self.skip(index)
                if index >= len(self.lines) or self.indent(index) != indent:
                    if index < len(self.lines) and self.indent(index) > indent:
                        raise self.error(index, "unexpected indentation")
                    return items, index
            stripped, first = self.entry(index, first), None
            if not (stripped == "-" or stripped.startswith("- ")):
                return items, index
            rest = stripped[1:].strip()
            column = indent + (len(stripped) - len(stripped[1:].lstrip()))
            if rest == "-" or rest.startswith("- "):
                # A nested sequence starts on the item line; its items continue at the column of the inner dash.
                value, index = self.sequence(index, column, rest)
            elif rest and rest[0] not in "[{\"'|>" and KEY.match(rest):
                # An inline mapping starts on the item line; its keys continue at the column of the first key.
                value, index = self.mapping(index, column, rest)
            else:
                value, index = self.value(rest, index, indent)
            items.append(value)

    def value(self, rest: str, index: int, indent: int) -> tuple[object, int]:
        """The value that starts at ``rest`` on line ``index``: a nested node, a block scalar or a scalar."""
        if rest == "":
            return self.node(index + 1, indent + 1)
        if rest[0] in "|>":
            return self.block(index + 1, indent)
        index += 1
        text = rest
        while index < len(self.lines) and not self.blank(index) and self.indent(index) > indent:
            text += " " + _strip_comment(self.lines[index].strip())
            index += 1
        return _unquote(text), index

    def block(self, index: int, indent: int) -> tuple[str, int]:
        collected: list[str] = []
        while index < len(self.lines) and (not self.lines[index].strip() or self.indent(index) > indent):
            collected.append(self.lines[index])
            index += 1
        while collected and not collected[-1].strip():
            collected.pop()
        common = min((len(line) - len(line.lstrip(" ")) for line in collected if line.strip()), default=0)
        return "\n".join(line[common:] for line in collected), index


def read_yaml(text: str, source: str = "the workflow") -> object:
    return _Reader(text, source).document()


# --- the definition ---------------------------------------------------------------------------------------


def without_comments(script: str) -> str:
    """A ``run`` script without its comments, so that no word of a comment counts as a path the step names.

    In the shells GitHub Actions runs, a ``#`` at the start of a line or after whitespace begins a comment that ends
    with the line, unless it stands inside quotes, which may span lines and hold a path the step names; a backslash
    escapes the next character outside single quotes. PowerShell's ``<# ... #>`` block comment ends at its marker.
    """
    kept: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(script):
        character = script[index]
        if character == "\\" and quote != "'" and index + 1 < len(script):
            kept.append(script[index:index + 2])
            index += 2
            continue
        if quote:
            if character == quote:
                quote = None
            kept.append(character)
        elif character in "'\"":
            quote = character
            kept.append(character)
        elif script.startswith("<#", index):
            end = script.find("#>", index + 2)
            index = len(script) if end < 0 else end + 2
            kept.append(" ")
            continue
        elif character == "#" and (index == 0 or script[index - 1] in " \t\n"):
            end = script.find("\n", index)
            index = len(script) if end < 0 else end
            continue
        else:
            kept.append(character)
        index += 1
    return "".join(kept)


OUTPUT_GATE = re.compile(r"^(?:needs|steps)\.\w+\.outputs\.([a-z0-9-]+) == 'run'$")
MODULE_GATE = re.compile(r"^contains\((?:needs|steps)\.\w+\.outputs\.([a-z0-9-]+)-modules, '([^']+)'\)$")
ENTRY_GATE = re.compile(r"^contains\((?:needs|steps)\.\w+\.outputs\.([a-z0-9-]+)-entries, '([^']+)'\)$")
JSON_GATE = re.compile(r"^fromJSON\((?:inputs\.[a-z0-9-]+|needs\.\w+\.outputs\.[a-z0-9-]+|steps\.\w+\.outputs\.[a-z0-9-]+)\)\.([a-z0-9-]+) == 'run'$")
JSON_ENTRY_GATE = re.compile(r"^contains\(fromJSON\((?:inputs\.entries|needs\.\w+\.outputs\.entries|steps\.\w+\.outputs\.entries)\)\.([a-z0-9-]+), '([^']+)'\)$")
LANGUAGE_STEPS_OUTPUT = r"fromJSON\(\s*(?:inputs|(?:needs|steps)\.[A-Za-z_][A-Za-z0-9_-]*\.outputs)\.language-steps\s*\)\s*\.\s*[a-z0-9-]+"
LANGUAGE_STEP_TERM = re.compile(rf"{LANGUAGE_STEPS_OUTPUT}\s*==\s*'run'|'run'\s*==\s*{LANGUAGE_STEPS_OUTPUT}")
SELECTION_OUTPUT = r"(?:needs|steps)\.[A-Za-z_][A-Za-z0-9_-]*\.outputs\.[a-z0-9-]+"
SELECTION_JSON = rf"(?:inputs\.[a-z0-9-]+|{SELECTION_OUTPUT})"
SELECTION_TERM = re.compile(
    rf"{SELECTION_OUTPUT}\s*==\s*'run'"
    rf"|'run'\s*==\s*{SELECTION_OUTPUT}"
    rf"|contains\(\s*{SELECTION_OUTPUT}-(?:modules|entries)\s*,"
    rf"|contains\(\s*fromJSON\(\s*{SELECTION_JSON}\s*\)\s*\.\s*[a-z0-9-]+\s*,"
    rf"|fromJSON\(\s*{SELECTION_JSON}\s*\)\s*\.\s*[a-z0-9-]+\s*==\s*'run'"
    rf"|'run'\s*==\s*fromJSON\(\s*{SELECTION_JSON}\s*\)\s*\.\s*[a-z0-9-]+"
)


def condition_gate(condition: object) -> Gate:
    """The suites a step or job condition gates on: a job condition ``needs.<job>.outputs.<suite> == 'run'``, or one
    or more step conditions ``steps.<id>.outputs.<suite> == 'run'`` joined with ``||``. An empty condition gates on
    nothing (an empty set). Other control conditions are ungated, but an unsupported condition that contains a
    selection term fails clearly instead of omitting its suites. A language step's output names no suite: a term on it
    is a control term."""
    text = str(condition or "").strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    if not text:
        return Gate()
    if text == "always()":
        return Gate()
    text = LANGUAGE_STEP_TERM.sub("language-step", text)
    gate = Gate()
    for term in text.split(" || "):
        if term.strip() == "language-step":
            continue
        output = OUTPUT_GATE.match(term)
        module = MODULE_GATE.match(term)
        entry_gate = ENTRY_GATE.match(term)
        json_gate = JSON_GATE.match(term)
        json_entry = JSON_ENTRY_GATE.match(term)
        if output:
            gate = gate.merge(Gate(frozenset({output.group(1)})))
        elif module:
            entry = module.group(2).replace(".", "/") + ".py"
            gate = gate.merge(Gate(frozenset({module.group(1)}), ((module.group(1), frozenset({entry})),)))
        elif entry := entry_gate or json_entry:
            gate = gate.merge(Gate(frozenset({entry.group(1)}), ((entry.group(1), frozenset({entry.group(2)})),)))
        elif json_gate:
            gate = gate.merge(Gate(frozenset({json_gate.group(1)})))
        else:
            if SELECTION_TERM.search(text):
                raise WorkflowReadError(f"unsupported suite-selection condition: {text}")
            return Gate()
    return gate


def _working_directory(value: str) -> str | None:
    """The repository path a ``working-directory`` value names: ``""`` for the repository root, ``None`` for a value
    that names no repository path, such as ``${{ runner.temp }}``."""
    text = value.strip().replace("\\", "/").rstrip("/")
    if text in ("", ".") or text in (prefix.rstrip("/") for prefix in WORKSPACE_PREFIXES):
        return ""
    return repository_path(text)


def _default_directory(node: dict, directory: str | None) -> str | None:
    """The working directory that ``defaults.run`` of a workflow or job sets, otherwise the inherited ``directory``."""
    defaults = node.get("defaults")
    run = defaults.get("run") if isinstance(defaults, dict) else None
    value = run.get("working-directory") if isinstance(run, dict) else None
    return _working_directory(value) if isinstance(value, str) else directory


def _references(node: object, gate: Gate = Gate(), directory: str | None = "") -> list[tuple[str, str, Gate, str | None]]:
    """Every ``uses`` value and every ``run``, ``working-directory`` and ``with`` value, in document order, each with
    the suites the step is gated on (empty for an ungated step) and the step's working directory; a ``run`` script
    counts without its comments. A step of a job gated on one suite is gated on it; a step with a condition on the
    selection's outputs is gated on those suites. Other control conditions remain ungated. An unsupported condition
    that contains a selection term fails the definition instead of silently omitting its suites. The working directory
    is the step's ``working-directory``, otherwise ``defaults.run.working-directory`` of the job, otherwise that of the
    workflow, otherwise the repository root (``""``); ``None`` when it names no repository path."""
    found: list[tuple[str, str, Gate, str | None]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "jobs" and isinstance(value, dict):
                workflow_directory = _default_directory(node, directory)
                for job in value.values():
                    job_gate = condition_gate(job.get("if")) if isinstance(job, dict) else Gate()
                    job_directory = _default_directory(job, workflow_directory) if isinstance(job, dict) else workflow_directory
                    found.extend(_references(job, job_gate, job_directory))
            elif key == "steps" and isinstance(value, list):
                for step in value:
                    step_gate = condition_gate(step.get("if")) if isinstance(step, dict) else Gate()
                    # A downstream job may be enabled by the union of many suite outputs. A step with its own
                    # selection condition serves that narrower gate; only an ungated step inherits the job gate.
                    effective_gate = step_gate if isinstance(step, dict) and str(step.get("if") or "").strip() else gate
                    own = step.get("working-directory") if isinstance(step, dict) else None
                    found.extend(_references(step, effective_gate, _working_directory(own) if isinstance(own, str) else directory))
            elif key == "uses" and isinstance(value, str):
                found.append(("uses", value, gate, directory))
            elif key == "run" and isinstance(value, str):
                found.append(("run", without_comments(value), gate, directory))
            elif key == "working-directory" and isinstance(value, str):
                found.append(("text", value, gate, directory))
            elif key == "with" and isinstance(value, dict):
                found.extend(("text", item, gate, directory) for item in value.values() if isinstance(item, str))
            elif key != "if":
                found.extend(_references(value, gate, directory))
    elif isinstance(node, list):
        for item in node:
            found.extend(_references(item, gate, directory))
    return found


def repository_path(token: str, directory: str | None = "") -> str | None:
    """The repository path a token names, or ``None``. A token that starts with ``..`` resolves against ``directory``
    (``""`` for the repository root, ``None`` for a working directory that names no repository path); ``..``
    segments resolve, and a path above the repository root names nothing."""
    token = token.replace("\\", "/")
    from_workspace = False
    for prefix in WORKSPACE_PREFIXES:
        if token.startswith(prefix):
            token = token[len(prefix):]
            from_workspace = True
    if "$" in token or "{" in token or "*" in token:
        return None
    while token.startswith("./"):
        token = token[2:]
    if not from_workspace and (token == ".." or token.startswith("../")):
        if directory is None:
            return None
        token = f"{directory}/{token}"
    token = token.strip("/")
    if ".." in token.split("/"):
        parts: list[str] = []
        for part in token.split("/"):
            if part == "..":
                if not parts:
                    return None
                parts.pop()
            elif part not in ("", "."):
                parts.append(part)
        token = "/".join(parts)
    return token or None


def _tokens(value: str, script: bool = False):
    """Path candidates, excluding unquoted shell assignment names while retaining their values."""
    quoted: set[int] = set()
    if script:
        quote: str | None = None
        escaped = False
        for index, character in enumerate(value):
            if quote or escaped:
                quoted.add(index)
            if escaped:
                escaped = False
            elif character == "\\" and quote != "'":
                escaped = True
            elif character == quote:
                quote = None
            elif quote is None and character in "'\"":
                quote = character
    for match in TOKEN.finditer(value):
        token = match.group()
        if (script and match.start() not in quoted and value[match.end():].startswith("=")
                and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*\+?", token)):
            continue
        yield token


def ci_definition(root: Path, workflow: str, suite_list: str, tracked: set[str]) -> list[Item]:
    """The CI definition of the calling workflow, the calling workflow first and the suite list last."""
    directories = {file[:index] for file in tracked for index in [position for position, character in enumerate(file) if character == "/"]}
    items: list[Item] = []
    seen: dict[str, int] = {}

    def add(pattern: str, role: str, gate: Gate = Gate()) -> None:
        """A pattern once: an ungated read does not broaden selection; a part that only gated steps name runs the
        suites of all those gates."""
        if pattern not in seen:
            seen[pattern] = len(items)
            items.append(Item(pattern, role, gate.suites, gate.entries))
            return
        existing = items[seen[pattern]]
        if existing.suites and not gate.suites:
            items[seen[pattern]] = Item(pattern, role, frozenset())
        elif existing.suites and gate.suites:
            combined = Gate(existing.suites, existing.entries).merge(gate)
            items[seen[pattern]] = Item(pattern, existing.role, combined.suites, combined.entries)

    workflow = repository_path(workflow) or workflow
    add(workflow, "the calling workflow")
    queue: list[tuple[str, Gate]] = [(workflow, Gate())]
    read_under: dict[str, Gate] = {workflow: Gate()}

    def enqueue(relative: str, gate: Gate) -> None:
        """Read a used workflow or action under the gate of this use; a later use under a wider gate (no gate, or
        more suites) reads it again, so that the files its steps name carry the widest gate of all its uses."""
        previous = read_under.get(relative)
        if previous is None:
            widened = gate
        elif not previous.suites or (gate.suites and previous.merge(gate) == previous):
            return
        else:
            widened = previous.merge(gate) if gate.suites else Gate()
        read_under[relative] = widened
        queue.append((relative, widened))

    position = 0
    while position < len(queue):
        path, inherited = queue[position]
        position += 1
        try:
            text = (root / path).read_text(encoding="utf-8")
        except OSError as error:
            raise WorkflowReadError(f"cannot read {path}: {error}") from error
        for spelling in ACTION_PATH_EXPRESSIONS:
            text = text.replace(spelling, ACTION_PATH)
        document = read_yaml(text, path)
        # A step of an action file names the action's own files through github.action_path, directly or through an
        # environment variable set to it; those references resolve against the action's folder.
        action_folder = path.rsplit("/", 1)[0] if path.rsplit("/", 1)[-1] in ACTION_FILES and "/" in path else None
        aliases = _action_path_aliases(document)
        action_prefixes = [f"{ACTION_PATH}/", "${GITHUB_ACTION_PATH}/"] + [f"${name}/" for name in aliases] + [f"${{{name}}}/" for name in aliases]
        for kind, value, step_gate, directory in _references(document, inherited):
            gate = step_gate if step_gate.suites else inherited
            if kind == "uses":
                if not value.startswith(LOCAL_PREFIXES):
                    continue
                relative = repository_path(value[2:])
                if relative is None:
                    continue
                if relative in tracked and relative.endswith(WORKFLOW_SUFFIXES):
                    add(relative, "a workflow it uses", gate)
                    enqueue(relative, gate)
                elif relative in directories:
                    # A local action contributes its action file and the files its steps name, not its whole folder.
                    action_file = next((f"{relative}/{name}" for name in ACTION_FILES if f"{relative}/{name}" in tracked), None)
                    if action_file is None:
                        add(f"{relative}/**", "a local action it uses, without an action file", gate)
                    else:
                        add(action_file, "a local action it uses", gate)
                        enqueue(action_file, gate)
                continue
            for token in _tokens(value, script=kind == "run"):
                if action_folder is not None:
                    for prefix in action_prefixes:
                        if token.startswith(prefix):
                            token = f"{action_folder}/{token[len(prefix):]}"
                            break
                relative = repository_path(token, directory if kind == "run" else "")
                if relative is None:
                    continue
                if relative in tracked:
                    add(relative, f"a file that a step of `{path}` names", gate)
                elif relative in directories:
                    add(f"{relative}/**", f"a directory that a step of `{path}` names", gate)
    add(repository_path(suite_list) or suite_list, "the suite list")
    return items


def definition_matches(items: list[Item], changed: list[str]) -> tuple[tuple[str, Item] | None, dict[str, list[tuple[str, Item]]]]:
    """The first changed file that matches a definition part with no product-suite attribution, with that part; and,
    per suite, the changed files that match parts gated on it, with those parts."""
    whole: tuple[str, Item] | None = None
    gated: dict[str, list[tuple[str, Item]]] = {}
    for item in items:
        for file in changed:
            if not item.matches(file):
                continue
            if not item.suites:
                if whole is None:
                    whole = (file, item)
            else:
                for suite in sorted(item.suites):
                    gated.setdefault(suite, []).append((file, item))
    return whole, gated


def changed_step_gate(before: str, after: str) -> Gate | None:
    """The suites and entries affected by changed steps, or ``None`` when comparison is unavailable.

    A changed, added, removed, or re-gated step belongs to both its old and new gates.  Ungated selection/control
    steps intentionally contribute no product suite: their own tests select them through ordinary path lists.
    """
    try:
        old, new = read_yaml(before), read_yaml(after)
    except WorkflowReadError:
        return None
    if not isinstance(old, dict) or not isinstance(new, dict):
        return None
    old_jobs, new_jobs = old.get("jobs"), new.get("jobs")
    if not isinstance(old_jobs, dict) or not isinstance(new_jobs, dict):
        return None
    affected = Gate()

    def gate_of(job: object, step: object | None = None) -> Gate | None:
        if not isinstance(job, dict) or (step is not None and not isinstance(step, dict)):
            return None
        job_gate = condition_gate(job.get("if"))
        if job_gate is None:
            return None
        if step is None:
            return job_gate
        step_gate = condition_gate(step.get("if"))
        if step_gate is None:
            return None
        return step_gate if step_gate.suites else job_gate

    def add_gate(job: object, step: object | None = None) -> bool:
        nonlocal affected
        gate = gate_of(job, step)
        if gate is None:
            return False
        affected = affected.merge(gate)
        return True

    def add_job(job: object) -> bool:
        if not isinstance(job, dict):
            return False
        steps = job.get("steps", [])
        if not isinstance(steps, list):
            return False
        return all(add_gate(job, step) for step in steps) if steps else add_gate(job)

    for name in set(old_jobs) | set(new_jobs):
        old_job, new_job = old_jobs.get(name), new_jobs.get(name)
        if old_job is None or new_job is None:
            job = old_job if old_job is not None else new_job
            if not add_job(job):
                return None
            continue
        if not isinstance(old_job, dict) or not isinstance(new_job, dict):
            return None
        if {key: value for key, value in old_job.items() if key != "steps"} != {key: value for key, value in new_job.items() if key != "steps"}:
            if not add_job(old_job) or not add_job(new_job):
                return None
        old_steps, new_steps = old_job.get("steps", []), new_job.get("steps", [])
        if not isinstance(old_steps, list) or not isinstance(new_steps, list):
            return None
        comparison = SequenceMatcher(None, [json.dumps(step, sort_keys=True) for step in old_steps],
                                     [json.dumps(step, sort_keys=True) for step in new_steps], autojunk=False)
        for operation, old_start, old_end, new_start, new_end in comparison.get_opcodes():
            if operation == "equal":
                continue
            for step in old_steps[old_start:old_end]:
                if not add_gate(old_job, step):
                    return None
            for step in new_steps[new_start:new_end]:
                if not add_gate(new_job, step):
                    return None
    return affected


def changed_step_suites(before: str, after: str) -> set[str] | None:
    """The suite-only view of a workflow comparison for callers that do not select entry files."""
    gate = changed_step_gate(before, after)
    return set(gate.suites) if gate is not None else None
