"""The inputs that shape a unit's run but that no recording sees: what a change to each selects, and what the reuse key holds.

A unit is a suite, or an entry file of a suite selected per entry file. This module keeps the unrecorded inputs per
entry file (``EntryUnit``): a suite selected as a whole runs when a change reaches any of its entry files, and its reuse
key holds the inputs of all of them. The unrecorded inputs of an entry file are the fields of its suite in the suite
list, the parts of the calling workflow and the workflows it calls that apply to the jobs running it, and the files of
the recording step. ``parts`` names the content of each such input per entry file on one side of a change: a revision of
the checkout, or its working tree. An entry file whose part differs between the two sides is selected; the reuse key
holds the parts of the tested tree.

- A suite job is a job that runs entry files under the selection: in the repository that holds the shared CI a Linux
  shard or the Windows job, in a consumer a job of its validation workflow whose gate names its suites or entry files.
  Every suite job records its entry files, so a job that uses the recording step is a suite job; a test on the workflow
  templates shows that the two agree. A job whose condition, strategy or environment reads the ``shards`` output runs every Linux entry file, one
  that reads the ``windows`` output every Windows entry file; any other suite job runs the units its gate names, or
  every unit without a gate.
- The jobs above a suite job are the jobs it needs, directly or through other jobs, other than the jobs that run
  ``select`` (through the suite-selection workflow, or a step that runs the Tool's ``select``), and the caller job of
  the workflow that holds it, with the jobs above that caller.
- A step of a suite job with a suite or entry-file gate belongs to the units of its gate, one whose condition names setups
  to the entry files of the job that state any of them, and one whose condition names a language step to that language
  step. A step after the last step that runs entry files and that uses no recording step only stores or reports results
  and belongs to no unit. Every other step, and the job's other keys, belong to every unit of the job.
- ``permissions``, ``env`` and ``defaults`` of a workflow that holds suite jobs belong to every unit of its suite jobs,
  and ``permissions`` of the calling workflow to every unit it reaches. ``on``, ``concurrency``, names and comments
  belong to no unit.
- The tracked files of a local recording step's folder and the modules it loads when it runs (``recording_modules.py``)
  belong to every unit of the jobs that use it; those of its ETW consumer only to the Windows entry files.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import NamedTuple

import ci_definition
import recording_modules
from reuse import content_digest

SELECTION_WORKFLOW = "suite-selection.yml"
SELECT_COMMAND = re.compile(r"suite_selection\.py\"?\s+select\b")
RECORDING_ACTION = "actions/recording"
RECORDING_SCRIPT = "recording.py"
SELECTION_TOOL = Path(__file__).resolve().parent.name
ETW_PARTS = ("etw-consumer/", "etw_consumer_build.py")
ENTRY_STEP = re.compile(r"\bexec\b[^\n]*--entry\b")
SETUP_TERM = re.compile(r"contains\(\s*(?:[A-Za-z_][\w.-]*\.setups|fromJSON\(\s*[\w.-]*setups[\w.-]*\s*\))\s*,\s*'([^']+)'\s*\)", re.IGNORECASE)
LANGUAGE_TERM = re.compile(r"fromJSON\(\s*(?:inputs|(?:needs|steps)\.[A-Za-z_][A-Za-z0-9_-]*\.outputs)\.language-steps\s*\)\s*\.\s*([a-z0-9-]+)")
SHARDS_OUTPUT = re.compile(r"\.outputs\.shards\b")
WINDOWS_OUTPUT = re.compile(r"\.outputs\.windows\b")


class EntryFile(NamedTuple):
    """An entry file of a suite, the same on both sides of a change."""

    suite: str
    entry: str


@dataclass(frozen=True)
class EntryUnit:
    """An entry file with what the workflow reads of it on one side: its suite's operating system and its setups."""

    file: EntryFile
    os: str
    setups: tuple[str, ...]


class Cause(NamedTuple):
    """Why a change selects an entry file: the changed file and the reason."""

    file: str
    reason: str


@dataclass(frozen=True)
class Part:
    """The content of one unrecorded input, with the files that hold it."""

    files: tuple[str, ...]
    digest: str


@dataclass
class Parts:
    """The parts of one side per entry file, and per language step the parts of the steps that name it."""

    entries: dict[EntryFile, dict[str, Part]] = field(default_factory=dict)
    languages: dict[str, dict[str, Part]] = field(default_factory=dict)


class Side:
    """One side of a change: a revision of the checkout, or its working tree when ``revision`` is None."""

    def __init__(self, root: Path, revision: str | None, tracked: frozenset[str]) -> None:
        self.root = root
        self.revision = revision
        self.tracked = tracked
        self._data: dict[str, bytes | None] = {}

    def data(self, path: str) -> bytes | None:
        if path not in self._data:
            if self.revision is None:
                target = self.root / path
                self._data[path] = target.read_bytes() if target.is_file() else None
            else:
                shown = subprocess.run(["git", "-C", str(self.root), "show", f"{self.revision}:{path}"], capture_output=True, check=False)
                self._data[path] = shown.stdout if shown.returncode == 0 else None
        return self._data[path]

    def text(self, path: str) -> str | None:
        data = self.data(path)
        return data.decode("utf-8", errors="replace") if data is not None else None

    def tracked_text(self, path: str) -> str | None:
        return self.text(path) if path in self.tracked else None


def entry_units(suite_list) -> list[EntryUnit]:
    """Every entry file of a resolved suite list with its suite's operating system and its setups."""
    return [EntryUnit(EntryFile(suite.name, entry), suite.os, suite.setups_of(entry)) for suite in suite_list.suites for entry in suite.entries]


# --- the suite list ------------------------------------------------------------------------------------------


def suite_fields(suite) -> dict:
    """The fields of a suite whose change runs every entry file of the suite."""
    return {"command": suite.command, "setups": suite.setups, "os": suite.os, "entry_selection": suite.entry_selection}


def stored_reads(suite, entry: str) -> tuple[set, set, set, set]:
    """The content patterns, directory memberships, existence reads and lookup directories of an entry file, extra reads
    included."""
    return set(suite.patterns_of(entry)), set(suite.memberships_of(entry)), set(suite.existence_reads_of(entry)), set(suite.lookup_directories_of(entry))


def removed_lookups(previous, current, suite, entry: str) -> bool:
    """Whether the change removes a module lookup of a directory in which the entry file looks up modules."""
    return any(set(previous.lookups_in(directory)) - set(current.lookups_in(directory)) for directory in suite.lookup_directories_of(entry))


def suite_list_changes(previous, current, list_path: str) -> dict[EntryFile, str]:
    """The entry files that a change of the suite list selects, with the reason: every entry file of a suite whose
    command, setups, operating system or selection per entry file changed; an entry file that the change adds or whose
    own setups it changes; and an entry file from whose lists, or from whose suite's lists, the change removes a read,
    a module lookup of a directory in which it looks up modules included."""
    before = {suite.name: suite for suite in previous.suites}
    selected: dict[EntryFile, str] = {}
    for suite in current.suites:
        old = before.get(suite.name)
        if old is None:
            selected.update({EntryFile(suite.name, entry): f"the suite is new in `{list_path}`" for entry in suite.entries})
            continue
        if suite_fields(old) != suite_fields(suite):
            selected.update({EntryFile(suite.name, entry): f"its command, setups, operating system or selection per entry file in `{list_path}` changed"
                             for entry in suite.entries})
            continue
        for entry in suite.entries:
            if entry not in old.entries:
                selected[EntryFile(suite.name, entry)] = f"`{entry}` is new in `{list_path}`"
            elif dict(old.entry_setups).get(entry, ()) != dict(suite.entry_setups).get(entry, ()):
                selected[EntryFile(suite.name, entry)] = f"the setups of `{entry}` in `{list_path}` changed"
            elif old.paths is not None and suite.paths is not None and (any(
                    before_reads - now_reads for before_reads, now_reads in zip(stored_reads(old, entry), stored_reads(suite, entry)))
                    or removed_lookups(previous, current, old, entry)):
                holder = f"`{entry}`" if suite.entry_selection else "the suite"
                selected[EntryFile(suite.name, entry)] = f"`{list_path}` removes reads from the lists of {holder}"
    return selected


# --- the workflows -------------------------------------------------------------------------------------------


def uses_path(value: object) -> str:
    """The path of a ``uses`` value without its ref and, for a local reference, without its prefix."""
    text = str(value or "").split("@", 1)[0].strip()
    for prefix in ci_definition.LOCAL_PREFIXES:
        if text.startswith(prefix):
            return ci_definition.repository_path(text[len(prefix):]) or ""
    return text


def is_local(value: object) -> bool:
    return isinstance(value, str) and value.startswith(ci_definition.LOCAL_PREFIXES)


def uses_recording(step: object) -> bool:
    return isinstance(step, dict) and uses_path(step.get("uses")).endswith(RECORDING_ACTION)


def condition_text(node: dict) -> str:
    text = str(node.get("if") or "").strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2].strip()
    return text


def step_role(step: dict, index: int, last_entry_step: int) -> tuple[str, frozenset[str] | ci_definition.Gate | None]:
    """What a step of a suite job belongs to: ``gate`` (its suite or entry-file gate), ``setups`` (the setups its
    condition names), ``language`` (the language steps its condition names), ``result`` (a step after the entry files
    that only stores or reports results) or ``job`` (every unit of the job)."""
    condition = condition_text(step)
    gate = ci_definition.condition_gate(condition)
    if gate.suites:
        return "gate", gate
    if setups := SETUP_TERM.findall(condition):
        return "setups", frozenset(setups)
    if languages := LANGUAGE_TERM.findall(condition):
        return "language", frozenset(languages)
    if index > last_entry_step and not uses_recording(step):
        return "result", None
    return "job", None


def suite_job_roles(job: dict) -> list[tuple[str, frozenset[str] | ci_definition.Gate | None]]:
    """The role of each step of a suite job, in step order."""
    steps = job.get("steps") if isinstance(job.get("steps"), list) else []
    entry_steps = [index for index, step in enumerate(steps) if isinstance(step, dict) and ENTRY_STEP.search(str(step.get("run") or ""))]
    last = entry_steps[-1] if entry_steps else len(steps)
    return [step_role(step if isinstance(step, dict) else {}, index, last) for index, step in enumerate(steps)]


def is_suite_job(job: object) -> bool:
    return isinstance(job, dict) and isinstance(job.get("steps"), list) and any(uses_recording(step) for step in job["steps"])


def runs_select(job: object) -> bool:
    """Whether a step of the job runs the suite-selection Tool's ``select``."""
    steps = job.get("steps") if isinstance(job, dict) else None
    return isinstance(steps, list) and any(isinstance(step, dict) and SELECT_COMMAND.search(str(step.get("run") or "")) for step in steps)


def in_gate(gate: ci_definition.Gate, unit: EntryUnit) -> bool:
    if unit.file.suite not in gate.suites:
        return False
    restricted = dict(gate.entries).get(unit.file.suite)
    return restricted is None or unit.file.entry in restricted


def job_units(job: dict, units: list[EntryUnit]) -> list[EntryUnit]:
    """The entry files a suite job runs: every Linux or Windows entry file for a job driven by the ``shards`` or
    ``windows`` output, otherwise those of the units its gate names, or every entry file for a job without a gate."""
    driven = json.dumps({key: value for key, value in job.items() if key in ("if", "strategy", "env")})
    if SHARDS_OUTPUT.search(driven):
        return [unit for unit in units if unit.os == "linux"]
    if WINDOWS_OUTPUT.search(driven):
        return [unit for unit in units if unit.os == "windows"]
    gate = ci_definition.condition_gate(job.get("if"))
    if not gate.suites:
        return list(units)
    return [unit for unit in units if in_gate(gate, unit)]


def job_needs(job: object) -> list[str]:
    value = job.get("needs") if isinstance(job, dict) else None
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            return [item.strip().strip("'\"") for item in text[1:-1].split(",") if item.strip()]
        return [text]
    return [str(item) for item in value] if isinstance(value, list) else []


def needed_jobs(name: str, needs: dict[str, set[str]]) -> set[str]:
    """The jobs that a job needs, directly or through other jobs."""
    found: set[str] = set()
    pending = list(needs.get(name, ()))
    while pending:
        other = pending.pop()
        if other not in found:
            found.add(other)
            pending.extend(needs.get(other, ()))
    return found


class CanonicalForms:
    """The canonical form of jobs and steps on one side: without names and without the comments of their scripts, with
    the document of each local action a step uses beside its reference. The recording step stands for itself, as its
    files are parts of their own."""

    def __init__(self, side: Side) -> None:
        self.side = side

    def of(self, node: object) -> tuple[object, frozenset[str]]:
        """The canonical form of a node and the local action files it reads."""
        files: set[str] = set()
        return self._canonical(node, frozenset(), files), frozenset(files)

    def _canonical(self, node: object, seen: frozenset[str], files: set[str]) -> object:
        if isinstance(node, list):
            return [self._canonical(item, seen, files) for item in node]
        if not isinstance(node, dict):
            return node
        result = {}
        for key, value in node.items():
            if key == "name" and isinstance(value, str):
                continue
            if key == "run" and isinstance(value, str):
                result[key] = ci_definition.without_comments(value)
            elif (key == "uses" and is_local(value) and not uses_path(value).endswith(ci_definition.WORKFLOW_SUFFIXES)
                  and not uses_path(value).endswith(RECORDING_ACTION)):
                result[key] = value
                result["uses-document"] = self._action(value, seen, files)
            else:
                result[key] = self._canonical(value, seen, files)
        return result

    def _action(self, value: str, seen: frozenset[str], files: set[str]) -> object:
        """The document of a local action, with the documents of the local actions its steps use."""
        folder = uses_path(value)
        file = next((f"{folder}/{name}" for name in ci_definition.ACTION_FILES if f"{folder}/{name}" in self.side.tracked), None)
        text = self.side.text(file) if file is not None else None
        if text is None or folder in seen:
            return None
        files.add(file)
        try:
            document = ci_definition.read_yaml(text, file)
        except ci_definition.WorkflowReadError:
            return text
        return self._canonical(document, seen | {folder}, files)


class PartsCollection:
    """The parts of every entry file as a walk over the workflows finds them."""

    def __init__(self, units: list[EntryUnit]) -> None:
        self.parts = Parts({unit.file: {} for unit in units})
        self.steps: dict[EntryFile | str, dict[str, dict[str, list[str]]]] = {}  # per entry file or language step, workflow and job
        self.step_files: dict[EntryFile | str, set[str]] = {}
        self.recording_units: dict[str, list[EntryUnit]] = {}  # per folder of a local recording step

    def add(self, units, label: str, files, content: object) -> None:
        part = Part(tuple(sorted(files)), content_digest(content))
        for unit in units:
            self.parts.entries[unit.file][label] = part

    def add_step(self, holder: EntryFile | str, workflow: str, job: str, content: object, files) -> None:
        """A step of a job that belongs to an entry file, or to a language step by its name."""
        self.steps.setdefault(holder, {}).setdefault(workflow, {}).setdefault(job, []).append(content_digest(content))
        self.step_files.setdefault(holder, {workflow}).update(files)

    def finish(self) -> Parts:
        for holder, workflows in self.steps.items():
            for workflow, jobs in workflows.items():
                part = Part(tuple(sorted(self.step_files[holder])), content_digest(jobs))
                target = self.parts.entries[holder] if isinstance(holder, EntryFile) else self.parts.languages.setdefault(holder, {})
                target[f"its step in `{workflow}`"] = part
        return self.parts


class WorkflowWalk:
    """A walk from the calling workflow over the jobs of the workflows it calls, adding each part to the entry files it
    applies to."""

    def __init__(self, side: Side, units: list[EntryUnit], collection: PartsCollection) -> None:
        self.side = side
        self.units = units
        self.collection = collection
        self.forms = CanonicalForms(side)

    def workflow(self, path: str, above: list[tuple[str, object, frozenset[str]]], calling: tuple[str, ...] = ()) -> list[EntryUnit]:
        """Add the parts of a workflow and of the workflows its jobs call, and return the entry files of its suite jobs.
        A workflow that ``calling``, the chain of workflows that led to it, already holds adds nothing."""
        text = self.side.text(path)
        if text is None or path in calling:
            return []
        try:
            document = ci_definition.read_yaml(text, path)
            jobs = document.get("jobs") if isinstance(document, dict) else None
            if not isinstance(jobs, dict):
                return []
            return self._jobs(path, document, jobs, above, calling + (path,))
        except ci_definition.WorkflowReadError:
            return []

    def selection_job(self, job: object) -> bool:
        """Whether a job runs ``select``: it calls the suite-selection workflow, or it or a local workflow it calls runs
        ``select`` in a step."""
        if not isinstance(job, dict):
            return False
        called = uses_path(job.get("uses"))
        if PurePosixPath(called).name == SELECTION_WORKFLOW or runs_select(job):
            return True
        if not is_local(job.get("uses")) or not called.endswith(ci_definition.WORKFLOW_SUFFIXES):
            return False
        try:
            document = ci_definition.read_yaml(self.side.text(called) or "", called)
        except ci_definition.WorkflowReadError:
            return False
        jobs = document.get("jobs") if isinstance(document, dict) else None
        return isinstance(jobs, dict) and any(runs_select(other) for other in jobs.values())

    def _job_part(self, path: str, name: str, job: object) -> tuple[str, object, frozenset[str]]:
        content, files = self.forms.of(job)
        return f"the job `{name}` of `{path}`", content, files | {path}

    def _jobs(self, path: str, document: dict, jobs: dict, above: list[tuple[str, object, frozenset[str]]],
              calling: tuple[str, ...]) -> list[EntryUnit]:
        units_by_job: dict[str, list[EntryUnit]] = {name: job_units(job, self.units) for name, job in jobs.items() if is_suite_job(job)}
        needs = {name: set(job_needs(job)) for name, job in jobs.items()}
        jobs_above = {name: [other for other in sorted(needed_jobs(name, needs)) if other in jobs and not self.selection_job(jobs[other])]
                      for name in jobs}
        # A job that calls another workflow is above that workflow's suite jobs, with the jobs above it.
        for name, job in jobs.items():
            if isinstance(job, dict) and is_local(job.get("uses")) and not self.selection_job(job):
                called = uses_path(job.get("uses"))
                if called.endswith(ci_definition.WORKFLOW_SUFFIXES):
                    chain = above + [self._job_part(path, name, job)] + [self._job_part(path, other, jobs[other]) for other in jobs_above[name]]
                    units_by_job[name] = units_by_job.get(name, []) + self.workflow(called, chain, calling)
        suite_units: set[EntryUnit] = set()
        for name, job in jobs.items():
            if not is_suite_job(job):
                continue
            units = units_by_job[name]
            suite_units.update(units)
            for label, content, files in above + [self._job_part(path, other, jobs[other]) for other in jobs_above[name]]:
                self.collection.add(units, label, files, content)
            content, files = self.forms.of({key: value for key, value in job.items() if key != "steps"})
            self.collection.add(units, f"the job `{name}` of `{path}`", files | {path}, content)
            self._steps(path, name, job, units)
            folder = next((uses_path(step.get("uses")) for step in job["steps"] if uses_recording(step) and is_local(step.get("uses"))), None)
            if folder is not None:
                self.collection.recording_units.setdefault(folder, []).extend(units)
        for key in ("permissions", "env", "defaults") if suite_units else ():
            self.collection.add(suite_units, f"`{key}` of `{path}`", {path}, document.get(key))
        return sorted({unit for units in units_by_job.values() for unit in units}, key=lambda unit: unit.file)

    def _steps(self, path: str, name: str, job: dict, units: list[EntryUnit]) -> None:
        for step, (role, value) in zip(job["steps"], suite_job_roles(job)):
            content, files = self.forms.of(step)
            if role == "language":
                for language in value:
                    self.collection.add_step(language, path, name, content, files)
                continue
            if role == "gate":
                covered = [unit for unit in self.units if in_gate(value, unit)]
            elif role == "setups":
                covered = [unit for unit in units if set(unit.setups) & value]
            else:
                covered = [] if role == "result" else units
            for unit in covered:
                self.collection.add_step(unit.file, path, name, content, files)


def recording_files(side: Side, folder: str) -> list[str]:
    """The tracked files of a local recording step's folder and the modules of the shared CI it loads when it runs."""
    search = [str(PurePosixPath(folder).parent / SELECTION_TOOL), folder]
    loaded = recording_modules.loaded_modules(f"{folder}/{RECORDING_SCRIPT}", side.tracked_text, search)
    return sorted({path for path in side.tracked if path.startswith(folder + "/")} | {path for path in loaded if path in side.tracked})


def parts(side: Side, workflow: str, units: list[EntryUnit]) -> Parts:
    """The unrecorded parts of every entry file on one side, from the calling workflow and the workflows it calls."""
    collection = PartsCollection(units)
    reached = WorkflowWalk(side, units, collection).workflow(workflow, [])
    try:
        caller = ci_definition.read_yaml(side.text(workflow) or "", workflow)
    except ci_definition.WorkflowReadError:
        caller = None
    if isinstance(caller, dict):
        collection.add(reached, f"`permissions` of `{workflow}`", {workflow}, caller.get("permissions"))
    for folder, units_of_folder in collection.recording_units.items():
        windows = [unit for unit in units_of_folder if unit.os == "windows"]
        for file in recording_files(side, folder):
            etw = file[len(folder) + 1:].startswith(ETW_PARTS)
            collection.add(windows if etw else units_of_folder, f"the recording step's file `{file}`", {file},
                           hashlib.sha256(side.data(file) or b"").hexdigest())
    return collection.finish()


def changes(previous: Parts, current: Parts, changed: set[str]) -> tuple[dict[EntryFile, Cause], set[str]]:
    """The entry files whose parts differ between the two sides, each with its cause, and the language steps whose steps
    differ; a part counts only when one of its files is among the changed files."""
    def difference(before: dict[str, Part], now: dict[str, Part]) -> Cause | None:
        for label in list(now) + [label for label in before if label not in now]:
            if before.get(label) == now.get(label):
                continue
            files = set((before.get(label) or now[label]).files) | set((now.get(label) or before[label]).files)
            file = next((file for file in sorted(files) if file in changed), None)
            if file is not None:
                return Cause(file, f"{label} changed")
        return None

    selected: dict[EntryFile, Cause] = {}
    for entry_file, now in current.entries.items():
        cause = difference(previous.entries.get(entry_file, {}), now)
        if cause is not None:
            selected[entry_file] = cause
    started = {language for language in set(current.languages) | set(previous.languages)
               if difference(previous.languages.get(language, {}), current.languages.get(language, {})) is not None}
    return selected, started


def unit_content(current: Parts, suite, entries) -> dict:
    """What the reuse key holds of a unit's unrecorded inputs: its suite's fields and the parts of its entry files."""
    return {"suite": suite_fields(suite) | {"entry_setups": {entry: list(dict(suite.entry_setups).get(entry, ())) for entry in entries}},
            "parts": {entry: {label: part.digest for label, part in sorted(current.entries.get(EntryFile(suite.name, entry), {}).items())}
                      for entry in entries}}
