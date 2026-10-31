#!/usr/bin/env python3
"""Record what a job's setups and entry files read, check it against the suite list, and apply the additions.

``base`` writes the job's environment before its first setup. ``start`` prepares the output directory of one job's
recording on that environment; on Windows it also starts a live Kernel-File ETW session that the committed consumer in
``etw-consumer/build`` reads into one neutral event file. ``provide`` records what a setup gives the entry files that
state it, and installs the lookup report into the site directory of a set-up Python interpreter. ``exec`` runs one setup
or one entry file under the recording: on Linux under ``strace -f --seccomp-bpf`` with the mid trace set, or the wide
trace set in the full-recording mode; on Windows as its own process, whose reads ``check`` attributes by process id. An
entry file runs in the job's environment before its setups, with only what its stated setups provide. Every Python
process with the lookup report writes its module lookups beside the recording.
``check`` parses and classifies the recording, maps it to repository paths, assigns each setup's reads to the
suites and entry files of the job that state the setup, and compares the reads with the stored lists. In a
pull-request or push run it writes the additions, lists them in the job summary and fails when there are any; in the
full-recording mode it writes the classified reads, the skip state and the duration of every entry file. ``apply``
adds a run's additions to the suite list, or, with the results of a full recording, replaces the lists of every entry
file that exited with 0 and whose test report in an accepted format shows no skipped test, and writes the duration
estimates. The lists of a suite selected per entry file are the union of its entry files' lists. ``docs/maintaining.md`` documents
the commands.

    python3 recording.py base [--file <file>]
    python3 recording.py start [--output <directory>] [--root <repository>] [--environment <file>] [--full]
    python3 recording.py exec [--output <directory>] --setup <name> -- <command> ...
    python3 recording.py exec [--output <directory>] --entry <file> --suites <suite list> [--root <repository>] [--suite <name>] -- <command> ...
    python3 recording.py provide [--output <directory>] --setup <name> [--path <directory>] ... [--variable <name>=<value>] ... [--python <interpreter>] ...
    python3 recording.py check [--output <directory>] --suites <suite list> [--root <repository>] [--summary <file>]
    python3 recording.py apply [--root <repository>] [--suites <suite list>] <additions or results> ...
    python3 recording.py digest [--expect <digest>] [--github-output <file>]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import functools
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

SHARED_FOLDER = Path(__file__).resolve().parent.parent  # the folder of the shared CI's parts, in a checkout or a fetched action
# Where this script's imports resolve first: the folder it inserts into sys.path, then its own folder.
MODULE_PATH = (SHARED_FOLDER / "suite-selection", Path(__file__).resolve().parent)
sys.path.insert(0, str(MODULE_PATH[0]))

import recording_modules  # noqa: E402
import suite_selection  # noqa: E402

PROCESS_CALLS = ("execve", "execveat", "chdir", "fchdir", "clone", "clone3", "fork", "vfork")
MID_SET = ("open", "openat", "openat2", "getdents", "getdents64", "access", "faccessat", "faccessat2", "newfstatat", "statx") + PROCESS_CALLS
WIDE_SET = MID_SET + ("stat", "lstat", "readlink", "readlinkat")
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
WRITE_FLAGS = ("O_WRONLY", "O_CREAT", "O_TRUNC")
STRING = re.compile(r'"((?:[^"\\]|\\.)*)"')
COMMAND_EXTENSIONS = (".ps1", ".exe", ".cmd", ".bat", ".com", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msc", ".cpl")  # PATHEXT and PowerShell
POWERSHELL = frozenset({"pwsh", "powershell"})
NODE = frozenset({"node", "tsc"})  # the programs of a Node process, as program_name gives them
MAIN_STREAM = "::$DATA"
# The options that take their value as the next argument: Git's own before the command, git clone's and git worktree add's.
GIT_OPTIONS_WITH_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
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
# The environment variables that change how Git reads a pathspec.
PATHSPEC_VARIABLES = frozenset({"GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS"})
# The short magic signs of a pathspec: top, exclude and its other spelling.
SHORT_MAGIC = "/!^"
CAT_FILE_MODES = frozenset({"-t", "-s", "-e", "-p"})  # without one, the first operand is the type
FULL_OBJECT_ID = re.compile(r"^([0-9a-fA-F]{40}|[0-9a-fA-F]{64})(\^\{(blob|tree|commit|tag|object)\})?$")
UNITTEST_RAN = re.compile(r"^Ran \d+ tests? in ", re.MULTILINE)
UNITTEST_STATUS = re.compile(r"^(OK|FAILED)\b.*$", re.MULTILINE)
PYTEST_SUMMARY = re.compile(r"^=+ .* in [\d.]+s\b.*=+$", re.MULTILINE)
SKIPPED_COUNT = re.compile(r"skipped=(\d+)")
PYTEST_SKIPPED = re.compile(r"(\d+) skipped")
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
ETW_KINDS = {"open": "read", "opendir": "diropen", "absent": "absent"}  # the event kinds of the consumer's opens
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
                                 f"rights: {log.read_text(encoding='utf-8', errors='replace').strip()}")
        if time.monotonic() > deadline:
            raise RecordingError(f"the ETW session did not start within {ETW_WAIT_SECONDS} s")
        time.sleep(0.05)
    return {"roots": roots, "git_directories": outside, "devices": devices, "events": ETW_EVENTS, "consumer": process.pid}


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


def skip_count(output: str) -> tuple[int | None, str | None]:
    """The number of skipped tests that the entry file's complete test reports in an accepted format state, or None when
    it has none, and, when it has none, why its TAP report is incomplete. An incomplete TAP report counts as no report."""
    tap = tap_report(output)
    counts = [tap[1]] if tap is not None and tap[0] is None else []
    if UNITTEST_RAN.search(output):
        lines = [match.group(0) for match in UNITTEST_STATUS.finditer(output)]
        counts += [int(found.group(1)) if (found := SKIPPED_COUNT.search(line)) else 0 for line in lines]
    for line in PYTEST_SUMMARY.findall(output):
        found = PYTEST_SKIPPED.search(line)
        counts.append(int(found.group(1)) if found else 0)
    if counts:
        return sum(counts), None
    return None, None if tap is None else tap[0]


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
    provision = provisions(output).get(options.setup) or no_provision()
    for path in options.path:
        if not os.path.isabs(path) or not os.path.isdir(path):
            raise RecordingError(f"the tool directory {path} of the setup {options.setup} is missing or not absolute")
        provision["paths"] = add_unique(provision["paths"], [path])
    for item in options.variable:
        name, separator, value = item.partition("=")
        if not name or not separator:
            raise RecordingError(f"--variable takes <name>=<value>, not {item}")
        provision["variables"][name] = value
    for interpreter in options.python:
        install_lookup_report(interpreter)
    write_provision(output, options.setup, provision)
    return 0


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


def run_exec(options: argparse.Namespace) -> int:
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
    if etw:
        # The live session records every process; check attributes the reads to this unit by its process id.
        recorder: list[str] = []
        environment = dict(base, TMPDIR=str(temporary), TEMP=str(temporary), TMP=str(temporary))
        command[0] = shutil.which(command[0], path=environment["PATH"]) or command[0]
    else:
        trace_set = TRACE_SETS[settings["trace_set"]]
        recorder = [settings["strace"], *STRACE[1:], "-o", str(trace), "-e", "trace=" + ",".join("?" + call for call in trace_set), *FULL_EXEC_ENVIRONMENT, "--"]
        environment = dict(base, TMPDIR=str(temporary))
    started = time.perf_counter()
    log = bytearray()
    with subprocess.Popen(recorder + command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=environment) as process:
        assert process.stdout is not None
        for chunk in iter(lambda: process.stdout.read1(65536), b""):
            sys.stdout.buffer.write(chunk)
            sys.stdout.buffer.flush()
            log += chunk
        code = process.wait()
    seconds = time.perf_counter() - started
    if not etw and trace.is_file():
        keep_pathspec_variables(trace)
    text = log.decode("utf-8", "replace")
    skipped, incomplete = skip_count(text)
    (output / "logs" / f"{unit}.log").write_text(text, encoding="utf-8")
    record = {"unit": unit, "kind": "setup" if options.setup else "entry", "name": options.setup or normalize_entry(options.entry),
              "suite": options.suite, "command": command, "exit_code": code, "seconds": round(seconds, 3), "skipped": skipped,
              "incomplete_report": incomplete,
              "trace": trace.name, "lookups": lookups.name, "temporary": str(temporary), "cache": str(cache), "cwd": os.getcwd()}
    if etw:
        record.update(trace=settings["events"], pid=process.pid, launcher=os.getpid(), recorder=["etw"])
    else:
        record["recorder"] = [*STRACE, *recorder[len(STRACE) + 2:-1]]
    (output / "units" / f"{unit}.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    if options.setup and code == 0 and options.setup not in provisions(output):
        write_provision(output, options.setup, no_provision())
    return code


def normalize_entry(entry: str) -> str:
    normalized = posixpath.normpath(entry.replace("\\", "/"))
    return normalized[2:] if normalized.startswith("./") else normalized


# --- parsing --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    pid: str
    executable: str  # the base name of the program the process ran when it made the call
    kind: str  # read, exec, exec-absent, stat, statdir, exists, absent, diropen, list (one per listing of a directory)
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


def with_pathspec_variables_only(line: str) -> str:
    """A strace line with only the pathspec variables left in the environment of an exec."""
    space = line.find(" ")
    paren = line.find("(", space + 1)
    if space <= 0 or paren < 0 or line[space + 1:paren].strip() not in EXECS:
        return line
    spans = array_spans(line[paren:])
    if len(spans) < 2:
        return line
    start, end = paren + spans[1][0], paren + spans[1][1]
    kept = [item for item in STRING.findall(line[start:end]) if is_pathspec_variable(unescape(item))]
    return line[:start] + "[" + ", ".join(f'"{item}"' for item in kept) + "]" + line[end:]


def is_pathspec_variable(entry: str) -> bool:
    """Whether an environment entry ``<name>=<value>`` sets a pathspec variable."""
    return entry.split("=", 1)[0] in PATHSPEC_VARIABLES


def keep_pathspec_variables(trace: Path) -> None:
    """Keep only the pathspec variables in the environment of each exec in ``trace``."""
    kept = trace.with_name(trace.name + ".kept")
    with trace.open("r", encoding="utf-8", errors="surrogateescape") as source, \
            kept.open("w", encoding="utf-8", errors="surrogateescape", newline="") as target:
        for line in source:
            target.write(with_pathspec_variables_only(line))
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
                if any(flag in flags for flag in WRITE_FLAGS):
                    continue
                if ok:
                    events.append(Event(pid, program, "diropen" if "O_DIRECTORY" in flags else "read", path, cwd=here))
                elif error in ABSENT_ERRORS:
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
    operation: str  # start, open, opendir, absent, enum, enum-absent
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
    """The arguments with forward slashes, each one that starts with a drive converted by ``convert``."""
    return [convert(argument) if re.match(r"^[A-Za-z]:[\\/]", argument) else argument.replace("\\", "/") for argument in command]


def attribute(lines: list[EtwLine], records: list[dict]) -> dict[str, list[tuple[int, EtwLine]]]:
    """The lines of each unit, by its unit name. A process belongs to the unit whose root process it descends from,
    by the latest start of each process id before the line; a root process without a start event is its own unit's."""
    roots = {(str(record["pid"]), str(record["launcher"])): record["unit"] for record in records}
    unstarted = {str(record["pid"]): record["unit"] for record in records}
    current: dict[str, str | None] = {}
    grouped: dict[str, list[tuple[int, EtwLine]]] = {record["unit"]: [] for record in records}
    for index, line in enumerate(lines):
        if line.operation == "start":
            parent = current[line.ppid] if line.ppid in current else unstarted.get(line.ppid)
            current[line.pid] = roots.get((line.pid, line.ppid), parent)
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
                events.append(Event(line.pid, program, "exists" if line.operation == "enum" else "absent", paths.convert(line.path), workdir))
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

    def update(self, other: Reads) -> None:
        self.paths |= other.paths
        for path, kind in other.existence.items():
            self.existence[path] = kind if self.existence.get(path, kind) == kind else "any"
        self.memberships |= other.memberships
        self.lookup_directories |= other.lookup_directories
        for directory, names in other.lookups.items():
            self.lookups.setdefault(directory, set()).update(names)

    def document(self) -> dict:
        return {"paths": sorted(self.paths),
                "existence_reads": [{"path": path, "kind": kind} for path, kind in sorted(self.existence.items())],
                "directory_memberships": sorted(self.memberships), "lookup_directories": sorted(self.lookup_directories),
                "module_lookups": {directory: sorted(names) for directory, names in sorted(self.lookups.items()) if names}}

    @staticmethod
    def of(document: dict) -> Reads:
        return Reads(set(document.get("paths", ())), {read["path"]: read["kind"] for read in document.get("existence_reads", ())},
                     set(document.get("directory_memberships", ())), set(document.get("lookup_directories", ())),
                     {directory: set(names) for directory, names in document.get("module_lookups", {}).items()})

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


def read_lookups(directory: Path, convert: Callable[[str], str]) -> Lookups:
    """The lookup reports in ``directory``, one file per process (``shared_lookup_report.py``), with each reported
    path converted by ``convert``."""
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
                for process in dict.fromkeys((report.stem, fields[1])):
                    key = (process, convert(fields[2]))
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


def in_git_state(path: str, directories: list[str]) -> bool:
    """Whether ``path`` lies in the index, objects or refs of one of the Git ``directories``, or of a linked worktree
    whose directory lies in one of them."""
    folded = path.lower()
    for directory in directories:
        if folded.startswith(directory.lower() + "/"):
            parts = path[len(directory) + 1:].split("/")
            return INDEX_OBJECTS_OR_REFS.match("/".join(parts[2:] if parts[0] == "worktrees" else parts)) is not None
    return False


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


def pathspec_reads(pathspec: str) -> Reads | None:
    """What one pathspec of a ``git ls-files`` can list, translated conservatively, or None for a form that only ``**``
    covers: a literal path is an existence read of that path, of any kind, and a directory membership ``<path>/**``; a
    pathspec with ``*`` or ``?`` a membership in which each of them is ``**``, and a ``:(glob)`` pathspec its own
    pattern, each with ``<pattern>/**`` beside it; an ``:(exclude)`` pathspec nothing."""
    pattern, glob = pathspec, False
    if pathspec.startswith(":("):
        end = pathspec.find(")")
        if end < 0:
            return None
        words = pathspec[2:end].split(",")
        if "exclude" in words:
            return Reads()
        if words != ["glob"]:
            return None
        pattern, glob = pathspec[end + 1:], True
    elif pathspec.startswith(":"):
        signs = pathspec[1:len(pathspec) - len(pathspec[1:].lstrip(SHORT_MAGIC))]
        return Reads() if "!" in signs or "^" in signs else None
    pattern = pattern.rstrip("/")
    if not pattern or "[" in pattern or "\\" in pattern or any(part in ("", ".", "..") for part in pattern.split("/")):
        return None
    if "*" in pattern or "?" in pattern:
        # Git also lists the paths beneath a directory whose name is the pathspec itself.
        translated = pattern if glob else pattern.replace("*", "**").replace("?", "**")
        return Reads(memberships={translated, translated + "/" + EVERY_PATH})
    return Reads(existence={pattern: "any"}, memberships={pattern + "/" + EVERY_PATH})


def listing_reads(event: Event, options: list[str], arguments: list[str], roots: list[str]) -> Reads:
    """The reads of a ``git ls-files`` that prints only names, with Git's own ``options`` before the command and the
    command's ``arguments``: a directory membership of ``**`` without a pathspec, and otherwise what each pathspec can
    list. A pathspec read through Git's own options, a pathspec file or the pathspec variables, or relative to a working
    directory other than a checkout root in ``roots``, also counts as ``**``, and so does a listing whose every pathspec
    is an exclude."""
    every = Reads(memberships={EVERY_PATH})
    pathspecs = [arguments[index] for index in operands(arguments, frozenset({"--pathspec-from-file"}))]
    from_file = any(argument.split("=", 1)[0] == "--pathspec-from-file" for argument in arguments[:arguments.index("--")
                                                                                                   if "--" in arguments else len(arguments)])
    if not pathspecs and not from_file:
        return every
    if options or from_file or event.cwd not in roots or event.environment is None \
            or any(is_pathspec_variable(item) for item in event.environment):
        return every
    reads = Reads()
    for pathspec in pathspecs:
        translated = pathspec_reads(pathspec)
        if translated is None:
            return every
        reads.update(translated)
    return reads if reads.memberships else every


def full_object_ids(arguments: list[str]) -> list[str] | None:
    """The object ids of a ``git cat-file`` whose every object argument is a full object id, with or without a
    ``^{<type>}`` suffix, or None."""
    names = [arguments[index] for index in operands(arguments, frozenset())]
    if not CAT_FILE_MODES & set(arguments):
        names = names[1:]
    found = [FULL_OBJECT_ID.match(name) for name in names]
    return [match.group(1).lower() for match in found if match] if names and all(found) else None


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


def git_directory_reads(events: list[Event], copies: list[str], project: Checkout, root: str | None = None) -> Reads:
    """The reads of the git processes that read the index, objects or refs of the checkout's Git directories or of the
    Git directory of a repository copy in ``copies``, whose paths the .git rule of ``relative_events`` drops: a
    ``git ls-files`` that prints only names is what ``listing_reads`` gives, a ``git cat-file`` of full object ids a
    content read of the tracked paths that hold each object, and any other such process a content read of every tracked
    path. ``root`` is the checkout in the form of the event paths, ``project.root`` by default."""
    directories = [*project.git_directories, *(copy + "/.git" for copy in copies)]
    readers = {event.pid for event in events if is_git(event.executable) and in_git_state(event.path, directories)}
    execs = {event.pid: event for event in events if event.kind == "exec" and event.pid in readers}  # each process's latest
    roots = [root or str(project.root), *copies]
    reads = Reads()
    for pid in readers:
        command, options, arguments = git_command(execs.get(pid)) or ("", [], [])
        if command == "ls-files" and prints_only_names(arguments):
            reads.update(listing_reads(execs[pid], options, arguments, roots))
        elif command == "cat-file" and (object_ids := full_object_ids(arguments)) is not None:
            for object_id in object_ids:
                reads.paths.update(held_paths(project.root).get(object_id, ()))
        else:
            reads.paths.add(EVERY_PATH)
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


def ignored_paths(project: Checkout, paths: set[str], directories: set[str]) -> set[str]:
    """The paths that the ignore rules of the checked commit match and that are not tracked; ``directories`` are the
    paths that the recording shows as a directory."""
    return {path for path in paths if path and path not in project.tracked and path not in project.directories
            and project.ignore.ignored(path, path in directories)}


def is_command_search(event: Event) -> bool:
    """An absent probe that a PowerShell process makes in its working directory for a name with ``.ps1`` or a
    ``PATHEXT`` extension, as its command search probes it."""
    return event.kind == "absent" and event.executable in POWERSHELL and posixpath.dirname(event.path) == event.cwd \
        and event.path.lower().endswith(COMMAND_EXTENSIONS)


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


def classify(unit: UnitRecording) -> Reads:
    """The reads of one unit's events and lookup reports. A listing is a directory membership unless the lookup report
    of its process attributes it to the import system: each process's listings of a directory count against the
    listings that its report names there. A module name looked up in a checkout directory without being found there, or
    found there as a namespace portion, is a module lookup, and a found module is a content read."""
    events, project, lookups, to_repository = unit.events, unit.project, unit.lookups, unit.to_repository
    mapped = [(event, relative) for event in events if (relative := to_repository(event.path, event.kind)) is not None]
    missing = [(relative, module.name) for module in lookups.missing if (relative := to_repository(module.directory, "list")) is not None]
    found = [(relative, to_repository(module.file, "read")) for module in lookups.found
             if (relative := to_repository(module.directory, "list")) is not None]
    shown = {relative for event, relative in mapped if event.kind in ("list", "statdir", "diropen")}
    shown.update(relative for relative, _ in missing + found)
    dropped = ignored_paths(project, {relative for _, relative in mapped} | shown | {file for _, file in found if file is not None}, shown)
    reads = Reads()
    directories: set[str] = set()
    absent: set[str] = set()
    listed: dict[tuple[str, str], int] = {}
    for event, relative in mapped:
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
            if is_directory or (kind == "exists" and (project.root / relative).is_dir()):
                directories.add(relative)
            else:
                reads.paths.add(relative)
        elif kind in ("statdir", "diropen"):
            directories.add(relative)
        elif kind in ("absent", "exec-absent"):
            if not is_command_search(event):
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
    for path in absent - reads.paths - {""}:
        reads.existence[path] = "any"
    reads.memberships = {"." if path == "" else path for path in reads.memberships}
    reads.update(git_directory_reads(events, unit.copies, project, unit.root))
    return reads


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


def exec_start(event: Event, fold: bool) -> Start:
    """An exec event as a start: its absolute arguments, and its relative ones resolved against the process's working
    directory where the recording knows it. On Windows (``fold``) the recorded command gives the relative arguments of
    the only process with a known working directory, spelled as Git tracks them."""
    arguments = []
    for argument in (event.arguments or ())[1:]:
        if argument.startswith("-"):
            continue
        if argument.startswith("/"):
            arguments.append(normalize(argument))
        elif event.cwd is not None and not fold:
            arguments.append(resolve(event.cwd, argument))
    return Start(event.executable, event.cwd, tuple(arguments))


def working_directories(events: list[Event], temporary: str | None, project: Checkout, recorded: Start, root: str | None, fold: bool) -> set[str]:
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
        for event, relative in relative_events([event for event in events if event.pid in unplaced], temporary, project, root):
            directories.add(relative if event.kind in ("list", "diropen", "statdir") else posixpath.dirname(relative))
    return directories


def toolchain_probes(events: list[Event], temporary: str | None, project: Checkout, recorded: Start, root: str | None = None,
                     fold: bool = False) -> set[str]:
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
        working = {(directory, "") for directory in working_directories(toolchain_events, temporary, project, recorded, root, fold)}
        anchors: dict[str, set[tuple[str, str]]] = {"project": set(), "solution": set(), "argument": set(), "working": working}
        files = [relative for _, relative in relative_events([event for event in toolchain_events if event.kind == "read"], temporary, project, root)]
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
    derived = {path: "any" for path in toolchain_probes(unit.events, unit.temporary, unit.project, recorded, unit.root, fold)}
    derived.update(preceding_modules(unit))
    return derived


def unit_reads(unit: UnitRecording, start: Start, fold: bool = False, memberships: set[str] = frozenset()) -> Reads:
    """The classified reads of one unit with every derived existence read that the recording does not already hold, and
    with ``memberships`` as directory memberships in place of any existence read of the same directory. ``start`` is
    the recorded command; ``fold`` compares paths without case, as on Windows."""
    reads = classify(unit)
    for path, kind in derived_existence_reads(unit, start, fold).items():
        if path not in reads.paths and path not in reads.existence:
            reads.existence[path] = kind
    for directory in memberships:
        reads.memberships.add(directory)
        reads.existence.pop(directory, None)
    return reads


def recorded_start(record: dict) -> Start:
    """The recorded command of a Linux unit as a start."""
    cwd = normalize(record["cwd"])
    return Start(program_name(record["command"][0]), cwd, tuple(resolve(cwd, argument) for argument in record["command"][1:]
                                                                if not argument.startswith("-")))


def recorded_windows_start(record: dict, paths: WindowsPaths) -> Start:
    """The recorded command of a Windows unit as a start, its paths mapped as ``paths`` maps the event paths."""
    arguments = tuple(paths.convert(argument if re.match(r"^[A-Za-z]:[\\/]", argument) else record["cwd"] + "\\" + argument)
                      for argument in record["command"][1:] if not argument.startswith("-"))
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

    def covers_path(self, path: str) -> bool:
        return any(suite_selection.matches(pattern, path) for pattern in self.patterns)

    def covers_membership(self, directory: str) -> bool:
        base = "" if directory == "." else directory
        for membership in self.memberships:
            stored = membership.strip("/")
            if stored in (directory, "." if base == "" else base) or (base == "" and stored == ""):
                return True
            if stored in ((base + "/*").lstrip("/"), (base + "/**").lstrip("/")) or covers_tree(stored, base):
                return True
        return any(covers_tree(pattern, base) for pattern in self.patterns)

    def covers_existence(self, path: str, kind: str) -> bool:
        if any(stored == path and (stored_kind == "any" or stored_kind == kind) for stored, stored_kind in self.existence):
            return True
        parent = posixpath.dirname(path)
        if any(membership.strip("/") in (parent, "." if parent == "" else parent) for membership in self.memberships):
            return True
        if any(covers_tree(pattern, path) for pattern in self.patterns):
            return True
        if kind == "directory":
            # The directory exists in the checkout: it disappears only when the tracked files beneath it go.
            return any(self.covers_path(file) for file in self.tracked if file.startswith(path + "/"))
        return self.covers_path(path)


def stored_lists(suite: suite_selection.Suite, entry: str, always: tuple[str, ...], tracked: frozenset[str] = frozenset(),
                 lookups: dict[str, tuple[str, ...]] | None = None) -> Stored | None:
    """The stored lists that select one entry file of a suite, or None when the suite has no stored path list;
    ``lookups`` are the module lookups of the suite list."""
    if suite.paths is None:
        return None
    patterns, memberships, existence = suite.patterns_of(entry), suite.memberships_of(entry), suite.existence_reads_of(entry)
    return Stored(tuple(patterns) + always, tuple(memberships), tuple((read.path.replace("\\", "/").strip("/"), read.kind) for read in existence), tracked,
                  suite.lookup_directories_of(entry), lookups or {})


def outside(reads: Reads, stored: Stored | None) -> Reads:
    """The reads that the stored lists do not cover; every read when there is no stored list."""
    if stored is None:
        return Reads(set(reads.paths), dict(reads.existence), set(reads.memberships), set(reads.lookup_directories),
                     {directory: set(names) for directory, names in reads.lookups.items()})
    lookups = {directory: {name for name in names if name not in stored.lookups.get(directory, ())} for directory, names in reads.lookups.items()}
    return Reads({path for path in reads.paths if not stored.covers_path(path)},
                 {path: kind for path, kind in reads.existence.items() if not stored.covers_existence(path, kind)},
                 {directory for directory in reads.memberships if not stored.covers_membership(directory)},
                 {directory for directory in reads.lookup_directories if directory not in stored.lookup_directories},
                 {directory: names for directory, names in lookups.items() if names})


# --- check ----------------------------------------------------------------------------------------------------


@dataclass
class Unit:
    record: dict
    reads: Reads


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
                                    recorded_start(record))) for record, events in zip(records, parsed)]


def unit_lookups(output: Path, record: dict, convert: Callable[[str], str]) -> Lookups:
    """The lookup reports of one unit; none for a unit recorded without them."""
    return read_lookups(output / "lookups" / record["lookups"], convert) if record.get("lookups") else Lookups()


def unreported_node_memberships(unit: UnitRecording) -> set[str]:
    """The checkout directories in which a Node process without a lookup report opened a file, the root as ``.``: on
    Windows no recording sees the modules it looks up, so each such directory is a directory membership."""
    node_pids = {event.pid for event in unit.events if event.executable in NODE and event.pid not in unit.lookups.processes}
    directories = set()
    for event in unit.events:
        if event.pid in node_pids and event.kind == "read" and (relative := unit.to_repository(event.path, event.kind)) is not None:
            if relative not in unit.project.directories and not (unit.project.root / relative).is_dir():
                directories.add(posixpath.dirname(relative))
    return {directory or "." for directory in directories - ignored_paths(unit.project, directories, directories)}


def stop_session(output: Path) -> tuple[float, re.Match]:
    """Stop the ETW consumer unless it has stopped, and return the seconds the stop took and its summary line."""
    log, stop = output / "logs" / "etw.log", output / "logs" / "etw.stop"
    started = time.perf_counter()
    deadline = time.monotonic() + ETW_WAIT_SECONDS
    while (found := ETW_SUMMARY.search(text := log.read_text(encoding="utf-8", errors="replace") if log.is_file() else "")) is None:
        if not stop.exists():
            stop.write_text("stop\n", encoding="utf-8")
        if time.monotonic() > deadline:
            raise RecordingError(f"the ETW consumer did not stop within {ETW_WAIT_SECONDS} s; its log {log} reads: {text.strip()}")
        time.sleep(0.05)
    return time.perf_counter() - started, found


def classify_etw_units(output: Path, project: Checkout, records: list[dict], settings: dict) -> list[Unit]:
    seconds, summary = stop_session(output)
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
        recording = UnitRecording(unit_events, dos_form(record["temporary"]), project, root, unit_lookups(output, record, paths.convert))
        units.append(Unit(record, unit_reads(recording, recorded_windows_start(record, paths), fold=True,
                                             memberships=unreported_node_memberships(recording))))
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
    for suite, unit, reads in recorded:
        entry = unit.record["name"]
        missing = outside(reads, stored_lists(suite, entry, suite_list.always, tracked, suite_list.lookups_by_directory))
        item = suites.setdefault(suite.name, {"suite": suite.name, "create": suite.paths is None, "recorded_entries": []})
        item["recorded_entries"] = sorted(set(item["recorded_entries"]) | {entry})
        if suite.entry_selection:
            if not missing.empty() or suite.paths is None:
                item.setdefault("entries", {})[entry] = missing.document()
        else:
            total = Reads.of(item.get("reads", {}))
            total.update(missing)
            item["reads"] = total.document()
    changes = [item for item in suites.values() if item["create"] or item.get("entries") or not Reads.of(item.get("reads", {})).empty()]
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


def run_check(options: argparse.Namespace) -> int:
    output = output_directory(options.output)
    settings = read_settings(output)
    root = options.root.resolve()
    project = checkout(root)
    suite_list = suite_selection.resolve_entries(suite_selection.load_suite_list(options.suites), set(project.tracked), str(options.suites))
    recorded = entry_reads(suite_list, classify_units(output, project, load_units(output), settings))
    list_path = suite_list_path(root, options.suites)
    if settings["mode"] == "full":
        return write_full_result(options, output, list_path, recorded, settings)
    document = additions_document(list_path, suite_list, recorded, project.tracked)
    target = options.additions or output / "additions.json"
    target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    lines = additions_lines(document)
    if lines:
        print(f"The recording reads outside the stored lists of {options.suites}. Apply the additions with:")
        print(f"  python3 recording.py apply {target}")
        for where, what in lines:
            print(f"  {where}: {what}")
    else:
        print(f"Every recorded read of {len(recorded)} entry file{'' if len(recorded) == 1 else 's'} lies inside the stored lists.")
    if options.summary is not None:
        with options.summary.open("a", encoding="utf-8") as handle:
            handle.write(check_summary(lines, len(recorded), target) + etw_summary(settings))
    if options.github_output is not None:
        with options.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"mode=pull-request\nfile={target if lines else ''}\n")
    return 1 if lines else 0


def check_summary(lines: list[tuple[str, str]], count: int, target: Path) -> str:
    text = ["## Recording check", ""]
    if not lines:
        text.append(f"Every recorded read of {count} entry file{'' if count == 1 else 's'} lies inside the stored lists.")
        return "\n".join(text) + "\n"
    text += [f"The recording reads outside the stored lists. Download the additions artifact (`{target.name}`) and run "
             f"`python3 actions/recording/recording.py apply {target.name}` from the repository root.", "",
             "| Suite or entry file | Addition |", "| --- | --- |"]
    text += [f"| {where} | {what} |" for where, what in lines]
    return "\n".join(text) + "\n"


def etw_summary(settings: dict) -> str:
    return f"\n{settings['etw_note']}\n" if "etw_note" in settings else ""


def write_full_result(options: argparse.Namespace, output: Path, list_path: str,
                      recorded: list[tuple[suite_selection.Suite, Unit, Reads]], settings: dict) -> int:
    document = {"kind": "full-recording", "suite_list": list_path,
                "entries": [{"suite": suite.name, "entry": unit.record["name"], "exit_code": unit.record["exit_code"],
                             "skipped": unit.record["skipped"], "incomplete_report": unit.record.get("incomplete_report"),
                             "seconds": unit.record["seconds"], "reads": reads.document()}
                            for suite, unit, reads in recorded]}
    target = options.additions or output / "full-recording.json"
    target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote the full recording of {len(recorded)} entry file{'' if len(recorded) == 1 else 's'} to {target}.")
    if options.summary is not None:
        recorder = "a live Kernel-File ETW session" if "etw_note" in settings else "the wide trace set"
        with options.summary.open("a", encoding="utf-8") as handle:
            handle.write(f"## Full recording\n\nRecorded {len(recorded)} entry files under {recorder} into `{target.name}`.\n"
                         + etw_summary(settings))
    if options.github_output is not None:
        with options.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"mode=full\nfile={target}\n")
    return 0


# --- apply ----------------------------------------------------------------------------------------------------


def add_unique(values: list, new: list) -> list:
    return values + [value for value in new if value not in values]


def add_reads(item: dict, reads: dict, entry: str | None) -> None:
    """Add reads to a suite's JSON entry: to its own fields, or to the per-entry fields of one entry file."""
    for read_field in READ_FIELDS:
        values = reads.get(read_field.key, [])
        if not values and not read_field.kept_empty:
            continue
        if entry is None:
            item[read_field.key] = add_unique(item.get(read_field.key, []), values)
        else:
            per_entry = item.setdefault(read_field.entry_key, {})
            per_entry[entry] = add_unique(per_entry.get(entry, []), values)


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
        joined = functools.reduce(add_unique, item.get(read_field.entry_key, {}).values(), [])
        if joined or read_field.kept_empty:
            item[read_field.key] = joined
        else:
            item.pop(read_field.key, None)


def canonical_order(item: dict) -> None:
    """Sort the recorded lists of a suite's JSON entry, and order its per-entry fields by its entry files, so that the
    rendered suite list does not depend on the order of the results that apply was given."""
    order = {entry: index for index, entry in enumerate(item["entries"])}

    def by_entry(entry: str) -> tuple[int, str]:
        return order.get(entry, len(order)), entry

    def sort(values: list) -> list:
        return sorted(values, key=lambda value: (value["path"], value["kind"]) if isinstance(value, dict) else (value, ""))

    for read_field in READ_FIELDS:
        if read_field.key in item:
            item[read_field.key] = sort(item[read_field.key])
        if read_field.entry_key in item:
            item[read_field.entry_key] = {entry: sort(item[read_field.entry_key][entry]) for entry in sorted(item[read_field.entry_key], key=by_entry)}
    if "entry_durations" in item:
        item["entry_durations"] = {entry: item["entry_durations"][entry] for entry in sorted(item["entry_durations"], key=by_entry)}


def raw_entries(suite: suite_selection.Suite, entry: str) -> str:
    if suite.entry_selection and entry not in suite.entries:
        raise RecordingError(f"the suite {suite.name} names {entry} only through a pattern; a suite selected per entry file "
                             "needs its entry files named one by one to store their lists")
    return entry


def load_documents(paths: list[Path]) -> list[dict]:
    documents = []
    for path in paths:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RecordingError(f"cannot read {path}: {error}") from error
        if not isinstance(document, dict) or document.get("kind") not in ("additions", "full-recording"):
            raise RecordingError(f"{path} holds neither additions nor the result of a full recording")
        documents.append(document)
    if len({document["kind"] for document in documents}) != 1:
        raise RecordingError("apply takes either additions or full-recording results, not both")
    return documents


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
    if documents[0]["kind"] == "additions":
        report = apply_additions(documents, raw, resolved, items, lookups)
    else:
        report = apply_full(documents, raw, resolved, items, lookups)
    for item in items.values():
        join_entry_lists(item)
        canonical_order(item)
    document = {"always": list(raw.always), "suites": [items[suite.name] for suite in raw.suites]}
    if lookups:
        document["module_lookups"] = {directory: sorted(lookups[directory]) for directory in sorted(lookups)}
    rendered = suite_selection.render_suite_list(suite_selection.parse_suite_list(json.dumps(document), str(suites_path)))
    if rendered != text:
        suites_path.write_text(rendered, encoding="utf-8", newline="\n")
    for line in report:
        print(line)
    if options.summary is not None:
        with options.summary.open("a", encoding="utf-8") as handle:
            handle.write("## Applied recording\n\n" + "".join(f"- {line}\n" for line in report))
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


def apply_additions(documents: list[dict], raw: suite_selection.SuiteList, resolved: dict, items: dict, lookups: dict[str, list[str]]) -> list[str]:
    merged: dict[str, dict] = {}
    for document in documents:
        for change in document["suites"]:
            target = merged.setdefault(change["suite"], {"create": False, "recorded": set(), "reads": Reads(), "entries": {}})
            target["create"] |= change["create"]
            target["recorded"] |= set(change["recorded_entries"])
            target["reads"].update(Reads.of(change.get("reads", {})))
            for entry, reads in change.get("entries", {}).items():
                target["entries"].setdefault(entry, Reads()).update(Reads.of(reads))
    report = []
    for name, change in sorted(merged.items()):
        suite = known_suite(resolved, name)
        item = items[name]
        if change["create"] and suite.paths is None:
            start_list(item, suite, suite.entries, change["recorded"])
        if not change["reads"].empty() or (change["create"] and not suite.entry_selection):
            add_reads(item, change["reads"].document(), None)
        for entry, reads in sorted(change["entries"].items()):
            add_reads(item, reads.document(), raw_entries(suite, entry))
            add_lookups(lookups, reads)
        add_lookups(lookups, change["reads"])
        count = len(change["reads"].document()["paths"]) + sum(len(reads.paths) for reads in change["entries"].values())
        report.append(f"{name}: added the recorded reads ({count} content path{'' if count == 1 else 's'} among them)")
    return report or ["No additions to apply."]


def kept_reason(entry: dict | None) -> str | None:
    """Why a full recording keeps an entry file's stored reads, or None when it may remove them."""
    if entry is None:
        return "it was not recorded"
    if entry["exit_code"] != 0:
        return f"it exited with {entry['exit_code']}"
    if entry["skipped"] is None:
        if entry.get("incomplete_report"):
            return f"it printed no complete test report: {entry['incomplete_report']}"
        return "its test runner reports no skip count"
    if entry["skipped"]:
        return f"{entry['skipped']} test{'' if entry['skipped'] == 1 else 's'} skipped"
    return None


def apply_full(documents: list[dict], raw: suite_selection.SuiteList, resolved: dict, items: dict, lookups: dict[str, list[str]]) -> list[str]:
    """Replace the lists of every recorded entry file that exited with 0 and whose test report in an accepted format
    shows no skipped test. The module lookups of a directory become those of the recorded entry files, and keep the
    stored ones while an entry file whose lists stay looks up modules there."""
    entries: dict[tuple[str, str], dict] = {}
    for document in documents:
        for entry in document["entries"]:
            entries[(entry["suite"], entry["entry"])] = entry
    recorded_lookups: dict[str, set[str]] = {}
    for entry in entries.values():
        for directory, names in entry["reads"].get("module_lookups", {}).items():
            recorded_lookups.setdefault(directory, set()).update(names)
    replaced: set[tuple[str, str]] = set()
    report = []
    for name in sorted({suite for suite, _ in entries}):
        suite = known_suite(resolved, name)
        item = items[name]
        recorded = {entry: entries.get((name, entry)) for entry in suite.entries}
        durations = dict(item.get("entry_durations", {}))
        for entry, result in recorded.items():
            if result is not None:
                durations[entry] = round(result["seconds"] / suite_selection.full_recording_scale(suite.os), 2)
        if durations:
            item["entry_durations"] = durations
        if suite.paths is None:
            start_list(item, suite, suite.entries, {entry for entry, result in recorded.items() if result is not None})
        reasons = {entry: kept_reason(result) for entry, result in recorded.items()}
        if suite.entry_selection:
            for entry, result in recorded.items():
                if result is None:
                    report.append(f"{name}: kept the list of {entry}: {reasons[entry]}")
                elif reasons[entry] is None:
                    replace_reads(item, result["reads"], raw_entries(suite, entry))
                    replaced.add((name, entry))
                else:
                    add_reads(item, result["reads"], raw_entries(suite, entry))
                    report.append(f"{name}: kept the list of {entry}: {reasons[entry]}")
            continue
        union = Reads()
        for result in recorded.values():
            if result is not None:
                union.update(Reads.of(result["reads"]))
        if all(reason is None for reason in reasons.values()):
            replace_reads(item, union.document(), None)
            replaced.update((name, entry) for entry in suite.entries)
        else:
            add_reads(item, union.document(), None)
            report += [f"{name}: kept the list of {entry}: {reason}" for entry, reason in reasons.items() if reason is not None]
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


# --- the shared digest -----------------------------------------------------------------------------------------


def loaded_modules(script: Path) -> list[Path]:
    """The script and every module of the shared CI that it loads when it runs (``recording_modules.py``), resolved on
    the search path that this script sets."""
    def read(path: str) -> str | None:
        return Path(path).read_text(encoding="utf-8") if Path(path).is_file() else None

    return [Path(path) for path in recording_modules.loaded_modules(script.as_posix(), read, [folder.as_posix() for folder in MODULE_PATH])]


def shared_files() -> list[Path]:
    """The files of the shared digest: every file of the recording step's folder, its action and the committed ETW
    consumer with its build included, and every module outside it that the recording step loads."""
    folder = Path(__file__).resolve().parent
    files = {path for path in folder.rglob("*") if path.is_file() and "__pycache__" not in path.relative_to(folder).parts}
    files.update(loaded_modules(Path(__file__).resolve()))
    return sorted(files)


def shared_digest() -> str:
    """The SHA-256 over the path and the SHA-256 of the content of every shared file, in the code-point order of the
    paths: a Windows path sorts case-insensitively, so ``Path`` order differs between the platforms."""
    names = {path.relative_to(SHARED_FOLDER).as_posix(): path for path in shared_files()}
    lines = "".join(f"{hashlib.sha256(names[name].read_bytes()).hexdigest()}  {name}\n" for name in sorted(names))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def run_digest(options: argparse.Namespace) -> int:
    digest = shared_digest()
    print(f"shared CI digest {digest}")
    if options.github_output is not None:
        with options.github_output.open("a", encoding="utf-8") as handle:
            handle.write(f"shared-digest={digest}\n")
    if options.expect is not None and options.expect != digest:
        print(f"The shared recording step in {SHARED_FOLDER} has the digest {digest}, but select took {options.expect}: "
              "v1 moved during the run. Start the whole run again.", file=sys.stderr)
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
    executing = commands.add_parser("exec", help="run one setup or one entry file under the recording")
    executing.add_argument("--output", type=Path)
    unit = executing.add_mutually_exclusive_group(required=True)
    unit.add_argument("--setup", help="the name of the setup that the command runs")
    unit.add_argument("--entry", help="the entry file that the command runs, as the suite list names it")
    executing.add_argument("--suite", help="the suite of the entry file, when the suite list names it in several suites")
    executing.add_argument("--suites", type=Path, help="the suite list, whose setups of the entry file decide its environment")
    executing.add_argument("--root", type=Path, default=Path("."), help="the repository root of the suite list's entry files "
                           "(default: the working directory)")
    executing.add_argument("arguments", nargs=argparse.REMAINDER, help="-- and the command")
    providing = commands.add_parser("provide", help="record what a setup installed in this job provides to the entry files that state it")
    providing.add_argument("--output", type=Path)
    providing.add_argument("--setup", required=True, help="the name of the setup, as the suite list states it")
    providing.add_argument("--path", action="append", default=[], help="an absolute tool directory of the setup (repeatable)")
    providing.add_argument("--variable", action="append", default=[], help="<name>=<value> of a variable that the setup sets (repeatable)")
    providing.add_argument("--python", action="append", default=[], help="a Python interpreter of the setup, into whose site directory "
                           "the lookup report is installed (repeatable)")
    checking = commands.add_parser("check", help="compare the recording with the suite list")
    checking.add_argument("--output", type=Path)
    checking.add_argument("--root", type=Path, default=Path("."), help="the repository root (default: the working directory)")
    checking.add_argument("--suites", type=Path, required=True, help="the suite list")
    checking.add_argument("--additions", type=Path, help="where to write the additions or the full-recording result")
    checking.add_argument("--summary", type=Path, help="append the Markdown summary to this file")
    checking.add_argument("--github-output", type=Path, help="append mode= and file= lines to this file")
    applying = commands.add_parser("apply", help="apply additions or full-recording results to the suite list")
    applying.add_argument("--root", type=Path, default=Path("."), help="the repository root (default: the working directory)")
    applying.add_argument("--suites", type=Path, help="the suite list (default: the one the files name, under the root)")
    applying.add_argument("--summary", type=Path, help="append the Markdown report to this file")
    applying.add_argument("files", type=Path, nargs="+", help="additions or full-recording results")
    digesting = commands.add_parser("digest", help="print the digest of the shared recording step")
    digesting.add_argument("--expect", help="the digest that select took; a different one fails")
    digesting.add_argument("--github-output", type=Path, help="append a shared-digest= line to this file")
    options = parser.parse_args(argv)
    if options.command == "exec" and options.entry is None and (options.suite is not None or options.suites is not None):
        parser.error("--suite and --suites go with --entry")
    if options.command == "exec" and options.entry is not None and options.suites is None:
        parser.error("--entry needs --suites")
    return options


def main(argv: list[str] | None = None) -> int:
    options = parse_arguments(sys.argv[1:] if argv is None else argv)
    handlers = {"base": run_base, "start": run_start, "exec": run_exec, "provide": run_provide, "check": run_check, "apply": run_apply, "digest": run_digest}
    try:
        return handlers[options.command](options)
    except (RecordingError, suite_selection.SuiteListError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
