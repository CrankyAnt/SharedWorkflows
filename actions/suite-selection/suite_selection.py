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
    python suite_selection.py shared-lookup --root <repository> --digest <digest> --branch <base branch> \\
        [--default-branch <default branch>] [--pushed <commit>] --workflow "$GITHUB_WORKFLOW_REF" --output <file>
    python suite_selection.py store --needs <needs context as JSON> [--selection-job <job>] --passed <directory> --run-id <id> \\
        --output <record file>
    python suite_selection.py check-dispatch --root <repository> --sha <client_payload.sha> --branch <default branch>

``lookup``, ``shared-lookup``, ``store`` and the reuse of passed results live in ``reuse.py``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import subprocess
import sys
import tempfile
import dataclasses
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ci_definition  # noqa: E402
from revisions import ALL_ZERO, commit_id, git  # noqa: E402
from suite_list import (  # noqa: E402, F401
    GLOB_CHARACTERS,
    IGNORE_FILE,
    JOB_DEFAULTS,
    NAME_PATTERN,
    OPERATING_SYSTEMS,
    PINNED_TOOLS,
    RECORDED_ENTRY_FIELDS,
    RUNNER_LABEL,
    SCRIPT_SUFFIXES,
    SETUP_KEYS,
    STANDARD_LIBRARY,
    SUITE_KEYS,
    TOOLS,
    TOP_LEVEL_KEYS,
    VERSION_FILE,
    WIDE_TO_MID,
    ExistenceRead,
    IgnorePattern,
    IgnoreRules,
    PinnedTool,
    Setup,
    Suite,
    SuiteList,
    SuiteListError,
    _with_extra,
    check_stated_setups,
    expand_entries,
    first_match,
    full_recording_scale,
    glob_entries,
    ignore_patterns,
    ignore_regex,
    ignore_rules,
    is_ignore_file,
    load_suite_list,
    matches,
    module_name,
    parse_suite_list,
    parse_suite_list_document,
    pattern_regex,
    render_suite_list,
    resolve_entries,
    revision_text,
    standard_library_names,
    suite_list_document,
    tracked_files,
)

if TYPE_CHECKING:
    from unrecorded import Cause, EntryFile, Parts

SELECTION_JOB = "select"
FAILING_RESULTS = ("failure", "cancelled")
SHARD_BOUND = 120.0  # seconds of estimated traced test time per Linux shard on its workers
FULL_SHARD_BOUND = 300.0  # seconds of estimated wide-traced test time per Linux shard of the full recording
FULL_REASON = "the full recording runs every entry file"
SHARD_WORKERS = 2
LANGUAGE_STEPS = {
    "python": ("**/*.py", "**/*.pyi", "**/pyproject.toml", "**/setup.cfg", "**/ruff.toml", "**/.ruff.toml", "**/mypy.ini", "**/.mypy.ini"),
}
RECORDING_SCRIPT = Path(__file__).resolve().parent.parent / "recording" / "recording.py"  # its apply keys a run under its additions


class VerdictError(Exception):
    """The needs context cannot be read."""


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


def with_pinned_tools(suite_list: SuiteList, names: list[str], pins: Path | None, source: str = "the suite list",
                      root: Path | None = None) -> SuiteList:
    """The suite list with the pinned tools that the configuration file names, each with its version of the pins file.
    A pinned tool is a setup that the suite jobs give: the suite list defines none of its name, and none that installs
    Node.js beside the pinned Node.js. A suite that states a pinned tool which the configuration file does not name states
    an undefined setup."""
    for name in names:
        if name in suite_list.setups_by_name:
            raise SuiteListError(f"{source}: setups ({name}): pinned-tools names {name}, so the suite jobs give this setup; remove it from setups")
    if "node" in names:
        own = next((setup.name for setup in suite_list.setups or () if setup.tool == "node"), None)
        if own is not None:
            raise SuiteListError(f"{source}: setups ({own}): the tool node stands beside node in pinned-tools; a suite list holds one version per tool")
    if suite_list.setups is not None:
        check_stated_setups(suite_list.suites, set(suite_list.setups_by_name) | set(names), source)
    if not names:
        return suite_list
    if pins is None:
        raise SuiteListError("select needs --pins, the pins file, with --pinned-tool")
    try:
        document = json.loads(pins.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SuiteListError(f"the pins file {pins} cannot be read as JSON: {error}") from error
    pins_relative: str | None = None
    if root is not None:
        resolved_pins = pins.resolve()
        resolved_root = root.resolve()
        if resolved_pins.is_relative_to(resolved_root):
            pins_relative = resolved_pins.relative_to(resolved_root).as_posix()
    pinned = []
    for name in dict.fromkeys(names):
        pin = (document.get("node") if name == "node" else document.get("tools", {}).get(name)) if isinstance(document, dict) else None
        version = pin.get("version") if isinstance(pin, dict) else None
        if not isinstance(version, str) or not version:
            raise SuiteListError(f"the pins file {pins} holds no version of {name}")
        pinned.append(PinnedTool(name, version, json.dumps(pin, sort_keys=True), pins_relative))
    return dataclasses.replace(suite_list, pinned=tuple(pinned))


def tool_versions(suite_list: SuiteList, root: Path) -> dict[str, str]:
    """The version of each tool that a setup of the suite list names, read from its version file where it names one, and
    of Node.js as a pinned tool."""
    versions: dict[str, str] = {tool.name: tool.version for tool in suite_list.pinned if tool.name in TOOLS}
    for setup in suite_list.setups or ():
        if setup.tool is None or setup.tool in versions:
            continue
        if setup.version is not None:
            versions[setup.tool] = setup.version
            continue
        file, _, pointer = (setup.version_file or "").partition("#")
        where = f"setups ({setup.name})"
        try:
            value = json.loads((root / file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise SuiteListError(f"{where}: {file} cannot be read as JSON: {error}") from error
        for key in pointer.strip("/").split("/"):
            value = value.get(key) if isinstance(value, dict) else None
        if not isinstance(value, str) or not value:
            raise SuiteListError(f"{where}: {file} holds no text at {pointer}")
        versions[setup.tool] = value
    return versions


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
            def patterns(entry: str | None, suite: Suite = suite) -> tuple[str, ...]:
                return _with_extra(suite.patterns_of(entry), suite_list.setup_files(suite, entry))

            result.append(Basis(suite, patterns(None), None, entry_patterns=suite.per_entry(patterns),
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
                     system: str, previous_tracked: frozenset[str] | None = None,
                     current_tracked: frozenset[str] | None = None) -> str | None:
    """A changed path that changes an exists/is_file/is_dir predicate, using tree names or the filesystem; ``system`` is
    the operating system of the suite that stores the read."""
    path = read.path_on(system)
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
    # The operating system of a suite decides which path a stored existence read names, so each read goes with it.
    existence = {(read, basis.suite.os) for basis in resolved for read in basis.existence_reads}
    existence.update((read, basis.suite.os) for basis in resolved for _, values in basis.entry_existence_reads for read in values)
    looked_in = {directory for basis in resolved for directory in basis.lookup_directories}
    looked_in.update(directory for basis in resolved for _, values in basis.entry_lookup_directories for directory in values)
    if (memberships or existence or looked_in) and current_tracked is None and root is not None:
        current_tracked = frozenset(tracked_files(root))
    membership_changes = {membership: membership_change(root, previous_root, membership, changed,
                          previous_tracked=previous_tracked, current_tracked=current_tracked) for membership in memberships}
    existence_changes = {(read, system): existence_change(root, previous_root, read, changed, system=system,
                         previous_tracked=previous_tracked, current_tracked=current_tracked) for read, system in existence}
    added = added_files(root, previous_root, changed, previous_tracked, current_tracked) if looked_in else []

    def existence_file(basis: Basis, read: ExistenceRead) -> str | None:
        """The changed file that changes an existence read of the basis's suite, or None."""
        return existence_changes[read, basis.suite.os]

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
                        if (file := existence_file(basis, dependency)) is not None)
            read.update(found[0] for directory in basis.lookup_directories if (found := lookup_changes[directory]) is not None)
        if basis.reason is not None:
            decisions.append(Decision(basis.suite.name, True, basis.reason, basis.suite.entries))
        elif basis.entry_patterns:
            # A suite selected per entry file: each entry runs when a changed file matches its own patterns.
            selected = []
            reasons = []
            for entry, patterns in basis.entry_patterns:
                if basis.suite.lacks_list(entry):
                    selected.append(entry)
                    reasons.append(f"`{entry}` {NO_PATH_LIST_REASON}")
                    continue
                match = first_match(patterns, changed)
                if match is None:
                    membership = next((value for value in dict(basis.entry_memberships).get(entry, ())
                                       if membership_changes[value] is not None), None)
                    if membership is not None:
                        match = (membership_changes[membership] or membership,
                                 f"directory membership `{membership}`")
                if match is None:
                    dependency = next((value for value in dict(basis.entry_existence_reads).get(entry, ())
                                       if existence_file(basis, value) is not None), None)
                    if dependency is not None:
                        match = (existence_file(basis, dependency), f"{dependency.kind} existence `{dependency.path}`")
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
                dependency = next((value for value in basis.existence_reads if existence_file(basis, value) is not None), None)
                if dependency is not None:
                    match = (existence_file(basis, dependency), f"{dependency.kind} existence `{dependency.path}`")
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


def shard_document(units: list[tuple[Suite, str, float]], estimate: float, suite_list: SuiteList | None = None,
                   tools: dict[str, str] | None = None) -> dict:
    """The estimate, the setups and the entry files with their suite's command of one job; for a suite list that states
    its setups as data, also the tools that the job's setups name, each with its version from ``tools``; and the pinned
    tools among the job's setups, when it has any."""
    setups: list[str] = []
    for suite, entry, _ in units:
        setups += [setup for setup in suite.setups_of(entry) if setup not in setups]
    named: dict = {}
    pinned = [tool.name for tool in suite_list.pinned if tool.name in setups] if suite_list is not None else []
    if suite_list is not None and (suite_list.setups is not None or pinned):
        # The tool that the job's action installs for a setup: the one its definition names, or the pinned tool itself.
        defined = {name: setup.tool for name, setup in suite_list.setups_by_name.items()}
        stated = [name if name in pinned else defined.get(name) for name in setups]
        named = {"tools": {tool: (tools or {})[tool] for tool in dict.fromkeys(stated) if tool in TOOLS}}
    if pinned:
        named["pinned"] = pinned
    return {"estimate": estimate, "setups": setups, **named,
            "entries": [{"suite": suite.name, "entry": entry, "command": list(suite.command) if suite.command is not None else None}
                        for suite, entry, _ in units]}


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


def shards_json(selection: Selection, suite_list: SuiteList, steps: dict[str, str], full: bool = False,
                tools: dict[str, str] | None = None) -> str:
    """The Linux shard matrix as a JSON list on one line; one shard without suites when only a language step runs. The
    full recording counts each estimate under the wide set and bounds each shard by ``FULL_SHARD_BOUND``."""
    units = selected_units(selection, suite_list, "linux", full_recording_scale("linux") if full else 1.0)
    shards = [shard_document(shard, two_worker_time([estimate for _, _, estimate in shard]), suite_list, tools)
              for shard in assign_shards(units, FULL_SHARD_BOUND if full else SHARD_BOUND)]
    if not shards and "run" in steps.values():
        shards = [shard_document([], 0.0, suite_list, tools)]
    return json.dumps([{"shard": number, **shard} for number, shard in enumerate(shards, start=1)], separators=(",", ":"))


def windows_json(selection: Selection, suite_list: SuiteList, full: bool = False, tools: dict[str, str] | None = None) -> str:
    """The selected Windows entry files as one job's JSON object on one line, or empty when none is selected. The job
    runs its entry files one after another, so its estimate is the sum of their estimates."""
    units = selected_units(selection, suite_list, "windows", full_recording_scale("windows") if full else 1.0)
    return json.dumps(shard_document(units, sum(estimate for _, _, estimate in units), suite_list, tools), separators=(",", ":")) if units else ""


# --- the push lookup and the reuse of passed results ------------------------------------------------------


def identical_selection(suite_list: SuiteList, reason: str) -> Selection:
    """The selection of a push whose tree equals the passed tested tree: no suite runs."""
    return Selection(tuple(Decision(suite.name, False, reason, per_entry=suite.entry_selection) for suite in suite_list.suites), (), None)


def both_sides(first: Selection, second: Selection) -> Selection:
    """The units that both selections run: a suite runs the entry files that both select.
    If either side could not determine its changed files, every suite runs and ``undetermined`` is preserved."""
    if first.undetermined is not None or second.undetermined is not None:
        why = first.undetermined if first.undetermined is not None else second.undetermined
        decisions = tuple(Decision(one.suite, True, f"the changed files could not be determined: {why}",
                                   one.entries or other.entries, per_entry=one.per_entry or other.per_entry)
                          for one, other in zip(first.decisions, second.decisions))
        return Selection(decisions, (), why, tuple(dict.fromkeys([*first.started, *second.started])))
    decisions = []
    for one, other in zip(first.decisions, second.decisions):
        entries = tuple(entry for entry in one.entries if entry in set(other.entries)) if one.run and other.run else ()
        per_entry = one.per_entry or other.per_entry
        if entries:
            decisions.append(Decision(one.suite, True, f"both sides: {one.reason}; {other.reason}", entries, per_entry,
                                      tuple(dict.fromkeys(one.definition_reads + other.definition_reads))))
        else:
            decisions.append(Decision(one.suite, False, "the pull request and the base since its tested base do not both affect it", per_entry=per_entry))
    return Selection(tuple(decisions), first.unread, None)


def unpassed_base_selection(first: Selection, second: Selection) -> Selection:
    """Selection for a behind merge whose landing base has not passed: nothing the newer base can affect is taken over."""
    if first.undetermined is not None or second.undetermined is not None:
        why = first.undetermined if first.undetermined is not None else second.undetermined
        decisions = tuple(Decision(one.suite, True, f"the changed files could not be determined: {why}",
                                   one.entries or other.entries, per_entry=one.per_entry or other.per_entry)
                          for one, other in zip(first.decisions, second.decisions))
        return Selection(decisions, (), why, tuple(dict.fromkeys([*first.started, *second.started])))
    decisions = []
    for one, other in zip(first.decisions, second.decisions):
        per_entry = one.per_entry or other.per_entry
        if other.run:
            overlap = one.run and bool(set(one.entries) & set(other.entries))
            reason = (f"both sides (newer base has not passed): {one.reason}; {other.reason}"
                      if overlap else f"the newer base has not passed: {other.reason}")
            decisions.append(Decision(other.suite, True, reason, other.entries, per_entry,
                                      tuple(dict.fromkeys(one.definition_reads + other.definition_reads))))
        else:
            reason = ("only the pull request's side selects it, which passed on its tested tree"
                      if one.run else "neither the pull request nor the newer base affects it")
            decisions.append(Decision(other.suite, False, reason, per_entry=per_entry))
    return Selection(tuple(decisions), second.unread, None, second.started)


def unit_digests(suite: Suite, setups: tuple[str, ...], digests: dict[str, str] | None,
                 language_steps: tuple[str, ...] = ()) -> dict[str, str]:
    """The shared CI digests that apply to one unit: its platform digest, the tool digest of each setup it states, and
    the digest of any language step it uses."""
    if not digests:
        return {}
    selected: dict[str, str] = {}
    platform_key = f"platform:{suite.os}"
    if platform_key in digests:
        selected[platform_key] = digests[platform_key]
    for setup in setups:
        tool_key = f"tool:{setup}"
        if tool_key in digests:
            selected[tool_key] = digests[tool_key]
    for step in language_steps:
        lang_key = f"language:{step}"
        if lang_key in digests:
            selected[lang_key] = digests[lang_key]
    return selected


def select_for_moved_digests(selection: Selection, suite_list: SuiteList, moved: frozenset[str], reason: str,
                             language_names: list[str]) -> tuple[Selection, bool]:
    """Add to ``selection`` every unit and language step whose platform, tool or language-step digest is in ``moved``.
    Returns the updated selection and whether any suite or language step is selected by the moved digests."""
    by_name = {suite.name: suite for suite in suite_list.suites}
    decisions: list[Decision] = []
    any_selected = False
    for decision in selection.decisions:
        suite = by_name[decision.suite]
        platform_key = f"platform:{suite.os}"
        if platform_key in moved:
            any_selected = True
            decisions.append(Decision(suite.name, True, f"`{platform_key}` digest moved: {reason}", suite.entries, False, decision.definition_reads))
            continue
        affected: dict[str, str] = {}
        for entry in suite.entries:
            tool_key = next((f"tool:{setup}" for setup in suite.setups_of(entry) if f"tool:{setup}" in moved), None)
            if tool_key is not None:
                affected[entry] = tool_key
        if not affected:
            decisions.append(decision)
            continue
        any_selected = True
        first_key = next(iter(affected.values()))
        move_reason = f"`{first_key}` digest moved: {reason}"
        if suite.entry_selection:
            existing = set(decision.entries) if decision.run else set()
            merged = tuple(entry for entry in suite.entries if entry in existing or entry in affected)
            combined_reason = f"{decision.reason}; {move_reason}" if decision.run else move_reason
            decisions.append(Decision(suite.name, True, combined_reason, merged, True, decision.definition_reads))
        else:
            combined_reason = f"{decision.reason}; {move_reason}" if decision.run else move_reason
            decisions.append(Decision(suite.name, True, combined_reason, suite.entries, False, decision.definition_reads))
    started = list(selection.started)
    for name in language_names:
        if f"language:{name}" in moved:
            any_selected = True
            if name not in started:
                started.append(name)
    return dataclasses.replace(selection, decisions=tuple(decisions), started=tuple(started)), any_selected


def reuse_units(selection: Selection, suite_list: SuiteList, tree, versions: dict[str, str], labels: dict[str, str],
                shared_digests: dict[str, str] | None = None, *, ignore_content: dict, parts: Parts | None = None,
                unit_language_steps: dict[str, tuple[str, ...]] | None = None) -> list[KeyedUnit]:
    """The keyed unit (``KeyedUnit``) of every running unit with a stored path list: a suite, or each entry
    file of a suite selected per entry file. The reads are the unit's patterns with the always-run patterns, its
    directory memberships and existence reads, and the files of the CI definition counted as read by its suite; for a
    suite selected as a whole also each of its entry files with its content, whether its lists match it or not;
    ``shared_digests`` holds the shared CI digests (from which each unit takes its platform, tool and language-step
    digests), ``ignore_content`` the content of the ignore rules (``reuse.Tree.ignore_content``) and ``parts`` the
    unrecorded parts of the tested tree (``unrecorded.parts``), of which the key holds those of the unit."""
    import reuse
    import unrecorded

    parts = parts if parts is not None else unrecorded.Parts()
    by_basis = {basis.suite.name: basis for basis in bases(suite_list)}
    standard = standard_library_names()
    units = []
    for decision in selection.decisions:
        basis = by_basis[decision.suite]
        suite = basis.suite
        if not decision.run or basis.patterns is None:
            continue
        always = suite_list.always + decision.definition_reads
        groups = [(entry,) for entry in decision.entries if not suite.lacks_list(entry)] if suite.entry_selection else [decision.entries]
        for entries in groups:  # an entry file without a stored list has unknown inputs, so no passed result of it is reused
            setups = tuple(dict.fromkeys(setup for entry in entries for setup in suite.setups_of(entry)))
            digests_for_unit = unit_digests(suite, setups, shared_digests, (unit_language_steps or {}).get(suite.name, ()))
            context = reuse.unit_context(suite.command, setups, versions, labels.get(suite.os, ""), digests_for_unit,
                                         ignore_content=ignore_content, unrecorded=unrecorded.unit_content(parts, suite, entries))
            reads = unit_reads(suite_list, basis, entries, always, tree, standard)
            units.append(KeyedUnit(reuse.unit_key(reads, context), decision, entries, context, always))
    return units


class KeyedUnit(NamedTuple):
    """A running unit with a stored path list: its key, its suite's decision, its entry files, the part of its key that
    the stored lists do not decide (``reuse.unit_context``) and the always-run patterns that it reads with its own."""

    key: str
    decision: Decision
    entries: tuple[str, ...]
    context: str
    always: tuple[str, ...]


def unit_reads(suite_list: SuiteList, basis: Basis, entries: tuple[str, ...], always: tuple[str, ...], tree, standard: frozenset[str]) -> dict:
    """The content of a unit's reads in the tree under the stored lists of ``suite_list``: for an entry file of a suite
    selected per entry file its own lists, for a suite selected as a whole the suite's lists and each of its entry files
    with its content."""
    suite = basis.suite

    def lookups(directories: tuple[str, ...]) -> list[tuple[str, frozenset[str]]]:
        return [(directory, standard | frozenset(suite_list.lookups_in(directory))) for directory in directories]

    if suite.entry_selection:
        entry = entries[0]
        return tree.reads(dict(basis.entry_patterns)[entry] + always, dict(basis.entry_memberships).get(entry, ()),
                          dict(basis.entry_existence_reads).get(entry, ()), lookups(dict(basis.entry_lookup_directories).get(entry, ())),
                          system=suite.os)
    reads = tree.reads(basis.patterns + always, basis.directory_memberships, basis.existence_reads, lookups(basis.lookup_directories),
                       system=suite.os)
    # No recording guarantees that the stored lists name an entry file's own path: the key holds each entry file with its
    # content, so a change to one that the lists do not match reuses no passed result.
    reads["entries"] = tree.entry_files(entries)
    return reads


def applied_keys(units: dict, suite_list: SuiteList, tree) -> dict[str, str]:
    """The key of each running unit that ``select`` output, by its key, under the stored lists of ``suite_list``: the
    suite list with the additions of the run applied. A unit whose suite or entry file has no stored list there, or
    whose output lacks its context, has none."""
    import reuse

    by_basis = {basis.suite.name: basis for basis in bases(suite_list)}
    standard = standard_library_names()
    keys = {}
    for key, unit in units.items():
        if not isinstance(unit, dict):
            continue
        basis, entries = by_basis.get(unit.get("suite")), unit.get("entries")
        context, always = unit.get("context"), unit.get("always")
        if (basis is None or basis.patterns is None or not isinstance(context, str) or not isinstance(always, list)
                or not isinstance(entries, list) or not entries or not all(isinstance(item, str) for item in entries + always)):
            continue
        suite = basis.suite
        if any(entry not in suite.entries for entry in entries):
            continue
        if suite.entry_selection and (len(entries) != 1 or suite.lacks_list(entries[0])):
            continue
        keys[key] = reuse.unit_key(unit_reads(suite_list, basis, tuple(entries), tuple(always), tree, standard), context)
    return keys


def apply_reuse(selection: Selection, units: list[KeyedUnit], record: dict[str, int]) -> tuple[Selection, dict, dict]:
    """The selection without the units whose key has a passed result, the running units by key with what ``store``
    needs to key them under applied additions, and the reused keys with the run each passed in."""
    reused = {unit.key: record[unit.key] for unit in units if unit.key in record}
    running = {unit.key: {"suite": unit.decision.suite, "entries": list(unit.entries), "context": unit.context, "always": list(unit.always)}
               for unit in units if unit.key not in record}
    dropped: dict[str, list[tuple[str, int]]] = {}
    for key, decision, entries, *_ in units:
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
    selecting.add_argument("--default-record", type=Path, help="the default-branch record from the default branch's cache scope; the file may be missing")
    selecting.add_argument("--run-attempt", type=int, default=1, help="github.run_attempt; above 1 no earlier result is reused")
    selecting.add_argument("--runner-label", action="append", default=[], help="<os>=<label> of the job that runs that system's units (repeatable)")
    selecting.add_argument("--setup-version", action="append", default=[], help="<setup>=<version> that a setup pins (repeatable)")
    selecting.add_argument("--shared-lookup", type=Path, help="the shared lookup from the shared-lookup command")
    selecting.add_argument("--pinned-tool", action="append", default=[], choices=PINNED_TOOLS,
                           help="a pinned tool that the configuration file names under pinned-tools (repeatable)")
    selecting.add_argument("--pins", type=Path, help="the pins file that holds the version of each pinned tool; needed with --pinned-tool")
    selecting.add_argument("--unit-decisions", type=Path, help="write the per-unit run/skip/reused decisions as JSON for plan.py")
    looking = commands.add_parser("lookup", help="find the tested tree of a pushed commit's pull request")
    looking.add_argument("--root", type=Path, required=True)
    looking.add_argument("--sha", required=True, help="the pushed commit, or client_payload.sha of a dependabot-merged run")
    looking.add_argument("--before", default="", help="the previous head of the push; empty for a dispatch run")
    looking.add_argument("--workflow", required=True, help="the calling workflow: its repository path, or github.workflow_ref")
    looking.add_argument("--run-attempt", type=int, default=1, help="github.run_attempt")
    looking.add_argument("--default-record", type=Path, help="the restored default-branch record; used to check whether the landing base passed")
    looking.add_argument("--output", type=Path, required=True, help="write the lookup as JSON to this file")
    looking.add_argument("--summary", type=Path, help="append the Markdown summary to this file")
    writing_state = commands.add_parser("tested-state", help="write the structured tested-state JSON of a pull-request merge checkout")
    writing_state.add_argument("--root", type=Path, required=True)
    writing_state.add_argument("--pull-request", type=int, required=True)
    writing_state.add_argument("--run-id", type=int, required=True)
    writing_state.add_argument("--run-attempt", type=int, default=1)
    writing_state.add_argument("--digests", default="", help="the shared CI digests JSON taken in the run, or empty")
    writing_state.add_argument("--output", type=Path, required=True)
    shared_parser = commands.add_parser("shared-lookup", help="compare the shared CI digests with the restored default-branch record")
    shared_parser.add_argument("--digests", required=True, help="the shared CI digests JSON that this run took")
    shared_parser.add_argument("--root", type=Path, default=Path("."), help="the checkout directory")
    shared_parser.add_argument("--default-record", type=Path, help="the restored default-branch record; may be missing")
    shared_parser.add_argument("--branch", default="", help="deprecated; kept for caller compatibility")
    shared_parser.add_argument("--default-branch", default="", help="deprecated; kept for caller compatibility")
    shared_parser.add_argument("--pushed", default="", help="deprecated; kept for caller compatibility")
    shared_parser.add_argument("--workflow", default="", help="deprecated; kept for caller compatibility")
    shared_parser.add_argument("--output", type=Path, required=True, help="write the shared lookup as JSON to this file")
    shared_parser.add_argument("--summary", type=Path, help="append the Markdown summary to this file")
    storing = commands.add_parser("store", help="write the record of the passed results of a run")
    storing.add_argument("--needs", type=Path, required=True, help="a file with the closing job's needs context as JSON")
    storing.add_argument("--selection-job", default=SELECTION_JOB, help="the job of the needs context that ran select (default: select)")
    storing.add_argument("--passed", type=Path, required=True, help="a directory with the passed entry files of each job")
    storing.add_argument("--run-id", type=int, required=True, help="github.run_id")
    storing.add_argument("--output", type=Path, required=True, help="write the record to this file")
    storing.add_argument("--github-output", type=Path, help="append stored=true or stored=false to this file")
    storing.add_argument("--additions", type=Path, help="a directory with the additions documents of the run's jobs")
    storing.add_argument("--root", type=Path, help="with --additions: the checkout of the tested tree")
    storing.add_argument("--default-branch", action="store_true", help="write the structured default-branch record for a passed default-branch run")
    storing.add_argument("--commit", default="", help="with --default-branch: the 40-character commit SHA tested on the default branch")
    storing.add_argument("--tree", default="", help="with --default-branch: the 40-character Git tree SHA tested on the default branch")
    storing.add_argument("--digests", default="", help="with --default-branch: the shared CI digests JSON taken in the run")
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
    if options.command == "store" and (options.additions is None) != (options.root is None):
        parser.error("store takes --additions and --root together")
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
    suite_list = with_pinned_tools(suite_list, options.pinned_tool, options.pins, str(options.suites), root=root)
    if options.full:
        return write_selection(options, suite_list, every_suite(suite_list, FULL_REASON), [], full=True)
    found = sides = None
    identical = False
    fallbacks: dict[str, int] = {}

    def count_fallback(reason: str, detail: str | None = None, *, emit: bool = True) -> None:
        import reuse as _reuse

        fallbacks[reason] = fallbacks.get(reason, 0) + 1
        if emit and detail is not None:
            print(_reuse.fallback_notice(reason, detail))

    if options.lookup is not None:
        with record_errors() as reuse:
            found = reuse.read_lookup(options.lookup)
        if found.fallback:
            count_fallback(found.fallback, emit=False)
        if found.case == "identical":
            identical = True
            changed, selection = [], identical_selection(suite_list, f"the tree equals the tested tree that passed on pull request #{found.pull_request}")
        elif found.case == "behind":
            sides = [selection_between(options, root, tracked, suite_list, found.landing_base, found.head),
                     selection_between(options, root, tracked, suite_list, found.tested_base, found.landing_base)]
            if found.landing_base_passed is True:
                changed, selection = sides[0][0], both_sides(sides[0][1], sides[1][1])
            else:
                if not found.fallback:
                    count_fallback("unpassed-landing-base", f"landing base {found.landing_base} has no passed default-branch record")
                changed, selection = sides[1][0], unpassed_base_selection(sides[0][1], sides[1][1])
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
    if selection.undetermined is not None:
        count_fallback("changed-files-unknown", selection.undetermined)
    shared_lookup = None
    if options.shared_lookup is not None:
        with record_errors() as reuse:
            shared_lookup = reuse.read_shared_lookup(options.shared_lookup)
        if reuse.uses_tested_digests(found):
            if found.tested_digests is None:
                moved = None
                why = reuse.tested_reason(found, shared_lookup.digests)
                count_fallback("unreadable-tested-digests", why)
            else:
                moved = shared_lookup.moved_digests(found.tested_digests, use_given=True)
                why = reuse.tested_reason(found, shared_lookup.digests)
        else:
            if shared_lookup.fallback:
                count_fallback(shared_lookup.fallback, emit=False)
            moved = shared_lookup.moved_digests()
            why = shared_lookup.reason
        if moved is None:
            identical = False
            selection = every_suite(suite_list, f"every suite runs: {why}")
            if options.language_step:
                selection = dataclasses.replace(selection, started=tuple(dict.fromkeys(list(selection.started) + list(options.language_step))))
        elif moved:
            selection, affected = select_for_moved_digests(selection, suite_list, moved, why, options.language_step)
            if affected:
                identical = False
    running = reused = None
    if options.results is not None or options.default_record is not None:
        labels, versions = _pairs(options.runner_label, "--runner-label"), _pairs(options.setup_version, "--setup-version")
        if suite_list.setups is not None or suite_list.jobs:
            # The suite list states its jobs and setups as data: the key holds each job and each setup as stated, with
            # the version of its tool.
            tools = tool_versions(suite_list, root)
            labels = {name: json.dumps(suite_list.job(name), sort_keys=True) for name in OPERATING_SYSTEMS} | labels
            versions = {setup.name: json.dumps(setup.entry() | {"installed": tools.get(setup.tool or "")}, sort_keys=True)
                        for setup in suite_list.setups or ()} | versions
        # A pinned tool counts with what its pin holds: a passed result with another version or asset is not reused.
        versions = {tool.name: tool.pin for tool in suite_list.pinned} | versions
        with record_errors() as reuse:
            tree = reuse.Tree(reuse.tree_entries(root), matches)
            ignore_content = tree.ignore_content(ignore_rules(root, "HEAD", tree.files))
            parts = tested_parts(options, root, tree.files, suite_list)
            digests_map = shared_lookup.digests if shared_lookup is not None else None
            default_rec = None
            if options.default_record is not None:
                default_rec, default_fb, default_detail = reuse.read_default_record(options.default_record)
                if default_fb is not None and default_fb not in fallbacks:
                    count_fallback(default_fb, default_detail)
            if options.results is not None:
                units = reuse_units(selection, suite_list, tree, versions, labels, digests_map,
                                    ignore_content=ignore_content, parts=parts)
                if options.run_attempt <= 1:
                    pr_record, pr_fb, pr_detail = reuse.read_pr_record_safe(options.results)
                    if pr_fb is not None:
                        count_fallback(pr_fb, pr_detail)
                    default_passed = default_rec.passed if default_rec is not None else {}
                    record = {**default_passed, **pr_record}
                else:
                    record = {}
                selection, running, reused = apply_reuse(selection, units, record)
            else:
                units = reuse_units(selection, suite_list, tree, versions, labels, digests_map,
                                    ignore_content=ignore_content, parts=parts)
                running = {unit.key: {"suite": unit.decision.suite, "entries": list(unit.entries), "context": unit.context, "always": list(unit.always)}
                           for unit in units}
                all_units = reuse_units(every_suite(suite_list, ""), suite_list, tree, versions, labels, digests_map,
                                        ignore_content=ignore_content, parts=parts)
                carried: dict[str, int] = {}
                for unit in all_units:
                    if unit.key in running:
                        continue
                    if default_rec is not None and unit.key in default_rec.passed:
                        carried[unit.key] = default_rec.passed[unit.key]
                    elif found is not None and found.case == "identical" and found.tested_run is not None:
                        carried[unit.key] = found.tested_run
                    elif (found is not None and found.case == "behind" and found.tested_run is not None and sides is not None):
                        pr_side = {d.suite: set(d.entries) for d in sides[0][1].decisions if d.run}
                        base_side = {d.suite: set(d.entries) for d in sides[1][1].decisions if d.run}
                        if all(e in pr_side.get(unit.decision.suite, set()) and e not in base_side.get(unit.decision.suite, set())
                               for e in unit.entries):
                            carried[unit.key] = found.tested_run
                reused = carried
    steps = None
    if sides is not None:
        if sides[0][1].undetermined is not None or sides[1][1].undetermined is not None:
            steps = {name: "run" for name in options.language_step}
        elif found is not None and found.landing_base_passed is True:
            steps = {name: "run" if (name in selection.started or all(language_steps([name], side, files)[name] == "run" for files, side in sides)) else "skip"
                     for name in options.language_step}
        else:
            steps = {name: "run" if (name in selection.started or language_steps([name], sides[1][1], sides[1][0])[name] == "run") else "skip"
                     for name in options.language_step}
    write_selection(options, suite_list, selection, changed, steps=steps)
    if options.unit_decisions is not None:
        effective_steps = steps if steps is not None else language_steps(options.language_step, selection, changed)
        tools = tool_versions(suite_list, root)
        shards = json.loads(shards_json(selection, suite_list, effective_steps, False, tools))
        windows = windows_json(selection, suite_list, False, tools)
        options.unit_decisions.parent.mkdir(parents=True, exist_ok=True)
        pr_reused = (reused or {}) if options.results is not None else {}
        doc = unit_decisions_document(suite_list, selection, pr_reused, identical, shards, windows)
        doc["fallbacks"] = dict(sorted(fallbacks.items()))
        options.unit_decisions.write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="utf-8")
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
            if fallbacks:
                counts = ", ".join(f"`{name}` ({count})" for name, count in sorted(fallbacks.items()))
                handle.write(f"\nFallbacks: {counts}.\n")
            if (options.results is not None or options.default_record is not None) and options.run_attempt > 1:
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
            if "unsupported suite-selection condition" in str(error):
                return every_suite(suite_list, f"every suite runs: {error}")
            raise SuiteListError(f"the CI definition could not be read: {error}") from error
        workflow = definition[0].pattern if definition else None
        if workflow is not None and workflow in changed and previous is not None:
            # The calling workflow changed: when only steps of particular suites differ between the two commits, those
            # suites run instead of every suite.
            before, after = revision_text(root, previous, workflow), revision_text(root, head, workflow)
            if before is not None and after is not None:
                try:
                    gate = ci_definition.changed_step_gate(before, after)
                except ci_definition.WorkflowReadError as error:
                    return every_suite(suite_list, f"every suite runs: {error}")
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
    # An entry file that the change adds as a tracked file runs, also when the suite list names it through a pattern
    # and so does not change itself.
    for entry_file, cause in unrecorded.added_entry_files(suite_list, before.tracked, now.tracked).items():
        causes.setdefault(entry_file, cause)
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


def unit_decisions_document(suite_list: SuiteList, selection: Selection, reused: dict, identical: bool,
                            shards: list, windows: str) -> dict:
    """Per-unit run/reused/reason decisions and part summary for ``plan.py plan --unit-decisions``."""
    suites_by_name = {suite.name: suite for suite in suite_list.suites}
    unit_map: dict[str, dict] = {}
    for decision in selection.decisions:
        suite = suites_by_name.get(decision.suite)
        if decision.run and suite is not None and suite.entry_selection and decision.entries:
            for entry in decision.entries:
                unit_map[f"{decision.suite}:{entry}"] = {"run": True, "reason": decision.reason}
        else:
            row: dict = {"run": decision.run, "reason": decision.reason}
            if not decision.run and (identical or "reuses the passed result" in decision.reason):
                row["reused"] = True
            unit_map[decision.suite] = row
    jobs_planned = bool(shards) or bool(windows)
    if jobs_planned:
        parts = []
        if shards:
            parts.append(f"{len(shards)} Linux shard{'' if len(shards) == 1 else 's'}")
        if windows:
            parts.append("Windows")
        reason = " and ".join(parts)
        if reused:
            count = len(reused)
            reason += f"; {count} passed unit{'' if count == 1 else 's'} taken over"
        return {"run": True, "reused": False, "reason": reason, "units": unit_map}
    if identical:
        reason = selection.decisions[0].reason if selection.decisions else "the tree equals a passed tested tree"
        return {"run": False, "reused": True, "reason": reason, "units": unit_map}
    if reused:
        count = len(reused)
        return {"run": False, "reused": True, "reason": f"{count} passed unit{'' if count == 1 else 's'} taken over", "units": unit_map}
    return {"run": False, "reused": False, "reason": "no suites selected", "units": unit_map}


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
            tools = tool_versions(suite_list, options.root.resolve())
            handle.write(f"shards={shards_json(selection, suite_list, steps, full, tools)}\n")
            handle.write(f"windows={windows_json(selection, suite_list, full, tools)}\n")
            handle.write(f"jobs={json.dumps({name: suite_list.job(name) for name in OPERATING_SYSTEMS}, separators=(',', ':'))}\n")
    if options.summary is not None:
        with options.summary.open("a", encoding="utf-8") as handle:
            handle.write(summary_markdown(selection, changed))
    return 0


def run_lookup(options: argparse.Namespace) -> int:
    import reuse

    found = reuse.lookup(options.root.resolve(), reuse.environment_api(), Path(workflow_path(options.workflow)).name, options.sha,
                         options.before, options.run_attempt, options.default_record)
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(found.document(), indent=2) + "\n", encoding="utf-8")
    if found.fallback:
        print(reuse.fallback_notice(found.fallback, found.reason))
    print(f"{found.case}: {found.reason}")
    if options.summary is not None:
        with options.summary.open("a", encoding="utf-8") as handle:
            handle.write(reuse.lookup_markdown(found))
    return 0


def run_write_tested_state(options: argparse.Namespace) -> int:
    import reuse

    digests = reuse.parse_digests(options.digests) if options.digests else None
    if options.digests and digests is None:
        raise SuiteListError("--digests must be a non-empty JSON object from digest name to 64-character hex SHA-256")
    try:
        state = reuse.write_tested_state(options.root.resolve(), options.pull_request, options.run_id, options.run_attempt, digests, options.output)
    except reuse.RecordError as error:
        raise SuiteListError(str(error)) from error
    print(f"Wrote tested state for pull request #{state['pull_request']} merge {state['tested_merge']}.")
    return 0


def run_shared_lookup(options: argparse.Namespace) -> int:
    import reuse

    digests = reuse.parse_digests(options.digests)
    if digests is None:
        raise SuiteListError("--digests must be a non-empty JSON object from digest name to 64-character hex SHA-256")
    found = reuse.shared_lookup(digests, options.default_record)
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_text(json.dumps(found.document(), indent=2) + "\n", encoding="utf-8")
    for line in found.log:
        print(line)
    if found.fallback:
        print(reuse.fallback_notice(found.fallback, found.reason))
    print(f"{'moved' if found.moved else 'unchanged'}: {found.reason}")
    if options.summary is not None:
        with options.summary.open("a", encoding="utf-8") as handle:
            handle.write(reuse.shared_markdown(found))
    return 0


def keys_under_additions(root: Path, additions: Path, units: dict) -> tuple[dict[str, str] | None, set[tuple[str, str]], str]:
    """The key of each running unit under the suite list of the tested tree at ``root`` with every ``additions.json``
    under ``additions`` applied, as the additions workflow applies them, by the unit's key; or None, with why. The
    second value holds the (suite, entry file) pairs that the documents name, as ``recording.py keep`` marks them."""
    import reuse

    documents = sorted(additions.rglob("additions.json")) if additions.is_dir() else []
    if not documents:
        return None, set(), "No additions in the run."
    paths, named = set(), set()
    for document in documents:
        try:
            raw = json.loads(document.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            return None, set(), f"No key under the additions: {document} cannot be read: {error}."
        paths.add(raw.get("suite_list") if isinstance(raw, dict) else None)
        for item in raw.get("suites", []) if isinstance(raw, dict) and isinstance(raw.get("suites"), list) else []:
            entries = (item.get("entries") if "entries" in item else item.get("recorded_entries")) if isinstance(item, dict) else None
            if isinstance(entries, (dict, list)):
                named |= {(item.get("suite"), entry) for entry in entries if isinstance(entry, str)}
    if len(paths) != 1 or not all(isinstance(path, str) and path for path in paths):
        return None, set(), "No key under the additions: their documents do not name one suite list."
    relative = paths.pop()
    with tempfile.TemporaryDirectory(prefix="store-") as folder:
        applied = Path(folder) / "suites.json"
        try:
            applied.write_bytes((root / relative).read_bytes())
        except OSError as error:
            return None, set(), f"No key under the additions: the suite list {relative} cannot be read: {error}."
        completed = subprocess.run([sys.executable, str(RECORDING_SCRIPT), "apply", "--root", str(root), "--suites", str(applied),
                                    *map(str, documents)], capture_output=True, text=True, encoding="utf-8", check=False)
        if completed.returncode != 0:
            return None, set(), f"No key under the additions: apply failed with {completed.returncode}: {completed.stderr.strip()}."
        try:
            suite_list = resolve_entries(load_suite_list(applied), tracked_files(root), relative)
            keys = applied_keys(units, suite_list, reuse.Tree(reuse.tree_entries(root), matches))
        except (SuiteListError, reuse.RecordError) as error:
            return None, set(), f"No key under the additions: {error}."
    return keys, named, f"Keyed {len(keys)} of {len(units)} running units under the suite list with the additions of {len(documents)} job{'' if len(documents) == 1 else 's'} applied."


def run_store(options: argparse.Namespace) -> int:
    import reuse

    try:
        needs = parse_needs(options.needs.read_text(encoding="utf-8"))
        passed, with_additions = reuse.read_passed(options.passed)
        if options.default_branch:
            if not FULL_COMMIT_ID.fullmatch(options.commit):
                raise SuiteListError("--default-branch requires --commit as a 40-character lowercase hex SHA")
            if not FULL_COMMIT_ID.fullmatch(options.tree):
                raise SuiteListError("--default-branch requires --tree as a 40-character lowercase hex SHA")
            digests = reuse.parse_digests(options.digests) if options.digests else None
            if options.digests and digests is None:
                raise SuiteListError("--digests must be a non-empty JSON object from digest name to 64-character hex SHA-256")
            default_record = reuse.store_default_branch(needs, passed, options.run_id, options.commit, options.tree, digests, options.selection_job)
            if default_record is None:
                print("Nothing to store: the default-branch run did not pass or was cancelled.")
                stored = False
            else:
                options.output.parent.mkdir(parents=True, exist_ok=True)
                options.output.write_text(json.dumps(default_record.document(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
                print(f"Stored default-branch record for {default_record.commit} with {len(default_record.passed)} passed result{'' if len(default_record.passed) == 1 else 's'}.")
                stored = True
            if options.github_output is not None:
                with options.github_output.open("a", encoding="utf-8") as handle:
                    handle.write(f"stored={'true' if stored else 'false'}\n")
            return 0
        applied, named = None, set()
        units, _ = reuse.selection_outputs(needs, options.selection_job)
        if options.additions is not None and units is not None:
            applied, named, note = keys_under_additions(options.root.resolve(), options.additions, units)
            print(note)
        # A marked entry file whose additions no downloaded document names, as after a failed upload of its job's
        # additions, would be keyed under lists without them: it counts as not passed.
        with_additions &= named
        record = reuse.store(needs, passed, options.run_id, options.selection_job, with_additions=with_additions, applied=applied)
    except reuse.RecordError as error:
        raise SuiteListError(str(error)) from error
    if record is None:
        print("Nothing to store: the selection did not succeed or the run was cancelled.")
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
        if options.command == "tested-state":
            return run_write_tested_state(options)
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
