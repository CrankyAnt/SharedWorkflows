#!/usr/bin/env python3
"""Select the suites of a CI job by the changed files, build its shards, and give the verdict.

A repository states its suites in a suite list: one entry per suite with its name, entry files, command, setups and the
duration estimate per entry file, and its stored path list; plus the always-run patterns. The selection compares the
changed files of a pull request or a push with the stored lists alone and answers ``run`` or ``skip`` per suite, each
with its reason; a suite without a stored path list runs on every change. It assigns
the selected Linux entry files to as few shards as keep each shard's estimated time on two workers under the bound,
lists the selected Windows entry files, and writes GitHub step outputs and Markdown for the job summary. With the calling
workflow named, files read only by gated steps run those suites. ``verdict`` reads the ``needs`` context of the calling
workflow's closing job and passes or fails on the results of the selection and the called jobs. ``docs/maintaining.md`` documents
the file format, the rules and the commands.

    python suite_selection.py select --root <repository> --suites <suite list> --base <sha> --head <sha> \\
        --compare merge-base --workflow "$GITHUB_WORKFLOW_REF" --language-step python --json-output "$GITHUB_OUTPUT" \\
        --summary "$GITHUB_STEP_SUMMARY"
    python suite_selection.py select --root <repository> --suites <suite list> --changed-file <path>
    python suite_selection.py verdict --needs <needs context as JSON> --summary "$GITHUB_STEP_SUMMARY"
    python suite_selection.py lookup --root <repository> --sha <commit> --before <commit> --workflow "$GITHUB_WORKFLOW_REF" --output <file>
    python suite_selection.py shared-lookup --root <repository> --digest <digest> --branch <base branch> [--pushed <commit>] \\
        --workflow "$GITHUB_WORKFLOW_REF" --output <file>
    python suite_selection.py store --needs <needs context as JSON> --passed <directory> --run-id <id> --output <record file>
    python suite_selection.py check-dispatch --root <repository> --sha <client_payload.sha> --branch <default branch>

``lookup``, ``shared-lookup``, ``store`` and the reuse of passed results live in ``reuse.py``.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import json
import math
import re
import subprocess
import sys
import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ci_definition  # noqa: E402
from revisions import ALL_ZERO, commit_id, git  # noqa: E402

if TYPE_CHECKING:
    from unrecorded import Cause, EntryFile, Parts

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
GLOB_CHARACTERS = ("*", "?")
SELECTION_JOB = "select"
FAILING_RESULTS = ("failure", "cancelled")
OPERATING_SYSTEMS = ("linux", "windows")
SHARD_BOUND = 120.0  # seconds of estimated traced test time per Linux shard on its workers
FULL_SHARD_BOUND = 300.0  # seconds of estimated wide-traced test time per Linux shard of the full recording
WIDE_TO_MID = 1.65  # traced test time under the wide set divided by the time under the mid set
FULL_REASON = "the full recording runs every entry file"
SHARD_WORKERS = 2
LANGUAGE_STEPS = {
    "python": ("**/*.py", "**/*.pyi", "**/pyproject.toml", "**/setup.cfg", "**/ruff.toml", "**/.ruff.toml", "**/mypy.ini", "**/.mypy.ini"),
}
SUITE_KEYS = frozenset({"name", "entries", "paths", "extra_paths", "entry_selection", "entry_paths",
                        "entry_extra_paths", "directory_memberships", "entry_directory_memberships", "existence_reads", "entry_existence_reads",
                        "extra_directory_memberships", "extra_existence_reads", "lookup_directories", "entry_lookup_directories",
                        "os", "command", "setups", "entry_setups", "entry_durations"})
# The union of sys.stdlib_module_names over the Python versions that the shared CI supports.
STANDARD_LIBRARY = Path(__file__).resolve().parent.parent / "recording" / "stdlib_module_names.json"


class SuiteListError(Exception):
    """The suite list is invalid; the message names the entry and the field."""


class VerdictError(Exception):
    """The needs context cannot be read."""


@dataclass(frozen=True)
class ExistenceRead:
    """A path whose presence and type, not its contents, a suite reads."""

    path: str
    kind: str

    def entry(self) -> dict:
        return {"path": self.path, "kind": self.kind}


def tracked_files(root: Path) -> set[str]:
    """The files Git tracks under ``root``, as posix paths relative to it, or every file when the root is no checkout."""
    listing = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", check=False)
    if listing.returncode == 0 and listing.stdout:
        return {item for item in listing.stdout.split("\0") if item}
    if not root.is_dir():
        return set()
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file() and ".git" not in path.relative_to(root).parts}


@dataclass(frozen=True)
class Suite:
    name: str
    entries: tuple[str, ...]
    paths: tuple[str, ...] | None = None  # the stored path list, or None when the suite has none
    extra_paths: tuple[str, ...] = ()
    entry_selection: bool = False  # the suite is selected per entry file: each entry runs when a changed file matches its own list
    entry_paths: tuple[tuple[str, tuple[str, ...]], ...] = ()  # the stored path list per entry file, in entry order
    entry_extra_paths: tuple[tuple[str, tuple[str, ...]], ...] = ()  # extra patterns per entry file, for reads no recording sees
    directory_memberships: tuple[str, ...] = ()  # directories whose direct child membership, not file contents, the suite reads
    entry_directory_memberships: tuple[tuple[str, tuple[str, ...]], ...] = ()
    existence_reads: tuple[ExistenceRead, ...] = ()
    entry_existence_reads: tuple[tuple[str, tuple[ExistenceRead, ...]], ...] = ()
    extra_directory_memberships: tuple[str, ...] = ()  # membership patterns no recording sees
    extra_existence_reads: tuple[ExistenceRead, ...] = ()  # existence reads no recording sees
    lookup_directories: tuple[str, ...] = ()  # the checkout directories in which the suite looks up modules, the root as .
    entry_lookup_directories: tuple[tuple[str, tuple[str, ...]], ...] = ()
    command: tuple[str, ...] | None = None  # the command that runs one entry file, with the entry file appended
    setups: tuple[str, ...] | None = None  # the setups the suite needs, or None when it states none
    entry_setups: tuple[tuple[str, tuple[str, ...]], ...] = ()  # further setups per entry file of a suite selected per entry
    entry_durations: tuple[tuple[str, float], ...] = ()  # the duration estimate per entry file, in seconds of traced test time
    os: str = "linux"  # the operating system its entry files run on: linux or windows

    def setups_of(self, entry: str) -> tuple[str, ...]:
        """The setups one entry file needs: the suite's and the entry file's own."""
        suite, own = self.setups or (), dict(self.entry_setups).get(entry, ())
        return suite + tuple(setup for setup in own if setup not in suite)

    def entry(self) -> dict:
        """The suite's JSON entry as the suite list states it."""
        item: dict = {"name": self.name, "entries": list(self.entries)}
        if self.os != "linux":
            item["os"] = self.os
        if self.command is not None:
            item["command"] = list(self.command)
        if self.setups is not None:
            item["setups"] = list(self.setups)
        if self.entry_setups:
            item["entry_setups"] = {entry: list(setups) for entry, setups in self.entry_setups}
        if self.entry_durations:
            item["entry_durations"] = dict(self.entry_durations)
        if self.entry_selection:
            item["entry_selection"] = True
        if self.paths is not None:
            item["paths"] = list(self.paths)
            if self.entry_selection:
                item["entry_paths"] = {entry: list(paths) for entry, paths in self.entry_paths}
        if self.extra_paths:
            item["extra_paths"] = list(self.extra_paths)
        if self.entry_extra_paths:
            item["entry_extra_paths"] = {entry: list(paths) for entry, paths in self.entry_extra_paths}
        if self.directory_memberships:
            item["directory_memberships"] = list(self.directory_memberships)
        if self.entry_directory_memberships:
            item["entry_directory_memberships"] = {entry: list(paths) for entry, paths in self.entry_directory_memberships}
        if self.existence_reads:
            item["existence_reads"] = [read.entry() for read in self.existence_reads]
        if self.entry_existence_reads:
            item["entry_existence_reads"] = {entry: [read.entry() for read in reads] for entry, reads in self.entry_existence_reads}
        if self.extra_directory_memberships:
            item["extra_directory_memberships"] = list(self.extra_directory_memberships)
        if self.extra_existence_reads:
            item["extra_existence_reads"] = [read.entry() for read in self.extra_existence_reads]
        if self.lookup_directories:
            item["lookup_directories"] = list(self.lookup_directories)
        if self.entry_lookup_directories:
            item["entry_lookup_directories"] = {entry: list(directories) for entry, directories in self.entry_lookup_directories}
        return item

    # Each of the three takes an entry file or None. An entry file of a suite selected per entry file has its own lists;
    # any other entry file, and None, stand for the suite as a whole.

    def _own(self, entry: str | None) -> str | None:
        return entry if self.entry_selection else None

    def patterns_of(self, entry: str | None) -> tuple[str, ...]:
        """The stored paths with the suite's extra paths and, for an entry file, its own extra paths added."""
        own = self._own(entry)
        if own is None:
            return _with_extra(self.paths or (), self.extra_paths)
        return _with_extra(dict(self.entry_paths).get(own, ()), self.extra_paths + dict(self.entry_extra_paths).get(own, ()))

    def memberships_of(self, entry: str | None) -> tuple[str, ...]:
        """The stored directory memberships with the suite's extra directory memberships added."""
        own = self._own(entry)
        stored = self.directory_memberships if own is None else dict(self.entry_directory_memberships).get(own, ())
        return _with_extra(stored, self.extra_directory_memberships)

    def existence_reads_of(self, entry: str | None) -> tuple[ExistenceRead, ...]:
        """The stored existence reads with the suite's extra existence reads added."""
        own = self._own(entry)
        stored = self.existence_reads if own is None else dict(self.entry_existence_reads).get(own, ())
        return _with_extra(stored, self.extra_existence_reads)

    def lookup_directories_of(self, entry: str | None) -> tuple[str, ...]:
        """The stored checkout directories in which the suite or the entry file looks up modules."""
        own = self._own(entry)
        return self.lookup_directories if own is None else dict(self.entry_lookup_directories).get(own, ())

    def per_entry[T](self, lists_of: Callable[[str], T]) -> tuple[tuple[str, T], ...]:
        """The lists of each entry file of a suite selected per entry file; none for a suite selected as a whole."""
        return tuple((entry, lists_of(entry)) for entry in self.entries) if self.entry_selection else ()


def _with_extra[T](own: tuple[T, ...], extra: tuple[T, ...]) -> tuple[T, ...]:
    """The stored items followed by the extra items that they lack."""
    return own + tuple(item for item in extra if item not in own)


@dataclass(frozen=True)
class SuiteList:
    always: tuple[str, ...]
    suites: tuple[Suite, ...]
    module_lookups: tuple[tuple[str, tuple[str, ...]], ...] = ()  # per checkout directory, the module lookups of every entry file

    @functools.cached_property
    def lookups_by_directory(self) -> dict[str, tuple[str, ...]]:
        return dict(self.module_lookups)

    def lookups_in(self, directory: str) -> tuple[str, ...]:
        return self.lookups_by_directory.get(directory, ())


@dataclass(frozen=True)
class Basis:
    """What a suite is selected on at select time: its path list, or the reason it runs on every change."""

    suite: Suite
    patterns: tuple[str, ...] | None  # the stored path list with the extra paths, or None
    reason: str | None  # why the suite runs on every change, or None
    entry_patterns: tuple[tuple[str, tuple[str, ...]], ...] = ()  # the patterns per entry file of a suite selected per entry
    directory_memberships: tuple[str, ...] = ()
    entry_memberships: tuple[tuple[str, tuple[str, ...]], ...] = ()
    existence_reads: tuple[ExistenceRead, ...] = ()
    entry_existence_reads: tuple[tuple[str, tuple[ExistenceRead, ...]], ...] = ()
    lookup_directories: tuple[str, ...] = ()
    entry_lookup_directories: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass(frozen=True)
class Decision:
    suite: str
    run: bool
    reason: str
    entries: tuple[str, ...] = ()  # the entry files that run: all of them, or, for a suite selected per entry, the selected ones
    per_entry: bool = False  # the decision selected the entries one by one on their own path lists
    definition_reads: tuple[str, ...] = ()  # files of the CI definition that the selection counts as read by the suite

    @property
    def result(self) -> str:
        return "run" if self.run else "skip"

    @property
    def modules(self) -> tuple[str, ...]:
        """The running Python entry files as module names, for a ``python -m unittest`` step."""
        return tuple(entry[:-3].replace("/", ".") for entry in self.entries if entry.endswith(".py"))


@dataclass(frozen=True)
class Selection:
    decisions: tuple[Decision, ...]
    unread: tuple[str, ...]  # changed files that no suite reads
    undetermined: str | None  # why the changed files could not be determined, or None
    started: tuple[str, ...] = ()  # the language steps that a changed step naming them starts


# --- the suite list ----------------------------------------------------------------------------------------


def _strings(value, location: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise SuiteListError(f"{location} must be a list of non-empty strings")
    return tuple(value)


def _existence_reads(value, location: str) -> tuple[ExistenceRead, ...]:
    if not isinstance(value, list):
        raise SuiteListError(f"{location} must be a list of path and kind records")
    reads = []
    for index, item in enumerate(value, start=1):
        if (not isinstance(item, dict) or set(item) != {"path", "kind"}
                or not isinstance(item["path"], str) or not item["path"]
                or item["kind"] not in ("file", "directory", "any")):
            raise SuiteListError(f"{location}[{index}] must name a non-empty path and kind file, directory or any")
        reads.append(ExistenceRead(item["path"], item["kind"]))
    return tuple(reads)


def _membership_patterns(value, location: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise SuiteListError(f"{location} must be a list of non-empty path patterns")
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str) or not item:
            raise SuiteListError(f"{location}[{index}] must be a non-empty path pattern")
    return tuple(value)


def _entry_existence_reads(value, entries: tuple[str, ...], location: str):
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SuiteListError(f"{location} must be an object with existence reads per entry file")
    unknown = sorted(set(value) - set(entries))
    if unknown:
        raise SuiteListError(f"{location} names {', '.join(unknown)}, which the entries lack")
    return tuple((entry, _existence_reads(value[entry], f"{location} ({entry})")) for entry in entries if entry in value)


def _command(value, location: str) -> tuple[str, ...]:
    command = _strings(value, location)
    if not command:
        raise SuiteListError(f"{location} must name at least the program")
    return command


def _duration(value, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise SuiteListError(f"{location} must be a number of seconds, zero or more")
    return value


def _per_entry_file(value, entries: tuple[str, ...], location: str, read) -> tuple[tuple[str, object], ...]:
    """A mapping from entry file to a value, in key order; a key names an entry or a file that a glob entry matches."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SuiteListError(f"{location} must be an object with one value per entry file")
    patterns = [entry for entry in entries if any(character in entry for character in GLOB_CHARACTERS)]
    unknown = sorted(key for key in value if key not in entries and not any(matches(pattern, key) for pattern in patterns))
    if unknown:
        raise SuiteListError(f"{location} names {', '.join(unknown)}, which the entries lack")
    return tuple((key, read(item, f"{location} ({key})")) for key, item in value.items())


def parse_suite_list(text: str, source: str = "the suite list") -> SuiteList:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise SuiteListError(f"{source} is not valid JSON: {error}") from error
    if not isinstance(raw, dict) or not {"always", "suites"} <= set(raw) <= {"always", "suites", "module_lookups"}:
        raise SuiteListError(f"{source} must be an object with the keys always and suites, and optionally module_lookups")
    always = _strings(raw["always"], f"{source}: always")
    module_lookups = _module_lookups(raw.get("module_lookups", {}), f"{source}: module_lookups")
    if not isinstance(raw["suites"], list):
        raise SuiteListError(f"{source}: suites must be a list")
    suites: list[Suite] = []
    names: set[str] = set()
    for index, item in enumerate(raw["suites"], start=1):
        location = f"{source}: suites[{index}]"
        if not isinstance(item, dict):
            raise SuiteListError(f"{location} must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not NAME_PATTERN.match(name):
            raise SuiteListError(f"{location}: name must be lowercase letters, digits and hyphens")
        if name in names:
            raise SuiteListError(f"{location}: duplicate suite name {name}")
        names.add(name)
        entries = _strings(item.get("entries"), f"{location} ({name}): entries")
        if not entries:
            raise SuiteListError(f"{location} ({name}): entries must name at least one file")
        has_paths = "paths" in item
        extra = set(item) - SUITE_KEYS
        if extra:
            raise SuiteListError(f"{location} ({name}): unknown keys {', '.join(sorted(extra))}")
        extra_paths = _strings(item["extra_paths"], f"{location} ({name}): extra_paths") if "extra_paths" in item else ()
        entry_selection = item.get("entry_selection", False)
        if not isinstance(entry_selection, bool):
            raise SuiteListError(f"{location} ({name}): entry_selection must be true or false")
        entry_paths = _per_entry(item.get("entry_paths"), entries, f"{location} ({name}): entry_paths") if "entry_paths" in item else ()
        entry_extra_paths = _per_entry(item.get("entry_extra_paths"), entries, f"{location} ({name}): entry_extra_paths", complete=False) if "entry_extra_paths" in item else ()
        memberships = _strings(item["directory_memberships"], f"{location} ({name}): directory_memberships") if "directory_memberships" in item else ()
        entry_memberships = _per_entry(item["entry_directory_memberships"], entries, f"{location} ({name}): entry_directory_memberships", complete=False) if "entry_directory_memberships" in item else ()
        existence = _existence_reads(item["existence_reads"], f"{location} ({name}): existence_reads") if "existence_reads" in item else ()
        entry_existence = _entry_existence_reads(item["entry_existence_reads"], entries, f"{location} ({name}): entry_existence_reads") if "entry_existence_reads" in item else ()
        extra_memberships = _membership_patterns(item["extra_directory_memberships"], f"{location} ({name}): extra_directory_memberships") if "extra_directory_memberships" in item else ()
        extra_existence = _existence_reads(item["extra_existence_reads"], f"{location} ({name}): extra_existence_reads") if "extra_existence_reads" in item else ()
        lookup_directories = _strings(item["lookup_directories"], f"{location} ({name}): lookup_directories") if "lookup_directories" in item else ()
        entry_lookup_directories = (_per_entry(item["entry_lookup_directories"], entries, f"{location} ({name}): entry_lookup_directories", complete=False)
                                    if "entry_lookup_directories" in item else ())
        operating_system = item.get("os", "linux")
        if operating_system not in OPERATING_SYSTEMS:
            raise SuiteListError(f"{location} ({name}): os must be one of {', '.join(OPERATING_SYSTEMS)}")
        command = _command(item["command"], f"{location} ({name}): command") if "command" in item else None
        setups = _strings(item["setups"], f"{location} ({name}): setups") if "setups" in item else None
        entry_setups = _per_entry_file(item["entry_setups"], entries, f"{location} ({name}): entry_setups", _strings) if "entry_setups" in item else ()
        entry_durations = _per_entry_file(item["entry_durations"], entries, f"{location} ({name}): entry_durations", _duration) if "entry_durations" in item else ()
        if entry_existence and not entry_selection:
            raise SuiteListError(f"{location} ({name}): entry_existence_reads needs entry_selection")
        if entry_memberships and not entry_selection:
            raise SuiteListError(f"{location} ({name}): entry_directory_memberships needs entry_selection")
        if entry_lookup_directories and not entry_selection:
            raise SuiteListError(f"{location} ({name}): entry_lookup_directories needs entry_selection")
        if entry_paths and not entry_selection:
            raise SuiteListError(f"{location} ({name}): entry_paths needs entry_selection")
        if entry_setups and not entry_selection:
            raise SuiteListError(f"{location} ({name}): entry_setups needs entry_selection")
        if has_paths and entry_selection and not entry_paths:
            raise SuiteListError(f"{location} ({name}): a stored suite with entry_selection needs entry_paths")
        common = dict(extra_paths=extra_paths, entry_selection=entry_selection, entry_extra_paths=entry_extra_paths,
                      directory_memberships=memberships, entry_directory_memberships=entry_memberships, existence_reads=existence,
                      entry_existence_reads=entry_existence, extra_directory_memberships=extra_memberships,
                      extra_existence_reads=extra_existence, lookup_directories=lookup_directories,
                      entry_lookup_directories=entry_lookup_directories, command=command, setups=setups,
                      entry_setups=entry_setups, entry_durations=entry_durations, os=operating_system)
        if has_paths:
            suites.append(Suite(name, entries, paths=_strings(item["paths"], f"{location} ({name}): paths"), entry_paths=entry_paths, **common))
        else:
            suites.append(Suite(name, entries, **common))
    return SuiteList(always, tuple(suites), module_lookups)


def _module_lookups(value, location: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The module lookups per checkout directory, in key order."""
    if not isinstance(value, dict) or not all(isinstance(key, str) and key for key in value):
        raise SuiteListError(f"{location} must be an object with a list of module names per checkout directory")
    return tuple((directory, _strings(names, f"{location} ({directory})")) for directory, names in value.items())


def _per_entry(value, entries: tuple[str, ...], location: str, *, complete: bool = True) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """A mapping from entry file to a list of patterns, in entry order; with ``complete``, every entry needs a key."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SuiteListError(f"{location} must be an object with one list of patterns per entry file")
    unknown = sorted(set(value) - set(entries))
    if unknown:
        raise SuiteListError(f"{location} names {', '.join(unknown)}, which the entries lack")
    if complete and set(value) != set(entries):
        raise SuiteListError(f"{location} must name every entry file")
    return tuple((entry, _strings(value[entry], f"{location} ({entry})")) for entry in entries if entry in value)


def load_suite_list(path: Path) -> SuiteList:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SuiteListError(f"cannot read {path}: {error}") from error
    return parse_suite_list(text, str(path))


def render_suite_list(suite_list: SuiteList) -> str:
    """The suite list as JSON text, one path per line, with a trailing newline."""
    document: dict = {"always": list(suite_list.always), "suites": [suite.entry() for suite in suite_list.suites]}
    if suite_list.module_lookups:
        document["module_lookups"] = {directory: list(names) for directory, names in suite_list.module_lookups}
    return json.dumps(document, indent=2) + "\n"


def expand_entries(entries: tuple[str, ...], tracked: set[str], location: str) -> tuple[str, ...]:
    """The tracked files the entries name: a glob pattern as the files it matches, a path as itself."""
    expanded: list[str] = []
    for entry in entries:
        normalized = entry.replace("\\", "/")
        if any(character in normalized for character in GLOB_CHARACTERS):
            found = sorted(file for file in tracked if matches(normalized, file))
        else:
            found = [normalized] if normalized in tracked else []
        if not found:
            raise SuiteListError(f"{location}: the entry {entry} matches no tracked file")
        expanded += [file for file in found if file not in expanded]
    return tuple(expanded)


def resolve_entries(suite_list: SuiteList, tracked: set[str], source: str = "the suite list") -> SuiteList:
    """The suite list with every entry pattern expanded to the tracked files it matches."""
    suites = []
    for index, suite in enumerate(suite_list.suites, start=1):
        entries = expand_entries(suite.entries, tracked, f"{source}: suites[{index}] ({suite.name})")
        suites.append(dataclasses.replace(suite, entries=entries))
    return SuiteList(suite_list.always, tuple(suites), suite_list.module_lookups)


def module_name(name: str) -> str:
    """The module name that a file or directory name carries: the name up to its first dot."""
    return name.split(".", 1)[0]


@functools.cache
def standard_library_names() -> frozenset[str]:
    """The standard-library module names of every Python version that the shared CI supports."""
    return frozenset(json.loads(STANDARD_LIBRARY.read_text(encoding="utf-8"))["names"])


# --- patterns -------------------------------------------------------------------------------------------------


@functools.lru_cache(maxsize=None)
def pattern_regex(pattern: str) -> re.Pattern[str]:
    """GitHub path-filter glob syntax: ``**`` crosses directory separators, ``*`` and ``?`` do not."""
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if pattern.startswith("**/", index):
            parts.append("(?:.*/)?")
            index += 3
            continue
        if pattern.startswith("**", index):
            parts.append(".*")
            index += 2
            continue
        if character == "*":
            parts.append("[^/]*")
        elif character == "?":
            parts.append("[^/]")
        else:
            parts.append(re.escape(character))
        index += 1
    return re.compile("^" + "".join(parts) + "$")


def matches(pattern: str, path: str) -> bool:
    return pattern_regex(pattern).match(path) is not None


def first_match(patterns: tuple[str, ...], files: list[str]) -> tuple[str, str] | None:
    """The first changed file that matches a pattern, with that pattern, in file order."""
    for file in files:
        for pattern in patterns:
            if matches(pattern, file):
                return file, pattern
    return None


# --- the ignore rules -----------------------------------------------------------------------------------------


IGNORE_FILE = ".gitignore"


def is_ignore_file(path: str) -> bool:
    return path.rsplit("/", 1)[-1] == IGNORE_FILE


def ignore_regex(pattern: str) -> str:
    """A gitignore(5) glob as a regular expression over a path relative to the ignore file's directory: ``*``, ``?`` and
    a bracket expression never match ``/``; a leading ``**/``, a trailing ``/**`` and an inner ``/**/`` cross it."""
    parts: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index) and index == 0:
            parts.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("/**/", index):
            parts.append("/(?:.*/)?")
            index += 4
        elif pattern.startswith("/**", index) and index + 3 == len(pattern):
            parts.append("/.*")
            index += 3
        elif pattern[index] == "*":
            while index < len(pattern) and pattern[index] == "*":
                index += 1
            parts.append("[^/]*")
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        elif pattern[index] == "\\" and index + 1 < len(pattern):
            parts.append(re.escape(pattern[index + 1]))
            index += 2
        elif pattern[index] == "[" and (end := pattern.find("]", index + (3 if pattern[index + 1:index + 2] in ("!", "^") else 2))) != -1:
            body = pattern[index + 1:end]
            negated = body[:1] in ("!", "^")
            body = body[1:] if negated else body
            members = "".join(character if character == "-" else re.escape(character) for character in body)
            parts.append(f"(?!/)[{'^' if negated else ''}{members}]")
            index = end + 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return "".join(parts)


@dataclass(frozen=True)
class IgnorePattern:
    base: str  # the directory of its ignore file, the root as ""
    regex: re.Pattern[str]
    negated: bool
    directory_only: bool


def ignore_patterns(path: str, text: str) -> list[IgnorePattern]:
    base = path.rpartition("/")[0]
    patterns = []
    for line in text.splitlines():
        line = re.sub(r"(?<!\\) +$", "", line)
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        line = line[1:] if negated else line
        directory_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            continue
        anchored = "/" in line
        expression = ignore_regex(line.lstrip("/"))
        patterns.append(IgnorePattern(base, re.compile(("" if anchored else "(?:.*/)?") + expression + r"\Z"), negated, directory_only))
    return patterns


class IgnoreRules:
    """The ignore rules of the tracked ``.gitignore`` files of one commit, as gitignore(5) defines them, matched
    case-sensitively against repository paths with ``/`` separators; no other ignore source takes part."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = dict(files)
        ordered = sorted(self.files, key=lambda path: (path.count("/"), path))
        self.patterns = [pattern for path in ordered for pattern in ignore_patterns(path, self.files[path])]
        self._decided: dict[tuple[str, bool], bool] = {}

    def ignored(self, path: str, directory: bool = False) -> bool:
        """Whether the rules match the path, or one of its parent directories; ``directory`` says that the path is one."""
        parts = path.split("/")
        return any(self._matched("/".join(parts[:index]), True) for index in range(1, len(parts))) or self._matched(path, directory)

    def _matched(self, path: str, directory: bool) -> bool:
        key = (path, directory)
        if key not in self._decided:
            result = False
            for pattern in self.patterns:
                if pattern.directory_only and not directory:
                    continue
                if pattern.base and not path.startswith(pattern.base + "/"):
                    continue
                if pattern.regex.match(path[len(pattern.base) + 1:] if pattern.base else path):
                    result = not pattern.negated
            self._decided[key] = result
        return self._decided[key]


def ignore_rules(root: Path, revision: str, files: frozenset[str]) -> IgnoreRules:
    """The ignore rules of the tracked ``.gitignore`` files among ``files``, with their text at the revision."""
    return IgnoreRules({path: revision_text(root, revision, path) or "" for path in sorted(file for file in files if is_ignore_file(file))})


def edited_ignore_files(root: Path, tracked: frozenset[str]) -> list[str]:
    """The tracked ``.gitignore`` files whose staged or working-tree state differs from ``HEAD``; every tracked one when
    ``HEAD`` cannot be compared."""
    listing = git(root, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--", ":(glob)**/.gitignore")
    if listing.returncode != 0:
        return sorted(path for path in tracked if is_ignore_file(path))
    return sorted(path for path in listing.stdout.split("\0") if path)


# --- the changed files ---------------------------------------------------------------------------------------


def changed_files(root: Path, base: str, head: str, compare: str) -> tuple[list[str] | None, str | None]:
    """The changed files between the commits, or None with the reason they could not be determined.

    ``compare`` is ``merge-base`` for a pull request (the files that differ between the merge base of
    base and head and head) or ``direct`` for a push (the files that differ between base and head). A
    renamed file counts under its old and its new path, a deleted file under its path.
    """
    if not base or ALL_ZERO.match(base):
        return None, f"the previous head {base or '(empty)'} is the all-zero commit"
    for label, commit in (("previous head", base), ("head", head)):
        if commit_id(root, commit) is None:
            return None, f"the {label} {commit} is absent from the checkout"
    start = base
    if compare == "merge-base":
        merge_base = git(root, "merge-base", base, head)
        if merge_base.returncode != 0:
            return None, f"{base} and {head} have no merge base"
        start = merge_base.stdout.strip()
    listing = git(root, "diff", "--name-only", "--no-renames", "-z", start, head)
    if listing.returncode != 0:
        return None, f"git diff failed: {listing.stderr.strip()}"
    return sorted({item for item in listing.stdout.split("\0") if item}), None


def previous_revision(root: Path, base: str, head: str, compare: str) -> str | None:
    """The commit the changed files are compared from: the merge base for a pull request, the base for a push."""
    if not base or ALL_ZERO.match(base) or commit_id(root, base) is None or commit_id(root, head) is None:
        return None
    if compare == "merge-base":
        merge_base = git(root, "merge-base", base, head)
        return merge_base.stdout.strip() if merge_base.returncode == 0 else None
    return base


def revision_text(root: Path, revision: str, path: str) -> str | None:
    """The text of a tracked file at a revision, or None when the revision or the file is not there."""
    shown = subprocess.run(["git", "-C", str(root), "show", f"{revision}:{path}", "--"], capture_output=True, check=False)
    if shown.returncode != 0:
        return None
    return shown.stdout.decode("utf-8", errors="replace")


def revision_files(root: Path, revision: str) -> frozenset[str]:
    """Tracked paths at a revision, without extracting file contents."""
    listing = git(root, "ls-tree", "-r", "--name-only", "-z", revision)
    if listing.returncode != 0:
        raise SuiteListError(f"the tracked paths of {revision} could not be read: {listing.stderr.strip()}")
    return frozenset(path for path in listing.stdout.split("\0") if path)


def workflow_path(value: str) -> str:
    """The repository path of the calling workflow from a path or from ``github.workflow_ref``
    (``<owner>/<repository>/<path>@<ref>``); the CI definition normalises it like every other path."""
    if "@" not in value:
        return value
    value = value.split("@", 1)[0]
    parts = value.split("/", 2)
    return parts[2] if len(parts) == 3 else value


# --- the selection --------------------------------------------------------------------------------------------


NO_PATH_LIST_REASON = "runs on every change: no stored path list"


def bases(suite_list: SuiteList) -> tuple[Basis, ...]:
    """The basis of every suite: its stored list, or the reason it runs on every change."""
    result = []
    for suite in suite_list.suites:
        if suite.paths is not None:
            result.append(Basis(suite, suite.patterns_of(None), None, entry_patterns=suite.per_entry(suite.patterns_of),
                                directory_memberships=suite.memberships_of(None),
                                entry_memberships=suite.per_entry(suite.memberships_of),
                                existence_reads=suite.existence_reads_of(None),
                                entry_existence_reads=suite.per_entry(suite.existence_reads_of),
                                lookup_directories=suite.lookup_directories_of(None),
                                entry_lookup_directories=suite.per_entry(suite.lookup_directories_of)))
        else:
            result.append(Basis(suite, None, NO_PATH_LIST_REASON))
    return tuple(result)


def every_suite(suite_list: SuiteList, reason: str, undetermined: str | None = None) -> Selection:
    """The selection in which every suite runs for one reason."""
    return Selection(tuple(Decision(suite.name, True, reason, suite.entries) for suite in suite_list.suites), (), undetermined)


def membership_change(root: Path | None, previous_root: Path | None, membership: str, changed: list[str], *,
                      previous_tracked: frozenset[str] | None = None,
                      current_tracked: frozenset[str] | None = None) -> str | None:
    """A changed filename that alters a directory listing or glob result, without treating contents as names."""
    prefix = "" if membership in ("", ".") else membership.strip("/") + "/"
    tracked = current_tracked
    if tracked is None:
        tracked = frozenset(tracked_files(root)) if root is not None else frozenset()
    if any(character in membership for character in GLOB_CHARACTERS):
        def names(files: frozenset[str]) -> set[str]:
            directories = {path[:index] for path in files for index, char in enumerate(path) if char == "/"}
            candidates = directories if membership.endswith("/") else set(files) | directories
            return {path for path in candidates if matches(membership.rstrip("/"), path)}

        if previous_tracked is not None:
            different = names(previous_tracked) ^ names(tracked)
            return next((file for file in changed if any(file == name or file.startswith(name + "/")
                                                        for name in different)), None)
        if root is not None:
            # Both sides are file sets, so that a pattern matches the same names as between two trees: the tracked files
            # before the change, and after it the tracked files that still exist with the changed files that do.
            current = frozenset(path for path in tracked if (root / path).is_file()) | {file for file in changed if (root / file).is_file()}
            previous = (frozenset(path.relative_to(previous_root).as_posix() for path in previous_root.rglob("*") if path.is_file())
                        if previous_root is not None else tracked)
            different = names(previous) ^ names(current)
            return next((file for file in changed if any(file == name or file.startswith(name + "/")
                                                        for name in different)), None)
        return None
    for file in changed:
        if not file.startswith(prefix):
            continue
        child = file[len(prefix):].split("/", 1)[0]
        if not child:
            continue
        child_path = prefix + child
        current = root / membership / child if root is not None else None
        previous = previous_root / membership / child if previous_root is not None else None
        if previous_tracked is not None:
            before = any(path == child_path or path.startswith(child_path + "/") for path in previous_tracked)
            after = any(path == child_path or path.startswith(child_path + "/") for path in tracked)
            if before != after:
                return file
        elif previous is not None and current is not None:
            if current.exists() != previous.exists():
                return file
        elif current is not None:
            known = any(path == child_path or path.startswith(child_path + "/") for path in tracked)
            if not known or (not current.exists() and known):
                return file
    return None


def existence_change(root: Path | None, previous_root: Path | None, read: ExistenceRead, changed: list[str], *,
                     previous_tracked: frozenset[str] | None = None,
                     current_tracked: frozenset[str] | None = None) -> str | None:
    """A changed path that changes an exists/is_file/is_dir predicate, using tree names or the filesystem."""
    path = read.path.replace("\\", "/").strip("/")
    candidates = [file for file in changed if file == path or file.startswith(path + "/") or path.startswith(file + "/")]
    if not candidates:
        return None

    def in_tree(files: frozenset[str]) -> bool:
        file = path in files
        directory = path == "." or any(name.startswith(path + "/") for name in files)
        return file if read.kind == "file" else directory if read.kind == "directory" else file or directory

    def on_disk(base: Path) -> bool:
        target = base / path
        return target.is_file() if read.kind == "file" else target.is_dir() if read.kind == "directory" else target.exists()

    tracked = current_tracked if current_tracked is not None else frozenset(tracked_files(root)) if root else frozenset()
    if previous_tracked is not None:
        before, after = in_tree(previous_tracked), in_tree(tracked)
    elif root is not None:
        before = on_disk(previous_root) if previous_root is not None else in_tree(tracked)
        after = on_disk(root)
    else:
        return None
    return candidates[0] if before != after else None


def added_files(root: Path | None, previous_root: Path | None, changed: list[str], previous_tracked: frozenset[str] | None,
                current_tracked: frozenset[str] | None) -> list[str]:
    """The changed files that the change adds: between two trees, the files only the head tracks; on disk, the files that
    exist and did not exist before, every one that exists without the revision before."""
    if previous_tracked is not None and current_tracked is not None:
        return [file for file in changed if file in current_tracked and file not in previous_tracked]
    if root is not None:
        return [file for file in changed if (root / file).exists() and (previous_root is None or not (previous_root / file).exists())]
    return list(changed)


def lookup_change(directory: str, names: frozenset[str], added: list[str], existed: Callable[[str], bool]) -> tuple[str, str] | None:
    """An added file that adds a file or directory in ``directory`` whose module name is one of ``names``, with that
    name: a file's name up to its first dot, or a new directory's name. A file added in a directory that existed
    before counts only as its ``__init__`` file, which makes the directory a package. ``existed`` tells whether a path
    was a directory before the change."""
    prefix = "" if directory in ("", ".") else directory.strip("/") + "/"
    for file in added:
        if not file.startswith(prefix):
            continue
        parts = file[len(prefix):].split("/")
        if len(parts) == 1:
            name = module_name(parts[0])
        elif existed(prefix + parts[0]) and not (len(parts) == 2 and parts[1].startswith("__init__.")):
            continue
        else:
            name = parts[0]
        if name in names:
            return file, name
    return None


def ignore_change(changed: list[str], ignore: IgnoreRules | None, previous_tracked: frozenset[str] | None,
                  current_tracked: frozenset[str] | None) -> str | None:
    """Why the change runs every unit through the ignore rules: it changes a tracked ``.gitignore`` file, or it adds a
    path that the ignore rules of its head commit match. Without the tracked paths before the change, every changed
    path that is tracked counts as added."""
    rules = next((file for file in changed if is_ignore_file(file)), None)
    if rules is not None:
        return f"the tracked ignore file `{rules}` changed"
    if ignore is None or current_tracked is None:
        return None
    added = next((file for file in changed if file in current_tracked and (previous_tracked is None or file not in previous_tracked)
                  and ignore.ignored(file)), None)
    return None if added is None else f"the added `{added}` is matched by the ignore rules of the head commit"


def select(suite_list: SuiteList, changed: list[str] | None, undetermined: str | None = None, *,
           root: Path | None = None, definition: list[ci_definition.Item] | None = None,
           previous_root: Path | None = None, workflow_steps: tuple[str, ci_definition.Gate | set[str]] | None = None,
           previous_tracked: frozenset[str] | None = None, current_tracked: frozenset[str] | None = None,
           ignore: IgnoreRules | None = None, unrecorded_causes: dict[EntryFile, Cause] | None = None,
           started: frozenset[str] = frozenset()) -> Selection:
    """One decision per suite, in suite-file order, and the changed files that no suite reads.

    ``definition`` is the CI definition of the calling workflow, when the caller names one; ``previous_root``
    a checkout of the revision before the change, for membership and existence on disk;
    ``workflow_steps`` the calling workflow's path and the suites whose own gated steps are all that changed in
    it between the two commits (``ci_definition.changed_step_gate``). The workflow then runs those consumers
    and the suites that read its contents. Ungated control steps do not attribute inputs to product suites.
    ``previous_tracked`` and ``current_tracked`` compare directory membership between Git trees without
    reading historical file contents. Manual changes use the index and current filesystem instead.
    ``ignore`` holds the ignore rules of the head commit. ``unrecorded_causes`` names the entry files whose unrecorded
    inputs the change alters (``unrecorded.py``), each with its cause; ``started`` the language steps a changed step
    starts. A suite without a stored path list runs on every change.
    """
    if changed is None:
        return every_suite(suite_list, f"the changed files could not be determined: {undetermined}", undetermined or "unknown")
    attributed = workflow_steps if workflow_steps is not None and workflow_steps[0] in changed else None
    _, gated = ci_definition.definition_matches(definition or [], [file for file in changed if attributed is None or file != attributed[0]])
    for gate_suite, matched in gated.items():
        if gate_suite not in {suite.name for suite in suite_list.suites}:
            file, item = matched[0]
            raise SuiteListError(f"`{file}` belongs to the CI definition ({item.role}) and a step is gated on `{gate_suite}`, which the suite list lacks")
    always = first_match(suite_list.always, changed)
    if always is not None:
        return every_suite(suite_list, f"every suite runs: `{always[0]}` matches the always-run pattern `{always[1]}`")
    ignore_reason = ignore_change(changed, ignore, previous_tracked, current_tracked)
    if ignore_reason is not None:
        return every_suite(suite_list, f"every suite runs: {ignore_reason}")
    resolved = bases(suite_list)
    memberships = {membership for basis in resolved for membership in basis.directory_memberships}
    memberships.update(membership for basis in resolved for _, values in basis.entry_memberships for membership in values)
    existence = {read for basis in resolved for read in basis.existence_reads}
    existence.update(read for basis in resolved for _, values in basis.entry_existence_reads for read in values)
    looked_in = {directory for basis in resolved for directory in basis.lookup_directories}
    looked_in.update(directory for basis in resolved for _, values in basis.entry_lookup_directories for directory in values)
    if (memberships or existence or looked_in) and current_tracked is None and root is not None:
        current_tracked = frozenset(tracked_files(root))
    membership_changes = {membership: membership_change(root, previous_root, membership, changed,
                          previous_tracked=previous_tracked, current_tracked=current_tracked) for membership in memberships}
    existence_changes = {read: existence_change(root, previous_root, read, changed,
                         previous_tracked=previous_tracked, current_tracked=current_tracked) for read in existence}
    added = added_files(root, previous_root, changed, previous_tracked, current_tracked) if looked_in else []

    def existed(path: str) -> bool:
        if previous_tracked is not None:
            return any(file.startswith(path + "/") for file in previous_tracked)
        if previous_root is not None:
            return (previous_root / path).is_dir()
        return any(file.startswith(path + "/") for file in current_tracked or () if file not in added)

    standard = standard_library_names() if looked_in else frozenset()
    lookup_changes = {directory: lookup_change(directory, standard | frozenset(suite_list.lookups_in(directory)), added, existed)
                      for directory in looked_in}

    def lookup_match(directories: tuple[str, ...]) -> tuple[str, str] | None:
        """The added file that a module lookup of one of ``directories`` matches, with the lookup's description."""
        for directory in directories:
            found = lookup_changes[directory]
            if found is not None:
                return found[0], f"module lookup `{found[1]}` in `{directory}`"
        return None
    decisions: list[Decision] = []
    read: set[str] = set()
    for basis in resolved:
        if basis.patterns is not None:
            read.update(file for file in changed if any(matches(pattern, file) for pattern in basis.patterns))
            read.update(file for membership in basis.directory_memberships
                        if (file := membership_changes[membership]) is not None)
            read.update(file for dependency in basis.existence_reads
                        if (file := existence_changes[dependency]) is not None)
            read.update(found[0] for directory in basis.lookup_directories if (found := lookup_changes[directory]) is not None)
        if basis.reason is not None:
            decisions.append(Decision(basis.suite.name, True, basis.reason, basis.suite.entries))
        elif basis.entry_patterns:
            # A suite selected per entry file: each entry runs when a changed file matches its own patterns.
            selected = []
            reasons = []
            for entry, patterns in basis.entry_patterns:
                match = first_match(patterns, changed)
                if match is None:
                    membership = next((value for value in dict(basis.entry_memberships).get(entry, ())
                                       if membership_changes[value] is not None), None)
                    if membership is not None:
                        match = (membership_changes[membership] or membership,
                                 f"directory membership `{membership}`")
                if match is None:
                    dependency = next((value for value in dict(basis.entry_existence_reads).get(entry, ())
                                       if existence_changes[value] is not None), None)
                    if dependency is not None:
                        match = (existence_changes[dependency], f"{dependency.kind} existence `{dependency.path}`")
                if match is None:
                    match = lookup_match(dict(basis.entry_lookup_directories).get(entry, ()))
                if match is not None:
                    read.add(match[0])
                    selected.append(entry)
                    reasons.append(f"`{match[0]}` matches `{match[1]}` of `{entry}`")
            if selected:
                decisions.append(Decision(basis.suite.name, True, reasons[0] + (f" and {len(selected) - 1} more" if len(selected) > 1 else ""), tuple(selected), per_entry=True))
            else:
                decisions.append(Decision(basis.suite.name, False, "no changed file matches the path list of any of its entry files", per_entry=True))
        else:
            match = first_match(basis.patterns or (), changed)
            if match is None:
                member = next(((membership, file) for membership in basis.directory_memberships
                               if (file := membership_changes[membership]) is not None), None)
                if member is not None:
                    match = (member[1], f"directory membership `{member[0]}`")
            if match is None:
                dependency = next((value for value in basis.existence_reads if existence_changes[value] is not None), None)
                if dependency is not None:
                    match = (existence_changes[dependency], f"{dependency.kind} existence `{dependency.path}`")
            if match is None:
                match = lookup_match(basis.lookup_directories)
            if match is None:
                decisions.append(Decision(basis.suite.name, False, "no changed file matches its path list"))
            else:
                decisions.append(Decision(basis.suite.name, True, f"`{match[0]}` matches `{match[1]}`", basis.suite.entries))
    by_name = {suite.name: suite for suite in suite_list.suites}
    if attributed is not None:
        # The calling workflow changed inside the steps of these suites alone: they run, and the workflow counts as read.
        workflow, affected = attributed
        if isinstance(affected, set):
            affected = ci_definition.Gate(frozenset(affected))
        read.add(workflow)
        for index, decision in enumerate(decisions):
            if decision.suite not in affected.suites:
                continue
            decision = decisions[index] = dataclasses.replace(decision, definition_reads=(workflow,))
            suite = by_name[decision.suite]
            restricted = dict(affected.entries).get(decision.suite)
            entries = (tuple(entry for entry in suite.entries if entry in set(decision.entries) | restricted)
                       if restricted is not None and suite.entry_selection else suite.entries)
            if not decision.run or set(entries) - set(decision.entries):
                decisions[index] = Decision(decision.suite, True, f"its step in `{workflow}` changed", entries,
                                             per_entry=suite.entry_selection and restricted is not None, definition_reads=(workflow,))
    for index, decision in enumerate(decisions):
        # A file that only steps gated on this suite name: the suite runs with every entry file, as its step does.
        for file, item in gated.get(decision.suite, ()):
            read.add(file)
            if file not in decision.definition_reads:
                decision = decisions[index] = dataclasses.replace(decision, definition_reads=decision.definition_reads + (file,))
            restricted = item.entries_for(decision.suite)
            entries = (tuple(entry for entry in by_name[decision.suite].entries if entry in set(decision.entries) | set(restricted))
                       if restricted is not None else by_name[decision.suite].entries)
            if not decision.run or set(entries) - set(decision.entries):
                decisions[index] = Decision(decision.suite, True, f"`{file}` belongs to the CI definition ({item.role}) and only steps gated on this suite name it", entries,
                                             per_entry=by_name[decision.suite].entry_selection and restricted is not None,
                                             definition_reads=decision.definition_reads)
                decision = decisions[index]
    if unrecorded_causes:
        from unrecorded import EntryFile
    for index, decision in enumerate(decisions if unrecorded_causes else ()):
        # A change to an input that no recording sees runs the units it shapes.
        suite = by_name[decision.suite]
        caused = [(entry, unrecorded_causes[EntryFile(suite.name, entry)]) for entry in suite.entries
                  if EntryFile(suite.name, entry) in unrecorded_causes]
        if not caused:
            continue
        read.update(cause.file for _, cause in caused)
        named = {entry for entry, _ in caused}
        entries = tuple(entry for entry in suite.entries if entry in set(decision.entries) | named) if suite.entry_selection else suite.entries
        if not decision.run or set(entries) - set(decision.entries):
            reason = caused[0][1].reason + (f" and {len(caused) - 1} more" if suite.entry_selection and len(caused) > 1 else "")
            decisions[index] = Decision(decision.suite, True, reason if not decision.run else f"{decision.reason}; {reason}", entries,
                                         per_entry=suite.entry_selection, definition_reads=decision.definition_reads)
    unread = tuple(file for file in changed if file not in read)
    return Selection(tuple(decisions), unread, None, tuple(sorted(started)))


def github_outputs(selection: Selection, suite_list: SuiteList | None = None) -> str:
    """One ``<suite>=run|skip`` line per suite, and for a suite selected per entry file ``<suite>-entries`` with the
    running entry files and ``<suite>-modules`` with the running Python entries as module names, space-separated."""
    per_entry = {suite.name for suite in (suite_list.suites if suite_list else ()) if suite.entry_selection}
    lines = []
    for decision in selection.decisions:
        lines.append(f"{decision.suite}={decision.result}\n")
        if decision.suite in per_entry:
            lines.append(f"{decision.suite}-entries={' '.join(decision.entries)}\n")
            lines.append(f"{decision.suite}-modules={' '.join(decision.modules)}\n")
    return "".join(lines)


def suites_json(selection: Selection) -> str:
    """One JSON object with ``run`` or ``skip`` for every suite, on one line."""
    return json.dumps({decision.suite: decision.result for decision in selection.decisions}, separators=(",", ":"))


def entries_json(selection: Selection, suite_list: SuiteList) -> str:
    """One JSON object with the running entry files of every suite selected per entry file, on one line."""
    per_entry = {suite.name for suite in suite_list.suites if suite.entry_selection}
    return json.dumps({decision.suite: list(decision.entries) for decision in selection.decisions if decision.suite in per_entry}, separators=(",", ":"))


def language_steps(names: list[str], selection: Selection, changed: list[str] | None) -> dict[str, str]:
    """``run`` or ``skip`` for each language step the caller calls, on the step's own patterns."""
    return {name: "run" if selection.undetermined is not None or changed is None or name in selection.started
            or first_match(LANGUAGE_STEPS[name], changed) else "skip" for name in names}


def two_worker_time(estimates: list[float]) -> float:
    """The estimated time of a shard that runs its entry files on two workers, each next file on the worker free first."""
    workers = [0.0] * SHARD_WORKERS
    for estimate in sorted(estimates, reverse=True):
        workers[workers.index(min(workers))] += estimate
    return max(workers)


def assign_shards(units: list[tuple[Suite, str, float]], bound: float = SHARD_BOUND) -> list[list[tuple[Suite, str, float]]]:
    """As few shards as keep each shard's two-worker time under the bound, filled greedily by estimate; an entry file
    over the bound gets a shard of its own."""
    ordered = sorted(units, key=lambda unit: -unit[2])
    for count in range(1, len(ordered) + 1):
        shards: list[list[tuple[Suite, str, float]]] = [[] for _ in range(count)]
        for unit in ordered:
            loads = [sum(item[2] for item in shard) for shard in shards]
            shards[loads.index(min(loads))].append(unit)
        if all(len(shard) == 1 or two_worker_time([item[2] for item in shard]) < bound for shard in shards):
            return shards
    return []


def shard_document(units: list[tuple[Suite, str, float]], estimate: float) -> dict:
    """The estimate, the setups and the entry files with their suite's command of one job."""
    setups: list[str] = []
    for suite, entry, _ in units:
        setups += [setup for setup in suite.setups_of(entry) if setup not in setups]
    return {"estimate": estimate, "setups": setups,
            "entries": [{"suite": suite.name, "entry": entry, "command": list(suite.command) if suite.command is not None else None}
                        for suite, entry, _ in units]}


def full_recording_scale(operating_system: str) -> float:
    """The traced seconds of an entry file in the full recording divided by its duration estimate: the wide-to-mid
    ratio on Linux, and 1 on Windows, which records under its one ETW session in every run."""
    return WIDE_TO_MID if operating_system == "linux" else 1.0


def selected_units(selection: Selection, suite_list: SuiteList, operating_system: str, scale: float = 1.0) -> list[tuple[Suite, str, float]]:
    """The selected entry files of one operating system with their estimates times ``scale``; an entry file without a
    stored estimate counts with the mean of the stored estimates of that operating system, or 0 s when the suite list
    stores none for it."""
    stored = [estimate for suite in suite_list.suites if suite.os == operating_system for _, estimate in suite.entry_durations]
    mean = sum(stored) / len(stored) if stored else 0
    by_name = {suite.name: suite for suite in suite_list.suites}
    units = []
    for decision in selection.decisions:
        suite = by_name[decision.suite]
        if decision.run and suite.os == operating_system:
            estimates = dict(suite.entry_durations)
            units += [(suite, entry, estimates.get(entry, mean) * scale if scale != 1.0 else estimates.get(entry, mean))
                      for entry in decision.entries]
    return units


def shards_json(selection: Selection, suite_list: SuiteList, steps: dict[str, str], full: bool = False) -> str:
    """The Linux shard matrix as a JSON list on one line; one shard without suites when only a language step runs. The
    full recording counts each estimate under the wide set and bounds each shard by ``FULL_SHARD_BOUND``."""
    units = selected_units(selection, suite_list, "linux", full_recording_scale("linux") if full else 1.0)
    shards = [shard_document(shard, two_worker_time([estimate for _, _, estimate in shard]))
              for shard in assign_shards(units, FULL_SHARD_BOUND if full else SHARD_BOUND)]
    if not shards and "run" in steps.values():
        shards = [shard_document([], 0.0)]
    return json.dumps([{"shard": number, **shard} for number, shard in enumerate(shards, start=1)], separators=(",", ":"))


def windows_json(selection: Selection, suite_list: SuiteList, full: bool = False) -> str:
    """The selected Windows entry files as one job's JSON object on one line, or empty when none is selected. The job
    runs its entry files one after another, so its estimate is the sum of their estimates."""
    units = selected_units(selection, suite_list, "windows", full_recording_scale("windows") if full else 1.0)
    return json.dumps(shard_document(units, sum(estimate for _, _, estimate in units)), separators=(",", ":")) if units else ""


# --- the push lookup and the reuse of passed results ------------------------------------------------------


def identical_selection(suite_list: SuiteList, reason: str) -> Selection:
    """The selection of a push whose tree equals the passed tested tree: no suite runs."""
    return Selection(tuple(Decision(suite.name, False, reason, per_entry=suite.entry_selection) for suite in suite_list.suites), (), None)


def both_sides(first: Selection, second: Selection) -> Selection:
    """The units that both selections run: a suite runs the entry files that both select."""
    decisions = []
    for one, other in zip(first.decisions, second.decisions):
        entries = tuple(entry for entry in one.entries if entry in set(other.entries)) if one.run and other.run else ()
        per_entry = one.per_entry or other.per_entry
        if entries:
            decisions.append(Decision(one.suite, True, f"both sides: {one.reason}; {other.reason}", entries, per_entry,
                                      tuple(dict.fromkeys(one.definition_reads + other.definition_reads))))
        else:
            decisions.append(Decision(one.suite, False, "the pull request and the base since its tested base do not both affect it", per_entry=per_entry))
    undetermined = first.undetermined if first.undetermined is not None and second.undetermined is not None else None
    return Selection(tuple(decisions), first.unread, undetermined)


def reuse_units(selection: Selection, suite_list: SuiteList, tree, versions: dict[str, str], labels: dict[str, str],
                shared_digest: str = "", *, ignore_content: dict, parts: Parts | None = None) -> list[tuple[str, Decision, tuple[str, ...]]]:
    """The key, the decision and the entry files of every running unit with a stored path list: a suite, or each entry
    file of a suite selected per entry file. The reads are the unit's patterns with the always-run patterns, its
    directory memberships and existence reads, and the files of the CI definition counted as read by its suite;
    ``shared_digest`` is the digest of the shared recording step, ``ignore_content`` the content of the ignore rules
    (``reuse.Tree.ignore_content``) and ``parts`` the unrecorded parts of the tested tree (``unrecorded.parts``), of
    which the key holds those of the unit."""
    import reuse
    import unrecorded

    parts = parts if parts is not None else unrecorded.Parts()

    by_basis = {basis.suite.name: basis for basis in bases(suite_list)}
    standard = standard_library_names()

    def lookups(directories: tuple[str, ...]) -> list[tuple[str, frozenset[str]]]:
        return [(directory, standard | frozenset(suite_list.lookups_in(directory))) for directory in directories]

    units = []
    for decision in selection.decisions:
        basis = by_basis[decision.suite]
        suite = basis.suite
        if not decision.run or basis.patterns is None:
            continue
        always = suite_list.always + decision.definition_reads
        if suite.entry_selection:
            for entry in decision.entries:
                reads = tree.reads(dict(basis.entry_patterns)[entry] + always, dict(basis.entry_memberships).get(entry, ()),
                                   dict(basis.entry_existence_reads).get(entry, ()), lookups(dict(basis.entry_lookup_directories).get(entry, ())))
                units.append((reuse.unit_key(reads, suite.command, suite.setups_of(entry), versions, labels.get(suite.os, ""),
                                           shared_digest, ignore_content=ignore_content,
                                           unrecorded=unrecorded.unit_content(parts, suite, (entry,))), decision, (entry,)))
        else:
            reads = tree.reads(basis.patterns + always, basis.directory_memberships, basis.existence_reads, lookups(basis.lookup_directories))
            setups = tuple(dict.fromkeys(setup for entry in decision.entries for setup in suite.setups_of(entry)))
            units.append((reuse.unit_key(reads, suite.command, setups, versions, labels.get(suite.os, ""), shared_digest,
                                          ignore_content=ignore_content, unrecorded=unrecorded.unit_content(parts, suite, decision.entries)),
                          decision, decision.entries))
    return units


def apply_reuse(selection: Selection, units: list[tuple[str, Decision, tuple[str, ...]]], record: dict[str, int]) -> tuple[Selection, dict, dict]:
    """The selection without the units whose key has a passed result, the running units by key, and the reused keys
    with the run each passed in."""
    reused = {key: record[key] for key, _, _ in units if key in record}
    running = {key: {"suite": decision.suite, "entries": list(entries)} for key, decision, entries in units if key not in record}
    dropped: dict[str, list[tuple[str, int]]] = {}
    for key, decision, entries in units:
        if key in reused:
            dropped.setdefault(decision.suite, []).extend((entry, reused[key]) for entry in entries)
    decisions = []
    for decision in selection.decisions:
        gone = dict(dropped.get(decision.suite, ()))
        if not gone:
            decisions.append(decision)
            continue
        runs = ", ".join(str(run) for run in sorted(set(gone.values())))
        remaining = tuple(entry for entry in decision.entries if entry not in gone)
        if remaining:
            decisions.append(dataclasses.replace(decision, reason=f"{decision.reason}; reuses the passed results of {len(gone)} entry files from run {runs}",
                                                 entries=remaining, per_entry=True))
        else:
            decisions.append(Decision(decision.suite, False, f"reuses the passed result of run {runs}", per_entry=decision.per_entry))
    return dataclasses.replace(selection, decisions=tuple(decisions)), running, reused


def _pairs(values: list[str], option: str) -> dict[str, str]:
    pairs = {}
    for value in values:
        name, separator, text = value.partition("=")
        if not separator or not name:
            raise SuiteListError(f"{option} {value} must take the form <name>=<value>")
        pairs[name] = text
    return pairs


def summary_markdown(selection: Selection, changed: list[str] | None) -> str:
    lines = ["## Suite selection", ""]
    if selection.undetermined is not None:
        lines.append(f"The changed files could not be determined: {selection.undetermined}. Every suite runs.")
    else:
        count = len(changed or [])
        lines.append(f"{count} changed file{'' if count == 1 else 's'}.")
    lines += ["", "| Suite | Result | Reason |", "| --- | --- | --- |"]
    for decision in selection.decisions:
        lines.append(f"| `{decision.suite}` | {decision.result} | {decision.reason}{entries_note(decision)} |")
    if selection.unread:
        lines += ["", "Read by no suite: " + ", ".join(f"`{file}`" for file in selection.unread) + "."]
    return "\n".join(lines) + "\n"


def text_report(selection: Selection, changed: list[str] | None) -> str:
    lines = []
    if selection.undetermined is not None:
        lines.append(f"Changed files could not be determined: {selection.undetermined}; every suite runs.")
    else:
        lines.append(f"Changed files: {', '.join(changed or []) or 'none'}")
    for decision in selection.decisions:
        lines.append(f"{decision.suite}: {decision.result} ({decision.reason}{entries_note(decision)})")
    if selection.unread:
        lines.append("Read by no suite: " + ", ".join(selection.unread))
    return "\n".join(lines) + "\n"


def entries_note(decision: Decision) -> str:
    """The running entry files of a suite selected per entry, for the reports; empty for a whole-suite decision."""
    if not decision.run or not decision.per_entry:
        return ""
    return "; entries: " + ", ".join(f"`{entry}`" for entry in decision.entries)


# --- the verdict ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    passed: bool
    reasons: tuple[str, ...]  # why the verdict fails; empty when it passes
    jobs: tuple[tuple[str, str], ...]  # every job of the needs context with its result
    suites: tuple[tuple[str, str], ...] | None  # the selection's decisions, or None when the output is missing
    note: str | None = None  # a remark for the summary, such as a missing selection output


def parse_needs(text: str) -> dict:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise VerdictError(f"the needs context is not valid JSON: {error}") from error
    if not isinstance(raw, dict):
        raise VerdictError("the needs context must be a JSON object with one entry per job")
    for name, job in raw.items():
        if not isinstance(job, dict) or not isinstance(job.get("result"), str):
            raise VerdictError(f"the needs context entry {name} must be an object with a string result")
    return raw


def verdict(needs: dict) -> Verdict:
    """Fail when ``select`` is not a success or any other job ended failure or cancelled; otherwise pass."""
    jobs = tuple((name, job["result"]) for name, job in needs.items())
    reasons: list[str] = []
    suites: tuple[tuple[str, str], ...] | None = None
    note: str | None = None
    selection_job = needs.get(SELECTION_JOB)
    if selection_job is None:
        reasons.append(f"the needs context has no `{SELECTION_JOB}` job")
    elif selection_job["result"] != "success":
        reasons.append(f"`{SELECTION_JOB}` ended {selection_job['result']}")
    if selection_job is not None:
        outputs = selection_job.get("outputs")
        raw = outputs.get("suites") if isinstance(outputs, dict) else None
        try:
            decoded = json.loads(raw) if isinstance(raw, str) and raw else None
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict) and all(isinstance(value, str) for value in decoded.values()):
            suites = tuple(decoded.items())
        else:
            note = f"the `{SELECTION_JOB}` job has no `suites` output to list"
    for name, result in jobs:
        if name != SELECTION_JOB and result in FAILING_RESULTS:
            reasons.append(f"`{name}` ended {result}")
    return Verdict(not reasons, tuple(reasons), jobs, suites, note)


def verdict_markdown(outcome: Verdict) -> str:
    lines = ["## Validation verdict", ""]
    if outcome.passed:
        lines.append("**Passed.** The selection succeeded and no called job ended failure or cancelled.")
    else:
        lines.append("**Failed.** " + "; ".join(outcome.reasons) + ".")
    if outcome.suites is not None:
        lines += ["", "| Suite | Decision |", "| --- | --- |"]
        lines += [f"| `{name}` | {decision} |" for name, decision in outcome.suites]
    elif outcome.note:
        lines += ["", outcome.note + "."]
    lines += ["", "| Job | Result |", "| --- | --- |"]
    lines += [f"| `{name}` | {result} |" for name, result in outcome.jobs]
    return "\n".join(lines) + "\n"


def verdict_text(outcome: Verdict) -> str:
    lines = ["Verdict: " + ("passed" if outcome.passed else "failed: " + "; ".join(outcome.reasons))]
    if outcome.suites is not None:
        lines += [f"{name}: {decision}" for name, decision in outcome.suites]
    elif outcome.note:
        lines.append(outcome.note)
    lines += [f"job {name}: {result}" for name, result in outcome.jobs]
    return "\n".join(lines) + "\n"


# --- the commands --------------------------------------------------------------------------------------------


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    selecting = commands.add_parser("select", help="decide run or skip per suite from the changed files")
    selecting.add_argument("--root", type=Path, required=True, help="the repository root")
    selecting.add_argument("--suites", type=Path, required=True, help="the suite list")
    selecting.add_argument("--changed-file", action="append", default=[], help="a changed file (repeatable)")
    selecting.add_argument("--changed-list", type=Path, help="a file with one changed path per line")
    selecting.add_argument("--base", help="the previous head of a push, or the base commit of a pull request")
    selecting.add_argument("--head", help="the pushed commit, or the head commit of a pull request")
    selecting.add_argument("--compare", choices=("merge-base", "direct"), default="direct",
                           help="merge-base for a pull request, direct for a push (default)")
    selecting.add_argument("--workflow", help="the calling workflow: its repository path, or github.workflow_ref")
    selecting.add_argument("--github-output", type=Path, help="append one <suite>=run|skip line per suite to this file")
    selecting.add_argument("--json-output", type=Path, help="append suites=<JSON object with run or skip per suite> to this file")
    selecting.add_argument("--summary", type=Path, help="append the Markdown summary to this file")
    selecting.add_argument("--language-step", action="append", default=[], choices=sorted(LANGUAGE_STEPS),
                           help="a language step the caller calls in its first Linux shard (repeatable)")
    selecting.add_argument("--full", action="store_true",
                           help="the full recording: every entry file, in shards bounded by their wide-traced time")
    selecting.add_argument("--lookup", type=Path, help="the push lookup of a push or dispatch run, from the lookup command")
    selecting.add_argument("--results", type=Path, help="the record of passed results from the pull request's cache; the file may be missing")
    selecting.add_argument("--run-attempt", type=int, default=1, help="github.run_attempt; above 1 no earlier result is reused")
    selecting.add_argument("--runner-label", action="append", default=[], help="<os>=<label> of the job that runs that system's units (repeatable)")
    selecting.add_argument("--setup-version", action="append", default=[], help="<setup>=<version> that a setup pins (repeatable)")
    selecting.add_argument("--shared-lookup", type=Path, help="the shared lookup from the shared-lookup command")
    looking = commands.add_parser("lookup", help="find the tested tree of a pushed commit's pull request")
    looking.add_argument("--root", type=Path, required=True)
    looking.add_argument("--sha", required=True, help="the pushed commit, or client_payload.sha of a dependabot-merged run")
    looking.add_argument("--before", default="", help="the previous head of the push; empty for a dispatch run")
    looking.add_argument("--workflow", required=True, help="the calling workflow: its repository path, or github.workflow_ref")
    looking.add_argument("--run-attempt", type=int, default=1, help="github.run_attempt")
    looking.add_argument("--output", type=Path, required=True, help="write the lookup as JSON to this file")
    looking.add_argument("--summary", type=Path, help="append the Markdown summary to this file")
    shared_parser = commands.add_parser("shared-lookup", help="find the digest of the shared recording step of the last passed run on the base branch")
    shared_parser.add_argument("--digest", required=True, help="the digest of the shared recording step that this run took")
    shared_parser.add_argument("--root", type=Path, required=True, help="the checkout whose first-parent history of the base branch the lookup walks")
    shared_parser.add_argument("--branch", required=True, help="the base branch: the pull request's base, or the pushed branch")
    shared_parser.add_argument("--pushed", default="", help="the pushed commit of a push or dispatch run, whose first parent starts the walk; "
                               "empty to start at the fetched tip origin/<branch>")
    shared_parser.add_argument("--workflow", required=True, help="the calling workflow: its repository path, or github.workflow_ref")
    shared_parser.add_argument("--output", type=Path, required=True, help="write the shared lookup as JSON to this file")
    shared_parser.add_argument("--summary", type=Path, help="append the Markdown summary to this file")
    storing = commands.add_parser("store", help="write the record of the passed results of a run")
    storing.add_argument("--needs", type=Path, required=True, help="a file with the closing job's needs context as JSON")
    storing.add_argument("--passed", type=Path, required=True, help="a directory with the passed entry files of each job")
    storing.add_argument("--run-id", type=int, required=True, help="github.run_id")
    storing.add_argument("--output", type=Path, required=True, help="write the record to this file")
    storing.add_argument("--github-output", type=Path, help="append stored=true or stored=false to this file")
    checking = commands.add_parser("check-dispatch", help="fail unless client_payload.sha of a dependabot-merged run is a commit on the default branch")
    checking.add_argument("--root", type=Path, required=True, help="the checkout, with the default branch fetched as origin/<branch>")
    checking.add_argument("--sha", required=True, help="client_payload.sha of the dependabot-merged run")
    checking.add_argument("--branch", required=True, help="the default branch")
    deciding = commands.add_parser("verdict", help="pass or fail on the results of the selection and the called jobs")
    deciding.add_argument("--needs", type=Path, required=True, help="a file with the caller's needs context as JSON")
    deciding.add_argument("--summary", type=Path, help="append the Markdown summary to this file")
    options = parser.parse_args(argv)
    if options.command == "select":
        from_list = bool(options.changed_file or options.changed_list)
        from_commits = options.base is not None or options.head is not None
        if options.full:
            if from_list or from_commits or options.language_step:
                parser.error("select --full takes no changed files, commits or language steps")
        elif options.lookup is not None:
            if from_list or from_commits or options.results is not None:
                parser.error("select takes --lookup without changed files, --base, --head or --results")
        elif from_list == from_commits or (from_commits and (options.base is None or options.head is None)):
            parser.error("select needs either changed files (--changed-file or --changed-list) or both --base and --head")
    return options


@contextlib.contextmanager
def record_errors():
    """The module ``reuse``, with a lookup, a record or a tree that cannot be read raised as an error of the selection."""
    import reuse

    try:
        yield reuse
    except reuse.RecordError as error:
        raise SuiteListError(str(error)) from error


def run_select(options: argparse.Namespace) -> int:
    root = options.root.resolve()
    tracked = tracked_files(root)
    suite_list = resolve_entries(load_suite_list(options.suites), tracked, str(options.suites))
    if options.full:
        return write_selection(options, suite_list, every_suite(suite_list, FULL_REASON), [], full=True)
    found = sides = None
    identical = False
    if options.lookup is not None:
        with record_errors() as reuse:
            found = reuse.read_lookup(options.lookup)
        if found.case == "identical":
            identical = True
            changed, selection = [], identical_selection(suite_list, f"the tree equals the tested tree that passed on pull request #{found.pull_request}")
        elif found.case == "behind":
            sides = [selection_between(options, root, tracked, suite_list, found.landing_base, found.head),
                     selection_between(options, root, tracked, suite_list, found.tested_base, found.landing_base)]
            changed, selection = sides[0][0], both_sides(sides[0][1], sides[1][1])
        else:
            changed, selection = selection_between(options, root, tracked, suite_list, found.landing_base or "", found.head)
    elif options.base is not None:
        changed, selection = selection_between(options, root, tracked, suite_list, options.base, options.head, options.compare)
    else:
        changed = [item.replace("\\", "/") for item in options.changed_file]
        if options.changed_list is not None:
            changed += [line.strip().replace("\\", "/") for line in options.changed_list.read_text(encoding="utf-8").splitlines() if line.strip()]
        changed = sorted(set(changed))
        selection = selected_on(options, root, tracked, suite_list, changed, None, None, None)
    shared_lookup = None
    if options.shared_lookup is not None:
        with record_errors() as reuse:
            shared_lookup = reuse.read_shared_lookup(options.shared_lookup)
        tested = reuse.tested_digest(found)
        if tested is not None:
            if tested != shared_lookup.digest:
                identical = False
                selection = every_suite(suite_list, f"every suite runs: {reuse.tested_reason(found, shared_lookup.digest)}")
        elif shared_lookup.moved:
            identical = False
            selection = every_suite(suite_list, f"every suite runs: {shared_lookup.reason}")
    running = reused = None
    if options.results is not None:
        labels, versions = _pairs(options.runner_label, "--runner-label"), _pairs(options.setup_version, "--setup-version")
        with record_errors() as reuse:
            tree = reuse.Tree(reuse.tree_entries(root), matches)
            units = reuse_units(selection, suite_list, tree, versions, labels, shared_lookup.digest if shared_lookup is not None else "",
                                ignore_content=tree.ignore_content(ignore_rules(root, "HEAD", tree.files)),
                                parts=tested_parts(options, root, tree.files, suite_list))
            record = reuse.read_record(options.results) if options.run_attempt <= 1 else {}
        selection, running, reused = apply_reuse(selection, units, record)
    steps = None
    if sides is not None:
        steps = {name: "run" if all(language_steps([name], side, files)[name] == "run" for files, side in sides) else "skip"
                 for name in options.language_step}
    write_selection(options, suite_list, selection, changed, steps=steps)
    if options.json_output is not None and running is not None:
        with options.json_output.open("a", encoding="utf-8") as handle:
            handle.write(f"units={json.dumps(running, separators=(',', ':'))}\n")
            handle.write(f"reused={json.dumps(reused, separators=(',', ':'))}\n")
    if options.summary is not None:
        with options.summary.open("a", encoding="utf-8") as handle:
            if found is not None:
                handle.write("\n" + reuse.lookup_markdown(found))
            if shared_lookup is not None:
                handle.write("\n" + reuse.shared_markdown(shared_lookup, found))
            if options.results is not None and options.run_attempt > 1:
                handle.write(f"\nAttempt {options.run_attempt} is a re-run: it reuses no earlier result.\n")
    # The last write: a step that fails before it leaves the output unset.
    if options.json_output is not None and identical:
        with options.json_output.open("a", encoding="utf-8") as handle:
            handle.write("identical=true\n")
    return 0


def selection_between(options: argparse.Namespace, root: Path, tracked, suite_list: SuiteList, base: str, head: str,
                      compare: str = "direct") -> tuple[list[str] | None, Selection]:
    """The changed files between two commits and the selection over them."""
    changed, undetermined = changed_files(root, base, head, compare)
    previous = previous_revision(root, base, head, compare)
    return changed, selected_on(options, root, tracked, suite_list, changed, undetermined, previous, head)


def selected_on(options: argparse.Namespace, root: Path, tracked, suite_list: SuiteList, changed: list[str] | None,
                undetermined: str | None, previous: str | None, head: str | None) -> Selection:
    """The selection over changed files, with the CI definition of the calling workflow when the caller names it."""
    definition = None
    workflow_steps = None
    if options.workflow and changed is not None:
        try:
            definition = ci_definition.ci_definition(root, workflow_path(options.workflow), list_path(options, root), tracked)
        except ci_definition.WorkflowReadError as error:
            raise SuiteListError(f"the CI definition could not be read: {error}") from error
        workflow = definition[0].pattern if definition else None
        if workflow is not None and workflow in changed and previous is not None:
            # The calling workflow changed: when only steps of particular suites differ between the two commits, those
            # suites run instead of every suite.
            before, after = revision_text(root, previous, workflow), revision_text(root, head, workflow)
            if before is not None and after is not None:
                gate = ci_definition.changed_step_gate(before, after)
                if gate is not None:
                    workflow_steps = (workflow, gate)
    previous_tracked = revision_files(root, previous) if previous is not None and changed is not None else None
    current_tracked = revision_files(root, head) if previous_tracked is not None else frozenset(tracked)
    ignore = ignore_rules(root, head or "HEAD", current_tracked) if changed is not None else None
    if changed is not None and previous_tracked is None:
        # A list of changed files is compared with HEAD: an edited tracked .gitignore file is a change to it as well.
        changed = sorted(set(changed) | set(edited_ignore_files(root, current_tracked)))
    causes, started = unrecorded_changes(options, root, suite_list, changed, previous, head if previous_tracked is not None else None,
                                         previous_tracked, current_tracked)
    relative = list_path(options, root)
    estimates_only = changed is not None and relative in changed and estimates_only_change(
        root, relative, previous, head if previous_tracked is not None else None)
    selection = select(suite_list, [file for file in changed or () if file != relative] if estimates_only else changed, undetermined,
                       root=root, definition=definition, workflow_steps=workflow_steps, previous_tracked=previous_tracked,
                       current_tracked=current_tracked, ignore=ignore, unrecorded_causes=causes, started=started)
    if estimates_only:
        # The suite list was left out of the path match, so no suite read it.
        selection = dataclasses.replace(selection, unread=tuple(file for file in changed or () if file in selection.unread or file == relative))
    return selection


def estimates_only_change(root: Path, relative: str, previous: str | None, head: str | None) -> bool:
    """Whether the change of the suite list at ``relative`` differs only in its duration estimates, which makes it no
    changed file for the path match. The sides are those that the unrecorded inputs compare: the previous commit, or
    the checkout's ``HEAD``, against the head commit or, without one, the working tree. A working tree that differs from
    ``HEAD`` at most in line endings, such as a list of changed files against a checkout that already holds them, shows
    no difference, so the change counts as changed."""
    import unrecorded

    before = unrecorded.Side(root, previous or "HEAD", frozenset()).text(relative)
    after = unrecorded.Side(root, head, frozenset()).text(relative)
    if head is None and before is not None and after is not None and before.replace("\r\n", "\n") == after.replace("\r\n", "\n"):
        return False
    return differs_in_estimates_only(before, after)


def differs_in_estimates_only(before: str | None, after: str | None) -> bool:
    """Whether two texts of the suite list differ only in ``entry_durations``: they differ, both parse as a suite list,
    and their parsed content without the duration estimates is equal. A text that is absent or does not parse, or two
    equal texts, count as a change: a changed file that the comparison sees no difference in may differ in anything."""
    if before is None or after is None or before == after:
        return False
    try:
        parse_suite_list(before)
        parse_suite_list(after)
    except SuiteListError:
        return False

    def without_estimates(text: str) -> dict:
        raw = json.loads(text)
        return {**raw, "suites": [{key: value for key, value in suite.items() if key != "entry_durations"} for suite in raw["suites"]]}
    return without_estimates(before) == without_estimates(after)


def list_path(options: argparse.Namespace, root: Path) -> str:
    """The suite list's repository path."""
    resolved = options.suites.resolve()
    return resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else str(options.suites)


def unrecorded_changes(options: argparse.Namespace, root: Path, suite_list: SuiteList, changed: list[str] | None,
                       previous: str | None, head: str | None, previous_tracked: frozenset[str] | None,
                       current_tracked: frozenset[str]) -> tuple[dict[EntryFile, Cause], frozenset[str]]:
    """The entry files whose unrecorded inputs the change alters, with their causes, and the language steps it starts.
    The previous side is the commit the changed files are compared from, or for a list of changed files the checkout's
    ``HEAD``, compared with the working tree; without either nothing is compared."""
    import unrecorded

    if changed is None:
        return {}, frozenset()
    if previous is None:
        if commit_id(root, "HEAD") is None:
            return {}, frozenset()
        previous, previous_tracked = "HEAD", revision_files(root, "HEAD")
    before = unrecorded.Side(root, previous, previous_tracked if previous_tracked is not None else revision_files(root, previous))
    now = unrecorded.Side(root, head, current_tracked)
    relative = list_path(options, root)
    causes: dict[EntryFile, Cause] = {}
    previous_list = suite_list
    if relative in changed:
        try:
            previous_list = resolve_entries(parse_suite_list(before.text(relative) or "", relative), set(before.tracked), relative)
        except SuiteListError:
            previous_list = SuiteList((), ())
            causes = {unit.file: unrecorded.Cause(relative, f"`{relative}` could not be read at {previous}") for unit in unrecorded.entry_units(suite_list)}
        for entry_file, reason in unrecorded.suite_list_changes(previous_list, suite_list, relative).items():
            causes.setdefault(entry_file, unrecorded.Cause(relative, reason))
    started: set[str] = set()
    if options.workflow:
        workflow = calling_workflow(options)
        selected, started = unrecorded.changes(unrecorded.parts(before, workflow, unrecorded.entry_units(previous_list)),
                                               unrecorded.parts(now, workflow, unrecorded.entry_units(suite_list)), set(changed))
        for entry_file, cause in selected.items():
            causes.setdefault(entry_file, cause)
    return causes, frozenset(started)


def calling_workflow(options: argparse.Namespace) -> str:
    """The repository path of the calling workflow that ``--workflow`` names."""
    return ci_definition.repository_path(workflow_path(options.workflow)) or workflow_path(options.workflow)


def tested_parts(options: argparse.Namespace, root: Path, tracked: frozenset[str], suite_list: SuiteList) -> Parts:
    """The unrecorded parts of the tested tree, the checkout's ``HEAD``, for the reuse key."""
    import unrecorded

    if not options.workflow:
        return unrecorded.Parts()
    return unrecorded.parts(unrecorded.Side(root, "HEAD", tracked), calling_workflow(options), unrecorded.entry_units(suite_list))


def write_selection(options: argparse.Namespace, suite_list: SuiteList, selection: Selection, changed: list[str] | None,
                    full: bool = False, steps: dict[str, str] | None = None) -> int:
    sys.stdout.write(text_report(selection, changed))
    if options.github_output is not None:
        with options.github_output.open("a", encoding="utf-8") as handle:
            handle.write(github_outputs(selection, suite_list))
    if options.json_output is not None:
        with options.json_output.open("a", encoding="utf-8") as handle:
            handle.write(f"suites={suites_json(selection)}\n")
            if any(suite.entry_selection for suite in suite_list.suites):
                handle.write(f"entries={entries_json(selection, suite_list)}\n")
            if steps is None:
                steps = language_steps(options.language_step, selection, changed)
            if options.language_step:
                handle.write(f"language-steps={json.dumps(steps, separators=(',', ':'))}\n")
            handle.write(f"shards={shards_json(selection, suite_list, steps, full)}\n")
            handle.write(f"windows={windows_json(selection, suite_list, full)}\n")
    if options.summary is not None:
        with options.summary.open("a", encoding="utf-8") as handle:
            handle.write(summary_markdown(selection, changed))
    return 0


def run_lookup(options: argparse.Namespace) -> int:
    import reuse

    found = reuse.lookup(options.root.resolve(), reuse.environment_api(), Path(workflow_path(options.workflow)).name, options.sha,
                         options.before, options.run_attempt)
    options.output.write_text(json.dumps(found.document(), indent=2) + "\n", encoding="utf-8")
    print(f"{found.case}: {found.reason}")
    if options.summary is not None:
        with options.summary.open("a", encoding="utf-8") as handle:
            handle.write(reuse.lookup_markdown(found))
    return 0


def run_shared_lookup(options: argparse.Namespace) -> int:
    import reuse

    found = reuse.shared_lookup(options.root.resolve(), reuse.environment_api(), Path(workflow_path(options.workflow)).name, options.branch,
                                options.digest, options.pushed or None)
    options.output.write_text(json.dumps(found.document(), indent=2) + "\n", encoding="utf-8")
    for line in found.log:
        print(line)
    print(f"{'moved' if found.moved else 'unchanged'}: {found.reason}")
    if options.summary is not None:
        with options.summary.open("a", encoding="utf-8") as handle:
            handle.write(reuse.shared_markdown(found))
    return 0


def run_store(options: argparse.Namespace) -> int:
    import reuse

    try:
        record = reuse.store(parse_needs(options.needs.read_text(encoding="utf-8")), reuse.passed_entries(options.passed), options.run_id)
    except reuse.RecordError as error:
        raise SuiteListError(str(error)) from error
    if record is None:
        print("Nothing to store: the selection did not succeed.")
    else:
        options.output.parent.mkdir(parents=True, exist_ok=True)
        options.output.write_text(json.dumps({"passed": record}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Stored {len(record)} passed result{'' if len(record) == 1 else 's'}.")
    if options.github_output is not None:
        with options.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"stored={'true' if record is not None else 'false'}\n")
    return 0


FULL_COMMIT_ID = re.compile(r"[0-9a-f]{40}")


def dispatch_problem(root: Path, payload: str, branch: str) -> str | None:
    """Why the payload commit of a dispatch run is not a commit on the fetched default branch, or None when it is."""
    if not FULL_COMMIT_ID.fullmatch(payload):
        return f"client_payload.sha {payload!r} is malformed: it is not a full 40-character commit ID in lowercase hexadecimal."
    if commit_id(root, payload) != payload:
        return f"client_payload.sha {payload} is absent from this job's checkout: it names no commit there."
    tip = f"refs/remotes/origin/{branch}"
    if commit_id(root, tip) is None:
        return f"client_payload.sha {payload} cannot be checked: `origin/{branch}`, the default branch, is not in this job's checkout."
    reached = git(root, "merge-base", "--is-ancestor", payload, tip)
    if reached.returncode == 1:
        return f"client_payload.sha {payload} is not on the default branch: `origin/{branch}` does not reach it."
    if reached.returncode != 0:
        return f"client_payload.sha {payload} cannot be checked: git merge-base failed: {reached.stderr.strip()}"
    return None


def run_check_dispatch(options: argparse.Namespace) -> int:
    problem = dispatch_problem(options.root.resolve(), options.sha, options.branch)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 1
    print(f"client_payload.sha {options.sha} is on `origin/{options.branch}`, the default branch.")
    return 0


def run_verdict(options: argparse.Namespace) -> int:
    outcome = verdict(parse_needs(options.needs.read_text(encoding="utf-8")))
    sys.stdout.write(verdict_text(outcome))
    if options.summary is not None:
        with options.summary.open("a", encoding="utf-8") as handle:
            handle.write(verdict_markdown(outcome))
    return 0 if outcome.passed else 1


def main(argv: list[str] | None = None) -> int:
    options = parse_arguments(sys.argv[1:] if argv is None else argv)
    try:
        if options.command == "select":
            return run_select(options)
        if options.command == "verdict":
            return run_verdict(options)
        if options.command == "lookup":
            return run_lookup(options)
        if options.command == "shared-lookup":
            return run_shared_lookup(options)
        if options.command == "check-dispatch":
            return run_check_dispatch(options)
        return run_store(options)
    except (SuiteListError, VerdictError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
