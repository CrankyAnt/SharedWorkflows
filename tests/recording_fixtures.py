"""Fixture repositories for the recording step, a runner of its commands as their own processes, and the stored strace
recordings that the stand-in ``strace`` on a fixture's ``PATH`` and the classification tests replay."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from dataclasses import dataclass, field
from pathlib import Path

from tests.strace_stand_in import BASE, detokenize

ROOT = Path(__file__).resolve().parents[1]
RECORDING = ROOT / "actions" / "recording" / "recording.py"
GIT_IDENTITY = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false"]
ETW_RECORDINGS = ROOT / "tests" / "fixtures" / "recording_etw"
RECORDING_FILES = ("recording.json", "events.tsv", "logs/etw.log", "units", "lookups")
STRACE_RECORDINGS = ROOT / "tests" / "fixtures" / "recording_strace"
STRACE_RECORDING_FILES = ("recording.json", "units", "traces", "lookups")
STAND_IN = ROOT / "tests" / "strace_stand_in.py"

requires_linux = unittest.skipUnless(sys.platform.startswith("linux"), "the recording step records under strace on Linux")
requires_posix_paths = unittest.skipIf(sys.platform == "win32", "a stored strace recording names POSIX paths")


def load_recording():
    """The recording step's module, loaded once by its path."""
    module = sys.modules.get("recording")
    if module is None:
        spec = importlib.util.spec_from_file_location("recording", RECORDING)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["recording"] = module
        spec.loader.exec_module(module)
    return module


def etw_available() -> bool:
    if sys.platform != "win32" or shutil.which("dotnet") is None:
        return False
    import ctypes

    return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]


requires_etw = unittest.skipUnless(etw_available(), "a live ETW session needs Windows, dotnet and administrator rights")


def copy_recording(source: Path, output: Path) -> None:
    """Copy a recording's settings, units, lookup reports, neutral event file and consumer log from ``source`` into
    ``output``."""
    for name in RECORDING_FILES:
        if (source / name).is_dir():
            shutil.copytree(source / name, output / name, dirs_exist_ok=True)
        else:
            (output / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, output / name)


def strace_recording_files(directory: Path) -> list[Path]:
    """The settings, units, traces and lookup reports of the strace recording in ``directory``."""
    return [path for name in STRACE_RECORDING_FILES
            for path in ([directory / name] if (directory / name).is_file() else
                         sorted(path for path in (directory / name).rglob("*") if path.is_file()) if (directory / name).is_dir() else [])]


def copy_strace_recording(source: Path, fixture: RecordingFixture) -> None:
    """Copy a stored strace recording's settings, units, traces and lookup reports from ``source`` into the output
    directory of ``fixture``, with the fixture's base directory in place of its token."""
    for path in strace_recording_files(source):
        target = fixture.output / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(detokenize(path.read_text(encoding="utf-8"), {BASE: str(fixture.base)}), encoding="utf-8", newline="\n")


def python_with_lookup_report(location: Path, provide) -> Path:
    """Create a virtual environment at ``location`` and give it the lookup report through ``provide``, which runs the
    recording step's ``provide`` of the setup ``python`` with the options it gets; return the environment's interpreter."""
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(location)], check=True)
    tools = location / ("Scripts" if sys.platform == "win32" else "bin")
    interpreter = tools / ("python.exe" if sys.platform == "win32" else "python3")
    provided = provide("--path", str(tools), "--python", str(interpreter))
    assert provided.returncode == 0, provided.stdout + provided.stderr
    return interpreter


def entry_command(entry: str, python: str = "python3") -> tuple[str, ...]:
    """The command that runs a fixture's entry file: PowerShell for a script of its own, Python otherwise."""
    return ("pwsh", "-NoProfile", "-NonInteractive", "-File", entry) if entry.endswith(".ps1") else (python, entry)


def suite(name: str, entries: list[str], **fields) -> dict:
    return {"name": name, "entries": entries, "command": ["python3"], **fields}


class RecordingFixture:
    """A Git repository with a suite list in a temporary directory, and the recording's output directory beside it."""

    def __init__(self, cleanup, files: dict[str, str], suites: list[dict], *, commit: bool = True, stand_in: bool = True) -> None:
        """``cleanup`` registers the removal of the fixture: a case's addCleanup or a class's addClassCleanup. Off Windows,
        ``stand_in`` puts the stand-in ``strace`` first on the ``PATH`` of the recording's commands."""
        self.base = Path(tempfile.mkdtemp(prefix="recording-"))
        cleanup(shutil.rmtree, self.base, ignore_errors=True)
        self.root = self.base / "repo"
        self.output = self.base / "recording"
        self.summary = self.base / "summary.md"
        for relative, text in files.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(textwrap.dedent(text), encoding="utf-8")
        self.write_suites(suites)
        self.git("init", "-q")
        self.git("add", "-A")
        if commit:
            self.git("commit", "-q", "-m", "fixture")
        self.stand_in = self.base / "stand-in" if stand_in and sys.platform != "win32" else None
        if self.stand_in is not None:
            (self.stand_in / "bin").mkdir(parents=True)
            program = self.stand_in / "bin" / "strace"
            program.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{STAND_IN}" "$@"\n', encoding="utf-8")
            program.chmod(0o755)
            self.replay()

    def git(self, *arguments: str, environment: dict[str, str] | None = None) -> str:
        """Run Git in the fixture repository, with ``environment`` added to the current environment."""
        return subprocess.run(["git", *GIT_IDENTITY, "-C", str(self.root), *arguments], capture_output=True, text=True, check=True,
                              env=None if environment is None else {**os.environ, **environment}).stdout

    def replay(self, stored: StoredFixture | None = None) -> None:
        """Let the stand-in ``strace`` write the stored recording of each command of ``stored``; any other command gets an
        empty recording."""
        assert self.stand_in is not None
        commands = {} if stored is None else {json.dumps(list(command)): f"{stored.name}/{name}" for name, command in stored.commands.items()}
        (self.stand_in / "commands.json").write_text(json.dumps(commands, indent=2) + "\n", encoding="utf-8")

    def stand_in_calls(self) -> list[list[str]]:
        """The arguments of every call of the stand-in ``strace``, in call order."""
        assert self.stand_in is not None
        calls = self.stand_in / "calls.jsonl"
        return [json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()] if calls.is_file() else []

    def write_suites(self, suites: list[dict]) -> None:
        path = self.root / "suites.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"always": [], "suites": suites}, indent=2) + "\n", encoding="utf-8")

    def suites(self) -> dict:
        return json.loads((self.root / "suites.json").read_text(encoding="utf-8"))

    def suite(self, name: str) -> dict:
        return next(item for item in self.suites()["suites"] if item["name"] == name)

    def run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        path = os.environ.get("PATH", "")
        return self.run_with_path(path if self.stand_in is None else str(self.stand_in / "bin") + os.pathsep + path, *arguments)

    def run_with_path(self, path: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(self.command(*arguments), cwd=self.root, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=self.environment(path), check=False)

    def command(self, *arguments: str) -> list[str]:
        return [sys.executable, str(RECORDING), *arguments]

    def environment(self, path: str | None = None) -> dict[str, str]:
        """The environment of the recording's commands, with ``path`` as ``PATH`` (the current one by default)."""
        environment = {key: value for key, value in os.environ.items() if key != "RUNNER_TEMP"}
        environment["PATH"] = os.environ.get("PATH", "") if path is None else path
        if self.stand_in is not None:
            environment["STRACE_STAND_IN"] = str(self.stand_in)
        return environment

    def start(self, *, full: bool = False, job: str | None = None) -> subprocess.CompletedProcess[str]:
        return self.run("start", "--output", str(self.output), *(["--full"] if full else []), *(["--job", job] if job else []))

    def exec_entry(self, entry: str, *command: str) -> subprocess.CompletedProcess[str]:
        return self.run("exec", "--output", str(self.output), "--entry", entry, "--suites", "suites.json", "--", *(command or entry_command(entry)))

    def exec_setup(self, name: str, *command: str) -> subprocess.CompletedProcess[str]:
        return self.run("exec", "--output", str(self.output), "--setup", name, "--", *command)

    def provide(self, name: str, *options: str) -> subprocess.CompletedProcess[str]:
        return self.run("provide", "--output", str(self.output), "--setup", name, *options)

    def set_up_python(self) -> Path:
        """Set up an interpreter of the fixture's own with the lookup report, as the setup ``python`` provides it, and
        return its tool directory."""
        return python_with_lookup_report(self.base / "python", lambda *options: self.provide("python", *options)).parent

    def check(self) -> subprocess.CompletedProcess[str]:
        return self.run("check", "--output", str(self.output), "--suites", "suites.json", "--summary", str(self.summary))

    def additions(self) -> dict:
        return json.loads((self.output / "additions.json").read_text(encoding="utf-8"))

    def full_result(self) -> dict:
        return json.loads((self.output / "full-recording.json").read_text(encoding="utf-8"))

    def apply(self, *files: Path) -> subprocess.CompletedProcess[str]:
        return self.run("apply", *(str(file) for file in files or (self.output / "additions.json",)))

    def record(self, entries: list[str], *, full: bool = False, job: str | None = None, python: bool = False) -> subprocess.CompletedProcess[str]:
        """Start a recording, as the shard job ``job`` when given, run each entry file with python3 under it, and check
        it; with ``python``, the setup ``python`` provides the interpreter, with the lookup report."""
        started = self.start(full=full, job=job)
        assert started.returncode == 0, started.stderr
        if python:
            self.set_up_python()
        for entry in entries:
            ran = self.exec_entry(entry)
            assert ran.returncode == 0, ran.stdout + ran.stderr
        return self.check()

    def reads_of(self, entry: str) -> dict:
        """The classified reads of one entry file in a full recording."""
        return next(item["reads"] for item in self.full_result()["entries"] if item["entry"] == entry)


@dataclass(frozen=True)
class StoredFixture:
    """A fixture repository whose commands have stored strace recordings, which the stand-in ``strace`` writes when a
    test runs a command under the recording step; "Renew the stored strace recordings" in ``docs/maintaining.md``
    describes them."""

    name: str
    files: dict[str, str]
    commands: dict[str, tuple[str, ...]]
    links: dict[str, str] = field(default_factory=dict)

    def fixture(self, cleanup, suites: list[dict], files: dict[str, str] | None = None, *, stand_in: bool = True) -> RecordingFixture:
        """The fixture repository with ``files`` beside the stored ones, committed, whose stand-in replays the commands."""
        fixture = RecordingFixture(cleanup, {**self.files, **(files or {})}, suites, commit=False, stand_in=stand_in)
        for link, target in self.links.items():
            os.symlink(target, fixture.root / link)
        fixture.git("add", "-A")
        fixture.git("commit", "-q", "-m", "fixture")
        if fixture.stand_in is not None:
            fixture.replay(self)
        return fixture
