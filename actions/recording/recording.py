#!/usr/bin/env python3
"""Record what a job's setups and entry files read, check it against the suite list, and apply the additions.

``base`` writes the job's environment before its first setup. ``start`` prepares the output directory of one job's
recording on that environment; on Windows it also starts a live Kernel-File ETW session that the committed consumer in
``etw-consumer/build`` reads into one neutral event file. ``provide`` records what a setup gives the entry files that
state it, and installs the lookup report into the site directory of a set-up Python interpreter. ``setups`` runs the
setups of a job that the suite list states as data, each with its one script: before ``start`` those that run outside the
recording, after it the others under the recording, in the suite list's order, and then provides what each script
stated. ``exec`` runs one setup
or one entry file under the recording: on Linux under ``strace -f --seccomp-bpf`` with the mid trace set, or the wide
trace set in the full-recording mode; on Windows as its own process, whose reads ``check`` attributes by process id. An
entry file runs in the job's environment before its setups, with only what its stated setups provide. Every Python
process with the lookup report writes its module lookups beside the recording.
``check`` parses and classifies the recording, maps it to repository paths, assigns each setup's reads to the
suites and entry files of the job that state the setup, and compares the reads with the stored lists. In a
pull-request or push run it writes the additions (with full reads of clean entry files under ``replace_entries``),
lists any additions in the job summary and fails when there are any; in the full-recording mode it writes the
classified reads, the skip and deselected counts and the duration of every entry file. ``keep``
marks the entry files that the additions name in the job's passed list, so that the job stores the passes of its other
entry files and ``store`` keys those with additions only under the lists with the additions applied. ``apply`` applies
a run's additions (replacing stored lists for clean entry files and adding for incomplete ones), or, with the results
of a full recording, verifies any ``--expect-job`` shard names, replaces the lists of every clean entry file that
exited with 0 and whose test report in an accepted format shows no skipped or deselected test, reports partly recorded
suites without aborting complete ones, and writes the duration estimates, corrected for the speed of each shard job;
``--keep-lists`` and ``--keep-all-lists`` name the entry files whose recorded reads it adds instead. The lists of a
suite selected per entry file are the union of its entry files' lists. ``docs/maintaining.md`` documents the commands.

    python3 recording.py base [--file <file>]
    python3 recording.py start [--output <directory>] [--root <repository>] [--environment <file>] [--full [--job <name>]]
    python3 recording.py exec [--output <directory>] --setup <name> -- <command> ...
    python3 recording.py exec [--output <directory>] --entry <file> --suites <suite list> [--root <repository>] [--suite <name>] -- <command> ...
    python3 recording.py provide [--output <directory>] --setup <name> [--path <directory>] ... [--variable <name>=<value>] ... [--python <interpreter>] ... [--write <directory>] ...
    python3 recording.py setups [--output <directory>] --suites <suite list> [--root <repository>] [--work <directory>] --phase before|recorded
                                [--setup <name>] ... [--tool <tool>=<path>] ... [--pinned <tool>=<directory>] ...
    python3 recording.py check [--output <directory>] --suites <suite list> [--root <repository>] [--summary <file>]
    python3 recording.py keep --additions <additions> <passed list>
    python3 recording.py apply [--root <repository>] [--suites <suite list>] [--expect-job <name>] ... [--keep-lists <selection> | --keep-all-lists] <additions or results> ...
    python3 recording.py digest [--expect <digest>] [--github-output <file>]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import functools
import hashlib
import json
import math
import os
import posixpath
import re
import shlex
import shutil
import signal
import stat
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field, replace
from pathlib import Path

SHARED_FOLDER = Path(__file__).resolve().parent.parent  # the folder of the shared CI's parts, in a checkout or a fetched action
# Where this script's imports resolve first: the folder it inserts into sys.path, then its own folder.
MODULE_PATH = (SHARED_FOLDER / "suite-selection", Path(__file__).resolve().parent)
sys.path.insert(0, str(MODULE_PATH[0]))

import recording_modules  # noqa: E402
import suite_list as suite_selection  # noqa: E402

PROCESS_CALLS = ("execve", "execveat", "chdir", "fchdir", "clone", "clone3", "fork", "vfork")
MID_SET = ("open", "openat", "openat2", "getdents", "getdents64", "access", "faccessat", "faccessat2",
           "stat", "lstat", "newfstatat", "statx", "readlink", "readlinkat") + PROCESS_CALLS
WIDE_SET = MID_SET
TRACE_SETS = {"mid": MID_SET, "wide": WIDE_SET}
STRACE = ("strace", "-f", "--seccomp-bpf", "-qq", "-y", "-s", "4096")
# An exec's environment is printed in full, so that check sees the pathspec variables of a git process; exec then keeps
# only those variables of each environment in the trace.
FULL_EXEC_ENVIRONMENT = ("-e", "abbrev=!execve,execveat")
SYSTEM_PREFIXES = ("/usr/", "/lib", "/etc/", "/proc/", "/sys/", "/dev/", "/run/", "/var/", "/bin/", "/sbin/", "/snap/", "/opt/")
OPENS = frozenset({"open", "openat", "openat2"})
STATS = frozenset({"stat", "lstat", "newfstatat", "statx"})
PROBES = frozenset({"access", "faccessat", "faccessat2"})
READLINKS = frozenset({"readlink", "readlinkat"})
EXECS = frozenset({"execve", "execveat"})
FORKS = frozenset({"clone", "clone3", "fork", "vfork"})
LISTINGS = frozenset({"getdents", "getdents64"})
AT_CALLS = frozenset({"openat", "openat2", "newfstatat", "statx", "faccessat", "faccessat2", "readlinkat", "execveat"})
ABSENT_ERRORS = ("ENOENT", "ENOTDIR")
WRITE_FLAGS = ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND", "O_TMPFILE")
STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
COMMAND_EXTENSIONS = (".ps1", ".exe", ".cmd", ".bat", ".com", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msc", ".cpl")  # PATHEXT and PowerShell
POWERSHELL = frozenset({"pwsh", "powershell"})
NODE = frozenset({"node", "tsc"})  # the programs of a Node process, as program_name gives them
MAIN_STREAM = "::$DATA"
# The options that take their value as the next argument: Git's own before the command, git clone's and git worktree add's.
GIT_OPTIONS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix", "--attr-source"})
# Global options of Git that do not alter which tracked paths a command reads.
GIT_SAFE_GLOBAL_FLAGS = frozenset({"--no-pager", "-P", "--paginate", "-p", "--no-optional-locks", "--no-lazy-fetch", "--no-advice",
                                   "--no-replace-objects", "--literal-pathspecs", "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs"})
# Configuration names allowed with ``git -c <name>=<value>`` without falling back to ``**``: none alters which paths are read.
GIT_SAFE_CONFIG = frozenset({
    "user.name", "user.email", "user.signingkey", "commit.gpgsign", "tag.gpgsign",
    "color.ui", "color.diff", "color.status", "color.branch", "color.interactive", "color.pager",
    "core.pager", "core.quotepath", "core.autocrlf", "core.safecrlf", "core.eol", "core.filemode",
    "core.protectntfs", "core.longpaths", "core.usereplacerefs",
    "advice.detachedhead", "safe.directory", "gc.auto", "protocol.file.allow",
})
CLONE_OPTIONS_WITH_VALUE = frozenset({"-o", "-b", "-u", "-c", "-j", "--origin", "--branch", "--upload-pack", "--reference",
                                      "--reference-if-able", "--separate-git-dir", "--depth", "--shallow-since", "--shallow-exclude",
                                      "--config", "--server-option", "--jobs", "--template", "--filter", "--bundle-uri", "--revision",
                                      "--ref-format"})
WORKTREE_ADD_OPTIONS_WITH_VALUE = frozenset({"-b", "-B", "--reason"})
# Beneath a Git directory, or the directory of one of its linked worktrees: the index, the objects and the refs.
INDEX_OBJECTS_OR_REFS = re.compile(r"^(index|objects|refs|packed-refs|[A-Z_]*HEAD)(/|$)")
EVERY_PATH = "**"
# The options of git ls-files that keep its output to the names of tracked paths.
LS_FILES_NAME_OPTIONS = frozenset({"-z", "-c", "--cached", "--full-name", "--deduplicate", "--error-unmatch", "--sparse", "--exclude-standard",
                                   "--pathspec-from-file", "--pathspec-file-nul"})
# The environment variables that change how Git reads pathspecs or replace refs.
GIT_ENV_VARIABLES = frozenset({"GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS",
                               "GIT_NO_REPLACE_OBJECTS"})
# The short magic signs of a pathspec: top, exclude and its other spelling.
SHORT_MAGIC = "/!^"
CAT_FILE_MODES = frozenset({"-t", "-s", "-e", "-p"})  # without one, the first operand is the type
CAT_FILE_SAFE_FLAGS = CAT_FILE_MODES | frozenset({"--allow-unknown-type"})
CAT_FILE_TYPES = frozenset({"blob", "tree", "commit", "tag"})
GIT_SHOW_SAFE_FLAGS = frozenset({"-s", "--no-patch", "-q", "--quiet", "--no-color", "--color", "--no-textconv", "--no-filters"})
GIT_LS_TREE_SAFE_FLAGS = frozenset({"-d", "-r", "-t", "-l", "--long", "-z", "--name-only", "--name-status", "--object-only",
                                    "--full-name", "--full-tree", "--abbrev"})
GIT_ARCHIVE_VALUE_OPTIONS = frozenset({"--format", "-o", "--output", "--prefix", "--add-file", "--add-virtual-file", "--mtime"})
GIT_ARCHIVE_SAFE_FLAGS = frozenset({"-l", "--list", "-v", "--verbose", "--worktree-attributes", "-0", "-1", "-2", "-3", "-4", "-5", "-6", "-7", "-8", "-9"})
GIT_REV_PARSE_NO_PATH_FLAGS = frozenset({
    "--git-dir", "--absolute-git-dir", "--git-common-dir", "--show-toplevel", "--show-superproject-working-tree",
    "--show-prefix", "--show-cdup", "--is-inside-git-dir", "--is-inside-work-tree", "--is-bare-repository",
    "--is-shallow-repository", "-q", "--quiet",
})
GIT_MERGE_BASE_SAFE_FLAGS = frozenset({"--is-ancestor", "--octopus", "--independent", "--all", "-a"})
GIT_DIFF_VALUE_OPTIONS = frozenset({
    "-U", "--unified", "--diff-filter", "--color", "--word-diff", "--word-diff-regex", "--abbrev",
    "--src-prefix", "--dst-prefix", "--line-prefix", "--inter-hunk-context", "--output", "--relative",
    "-M", "--find-renames",
})
GIT_DIFF_SAFE_FLAGS = frozenset({
    "--cached", "--staged", "--name-only", "--name-status", "--stat", "--numstat", "--shortstat", "--raw",
    "-p", "-u", "--patch", "--no-patch", "-s", "-q", "--quiet", "--exit-code", "--no-color", "--color",
    "--no-ext-diff", "--no-textconv", "--no-renames", "-M", "--find-renames", "-w", "--ignore-all-space",
    "-b", "--ignore-space-change", "--ignore-blank-lines", "--no-prefix", "--full-index", "--binary",
    "--check", "-R", "--relative", "--summary", "--compact-summary", "--no-stat", "--patch-with-stat",
})
GIT_ADD_SAFE_FLAGS = frozenset({
    "-n", "--dry-run", "-v", "--verbose", "-f", "--force", "-u", "--update", "-A", "--all", "--no-all",
    "--ignore-removal", "-N", "--intent-to-add", "--refresh", "--ignore-errors", "--ignore-missing", "--renormalize", "--sparse",
})
GIT_RESTORE_VALUE_OPTIONS = frozenset({"-s", "--source"})
GIT_RESTORE_SAFE_FLAGS = frozenset({"-W", "--worktree", "-S", "--staged", "-q", "--quiet", "--ignore-unmerged",
                                    "--overlay", "--no-overlay", "--ours", "--theirs", "-m", "--merge"})
GIT_CHECKOUT_SAFE_FLAGS = frozenset({"-q", "--quiet", "-f", "--force", "--ours", "--theirs", "-m", "--merge",
                                     "--overlay", "--no-overlay", "--ignore-skip-worktree-bits"})
GIT_GREP_VALUE_OPTIONS = frozenset({"-e", "-f", "-m", "--max-count", "-A", "--after-context", "-B", "--before-context",
                                    "-C", "--context", "--max-depth", "--threads", "--color"})
GIT_GREP_SAFE_FLAGS = frozenset({
    "-n", "--line-number", "-l", "--files-with-matches", "-L", "--files-without-match", "-c", "--count",
    "-i", "--ignore-case", "-v", "--invert-match", "-w", "--word-regexp", "-F", "--fixed-strings",
    "-E", "--extended-regexp", "-G", "--basic-regexp", "-P", "--perl-regexp", "-q", "--quiet", "-I",
    "--cached", "-h", "-H", "-o", "--only-matching", "--name-only", "--full-name", "--no-color", "--color",
    "--heading", "--break", "-z", "--null", "--and", "--or", "--not", "-a", "--text",
})
FULL_OBJECT_ID = re.compile(r"^([0-9a-fA-F]{40}|[0-9a-fA-F]{64})(\^\{(blob|tree|commit|tag|object)\})?$")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
UNITTEST_RAN = re.compile(r"^Ran \d+ tests? in ", re.MULTILINE)
UNITTEST_STATUS = re.compile(r"^(OK|FAILED)\b.*$", re.MULTILINE)
PYTEST_OUTCOME_WORDS = r"(?:passed|failed|skipped|deselected|xfailed|xpassed|errors?|subtests? (?:passed|failed|skipped))"
PYTEST_ITEM = rf"\d+ (?:{PYTEST_OUTCOME_WORDS}|warnings?|reruns?)"
PYTEST_OUTCOME = re.compile(rf"\b\d+ {PYTEST_OUTCOME_WORDS}\b")
PYTEST_SUMMARY = re.compile(r"^=+ .* in [\d.]+s\b.*=+$", re.MULTILINE)
PYTEST_QUIET_SUMMARY = re.compile(rf"^{PYTEST_ITEM}(?:, {PYTEST_ITEM})* in \d+(?:\.\d+)?s(?:\s+\(\d+:\d{{2}}:\d{{2}}\))?$", re.MULTILINE)
SKIPPED_COUNT = re.compile(r"skipped=(\d+)")
PYTEST_SKIPPED = re.compile(r"\b(\d+) skipped\b")
PYTEST_DESELECTED = re.compile(r"\b(\d+) deselected\b")
TAP_PLAN = re.compile(r"^1\.\.(\d+)(?:\s+#.*)?$")
TAP_TEST = re.compile(r"^(?:not )?ok(?: (.*))?$")
TAP_SKIP = re.compile(r"(?:^|\s)#\s*skip(?:\s.*)?$", re.IGNORECASE)


@dataclass(frozen=True)
class ReadField:
    """A stored list of a suite: its field, its per-entry field, and whether an empty list stays stored; a suite and an
    entry file always keep their paths, even empty."""

    key: str
    entry_key: str
    kept_empty: bool


READ_FIELDS = (ReadField("paths", "entry_paths", kept_empty=True),
               ReadField("existence_reads", "entry_existence_reads", kept_empty=False),
               ReadField("directory_memberships", "entry_directory_memberships", kept_empty=False),
               ReadField("lookup_directories", "entry_lookup_directories", kept_empty=False))
CONSUMER = Path(__file__).resolve().parent / "etw-consumer" / "build" / "EtwConsumer.dll"
ETW_SESSION = "shared-ci-recording"
ETW_EVENTS = "events.tsv"
ETW_WAIT_SECONDS = 60
ENVIRONMENT = "environment.json"  # the job's environment before its setups, in the output directory
ETW_SUMMARY = re.compile(r"drained_ms=(\d+) events=(\d+) lines=(\d+) lost=(-?\d+)")
WILDCARDS = frozenset('*?<>"')
# The event kinds of the consumer's opens; an attribute-only open, such as CPython's stat, is a stat, as on Linux.
ETW_KINDS = {"open": "read", "openattr": "stat", "opendir": "diropen", "write": "write", "absent": "absent"}
TOOLCHAIN_PROBES = Path(__file__).resolve().parent / "toolchain_probes.json"
LOOKUP_REPORT = Path(__file__).resolve().parent / "shared_lookup_report.py"
LOOKUP_LINE = "shared-lookups.pth"  # the site directory's file whose import line loads the lookup report
LOOKUPS = "SHARED_LOOKUPS"  # the variable that names the directory of a unit's lookup reports


class RecordingError(Exception):
    """The recording or its inputs cannot be used; the message says why."""


# --- the output directory -----------------------------------------------------------------------------------


def default_output() -> Path | None:
    temporary = os.environ.get("RUNNER_TEMP")
    return Path(temporary) / "recording" if temporary else None


def output_directory(value: Path | None) -> Path:
    output = value or default_output()
    if output is None:
        raise RecordingError("name the output directory with --output; RUNNER_TEMP is not set")
    return output.resolve()


def read_settings(output: Path) -> dict:
    try:
        return json.loads((output / "recording.json").read_text(encoding="utf-8"))
    except OSError as error:
        raise RecordingError(f"{output} holds no recording: run start first ({error})") from error


def default_environment() -> Path | None:
    temporary = os.environ.get("RUNNER_TEMP")
    return Path(temporary) / "recording-environment.json" if temporary else None


def run_base(options: argparse.Namespace) -> int:
    target = options.file or default_environment()
    if target is None:
        raise RecordingError("name the file with --file; RUNNER_TEMP is not set")
    target.write_text(json.dumps(dict(os.environ), indent=2) + "\n", encoding="utf-8")
    return 0


def job_environment(options: argparse.Namespace) -> dict[str, str]:
    """The job's environment before its setups: the snapshot that base wrote, or the current one without a snapshot. The
    recording step must run on the snapshot's interpreter, which no setup replaces."""
    snapshot = options.environment or default_environment()
    if snapshot is None or not snapshot.is_file():
        return dict(os.environ)
    environment = json.loads(snapshot.read_text(encoding="utf-8"))
    name = "python" if sys.platform == "win32" else "python3"
    own = shutil.which(name, path=environment.get("PATH", ""))
    if own is None or os.path.realpath(own) != os.path.realpath(sys.executable):
        raise RecordingError(f"the recording step runs on {sys.executable}, but {name} of the job's environment before its setups is {own}; "
                             f"run it with the job's own {name}")
    return environment


def run_start(options: argparse.Namespace) -> int:
    windows = sys.platform == "win32"
    if not windows and not sys.platform.startswith("linux"):
        raise RecordingError("the recording step records on Linux and Windows; this system is " + sys.platform)
    environment = job_environment(options)
    tool = shutil.which("dotnet" if windows else "strace", path=environment.get("PATH", ""))
    if not windows and tool is None:
        raise RecordingError("strace is not on PATH; the recording step needs strace on Linux and installs nothing")
    if windows and tool is None:
        raise RecordingError("dotnet is not on PATH; the recording step runs its ETW consumer on the .NET runtime and installs nothing")
    output = output_directory(options.output)
    if output.exists() and any(output.iterdir()):
        if not (output / "recording.json").is_file():
            raise RecordingError(f"{output} is not empty and holds no recording; name an empty or new directory")
        shutil.rmtree(output)
    for part in ("units", "traces", "lookups", "logs", "work", "setups"):
        (output / part).mkdir(parents=True, exist_ok=True)
    (output / ENVIRONMENT).write_text(json.dumps(environment, indent=2) + "\n", encoding="utf-8")
    mode = "full" if options.full else "pull-request"
    settings = {"mode": mode, "trace_set": "wide" if options.full else "mid", "strace": tool}
    if windows:
        settings = {"mode": mode, "trace_set": "etw", **start_session(output, options.root, tool, environment)}
    if options.job:
        settings["job"] = options.job
    (output / "recording.json").write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    recorder = "a live Kernel-File ETW session" if windows else f"the {settings['trace_set']} trace set"
    print(f"Recording into {output} with {recorder} ({settings['mode']} mode).")
    return 0


def dos_devices() -> dict[str, str]:
    """The NT device of every drive letter, such as ``\\Device\\HarddiskVolume3`` for ``D:``."""
    import ctypes

    kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
    devices = {}
    mask = kernel.GetLogicalDrives()
    for index in range(26):
        if mask & (1 << index):
            drive = chr(ord("A") + index) + ":"
            buffer = ctypes.create_unicode_buffer(1024)
            if kernel.QueryDosDeviceW(drive, buffer, len(buffer)):
                devices[buffer.value] = drive
    return devices


def device_path(path: str, devices: dict[str, str]) -> str:
    drives = {drive.upper(): device for device, drive in devices.items()}
    device = drives.get(path[:2].upper())
    if device is None:
        raise RecordingError(f"{path} lies on no drive with an NT device")
    return device + path[2:]


def start_session(output: Path, root: Path, dotnet: str, environment: dict[str, str]) -> dict:
    """Start the ETW consumer on the checkout and the output directory and wait until its session runs. The checkout
    counts in its given and in its resolved form, since a process names a path in the form it was given."""
    if not CONSUMER.is_file():
        raise RecordingError(f"the committed ETW consumer {CONSUMER} is missing")
    devices = dos_devices()
    roots = list(dict.fromkeys((os.path.abspath(root), str(root.resolve()))))
    # A linked worktree's Git directory and common directory lie outside the checkout.
    outside = [directory for directory in git_directories(root.resolve())
               if not any(directory.lower().startswith(path.lower() + os.sep) for path in roots)]
    ready, stop, log = output / "logs" / "etw.ready", output / "logs" / "etw.stop", output / "logs" / "etw.log"
    command = [dotnet, str(CONSUMER), f"{ETW_SESSION}-{os.getpid()}", str(output / ETW_EVENTS), str(ready), str(stop),
               *(device_path(path, devices) for path in roots + outside), device_path(str(output), devices)]
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS  # type: ignore[attr-defined]
    with log.open("wb") as handle:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, creationflags=flags, env=environment)
    deadline = time.monotonic() + ETW_WAIT_SECONDS
    while not ready.is_file():
        if process.poll() is not None:
            raise RecordingError(f"the ETW consumer exited with {process.returncode} before its session ran; it needs administrator "
                                 "rights or, without elevation, an account that the one-time setup tests/etw_access.ps1 of "
                                 f"CrankyAnt/SharedWorkflows prepared: {log.read_text(encoding='utf-8', errors='replace').strip()}")
        if time.monotonic() > deadline:
            raise RecordingError(f"the ETW session did not start within {ETW_WAIT_SECONDS} s")
        time.sleep(0.05)
    return {"roots": roots, "git_directories": outside, "devices": devices, "events": ETW_EVENTS, "consumer": process.pid,
            "consumer_started": process_started(process.pid)}


# --- exec ---------------------------------------------------------------------------------------------------


def tap_report(output: str) -> tuple[str | None, int] | None:
    """Why the TAP report in an entry file's output is incomplete, or None when it is complete, and the number of its
    skipped tests; None when the output has no TAP plan. The report is complete when it has one plan and as many test
    lines as that plan states. A plan of ``1..0`` skips the whole entry file, which counts as one skipped test."""
    plans, tests, skipped = [], 0, 0
    for line in output.splitlines():
        line = line.rstrip()
        if plan := TAP_PLAN.match(line):
            plans.append(int(plan.group(1)))
        elif test := TAP_TEST.match(line):
            tests += 1
            skipped += bool(TAP_SKIP.search(test.group(1) or ""))
    if not plans:
        return None
    if len(plans) > 1:
        return "its TAP report has more than one plan", skipped
    if plans[0] != tests:
        return "its TAP plan does not match its test lines", skipped
    return None, (skipped if plans[0] else 1)


def skip_count(output: str) -> tuple[int | None, int, str | None]:
    """The number of skipped tests and deselected tests that the entry file's complete test reports in an accepted
    format state, or None for skipped when it has none, and, when it has none, why its TAP report is incomplete. An
    incomplete TAP report counts as no report."""
    plain = ANSI_ESCAPE.sub("", output).replace("\r\n", "\n")
    tap = tap_report(plain)
    counts = [tap[1]] if tap is not None and tap[0] is None else []
    deselected = 0
    if UNITTEST_RAN.search(plain):
        lines = [match.group(0) for match in UNITTEST_STATUS.finditer(plain)]
        counts += [int(found.group(1)) if (found := SKIPPED_COUNT.search(line)) else 0 for line in lines]
    for pattern in (PYTEST_SUMMARY, PYTEST_QUIET_SUMMARY):
        for line in pattern.findall(plain):
            if not PYTEST_OUTCOME.search(line):
                continue
            found_skipped = PYTEST_SKIPPED.search(line)
            counts.append(int(found_skipped.group(1)) if found_skipped else 0)
            found_deselected = PYTEST_DESELECTED.search(line)
            if found_deselected:
                deselected += int(found_deselected.group(1))
    if counts:
        return sum(counts), deselected, None
    return None, 0, None if tap is None else tap[0]


def provisions(output: Path) -> dict[str, dict]:
    """What each setup installed in this job provides, by setup name."""
    return {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted((output / "setups").glob("*.json"))}


def no_provision() -> dict:
    """What a setup that gives the entry files nothing provides."""
    return {"paths": [], "variables": {}}


def write_provision(output: Path, setup: str, provision: dict) -> None:
    """Record a setup as installed in this job, with what it provides."""
    (output / "setups" / f"{setup}.json").write_text(json.dumps(provision, indent=2) + "\n", encoding="utf-8")


def run_provide(options: argparse.Namespace) -> int:
    output = output_directory(options.output)
    read_settings(output)
    provide(output, options.setup, options.path, options.variable, options.python, options.write)
    return 0


def provide(output: Path, setup: str, paths: list[str], variables: list[str], interpreters: list[str], writes: Collection[str] = ()) -> None:
    """Record what a setup installed in this job provides: its tool directories, its variables, its writable directory
    grants and, for each Python interpreter, the lookup report in its site directory."""
    provision = provisions(output).get(setup) or no_provision()
    for path in paths:
        if not os.path.isabs(path) or not os.path.isdir(path):
            raise RecordingError(f"the tool directory {path} of the setup {setup} is missing or not absolute")
        provision["paths"] = add_unique(provision["paths"], [path])
    for item in variables:
        name, separator, value = item.partition("=")
        if not name or not separator:
            raise RecordingError(f"a variable of the setup {setup} takes <name>=<value>, not {item}")
        provision["variables"][name] = value
    for interpreter in interpreters:
        install_lookup_report(interpreter)
    for path in writes:
        if not os.path.isabs(path) or not os.path.isdir(path):
            raise RecordingError(f"the writable directory {path} of the setup {setup} is missing or not absolute")
        provision["writes"] = add_unique(provision.get("writes", []), [path])
    write_provision(output, setup, provision)


# --- the setups that the suite list states as data ------------------------------------------------------------


SCRIPT_RUNNERS = {".sh": ("bash",), ".ps1": ("pwsh", "-NoProfile", "-NonInteractive", "-File")}
PROVISIONS = {"path": "path=<directory>", "variable": "variable=<name>=<value>", "python": "python=<interpreter>", "write": "write=<directory>"}


def script_command(script: str) -> list[str]:
    """The command that runs a setup script: the job's own Python for ``.py``, Bash for ``.sh``, PowerShell for ``.ps1``."""
    return [*SCRIPT_RUNNERS.get(Path(script).suffix, (sys.executable,)), script]


def stated_provisions(file: Path, setup: str) -> tuple[list[str], list[str], list[str], list[str]]:
    """The tool directories, variables, Python interpreters and writable directories that a setup script wrote to its
    provisions file."""
    found: dict[str, list[str]] = {key: [] for key in PROVISIONS}
    for line in file.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.strip().partition("=")
        if not line.strip():
            continue
        if not separator or key not in found or not value:
            forms = list(PROVISIONS.values())
            raise RecordingError(f"the setup {setup} states `{line.strip()}`; a line is {', '.join(forms[:-1])} or {forms[-1]}")
        found[key].append(value)
    return found["path"], found["variable"], found["python"], found["write"]


def default_work() -> Path:
    temporary = os.environ.get("RUNNER_TEMP")
    if not temporary:
        raise RecordingError("name the directory of the setups with --work; RUNNER_TEMP is not set")
    return Path(temporary) / "setups"


def run_setups(options: argparse.Namespace) -> int:
    """Run the setups that the job names, as the suite list defines them and in its order: with ``--phase before`` those
    that run before the recording starts, outside it; with ``--phase recorded``, after ``start``, the others under the
    recording, and then record what every setup of the job provides. A pinned tool is a setup without a script, which
    the job installed itself: ``--pinned`` names its directory, which the entry files that state it get on their PATH."""
    root = options.root.resolve()
    suite_list = suite_selection.load_suite_list(options.suites)
    pinned = dict(item.partition("=")[::2] for item in options.pinned)
    for name, directory in pinned.items():
        if name not in suite_selection.PINNED_TOOLS or not directory:
            raise RecordingError(f"--pinned takes <tool>=<directory> with one of {', '.join(suite_selection.PINNED_TOOLS)}, not {name}={directory}")
        if name in suite_list.setups_by_name:
            raise RecordingError(f"the suite list {options.suites} defines the setup {name}, so it is no pinned tool of this job")
    undefined = [name for name in options.setup if name not in suite_list.setups_by_name and name not in pinned]
    if undefined:
        raise RecordingError(f"the suite list {options.suites} defines no setup {undefined[0]}; state it under setups with its script")
    work = (options.work or default_work()).resolve()
    tools = dict(item.partition("=")[::2] for item in options.tool)
    recorded_phase = options.phase == "recorded"
    output = output_directory(options.output) if recorded_phase else None
    if output is not None:
        read_settings(output)
    setups = [setup for setup in suite_list.setups or () if setup.name in options.setup]
    for setup in setups:
        provisions_file = work / f"{setup.name}.provides"
        if setup.recorded != recorded_phase:
            if recorded_phase and not provisions_file.is_file():
                raise RecordingError(f"the setup {setup.name} runs before the recording starts: run setups --phase before ahead of start")
            continue
        if setup.tool is not None and not tools.get(setup.tool):
            raise RecordingError(f"the setup {setup.name} names the tool {setup.tool}, which this job did not install")
        directory = work / setup.name
        directory.mkdir(parents=True, exist_ok=True)
        provisions_file.write_text("", encoding="utf-8")
        given = {"SETUP_NAME": setup.name, "SETUP_DIRECTORY": str(directory), "SETUP_PROVIDES": str(provisions_file)}
        if setup.tool is not None:
            given["SETUP_TOOL"] = tools[setup.tool]
        command = script_command(setup.script)
        kept = {name: os.environ.get(name) for name in (*given, "SETUP_TOOL")}
        os.environ.pop("SETUP_TOOL", None)
        os.environ.update(given)
        try:
            if setup.recorded:
                code = run_exec(argparse.Namespace(output=options.output, setup=setup.name, entry=None, suite=None, suites=None, root=root,
                                                   arguments=command, directory=root))
            else:
                code = subprocess.run(command, cwd=root, check=False).returncode
        finally:
            for name, value in kept.items():
                os.environ.pop(name, None)
                if value is not None:
                    os.environ[name] = value
        if code != 0:
            print(f"the setup {setup.name} failed with exit code {code}", file=sys.stderr)
            return code
    if output is not None:
        for setup in setups:
            provide(output, setup.name, *stated_provisions(work / f"{setup.name}.provides", setup.name))
        for name in options.setup:
            if name in pinned:
                provide(output, name, [pinned[name]], [], [])
        if sys.platform != "win32":
            snapshot_user_cache(output)
    return 0


USER_CACHE = "user-cache"


def snapshot_user_cache(output: Path) -> Path:
    """Snapshot the job's Linux per-user cache into ``output / USER_CACHE`` after the setups finish."""
    snapshot = output / USER_CACHE
    if snapshot.exists():
        remove_tree(snapshot)
    job_env = json.loads((output / ENVIRONMENT).read_text(encoding="utf-8"))
    copy_user_cache(user_cache_source(job_env), snapshot, exclude=output)
    return snapshot


def prepare_user_cache(output: Path, work: Path, base: dict[str, str]) -> Path:
    """Prepare one unit's per-user cache under ``work / USER_CACHE`` from the post-setup snapshot or ``base``."""
    user_cache = work / USER_CACHE
    job_env = json.loads((output / ENVIRONMENT).read_text(encoding="utf-8"))
    snapshot = output / USER_CACHE
    source = user_cache_source(base)
    if snapshot.is_dir() and source == user_cache_source(job_env):
        source = snapshot
    copy_user_cache(source, user_cache, exclude=output)
    return user_cache


def user_cache_source(environment: dict[str, str]) -> Path | None:
    """The Linux per-user cache directory for ``environment``: ``XDG_CACHE_HOME`` when set to an absolute path,
    otherwise ``$HOME/.cache``."""
    configured = environment.get("XDG_CACHE_HOME", "")
    if configured and os.path.isabs(configured):
        return Path(configured)
    home = environment.get("HOME", "")
    if home:
        return Path(home) / ".cache"
    try:
        return Path.home() / ".cache"
    except RuntimeError:
        return None


def copy_user_cache(source: Path | None, destination: Path, *, exclude: Path | None = None) -> None:
    """Create ``destination`` as an independent copy of ``source`` when it exists as a directory, or an empty
    directory otherwise. Preserves symlinks and skips non-regular special files and ``exclude`` when it lies inside
    ``source``."""
    if source is None or not source.is_dir():
        destination.mkdir(parents=True, exist_ok=True)
        return
    resolved_exclude = exclude.resolve() if exclude is not None else None

    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored: set[str] = set()
        current = Path(directory)
        for name in names:
            candidate = current / name
            try:
                mode = os.lstat(candidate).st_mode
            except OSError:
                ignored.add(name)
                continue
            if stat.S_ISLNK(mode):
                continue
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                ignored.add(name)
                continue
            if resolved_exclude is not None and stat.S_ISDIR(mode) and candidate.resolve() == resolved_exclude:
                ignored.add(name)
        return ignored

    shutil.copytree(source, destination, symlinks=True, ignore=ignore, dirs_exist_ok=True)


def install_lookup_report(interpreter: str) -> Path:
    """Install the lookup report into the site directory of a Python interpreter: the report module, and a ``.pth`` file
    whose import line loads it, so that no directory joins the module search path. Returns the site directory."""
    found = subprocess.run([interpreter, "-I", "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                           capture_output=True, text=True, encoding="utf-8", check=False)
    if found.returncode != 0 or not found.stdout.strip():
        raise RecordingError(f"the Python interpreter {interpreter} names no site directory: {found.stderr.strip()}")
    site = Path(found.stdout.strip())
    site.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(LOOKUP_REPORT, site / LOOKUP_REPORT.name)
    (site / LOOKUP_LINE).write_text(f"import {LOOKUP_REPORT.stem}\n", encoding="utf-8")
    return site


def entry_environment(output: Path, entry: str, stated: tuple[str, ...]) -> dict[str, str]:
    """The job's environment before its setups, with what the setups that the entry file states provide: their tool
    directories first on PATH, in the order of the stated setups, and their variables."""
    provided = provisions(output)
    missing = [setup for setup in stated if setup not in provided]
    if missing:
        raise RecordingError(f"the setup {missing[0]} that {entry} states was not installed in this job; install it before the entry files run")
    environment = json.loads((output / ENVIRONMENT).read_text(encoding="utf-8"))
    paths = [path for setup in stated for path in provided[setup]["paths"]]
    environment["PATH"] = os.pathsep.join(paths + [path for path in environment.get("PATH", "").split(os.pathsep) if path])
    for setup in stated:
        environment.update(provided[setup]["variables"])
    return environment


def stated_setups(suites: Path, root: Path, entry: str, suite: str | None) -> tuple[str, ...]:
    """The setups that the suite list states for the entry file: its suite's and its own."""
    suite_list = suite_selection.resolve_entries(suite_selection.load_suite_list(suites), set(checkout(root).tracked), str(suites))
    return suite_of(suite_list, {"name": entry, "suite": suite}).setups_of(entry)


TIMEOUT_EXIT_CODE = 124


def terminate_process_tree(process: subprocess.Popen) -> None:
    """Kill ``process`` and every descendant it started."""
    if sys.platform == "win32":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, check=False, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        if process.poll() is None:
            process.kill()


def process_elapsed_seconds() -> float | None:
    """Wall seconds elapsed since the OS created this process, or None when unavailable."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            ]
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.GetSystemTimePreciseAsFileTime.argtypes = [ctypes.POINTER(wintypes.FILETIME)]
            creation, exited, kernel, user, now = (
                wintypes.FILETIME(),
                wintypes.FILETIME(),
                wintypes.FILETIME(),
                wintypes.FILETIME(),
                wintypes.FILETIME(),
            )
            if not kernel32.GetProcessTimes(
                kernel32.GetCurrentProcess(), ctypes.byref(creation), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
            ):
                return None
            kernel32.GetSystemTimePreciseAsFileTime(ctypes.byref(now))
            created_100ns = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            now_100ns = (now.dwHighDateTime << 32) | now.dwLowDateTime
            return max(0.0, (now_100ns - created_100ns) / 1e7)
        except (AttributeError, OSError, ValueError):
            return None
    if sys.platform.startswith("linux"):
        try:
            boottime_clock = getattr(time, "CLOCK_BOOTTIME", None)
            if boottime_clock is None:
                return None
            stat_text = Path("/proc/self/stat").read_text(encoding="utf-8")
            fields = stat_text[stat_text.rindex(")") + 2:].split()
            start_ticks = int(fields[19])
            clk_tck = os.sysconf("SC_CLK_TCK")
            if clk_tck <= 0:
                return None
            return max(0.0, time.clock_gettime(boottime_clock) - start_ticks / clk_tck)
        except (AttributeError, IndexError, OSError, ValueError):
            return None
    return None


def run_exec(options: argparse.Namespace) -> int:
    started = time.perf_counter()
    startup = getattr(options, "_process_startup_seconds", 0.0)
    output = output_directory(options.output)
    settings = read_settings(output)
    command = list(options.arguments)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise RecordingError("exec needs a command after --")
    if options.entry is not None:
        entry = normalize_entry(options.entry)
        base = entry_environment(output, entry, stated_setups(options.suites, options.root, entry, options.suite))
    else:
        base = dict(os.environ)
    unit = f"{time.time_ns()}-{os.getpid()}"
    trace = output / "traces" / f"{unit}.strace"
    work = output / "work" / unit
    temporary, cache = work / "tmp", work / "pycache"
    temporary.mkdir(parents=True)
    cache.mkdir(parents=True)
    lookups = output / "lookups" / unit
    lookups.mkdir(parents=True)
    base = dict(base, PYTHONPYCACHEPREFIX=str(cache), **{LOOKUPS: str(lookups)})
    etw = settings["trace_set"] == "etw"
    user_cache = prepare_user_cache(output, work, base) if not etw and options.entry is not None else None
    if etw:
        # The live session records every process; check attributes the reads to this unit by its process id.
        recorder: list[str] = []
        environment = dict(base, TMPDIR=str(temporary), TEMP=str(temporary), TMP=str(temporary))
        command[0] = shutil.which(command[0], path=environment["PATH"]) or command[0]
    else:
        trace_set = TRACE_SETS[settings["trace_set"]]
        recorder = [settings["strace"], *STRACE[1:], "-o", str(trace), "-e", "trace=" + ",".join("?" + call for call in trace_set), *FULL_EXEC_ENVIRONMENT, "--"]
        environment = dict(base, TMPDIR=str(temporary))
        if user_cache is not None:
            environment["XDG_CACHE_HOME"] = str(user_cache)
    log = bytearray()
    timeout = getattr(options, "timeout", None)
    timed_out = False
    with subprocess.Popen(recorder + command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment,
                          cwd=getattr(options, "directory", None), start_new_session=(sys.platform != "win32")) as process:
        assert process.stdout is not None

        def drain() -> None:
            assert process.stdout is not None
            for chunk in iter(lambda: process.stdout.read1(65536), b""):
                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()
                log.extend(chunk)

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate_process_tree(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            code = TIMEOUT_EXIT_CODE
        finally:
            reader.join(timeout=5)
    if timed_out:
        unit_name = options.setup or normalize_entry(options.entry)
        label = f"{unit_name} ({options.suite})" if options.suite else unit_name
        print(f"{label} timed out after {timeout:g} s", file=sys.stderr, flush=True)
    if not etw and trace.is_file():
        keep_git_env_variables(trace)
    text = log.decode("utf-8", "replace")
    skipped, deselected, incomplete = skip_count(text)
    (output / "logs" / f"{unit}.log").write_text(text, encoding="utf-8")
    remove_work(work)
    seconds = startup + (time.perf_counter() - started)
    record = {"unit": unit, "kind": "setup" if options.setup else "entry", "name": options.setup or normalize_entry(options.entry),
              "suite": options.suite, "command": command, "exit_code": code, "seconds": round(seconds, 3), "skipped": skipped,
              "deselected": deselected, "incomplete_report": incomplete,
              "trace": trace.name, "lookups": lookups.name, "temporary": str(temporary), "cache": str(cache),
              "cwd": str(getattr(options, "directory", None) or os.getcwd())}
    if user_cache is not None:
        record["user_cache"] = str(user_cache)
    if timed_out:
        record["timed_out"] = True
    if "job" in settings:
        record["job"] = settings["job"]
    if etw:
        record.update(trace=settings["events"], pid=process.pid, launcher=os.getpid(), recorder=["etw"])
    else:
        record["recorder"] = [*STRACE, *recorder[len(STRACE) + 2:-1]]
    (output / "units" / f"{unit}.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if options.setup and code == 0 and options.setup not in provisions(output):
        write_provision(output, options.setup, no_provision())
    return code


# Windows errors of a removal that another process's open handle or a pending delete causes, which end on their own:
# ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION and ERROR_DIR_NOT_EMPTY.
TRANSIENT_REMOVAL_ERRORS = frozenset({5, 32, 33, 145})
REMOVAL_RETRY_SECONDS = 5.0
REMOVAL_RETRY_PAUSE = 0.05


def remove_work(work: Path) -> None:
    """Remove a unit's work folder after its record is written. A folder that cannot be removed yields a warning that
    names it and the error, as an annotation on a runner; the unit's exit code and record stay as they are."""
    try:
        remove_tree(work)
    except OSError as error:
        message = f"cannot remove the unit's work folder {work}: {error}"
        if os.environ.get("GITHUB_ACTIONS") == "true":
            escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print(f"::warning::{escaped}", flush=True)
        else:
            print(f"warning: {message}", file=sys.stderr, flush=True)


def remove_tree(path: Path) -> None:
    """Remove the directory tree ``path``. A file or directory that cannot be removed loses its read-only attribute, as
    Git's object files carry on Windows, and on Linux its parent directory becomes writable, before another try; on
    Windows a removal that another process's handle blocks is retried for up to ``REMOVAL_RETRY_SECONDS``. A path that
    vanished meanwhile counts as removed; a directory that cannot be listed or opened gets one more try. Any other
    failure raises."""
    deadline = time.monotonic() + REMOVAL_RETRY_SECONDS
    recovered: set[str] = set()

    def retry(function: Callable, name: str) -> None:
        while True:
            make_writable(name)
            try:
                function(name)
                return
            except FileNotFoundError:
                return
            except OSError as error:
                if sys.platform == "win32" and getattr(error, "winerror", None) in TRANSIENT_REMOVAL_ERRORS and time.monotonic() < deadline:
                    time.sleep(REMOVAL_RETRY_PAUSE)
                    continue
                raise

    def recover(function: Callable, name: str, error: BaseException) -> None:
        if isinstance(error, FileNotFoundError):
            return
        if function in (os.unlink, os.remove, os.rmdir):
            retry(function, name)
            return
        if not isinstance(error, PermissionError) or name in recovered:
            raise error
        # A directory that could not be listed or opened: make it accessible and remove it with its contents, once.
        recovered.add(name)
        make_writable(name)
        shutil.rmtree(name, onexc=recover)

    try:
        shutil.rmtree(path, onexc=recover)
    except FileNotFoundError:
        return


def make_writable(name: str) -> None:
    """Clear the read-only attribute of ``name``; on Linux also make it and its parent directory accessible."""
    try:
        mode = os.lstat(name).st_mode
        if stat.S_ISLNK(mode):
            return
        wanted = mode | stat.S_IWUSR | stat.S_IRUSR | (stat.S_IXUSR if stat.S_ISDIR(mode) else 0)
        if wanted != mode:
            os.chmod(name, stat.S_IMODE(wanted))
        if sys.platform != "win32":
            parent = os.path.dirname(name) or "."
            parent_mode = os.stat(parent).st_mode
            if parent_mode & (stat.S_IWUSR | stat.S_IXUSR) != stat.S_IWUSR | stat.S_IXUSR:
                os.chmod(parent, stat.S_IMODE(parent_mode | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRUSR))
    except OSError:
        pass


def normalize_entry(entry: str) -> str:
    normalized = posixpath.normpath(entry.replace("\\", "/"))
    return normalized[2:] if normalized.startswith("./") else normalized


# --- parsing --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    pid: str
    executable: str  # the base name of the program the process ran when it made the call
    # read, exec, exec-absent, stat, statdir, exists, absent, diropen, list (one per listing of a directory); on Windows an
    # enumeration of one literal name that finds it is a stat
    kind: str
    path: str  # absolute and normalized
    cwd: str | None = None  # the working directory the recording knows for the event's process, None where it knows none
    # Of an exec: the argument vector, its absolute paths in the form of ``path``; None when the recording lacks it.
    arguments: tuple[str, ...] | None = ()
    # Of an exec: the entries of its environment that the trace keeps, the pathspec variables; None when the recording
    # lacks its environment.
    environment: tuple[str, ...] | None = None


def array_spans(text: str) -> list[tuple[int, int]]:
    """The spans of the arrays in the arguments of a strace call, each from its ``[`` to its ``]`` outside the quoted
    strings."""
    spans: list[tuple[int, int]] = []
    start, quoted, index = None, False, 0
    while index < len(text):
        character = text[index]
        if quoted:
            if character == "\\":
                index += 1
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character == "[":
            start = index
        elif character == "]" and start is not None:
            spans.append((start, index + 1))
            start = None
        index += 1
    return spans


def with_git_env_variables_only(line: str) -> str:
    """A strace line with only the Git pathspec and replace-ref variables left in the environment of an exec."""
    space = line.find(" ")
    paren = line.find("(", space + 1)
    if space <= 0 or paren < 0 or line[space + 1:paren].strip() not in EXECS:
        return line
    spans = array_spans(line[paren:])
    if len(spans) < 2:
        return line
    start, end = paren + spans[1][0], paren + spans[1][1]
    kept = [item for item in STRING.findall(line[start:end]) if is_git_env_variable(unescape(item))]
    return line[:start] + "[" + ", ".join(f'"{item}"' for item in kept) + "]" + line[end:]


def is_git_env_variable(entry: str) -> bool:
    """Whether an environment entry ``<name>=<value>`` sets a kept Git environment variable."""
    return entry.split("=", 1)[0] in GIT_ENV_VARIABLES


def keep_git_env_variables(trace: Path) -> None:
    """Keep only the Git pathspec and replace-ref variables in the environment of each exec in ``trace``."""
    kept = trace.with_name(trace.name + ".kept")
    with trace.open("r", encoding="utf-8", errors="surrogateescape") as source, \
            kept.open("w", encoding="utf-8", errors="surrogateescape", newline="") as target:
        for line in source:
            target.write(with_git_env_variables_only(line))
    kept.replace(trace)


def unescape(text: str) -> str:
    if "\\" not in text:
        return text
    try:
        return bytes(text, "latin-1").decode("unicode_escape").encode("latin-1").decode("utf-8", "replace")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return text


def normalize(path: str) -> str:
    parts: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if parts:
                parts.pop()
            continue
        parts.append(segment)
    return "/" + "/".join(parts)


def resolve(base: str, path: str) -> str:
    return normalize(path if path.startswith("/") else base + "/" + path)


def descriptor_path(argument: str) -> str | None:
    """The path that ``-y`` prints for a descriptor argument such as ``3</dir>`` or ``AT_FDCWD</cwd>``."""
    start = argument.find("<")
    if start < 0 or not argument.endswith(">"):
        return None
    return argument[start + 1:-1]


def fork_parents(trace: Path) -> dict[str, str]:
    """The parent of each process that a clone or fork in one strace output returns, by the first such return."""
    parents: dict[str, str] = {}
    with trace.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            pid, _, body = raw.partition(" ")
            body = body.strip()
            call = body[len("<... "):].split(" ", 1)[0] if body.startswith("<... ") else body.split("(", 1)[0]
            if pid.isdigit() and call in FORKS and " = " in body:
                child = body.rsplit(" = ", 1)[1].split(" ", 1)[0]
                if child.isdigit() and child != "0":
                    parents.setdefault(child, pid)
    return parents


def parse_trace(trace: Path, keep: tuple[str, ...], start: str) -> list[Event]:
    """The file events of one strace output, by string operations. Lines whose path lies under a system directory are
    skipped first, unless the path lies under one of ``keep`` (the checkout and the temporary directory). ``start`` is the
    working directory of the recorded command. A listing is the first ``getdents`` call on a descriptor since the open
    that returned it. A process whose own lines come before its parent's clone or fork return starts in the parent's
    working directory, which the parent cannot change before that return."""
    skip = tuple(prefix for prefix in SYSTEM_PREFIXES if not any((path + "/").startswith(prefix) for path in keep))
    parents = fork_parents(trace)
    cwd: dict[str, str] = {}
    executable: dict[str, str] = {}
    pending: dict[str, str] = {}
    listed: set[tuple[str, str]] = set()  # (process, descriptor) of each listing since the descriptor's open
    events: list[Event] = []
    with trace.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            space = raw.find(" ")
            pid = raw[:space]
            if space <= 0 or not pid.isdigit():
                continue
            body = raw[space + 1:].strip()
            if body.endswith("<unfinished ...>"):
                pending[pid] = body[:-len("<unfinished ...>")].rstrip()
                continue
            if body.startswith("<... "):
                resumed = body.find(" resumed>")
                if resumed < 0 or pid not in pending:
                    continue
                body = pending.pop(pid) + body[resumed + len(" resumed>"):]
            paren = body.find("(")
            if paren <= 0:
                continue
            call = body[:paren]
            if call not in OPENS and call not in STATS and call not in PROBES and call not in READLINKS and call not in EXECS \
                    and call not in FORKS and call not in LISTINGS and call not in ("chdir", "fchdir"):
                continue
            rest = body[paren + 1:]
            if skip and (call in OPENS or call in STATS or call in PROBES):
                quote = rest.find('"')
                if quote >= 0 and rest.startswith(skip, quote + 1):
                    continue
            equals = rest.rfind(" = ")
            head = rest[:equals].rstrip() if equals >= 0 else ""
            if not head.endswith(")"):
                continue
            arguments, result = head[:-1], rest[equals + 3:]
            value = result.split(" ", 1)[0]
            returned = value[:value.find("<")] if "<" in value else value
            if not (returned.lstrip("-").isdigit() or returned == "?"):
                continue
            ok = returned != "-1"
            error = result.split(" ", 2)[1] if not ok and " " in result else None
            if ok and call in OPENS:
                listed.discard((pid, returned))
            unplaced, ancestor = [], pid
            while ancestor not in cwd and ancestor in parents and ancestor not in unplaced:
                unplaced.append(ancestor)
                ancestor = parents[ancestor]
            for child in reversed(unplaced):
                cwd[child] = cwd.get(parents[child], start)
                executable.setdefault(child, executable.get(parents[child], "?"))
            here = cwd.get(pid, start)
            if call in FORKS:
                if ok and returned.isdigit() and returned != "0":
                    # A child whose own lines came first has its place already; otherwise it starts here, whatever
                    # the parent does after this return.
                    cwd.setdefault(returned, here)
                    executable.setdefault(returned, executable.get(pid, "?"))
                continue
            if call == "fchdir":
                path = descriptor_path(arguments.strip())
                if ok and path:
                    cwd[pid] = normalize(path)
                continue
            first = arguments.split(",", 1)[0].strip()
            base = None
            if call in AT_CALLS:
                base = descriptor_path(first)
                if base is not None and first.startswith("AT_FDCWD"):
                    cwd[pid] = here = normalize(base)
                tail = arguments.split(",", 1)[1] if "," in arguments else ""
                found = STRING.search(tail)
                flags = tail
            else:
                found = STRING.match(arguments.strip())
                flags = arguments
            if call in LISTINGS:
                path = descriptor_path(first)
                descriptor = (pid, first.split("<", 1)[0])
                if ok and path and descriptor not in listed:
                    listed.add(descriptor)
                    events.append(Event(pid, executable.get(pid, "?"), "list", normalize(path), cwd=here))
                continue
            if found is None:
                continue
            name = unescape(found.group(1))
            if base is None:
                base = here
            path = normalize(base) if name == "" and "AT_EMPTY_PATH" in flags else resolve(base, name)
            if call == "chdir":
                if ok:
                    cwd[pid] = path
                continue
            if call in EXECS:
                if ok:
                    executable[pid] = posixpath.basename(path)
                    arrays = [arguments[start:end] for start, end in array_spans(arguments)]
                    argv = [unescape(item) for item in STRING.findall(arrays[0])] if arrays else []
                    environment = tuple(unescape(item) for item in STRING.findall(arrays[1])) if len(arrays) > 1 else None
                    events.append(Event(pid, executable[pid], "exec", path, here, tuple(argv), environment))
                elif error in ABSENT_ERRORS:
                    events.append(Event(pid, executable.get(pid, "?"), "exec-absent", path, cwd=here))
                continue
            program = executable.get(pid, "?")
            if call in OPENS:
                is_write = any(flag in flags for flag in WRITE_FLAGS)
                if ok:
                    target = descriptor_path(result.strip())
                    resolved_target = normalize(target) if target is not None and target.startswith("/") and not target.endswith(" (deleted)") else None
                    if is_write:
                        events.append(Event(pid, program, "write", path, cwd=here))
                        if resolved_target is not None and resolved_target != path:
                            events.append(Event(pid, program, "write", resolved_target, cwd=here))
                    if not is_write or ("O_RDWR" in flags and "O_TRUNC" not in flags):
                        kind = "diropen" if "O_DIRECTORY" in flags else "read"
                        events.append(Event(pid, program, kind, path, cwd=here))
                        if resolved_target is not None and resolved_target != path:
                            events.append(Event(pid, program, kind, resolved_target, cwd=here))
                elif error in ABSENT_ERRORS and not is_write:
                    events.append(Event(pid, program, "absent", path, cwd=here))
            elif call in STATS:
                if ok:
                    events.append(Event(pid, program, "statdir" if "S_IFDIR" in arguments else "stat", path, cwd=here))
                elif error in ABSENT_ERRORS:
                    events.append(Event(pid, program, "absent", path, cwd=here))
            elif call in PROBES or call in READLINKS:
                if ok or error == "EINVAL":
                    events.append(Event(pid, program, "exists", path, cwd=here))
                elif error in ABSENT_ERRORS:
                    events.append(Event(pid, program, "absent", path, cwd=here))
    return events


# --- the neutral event file of the ETW consumer -------------------------------------------------------------------


@dataclass(frozen=True)
class EtwLine:
    pid: str
    ppid: str
    operation: str  # start, stop, open, openattr, opendir, write, absent, enum, enum-absent
    path: str  # an NT device path
    command: str | None = None  # of a start: the command line, None when the consumer did not receive it


ESCAPES = {"\\": "\\", "t": "\t", "n": "\n", "r": "\r"}


def unescape_field(text: str) -> str:
    """A field of the neutral event file or of a lookup report with its escaped backslashes, tabs and line breaks restored."""
    return re.sub(r"\\(.)", lambda found: ESCAPES.get(found.group(1), found.group(0)), text)


def read_events(path: Path) -> list[EtwLine]:
    """The lines of a neutral event file, in stream order. A start's command line has its backslashes, tabs and line
    breaks escaped; a start without it has no fifth column."""
    lines = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            parts = raw.rstrip("\n").split("\t", 4)
            if len(parts) in (4, 5) and parts[0].isdigit():
                if len(parts) == 5:
                    parts[4] = unescape_field(parts[4])
                lines.append(EtwLine(*parts))
    return lines


def windows_argv(command: str) -> list[str]:
    """The arguments of a Windows command line, split as the C runtime splits it: the program up to a space unless
    quoted, then arguments separated by spaces or tabs outside quotes, with ``2n`` backslashes before a quote giving
    ``n`` backslashes and a quote, ``2n + 1`` giving ``n`` backslashes and a literal quote, and ``""`` inside quotes a
    literal quote."""
    command = command.lstrip(" \t")
    if command.startswith('"'):
        end = command.find('"', 1)
        end = len(command) if end < 0 else end
        program, index = command[1:end], end + 1
    else:
        end = next((position for position, character in enumerate(command) if character in " \t"), len(command))
        program, index = command[:end], end
    arguments = [program] if command else []
    current: list[str] | None = None
    quoted = False
    while index < len(command):
        character = command[index]
        if character in " \t" and not quoted:
            if current is not None:
                arguments.append("".join(current))
                current = None
            index += 1
            continue
        current = [] if current is None else current
        if character == "\\":
            count = len(command[index:]) - len(command[index:].lstrip("\\"))
            index += count
            if command[index:index + 1] == '"':
                current.append("\\" * (count // 2))
                if count % 2:
                    current.append('"')
                    index += 1
            else:
                current.append("\\" * count)
        elif character == '"':
            if quoted and command[index + 1:index + 2] == '"':
                current.append('"')
                index += 2
            else:
                quoted = not quoted
                index += 1
        else:
            current.append(character)
            index += 1
    if current is not None:
        arguments.append("".join(current))
    return arguments


def program_name(path: str) -> str:
    """The base name of a Windows program as an executable name, without ``.exe``, in lower case."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


@dataclass
class WindowsPaths:
    """Maps the NT device paths of a Windows recording to absolute paths of the form ``/D:/dir/name``, with the recorded
    checkout replaced by the checkout that ``check`` reads, its paths spelled as Git tracks them."""

    devices: dict[str, str]
    drive: str  # the drive of the recorded checkout, such as ``D:``
    recorded_roots: tuple[str, ...]  # the forms of the recorded checkout, as paths of the form above
    root: str
    spelling: dict[str, str]  # tracked file or directory in lower case -> its tracked spelling
    temporary: str = ""  # the temporary directory of the unit whose lines are converted

    @staticmethod
    def of(settings: dict, root: str, project: Checkout) -> WindowsPaths:
        spelling = {path.lower(): path for path in project.tracked | project.directories if path}
        roots = tuple(sorted({dos_form(path) for path in settings["roots"]}, key=len, reverse=True))
        return WindowsPaths(settings["devices"], settings["roots"][0][:2], roots, root, spelling)

    def dos(self, path: str) -> str:
        for device, drive in sorted(self.devices.items(), key=lambda item: -len(item[0])):
            if path[:len(device)].lower() == device.lower() and path[len(device):len(device) + 1] in ("", "\\"):
                return drive + path[len(device):]
        return path

    def convert(self, path: str) -> str:
        """The absolute form of a device or drive path."""
        form = dos_form(self.dos(path))
        folded = form.lower()
        if self.temporary and folded.startswith(self.temporary.lower() + "/"):
            return self.temporary + form[len(self.temporary):]
        for base in self.recorded_roots:
            if folded == base.lower():
                return self.root
            if folded.startswith(base.lower() + "/"):
                return self.root + "/" + self.respell(form[len(base) + 1:])
        return form

    def respell(self, relative: str) -> str:
        found = self.spelling.get(relative.lower())
        if found is not None:
            return found
        parts = relative.split("/")
        for index in range(len(parts) - 1, 0, -1):
            known = self.spelling.get("/".join(parts[:index]).lower())
            if known is not None:
                return known + "/" + "/".join(parts[index:])
        return relative


def dos_form(path: str) -> str:
    """``D:\\dir\\name`` as ``/D:/dir/name``; a path without a drive keeps its own form with forward slashes."""
    return normalize("/" + path.replace("\\", "/"))


def windows_arguments(command: list[str], convert=dos_form) -> list[str]:
    """The arguments with forward slashes, each drive-prefixed path (standalone or after ``-C`` / ``--<opt>=``)
    converted by ``convert``."""
    converted: list[str] = []
    for argument in command:
        if re.match(r"^[A-Za-z]:[\\/]", argument):
            converted.append(convert(argument))
        elif argument.startswith("-C") and re.match(r"^[A-Za-z]:[\\/]", argument[2:]):
            converted.append("-C" + convert(argument[2:]))
        elif "=" in argument and re.match(r"^[A-Za-z]:[\\/]", argument.split("=", 1)[1]):
            option, value = argument.split("=", 1)
            converted.append(option + "=" + convert(value))
        else:
            converted.append(argument.replace("\\", "/"))
    return converted


def attribute(lines: list[EtwLine], records: list[dict]) -> dict[str, list[tuple[int, EtwLine]]]:
    """The lines of each unit, by its unit name. A process belongs to the unit whose root process it descends from,
    by the latest start of each process id before the line until its stop; a root process whose id no other unit
    reuses forms its own group even without a start event."""
    roots = {(str(record["pid"]), str(record["launcher"])): record["unit"] for record in records}
    pid_counts: dict[str, int] = {}
    for record in records:
        pid = str(record["pid"])
        pid_counts[pid] = pid_counts.get(pid, 0) + 1
    unstarted = {str(record["pid"]): record["unit"] for record in records if pid_counts[str(record["pid"])] == 1}
    current: dict[str, str | None] = {}
    parent_unit: dict[str, str | None] = {}
    grouped: dict[str, list[tuple[int, EtwLine]]] = {record["unit"]: [] for record in records}
    for index, line in enumerate(lines):
        if line.operation == "start":
            parent = parent_unit[line.ppid] if line.ppid in parent_unit else unstarted.get(line.ppid)
            unit = roots.get((line.pid, line.ppid), parent)
            current[line.pid] = parent_unit[line.pid] = unit
        elif line.operation == "stop":
            if line.pid in current:
                current[line.pid] = None
            elif (line.pid, line.ppid) in roots or (line.pid in unstarted and not line.ppid):
                current[line.pid] = None
            continue
        elif (line.pid, line.ppid) in roots:
            unit = current[line.pid] = parent_unit[line.pid] = roots[(line.pid, line.ppid)]
        else:
            unit = current[line.pid] if line.pid in current else unstarted.get(line.pid)
        if unit is not None:
            grouped[unit].append((index, line))
    return grouped


def file_of_main_stream(path: str) -> str | None:
    """A Windows path without a ``::$DATA`` suffix, which names the file's main stream, or None for a path that names
    another stream, such as a download's ``<file>:Zone.Identifier``."""
    if path.upper().endswith(MAIN_STREAM):
        path = path[:-len(MAIN_STREAM)]
    return None if ":" in (path[2:] if re.match(r"^[A-Za-z]:", path) else path) else path


def windows_events(lines: list[tuple[int, EtwLine]], record: dict, paths: WindowsPaths) -> list[Event]:
    """The file events of one unit's lines, in the records that ``classify`` takes on Linux. The consumer writes the
    first enumeration of each open of a directory, so each listing is one event, as on Linux."""
    executable: dict[str, str] = {}
    root_program = program_name(record["command"][0])
    cache = dos_form(record["cache"])
    paths.temporary = dos_form(record["temporary"])
    cwd = paths.convert(record["cwd"])
    root_pid = str(record["pid"])
    # ETW reports no working directory, so only the unit's root process has one: the working directory of its record.
    workdirs: dict[str, str | None] = {root_pid: cwd}
    events = []
    for _, line in lines:
        if line.operation == "stop":
            continue
        if line.operation != "start":
            file_path = file_of_main_stream(line.path)
            if file_path is None:
                continue  # another stream than the main one is no repository path
            line = replace(line, path=file_path)
        if line.operation == "start":
            executable[line.pid] = program_name(line.path)
            workdirs[line.pid] = cwd if (line.pid, line.ppid) == (root_pid, str(record["launcher"])) else None
            arguments = None if line.command is None else tuple(windows_arguments(windows_argv(line.command), paths.convert))
            events.append(Event(line.pid, executable[line.pid], "exec", paths.convert(line.path), workdirs[line.pid], arguments))
            continue
        program = executable.get(line.pid, root_program if line.pid == root_pid else "?")
        workdir = workdirs.get(line.pid)
        if line.operation in ("enum", "enum-absent"):
            directory, _, pattern = line.path.rpartition("\\")
            if not pattern or WILDCARDS & set(pattern):
                events.append(Event(line.pid, program, "list", paths.convert(directory), workdir))
            else:
                events.append(Event(line.pid, program, "stat" if line.operation == "enum" else "absent", paths.convert(line.path), workdir))
            continue
        path = dos_form(paths.dos(line.path))
        if path.lower().startswith(cache.lower() + "/"):
            # The bytecode cache mirrors a source path without its drive; give it the checkout's drive and map it.
            path = cache + paths.convert(paths.drive + path[len(cache):])
        else:
            path = paths.convert(line.path)
        if line.operation in ETW_KINDS:
            events.append(Event(line.pid, program, ETW_KINDS[line.operation], path, workdir))
    return events


# --- classification -------------------------------------------------------------------------------------------


@dataclass
class Reads:
    """The classified reads of one unit: content paths, existence reads by kind, directory memberships, the checkout
    directories in which it looks up modules and its module lookups by directory, the root as ``.``."""

    paths: set[str] = field(default_factory=set)
    existence: dict[str, str] = field(default_factory=dict)  # path -> directory or any
    memberships: set[str] = field(default_factory=set)
    lookup_directories: set[str] = field(default_factory=set)
    lookups: dict[str, set[str]] = field(default_factory=dict)  # directory -> module names outside the standard library
    whole_tree_commands: dict[str, set[str]] = field(default_factory=dict)  # content|membership -> commands that caused **

    def update(self, other: Reads) -> None:
        self.paths |= other.paths
        for path, kind in other.existence.items():
            self.existence[path] = kind if self.existence.get(path, kind) == kind else "any"
        self.memberships |= other.memberships
        self.lookup_directories |= other.lookup_directories
        for directory, names in other.lookups.items():
            self.lookups.setdefault(directory, set()).update(names)
        for kind, commands in other.whole_tree_commands.items():
            self.whole_tree_commands.setdefault(kind, set()).update(commands)

    def document(self) -> dict:
        doc = {"paths": sorted(self.paths),
               "existence_reads": [{"path": path, "kind": kind} for path, kind in sorted(self.existence.items())],
               "directory_memberships": sorted(self.memberships), "lookup_directories": sorted(self.lookup_directories),
               "module_lookups": {directory: sorted(names) for directory, names in sorted(self.lookups.items()) if names}}
        commands = {kind: sorted(items) for kind, items in sorted(self.whole_tree_commands.items()) if items}
        if commands:
            doc["whole_tree_commands"] = commands
        return doc

    @staticmethod
    def of(document: dict) -> Reads:
        return Reads(set(document.get("paths", ())), {read["path"]: read["kind"] for read in document.get("existence_reads", ())},
                     set(document.get("directory_memberships", ())), set(document.get("lookup_directories", ())),
                     {directory: set(names) for directory, names in document.get("module_lookups", {}).items()},
                     {kind: set(commands) for kind, commands in document.get("whole_tree_commands", {}).items() if commands})

    def empty(self) -> bool:
        return not (self.paths or self.existence or self.memberships or self.lookup_directories or any(self.lookups.values()))


@dataclass(frozen=True)
class FoundModule:
    """A module or package that an import found in a directory."""

    directory: str
    name: str
    file: str  # the file that the import loads
    suffixes: tuple[str, ...]  # the file suffixes that its process's path finder tries, in order


@dataclass(frozen=True)
class MissingModule:
    """A module name looked up in a directory and not found there, or found there as a namespace portion."""

    directory: str
    name: str


@dataclass(frozen=True)
class Lookups:
    """The lookup reports of one unit's processes, their paths in the form of the event paths."""

    processes: frozenset[str] = frozenset()  # the ids of the processes, and of the threads that listed, with a report
    listings: dict[tuple[str, str], int] = field(default_factory=dict)  # (process or thread, directory) -> listings
    found: tuple[FoundModule, ...] = ()
    missing: tuple[MissingModule, ...] = ()


def read_lookups(directory: Path, convert: Callable[[str], str], *, by_thread: bool = True) -> Lookups:
    """The lookup reports in ``directory``, one file per process (``shared_lookup_report.py``), with each reported
    path converted by ``convert``. When ``by_thread`` holds (Linux ``strace``), each listing is credited to the
    reporting thread's id; otherwise (Windows ETW, whose events carry process ids only) to the process id."""
    processes: set[str] = set()
    listings: dict[tuple[str, str], int] = {}
    found: list[FoundModule] = []
    missing: list[MissingModule] = []
    for report in sorted(directory.glob("*.tsv")) if directory.is_dir() else ():
        processes.add(report.stem)
        suffixes: tuple[str, ...] = ()
        for line in report.read_text(encoding="utf-8", errors="replace").splitlines():
            fields = [unescape_field(item) for item in line.split("\t")]
            if fields[0] == "suffixes":
                suffixes = tuple(fields[1:])
            elif fields[0] == "list" and len(fields) == 3:
                processes.add(fields[1])
                key = (fields[1] if by_thread else report.stem, convert(fields[2]))
                listings[key] = listings.get(key, 0) + 1
            elif fields[0] == "lookup" and len(fields) == 5 and fields[3] == "found":
                found.append(FoundModule(convert(fields[1]), fields[2], convert(fields[4]), suffixes))
            elif fields[0] == "lookup" and len(fields) == 4 and fields[3] in ("absent", "namespace"):
                missing.append(MissingModule(convert(fields[1]), fields[2]))
    return Lookups(frozenset(processes), listings, tuple(found), tuple(missing))


@dataclass(frozen=True)
class Checkout:
    root: Path
    tracked: frozenset[str]
    directories: frozenset[str]  # every directory that holds a tracked file, the root as ""
    ignore: suite_selection.IgnoreRules  # the ignore rules of the checked commit
    # The Git directory and the common directory in the form of the event paths; Windows takes the recorded ones.
    git_directories: tuple[str, ...] = ()


def git_directories(root: Path) -> list[str]:
    """The Git directory of a checkout, and for a linked worktree the common directory: ``<root>/.git``, or the
    ``gitdir:`` target of a ``.git`` file with the directory that the target's ``commondir`` file names."""
    dot = root / ".git"
    text = dot.read_text(encoding="utf-8").strip() if dot.is_file() else ""
    if not text.startswith("gitdir:"):
        return [str(dot)]
    target = Path(os.path.normpath(root / text[len("gitdir:"):].strip()))
    common = target / "commondir"
    found = [target, Path(os.path.normpath(target / common.read_text(encoding="utf-8").strip()))] if common.is_file() else [target]
    return [str(directory) for directory in found]


def checkout(root: Path) -> Checkout:
    listing = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, text=True, encoding="utf-8", check=False)
    if listing.returncode != 0:
        raise RecordingError(f"{root} is not a Git checkout: {listing.stderr.strip()}")
    tracked = frozenset(item for item in listing.stdout.split("\0") if item)
    directories = {""}
    for path in tracked:
        parts = path.split("/")
        directories.update("/".join(parts[:index]) for index in range(1, len(parts)))
    return Checkout(root, tracked, frozenset(directories), suite_selection.ignore_rules(root, "HEAD", tracked), tuple(git_directories(root)))


def operands(arguments: list[str], with_value: frozenset[str]) -> list[int]:
    """The positions of the arguments of a Git command that are no options, skipping the value of each option in
    ``with_value``."""
    found = []
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return found + list(range(index + 1, len(arguments)))
        if argument.startswith("-") and argument != "-":
            index += 2 if argument in with_value else 1
            continue
        found.append(index)
        index += 1
    return found


def local_path(argument: str) -> str | None:
    """The path that a repository argument of ``git clone`` names, or None for a URL or a remote ``host:path``."""
    if argument.startswith("file://"):
        return argument[len("file://"):]
    head = argument.split("/", 1)[0]
    if "://" in argument or (":" in head and not argument.startswith("/")):
        return None
    return argument


def git_copy(event: Event, root: str) -> str | None:
    """The directory that a ``git clone`` or ``git worktree add`` with the checkout ``root`` as source creates, or None.
    Fails when the recording lacks a ``git`` process's arguments, or the working directory against which such a command
    resolves a relative source or destination."""
    if event.kind != "exec" or event.executable != "git":
        return None
    if event.arguments is None:
        raise RecordingError(f"the recording holds no command line of process {event.pid} ({event.path}), so check cannot tell "
                             "whether it copies the checkout; record again")
    arguments = list(event.arguments[1:])
    positions = operands(arguments, GIT_OPTIONS_WITH_VALUE)
    if not positions:
        return None
    directory = event.cwd
    for index in range(positions[0] - 1):
        if arguments[index] == "-C":  # each -C names the next working directory, relative to the one before
            value = arguments[index + 1]
            directory = normalize(value) if value.startswith("/") else None if directory is None else resolve(directory, value)

    def located(path: str) -> str:
        if path.startswith("/"):
            return normalize(path)
        if directory is None:
            raise RecordingError(f"process {event.pid} ran `{' '.join(event.arguments)}` with a relative source or destination in a "
                                 "working directory that the recording does not know, so check cannot tell whether it copies the "
                                 "checkout; give it absolute paths, for git worktree add with -C <checkout>")
        return resolve(directory, path)

    command, rest = arguments[positions[0]], arguments[positions[0] + 1:]
    if command == "clone":
        names = [rest[index] for index in operands(rest, CLONE_OPTIONS_WITH_VALUE)]
        source = local_path(names[0]) if names else None
        if source is None:
            return None
        resolved = located(source)
        if (resolved[:-len("/.git")] if resolved.endswith("/.git") else resolved) != root:
            return None
        if len(names) > 1:
            return located(names[1])
        name = posixpath.basename(resolved)  # the directory that Git derives from the source, as in git clone <repository>
        name = posixpath.basename(posixpath.dirname(resolved)) if name == ".git" else name
        return located(name[:-len(".git")] if name.endswith(".git") else name)
    if command == "worktree" and rest[:1] == ["add"]:
        repository = located(".")  # the source of a worktree is the repository of the working directory
        names = [rest[1:][index] for index in operands(rest[1:], WORKTREE_ADD_OPTIONS_WITH_VALUE)]
        if names and (repository == root or repository.startswith(root + "/")):
            return located(names[0])
    return None


def copy_roots(events: list[Event], temporary: str, root: str) -> list[str]:
    """The roots of temporary repository copies: a directory under the temporary directory that a ``git clone`` or a
    ``git worktree add`` with the checkout as source creates. Longest first."""
    prefix = temporary.rstrip("/") + "/"
    roots = {copy for event in events if (copy := git_copy(event, root)) is not None and copy.startswith(prefix)}
    return sorted(roots, key=len, reverse=True)


def is_git(executable: str) -> bool:
    # git_command reads only a git exec's command, so a git-<name> reader always reads every tracked path.
    return executable == "git" or executable.startswith("git-")


def git_state_path(path: str, directories: list[str]) -> str | None:
    """The path relative to one of the Git ``directories`` (with ``worktrees/<name>/`` stripped) when ``path`` lies in
    the index, objects or refs of that directory, or None."""
    folded = path.lower()
    for directory in directories:
        if folded.startswith(directory.lower() + "/"):
            parts = path[len(directory) + 1:].split("/")
            relative = "/".join(parts[2:] if parts[0] == "worktrees" else parts)
            if INDEX_OBJECTS_OR_REFS.match(relative) is not None:
                return relative
    return None


def git_command(event: Event | None) -> tuple[str, list[str], list[str]] | None:
    """The command of a ``git`` exec with Git's own options before it and the arguments after it, or None."""
    if event is None or event.executable != "git" or event.arguments is None:
        return None
    arguments = list(event.arguments[1:])
    positions = operands(arguments, GIT_OPTIONS_WITH_VALUE)
    return (arguments[positions[0]], arguments[:positions[0]], arguments[positions[0] + 1:]) if positions else None


def prints_only_names(arguments: list[str]) -> bool:
    """Whether a ``git ls-files`` with these arguments prints only the names of tracked paths."""
    for argument in arguments:
        if argument == "--":
            break
        if argument.startswith("--"):
            if argument.split("=", 1)[0] not in LS_FILES_NAME_OPTIONS:
                return False
        elif argument.startswith("-") and not all(f"-{letter}" in LS_FILES_NAME_OPTIONS for letter in argument[1:]):
            return False
    return True


def git_env_bool(value: str) -> bool:
    """Whether a Git boolean environment variable value is truthy."""
    return value.strip().lower() not in ("", "0", "false", "no", "off")


@dataclass(frozen=True)
class GitGlobalOptions:
    """Parsed global options and relevant environment flags of a ``git`` process."""

    cwd: str | None
    work_tree: str | None
    pathspec_mode: str | None  # "literal", "glob", "noglob", "icase", or None
    replace_refs_off: bool
    unknown_env: bool


@dataclass(frozen=True)
class GitObjectContext:
    """Lookup state for attributing Git object ids and ``<rev>:<path>`` expressions."""

    held: dict[str, list[str]]
    replaced: dict[str, str]
    replace_refs_off: bool
    replace_ref_seen: bool


class _Exclude:
    """Sentinel returned by ``translate_pathspec`` for an exclude pathspec."""


EXCLUDE = _Exclude()


def parse_git_global_options(event: Event, options: list[str]) -> GitGlobalOptions | None:
    """Parse Git's global options before the subcommand and its environment variables, or return None when an option
    may alter which tracked paths are read."""
    cwd = event.cwd
    work_tree: str | None = None
    pathspec_mode: str | None = None
    replace_refs_off = False

    def set_mode(new_mode: str) -> bool:
        nonlocal pathspec_mode
        if new_mode == "icase":
            pathspec_mode = "icase"
            return True
        if pathspec_mode == "icase":
            return True
        if pathspec_mode is not None and pathspec_mode != new_mode:
            return False
        pathspec_mode = new_mode
        return True

    def resolve_dir(value: str) -> str | None:
        if re.match(r"^[A-Za-z]:[\\/]", value):
            return dos_form(value)
        if value.startswith("/"):
            return normalize(value)
        return None if cwd is None else resolve(cwd, value)

    index = 0
    while index < len(options):
        argument = options[index]
        if argument in ("-C", "-c", "--git-dir", "--work-tree"):
            if index + 1 >= len(options):
                return None
            name, value = argument, options[index + 1]
            index += 2
        elif argument.startswith(("--git-dir=", "--work-tree=")):
            name, value = argument.split("=", 1)
            index += 1
        elif argument.startswith("-C") and len(argument) > 2:
            name, value = "-C", argument[2:]
            index += 1
        elif argument.startswith("-c") and len(argument) > 2:
            name, value = "-c", argument[2:]
            index += 1
        elif argument in GIT_SAFE_GLOBAL_FLAGS:
            if argument == "--no-replace-objects":
                replace_refs_off = True
            elif argument == "--literal-pathspecs":
                if not set_mode("literal"):
                    return None
            elif argument == "--glob-pathspecs":
                if not set_mode("glob"):
                    return None
            elif argument == "--noglob-pathspecs":
                if not set_mode("noglob"):
                    return None
            elif argument == "--icase-pathspecs":
                set_mode("icase")
            index += 1
            continue
        else:
            return None

        if name == "-C":
            cwd = resolve_dir(value)
        elif name == "--work-tree":
            work_tree = resolve_dir(value)
        elif name == "--git-dir":
            pass
        elif name == "-c":
            if "=" not in value:
                return None
            key, setting = value.split("=", 1)
            folded_key = key.strip().lower()
            if folded_key not in GIT_SAFE_CONFIG:
                return None
            if folded_key == "core.usereplacerefs" and setting.strip().lower() in ("false", "0", "no", "off"):
                replace_refs_off = True

    unknown_env = event.environment is None
    if event.environment is not None:
        for entry in event.environment:
            key, _, val = entry.partition("=")
            if key == "GIT_NO_REPLACE_OBJECTS" and val.strip().lower() not in ("0", "false", "no", "off"):
                replace_refs_off = True
            elif key == "GIT_ICASE_PATHSPECS" and git_env_bool(val):
                set_mode("icase")
            elif key == "GIT_LITERAL_PATHSPECS" and git_env_bool(val):
                if not set_mode("literal"):
                    return None
            elif key == "GIT_GLOB_PATHSPECS" and git_env_bool(val):
                if not set_mode("glob"):
                    return None
            elif key == "GIT_NOGLOB_PATHSPECS" and git_env_bool(val):
                if not set_mode("noglob"):
                    return None

    return GitGlobalOptions(cwd, work_tree, pathspec_mode, replace_refs_off, unknown_env)


def working_prefix(gopts: GitGlobalOptions, roots: list[str], *, fold: bool = False) -> str | None:
    """The repository-relative directory against which relative pathspecs resolve, ``""`` for the root, or None when
    the working directory is unknown or outside ``roots``."""
    if gopts.work_tree is not None:
        wt_rel = next((checkout_relative(gopts.work_tree, base, fold) for base in roots if checkout_relative(gopts.work_tree, base, fold) == ""), None)
        if wt_rel is None:
            return None
        if gopts.cwd is not None:
            rel = checkout_relative(gopts.cwd, gopts.work_tree, fold)
            if rel is not None:
                return rel
        return ""
    if gopts.cwd is None:
        return None
    for base in roots:
        rel = checkout_relative(gopts.cwd, base, fold)
        if rel is not None:
            return rel
    return None


def collapse_relative(base: str, pattern: str) -> str | None:
    """Join ``base`` and ``pattern`` and collapse ``.`` and ``..`` segments, returning None when the path escapes the
    repository root, collapses to the root itself, or places ``..`` after a wildcard segment."""
    raw = (base + "/" + pattern) if (base and pattern) else (base or pattern)
    parts: list[str] = []
    for segment in raw.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not parts or "*" in parts[-1] or "?" in parts[-1]:
                return None
            parts.pop()
            continue
        parts.append(segment)
    return "/".join(parts) if parts else None


def gitattributes_paths(pattern: str, *, is_dir: bool) -> set[str]:
    """The ``.gitattributes`` files that Git reads for ``pattern``: every ancestor directory up to the repository root,
    plus a recursive ``.gitattributes`` beneath a wildcard segment."""
    if "*" in pattern or "?" in pattern:
        literal_parts: list[str] = []
        for segment in pattern.split("/"):
            if "*" in segment or "?" in segment:
                break
            literal_parts.append(segment)
        prefix = "/".join(literal_parts)
        paths = {posixpath.join(directory, ".gitattributes") for directory in ancestors(prefix)}
        paths.add(posixpath.join(prefix, "**", ".gitattributes") if prefix else "**/.gitattributes")
        return paths
    start_dir = pattern if is_dir else posixpath.dirname(pattern)
    return {posixpath.join(directory, ".gitattributes") for directory in ancestors(start_dir)}


def translate_pathspec(pathspec: str, prefix: str | None, mode: str | None, unknown_env: bool, *,
                       content: bool = False, project: Checkout | None = None,
                       literal_both: bool = False, include_gitattributes: bool = False) -> Reads | _Exclude | None:
    """Translate one Git pathspec into conservative ``Reads``, ``EXCLUDE`` for an exclude pathspec, or None when only
    ``**`` covers it."""
    if mode == "icase":
        return None
    from_top = False
    local_mode: str | None = None
    pattern = pathspec
    if pathspec.startswith(":("):
        end = pathspec.find(")")
        if end < 0:
            return None
        words = pathspec[2:end].split(",")
        if not words or any(not word for word in words):
            return None
        if "exclude" in words:
            return EXCLUDE
        for word in words:
            if word == "top":
                from_top = True
            elif word == "literal":
                if local_mode == "glob":
                    return None
                local_mode = "literal"
            elif word == "glob":
                if local_mode == "literal":
                    return None
                local_mode = "glob"
            else:
                return None
        pattern = pathspec[end + 1:]
    elif pathspec.startswith(":"):
        rest = pathspec[1:]
        stripped = rest.lstrip(SHORT_MAGIC)
        signs = rest[:len(rest) - len(stripped)]
        if not signs:
            return None
        if "!" in signs or "^" in signs:
            return EXCLUDE
        if "/" in signs:
            from_top = True
        pattern = stripped[1:] if stripped.startswith(":") else stripped

    if local_mode is not None:
        if mode is not None and mode != local_mode:
            return None
        effective_mode = local_mode
    elif mode is not None:
        effective_mode = mode
    elif unknown_env:
        effective_mode = "union"
    else:
        effective_mode = "noglob"

    base = "" if from_top else prefix
    if base is None:
        return None
    trailing_slash = pattern.endswith("/")
    norm = collapse_relative(base, pattern)
    if norm is None:
        return None
    if effective_mode != "literal" and ("[" in norm or "\\" in norm):
        return None

    has_wildcard = "*" in norm or "?" in norm
    if has_wildcard:
        translated = norm if effective_mode in ("glob", "literal") else re.sub(r"[*?]+", "**", norm)
        if content:
            paths = {translated, translated + "/" + EVERY_PATH}
            if include_gitattributes:
                paths.update(gitattributes_paths(translated, is_dir=False))
            return Reads(paths=paths)
        return Reads(memberships={translated, translated + "/" + EVERY_PATH})

    if content:
        is_dir = trailing_slash or (project is not None and norm in project.directories and norm not in project.tracked)
        if literal_both:
            paths = {norm, norm + "/" + EVERY_PATH}
        elif is_dir:
            paths = {norm + "/" + EVERY_PATH}
        elif project is not None and norm in project.tracked:
            paths = {norm}
        else:
            paths = {norm, norm + "/" + EVERY_PATH}
        if include_gitattributes:
            paths.update(gitattributes_paths(norm, is_dir=is_dir))
        return Reads(paths=paths)
    return Reads(existence={norm: "any"}, memberships={norm + "/" + EVERY_PATH})


def translate_pathspecs(pathspecs: list[str], prefix: str | None, gopts: GitGlobalOptions, *,
                        content: bool = False, project: Checkout | None = None,
                        literal_both: bool = False, include_gitattributes: bool = False) -> Reads | None:
    """Translate a list of pathspecs into ``Reads``, or None when empty, all-exclude, or any pathspec requires ``**``."""
    if not pathspecs:
        return None
    reads = Reads()
    included = False
    for pathspec in pathspecs:
        translated = translate_pathspec(pathspec, prefix, gopts.pathspec_mode, gopts.unknown_env,
                                        content=content, project=project, literal_both=literal_both,
                                        include_gitattributes=include_gitattributes)
        if translated is None:
            return None
        if isinstance(translated, _Exclude):
            continue
        included = True
        reads.update(translated)
    return reads if included else None


def listing_reads(event: Event, options: list[str], arguments: list[str], roots: list[str], *, fold: bool = False) -> Reads:
    """The reads of a ``git ls-files`` that prints only names, with Git's own ``options`` before the command and the
    command's ``arguments``: a directory membership of ``**`` without a pathspec, and otherwise what each pathspec can
    list."""
    every = Reads(memberships={EVERY_PATH})
    pathspecs = [arguments[index] for index in operands(arguments, frozenset({"--pathspec-from-file"}))]
    before_dash = arguments[:arguments.index("--")] if "--" in arguments else arguments
    from_file = any(argument.split("=", 1)[0] == "--pathspec-from-file" for argument in before_dash)
    if not pathspecs or from_file:
        return every
    gopts = parse_git_global_options(event, options)
    if gopts is None:
        return every
    prefix = working_prefix(gopts, roots, fold=fold)
    translated = translate_pathspecs(pathspecs, prefix, gopts, content=False)
    return translated if translated is not None else every


def scan_git_options(before_dash: list[str], safe_flags: frozenset[str], value_options: frozenset[str] = frozenset(), *,
                     attached_prefixes: tuple[str, ...] = ()) -> bool:
    """Whether every option in ``before_dash`` is in ``safe_flags``, ``value_options`` (separate or ``=``-attached),
    or starts with one of ``attached_prefixes``."""
    index = 0
    while index < len(before_dash):
        argument = before_dash[index]
        if argument.startswith("-") and argument != "-":
            if argument in value_options:
                if index + 1 >= len(before_dash):
                    return False
                index += 2
                continue
            if argument in safe_flags or any(argument.startswith(opt + "=") for opt in value_options) \
                    or (attached_prefixes and argument.startswith(attached_prefixes)):
                index += 1
                continue
            return False
        index += 1
    return True


def split_tree_and_pathspecs(arguments: list[str], value_options: frozenset[str] = frozenset()) -> list[str] | None:
    """The pathspec arguments of a ``<command> [options] <tree-ish> [--] <path>...`` command, or None when no pathspec
    limits it."""
    if "--" in arguments:
        dash = arguments.index("--")
        tree_operands = [arguments[index] for index in operands(arguments[:dash], value_options)]
        if len(tree_operands) != 1:
            return None
        return arguments[dash + 1:] or None
    all_operands = [arguments[index] for index in operands(arguments, value_options)]
    return all_operands[1:] if len(all_operands) >= 2 else None


def os_path_of(path: str) -> Path:
    """Convert an event path (including ``/D:/...`` on Windows) to a local ``Path``."""
    if sys.platform == "win32" and re.match(r"^/[A-Za-z]:/", path):
        return Path(path[1:])
    return Path(path)


def replace_refs(directories: tuple[str, ...]) -> dict[str, str]:
    """The replace refs (``refs/replace/<oid>`` -> target oid) in ``directories``, from ``packed-refs`` and loose ref
    files, with loose refs taking precedence over packed ones."""
    refs: dict[str, str] = {}
    for directory in directories:
        base = os_path_of(directory)
        packed = base / "packed-refs"
        if packed.is_file():
            try:
                for line in packed.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not line or line.startswith(("#", "^")):
                        continue
                    parts = line.split()
                    if len(parts) == 2 and parts[1].startswith("refs/replace/"):
                        refs[parts[1][len("refs/replace/"):].lower()] = parts[0].lower()
            except OSError:
                pass
        loose = base / "refs" / "replace"
        if loose.is_dir():
            for entry in sorted(loose.rglob("*")):
                if entry.is_file():
                    try:
                        orig = entry.relative_to(loose).as_posix().lower()
                        refs[orig] = entry.read_text(encoding="utf-8", errors="replace").strip().lower()
                    except OSError:
                        pass
    return refs


def resolve_object_id(object_id: str, context: GitObjectContext) -> list[str] | None:
    """The tracked paths for a read of ``object_id``: its held paths, the replaced object's paths or ``["**"]`` when a
    replace ref applies, or None when HEAD does not hold the object and no replace ref applies."""
    if not context.replace_refs_off:
        if object_id in context.replaced:
            current = object_id
            visited: set[str] = set()
            while current in context.replaced and current not in visited:
                visited.add(current)
                current = context.replaced[current]
            if current in visited:
                return [EVERY_PATH]
            paths = context.held.get(current)
            return paths if paths else [EVERY_PATH]
        if context.replace_ref_seen:
            return [EVERY_PATH]
    return context.held.get(object_id)


def rev_path_reads(spec: str, prefix: str | None, project: Checkout, context: GitObjectContext) -> Reads | None:
    """The reads of a ``<rev>:<path>`` or ``:<path>`` (index) object expression, or None when ``**`` is needed."""
    if "://" in spec or ":" not in spec:
        return None
    if spec.startswith(":"):
        is_index = True
        rev = ""
        rest = spec[1:]
        if len(rest) >= 2 and rest[0] in "0123" and rest[1] == ":":
            rest = rest[2:]
        path = rest
    else:
        is_index = False
        rev, path = spec.split(":", 1)
        if not rev:
            return None
    if not path:
        return None
    trailing_slash = path.endswith("/")
    if path.startswith(("./", "../")) or path in (".", ".."):
        if prefix is None:
            return None
        norm = collapse_relative(prefix, path)
    else:
        norm = collapse_relative("", path)
    if norm is None:
        return None

    def reads_for(target: str) -> Reads:
        is_dir = trailing_slash or (target in project.directories and target not in project.tracked)
        if is_dir:
            paths = {target + "/" + EVERY_PATH}
        elif target in project.tracked:
            paths = {target}
        else:
            paths = {target, target + "/" + EVERY_PATH}
        result = Reads(paths=paths)
        if is_index:
            result.paths.update(gitattributes_paths(target, is_dir=is_dir))
        return result

    if not is_index:
        matched = FULL_OBJECT_ID.match(rev)
        if matched is not None:
            oid = matched.group(1).lower()
            resolved = resolve_object_id(oid, context)
            if resolved is None:
                return Reads()
            if EVERY_PATH in resolved:
                return reads_for(norm)
            if all(entry.endswith("/" + EVERY_PATH) for entry in resolved):
                combined = Reads()
                for entry in resolved:
                    combined.update(reads_for(entry[:-len("/" + EVERY_PATH)] + "/" + norm))
                return combined
            return Reads(paths={EVERY_PATH})
    return reads_for(norm)


def cat_file_reads(arguments: list[str], prefix: str | None, project: Checkout, context: GitObjectContext) -> Reads | None:
    """The reads of a ``git cat-file`` command, or None when ``**`` is needed."""
    before_dash = arguments[:arguments.index("--")] if "--" in arguments else arguments
    if not scan_git_options(before_dash, CAT_FILE_SAFE_FLAGS):
        return None
    names = [arguments[index] for index in operands(arguments, frozenset())]
    if not CAT_FILE_MODES & set(before_dash):
        if not names or names[0] not in CAT_FILE_TYPES:
            return None
        names = names[1:]
    if not names:
        return None
    reads = Reads()
    for name in names:
        matched = FULL_OBJECT_ID.match(name)
        if matched is not None:
            resolved = resolve_object_id(matched.group(1).lower(), context)
            if resolved:
                reads.paths.update(resolved)
        elif ":" in name and "://" not in name:
            part = rev_path_reads(name, prefix, project, context)
            if part is None:
                return None
            reads.update(part)
        else:
            return None
    return reads


def show_reads(arguments: list[str], prefix: str | None, project: Checkout, context: GitObjectContext) -> Reads | None:
    """The reads of a ``git show`` command when every operand is a ``<rev>:<path>`` or ``:<path>``, or None."""
    if "--" in arguments:
        return None
    if not scan_git_options(arguments, GIT_SHOW_SAFE_FLAGS, attached_prefixes=("--color=", "--format=", "--pretty=")):
        return None
    names = [argument for argument in arguments if not argument.startswith("-")]
    if not names:
        return None
    reads = Reads()
    for name in names:
        if ":" not in name or "://" in name:
            return None
        part = rev_path_reads(name, prefix, project, context)
        if part is None:
            return None
        reads.update(part)
    return reads


def ls_tree_reads(arguments: list[str], prefix: str | None, gopts: GitGlobalOptions, project: Checkout) -> Reads | None:
    """The reads of a ``git ls-tree <rev> -- <prefix>...`` command, or None when no pathspec limits it."""
    before_dash = arguments[:arguments.index("--")] if "--" in arguments else arguments
    if not scan_git_options(before_dash, GIT_LS_TREE_SAFE_FLAGS, attached_prefixes=("--abbrev=", "--format=")):
        return None
    pathspecs = split_tree_and_pathspecs(arguments)
    if pathspecs is None:
        return None
    full_tree = "--full-tree" in before_dash
    return translate_pathspecs(pathspecs, "" if full_tree else prefix, gopts, content=True, project=project, literal_both=True)


def archive_reads(arguments: list[str], prefix: str | None, gopts: GitGlobalOptions, project: Checkout) -> Reads | None:
    """The reads of a ``git archive <rev> -- <paths>...`` command, including ancestor ``.gitattributes``, or None."""
    before_dash = arguments[:arguments.index("--")] if "--" in arguments else arguments
    if not scan_git_options(before_dash, GIT_ARCHIVE_SAFE_FLAGS, GIT_ARCHIVE_VALUE_OPTIONS, attached_prefixes=("-o",)):
        return None
    pathspecs = split_tree_and_pathspecs(arguments, GIT_ARCHIVE_VALUE_OPTIONS)
    if pathspecs is None:
        return None
    return translate_pathspecs(pathspecs, prefix, gopts, content=True, project=project, include_gitattributes=True)


def rev_parse_reads(arguments: list[str]) -> Reads | None:
    """Empty ``Reads`` when ``git rev-parse`` queries only repository location or work-tree flags, or None."""
    if not arguments:
        return None
    for argument in arguments:
        if argument in GIT_REV_PARSE_NO_PATH_FLAGS or argument.startswith("--path-format="):
            continue
        return None
    return Reads()


def merge_base_reads(arguments: list[str], context: GitObjectContext) -> Reads | None:
    """Empty ``Reads`` when ``git merge-base`` takes only full object ids without applicable replace refs, or None."""
    before_dash = arguments[:arguments.index("--")] if "--" in arguments else arguments
    if not scan_git_options(before_dash, GIT_MERGE_BASE_SAFE_FLAGS):
        return None
    names = [arguments[index] for index in operands(arguments, frozenset())]
    if len(names) < 2:
        return None
    for name in names:
        matched = FULL_OBJECT_ID.match(name)
        if matched is None:
            return None
        oid = matched.group(1).lower()
        if not context.replace_refs_off and (oid in context.replaced or context.replace_ref_seen):
            return None
    return Reads()


def diff_reads(arguments: list[str], prefix: str | None, gopts: GitGlobalOptions, project: Checkout) -> Reads | None:
    """The reads of a ``git diff ... -- <path>...`` command, or None when no pathspec after ``--`` limits it or an
    unlisted option is present."""
    if "--" not in arguments:
        return None
    dash = arguments.index("--")
    if not scan_git_options(arguments[:dash], GIT_DIFF_SAFE_FLAGS, GIT_DIFF_VALUE_OPTIONS, attached_prefixes=("-U", "-M")):
        return None
    pathspecs = arguments[dash + 1:]
    return translate_pathspecs(pathspecs, prefix, gopts, content=True, project=project)


def git_add_reads(arguments: list[str], prefix: str | None, gopts: GitGlobalOptions, project: Checkout) -> Reads | None:
    """The reads of a ``git add [options] [--] <pathspec>...`` command, or None when no pathspec limits it."""
    before_dash = arguments[:arguments.index("--")] if "--" in arguments else arguments
    if not scan_git_options(before_dash, GIT_ADD_SAFE_FLAGS, attached_prefixes=("--chmod=",)):
        return None
    pathspecs = arguments[arguments.index("--") + 1:] if "--" in arguments else [arguments[i] for i in operands(arguments, frozenset())]
    return translate_pathspecs(pathspecs, prefix, gopts, content=True, project=project)


def restore_reads(arguments: list[str], prefix: str | None, gopts: GitGlobalOptions, project: Checkout) -> Reads | None:
    """The reads of a ``git restore [options] [--] <pathspec>...`` command, or None."""
    before_dash = arguments[:arguments.index("--")] if "--" in arguments else arguments
    if not scan_git_options(before_dash, GIT_RESTORE_SAFE_FLAGS, GIT_RESTORE_VALUE_OPTIONS):
        return None
    pathspecs = arguments[arguments.index("--") + 1:] if "--" in arguments else [arguments[i] for i in operands(arguments, GIT_RESTORE_VALUE_OPTIONS)]
    return translate_pathspecs(pathspecs, prefix, gopts, content=True, project=project)


def checkout_path_reads(arguments: list[str], prefix: str | None, gopts: GitGlobalOptions, project: Checkout) -> Reads | None:
    """The reads of a ``git checkout [<tree-ish>] -- <pathspec>...`` command, or None."""
    if "--" not in arguments:
        return None
    dash = arguments.index("--")
    if not scan_git_options(arguments[:dash], GIT_CHECKOUT_SAFE_FLAGS):
        return None
    pathspecs = arguments[dash + 1:]
    return translate_pathspecs(pathspecs, prefix, gopts, content=True, project=project)


def grep_reads(arguments: list[str], prefix: str | None, gopts: GitGlobalOptions, project: Checkout) -> Reads | None:
    """The reads of a ``git grep ... -- <pathspec>...`` command, or None when no pathspec after ``--`` limits it or an
    unlisted option is present."""
    if "--" not in arguments:
        return None
    dash = arguments.index("--")
    if not scan_git_options(arguments[:dash], GIT_GREP_SAFE_FLAGS, GIT_GREP_VALUE_OPTIONS):
        return None
    pathspecs = arguments[dash + 1:]
    return translate_pathspecs(pathspecs, prefix, gopts, content=True, project=project)


@functools.cache
def held_paths(root: Path) -> dict[str, list[str]]:
    """The tracked paths of the checked commit that hold each object: the paths of a blob or a submodule's commit,
    ``<directory>/**`` for the tree of a directory, and ``**`` for the commit itself and its tree."""
    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD", "HEAD^{tree}"], capture_output=True, text=True, check=False)
    listing = subprocess.run(["git", "-C", str(root), "ls-tree", "-r", "-t", "-z", "--full-tree", "HEAD"], capture_output=True, text=True,
                             encoding="utf-8", check=False)
    if commit.returncode != 0 or listing.returncode != 0:
        raise RecordingError(f"the checked commit of {root} could not be read: {(commit.stderr + listing.stderr).strip()}")
    held: dict[str, list[str]] = {object_id: [EVERY_PATH] for object_id in commit.stdout.split()}
    for item in listing.stdout.split("\0"):
        if item:
            meta, path = item.split("\t", 1)
            _, kind, object_id = meta.split(" ")
            held.setdefault(object_id, []).append(path + "/" + EVERY_PATH if kind == "tree" else path)
    return held


def format_command(exec_event: Event | None, fallback_executable: str) -> str:
    """Human-readable process and arguments as the recording saw them."""
    if exec_event is not None:
        if exec_event.arguments:
            return shlex.join(exec_event.arguments)
        if exec_event.executable:
            return exec_event.executable
    return fallback_executable or "unknown"


def git_directory_reads(events: list[Event], copies: list[str], project: Checkout, root: str | None = None, *, fold: bool = False) -> Reads:
    """The reads of any process that reads the index, objects or refs of the checkout's Git directories or of the Git
    directory of a repository copy in ``copies``, whose paths the .git rule of ``relative_events`` drops. Each ``git``
    command is attributed to the tracked paths its output can depend on, or to none; any whole-tree or unattributable
    command, Git helper, or non-Git file read of Git state falls back to ``**``. A non-Git process that only lists
    ``.git/objects`` or ``.git/refs`` adds no tracked path."""
    directories = [*project.git_directories, *(copy + "/.git" for copy in copies)]
    active_exec: dict[str, Event] = {}
    readers: list[tuple[str, Event | None, str]] = []
    seen: set[tuple[str, Event | None]] = set()
    replace_ref_seen: set[tuple[str, Event | None]] = set()
    for event in events:
        if event.kind == "exec":
            active_exec[event.pid] = event
            continue
        if event.kind == "write":
            continue
        state_rel = git_state_path(event.path, directories)
        if state_rel is None:
            continue
        exec_event = active_exec.get(event.pid)
        key = (event.pid, exec_event)
        if state_rel.startswith("refs/replace/") and event.kind in ("read", "stat", "exists"):
            replace_ref_seen.add(key)
        if not is_git(event.executable) and event.kind in ("list", "diropen", "statdir") \
                and (state_rel in ("objects", "refs") or state_rel.startswith(("objects/", "refs/"))):
            continue
        if key not in seen:
            seen.add(key)
            readers.append((event.pid, exec_event, event.executable))
    roots = [root or str(project.root), *copies]
    # Read replace refs from the checkout's real Git directories and any copy Git directories still on disk.
    replaced = replace_refs(tuple(dict.fromkeys([*git_directories(project.root), *(copy + "/.git" for copy in copies)])))
    reads = Reads()
    for pid, exec_event, fallback_executable in readers:
        key = (pid, exec_event)
        reader_reads = Reads()
        parsed = git_command(exec_event)
        if parsed is None:
            reader_reads.paths.add(EVERY_PATH)
        else:
            assert exec_event is not None
            command, options, arguments = parsed
            if command == "ls-files" and prints_only_names(arguments):
                reader_reads.update(listing_reads(exec_event, options, arguments, roots, fold=fold))
            else:
                gopts = parse_git_global_options(exec_event, options)
                if gopts is None:
                    reader_reads.paths.add(EVERY_PATH)
                else:
                    prefix = working_prefix(gopts, roots, fold=fold)
                    context = GitObjectContext(held_paths(project.root), replaced, gopts.replace_refs_off, key in replace_ref_seen)
                    attributed: Reads | None = None
                    if command == "cat-file":
                        attributed = cat_file_reads(arguments, prefix, project, context)
                    elif command == "show":
                        attributed = show_reads(arguments, prefix, project, context)
                    elif command == "ls-tree":
                        attributed = ls_tree_reads(arguments, prefix, gopts, project)
                    elif command == "archive":
                        attributed = archive_reads(arguments, prefix, gopts, project)
                    elif command == "rev-parse":
                        attributed = rev_parse_reads(arguments)
                    elif command == "merge-base":
                        attributed = merge_base_reads(arguments, context)
                    elif command == "diff":
                        attributed = diff_reads(arguments, prefix, gopts, project)
                    elif command == "add":
                        attributed = git_add_reads(arguments, prefix, gopts, project)
                    elif command == "restore":
                        attributed = restore_reads(arguments, prefix, gopts, project)
                    elif command == "checkout":
                        attributed = checkout_path_reads(arguments, prefix, gopts, project)
                    elif command == "grep":
                        attributed = grep_reads(arguments, prefix, gopts, project)
                    if attributed is None:
                        reader_reads.paths.add(EVERY_PATH)
                    else:
                        reader_reads.update(attributed)
        if EVERY_PATH in reader_reads.paths:
            reader_reads.whole_tree_commands.setdefault("content", set()).add(format_command(exec_event, fallback_executable))
        if EVERY_PATH in reader_reads.memberships:
            reader_reads.whole_tree_commands.setdefault("membership", set()).add(format_command(exec_event, fallback_executable))
        reads.update(reader_reads)
    return reads


def repository_paths(events: list[Event], temporary: str | None, project: Checkout, root: str | None = None,
                     copies: list[str] | None = None) -> Callable[[str, str], str | None]:
    """The repository path that a path maps to, given the kind of event that names it, or None: a path inside the checkout,
    or inside a temporary repository copy. ``root`` is the checkout in the form of the event paths, ``project.root`` by
    default; ``copies`` are the roots of the copies, found in ``events`` by default."""
    root = root or str(project.root)
    if copies is None:
        copies = copy_roots(events, temporary, root) if temporary else []

    def mapped(path: str, kind: str) -> str | None:
        relative = None
        if path == root or path.startswith(root + "/"):
            relative = path[len(root) + 1:]
        else:
            for copy in copies:
                if path == copy or path.startswith(copy + "/"):
                    candidate = path[len(copy) + 1:]
                    if candidate in project.tracked or candidate in project.directories or kind in ("absent", "exec-absent"):
                        relative = candidate
                    break
        # Git tracks no path with a .git component: such a path lies in the Git directory of the checkout or of a
        # repository nested in it or in a copy.
        if relative is None or ".git" in relative.lower().split("/"):
            return None
        return relative

    return mapped


def relative_events(events: list[Event], temporary: str | None, project: Checkout, root: str | None = None,
                    copies: list[str] | None = None) -> list[tuple[Event, str]]:
    """The events inside the checkout or a temporary repository copy, with the repository path they map to."""
    mapped = repository_paths(events, temporary, project, root, copies)
    return [(event, relative) for event in events if (relative := mapped(event.path, event.kind)) is not None]


def ignored_paths(project: Checkout, paths: set[str], directories: set[str], *, fold: bool = False) -> set[str]:
    """The paths that the ignore rules of the checked commit match and that are not tracked; ``directories`` are the
    paths that the recording shows as a directory."""
    return {path for path in paths if path and path not in project.tracked and path not in project.directories
            and project.ignore.ignored(path, path in directories, fold=fold)}


def is_command_search(event: Event) -> bool:
    """An absent probe that a PowerShell process makes in its working directory for a name with ``.ps1`` or a
    ``PATHEXT`` extension, as its command search probes it."""
    return event.kind == "absent" and event.executable in POWERSHELL and posixpath.dirname(event.path) == event.cwd \
        and event.path.lower().endswith(COMMAND_EXTENSIONS)


def is_git_revision_path(event: Event, exec_event: Event | None) -> bool:
    """An absent probe that a ``git`` process makes for a ``<revision>:<path>`` argument before ``--``."""
    if event.kind != "absent" or not is_git(event.executable) or event.cwd is None:
        return False
    parsed = git_command(exec_event)
    if parsed is None:
        return False
    _, _, arguments = parsed
    before_dash = arguments[:arguments.index("--")] if "--" in arguments else arguments
    for argument in before_dash:
        if argument.startswith("-") or ":" not in argument or "://" in argument or argument.endswith(":"):
            continue
        if argument.startswith((":(", ":!", ":^", ":/")):
            continue
        if resolve(event.cwd, argument) == event.path:
            return True
    return False


@dataclass(frozen=True)
class UnitRecording:
    """One unit's events and lookup reports, with its temporary directory and the checkout, ``root`` in the form of the
    event paths (``project.root`` by default)."""

    events: list[Event]
    temporary: str | None
    project: Checkout
    root: str | None = None
    lookups: Lookups = field(default_factory=Lookups)

    @functools.cached_property
    def copies(self) -> list[str]:
        """The roots of the temporary repository copies that the events create."""
        return copy_roots(self.events, self.temporary, self.root or str(self.project.root)) if self.temporary else []

    @functools.cached_property
    def to_repository(self) -> Callable[[str, str], str | None]:
        return repository_paths(self.events, self.temporary, self.project, self.root, self.copies)


def classify(unit: UnitRecording, *, fold: bool = False) -> Reads:
    """The reads of one unit's events and lookup reports. A listing is a directory membership unless the lookup report
    of its process attributes it to the import system: each process's listings of a directory count against the
    listings that its report names there. A module name looked up in a checkout directory without being found there, or
    found there as a namespace portion, is a module lookup, and a found module is a content read. A stat of an existing
    file by a process with a lookup report (``reported_stats``) is an existence read of kind ``any`` unless the unit opens,
    reads or runs that file or a report names it as a found module; a stat by any other process is a content read."""
    events, project, lookups, to_repository = unit.events, unit.project, unit.lookups, unit.to_repository
    reported = reported_stats(unit)
    active_exec: dict[str, Event] = {}
    mapped: list[tuple[Event, str, bool, Event | None]] = []
    for index, event in enumerate(events):
        if event.kind == "exec":
            active_exec[event.pid] = event
        if (relative := to_repository(event.path, event.kind)) is not None:
            mapped.append((event, relative, index in reported, active_exec.get(event.pid)))
    missing = [(relative, module.name) for module in lookups.missing if (relative := to_repository(module.directory, "list")) is not None]
    found = [(relative, to_repository(module.file, "read")) for module in lookups.found
             if (relative := to_repository(module.directory, "list")) is not None]
    shown = {relative for event, relative, _, _ in mapped
             if event.kind in ("list", "statdir", "diropen")
             or (event.kind in ("stat", "exists") and relative and relative not in project.tracked and (project.root / relative).is_dir())}
    shown.update(relative for relative, _ in missing + found)
    dropped = ignored_paths(project, {relative for _, relative, _, _ in mapped} | shown | {file for _, file in found if file is not None},
                            shown, fold=fold)
    reads = Reads()
    directories: set[str] = set()
    absent: set[str] = set()
    reported_files: set[str] = set()  # the existing files of the reported stats
    listed: dict[tuple[str, str], int] = {}
    for event, relative, by_reporter, exec_event in mapped:
        if relative in dropped:
            continue
        kind = event.kind
        if kind == "list":
            key = (event.pid, event.path)
            listed[key] = listed.get(key, 0) + 1
            if listed[key] > lookups.listings.get(key, 0):
                reads.memberships.add(relative)
            continue
        is_directory = relative in project.directories and relative not in project.tracked
        if kind in ("read", "exec", "stat", "exists"):
            # A Windows stat, an enumeration of a literal name or an attribute-only open, does not say whether it names a
            # directory, so an untracked name that is a directory in the checkout is one; a tracked path, such as a
            # symbolic link, stays a file.
            if is_directory or ((kind == "exists" or (kind == "stat" and relative not in project.tracked)) and (project.root / relative).is_dir()):
                directories.add(relative)
            elif kind == "stat" and by_reporter:
                reported_files.add(relative)
            else:
                reads.paths.add(relative)
        elif kind in ("statdir", "diropen"):
            directories.add(relative)
        elif kind in ("absent", "exec-absent"):
            if not is_command_search(event) and not is_git_revision_path(event, exec_event):
                absent.add(relative)
    standard = suite_selection.standard_library_names()
    for relative, name in missing:
        if relative not in dropped:
            reads.lookup_directories.add(relative or ".")
            if name not in standard:
                reads.lookups.setdefault(relative or ".", set()).add(name)
    for relative, file in found:
        if relative not in dropped:
            reads.lookup_directories.add(relative or ".")
            if file is not None and file not in dropped:
                reads.paths.add(file)
    for path in directories - reads.memberships - {""}:
        reads.existence[path] = "directory"
    for path in (absent | reported_files) - reads.paths - {""}:
        reads.existence[path] = "any"
    reads.memberships = {"." if path == "" else path for path in reads.memberships}
    reads.update(git_directory_reads(events, unit.copies, project, unit.root, fold=fold))
    return reads


def reported_stats(unit: UnitRecording) -> set[int]:
    """The positions in ``unit.events`` of the stats that a process with a lookup report makes. A report names its
    process by id alone, so a stat counts only under a process id that runs at most one program in the unit, and only
    after that program's start: a shell that starts Python in its own process, a Python process that runs another
    program in its place, and on Windows a process id that a later process reuses all stat as unreported processes."""
    starts: dict[str, int] = {}
    for event in unit.events:
        if event.kind == "exec":
            starts[event.pid] = starts.get(event.pid, 0) + 1
    started: set[str] = set()
    found: set[int] = set()
    for index, event in enumerate(unit.events):
        if event.kind == "exec":
            started.add(event.pid)
        elif event.kind == "stat" and event.pid in unit.lookups.processes and (starts.get(event.pid, 0) == 0 or
                                                                                (starts[event.pid] == 1 and event.pid in started)):
            found.add(index)
    return found


@dataclass(frozen=True)
class ProbeRule:
    names: tuple[str, ...]  # ``{file}`` stands for the name of the solution or project file
    anchors: tuple[str, ...]  # project, solution, argument or working
    upward: bool


@dataclass(frozen=True)
class Toolchain:
    programs: frozenset[str]
    project_extensions: tuple[str, ...]
    solution_extensions: tuple[str, ...]
    rules: tuple[ProbeRule, ...]


@dataclass(frozen=True)
class Start:
    """A program start whose working directory and arguments ``check`` knows, both in the form of the event paths."""

    program: str  # as ``program_name`` gives it
    cwd: str | None  # None where the recording knows no working directory
    arguments: tuple[str, ...]  # each argument that is no option, resolved against ``cwd``


@functools.cache
def toolchains() -> tuple[Toolchain, ...]:
    document = json.loads(TOOLCHAIN_PROBES.read_text(encoding="utf-8"))
    return tuple(Toolchain(frozenset(item["programs"]), tuple(item.get("project_extensions", ())), tuple(item.get("solution_extensions", ())),
                           tuple(ProbeRule(tuple(rule["names"]), tuple(rule["from"]), rule["upward"]) for rule in item["rules"]))
                 for item in document["toolchains"])


def checkout_relative(path: str, root: str, fold: bool = False) -> str | None:
    """The repository path of ``path``, ``""`` for the checkout root, or None outside the checkout; both in the form of
    the event paths. ``fold`` compares without case, as Windows paths compare."""
    probe, base = (path.lower(), root.lower()) if fold else (path, root)
    if probe == base:
        return ""
    return path[len(root) + 1:] if probe.startswith(base + "/") else None


def ancestors(directory: str) -> list[str]:
    """The checkout directory and every directory above it in the checkout, the root as ``""``."""
    chain = [directory]
    while directory:
        directory = posixpath.dirname(directory)
        chain.append(directory)
    return chain


# Non-project toolchain flags that consume the next argument as their value (e.g. dotnet --results-directory out).
TOOLCHAIN_OPTIONS_WITH_VALUE = frozenset({
    "-o", "--output", "--results-directory", "-r", "--runtime", "-f", "--framework",
    "-c", "--configuration", "-l", "--logger", "-p", "--property", "-v", "--verbosity",
    "--artifacts-path", "--arch", "-a", "--os", "--source", "-s", "--configfile",
    "--packages", "--filter", "--diag", "--collect", "--settings", "--test-adapter-path",
    "-e", "--environment", "--blame-hang-timeout", "--blame-crash-dump-type",
    "--blame-hang-dump-type", "-t", "--target", "-m", "--maxCpuCount",
})


def non_option_arguments(argv: tuple[str, ...] | list[str]) -> list[str]:
    """The non-option arguments of ``argv[1:]`` before ``--``, skipping values of non-project flags."""
    found: list[str] = []
    index = 1
    while index < len(argv):
        argument = argv[index]
        if argument == "--":
            break
        if argument.startswith("-"):
            if "=" not in argument and argument in TOOLCHAIN_OPTIONS_WITH_VALUE:
                index += 2
                continue
            index += 1
            continue
        found.append(argument)
        index += 1
    return found


def exec_start(event: Event, fold: bool) -> Start:
    """An exec event as a start: its absolute arguments, and its relative ones resolved against the process's working
    directory where the recording knows it. On Windows (``fold``) the recorded command gives the relative arguments of
    the only process with a known working directory, spelled as Git tracks them."""
    arguments = []
    for argument in non_option_arguments(event.arguments or ()):
        if argument.startswith("/"):
            arguments.append(normalize(argument))
        elif event.cwd is not None and not fold:
            arguments.append(resolve(event.cwd, argument))
    return Start(event.executable, event.cwd, tuple(arguments))


def working_directories(events: list[Event], temporary: str | None, project: Checkout, recorded: Start, root: str | None, fold: bool,
                        copies: list[str] | None = None) -> set[str]:
    """The checkout directories in which the processes of ``events`` ran: each process's own working directory where
    the recording knows it; for a process whose working directory it does not know, the working directory of the
    recorded command and every checkout directory in which the process read."""
    base = root or str(project.root)
    known = {event.cwd for event in events if event.cwd is not None}
    unplaced = {event.pid for event in events if event.cwd is None}
    directories = {relative for cwd in known if (relative := checkout_relative(cwd, base, fold)) is not None}
    if unplaced:
        if (relative := checkout_relative(recorded.cwd or "", base, fold)) is not None:
            directories.add(relative)
        for event, relative in relative_events([event for event in events if event.pid in unplaced], temporary, project, root, copies):
            directories.add(relative if event.kind in ("list", "diropen", "statdir") else posixpath.dirname(relative))
    return directories


def toolchain_probes(events: list[Event], temporary: str | None, project: Checkout, recorded: Start, root: str | None = None,
                     fold: bool = False, copies: list[str] | None = None) -> set[str]:
    """The names that the probe rules of each toolchain whose program the recording shows running probe: each name of a
    rule in each directory it starts from and, for an upward rule, in every directory above it in the checkout.
    ``recorded`` is the unit's recorded command."""
    base = root or str(project.root)
    starts = [recorded, *(exec_start(event, fold) for event in events if event.kind == "exec")]
    probes = set()
    for toolchain in toolchains():
        toolchain_events = [event for event in events if event.executable in toolchain.programs]
        toolchain_starts = [start for start in starts if start.program in toolchain.programs]
        if not toolchain.rules or not (toolchain_events or toolchain_starts):
            continue
        working = {(directory, "") for directory in working_directories(toolchain_events, temporary, project, recorded, root, fold, copies)}
        anchors: dict[str, set[tuple[str, str]]] = {"project": set(), "solution": set(), "argument": set(), "working": working}
        files = [relative for _, relative in relative_events([event for event in toolchain_events if event.kind == "read"], temporary, project, root, copies)]
        for start in toolchain_starts:
            for argument in start.arguments:
                relative = checkout_relative(argument, base, fold)
                if relative is None:
                    continue
                if relative.lower().endswith(toolchain.project_extensions + toolchain.solution_extensions):
                    files.append(relative)
                elif relative in project.directories or (project.root / relative).is_dir():
                    anchors["argument"].add((relative, ""))
        for relative in files:
            if relative.lower().endswith(toolchain.project_extensions):
                anchors["project"].add((posixpath.dirname(relative), posixpath.basename(relative)))
            elif relative.lower().endswith(toolchain.solution_extensions):
                anchors["solution"].add((posixpath.dirname(relative), posixpath.basename(relative)))
        for rule in toolchain.rules:
            for anchor in rule.anchors:
                for directory, file in anchors[anchor]:
                    for candidate in ancestors(directory) if rule.upward else [directory]:
                        probes.update(posixpath.join(candidate, name.format(file=file)) for name in rule.names)
    return probes


def preceding_modules(unit: UnitRecording) -> dict[str, str]:
    """The names that would precede each found checkout module in its directory, as a path finder tries them: its
    package directory, and its name with each file suffix that the finder tries before the found file's suffix (the
    extension-module suffixes for a source file). A found package has none."""
    names: dict[str, str] = {}
    for module in unit.lookups.found:
        relative = unit.to_repository(module.directory, "list")
        base = posixpath.basename(module.file)
        if relative is None or posixpath.dirname(module.file) != module.directory or not base.startswith(module.name):
            continue
        suffix, suffixes = base[len(module.name):], module.suffixes
        names[posixpath.join(relative, module.name)] = "directory"
        for earlier in suffixes[:suffixes.index(suffix)] if suffix in suffixes else suffixes:
            names[posixpath.join(relative, module.name + earlier)] = "any"
    return names


def derived_existence_reads(unit: UnitRecording, recorded: Start, fold: bool = False) -> dict[str, str]:
    """The existence reads that ``check`` derives from a unit's events and lookup reports beside the reads it classifies:
    the names that the probe rules of a toolchain that ran probe, and the names that would precede a found module."""
    derived = {path: "any" for path in toolchain_probes(unit.events, unit.temporary, unit.project, recorded, unit.root, fold, unit.copies)}
    for path, kind in preceding_modules(unit).items():
        derived[path] = "any" if derived.get(path, kind) != kind else kind
    return derived


def unit_reads(unit: UnitRecording, start: Start, fold: bool = False, memberships: set[str] = frozenset()) -> Reads:
    """The classified reads of one unit with every derived existence read that the recording does not already hold, and
    with ``memberships`` as directory memberships in place of any existence read of the same directory, without the
    existence reads that its directory memberships cover. ``start`` is the recorded command; ``fold`` compares paths
    without case, as on Windows."""
    reads = classify(unit, fold=fold)
    derived = derived_existence_reads(unit, start, fold)
    derived_directories = {path for path, kind in derived.items() if kind == "directory"}
    ignored_derived = ignored_paths(unit.project, set(derived), derived_directories, fold=fold)
    for path, kind in derived.items():
        if path in ignored_derived:
            continue
        if path not in reads.paths and path not in reads.existence:
            reads.existence[path] = kind
    for directory in memberships:
        reads.memberships.add(directory)
        reads.existence.pop(directory, None)
    for path in [path for path, kind in reads.existence.items() if held_existence(path, kind, reads.memberships, unit.project.tracked)]:
        del reads.existence[path]
    return reads


def held_existence(path: str, kind: str, memberships: set[str], tracked: frozenset[str]) -> bool:
    """Whether the directory memberships of a unit cover its existence read of ``path``: one of kind ``any`` of a direct
    child of a held directory, and any below a tracked file in a held directory, which a removal or rename of that file
    changes."""
    def held(directory: str) -> bool:
        return (directory or ".") in memberships

    parts = path.split("/")
    if kind == "any" and held(posixpath.dirname(path)):
        return True
    return any(file in tracked and held(posixpath.dirname(file)) for file in ("/".join(parts[:index]) for index in range(1, len(parts))))


def recorded_start(record: dict) -> Start:
    """The recorded command of a Linux unit as a start."""
    cwd = normalize(record["cwd"])
    return Start(program_name(record["command"][0]), cwd, tuple(resolve(cwd, argument) for argument in non_option_arguments(record["command"])))


def recorded_windows_start(record: dict, paths: WindowsPaths) -> Start:
    """The recorded command of a Windows unit as a start, its paths mapped as ``paths`` maps the event paths."""
    arguments = tuple(paths.convert(argument if re.match(r"^[A-Za-z]:[\\/]", argument) else record["cwd"] + "\\" + argument)
                      for argument in non_option_arguments(record["command"]))
    return Start(program_name(record["command"][0]), paths.convert(record["cwd"]), arguments)


# --- the stored lists -------------------------------------------------------------------------------------------


def literal(pattern: str) -> bool:
    return not any(character in pattern for character in suite_selection.GLOB_CHARACTERS)


def covers_tree(pattern: str, directory: str) -> bool:
    """The pattern matches every path beneath the directory, so that any change there is a matching changed file."""
    pattern = pattern.strip("/")
    if pattern == "**":
        return True
    if not pattern.endswith("/**") or not literal(pattern[:-3]):
        return False
    base = pattern[:-3]
    return directory == base or directory.startswith(base + "/")


@dataclass(frozen=True)
class Stored:
    patterns: tuple[str, ...]
    memberships: tuple[str, ...]
    existence: tuple[tuple[str, str], ...]
    tracked: frozenset[str] = frozenset()  # the tracked files of the checkout, which hold an existing directory
    lookup_directories: tuple[str, ...] = ()
    lookups: dict[str, tuple[str, ...]] = field(default_factory=dict)  # the suite list's module lookups by directory

    @functools.cached_property
    def literal_patterns(self) -> frozenset[str]:
        return frozenset(pattern for pattern in self.patterns if literal(pattern))

    @functools.cached_property
    def covers_all(self) -> bool:
        return any(pattern.strip("/") == "**" for pattern in self.patterns)

    @functools.cached_property
    def tree_bases(self) -> frozenset[str]:
        return frozenset(stripped[:-3] for pattern in self.patterns
                         if (stripped := pattern.strip("/")).endswith("/**") and literal(stripped[:-3]))

    @functools.cached_property
    def non_tree_globs(self) -> tuple[str, ...]:
        return tuple(pattern for pattern in self.patterns
                     if not literal(pattern) and (stripped := pattern.strip("/")) != "**"
                     and not (stripped.endswith("/**") and literal(stripped[:-3])))

    @functools.cached_property
    def membership_set(self) -> frozenset[str]:
        return frozenset(membership.strip("/") for membership in self.memberships)

    @functools.cached_property
    def membership_all(self) -> bool:
        return "**" in self.membership_set

    @functools.cached_property
    def membership_tree_bases(self) -> frozenset[str]:
        return frozenset(stored[:-3] for stored in self.membership_set if stored.endswith("/**") and literal(stored[:-3]))

    @functools.cached_property
    def existence_map(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for stored, stored_kind in self.existence:
            mapping[stored] = stored_kind if mapping.get(stored, stored_kind) == stored_kind else "any"
        return mapping

    @functools.cached_property
    def covered_directories(self) -> frozenset[str]:
        directories: set[str] = set()
        if self.covers_all:
            covered_files: Collection[str] = self.tracked
        elif not self.tree_bases and not self.non_tree_globs:
            covered_files = self.literal_patterns & self.tracked
        else:
            covered_files = [file for file in self.tracked if self.covers_path(file)]
        for file in covered_files:
            directory = posixpath.dirname(file)
            while directory and directory not in directories:
                directories.add(directory)
                directory = posixpath.dirname(directory)
        return frozenset(directories)

    @functools.cached_property
    def lookup_directory_set(self) -> frozenset[str]:
        return frozenset(self.lookup_directories)

    @functools.cached_property
    def lookup_sets(self) -> dict[str, frozenset[str]]:
        return {directory: frozenset(names) for directory, names in self.lookups.items()}

    def _in_bases(self, path: str, bases: frozenset[str], *, include_self: bool) -> bool:
        if not bases:
            return False
        current = path if include_self else posixpath.dirname(path)
        while current:
            if current in bases:
                return True
            current = posixpath.dirname(current)
        return False

    def covers_path(self, path: str) -> bool:
        if self.covers_all or path in self.literal_patterns:
            return True
        if self._in_bases(path, self.tree_bases, include_self=False):
            return True
        return any(suite_selection.matches(pattern, path) for pattern in self.non_tree_globs)

    def covers_membership(self, directory: str) -> bool:
        base = "" if directory == "." else directory
        if self.membership_all or self.covers_all:
            return True
        if directory in self.membership_set or (base == "" and ("." in self.membership_set or "" in self.membership_set)) or base in self.membership_set:
            return True
        if (base + "/*").lstrip("/") in self.membership_set or (base + "/**").lstrip("/") in self.membership_set:
            return True
        if self._in_bases(base, self.membership_tree_bases, include_self=True):
            return True
        return self._in_bases(base, self.tree_bases, include_self=True)

    def covers_existence(self, path: str, kind: str) -> bool:
        stored_kind = self.existence_map.get(path)
        if stored_kind is not None and (stored_kind == "any" or stored_kind == kind):
            return True
        if kind == "any":
            parent = posixpath.dirname(path)
            if parent in self.membership_set or (parent == "" and ("." in self.membership_set or "" in self.membership_set)):
                return True
        if self.covers_all:
            return True
        if kind == "directory":
            if self._in_bases(path, self.tree_bases, include_self=True):
                return True
            # The directory exists in the checkout: it disappears only when the tracked files beneath it go.
            return path in self.covered_directories
        if self._in_bases(path, self.tree_bases, include_self=False):
            return True
        return self.covers_path(path)


def stored_lists(suite: suite_selection.Suite, entry: str, always: tuple[str, ...], tracked: frozenset[str] = frozenset(),
                 lookups: dict[str, tuple[str, ...]] | None = None) -> Stored | None:
    """The stored lists that select one entry file of a suite, or None when the suite or the entry file has none;
    ``lookups`` are the module lookups of the suite list."""
    if suite.paths is None or suite.lacks_list(entry):
        return None
    patterns, memberships, existence = suite.patterns_of(entry), suite.memberships_of(entry), suite.existence_reads_of(entry)
    return Stored(tuple(patterns) + always, tuple(memberships), tuple((read.path_on(suite.os), read.kind) for read in existence), tracked,
                  suite.lookup_directories_of(entry), lookups or {})


def outside(reads: Reads, stored: Stored | None) -> Reads:
    """The reads that the stored lists do not cover; every read when there is no stored list."""
    if stored is None:
        return Reads(set(reads.paths), dict(reads.existence), set(reads.memberships), set(reads.lookup_directories),
                     {directory: set(names) for directory, names in reads.lookups.items()},
                     {kind: set(commands) for kind, commands in reads.whole_tree_commands.items() if commands})
    lookups = {directory: {name for name in names if name not in stored.lookup_sets.get(directory, frozenset())}
               for directory, names in reads.lookups.items()}
    missing_paths = {path for path in reads.paths if not stored.covers_path(path)}
    missing_memberships = {directory for directory in reads.memberships if not stored.covers_membership(directory)}
    missing_commands: dict[str, set[str]] = {}
    if EVERY_PATH in missing_paths and reads.whole_tree_commands.get("content"):
        missing_commands["content"] = set(reads.whole_tree_commands["content"])
    if EVERY_PATH in missing_memberships and reads.whole_tree_commands.get("membership"):
        missing_commands["membership"] = set(reads.whole_tree_commands["membership"])
    return Reads(missing_paths,
                 {path: kind for path, kind in reads.existence.items() if not stored.covers_existence(path, kind)},
                 missing_memberships,
                 {directory for directory in reads.lookup_directories if directory not in stored.lookup_directory_set},
                 {directory: names for directory, names in lookups.items() if names},
                 missing_commands)


# --- check ----------------------------------------------------------------------------------------------------


@dataclass
class Unit:
    record: dict
    reads: Reads
    writes: tuple[str, ...] = ()


def load_units(output: Path) -> list[dict]:
    units = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((output / "units").glob("*.json"))]
    return sorted(units, key=lambda unit: (unit["kind"], unit["name"], unit["unit"]))


def parse_unit(arguments: tuple[str, tuple[str, ...], str]) -> list[Event]:
    trace, keep, start = arguments
    path = Path(trace)
    return parse_trace(path, keep, start) if path.is_file() else []


def classify_units(output: Path, project: Checkout, records: list[dict], settings: dict | None = None) -> list[Unit]:
    if settings is not None and settings["trace_set"] == "etw":
        return classify_etw_units(output, project, records, settings)
    jobs = [(str(output / "traces" / record["trace"]), (str(project.root), record["temporary"]), record["cwd"]) for record in records]
    if len(jobs) > 1:
        with concurrent.futures.ProcessPoolExecutor(max_workers=2) as pool:
            parsed = list(pool.map(parse_unit, jobs))
    else:
        parsed = [parse_unit(job) for job in jobs]
    return [Unit(record, unit_reads(UnitRecording(events, record["temporary"], project, lookups=unit_lookups(output, record, normalize)),
                                    recorded_start(record)),
                 tuple(sorted({event.path for event in events if event.kind == "write"}))) for record, events in zip(records, parsed)]


def unit_lookups(output: Path, record: dict, convert: Callable[[str], str], *, by_thread: bool = True) -> Lookups:
    """The lookup reports of one unit; none for a unit recorded without them."""
    return read_lookups(output / "lookups" / record["lookups"], convert, by_thread=by_thread) if record.get("lookups") else Lookups()


def unreported_node_memberships(unit: UnitRecording, *, fold: bool = False) -> set[str]:
    """The checkout directories in which a Node process without a lookup report opened a file, the root as ``.``: on
    Windows no recording sees the modules it looks up, so each such directory is a directory membership."""
    node_pids = {event.pid for event in unit.events if event.executable in NODE and event.pid not in unit.lookups.processes}
    directories = set()
    for event in unit.events:
        if event.pid in node_pids and event.kind == "read" and (relative := unit.to_repository(event.path, event.kind)) is not None:
            if relative not in unit.project.directories and not (unit.project.root / relative).is_dir():
                directories.add(posixpath.dirname(relative))
    return {directory or "." for directory in directories - ignored_paths(unit.project, directories, directories, fold=fold)}


def process_started(pid: int) -> int | None:
    """The start time of the running process ``pid``, in the system's own unit, or None when no process runs under it. A
    process id comes back after its process ends; the start time tells the processes that held it apart."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            code, times = wintypes.DWORD(), [wintypes.FILETIME() for _ in range(4)]
            # An ended process whose handle another process still holds can be opened; its exit code is no longer 259.
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
                return None
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(time) for time in times)):
                return None
            return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
        finally:
            kernel.CloseHandle(handle)
    try:
        status = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    fields = status[status.rindex(")") + 2:].split()  # the command name in parentheses may hold spaces
    return None if fields[0] in ("Z", "X") else int(fields[19])


def consumer_runs(settings: dict) -> bool:
    """Whether the ETW consumer that start stored still runs: a process under its id with its start time."""
    pid, started = settings.get("consumer"), settings.get("consumer_started")
    return isinstance(pid, int) and started is not None and process_started(pid) == started


def read_log(log: Path) -> tuple[re.Match | None, str]:
    """The consumer's summary line in its log, None before it wrote one, and the log's text."""
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    return ETW_SUMMARY.search(text), text


def quoted_log(log: Path, text: str) -> str:
    return f"its log {log} reads: {text.strip()}" if text.strip() else f"its log {log} is empty"


def stop_session(output: Path, settings: dict) -> tuple[float, re.Match]:
    """Stop the ETW consumer unless it has stopped, and return the seconds the stop took and its summary line. A consumer
    whose process is gone without a summary line fails at once; one that still runs fails after the wait."""
    log, stop = output / "logs" / "etw.log", output / "logs" / "etw.stop"
    consumer = f"the ETW consumer (process {settings.get('consumer')})"
    started = time.perf_counter()
    deadline = None  # set when check writes the stop file
    while True:
        found, text = read_log(log)
        if found is None and not consumer_runs(settings):
            found, text = read_log(log)  # the summary line may have come just before the consumer ended
            if found is None:
                when = "before the stop" if deadline is None else "after the stop without writing its summary line"
                raise RecordingError(f"{consumer} ended {when}; {quoted_log(log, text)}")
        if found is not None:
            return time.perf_counter() - started, found
        if deadline is None:
            stop.write_text("stop\n", encoding="utf-8")
            deadline = time.monotonic() + ETW_WAIT_SECONDS
        elif time.monotonic() > deadline:
            raise RecordingError(f"{consumer} still runs {ETW_WAIT_SECONDS} s after the stop; {quoted_log(log, text)}")
        time.sleep(0.05)


def classify_etw_units(output: Path, project: Checkout, records: list[dict], settings: dict) -> list[Unit]:
    seconds, summary = stop_session(output, settings)
    drained, events, lines, lost = (int(value) for value in summary.groups())
    if lost:
        raise RecordingError(f"the ETW session lost {lost} events; the recording is incomplete")
    started = time.perf_counter()
    root = str(project.root) if os.sep == "/" else dos_form(str(project.root))
    # Paths under the recorded checkout map to the checked one; a Git directory outside it keeps its recorded form.
    project = replace(project, git_directories=(root + "/.git", *(dos_form(path) for path in settings.get("git_directories", ()))))
    paths = WindowsPaths.of(settings, root, project)
    grouped = attribute(read_events(output / settings["events"]), records)
    units = []
    for record in records:
        unit_events = windows_events(grouped[record["unit"]], record, paths)
        recording = UnitRecording(unit_events, dos_form(record["temporary"]), project, root, unit_lookups(output, record, paths.convert, by_thread=False))
        units.append(Unit(record, unit_reads(recording, recorded_windows_start(record, paths), fold=True,
                                             memberships=unreported_node_memberships(recording, fold=True)),
                          tuple(sorted({event.path for event in unit_events if event.kind == "write"}))))
    settings["etw_note"] = (f"Stopped the ETW session in {seconds:.2f} s (the consumer drained it in {drained} ms: {events} events, "
                            f"{lines} lines, none lost); parsed and classified the event file in {time.perf_counter() - started:.2f} s.")
    print(settings["etw_note"])
    return units


def suite_of(suite_list: suite_selection.SuiteList, record: dict) -> suite_selection.Suite:
    entry = record["name"]
    owners = [suite for suite in suite_list.suites if entry in suite.entries and record.get("suite") in (None, suite.name)]
    if len(owners) != 1:
        named = f" in the suite {record['suite']}" if record.get("suite") else ""
        problem = "names it in more than one suite; pass --suite to exec" if owners else f"lacks it{named}"
        raise RecordingError(f"the entry file {entry} ran under the recording, but the suite list {problem}")
    return owners[0]


def entry_reads(suite_list: suite_selection.SuiteList, units: list[Unit]) -> list[tuple[suite_selection.Suite, Unit, Reads]]:
    """Every recorded entry file with its suite and its reads, its suite's setups' reads included."""
    setups: dict[str, Reads] = {}
    for unit in units:
        if unit.record["kind"] == "setup":
            setups.setdefault(unit.record["name"], Reads()).update(unit.reads)
    result = []
    for unit in units:
        if unit.record["kind"] != "entry":
            continue
        suite = suite_of(suite_list, unit.record)
        reads = Reads()
        reads.update(unit.reads)
        for setup in suite.setups_of(unit.record["name"]):
            if setup in setups:
                reads.update(setups[setup])
        result.append((suite, unit, reads))
    return result


def additions_document(list_path: str, suite_list: suite_selection.SuiteList, recorded: list[tuple[suite_selection.Suite, Unit, Reads]],
                       tracked: frozenset[str]) -> dict:
    suites: dict[str, dict] = {}
    recorded_entries: dict[str, set[str]] = {}
    for suite, unit, reads in recorded:
        entry = unit.record["name"]
        missing = outside(reads, stored_lists(suite, entry, suite_list.always, tracked, suite_list.unit_lookups_map(suite, entry)))
        item = suites.setdefault(suite.name, {"suite": suite.name, "create": suite.paths is None, "recorded_entries": []})
        recorded_entries.setdefault(suite.name, set()).add(entry)
        if suite.entry_selection:
            if not missing.empty() or suite.paths is None or suite.lacks_list(entry):
                item.setdefault("entries", {})[entry] = missing.document()
        else:
            total = Reads.of(item.get("reads", {}))
            total.update(missing)
            item["reads"] = total.document()
        if kept_reason(unit.record) is None:
            item.setdefault("replace_entries", {})[entry] = reads.document()
        if unit.record.get("exit_code") == 0:
            seconds = unit.record.get("seconds")
            if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and math.isfinite(seconds) and seconds >= 0:
                item.setdefault("durations", {})[entry] = seconds
    for name, item in suites.items():
        item["recorded_entries"] = sorted(recorded_entries[name])
    changes = [item for item in suites.values()
               if item["create"] or item.get("entries") or item.get("replace_entries") or item.get("durations")
               or not Reads.of(item.get("reads", {})).empty()]
    for item in changes:
        if "reads" in item and Reads.of(item["reads"]).empty() and not item["create"]:
            del item["reads"]
    return {"kind": "additions", "suite_list": list_path, "suites": sorted(changes, key=lambda item: item["suite"])}


def describe(reads: dict) -> list[str]:
    return ([f"path `{path}`" for path in reads.get("paths", ())]
            + [f"{read['kind']} existence `{read['path']}`" for read in reads.get("existence_reads", ())]
            + [f"directory membership `{path}`" for path in reads.get("directory_memberships", ())]
            + [f"lookup directory `{path}`" for path in reads.get("lookup_directories", ())]
            + [f"module lookup `{name}` in `{directory}`" for directory, names in reads.get("module_lookups", {}).items() for name in names])


def additions_lines(document: dict) -> list[tuple[str, str]]:
    """(where, what) pairs of every addition, in suite and entry order."""
    lines = []
    for item in document["suites"]:
        if item["create"]:
            lines.append((f"`{item['suite']}`", "a stored path list (the suite has none)"))
        lines += [(f"`{item['suite']}`", text) for text in describe(item.get("reads", {}))]
        for entry, reads in sorted(item.get("entries", {}).items()):
            lines += [(f"`{item['suite']}`: `{entry}`", text) for text in describe(reads)]
    return lines


def suite_list_path(root: Path, suites: Path) -> str:
    resolved = suites.resolve()
    return resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else str(suites)


def granted_write_directories(output: Path, provided: dict[str, dict], suite: suite_selection.Suite, record: dict,
                              *, etw: bool = False) -> tuple[str, ...]:
    """The directories into which an entry file may write: its own temporary, bytecode-cache, per-user cache and lookup
    directories, and the writable directories that its stated setups provide."""
    raw = [record["temporary"], record["cache"]]
    if record.get("user_cache"):
        raw.append(record["user_cache"])
    if record.get("lookups"):
        raw.append(str(output / "lookups" / record["lookups"]))
        if etw:
            temp = dos_form(record["temporary"])
            if posixpath.basename(temp) == "tmp" and posixpath.basename(posixpath.dirname(posixpath.dirname(temp))) == "work":
                raw.append(posixpath.dirname(posixpath.dirname(posixpath.dirname(temp))) + "/lookups/" + record["lookups"])
    for setup in suite.setups_of(record["name"]):
        raw.extend(provided.get(setup, {}).get("writes", ()))
    directories: list[str] = []
    for directory in raw:
        forms = [dos_form(directory)] if etw else [normalize(directory), normalize(os.path.realpath(directory))]
        if etw and os.path.exists(directory):
            forms.append(dos_form(os.path.realpath(directory)))
        for form in forms:
            if form not in directories:
                directories.append(form)
    return tuple(directories)


WINDOWS_SYSTEM_PREFIXES = ("windows", "programdata/microsoft/crypto")
WINDOWS_USER_CACHE_SUBPATHS = (
    "/appdata/locallow/microsoft/cryptneturlcache",
    "/appdata/roaming/microsoft/systemcertificates",
)
MAX_DEPARTURES_PER_ENTRY = 10


def is_windows_system_path(probe: str, system_root: str | None = None) -> bool:
    """Whether a lowercased Windows ``dos_form`` path outside the checkout lies in an OS directory or OS-managed cache."""
    if system_root and (probe == system_root or probe.startswith(system_root + "/")):
        return True
    if len(probe) >= 4 and probe[0] == "/" and probe[1].isalpha() and probe[2:4] == ":/":
        rest = probe[4:]
        if any(rest == prefix or rest.startswith(prefix + "/") for prefix in WINDOWS_SYSTEM_PREFIXES):
            return True
    return any(probe.endswith(subpath) or (subpath + "/") in probe for subpath in WINDOWS_USER_CACHE_SUBPATHS)


def write_departures(output: Path, recorded: list[tuple[suite_selection.Suite, Unit, Reads]], root: Path,
                     *, etw: bool = False) -> list[tuple[str, str]]:
    """(where, path) pairs for every entry file that wrote outside its setup's grants."""
    provided = provisions(output)
    checkout_root = (str(root) if os.sep == "/" else dos_form(str(root))) if etw else normalize(str(root))
    folded_root = checkout_root.lower()
    raw_system_root = os.environ.get("SystemRoot") if etw else None
    system_root = dos_form(raw_system_root).lower() if raw_system_root and re.match(r"^[A-Za-z]:[\\/]", raw_system_root) else None
    departures: list[tuple[str, str]] = []
    for suite, unit, _ in recorded:
        if not unit.writes:
            continue
        grants = granted_write_directories(output, provided, suite, unit.record, etw=etw)
        folded_grants = tuple(grant.lower() for grant in grants) if etw else grants
        where = f"`{suite.name}`: `{unit.record['name']}`"
        for path in unit.writes:
            probe = path.lower() if etw else path
            if any(probe == grant or probe.startswith(grant + "/") for grant in folded_grants):
                continue
            inside_checkout = (probe if etw else path).startswith((folded_root if etw else checkout_root) + "/")
            if etw and not inside_checkout and is_windows_system_path(probe, system_root):
                continue
            if inside_checkout:
                display = path[len(checkout_root) + 1:]
            elif etw and re.match(r"^/[A-Za-z]:(/|$)", path):
                display = path[1:]
            else:
                display = path
            departures.append((where, display))
    return departures


def bounded_departures(departures: list[tuple[str, str]]) -> list[tuple[str, list[str], int, int]]:
    """Group ``(where, path)`` departures by entry file, yielding ``(where, shown, omitted, total)``."""
    grouped: dict[str, list[str]] = {}
    for where, path in departures:
        grouped.setdefault(where, []).append(path)
    return [(where, paths[:MAX_DEPARTURES_PER_ENTRY], max(0, len(paths) - MAX_DEPARTURES_PER_ENTRY), len(paths))
            for where, paths in grouped.items()]


def emit_warning(message: str) -> None:
    """Emit a GitHub Actions ``::warning::`` annotation with workflow-command escaping."""
    escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    print(f"::warning::{escaped}", flush=True)


def report_departures(departures: list[tuple[str, str]]) -> None:
    """Print each write departure and emit bounded warning annotations on a runner."""
    on_runner = os.environ.get("GITHUB_ACTIONS") == "true"
    for where, shown, omitted, total in bounded_departures(departures):
        for path in shown:
            message = f"Departure: {where} wrote outside its setup's grants: `{path}`"
            print(message)
            if on_runner and not omitted:
                emit_warning(message)
        if omitted:
            print(f"Departure: {where} wrote to {omitted} more path{'' if omitted == 1 else 's'} outside its setup's grants ({total} total)")
            if on_runner:
                emit_warning(f"Departure: {where} wrote to {total} paths outside its setup's grants "
                             f"({omitted} omitted from log; first: `{shown[0]}`)")


def departures_summary(departures: list[tuple[str, str]]) -> str:
    if not departures:
        return ""
    lines = ["", "### Departures", "", "| Entry file | Write outside setup grants |", "| --- | --- |"]
    for where, shown, omitted, total in bounded_departures(departures):
        lines += [f"| {where} | `{path}` |" for path in shown]
        if omitted:
            lines.append(f"| {where} | ... and {omitted} more path{'' if omitted == 1 else 's'} ({total} total) |")
    return "\n".join(lines) + "\n"


def entry_times_artifact_name(name: str) -> str:
    """The artifact name for a document that holds entry times or list replacements without read additions."""
    if not name.startswith("recording-additions"):
        raise RecordingError(f"--artifact-name {name!r} must start with recording-additions")
    return "recording-entry-times" + name[len("recording-additions"):]


def _format_unit(suite_name: str, entry: str | None) -> str:
    return f"`{suite_name}`: `{entry}`" if entry is not None else f"`{suite_name}`"


def unit_estimated_duration(suite: suite_selection.Suite, entry: str | None) -> str:
    """The unit's estimated duration per run from the suite list, or ``none stored`` when absent."""
    durations = dict(suite.entry_durations)
    if entry is not None:
        seconds = durations.get(entry)
        return f"{seconds:.2f} s" if seconds is not None else "none stored"
    values = [durations[item] for item in suite.entries if item in durations]
    return f"{sum(values):.2f} s" if values else "none stored"


def unreasoned_whole_tree_findings(suite_list: suite_selection.SuiteList,
                                   recorded: list[tuple[suite_selection.Suite, Unit, Reads]],
                                   tracked: frozenset[str]) -> list[tuple[str, str, tuple[str, ...], str]]:
    """Every newly recorded bare ``**`` (outside the stored lists) whose unit of selection lacks a recorded reason for
    that kind: ``(where, kind, commands, estimated_duration)`` in suite, entry and kind order. Already-stored ``**``
    reads stay inside the stored lists so pushes to the default branch keep working; ``select`` fails pull requests on
    them."""
    grouped: dict[tuple[str, str | None, str], tuple[suite_selection.Suite, set[str]]] = {}
    for suite, unit, reads in recorded:
        entry = unit.record["name"]
        missing = outside(reads, stored_lists(suite, entry, suite_list.always, tracked, suite_list.unit_lookups_map(suite, entry)))
        unit_entry = entry if suite.entry_selection else None
        for kind, present in (("content", EVERY_PATH in missing.paths), ("membership", EVERY_PATH in missing.memberships)):
            if present and suite.whole_tree_reason(entry, kind) is None:
                _, commands = grouped.setdefault((suite.name, unit_entry, kind), (suite, set()))
                commands.update(missing.whole_tree_commands.get(kind, ()))
    findings: list[tuple[str, str, tuple[str, ...], str]] = []
    for (suite_name, unit_entry, kind), (suite, commands) in sorted(grouped.items(), key=lambda item: (item[0][0], item[0][1] or "", item[0][2])):
        findings.append((_format_unit(suite_name, unit_entry), kind, tuple(sorted(commands)) or ("unknown",),
                         unit_estimated_duration(suite, unit_entry)))
    return findings


def run_check(options: argparse.Namespace) -> int:
    output = output_directory(options.output)
    settings = read_settings(output)
    root = options.root.resolve()
    project = checkout(root)
    suite_list = suite_selection.resolve_entries(suite_selection.load_suite_list(options.suites), set(project.tracked), str(options.suites))
    recorded = entry_reads(suite_list, classify_units(output, project, load_units(output), settings))
    departures = write_departures(output, recorded, root, etw=settings["trace_set"] == "etw")
    report_departures(departures)
    list_path = suite_list_path(root, options.suites)
    if settings["mode"] == "full":
        return write_full_result(options, output, list_path, recorded, settings, departures)
    document = additions_document(list_path, suite_list, recorded, project.tracked)
    target = options.additions or output / "additions.json"
    target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    unreasoned = unreasoned_whole_tree_findings(suite_list, recorded, project.tracked)
    unreasoned_pairs = {(where, kind) for where, kind, _, _ in unreasoned}
    lines = [(where, what) for where, what in additions_lines(document)
             if not ((what == "path `**`" and (where, "content") in unreasoned_pairs)
                     or (what == "directory membership `**`" and (where, "membership") in unreasoned_pairs))]
    failed = bool(lines or unreasoned)
    has_changes = bool(document["suites"])
    if lines:
        print(f"The recording reads outside the stored lists of {options.suites}. Apply the additions with:")
        print(f"  python3 recording.py apply {target}")
        for where, what in lines:
            print(f"  {where}: {what}")
    if unreasoned:
        print(f"The recording reports whole-tree reads (`**`) without a recorded reason in {options.suites}:")
        for where, kind, commands, duration in unreasoned:
            print(f"  {where}: {kind} `**` (command: {', '.join(commands)}; estimated duration per run: {duration})")
    if not failed:
        print(f"Every recorded read of {len(recorded)} entry file{'' if len(recorded) == 1 else 's'} lies inside the stored lists.")
    if options.summary is not None:
        with options.summary.open("a", encoding="utf-8") as handle:
            route = additions_route(target.name, project.tracked, os.environ.get("GITHUB_RUN_ID"), os.environ.get("GITHUB_EVENT_NAME"),
                                    holds_automation_caller(root))
            handle.write(check_summary(lines, len(recorded), route, unreasoned=unreasoned) + departures_summary(departures) + etw_summary(settings))
    if options.github_output is not None:
        artifact = ""
        if options.artifact_name:
            artifact = options.artifact_name if failed else (entry_times_artifact_name(options.artifact_name) if has_changes else "")
        with options.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"mode=pull-request\nfile={target if has_changes else ''}\n")
            if options.artifact_name:
                handle.write(f"artifact-name={artifact}\n")
    return 1 if failed else 0


# A tracked file that only the repository of the shared parts holds; there the additions follow its maintenance documentation.
SHARED_PARTS_MARKER = "actions/recording/recording.py"


# The fixed caller of the automation workflow in a consumer that adopted the pipeline, and the reference by which it calls that workflow.
AUTOMATION_CALLER = ".github/workflows/automation.yml"
AUTOMATION_CALL = re.compile(r"(?m)^\s*uses:\s*[^\s/#]+/[^\s/#]+/\.github/workflows/automation\.yml@\S+")


def holds_automation_caller(root: Path) -> bool:
    """Whether the repository's ``.github/workflows/automation.yml`` is the fixed caller: the file that calls the shared
    automation workflow. A repository that runs the suite selection in its earlier form holds no such caller."""
    try:
        return AUTOMATION_CALL.search((root / AUTOMATION_CALLER).read_text(encoding="utf-8")) is not None
    except (OSError, UnicodeDecodeError):
        return False


def additions_route(file_name: str, tracked: Collection[str], run_id: str | None, event: str | None, automation: bool = True) -> str:
    """The sentence that sends the reader of a failed check to the route that applies its additions in this repository:
    the maintenance documentation's in the repository of the shared parts, and in a consumer the route of the form it
    runs. ``automation`` says that the consumer holds the fixed caller ``automation.yml``, so the additions workflow is
    the part ``additions`` of the automation workflow; without that caller the consumer runs the suite selection in its
    earlier form, whose additions workflow is a workflow of its own, ``Apply recorded additions``."""
    if SHARED_PARTS_MARKER in tracked:
        return (f"Download the additions artifacts (`{file_name}` in each) and apply them to the suite list "
                'as "Validation" in `docs/maintaining.md` states.')
    if automation:
        kit = 'as the section "Additions" of the suite-selection consumer kit states'
        workflow = "the additions workflow, the part `additions` of the automation workflow,"
        decides = "the run of `CI` on the new head decides"
        run = f"`{run_id}`" if run_id else "of the pull request's failed `CI` run"
        manual = (f"start the fixed caller `automation.yml` by hand with the part `additions` and the run id {run} "
                  f"(`gh workflow run automation.yml -f part=additions -f run-id={run_id or '<run id>'}`)")
    else:
        kit = 'as the section "When a repository adopted it earlier" of the suite-selection consumer kit states for a repository that keeps that workflow'
        workflow = "the additions workflow, the repository's own workflow `Apply recorded additions`,"
        decides = "the validation of the new head decides"
        run = f"`{run_id}`" if run_id else "of the pull request's validation run"
        manual = f"start the workflow `Apply recorded additions` by hand with the run id {run}"
    if run_id and event != "pull_request":
        return (f"This {event} run belongs to no pull request, so the additions workflow cannot propose them; the run of a "
                f"pull request that makes the same reads reports them again, and that pull request applies them {kit}.")
    return (f"For a pull request from a branch of this repository, {workflow} commits them onto its branch as the GitHub App, "
            f"and {decides}. When it comments that it cannot, {manual}, and merge the pull request it opens into this pull "
            f"request's branch, {kit}.")


def check_summary(lines: list[tuple[str, str]], count: int, route: str,
                  unreasoned: list[tuple[str, str, tuple[str, ...], str]] = ()) -> str:
    text = ["## Recording check", ""]
    if not lines and not unreasoned:
        text.append(f"Every recorded read of {count} entry file{'' if count == 1 else 's'} lies inside the stored lists.")
        return "\n".join(text) + "\n"
    if lines:
        text += [f"The recording reads outside the stored lists. {route}", "",
                 "| Suite or entry file | Addition |", "| --- | --- |"]
        text += [f"| {where} | {what} |" for where, what in lines]
    if unreasoned:
        if lines:
            text.append("")
        text += [
            "### Whole-tree reads (`**`) without a reason",
            "",
            "A `**` read is not applied automatically. Resolve each entry in one of two ways: "
            "change the test so its inputs are explicit (fixture files, a temporary repository not copied from the checkout, "
            "or walks and pathspecs limited to the directories it reads), or record a reason for that unit and kind under "
            "`entry_whole_tree_reasons` (or `whole_tree_reasons` for a suite selected as a whole) in the suite list.",
            "",
            "| Unit | Kind | Command | Estimated duration per run |",
            "| --- | --- | --- | --- |",
        ]
        for where, kind, commands, duration in unreasoned:
            command_text = ", ".join(f"`{cmd}`" for cmd in commands)
            text.append(f"| {where} | {kind} | {command_text} | {duration} |")
    return "\n".join(text) + "\n"


def etw_summary(settings: dict) -> str:
    return f"\n{settings['etw_note']}\n" if "etw_note" in settings else ""


def write_full_result(options: argparse.Namespace, output: Path, list_path: str,
                      recorded: list[tuple[suite_selection.Suite, Unit, Reads]], settings: dict,
                      departures: list[tuple[str, str]] = ()) -> int:
    document = {"kind": "full-recording", "suite_list": list_path,
                **({"job": settings["job"]} if "job" in settings else {}),
                "entries": [{"suite": suite.name, "entry": unit.record["name"], **({"job": unit.record["job"]} if "job" in unit.record else {}),
                             "exit_code": unit.record["exit_code"], "skipped": unit.record["skipped"],
                             "deselected": unit.record.get("deselected", 0), "incomplete_report": unit.record.get("incomplete_report"),
                             "seconds": unit.record["seconds"], "reads": reads.document()}
                            for suite, unit, reads in recorded]}
    target = options.additions or output / "full-recording.json"
    target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote the full recording of {len(recorded)} entry file{'' if len(recorded) == 1 else 's'} to {target}.")
    if options.summary is not None:
        recorder = "a live Kernel-File ETW session" if "etw_note" in settings else "the wide trace set"
        with options.summary.open("a", encoding="utf-8") as handle:
            handle.write(f"## Full recording\n\nRecorded {len(recorded)} entry files under {recorder} into `{target.name}`.\n"
                         + departures_summary(departures) + etw_summary(settings))
    if options.github_output is not None:
        with options.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"mode=full\nfile={target}\n")
            if options.artifact_name:
                handle.write(f"artifact-name={options.artifact_name}\n")
    return 0


# --- keep -----------------------------------------------------------------------------------------------------


def read_json_file(path: Path, what: str) -> object:
    try:
        # utf-8-sig: a passed list that PowerShell wrote may start with a byte order mark.
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as error:
        raise RecordingError(f"{path} holds no {what}: {error}") from None


def entries_with_additions(document: object, source: Path) -> set[tuple[str, str]]:
    """The (suite, entry file) pairs that an additions document of ``check`` marks: each entry file listed with additions,
    and every recorded entry file of a suite whose additions are pooled."""
    not_additions = RecordingError(f"{source} is no additions document of check")
    if not isinstance(document, dict) or document.get("kind") != "additions" or not isinstance(document.get("suites"), list):
        raise not_additions
    marked = set()
    for item in document["suites"]:
        if not isinstance(item, dict) or not isinstance(item.get("suite"), str):
            raise not_additions
        # A suite selected per entry file lists its entry files with additions; a pooled suite has no such list.
        if "entries" in item:
            entries = item["entries"]
            if not isinstance(entries, dict) or not all(isinstance(entry, str) for entry in entries):
                raise not_additions
            marked |= {(item["suite"], entry) for entry in entries}
        else:
            entries = item.get("recorded_entries")
            if not isinstance(entries, list) or not all(isinstance(entry, str) for entry in entries):
                raise not_additions
            if "reads" in item or item.get("create"):
                marked |= {(item["suite"], entry) for entry in entries}
    return marked


def run_keep(options: argparse.Namespace) -> int:
    marked = entries_with_additions(read_json_file(options.additions, "additions document"), options.additions)
    passed = read_json_file(options.passed, "passed list")
    if not isinstance(passed, list) or not all(isinstance(record, dict) and isinstance(record.get("suite"), str)
                                               and isinstance(record.get("entry"), str) for record in passed):
        raise RecordingError(f"{options.passed} is no list of passed entry files with their suites")
    # An entry file with additions stays in the list, marked: its pass counts only under the lists with the additions applied.
    kept = [{"suite": record["suite"], "entry": record["entry"]} | ({"additions": True} if (record["suite"], record["entry"]) in marked else {})
            for record in passed]
    left = [record["entry"] for record in kept if record.get("additions")]
    options.passed.write_text(json.dumps(kept, indent=2) + "\n", encoding="utf-8")
    print(f"Kept {len(kept) - len(left)} of {len(passed)} passed entry file{'' if len(passed) == 1 else 's'} for reuse under the stored lists"
          + (f"; marked with additions, for reuse only under the lists with the additions applied: {', '.join(left)}." if left else "."))
    return 0


# --- apply ----------------------------------------------------------------------------------------------------


def _unique_key(value: object) -> object:
    return (value["path"], value["kind"]) if isinstance(value, dict) else value


def add_unique(values: list, new: list) -> list:
    seen = {_unique_key(value) for value in values}
    result = list(values)
    for value in new:
        key = _unique_key(value)
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _merge_unit_lookups(existing: dict[str, list[str]], incoming: dict[str, list[str]]) -> dict[str, list[str]]:
    merged = {directory: list(names) for directory, names in existing.items() if names}
    for directory, names in sorted(incoming.items()):
        if names:
            merged[directory] = add_unique(merged.get(directory, []), sorted(names))
    return merged


def add_reads(item: dict, reads: dict, entry: str | None, *, fallback_lookups: dict[str, list[str]] | None = None) -> None:
    """Add reads to a suite's JSON entry: to its own fields, or to the per-entry fields of one entry file."""
    prior_dirs = tuple(item.get("lookup_directories", ()) if entry is None else item.get("entry_lookup_directories", {}).get(entry, ()))
    for read_field in READ_FIELDS:
        values = reads.get(read_field.key, [])
        if not values and not read_field.kept_empty:
            continue
        if entry is None:
            item[read_field.key] = add_unique(item.get(read_field.key, []), values)
        else:
            per_entry = item.setdefault(read_field.entry_key, {})
            per_entry[entry] = add_unique(per_entry.get(entry, []), values)
    incoming = reads.get("module_lookups", {})
    if entry is None:
        if "module_lookups" in item:
            item["module_lookups"] = _merge_unit_lookups(item["module_lookups"], incoming)
        elif incoming:
            seed = {directory: list(fallback_lookups[directory]) for directory in prior_dirs
                    if fallback_lookups and fallback_lookups.get(directory)}
            item["module_lookups"] = _merge_unit_lookups(seed, incoming)
    else:
        per_entry_lookups = item.get("entry_module_lookups", {})
        if entry in per_entry_lookups:
            per_entry_lookups[entry] = _merge_unit_lookups(per_entry_lookups[entry], incoming)
        elif incoming:
            seed = {directory: list(fallback_lookups[directory]) for directory in prior_dirs
                    if fallback_lookups and fallback_lookups.get(directory)}
            item.setdefault("entry_module_lookups", {})[entry] = _merge_unit_lookups(seed, incoming)


def replace_reads(item: dict, reads: dict, entry: str | None) -> None:
    """Replace the recorded lists of a suite or of one entry file of a suite selected per entry file."""
    for read_field in READ_FIELDS:
        values = list(reads.get(read_field.key, []))
        target, name = (item, read_field.key) if entry is None else (item.setdefault(read_field.entry_key, {}), entry)
        if values or read_field.kept_empty:
            target[name] = values
        else:
            target.pop(name, None)
        if entry is not None and not target:
            del item[read_field.entry_key]
    unit_lookups = {directory: sorted(names) for directory, names in sorted(reads.get("module_lookups", {}).items()) if names}
    has_dirs = bool(reads.get("lookup_directories")) or bool(unit_lookups)
    if entry is None:
        if has_dirs:
            item["module_lookups"] = unit_lookups
        else:
            item.pop("module_lookups", None)
    else:
        per_entry = item.setdefault("entry_module_lookups", {})
        if has_dirs:
            per_entry[entry] = unit_lookups
        else:
            per_entry.pop(entry, None)
        if not per_entry:
            del item["entry_module_lookups"]


def start_list(item: dict, suite: suite_selection.Suite, entries: tuple[str, ...], recorded: set[str]) -> None:
    """Give a suite without a stored path list one, once every entry file of the suite is recorded."""
    unrecorded = [entry for entry in entries if entry not in recorded]
    if unrecorded:
        raise RecordingError(f"the suite {suite.name} has no stored path list and these entry files are not recorded: "
                             f"{', '.join(unrecorded)}; apply the additions of every job that ran the suite together")
    item["paths"] = item.get("paths", [])
    if suite.entry_selection:
        item["entry_paths"] = {entry: item.get("entry_paths", {}).get(entry, []) for entry in suite.entries}


def join_entry_lists(item: dict) -> None:
    """Write the lists of a suite selected per entry file as the union of its entry files' lists."""
    if not item.get("entry_selection") or "entry_paths" not in item:
        return
    for read_field in READ_FIELDS:
        seen: set[object] = set()
        joined: list = []
        for values in item.get(read_field.entry_key, {}).values():
            for value in values:
                key = _unique_key(value)
                if key not in seen:
                    seen.add(key)
                    joined.append(value)
        if joined or read_field.kept_empty:
            item[read_field.key] = joined
        else:
            item.pop(read_field.key, None)


    if "entry_module_lookups" in item:
        joined_lookups: dict[str, list[str]] = {}
        for entry_lookups in item["entry_module_lookups"].values():
            joined_lookups = _merge_unit_lookups(joined_lookups, entry_lookups)
        if joined_lookups or item.get("lookup_directories"):
            item["module_lookups"] = joined_lookups
        else:
            item.pop("module_lookups", None)
    else:
        item.pop("module_lookups", None)


def canonical_order(item: dict) -> None:
    """Sort the recorded lists of a suite's JSON entry, and order its per-entry fields by its entry files, so that the
    rendered suite list does not depend on the order of the results that apply was given."""
    order = {entry: index for index, entry in enumerate(item["entries"])}

    def by_entry(entry: str) -> tuple[int, str]:
        return order.get(entry, len(order)), entry

    def sort(values: list) -> list:
        return sorted(values, key=lambda value: (value["path"], value["kind"]) if isinstance(value, dict) else (value, ""))

    def sort_lookups(mapping: dict[str, list[str]]) -> dict[str, list[str]]:
        return {directory: sorted(mapping[directory]) for directory in sorted(mapping) if mapping[directory]}

    for read_field in READ_FIELDS:
        if read_field.key in item:
            item[read_field.key] = sort(item[read_field.key])
        if read_field.entry_key in item:
            item[read_field.entry_key] = {entry: sort(item[read_field.entry_key][entry]) for entry in sorted(item[read_field.entry_key], key=by_entry)}
    if "module_lookups" in item:
        item["module_lookups"] = sort_lookups(item["module_lookups"])
    if "entry_module_lookups" in item:
        item["entry_module_lookups"] = {entry: sort_lookups(item["entry_module_lookups"][entry])
                                        for entry in sorted(item["entry_module_lookups"], key=by_entry)}
    if "entry_durations" in item:
        item["entry_durations"] = {entry: item["entry_durations"][entry] for entry in sorted(item["entry_durations"], key=by_entry)}
    if "whole_tree_reasons" in item:
        item["whole_tree_reasons"] = {kind: item["whole_tree_reasons"][kind]
                                      for kind in suite_selection.WHOLE_TREE_KINDS if kind in item["whole_tree_reasons"]}
    if "entry_whole_tree_reasons" in item:
        item["entry_whole_tree_reasons"] = {
            entry: {kind: item["entry_whole_tree_reasons"][entry][kind]
                    for kind in suite_selection.WHOLE_TREE_KINDS if kind in item["entry_whole_tree_reasons"][entry]}
            for entry in sorted(item["entry_whole_tree_reasons"], key=by_entry)}


def drop_stale_keys(item: dict, suite: suite_selection.Suite) -> list[str]:
    """Remove from the per-entry fields that the recording writes every key that names no entry file any more, the key
    of a deleted or renamed file, and return those keys. ``suite`` is the suite with its patterns expanded to the
    tracked files."""
    entries = set(suite.entries)
    stale: set[str] = set()
    for key in suite_selection.RECORDED_ENTRY_FIELDS:
        gone = [entry for entry in item.get(key, {}) if entry not in entries]
        for entry in gone:
            del item[key][entry]
        stale.update(gone)
        if gone and not item[key] and key != "entry_paths":
            del item[key]
    return sorted(stale)


def is_string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def reads_problem(reads: object, where: str) -> str | None:
    """Why ``reads`` is no reads object as ``Reads.document`` writes it, or None."""
    if not isinstance(reads, dict):
        return f"{where} is no object"
    unknown = sorted(set(reads) - {read_field.key for read_field in READ_FIELDS} - {"module_lookups", "whole_tree_commands"})
    if unknown:
        return f"{where} holds the unknown field {unknown[0]}"
    for key in ("paths", "directory_memberships", "lookup_directories"):
        if not is_string_list(reads.get(key, [])):
            return f"{where}: {key} is no list of strings"
    existence = reads.get("existence_reads", [])
    if not isinstance(existence, list) or not all(isinstance(read, dict) and set(read) == {"path", "kind"} and isinstance(read["path"], str)
                                                  and isinstance(read["kind"], str) for read in existence):
        return f"{where}: existence_reads is no list of objects with a path and a kind"
    lookups = reads.get("module_lookups", {})
    if not isinstance(lookups, dict) or not all(is_string_list(names) for names in lookups.values()):
        return f"{where}: module_lookups is no object of lists of strings"
    commands = reads.get("whole_tree_commands", {})
    if not isinstance(commands, dict) or not set(commands) <= set(suite_selection.WHOLE_TREE_KINDS) or not all(is_string_list(items) for items in commands.values()):
        return f"{where}: whole_tree_commands is no object mapping content or membership to lists of strings"
    return None


def additions_problem(document: dict) -> str | None:
    """Why an additions document is not in the form that ``additions_document`` writes, or None; apply reads nothing
    else from it."""
    if not isinstance(document.get("suite_list"), str):
        return "suite_list is no string"
    if not isinstance(document.get("suites"), list):
        return "suites is no list"
    for index, change in enumerate(document["suites"]):
        if not isinstance(change, dict):
            return f"suites[{index}] is no object"
        unknown = sorted(set(change) - {"suite", "create", "recorded_entries", "reads", "entries", "replace_entries", "durations"})
        if unknown:
            return f"suites[{index}] holds the unknown field {unknown[0]}"
        if not isinstance(change.get("suite"), str):
            return f"suites[{index}].suite is no string"
        if not isinstance(change.get("create"), bool):
            return f"suites[{index}].create is no boolean"
        if not is_string_list(change.get("recorded_entries")):
            return f"suites[{index}].recorded_entries is no list of strings"
        if "reads" in change and (problem := reads_problem(change["reads"], f"suites[{index}].reads")):
            return problem
        for field_name in ("entries", "replace_entries"):
            entries = change.get(field_name, {})
            if not isinstance(entries, dict):
                return f"suites[{index}].{field_name} is no object"
            for entry, reads in entries.items():
                if problem := reads_problem(reads, f"suites[{index}].{field_name}[{entry}]"):
                    return problem
        if "durations" in change:
            durations = change["durations"]
            if not isinstance(durations, dict):
                return f"suites[{index}].durations is no object"
            for entry, seconds in durations.items():
                if not isinstance(entry, str) or isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
                    return f"suites[{index}].durations[{entry}] is no non-negative number"
    return None


def full_recording_problem(document: dict) -> str | None:
    """Why a full-recording document is not in the form that ``write_full_result`` writes, or None."""
    unknown_top = sorted(set(document) - {"kind", "suite_list", "entries", "job"})
    if unknown_top:
        return f"document holds the unknown field {unknown_top[0]}"
    if not isinstance(document.get("suite_list"), str):
        return "suite_list is no string"
    if "job" in document and not isinstance(document["job"], str):
        return "job is no string"
    if not isinstance(document.get("entries"), list):
        return "entries is no list"
    allowed = {"suite", "entry", "job", "exit_code", "skipped", "deselected", "incomplete_report", "seconds", "reads"}
    for index, entry in enumerate(document["entries"]):
        if not isinstance(entry, dict):
            return f"entries[{index}] is no object"
        unknown = sorted(set(entry) - allowed)
        if unknown:
            return f"entries[{index}] holds the unknown field {unknown[0]}"
        if not isinstance(entry.get("suite"), str):
            return f"entries[{index}].suite is no string"
        if not isinstance(entry.get("entry"), str):
            return f"entries[{index}].entry is no string"
        if "job" in entry and entry["job"] is not None and not isinstance(entry["job"], str):
            return f"entries[{index}].job is no string"
        exit_code = entry.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            return f"entries[{index}].exit_code is no integer"
        skipped = entry.get("skipped")
        if skipped is not None and (isinstance(skipped, bool) or not isinstance(skipped, int)):
            return f"entries[{index}].skipped is no integer or null"
        deselected = entry.get("deselected", 0)
        if isinstance(deselected, bool) or not isinstance(deselected, int):
            return f"entries[{index}].deselected is no integer"
        incomplete = entry.get("incomplete_report")
        if incomplete is not None and not isinstance(incomplete, str):
            return f"entries[{index}].incomplete_report is no string or null"
        seconds = entry.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return f"entries[{index}].seconds is no number"
        if problem := reads_problem(entry.get("reads"), f"entries[{index}].reads"):
            return problem
    return None


def load_documents(paths: list[Path]) -> list[dict]:
    documents = []
    for path in paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RecordingError(f"cannot read {path}: {error}") from error
        if not isinstance(document, dict) or document.get("kind") not in ("additions", "full-recording"):
            raise RecordingError(f"{path} holds neither additions nor the result of a full recording")
        if document["kind"] == "additions":
            problem = additions_problem(document)
            if problem:
                raise RecordingError(f"{path} is no valid additions document: {problem}")
        else:
            problem = full_recording_problem(document)
            if problem:
                raise RecordingError(f"{path} is no valid full-recording document: {problem}")
        documents.append(document)
    if len({document["kind"] for document in documents}) != 1:
        raise RecordingError("apply takes either additions or full-recording results, not both")
    return documents


@dataclass
class ApplyOutcome:
    """Whole-tree side outcomes of ``apply`` for workflow step outputs."""

    skipped_whole_tree: list[str] = field(default_factory=list)
    unreasoned_whole_tree: bool = False


def run_apply(options: argparse.Namespace) -> int:
    documents = load_documents(options.files)
    root = options.root.resolve()
    suites_path = options.suites or root / documents[0]["suite_list"]
    text = suites_path.read_text(encoding="utf-8")
    raw = suite_selection.parse_suite_list(text, str(suites_path))
    tracked = set(checkout(root).tracked)
    resolved = {suite.name: suite for suite in suite_selection.resolve_entries(raw, tracked, str(suites_path)).suites}
    items = {suite.name: suite.entry() for suite in raw.suites}
    lookups = {directory: list(names) for directory, names in raw.module_lookups}
    durations_version = raw.entry_durations_version
    outcome = ApplyOutcome()
    if documents[0]["kind"] == "additions":
        if options.keep_lists is not None or options.keep_all_lists:
            raise RecordingError("--keep-lists and --keep-all-lists go with full-recording results")
        if options.expect_job:
            raise RecordingError("--expect-job goes with full-recording results")
        report = apply_additions(documents, raw, resolved, items, lookups, outcome=outcome)
    else:
        if options.expect_job:
            present = {entry["job"] for document in documents for entry in document["entries"] if entry.get("job")}
            missing_jobs = [job for job in dict.fromkeys(options.expect_job) if job not in present]
            if missing_jobs:
                raise RecordingError(f"the full recording is missing the result of: {', '.join(missing_jobs)}")
        report = apply_full(documents, raw, resolved, items, lookups, kept_lists(options, resolved), outcome=outcome)
        durations_version = suite_selection.ENTRY_DURATIONS_VERSION
    for name, item in items.items():
        report += [f"{name}: removed the stored lists and the estimate of {entry}: no entry names it" for entry in drop_stale_keys(item, resolved[name])]
        join_entry_lists(item)
        canonical_order(item)
    document = suite_selection.suite_list_document(raw, [items[suite.name] for suite in raw.suites],
                                                   {directory: sorted(lookups[directory]) for directory in sorted(lookups)},
                                                   entry_durations_version=durations_version)
    rendered = suite_selection.render_suite_list(suite_selection.parse_suite_list_document(document, str(suites_path)))
    if rendered != text:
        suites_path.write_text(rendered, encoding="utf-8", newline="\n")
    for line in report:
        print(line)
    if options.summary is not None:
        with options.summary.open("a", encoding="utf-8") as handle:
            handle.write("## Applied recording\n\n" + "".join(f"- {line}\n" for line in report))
    if options.github_output is not None:
        with options.github_output.open("a", encoding="utf-8") as handle:
            if documents[0]["kind"] == "full-recording":
                handle.write(f"unreasoned-whole-tree={'true' if outcome.unreasoned_whole_tree else 'false'}\n")
            elif outcome.skipped_whole_tree:
                delimiter = f"SKIPPED_WHOLE_TREE_{os.urandom(8).hex()}"
                handle.write(f"skipped-whole-tree-report<<{delimiter}\n"
                             + "\n".join(f"- {line}" for line in outcome.skipped_whole_tree) + f"\n{delimiter}\n")
    return 0


def known_suite(resolved: dict, name: str) -> suite_selection.Suite:
    if name not in resolved:
        raise RecordingError(f"the suite list lacks the suite {name}")
    return resolved[name]


def add_lookups(lookups: dict[str, list[str]], reads: Reads) -> None:
    """Add the module lookups of ``reads`` to the suite list's module lookups by directory."""
    for directory, names in sorted(reads.lookups.items()):
        if names:
            lookups[directory] = add_unique(lookups.get(directory, []), sorted(names))


def _strip_unreasoned_whole_tree(suite: suite_selection.Suite, entry: str | None, reads: Reads,
                                  skipped: dict[tuple[str, str | None, str], set[str]]) -> None:
    """Remove any bare ``**`` from ``reads`` whose unit lacks a recorded reason, recording its commands in ``skipped``."""
    unit = entry if suite.entry_selection else None
    if EVERY_PATH in reads.paths and suite.whole_tree_reason(entry, "content") is None:
        reads.paths.discard(EVERY_PATH)
        skipped.setdefault((suite.name, unit, "content"), set()).update(reads.whole_tree_commands.get("content", ()))
    if EVERY_PATH in reads.memberships and suite.whole_tree_reason(entry, "membership") is None:
        reads.memberships.discard(EVERY_PATH)
        skipped.setdefault((suite.name, unit, "membership"), set()).update(reads.whole_tree_commands.get("membership", ()))


def _format_whole_tree_commands(commands: Collection[str]) -> str:
    ordered = sorted(commands)
    return ", ".join(f"`{cmd}`" for cmd in ordered) if ordered else "`unknown`"


def apply_additions(documents: list[dict], raw: suite_selection.SuiteList, resolved: dict, items: dict, lookups: dict[str, list[str]],
                    *, outcome: ApplyOutcome | None = None) -> list[str]:
    merged: dict[str, dict] = {}
    for document in documents:
        for change in document["suites"]:
            target = merged.setdefault(change["suite"], {"create": False, "recorded": set(), "reads": Reads(), "entries": {}, "replace_entries": {}, "durations": {}})
            target["create"] |= change["create"]
            target["recorded"] |= set(change["recorded_entries"])
            target["reads"].update(Reads.of(change.get("reads", {})))
            for entry, reads in change.get("entries", {}).items():
                target["entries"].setdefault(entry, Reads()).update(Reads.of(reads))
            for entry, reads in change.get("replace_entries", {}).items():
                target["replace_entries"].setdefault(entry, Reads()).update(Reads.of(reads))
            for entry, seconds in change.get("durations", {}).items():
                target["durations"][entry] = seconds
    report = []
    skipped_whole_tree: dict[tuple[str, str | None, str], set[str]] = {}
    replaced: set[tuple[str, str]] = set()
    recorded_lookups: dict[str, set[str]] = {}
    initial_lookups = {directory: list(names) for directory, names in lookups.items()}
    for name, change in sorted(merged.items()):
        suite = known_suite(resolved, name)
        item = items[name]
        suite_entries = set(suite.entries)
        if change["create"] and suite.paths is None:
            start_list(item, suite, suite.entries, change["recorded"])
        if change["durations"]:
            durations = dict(item.get("entry_durations", {}))
            for entry, seconds in sorted(change["durations"].items()):
                if entry in suite_entries:
                    durations[entry] = new_estimate(seconds, durations.get(entry))
            if durations:
                item["entry_durations"] = durations
        replaced_here = 0
        added_paths = 0
        if suite.entry_selection:
            for entry, reads in sorted(change["replace_entries"].items()):
                if entry in suite_entries:
                    _strip_unreasoned_whole_tree(suite, entry, reads, skipped_whole_tree)
                    replace_reads(item, reads.document(), entry)
                    replaced.add((name, entry))
                    replaced_here += 1
                    for directory, names in reads.lookups.items():
                        recorded_lookups.setdefault(directory, set()).update(names)
            for entry, reads in sorted(change["entries"].items()):
                _strip_unreasoned_whole_tree(suite, entry, reads, skipped_whole_tree)
                if (name, entry) not in replaced and (not reads.empty() or suite.paths is None or suite.lacks_list(entry)):
                    add_reads(item, reads.document(), entry, fallback_lookups=initial_lookups)
                    add_lookups(lookups, reads)
                    added_paths += len(reads.paths)
        elif suite_entries and suite_entries <= set(change["replace_entries"]):
            union = Reads()
            for entry in suite.entries:
                union.update(change["replace_entries"][entry])
            _strip_unreasoned_whole_tree(suite, None, union, skipped_whole_tree)
            replace_reads(item, union.document(), None)
            replaced.update((name, entry) for entry in suite.entries)
            replaced_here = len(suite.entries)
            for directory, names in union.lookups.items():
                recorded_lookups.setdefault(directory, set()).update(names)
        else:
            _strip_unreasoned_whole_tree(suite, None, change["reads"], skipped_whole_tree)
            if not change["reads"].empty() or change["create"]:
                add_reads(item, change["reads"].document(), None, fallback_lookups=initial_lookups)
            add_lookups(lookups, change["reads"])
            added_paths = len(change["reads"].paths)
        if replaced_here and added_paths:
            report.append(f"{name}: replaced the stored lists of {replaced_here} complete entry file{'' if replaced_here == 1 else 's'} "
                          f"and added the recorded reads ({added_paths} content path{'' if added_paths == 1 else 's'} among them)")
        elif replaced_here:
            report.append(f"{name}: replaced the stored lists of {replaced_here} complete entry file{'' if replaced_here == 1 else 's'}")
        elif added_paths or change["create"] or not change["reads"].empty() or any(not r.empty() for r in change["entries"].values()) or change["durations"]:
            report.append(f"{name}: added the recorded reads ({added_paths} content path{'' if added_paths == 1 else 's'} among them)")
    skipped_lines: list[str] = []
    for (suite_name, unit, kind), commands in sorted(skipped_whole_tree.items(), key=lambda entry: (entry[0][0], entry[0][1] or "", entry[0][2])):
        line = f"{_format_unit(suite_name, unit)}: left out {kind} `**` without a recorded reason (command: {_format_whole_tree_commands(commands)})"
        skipped_lines.append(line)
        report.append(line)
    if outcome is not None:
        outcome.skipped_whole_tree = skipped_lines
    if replaced:
        directories_of_kept_lists = {directory for suite in resolved.values() for entry in suite.entries if (suite.name, entry) not in replaced
                                     for directory in suite.lookup_directories_of(entry)}
        replaced_old_dirs = {directory for suite in resolved.values() for entry in suite.entries if (suite.name, entry) in replaced
                             for directory in suite.lookup_directories_of(entry)}
        for directory in sorted(replaced_old_dirs | set(recorded_lookups)):
            kept_names = lookups.get(directory, []) if directory in directories_of_kept_lists else []
            names = add_unique(kept_names, sorted(recorded_lookups.get(directory, ())))
            if names:
                lookups[directory] = names
            else:
                lookups.pop(directory, None)
    return report or ["No additions to apply."]


def kept_reason(entry: dict | None) -> str | None:
    """Why a recording keeps an entry file's stored reads, or None when it may replace them."""
    if entry is None:
        return "it was not recorded"
    if entry.get("exit_code") != 0:
        return f"it exited with {entry.get('exit_code')}"
    if entry.get("skipped") is None:
        if entry.get("incomplete_report"):
            return f"it printed no complete test report: {entry['incomplete_report']}"
        return "its test runner reports no skip count"
    parts = []
    if entry["skipped"]:
        parts.append(f"{entry['skipped']} test{'' if entry['skipped'] == 1 else 's'} skipped")
    deselected = entry.get("deselected") or 0
    if deselected:
        parts.append(f"{deselected} test{'' if deselected == 1 else 's'} deselected")
    if parts:
        return ", ".join(parts)
    return None


# A stored duration estimate changes only when the corrected value differs from it by more than this share of it.
ESTIMATE_THRESHOLD = 0.25


def corrected_durations(entries: dict[tuple[str, str], dict], jobs: dict[tuple[str, str], tuple], resolved: dict,
                        items: dict, *, rescale: bool = False) -> dict[tuple[str, str], float]:
    """The corrected value of each recorded entry file: its measured entry time divided by the speed factor of its shard
    job. A job's factor is the median, over its entry files that ran clean (``kept_reason`` gives none) and have a stored
    estimate, of the measured entry time divided by that estimate; a job without such an entry file, or whose median is
    not positive, has factor 1. When ``rescale`` is True (stored ``entry_durations_version`` is older than the code's),
    no speed factor is applied across versions."""
    measured = {key: entry["seconds"] for key, entry in entries.items()}
    if rescale:
        return measured
    ratios: dict[tuple, list[float]] = {}
    for key, value in measured.items():
        stored = items[key[0]].get("entry_durations", {}).get(key[1])
        if stored and kept_reason(entries[key]) is None:
            ratios.setdefault(jobs[key], []).append(value / stored)
    factors = {job: median for job, values in ratios.items() if (median := statistics.median(values)) > 0}
    return {key: value / factors.get(jobs[key], 1.0) for key, value in measured.items()}


def new_estimate(corrected: float, stored: float | None) -> float:
    """The corrected value rounded to hundredths of a second, unless it lies within the threshold of the stored estimate."""
    return round(corrected, 2) if stored is None or abs(corrected - stored) > ESTIMATE_THRESHOLD * stored else stored


# Why apply keeps the stored reads of an entry file that the selection of the change since the recorded commit selects,
# and of every entry file when that selection could not be computed.
SELECTED_REASON = "the change since the recorded commit selects it"
UNSELECTED_REASON = "the change since the recorded commit could not be selected"


def kept_lists(options: argparse.Namespace, resolved: dict) -> dict[tuple[str, str], str]:
    """The suite and entry file pairs whose stored reads a full recording keeps, with the reason: every entry file under
    ``--keep-all-lists``; under ``--keep-lists``, each entry file of a suite that the selection runs, limited to the
    selection's ``entries`` of that suite when it names them."""
    if options.keep_all_lists:
        return {(suite.name, entry): UNSELECTED_REASON for suite in resolved.values() for entry in suite.entries}
    if options.keep_lists is None:
        return {}
    try:
        outputs = dict(line.split("=", 1) for line in options.keep_lists.read_text(encoding="utf-8").splitlines() if "=" in line)
        suites = json.loads(outputs["suites"])
        listed = json.loads(outputs.get("entries") or "{}")
    except (OSError, KeyError, ValueError) as error:
        raise RecordingError(f"cannot read the selection {options.keep_lists}: {error!r}") from error
    kept = {}
    for name, result in suites.items():
        if result == "run" and name in resolved:
            for entry in listed.get(name, resolved[name].entries):
                kept[(name, entry)] = SELECTED_REASON
    return kept


def _unit_has_whole_tree(item: dict, entry: str | None) -> dict[str, bool]:
    """Whether a suite JSON entry holds bare ``**`` for ``content`` and ``membership`` for ``entry`` (or whole suite)."""
    if entry is not None:
        paths = list(item.get("entry_paths", {}).get(entry, ())) + list(item.get("extra_paths", ())) + list(item.get("entry_extra_paths", {}).get(entry, ()))
        memberships = list(item.get("entry_directory_memberships", {}).get(entry, ())) + list(item.get("extra_directory_memberships", ()))
    else:
        paths = list(item.get("paths", ())) + list(item.get("extra_paths", ()))
        memberships = list(item.get("directory_memberships", ())) + list(item.get("extra_directory_memberships", ()))
    return {
        "content": any(v.strip("/") == EVERY_PATH for v in paths),
        "membership": any(v.strip("/") == EVERY_PATH for v in memberships),
    }


def apply_full(documents: list[dict], raw: suite_selection.SuiteList, resolved: dict, items: dict, lookups: dict[str, list[str]],
               kept: dict[tuple[str, str], str], *, outcome: ApplyOutcome | None = None) -> list[str]:
    """Replace the lists of every recorded entry file that exited with 0 and whose test report in an accepted format
    shows no skipped test, unless ``kept`` names it with a reason to keep its stored reads. The module lookups of a
    directory become those of the recorded entry files, and keep the stored ones while an entry file whose lists stay
    looks up modules there. The duration estimate of each entry file that exited with 0 follows its corrected value as
    ``corrected_durations`` (whose speed factor uses clean entry files only) and ``new_estimate`` say, judged per entry
    file, also in a suite selected as a whole; every other entry file keeps its stored estimate, or stays without one.
    Results for a suite or an entry file that the suite list lacks are left out and reported."""
    prior_whole_tree: set[tuple[str, str | None, str]] = set()
    for suite in resolved.values():
        units: tuple[str | None, ...] = suite.entries if suite.entry_selection else (None,)
        for unit in units:
            if any(p.strip("/") == EVERY_PATH for p in suite.patterns_of(unit)):
                prior_whole_tree.add((suite.name, unit, "content"))
            if any(m.strip("/") == EVERY_PATH for m in suite.memberships_of(unit)):
                prior_whole_tree.add((suite.name, unit, "membership"))
    entries: dict[tuple[str, str], dict] = {}
    jobs: dict[tuple[str, str], tuple] = {}
    unit_commands: dict[tuple[str, str | None, str], set[str]] = {}
    report: list[str] = []
    seen_report: set[str] = set()
    resolved_entries = {name: set(suite.entries) for name, suite in resolved.items()}
    for index, document in enumerate(documents):
        for entry in document["entries"]:
            key = (entry["suite"], entry["entry"])
            if key[0] not in resolved:
                line = f"left out the results of the suite {key[0]}: the suite list lacks it"
            elif key[1] not in resolved_entries[key[0]]:
                line = f"{key[0]}: left out the result of {key[1]}: the suite list lacks it"
            else:
                entries[key] = entry
                # A result without a job comes from a workflow that names none at start; each such file is one job's result.
                jobs[key] = ("job", entry["job"]) if entry.get("job") else ("result", index)
                unit_key = key[1] if resolved[key[0]].entry_selection else None
                for kind, cmds in entry["reads"].get("whole_tree_commands", {}).items():
                    unit_commands.setdefault((key[0], unit_key, kind), set()).update(cmds)
                continue
            if line not in seen_report:
                seen_report.add(line)
                report.append(line)
    rescale = raw.entry_durations_version < suite_selection.ENTRY_DURATIONS_VERSION
    corrected = corrected_durations(entries, jobs, resolved, items, rescale=rescale)
    recorded_lookups: dict[str, set[str]] = {}
    for entry in entries.values():
        for directory, names in entry["reads"].get("module_lookups", {}).items():
            recorded_lookups.setdefault(directory, set()).update(names)
    replaced: set[tuple[str, str]] = set()
    for name in sorted(resolved):
        suite = known_suite(resolved, name)
        item = items[name]
        recorded = {entry: entries.get((name, entry)) for entry in suite.entries}
        if suite.paths is None:
            missing_entries = [entry for entry in suite.entries if recorded[entry] is None]
            if missing_entries:
                report.append(f"{name}: left without stored lists: partly recorded (missing {', '.join(missing_entries)})")
                continue
            start_list(item, suite, suite.entries, set(suite.entries))
        reasons = {entry: kept_reason(result) for entry, result in recorded.items()}
        # What keeps a list: the reason of the recording, and for an entry file that ran clean, the reason ``kept`` gives.
        list_reasons = {entry: reason if reason is not None else kept.get((name, entry)) for entry, reason in reasons.items()}
        durations = dict(item.get("entry_durations", {}))
        for entry in suite.entries:
            result = recorded[entry]
            if result is not None and result.get("exit_code") == 0:
                durations[entry] = round(corrected[(name, entry)], 2) if rescale else new_estimate(corrected[(name, entry)], durations.get(entry))
            elif entry in durations:
                report.append(f"{name}: kept the estimate of {entry}: {reasons[entry]}")
        if durations:
            item["entry_durations"] = durations
        if suite.entry_selection:
            for entry, result in recorded.items():
                if result is None and suite.lacks_list(entry):
                    report.append(f"{name}: left {entry} without a stored list: {list_reasons[entry]}")
                elif result is None:
                    report.append(f"{name}: kept the list of {entry}: {list_reasons[entry]}")
                elif list_reasons[entry] is None:
                    replace_reads(item, result["reads"], entry)
                    replaced.add((name, entry))
                else:
                    add_reads(item, result["reads"], entry, fallback_lookups=lookups)
                    report.append(f"{name}: kept the list of {entry}: {list_reasons[entry]}")
            continue
        union = Reads()
        for result in recorded.values():
            if result is not None:
                union.update(Reads.of(result["reads"]))
        if all(reason is None for reason in list_reasons.values()):
            replace_reads(item, union.document(), None)
            replaced.update((name, entry) for entry in suite.entries)
        else:
            add_reads(item, union.document(), None, fallback_lookups=lookups)
            report += [f"{name}: kept the list of {entry}: {reason}" for entry, reason in list_reasons.items() if reason is not None]
    added_unreasoned = False
    for name in sorted(resolved):
        suite = known_suite(resolved, name)
        item = items[name]
        if suite.entry_selection:
            per_entry_reasons = item.get("entry_whole_tree_reasons", {})
            for entry in list(per_entry_reasons):
                if entry not in set(suite.entries):
                    del per_entry_reasons[entry]
                    report.append(f"{name}: removed the stale whole-tree reason for {entry}: no entry names it")
            units: tuple[str | None, ...] = suite.entries
        else:
            units = (None,)
        for unit in units:
            present = _unit_has_whole_tree(item, unit)
            unit_replaced = (name, unit) in replaced if unit is not None else all((name, e) in replaced for e in suite.entries)
            reasons = (item.get("entry_whole_tree_reasons", {}).get(unit) if unit is not None else item.get("whole_tree_reasons")) or {}
            if unit_replaced and reasons:
                for kind in suite_selection.WHOLE_TREE_KINDS:
                    if kind in reasons and not present[kind]:
                        del reasons[kind]
                        report.append(f"{_format_unit(name, unit)}: removed the stale {kind} whole-tree reason: the full recording no longer records `**`")
                if not reasons:
                    if unit is not None:
                        item["entry_whole_tree_reasons"].pop(unit, None)
                    else:
                        item.pop("whole_tree_reasons", None)
            for kind in suite_selection.WHOLE_TREE_KINDS:
                if present[kind] and (name, unit, kind) not in prior_whole_tree and kind not in reasons:
                    added_unreasoned = True
                    cmds = _format_whole_tree_commands(unit_commands.get((name, unit, kind), ()))
                    report.append(f"{_format_unit(name, unit)}: added {kind} `**` without a recorded reason (command: {cmds}); auto-merge disabled")
        if "entry_whole_tree_reasons" in item and not item["entry_whole_tree_reasons"]:
            del item["entry_whole_tree_reasons"]
    if outcome is not None:
        outcome.unreasoned_whole_tree = added_unreasoned
    directories_of_kept_lists = {directory for suite in resolved.values() for entry in suite.entries if (suite.name, entry) not in replaced
                                 for directory in suite.lookup_directories_of(entry)}
    stored_lookups = dict(lookups)
    lookups.clear()
    for directory in sorted(set(stored_lookups) | set(recorded_lookups)):
        names = add_unique(stored_lookups.get(directory, []) if directory in directories_of_kept_lists else [],
                           sorted(recorded_lookups.get(directory, ())))
        if names:
            lookups[directory] = names
    return report


# --- the shared digests ----------------------------------------------------------------------------------------


def loaded_modules(script: Path) -> list[Path]:
    """The script and every module of the shared CI that it loads when it runs (``recording_modules.py``), resolved on
    the search path that this script sets."""
    def read(path: str) -> str | None:
        return Path(path).read_text(encoding="utf-8") if Path(path).is_file() else None

    return [Path(path) for path in recording_modules.loaded_modules(script.as_posix(), read, [folder.as_posix() for folder in MODULE_PATH])]


SUITE_JOBS = SHARED_FOLDER.parent / ".github" / "workflows" / "suite-jobs.yml"  # the shared workflow that runs the entry files
PIPELINE_WORKFLOW = SHARED_FOLDER.parent / ".github" / "workflows" / "pipeline.yml"
TIMEOUTS_FILE = SHARED_FOLDER / "pipeline" / "timeouts.json"
WINDOWS_ONLY_PARTS = ("etw-consumer/", "etw_consumer_build.py")
PINS_FILE = SHARED_FOLDER / "repository-hygiene" / "pins.json"
TOOL_INSTALLER_FILES = (SHARED_FOLDER / "repository-hygiene" / "install_tools.py", SHARED_FOLDER / "repository-hygiene" / "pins.py")
LANGUAGE_STEP_FOLDERS = {"python": SHARED_FOLDER / "python-language-step"}
LANGUAGE_STEP_PYTHON_TOOLS = {"python": ("ruff", "mypy")}
# The shared folders whose files each pipeline part's job runs, apart from the consumer kit, which no job runs; with
# the part's extra files, its time limit in ``timeouts.json`` and its job in the pipeline workflow (``part_job_text``)
# they make up its ``part:<name>`` digest. Kept equal to ``PART_DIGEST_FOLDERS`` and ``PART_DIGEST_FILES`` of
# actions/repository-hygiene/promote_v1.py, as ``SharedDigestScopeTests`` in tests/test_suite_selection_reuse.py checks.
PART_DIGEST_FOLDERS: dict[str, tuple[Path, ...]] = {
    "custom": (),
    "dotnet-test": (SHARED_FOLDER / "dotnet-test",),
    "node-test": (SHARED_FOLDER / "node-test",),
    "python-test": (SHARED_FOLDER / "python-tests",),
    "python-verify": (SHARED_FOLDER / "python-tests",),
}
PART_DIGEST_FILES: dict[str, tuple[Path, ...]] = {
    "custom": (SHARED_FOLDER / "pipeline" / "schema" / "v1" / "setups.json",),
}
PART_EXCLUDED_FOLDERS = ("consumer-kit",)
HYGIENE_FOLDER = SHARED_FOLDER / "repository-hygiene"
# Each hygiene check's scripts, tool pins and pipeline workflow block/step; kept equal to ``HYGIENE_DIGEST_*`` of
# actions/repository-hygiene/promote_v1.py, as ``SharedDigestScopeTests`` in tests/test_suite_selection_reuse.py checks.
HYGIENE_DIGEST_SCRIPTS: dict[str, tuple[str, ...]] = {
    "actionlint": ("hygiene.py", "install_tools.py", "pins.py"),
    "zizmor": ("hygiene.py", "install_tools.py", "pins.py"),
    "zizmor-policy": ("zizmor-policy.yml",),
    "packages": ("hygiene.py", "dependabot_coverage.py", "package-ecosystems.json"),
    "markdown": ("hygiene.py", "link_check.py"),
    "reuse": ("hygiene.py", "install_tools.py", "pins.py"),
}
HYGIENE_DIGEST_PINS: dict[str, tuple[tuple[str, str], ...]] = {
    "actionlint": (("tools", "actionlint"), ("tools", "shellcheck")),
    "zizmor": (("tools", "zizmor"),),
    "reuse": (("python", "reuse"),),
}
HYGIENE_DIGEST_WORKFLOW: dict[str, tuple[str, str]] = {
    "actionlint": ("job", "repository-hygiene"),
    "zizmor": ("job", "repository-hygiene"),
    "packages": ("plan-step", "coverage"),
    "markdown": ("plan-step", "links"),
    "reuse": ("job", "repository-hygiene"),
}
# The line that ends a job's block in the pipeline workflow: the next job (``  <name>:``) or a top-level key.
JOB_BOUNDARY = re.compile(r"^(?:  )?[A-Za-z_][A-Za-z0-9_-]*:(?:\s|$)")


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _folder_files(folder: Path) -> list[Path]:
    return [path for path in folder.rglob("*") if path.is_file() and "__pycache__" not in path.relative_to(folder).parts]


def platform_files(platform: str) -> list[Path]:
    """The files of one platform's recording digest: the recording step's folder (without the Windows-only ETW
    consumer files on Linux), every module outside it that the recording step loads, and the shared workflow of the
    suite jobs."""
    if platform not in suite_selection.OPERATING_SYSTEMS:
        raise RecordingError(f"unknown platform {platform!r}")
    folder = Path(__file__).resolve().parent
    files: set[Path] = set()
    for path in _folder_files(folder):
        relative = path.relative_to(folder).as_posix()
        if platform == "linux" and relative.startswith(WINDOWS_ONLY_PARTS):
            continue
        files.add(path)
    files.update(loaded_modules(Path(__file__).resolve()))
    if SUITE_JOBS.is_file():
        files.add(SUITE_JOBS)
    return sorted(files)


def shared_name(path: Path) -> str:
    """The name of a shared file in a digest: its path from the folder of the shared parts, with ``/`` separators."""
    return Path(os.path.relpath(path, SHARED_FOLDER)).as_posix()


def files_digest(files: Collection[Path], extra_entries: dict[str, bytes] | None = None) -> str:
    """The SHA-256 over the name and the SHA-256 of the content of every file and extra entry, in code-point order of
    their names: a Windows path sorts case-insensitively, so ``Path`` order differs between the platforms."""
    entries = {shared_name(path): path.read_bytes() for path in files}
    if extra_entries:
        entries.update(extra_entries)
    lines = "".join(f"{hashlib.sha256(entries[name]).hexdigest()}  {name}\n" for name in sorted(entries))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def _load_pins_document() -> dict:
    if not PINS_FILE.is_file():
        return {}
    try:
        document = json.loads(PINS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def tool_digests(pins: dict | None = None) -> dict[str, str]:
    """One digest per pinned tool in ``pins.json``, keyed as ``tool:<name>``."""
    document = _load_pins_document() if pins is None else pins
    if not document:
        return {}
    installer = [path for path in TOOL_INSTALLER_FILES if path.is_file()]
    digests: dict[str, str] = {}
    if isinstance(document.get("node"), dict):
        digests["tool:node"] = files_digest((), {"pins:node": _canonical_json(document["node"])})
    for section in ("tools", "python"):
        entries = document.get(section)
        if isinstance(entries, dict):
            for name, entry in sorted(entries.items()):
                digests[f"tool:{name}"] = files_digest(installer, {f"pins:{section}/{name}": _canonical_json(entry)})
    return digests


def language_step_digests(pins: dict | None = None) -> dict[str, str]:
    """One digest per shared language step, keyed as ``language:<name>``: the files of its folder outside the consumer
    kit, which no job runs, the tool installer and the pins of its Python tools."""
    document = _load_pins_document() if pins is None else pins
    installer = [path for path in TOOL_INSTALLER_FILES if path.is_file()]
    python_pins = document.get("python") if isinstance(document.get("python"), dict) else {}
    digests: dict[str, str] = {}
    for name, folder in sorted(LANGUAGE_STEP_FOLDERS.items()):
        if not folder.is_dir():
            continue
        files = part_files((folder,)) + installer
        extra = {f"pins:python/{tool}": _canonical_json(python_pins[tool])
                 for tool in LANGUAGE_STEP_PYTHON_TOOLS.get(name, ()) if tool in python_pins}
        digests[f"language:{name}"] = files_digest(files, extra)
    return digests


def part_job_text(workflow: str, job: str) -> str | None:
    """The lines of the pipeline workflow that a part's job runs under: the workflow's lines before ``jobs:`` and the
    job's own block, from its ``  <job>:`` line to the next job line or top-level key (``JOB_BOUNDARY``) or the end of
    the file, without trailing blank lines and trailing comments at the job's indentation or less, so that a comment
    inside the job keeps the rest of the job in it; None when the workflow has no such job.
    ``promote_v1.part_job_text`` reads it the same way."""
    lines = workflow.splitlines()
    try:
        jobs = lines.index("jobs:")
        start = lines.index(f"  {job}:", jobs + 1)
    except ValueError:
        return None
    end = start + 1
    while end < len(lines) and not JOB_BOUNDARY.match(lines[end]):
        end += 1
    block = lines[start:end]
    while block and (not block[-1].strip() or (block[-1].lstrip().startswith("#") and not block[-1].startswith("   "))):
        block.pop()
    return "\n".join(lines[:jobs] + block) + "\n"


def plan_step_text(workflow: str, step_id: str) -> str | None:
    """The lines of the pipeline workflow that one light-check step of ``plan`` runs under: the workflow's lines before
    ``jobs:`` and that step's block in ``jobs.plan.steps``, identified by ``id: <step_id>``, without trailing blank
    lines and trailing comments at the step's indentation or less; None when absent.
    ``promote_v1.plan_step_text`` reads it the same way."""
    lines = workflow.splitlines()
    try:
        jobs = lines.index("jobs:")
        plan_start = lines.index("  plan:", jobs + 1)
    except ValueError:
        return None
    plan_end = plan_start + 1
    while plan_end < len(lines) and not JOB_BOUNDARY.match(lines[plan_end]):
        plan_end += 1
    try:
        steps_start = lines.index("    steps:", plan_start + 1, plan_end)
    except ValueError:
        return None
    step_starts = [i for i in range(steps_start + 1, plan_end) if lines[i].startswith("      - ")]
    for pos, start in enumerate(step_starts):
        end = step_starts[pos + 1] if pos + 1 < len(step_starts) else plan_end
        block = lines[start:end]
        if any(line in (f"        id: {step_id}", f"      - id: {step_id}") for line in block):
            while block and (not block[-1].strip() or (block[-1].lstrip().startswith("#") and not block[-1].startswith("       "))):
                block.pop()
            return "\n".join(lines[:jobs] + block) + "\n"
    return None


def _load_timeouts_document() -> dict[str, int]:
    """Read and validate ``actions/pipeline/timeouts.json``; fails loudly when missing, unreadable or malformed."""
    if not TIMEOUTS_FILE.is_file():
        raise RecordingError(f"{shared_name(TIMEOUTS_FILE)} is missing; part digests cannot be taken")
    try:
        document = json.loads(TIMEOUTS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise RecordingError(f"{shared_name(TIMEOUTS_FILE)} cannot be read: {error}") from error
    if (not isinstance(document, dict)
            or not all(isinstance(k, str) and k and isinstance(v, int) and not isinstance(v, bool) and v > 0
                       for k, v in document.items())):
        raise RecordingError(f"{shared_name(TIMEOUTS_FILE)} must map job names to positive integer minute limits")
    return document


def part_files(folders: Collection[Path]) -> list[Path]:
    """The files of a part's shared folders that its job runs: every file outside the consumer kit."""
    files: list[Path] = []
    for folder in folders:
        if folder.is_dir():
            files.extend(path for path in _folder_files(folder) if path.relative_to(folder).parts[0] not in PART_EXCLUDED_FOLDERS)
    return files


def _part_digest_inputs() -> dict[str, tuple[list[Path], dict[str, bytes]]]:
    """Per pipeline part, its files and extra entries. Fails loudly when the pipeline workflow or a part folder is
    present while the pipeline workflow, a part's job or a part's folder is missing."""
    workflow_exists = PIPELINE_WORKFLOW.is_file()
    any_part_folder = any(folder.is_dir() for folders in PART_DIGEST_FOLDERS.values() for folder in folders)
    if not workflow_exists and not any_part_folder:
        return {}
    if not workflow_exists:
        raise RecordingError(f"{shared_name(PIPELINE_WORKFLOW)} is missing; part digests cannot be taken")
    workflow_text = PIPELINE_WORKFLOW.read_text(encoding="utf-8")
    timeouts = _load_timeouts_document()
    inputs: dict[str, tuple[list[Path], dict[str, bytes]]] = {}
    for name, folders in sorted(PART_DIGEST_FOLDERS.items()):
        files: list[Path] = []
        for folder in folders:
            folder_files = part_files((folder,)) if folder.is_dir() else []
            if not folder_files:
                raise RecordingError(f"the part folder {shared_name(folder)} for part {name} is missing or empty; its part digest cannot be taken")
            files.extend(folder_files)
        files.extend(path for path in PART_DIGEST_FILES.get(name, ()) if path.is_file())
        text = part_job_text(workflow_text, name)
        if text is None:
            raise RecordingError(f"the job of the part {name} cannot be found in {shared_name(PIPELINE_WORKFLOW)} "
                                 f"as a line `  {name}:` under `jobs:`; its part digest cannot be taken")
        extra: dict[str, bytes] = {f"{shared_name(PIPELINE_WORKFLOW)}#jobs.{name}": text.encode("utf-8")}
        if name in timeouts:
            extra[f"{shared_name(TIMEOUTS_FILE)}#{name}"] = _canonical_json(timeouts[name])
        inputs[name] = (files, extra)
    return inputs


def part_digests() -> dict[str, str]:
    """One digest per pipeline part, keyed as ``part:<name>``: the files its job runs, its time limit and its job in
    the pipeline workflow, so that a README, a schema document or another job leaves it unmoved."""
    return {f"part:{name}": files_digest(files, extra) for name, (files, extra) in _part_digest_inputs().items()}


def _hygiene_digest_inputs(pins: dict) -> dict[str, tuple[list[Path], dict[str, bytes]]]:
    """Per hygiene check, its scripts, tool pin entries and pipeline workflow job or light-check step."""
    workflow_text = PIPELINE_WORKFLOW.read_text(encoding="utf-8") if PIPELINE_WORKFLOW.is_file() else None
    inputs: dict[str, tuple[list[Path], dict[str, bytes]]] = {}
    for name, scripts in sorted(HYGIENE_DIGEST_SCRIPTS.items()):
        files = [HYGIENE_FOLDER / script for script in scripts if (HYGIENE_FOLDER / script).is_file()]
        extra: dict[str, bytes] = {}
        for section, tool in HYGIENE_DIGEST_PINS.get(name, ()):
            entries = pins.get(section)
            if isinstance(entries, dict) and tool in entries:
                extra[f"pins:{section}/{tool}"] = _canonical_json(entries[tool])
        if workflow_text is not None and name in HYGIENE_DIGEST_WORKFLOW:
            kind, target = HYGIENE_DIGEST_WORKFLOW[name]
            if kind == "job":
                text = part_job_text(workflow_text, target)
                if text is None:
                    raise RecordingError(f"the job {target} for hygiene check {name} cannot be found in {shared_name(PIPELINE_WORKFLOW)} "
                                         f"as a line `  {target}:` under `jobs:`; its digest cannot be taken")
                extra[f"{shared_name(PIPELINE_WORKFLOW)}#jobs.{target}"] = text.encode("utf-8")
            else:
                text = plan_step_text(workflow_text, target)
                if text is None:
                    raise RecordingError(f"the step {target} of plan for hygiene check {name} cannot be found in {shared_name(PIPELINE_WORKFLOW)}; "
                                         f"its digest cannot be taken")
                extra[f"{shared_name(PIPELINE_WORKFLOW)}#jobs.plan.steps.{target}"] = text.encode("utf-8")
        if files or extra:
            inputs[name] = (files, extra)
    return inputs


def hygiene_digests(pins: dict | None = None) -> dict[str, str]:
    """One digest per hygiene check, keyed as ``hygiene:<name>``: its scripts, tool pins and pipeline workflow job or
    light-check step, with ``hygiene:zizmor-policy`` separate so consumers with their own zizmor config ignore it."""
    document = _load_pins_document() if pins is None else pins
    return {f"hygiene:{name}": files_digest(files, extra) for name, (files, extra) in _hygiene_digest_inputs(document).items()}


def shared_digest_scopes() -> dict[str, list[str]]:
    """The entry names of every shared CI digest, keyed like ``shared_digests``: each file by ``shared_name`` and each
    extra entry by its own name. ``SharedDigestScopeTests`` in tests/test_suite_selection_reuse.py ties them to
    ``promote_v1.commit_digest_scopes``."""
    pins = _load_pins_document()
    installer = [path for path in TOOL_INSTALLER_FILES if path.is_file()]
    scopes: dict[str, list[str]] = {f"platform:{platform}": [shared_name(path) for path in platform_files(platform)]
                                    for platform in suite_selection.OPERATING_SYSTEMS}
    if isinstance(pins.get("node"), dict):
        scopes["tool:node"] = ["pins:node"]
    for section in ("tools", "python"):
        entries = pins.get(section)
        if isinstance(entries, dict):
            for name in entries:
                scopes[f"tool:{name}"] = [*(shared_name(path) for path in installer), f"pins:{section}/{name}"]
    python_pins = pins.get("python") if isinstance(pins.get("python"), dict) else {}
    for name, folder in LANGUAGE_STEP_FOLDERS.items():
        if folder.is_dir():
            scopes[f"language:{name}"] = [*(shared_name(path) for path in part_files((folder,)) + installer),
                                          *(f"pins:python/{tool}" for tool in LANGUAGE_STEP_PYTHON_TOOLS.get(name, ()) if tool in python_pins)]
    for name, (files, extra) in _part_digest_inputs().items():
        scopes[f"part:{name}"] = [*(shared_name(path) for path in files), *extra]
    for name, (files, extra) in _hygiene_digest_inputs(pins).items():
        scopes[f"hygiene:{name}"] = [*(shared_name(path) for path in files), *extra]
    return {name: sorted(entries) for name, entries in sorted(scopes.items())}


def shared_digests() -> dict[str, str]:
    """All shared CI digests: one per platform (``platform:linux``, ``platform:windows``), one per pinned tool
    (``tool:<name>``), one per language step (``language:<name>``), one per pipeline part (``part:<name>``) and one
    per hygiene check (``hygiene:<name>``), sorted by key."""
    pins = _load_pins_document()
    digests = {f"platform:{platform}": files_digest(platform_files(platform)) for platform in suite_selection.OPERATING_SYSTEMS}
    digests.update(tool_digests(pins))
    digests.update(language_step_digests(pins))
    digests.update(part_digests())
    digests.update(hygiene_digests(pins))
    return dict(sorted(digests.items()))


def run_digest(options: argparse.Namespace) -> int:
    digests = shared_digests()
    rendered = json.dumps(digests, sort_keys=True, separators=(",", ":"))
    print(f"shared CI digests {rendered}")
    if options.github_output is not None:
        with options.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"shared-digests={rendered}\n")
    if options.expect is not None:
        try:
            expected = json.loads(options.expect)
        except json.JSONDecodeError as error:
            print(f"The expected shared CI digests could not be read as JSON: {error}", file=sys.stderr)
            return 1
        if not isinstance(expected, dict) or not expected or not all(isinstance(k, str) and isinstance(v, str) for k, v in expected.items()):
            print("The expected shared CI digests must be a non-empty JSON object from digest name to hex digest.", file=sys.stderr)
            return 1
        mismatched = [name for name, value in sorted(expected.items()) if digests.get(name) != value]
        if mismatched:
            details = ", ".join(f"{name} (got {digests.get(name)}, select took {expected[name]})" for name in mismatched)
            print(f"The shared CI in {SHARED_FOLDER} differs for {details}: v1 moved during the run. Start the whole run again.", file=sys.stderr)
            return 1
    return 0


# --- the command line -------------------------------------------------------------------------------------------


def parse_arguments(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    basing = commands.add_parser("base", help="write the job's environment before its first setup")
    basing.add_argument("--file", type=Path, help="the file to write (default: $RUNNER_TEMP/recording-environment.json)")
    starting = commands.add_parser("start", help="prepare the output directory of a recording")
    starting.add_argument("--output", type=Path, help="the output directory (default: $RUNNER_TEMP/recording)")
    starting.add_argument("--root", type=Path, default=Path("."), help="the repository root that a Windows recording records "
                          "(default: the working directory)")
    starting.add_argument("--environment", type=Path, help="the job's environment before its setups, as base wrote it "
                          "(default: $RUNNER_TEMP/recording-environment.json when it exists, otherwise the current one)")
    starting.add_argument("--full", action="store_true", help="the full-recording mode: the wide trace set and results for apply")
    starting.add_argument("--job", help="in the full-recording mode, the name of the shard job, which every measurement of the "
                          "recording names and apply corrects for its speed")
    executing = commands.add_parser("exec", help="run one setup or one entry file under the recording")
    executing.add_argument("--output", type=Path)
    unit = executing.add_mutually_exclusive_group(required=True)
    unit.add_argument("--setup", help="the name of the setup that the command runs")
    unit.add_argument("--entry", help="the entry file that the command runs, as the suite list names it")
    executing.add_argument("--suite", help="the suite of the entry file, when the suite list names it in several suites")
    executing.add_argument("--suites", type=Path, help="the suite list, whose setups of the entry file decide its environment")
    executing.add_argument("--root", type=Path, default=Path("."), help="the repository root of the suite list's entry files "
                           "(default: the working directory)")
    executing.add_argument("--timeout", type=float, help="time limit in seconds for the command")
    executing.add_argument("arguments", nargs=argparse.REMAINDER, help="-- and the command")
    providing = commands.add_parser("provide", help="record what a setup installed in this job provides to the entry files that state it")
    providing.add_argument("--output", type=Path)
    providing.add_argument("--setup", required=True, help="the name of the setup, as the suite list states it")
    providing.add_argument("--path", action="append", default=[], help="an absolute tool directory of the setup (repeatable)")
    providing.add_argument("--variable", action="append", default=[], help="<name>=<value> of a variable that the setup sets (repeatable)")
    providing.add_argument("--python", action="append", default=[], help="a Python interpreter of the setup, into whose site directory "
                           "the lookup report is installed (repeatable)")
    providing.add_argument("--write", action="append", default=[], help="an absolute writable directory that the setup grants (repeatable)")
    setting = commands.add_parser("setups", help="run the setups of this job that the suite list states as data")
    setting.add_argument("--output", type=Path)
    setting.add_argument("--suites", type=Path, required=True, help="the suite list, whose setups name their scripts")
    setting.add_argument("--root", type=Path, default=Path("."), help="the repository root, from which the scripts run "
                         "(default: the working directory)")
    setting.add_argument("--work", type=Path, help="the directory that holds a directory per setup (default: $RUNNER_TEMP/setups)")
    setting.add_argument("--phase", choices=("before", "recorded"), required=True, help="before: the setups that run before start, "
                         "outside the recording; recorded: after start, the setups under the recording and what every setup provides")
    setting.add_argument("--setup", action="append", default=[], help="a setup of this job (repeatable)")
    setting.add_argument("--tool", action="append", default=[], help="<tool>=<path> of a tool that the job installed for its "
                         "setups: the interpreter of python, the directory of node's programs, the root of dotnet (repeatable)")
    setting.add_argument("--pinned", action="append", default=[], help="<tool>=<directory> of a pinned tool that the job "
                         "installed: the directory that the entry files which state the tool get on their PATH (repeatable)")
    checking = commands.add_parser("check", help="compare the recording with the suite list")
    checking.add_argument("--output", type=Path)
    checking.add_argument("--root", type=Path, default=Path("."), help="the repository root (default: the working directory)")
    checking.add_argument("--suites", type=Path, required=True, help="the suite list")
    checking.add_argument("--additions", type=Path, help="where to write the additions or the full-recording result")
    checking.add_argument("--artifact-name", help="artifact name for uploads; entry-times-only uploads switch recording-additions* to recording-entry-times*")
    checking.add_argument("--summary", type=Path, help="append the Markdown summary to this file")
    checking.add_argument("--github-output", type=Path, help="append mode=, file= and artifact-name= lines to this file, and error= on a recording error")
    keeping = commands.add_parser("keep", help="mark the entry files that check's additions name in a passed list")
    keeping.add_argument("--additions", type=Path, required=True, help="the additions document that check wrote")
    keeping.add_argument("passed", type=Path, help="the passed list of the job, a JSON list of suites and entry files, rewritten in place")
    applying = commands.add_parser("apply", help="apply additions or full-recording results to the suite list")
    applying.add_argument("--root", type=Path, default=Path("."), help="the repository root (default: the working directory)")
    applying.add_argument("--suites", type=Path, help="the suite list (default: the one the files name, under the root)")
    applying.add_argument("--summary", type=Path, help="append the Markdown report to this file")
    applying.add_argument("--github-output", type=Path, help="append whole-tree status lines to this file")
    applying.add_argument("--expect-job", action="append", default=[], help="a shard job whose full-recording result must be present (repeatable)")
    kept = applying.add_mutually_exclusive_group()
    kept.add_argument("--keep-lists", type=Path, help="the output of select --json-output for the change since the recorded commit: "
                      "the recorded reads of each entry file it selects are added to its stored lists instead of replacing them")
    kept.add_argument("--keep-all-lists", action="store_true", help="add the recorded reads of every entry file to its stored lists: "
                      "the change since the recorded commit could not be selected")
    applying.add_argument("files", type=Path, nargs="+", help="additions or full-recording results")
    digesting = commands.add_parser("digest", help="print the shared CI digests per platform, tool and language step")
    digesting.add_argument("--expect", help="the shared CI digests JSON that select took; any difference fails")
    digesting.add_argument("--github-output", type=Path, help="append a shared-digests= line to this file")
    options = parser.parse_args(argv)
    if options.command == "start" and options.job is not None and not options.full:
        parser.error("--job goes with --full")
    if options.command == "exec" and options.entry is None and (options.suite is not None or options.suites is not None):
        parser.error("--suite and --suites go with --entry")
    if options.command == "exec" and options.entry is not None and options.suites is None:
        parser.error("--entry needs --suites")
    if options.command == "exec" and options.timeout is not None and (not math.isfinite(options.timeout) or options.timeout <= 0):
        parser.error("--timeout must be a positive number of seconds")
    return options


def main(argv: list[str] | None = None, *, include_process_startup: bool = False) -> int:
    options = parse_arguments(sys.argv[1:] if argv is None else argv)
    if include_process_startup and options.command == "exec":
        options._process_startup_seconds = process_elapsed_seconds() or 0.0
    handlers = {"base": run_base, "start": run_start, "exec": run_exec, "provide": run_provide, "setups": run_setups, "check": run_check, "keep": run_keep, "apply": run_apply, "digest": run_digest}
    try:
        return handlers[options.command](options)
    except (RecordingError, suite_selection.SuiteListError, OSError) as error:
        print(str(error), file=sys.stderr)
        if options.command == "check" and options.github_output is not None:
            # The recording step's error annotation quotes the message; a random delimiter lets it span lines.
            delimiter = f"ERROR_{os.urandom(8).hex()}"
            with options.github_output.open("a", encoding="utf-8") as handle:
                handle.write(f"error<<{delimiter}\n{error}\n{delimiter}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main(include_process_startup=True))
