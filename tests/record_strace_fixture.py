"""Record the stored strace recordings that the classification tests and the stand-in strace replay, on Linux with strace.

    python3 -m tests.record_strace_fixture <destination>

The classification fixture's entry files run twice in a full recording, the linked-worktree fixture's once, and each
command of the stored fixtures of ``tests/test_recording_check.py`` and ``tests/test_recording_full.py`` runs once under
the wide trace set, each on an interpreter of the fixture's own with the lookup report, whose reports are stored beside
the traces. Copy the contents of ``<destination>`` over ``tests/fixtures/recording_strace`` to renew the recordings. "Renew the stored
strace recordings" in ``docs/maintaining.md`` describes the stored files.
"""

from __future__ import annotations

import getpass
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

from tests import test_recording_check, test_recording_full
from tests.recording_fixtures import RECORDING, ROOT, RecordingFixture, StoredFixture, strace_recording_files
from tests.strace_stand_in import BASE, PYTHONPYCACHEPREFIX, TMPDIR, parse_line, tokenize
from tests.test_recording_classification import LINKED_ENTRIES, STRACE_ENTRIES, STRACE_FILES, classification_fixture, linked_worktree_fixture

SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def load_recording():
    spec = importlib.util.spec_from_file_location("recording", RECORDING)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["recording"] = module
    spec.loader.exec_module(module)
    return module


def drop_system_lines(text: str, base: str, recording) -> str:
    """The trace without the open, stat and probe lines of a system path, which the parser of ``check`` skips."""
    skip = tuple(prefix for prefix in recording.SYSTEM_PREFIXES if not (base + "/").startswith(prefix))
    calls = recording.OPENS | recording.STATS | recording.PROBES
    kept, unfinished = [], set()
    for line in text.splitlines(keepends=True):
        pid, body, call = parse_line(line)
        if body.startswith("<... "):
            if pid in unfinished:
                unfinished.discard(pid)
                continue
        elif call in calls:
            quote = body.find('"', body.find("("))
            if quote >= 0 and body.startswith(skip, quote + 1):
                if body.endswith("<unfinished ...>"):
                    unfinished.add(pid)
                continue
        kept.append(line)
    return "".join(kept)


def drop_system_lookups(text: str, base: str) -> str:
    """The lookup report without the listings and lookups in a directory outside the fixture's base directory."""
    kept = []
    for line in text.splitlines(keepends=True):
        fields = line.rstrip("\n").split("\t")
        directory = fields[2] if fields[0] == "list" else fields[1] if fields[0] == "lookup" else base
        if directory == base or directory.startswith(base + "/"):
            kept.append(line)
    return "".join(kept)


def write_stored(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def give_home(fixture: RecordingFixture) -> None:
    home = fixture.base / "profile"
    home.mkdir()
    os.environ["HOME"] = str(home)


def store_recording(fixture: RecordingFixture, target: Path, recording) -> None:
    base = str(fixture.base)
    for path in strace_recording_files(fixture.output):
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".strace":
            text = drop_system_lines(text, base, recording)
        elif path.suffix == ".tsv":
            text = drop_system_lookups(text, base)
        write_stored(target / path.relative_to(fixture.output), tokenize(text, {BASE: base}))


def store_commands(stored: StoredFixture, cleanup, target: Path, recording) -> str | None:
    """Record each command of a stored fixture; the output of a failed step, or None."""
    fixture = stored.fixture(cleanup, [], stand_in=False)
    give_home(fixture)
    started = fixture.start(full=True)
    if started.returncode != 0:
        return started.stdout + started.stderr
    os.environ["PATH"] = str(fixture.set_up_python()) + os.pathsep + SYSTEM_PATH
    try:
        for name, command in stored.commands.items():
            ran = fixture.exec_setup(name, *command)
            if ran.returncode != 0:
                return ran.stdout + ran.stderr
    finally:
        os.environ["PATH"] = SYSTEM_PATH
    for path in (fixture.output / "units").glob("*.json"):
        record = json.loads(path.read_text(encoding="utf-8"))
        trace = (fixture.output / "traces" / record["trace"]).read_text(encoding="utf-8")
        paths = {TMPDIR: record["temporary"], PYTHONPYCACHEPREFIX: record["cache"], BASE: str(fixture.base)}
        write_stored(target / stored.name / f"{record['name']}.strace", tokenize(drop_system_lines(trace, str(fixture.base), recording), paths))
        for report in sorted((fixture.output / "lookups" / record["lookups"]).glob("*.tsv")):
            write_stored(target / stored.name / f"{record['name']}.lookups" / report.name,
                         tokenize(drop_system_lookups(report.read_text(encoding="utf-8"), str(fixture.base)), paths))
    return None


def leaking_files(destination: Path, names: tuple[str, ...]) -> list[str]:
    """The stored files that name any of ``names``."""
    return [str(path) for path in sorted(destination.rglob("*")) if path.is_file() and any(name in path.read_text(encoding="utf-8") for name in names)]


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    if not sys.platform.startswith("linux") or shutil.which("strace", path=SYSTEM_PATH) is None:
        print("the stored strace recordings need Linux with strace", file=sys.stderr)
        return 2
    recording = load_recording()
    private = (str(Path.home()), f"/{getpass.getuser()}/", str(ROOT), os.getcwd())
    destination = Path(argv[0]).resolve()
    # Git compares PWD with its working directory, so an inherited PWD would name the directory the script runs in.
    for key in [key for key in os.environ if key.startswith(("PYTHON", "XDG_", "GIT_")) or key in ("PWD", "OLDPWD")]:
        del os.environ[key]
    os.environ["PATH"] = SYSTEM_PATH
    cleanups = []
    cleanup = lambda function, *arguments, **keywords: cleanups.append((function, arguments, keywords))  # noqa: E731
    try:
        runs = [(Path("classification", run), STRACE_ENTRIES, lambda: classification_fixture(cleanup, STRACE_FILES, STRACE_ENTRIES, stand_in=False))
                for run in ("first", "second")]
        runs.append((Path("linked"), LINKED_ENTRIES, lambda: linked_worktree_fixture(cleanup, stand_in=False)))
        for target, entries, make in runs:
            fixture = make()
            give_home(fixture)
            checked = fixture.record(entries, full=True, python=True)
            if checked.returncode != 0:
                print(checked.stdout + checked.stderr, file=sys.stderr)
                return 1
            store_recording(fixture, destination / target, recording)
        for stored in (*test_recording_check.STORED, *test_recording_full.STORED):
            failure = store_commands(stored, cleanup, destination / "commands", recording)
            if failure is not None:
                print(failure, file=sys.stderr)
                return 1
    finally:
        for function, arguments, keywords in reversed(cleanups):
            function(*arguments, **keywords)
    found = leaking_files(destination, private)
    if found:
        print("these recordings name the home directory, the user name or the checkout; record again from another place: " + ", ".join(found),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
