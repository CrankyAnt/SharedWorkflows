"""The suite list model, parser, renderer, pattern matcher and ignore rules shared by selection and recording.

This module holds only the data structures, suite-list I/O, glob matching, standard-library lookup names and
``.gitignore`` rules that both ``recording.py`` and ``suite_selection.py`` use,
without the selection, sharding or lookup logic. Changes to ``suite_selection.py`` therefore do not enter the
recording step's loaded modules or digests.
"""

from __future__ import annotations

import dataclasses
import functools
import json
import math
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
GLOB_CHARACTERS = ("*", "?")
OPERATING_SYSTEMS = ("linux", "windows")
# Scale version of stored entry_durations: 1 was Popen..wait with Linux /1.65; 2 is full process wall time at scale 1.
ENTRY_DURATIONS_VERSION = 2
WHOLE_TREE_KINDS = ("content", "membership")
SUITE_KEYS = frozenset({"name", "entries", "paths", "extra_paths", "entry_selection", "entry_paths",
                        "entry_extra_paths", "directory_memberships", "entry_directory_memberships", "existence_reads", "entry_existence_reads",
                        "extra_directory_memberships", "extra_existence_reads", "lookup_directories", "entry_lookup_directories",
                        "module_lookups", "entry_module_lookups", "whole_tree_reasons", "entry_whole_tree_reasons",
                        "os", "command", "setups", "entry_setups", "entry_durations"})
# The per-entry fields that the recording writes; the repository writes entry_extra_paths and entry_setups.
RECORDED_ENTRY_FIELDS = ("entry_paths", "entry_directory_memberships", "entry_existence_reads", "entry_lookup_directories",
                         "entry_module_lookups", "entry_durations")
TOP_LEVEL_KEYS = ("always", "entry_durations_version", "jobs", "setups", "suites", "module_lookups")
TOOLS = ("python", "node", "dotnet")  # the tools that a suite job installs with its pinned action, outside the recording
# The pinned tools: the setups that the suite jobs give at the version of the pins file when the configuration file names
# them under pinned-tools of the suite selection. A suite list defines no setup of such a name.
PINNED_TOOLS = ("node", "gitleaks")
SCRIPT_SUFFIXES = (".py", ".sh", ".ps1")  # a setup script runs on the job's Python, in Bash or in PowerShell
SETUP_KEYS = frozenset({"script", "recorded", "tool", "version", "version-file"})
JOB_DEFAULTS = {"linux": {"runner": "ubuntu-26.04", "fetch-depth": 1}, "windows": {"runner": "windows-2025", "fetch-depth": 1}}
VERSION_FILE = re.compile(r"^[^#]+#(/[^/]+)+$")
# A label of one GitHub-hosted runner, as the definition runner-label of the pipeline's schema has it: no suite job runs on a self-hosted runner.
RUNNER_LABEL = re.compile(r"^(ubuntu|windows|macos)-[A-Za-z0-9][A-Za-z0-9.-]*$")
# The union of sys.stdlib_module_names over the Python versions that the shared CI supports.
STANDARD_LIBRARY = Path(__file__).resolve().parent.parent / "recording" / "stdlib_module_names.json"


class SuiteListError(Exception):
    """The suite list is invalid; the message names the entry and the field."""


@dataclass(frozen=True)
class ExistenceRead:
    """A path whose presence and type, not its contents, a suite reads."""

    path: str
    kind: str

    def entry(self) -> dict:
        return {"path": self.path, "kind": self.kind}

    def path_on(self, system: str) -> str:
        """The repository path that the read names in a suite of the operating system ``system``. On Windows a backslash
        separates directories, as a slash does; on Linux it is a character of a name, as a recording writes it."""
        path = self.path.replace("\\", "/") if system == "windows" else self.path
        return path.strip("/")


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
    module_lookups: tuple[tuple[str, tuple[str, ...]], ...] | None = None  # per-suite module lookups by directory; None falls back to top-level
    entry_module_lookups: tuple[tuple[str, tuple[tuple[str, tuple[str, ...]], ...]], ...] = ()  # per-entry module lookups by directory
    whole_tree_reasons: tuple[tuple[str, str], ...] = ()  # per kind (content, membership), why a whole-suite unit reads **
    entry_whole_tree_reasons: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = ()  # per entry file and kind, why it reads **
    command: tuple[str, ...] | None = None  # the command that runs one entry file, with the entry file appended
    setups: tuple[str, ...] | None = None  # the setups the suite needs, or None when it states none
    entry_setups: tuple[tuple[str, tuple[str, ...]], ...] = ()  # further setups per entry file of a suite selected per entry
    entry_durations: tuple[tuple[str, float], ...] = ()  # the duration estimate per entry file, in seconds of traced test time
    os: str = "linux"  # the operating system its entry files run on: linux or windows

    @functools.cached_property
    def _entries_with_list(self) -> frozenset[str]:
        return frozenset(entry for entry, _ in self.entry_paths)

    def lacks_list(self, entry: str) -> bool:
        """Whether an entry file of a stored suite selected per entry file has no stored list of its own: an entry file,
        named by path or matched by a pattern of ``entries``, whose reads no recording has stored yet. Its inputs are
        unknown."""
        return self.entry_selection and self.paths is not None and entry not in self._entries_with_list

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
        if self.module_lookups is not None:
            item["module_lookups"] = {directory: list(names) for directory, names in self.module_lookups}
        if self.entry_module_lookups:
            item["entry_module_lookups"] = {entry: {directory: list(names) for directory, names in lookups}
                                            for entry, lookups in self.entry_module_lookups}
        if self.whole_tree_reasons:
            item["whole_tree_reasons"] = dict(self.whole_tree_reasons)
        if self.entry_whole_tree_reasons:
            item["entry_whole_tree_reasons"] = {entry: dict(reasons) for entry, reasons in self.entry_whole_tree_reasons}
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

    def own_module_lookups(self, entry: str | None) -> dict[str, tuple[str, ...]] | None:
        """The unit's own stored module lookups by directory, or None when the suite list has none for this unit."""
        own = self._own(entry)
        if own is None:
            return dict(self.module_lookups) if self.module_lookups is not None else None
        per_entry = dict(self.entry_module_lookups)
        return dict(per_entry[own]) if own in per_entry else None

    def per_entry[T](self, lists_of: Callable[[str], T]) -> tuple[tuple[str, T], ...]:
        """The lists of each entry file of a suite selected per entry file; none for a suite selected as a whole."""
        return tuple((entry, lists_of(entry)) for entry in self.entries) if self.entry_selection else ()

    def whole_tree_reason(self, entry: str | None, kind: str) -> str | None:
        """The recorded reason why this unit of selection reads ``**`` of ``kind`` (``content`` or ``membership``), or None."""
        own = self._own(entry)
        if own is None:
            return dict(self.whole_tree_reasons).get(kind)
        return dict(dict(self.entry_whole_tree_reasons).get(own, ())).get(kind)

    def unreasoned_whole_tree(self) -> list[tuple[str | None, str]]:
        """Every ``(entry_or_none, kind)`` unit of selection that stores a bare ``**`` without a recorded reason."""
        findings: list[tuple[str | None, str]] = []
        units: tuple[str | None, ...] = self.entries if self.entry_selection else (None,)
        for unit in units:
            if any(pattern.strip("/") == "**" for pattern in self.patterns_of(unit)) and self.whole_tree_reason(unit, "content") is None:
                findings.append((unit, "content"))
            if any(pattern.strip("/") == "**" for pattern in self.memberships_of(unit)) and self.whole_tree_reason(unit, "membership") is None:
                findings.append((unit, "membership"))
        return findings


def _with_extra[T](own: tuple[T, ...], extra: tuple[T, ...]) -> tuple[T, ...]:
    """The stored items followed by the extra items that they lack."""
    return own + tuple(item for item in extra if item not in own)


@dataclass(frozen=True)
class Setup:
    """A setup as the suite list states it: its one script, whether the script runs under the recording, and the tool
    that the suite job installs for it with a pinned action before the recording starts."""

    name: str
    script: str
    recorded: bool = True  # False: the script runs before the recording starts, outside it
    tool: str | None = None
    version: str | None = None
    version_file: str | None = None  # <JSON file>#/<key>/<key>: the tracked file and the place in it that hold the version

    def entry(self) -> dict:
        """The setup's JSON entry as the suite list states it."""
        item: dict = {"script": self.script}
        if not self.recorded:
            item["recorded"] = False
        if self.tool is not None:
            item["tool"] = self.tool
        if self.version is not None:
            item["version"] = self.version
        if self.version_file is not None:
            item["version-file"] = self.version_file
        return item

    @property
    def files(self) -> tuple[str, ...]:
        """The tracked files that decide what the setup installs: its script and the file that holds its tool's version."""
        return (self.script,) + ((self.version_file.split("#", 1)[0],) if self.version_file is not None else ())


@dataclass(frozen=True)
class PinnedTool:
    """A pinned tool that the configuration file names: its version of the pins file, what its pin holds as text, and
    the repository-relative path of the pins file when it lives in the checkout."""

    name: str
    version: str
    pin: str
    file: str | None = None


@dataclass(frozen=True)
class SuiteList:
    always: tuple[str, ...]
    suites: tuple[Suite, ...]
    module_lookups: tuple[tuple[str, tuple[str, ...]], ...] = ()  # per checkout directory, the module lookups of every entry file
    setups: tuple[Setup, ...] | None = None  # the setups as data, in the order they run; None when the suite list states none
    jobs: tuple[tuple[str, tuple[tuple[str, object], ...]], ...] = ()  # per operating system, what the suite list states of its job
    pinned: tuple[PinnedTool, ...] = ()  # the pinned tools that the configuration file names
    entry_durations_version: int = 1  # scale version of stored entry_durations; omitted in v1 suite lists

    @functools.cached_property
    def setups_by_name(self) -> dict[str, Setup]:
        return {setup.name: setup for setup in self.setups or ()}

    def job(self, operating_system: str) -> dict:
        """The runner label and the fetch depth of the checkout of one system's suite job, with the defaults of what the
        suite list leaves out."""
        return JOB_DEFAULTS[operating_system] | dict(dict(self.jobs).get(operating_system, ()))

    def setup_files(self, suite: Suite, entry: str | None) -> tuple[str, ...]:
        """The files of the setups that an entry file states, or with None of those that any entry file of the suite
        states: their content decides what the entry file runs with, so the entry file reads them."""
        entries = suite.entries if entry is None or not suite.entry_selection else (entry,)
        names = dict.fromkeys(setup for item in entries for setup in suite.setups_of(item))
        pinned_files = {tool.name: tool.file for tool in self.pinned if tool.file is not None}
        files = [file for name in names if name in self.setups_by_name for file in self.setups_by_name[name].files]
        files += [pinned_files[name] for name in names if name in pinned_files]
        return tuple(dict.fromkeys(files))

    @functools.cached_property
    def lookups_by_directory(self) -> dict[str, tuple[str, ...]]:
        return dict(self.module_lookups)

    def lookups_in(self, directory: str) -> tuple[str, ...]:
        return self.lookups_by_directory.get(directory, ())

    def unit_lookups_in(self, suite: Suite, entry: str | None, directory: str) -> tuple[str, ...]:
        """The module-lookup names of one unit in ``directory``: the unit's own stored names when present, otherwise
        the suite list's top-level ``module_lookups`` for ``directory``."""
        own = suite.own_module_lookups(entry)
        if own is not None:
            return own.get(directory, ())
        return self.lookups_in(directory)

    def unit_lookups_map(self, suite: Suite, entry: str | None) -> dict[str, tuple[str, ...]]:
        """The module-lookup map for one unit: its own stored map when present, otherwise the top-level map."""
        own = suite.own_module_lookups(entry)
        return own if own is not None else self.lookups_by_directory


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
    return tuple((entry, _existence_reads(value[entry], f"{location} ({entry})")) for entry in _entry_keys(value, entries, location, recorded=True))


def _command(value, location: str) -> tuple[str, ...]:
    command = _strings(value, location)
    if not command:
        raise SuiteListError(f"{location} must name at least the program")
    return command


def _duration(value, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise SuiteListError(f"{location} must be a number of seconds, zero or more")
    return value


def _whole_tree_reasons(value, location: str) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, dict) or not value:
        raise SuiteListError(f"{location} must be a non-empty object mapping content or membership to a non-empty reason")
    unknown = sorted(set(value) - set(WHOLE_TREE_KINDS))
    if unknown:
        raise SuiteListError(f"{location}: unknown keys {', '.join(unknown)}; expected content or membership")
    reasons: list[tuple[str, str]] = []
    for kind in WHOLE_TREE_KINDS:
        if kind in value:
            text = value[kind]
            if not isinstance(text, str) or not text.strip():
                raise SuiteListError(f"{location} ({kind}): reason must be a non-empty string")
            reasons.append((kind, text))
    return tuple(reasons)


def glob_entries(entries) -> tuple[str, ...]:
    """The entries that are glob patterns."""
    return tuple(entry for entry in entries if any(character in entry for character in GLOB_CHARACTERS))


def _entry_keys(value: dict, entries: tuple[str, ...], location: str, *, recorded: bool) -> tuple[str, ...]:
    """The keys of a per-entry field, checked against the entries. A key names an entry or a file that a glob entry
    matches. A field that the recording writes (``recorded``) may also hold a key that names no entry file any more, of a
    deleted or renamed file, in any suite: the next apply removes it. The keys of a suite without a glob entry come in
    entry order, followed by such keys in the order the field states them; those of a suite with one come in the order
    the field states them."""
    patterns = glob_entries(entries)
    unknown = [key for key in value if key not in entries and not any(matches(pattern, key) for pattern in patterns)]
    if unknown and not recorded:
        raise SuiteListError(f"{location} names {', '.join(sorted(unknown))}, which the entries lack")
    return tuple(value) if patterns else tuple(entry for entry in entries if entry in value) + tuple(unknown)


def _per_entry_file(value, entries: tuple[str, ...], location: str, read, *, recorded: bool) -> tuple[tuple[str, object], ...]:
    """A mapping from entry file to a value, in key order; ``_entry_keys`` states which keys it may hold."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SuiteListError(f"{location} must be an object with one value per entry file")
    _entry_keys(value, entries, location, recorded=recorded)
    return tuple((key, read(item, f"{location} ({key})")) for key, item in value.items())


def parse_suite_list(text: str, source: str = "the suite list") -> SuiteList:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise SuiteListError(f"{source} is not valid JSON: {error}") from error
    return parse_suite_list_document(raw, source)


def parse_suite_list_document(raw: object, source: str = "the suite list") -> SuiteList:
    if not isinstance(raw, dict) or not {"always", "suites"} <= set(raw) <= set(TOP_LEVEL_KEYS):
        raise SuiteListError(f"{source} must be an object with the keys always and suites, and optionally entry_durations_version, jobs, setups and module_lookups")
    durations_version = raw.get("entry_durations_version", 1)
    if not isinstance(durations_version, int) or isinstance(durations_version, bool) or durations_version < 1:
        raise SuiteListError(f"{source}: entry_durations_version must be a positive whole number")
    job_data = _jobs(raw["jobs"], f"{source}: jobs") if "jobs" in raw else ()
    setup_data = _setups(raw["setups"], f"{source}: setups") if "setups" in raw else None
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
        entry_paths = (_per_entry(item.get("entry_paths"), entries, f"{location} ({name}): entry_paths", recorded=True)
                       if "entry_paths" in item else ())
        entry_extra_paths = (_per_entry(item.get("entry_extra_paths"), entries, f"{location} ({name}): entry_extra_paths", recorded=False)
                             if "entry_extra_paths" in item else ())
        memberships = _strings(item["directory_memberships"], f"{location} ({name}): directory_memberships") if "directory_memberships" in item else ()
        entry_memberships = _per_entry(item["entry_directory_memberships"], entries, f"{location} ({name}): entry_directory_memberships", recorded=True) if "entry_directory_memberships" in item else ()
        existence = _existence_reads(item["existence_reads"], f"{location} ({name}): existence_reads") if "existence_reads" in item else ()
        entry_existence = _entry_existence_reads(item["entry_existence_reads"], entries, f"{location} ({name}): entry_existence_reads") if "entry_existence_reads" in item else ()
        extra_memberships = _membership_patterns(item["extra_directory_memberships"], f"{location} ({name}): extra_directory_memberships") if "extra_directory_memberships" in item else ()
        extra_existence = _existence_reads(item["extra_existence_reads"], f"{location} ({name}): extra_existence_reads") if "extra_existence_reads" in item else ()
        lookup_directories = _strings(item["lookup_directories"], f"{location} ({name}): lookup_directories") if "lookup_directories" in item else ()
        entry_lookup_directories = (_per_entry(item["entry_lookup_directories"], entries, f"{location} ({name}): entry_lookup_directories", recorded=True)
                                    if "entry_lookup_directories" in item else ())
        suite_module_lookups = (_module_lookups(item["module_lookups"], f"{location} ({name}): module_lookups")
                                if "module_lookups" in item else None)
        entry_module_lookups = (_per_entry_file(item["entry_module_lookups"], entries, f"{location} ({name}): entry_module_lookups",
                                                _module_lookups, recorded=True)
                                if "entry_module_lookups" in item else ())
        whole_tree_reasons = (_whole_tree_reasons(item["whole_tree_reasons"], f"{location} ({name}): whole_tree_reasons")
                              if "whole_tree_reasons" in item else ())
        entry_whole_tree_reasons = (_per_entry_file(item["entry_whole_tree_reasons"], entries, f"{location} ({name}): entry_whole_tree_reasons",
                                                    _whole_tree_reasons, recorded=False)
                                    if "entry_whole_tree_reasons" in item else ())
        operating_system = item.get("os", "linux")
        if operating_system not in OPERATING_SYSTEMS:
            raise SuiteListError(f"{location} ({name}): os must be one of {', '.join(OPERATING_SYSTEMS)}")
        command = _command(item["command"], f"{location} ({name}): command") if "command" in item else None
        setups = _strings(item["setups"], f"{location} ({name}): setups") if "setups" in item else None
        entry_setups = _per_entry_file(item["entry_setups"], entries, f"{location} ({name}): entry_setups", _strings, recorded=False) if "entry_setups" in item else ()
        entry_durations = _per_entry_file(item["entry_durations"], entries, f"{location} ({name}): entry_durations", _duration, recorded=True) if "entry_durations" in item else ()
        if entry_existence and not entry_selection:
            raise SuiteListError(f"{location} ({name}): entry_existence_reads needs entry_selection")
        if entry_memberships and not entry_selection:
            raise SuiteListError(f"{location} ({name}): entry_directory_memberships needs entry_selection")
        if entry_lookup_directories and not entry_selection:
            raise SuiteListError(f"{location} ({name}): entry_lookup_directories needs entry_selection")
        if entry_module_lookups and not entry_selection:
            raise SuiteListError(f"{location} ({name}): entry_module_lookups needs entry_selection")
        if entry_paths and not entry_selection:
            raise SuiteListError(f"{location} ({name}): entry_paths needs entry_selection")
        if entry_setups and not entry_selection:
            raise SuiteListError(f"{location} ({name}): entry_setups needs entry_selection")
        if entry_extra_paths and not entry_selection:
            raise SuiteListError(f"{location} ({name}): entry_extra_paths needs entry_selection")
        if entry_whole_tree_reasons and not entry_selection:
            raise SuiteListError(f"{location} ({name}): entry_whole_tree_reasons needs entry_selection")
        if whole_tree_reasons and entry_selection:
            raise SuiteListError(f"{location} ({name}): whole_tree_reasons goes with a suite selected as a whole; use entry_whole_tree_reasons")
        if has_paths and entry_selection and "entry_paths" not in item:
            raise SuiteListError(f"{location} ({name}): a stored suite with entry_selection needs entry_paths")
        common = dict(extra_paths=extra_paths, entry_selection=entry_selection, entry_extra_paths=entry_extra_paths,
                      directory_memberships=memberships, entry_directory_memberships=entry_memberships, existence_reads=existence,
                      entry_existence_reads=entry_existence, extra_directory_memberships=extra_memberships,
                      extra_existence_reads=extra_existence, lookup_directories=lookup_directories,
                      entry_lookup_directories=entry_lookup_directories, module_lookups=suite_module_lookups,
                      entry_module_lookups=entry_module_lookups, whole_tree_reasons=whole_tree_reasons,
                      entry_whole_tree_reasons=entry_whole_tree_reasons, command=command, setups=setups,
                      entry_setups=entry_setups, entry_durations=entry_durations, os=operating_system)
        if has_paths:
            suites.append(Suite(name, entries, paths=_strings(item["paths"], f"{location} ({name}): paths"), entry_paths=entry_paths, **common))
        else:
            suites.append(Suite(name, entries, **common))
    if setup_data is not None:
        # A pinned tool is no setup of the suite list: select decides on it with the configuration file's names.
        check_stated_setups(suites, {setup.name for setup in setup_data} | set(PINNED_TOOLS), source)
    return SuiteList(always, tuple(suites), module_lookups, setup_data, job_data, entry_durations_version=durations_version)


def check_stated_setups(suites, known: set[str], source: str) -> None:
    """Fail on the first setup that a suite or one of its entry files states and ``known`` lacks."""
    for index, suite in enumerate(suites, start=1):
        stated = [("setups", name) for name in suite.setups or ()]
        stated += [(f"entry_setups ({entry})", name) for entry, names in suite.entry_setups for name in names]
        for key, name in stated:
            if name not in known:
                raise SuiteListError(f"{source}: suites[{index}] ({suite.name}): {key} names the setup {name}, which setups lacks")


def _setups(value, location: str) -> tuple[Setup, ...]:
    """The setups of a suite list, in the order it states them."""
    if not isinstance(value, dict):
        raise SuiteListError(f"{location} must be an object with one setup per name")
    setups: list[Setup] = []
    versions: dict[str, tuple[str, str]] = {}  # per tool, the version that the first setup naming it gives, with that setup
    for name, item in value.items():
        if not NAME_PATTERN.match(name):
            raise SuiteListError(f"{location}: the name {name} must be lowercase letters, digits and hyphens")
        where = f"{location} ({name})"
        if not isinstance(item, dict):
            raise SuiteListError(f"{where} must be an object")
        unknown = set(item) - SETUP_KEYS
        if unknown:
            raise SuiteListError(f"{where}: unknown keys {', '.join(sorted(unknown))}")
        script = item.get("script")
        if not isinstance(script, str) or not script.endswith(SCRIPT_SUFFIXES) or script.startswith(("/", "../", "./")) or "\\" in script:
            raise SuiteListError(f"{where}: script must be a repository path that ends in {', '.join(SCRIPT_SUFFIXES[:-1])} or {SCRIPT_SUFFIXES[-1]}")
        recorded = item.get("recorded", True)
        if not isinstance(recorded, bool):
            raise SuiteListError(f"{where}: recorded must be true or false")
        tool, version, version_file = item.get("tool"), item.get("version"), item.get("version-file")
        if tool is None and (version is not None or version_file is not None):
            raise SuiteListError(f"{where}: version and version-file go with tool")
        if tool is not None:
            if tool not in TOOLS:
                raise SuiteListError(f"{where}: tool must be one of {', '.join(TOOLS)}")
            if (version is None) == (version_file is None):
                raise SuiteListError(f"{where}: a tool needs either version or version-file")
            if version is not None and (not isinstance(version, str) or not version):
                raise SuiteListError(f"{where}: version must be a non-empty string")
            if version_file is not None and (not isinstance(version_file, str) or not VERSION_FILE.match(version_file)):
                raise SuiteListError(f"{where}: version-file must be <JSON file>#/<key>/<key>")
            given = version if version is not None else f"from {version_file}"
            first, owner = versions.setdefault(tool, (given, name))
            if first != given:
                raise SuiteListError(f"{where}: the tool {tool} has the version {given}, but the setup {owner} gives it {first}; "
                                     "a suite list holds one version per tool")
        setups.append(Setup(name, script, recorded, tool, version, version_file))
    return tuple(setups)


def _jobs(value, location: str) -> tuple[tuple[str, tuple[tuple[str, object], ...]], ...]:
    """What the suite list states of the suite job of each operating system: its runner label and its fetch depth."""
    if not isinstance(value, dict):
        raise SuiteListError(f"{location} must be an object with one job per operating system")
    jobs = []
    for name, item in value.items():
        if name not in OPERATING_SYSTEMS:
            raise SuiteListError(f"{location}: {name} must be one of {', '.join(OPERATING_SYSTEMS)}")
        where = f"{location} ({name})"
        if not isinstance(item, dict):
            raise SuiteListError(f"{where} must be an object")
        unknown = set(item) - set(JOB_DEFAULTS[name])
        if unknown:
            raise SuiteListError(f"{where}: unknown keys {', '.join(sorted(unknown))}")
        if "runner" in item and (not isinstance(item["runner"], str) or not RUNNER_LABEL.fullmatch(item["runner"])):
            raise SuiteListError(f"{where}: runner must be a label of a GitHub-hosted runner of the ubuntu-, windows- or macos- family, such as ubuntu-26.04")
        depth = item.get("fetch-depth", 0)
        if not isinstance(depth, int) or isinstance(depth, bool) or depth < 0:
            raise SuiteListError(f"{where}: fetch-depth must be a whole number of 0 or more")
        jobs.append((name, tuple(item.items())))
    return tuple(jobs)


def _module_lookups(value, location: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The module lookups per checkout directory, in key order."""
    if not isinstance(value, dict) or not all(isinstance(key, str) and key for key in value):
        raise SuiteListError(f"{location} must be an object with a list of module names per checkout directory")
    return tuple((directory, _strings(names, f"{location} ({directory})")) for directory, names in value.items())


def _per_entry(value, entries: tuple[str, ...], location: str, *, recorded: bool) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """A mapping from entry file to a list of patterns; ``_entry_keys`` states which keys it may hold and their order.
    An entry file may lack a key: in ``entry_paths`` it then has no stored list, as ``Suite.lacks_list`` states."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise SuiteListError(f"{location} must be an object with one list of patterns per entry file")
    keys = _entry_keys(value, entries, location, recorded=recorded)
    return tuple((entry, _strings(value[entry], f"{location} ({entry})")) for entry in keys)


def load_suite_list(path: Path) -> SuiteList:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise SuiteListError(f"cannot read {path}: {error}") from error
    return parse_suite_list(text, str(path))


def render_suite_list(suite_list: SuiteList) -> str:
    """The suite list as JSON text, one path per line, with a trailing newline."""
    return json.dumps(suite_list_document(suite_list, [suite.entry() for suite in suite_list.suites],
                                          {directory: list(names) for directory, names in suite_list.module_lookups}), indent=2) + "\n"


def suite_list_document(suite_list: SuiteList, suites: list[dict], module_lookups: dict[str, list[str]], *,
                        entry_durations_version: int | None = None) -> dict:
    """The JSON document of a suite list with the given suite entries and module lookups, its keys in their fixed order."""
    version = suite_list.entry_durations_version if entry_durations_version is None else entry_durations_version
    document: dict = {"always": list(suite_list.always)}
    if version > 1:
        document["entry_durations_version"] = version
    if suite_list.jobs:
        document["jobs"] = {name: dict(item) for name, item in suite_list.jobs}
    if suite_list.setups is not None:
        document["setups"] = {setup.name: setup.entry() for setup in suite_list.setups}
    document["suites"] = suites
    if module_lookups:
        document["module_lookups"] = module_lookups
    return document


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
    """The suite list with every entry pattern expanded to the tracked files it matches. A suite loses the keys of its
    recorded per-entry fields that name no entry file any more, so nothing that selects reads them."""
    suites = []
    for index, suite in enumerate(suite_list.suites, start=1):
        entries = expand_entries(suite.entries, tracked, f"{source}: suites[{index}] ({suite.name})")
        unknown_reasons = [entry for entry, _ in suite.entry_whole_tree_reasons if entry not in entries]
        if unknown_reasons:
            raise SuiteListError(f"{source}: suites[{index}] ({suite.name}): entry_whole_tree_reasons names {', '.join(sorted(unknown_reasons))}, "
                                 "which the suite does not hold")
        current = {field: tuple(pair for pair in getattr(suite, field) if pair[0] in entries) for field in RECORDED_ENTRY_FIELDS}
        suites.append(dataclasses.replace(suite, entries=entries, **current))
    for setup in suite_list.setups or ():
        for file, what in zip(setup.files, ("script", "version file")):
            if file not in tracked:
                raise SuiteListError(f"{source}: setups ({setup.name}): the {what} {file} is no tracked file")
    return dataclasses.replace(suite_list, suites=tuple(suites))


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
    folded_base: str = ""
    folded_regex: re.Pattern[str] | None = None


def ignore_patterns(path: str, text: str) -> list[IgnorePattern]:
    base = path.rpartition("/")[0]
    folded_base = base.lower()
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
        pattern_text = ("" if anchored else "(?:.*/)?") + expression + r"\Z"
        patterns.append(IgnorePattern(base, re.compile(pattern_text), negated, directory_only,
                                      folded_base, re.compile(pattern_text, re.IGNORECASE)))
    return patterns


class IgnoreRules:
    """The ignore rules of the tracked ``.gitignore`` files of one commit, as gitignore(5) defines them, matched
    against repository paths with ``/`` separators (case-sensitively by default, or case-insensitively with ``fold``);
    no other ignore source takes part."""

    def __init__(self, files: dict[str, str]) -> None:
        self.files = dict(files)
        ordered = sorted(self.files, key=lambda path: (path.count("/"), path))
        self.patterns = [pattern for path in ordered for pattern in ignore_patterns(path, self.files[path])]
        self._decided: dict[tuple[str, bool, bool], bool] = {}

    def ignored(self, path: str, directory: bool = False, *, fold: bool = False) -> bool:
        """Whether the rules match the path, or one of its parent directories; ``directory`` says that the path is one."""
        parts = path.split("/")
        return any(self._matched("/".join(parts[:index]), True, fold) for index in range(1, len(parts))) or self._matched(path, directory, fold)

    def _matched(self, path: str, directory: bool, fold: bool = False) -> bool:
        key = (path.lower() if fold else path, directory, fold)
        if key not in self._decided:
            result = False
            folded = path.lower() if fold else path
            for pattern in self.patterns:
                if pattern.directory_only and not directory:
                    continue
                if fold:
                    if pattern.folded_base and not folded.startswith(pattern.folded_base + "/"):
                        continue
                    regex = pattern.folded_regex or pattern.regex
                    target = path[len(pattern.base) + 1:] if pattern.base else path
                else:
                    if pattern.base and not path.startswith(pattern.base + "/"):
                        continue
                    regex = pattern.regex
                    target = path[len(pattern.base) + 1:] if pattern.base else path
                if regex.match(target):
                    result = not pattern.negated
            self._decided[key] = result
        return self._decided[key]


def revision_text(root: Path, revision: str, path: str) -> str | None:
    """The text of a tracked file at a revision, or None when the revision or the file is not there."""
    shown = subprocess.run(["git", "-C", str(root), "show", f"{revision}:{path}", "--"], capture_output=True, check=False)
    if shown.returncode != 0:
        return None
    return shown.stdout.decode("utf-8", errors="replace")


def ignore_rules(root: Path, revision: str, files: frozenset[str]) -> IgnoreRules:
    """The ignore rules of the tracked ``.gitignore`` files among ``files``, with their text at the revision."""
    return IgnoreRules({path: revision_text(root, revision, path) or "" for path in sorted(file for file in files if is_ignore_file(file))})
