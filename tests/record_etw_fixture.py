"""Record the neutral event files that the recorded-event-file tests read, on Windows where a live ETW session
runs: with administrator rights or after the setup of tests/etw_access.ps1.

    python -m tests.record_etw_fixture <destination> [<run> ...]

The classification fixture's Windows entry files run twice under a live ETW session, and the entry files of the
linked-worktree fixture and of the Node fixture once, each run in a full recording on an interpreter of the fixture's own
with the lookup report, whose settings, units, lookup reports, neutral event file and consumer log land in
``<destination>/classification/first`` and ``second``, in ``<destination>/linked`` and in ``<destination>/node``. Naming
runs, such as ``node`` or ``classification/first``, records only those. Replace each recorded directory of
``tests/fixtures/recording_etw`` with its counterpart in ``<destination>`` to renew the recordings; a copy over it keeps
the units and lookup reports of the earlier recording.
"""

from __future__ import annotations

import getpass
import re
import sys
from pathlib import Path

from tests.recording_fixtures import ETW_UNAVAILABLE, RECORDING_FILES, entry_command, load_recording
from tests.test_suite_selection_recording_classification import ENTRIES, LINKED_ENTRIES, WINDOWS_FILES, classification_fixture, linked_worktree_fixture
from tests.test_suite_selection_recording_etw import NODE_ENTRIES, node_fixture


def attributed_events(output: Path) -> str:
    """Only the lines of ``events.tsv`` that belong to the recording's units, plus their ``stop`` lines, so unrelated
    processes on the recording machine do not enter the stored fixtures."""
    recording = load_recording()
    events_path = output / "events.tsv"
    raw_lines: list[str] = []
    parsed = []
    with events_path.open("r", encoding="utf-8", errors="replace") as handle:
        for raw in handle:
            parts = raw.rstrip("\r\n").split("\t", 4)
            if len(parts) in (4, 5) and parts[0].isdigit():
                if len(parts) == 5:
                    parts[4] = recording.unescape_field(parts[4])
                raw_lines.append(raw.rstrip("\r\n") + "\n")
                parsed.append(recording.EtwLine(*parts))
    grouped = recording.attribute(parsed, recording.load_units(output))
    kept_indices = {index for items in grouped.values() for index, _ in items}
    pids = {line.pid for items in grouped.values() for _, line in items}
    for index, line in enumerate(parsed):
        if line.operation == "stop" and line.pid in pids:
            kept_indices.add(index)
    return "".join(raw_lines[index] for index in sorted(kept_indices))


PERSONAL_HOME_PATH = re.compile(r"""(?:(?i:[a-z]:[\\/]+users[\\/]+)|/(?:home|Users)/)([^\\/\s"'<>|:]+)(?:[\\/]|["']|\s|$)""")


def scrub_user(text: str, user: str) -> str:
    """Replace ``\\Users\\<user>`` segments with ``\\Profiles\\fixture`` so stored fixtures neither name a local
    account nor match the personal-user-home-path Gitleaks rule."""
    if not user:
        return text
    pattern = re.compile(rf"(?i)([\\/])Users([\\/]+){re.escape(user)}(?=[\\/\"'\s]|$)")
    return pattern.sub(r"\1Profiles\2fixture", text)


def write_scrubbed(source: Path, target: Path, user: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    text = source.read_text(encoding="utf-8", errors="replace")
    target.write_text(scrub_user(text, user), encoding="utf-8", newline="\n")


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    if ETW_UNAVAILABLE is not None:
        print(ETW_UNAVAILABLE, file=sys.stderr)
        return 2
    destination = Path(argv[0]).resolve()
    user = getpass.getuser()
    cleanups = []
    cleanup = lambda function, *arguments, **keywords: cleanups.append((function, arguments, keywords))  # noqa: E731
    runs = [(Path("classification", run), ENTRIES, lambda: classification_fixture(cleanup, WINDOWS_FILES)) for run in ("first", "second")]
    runs.append((Path("linked"), LINKED_ENTRIES, lambda: linked_worktree_fixture(cleanup)))
    runs.append((Path("node"), NODE_ENTRIES, lambda: node_fixture(cleanup)))
    if argv[1:]:
        runs = [run for run in runs if run[0].as_posix() in argv[1:]]
    try:
        for run, entries, make in runs:
            fixture = make()
            started = fixture.start(full=True)
            if started.returncode != 0:
                print(started.stdout + started.stderr, file=sys.stderr)
                return 1
            fixture.set_up_python()
            for entry in entries:
                ran = fixture.exec_entry(entry, *entry_command(entry, "python"))
                if ran.returncode != 0:
                    print(ran.stdout + ran.stderr, file=sys.stderr)
            checked = fixture.check()
            print(checked.stdout + checked.stderr)
            if checked.returncode != 0:
                return 1
            for name in RECORDING_FILES:
                source, target = fixture.output / name, destination / run / name
                if name == "events.tsv":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(scrub_user(attributed_events(fixture.output), user), encoding="utf-8", newline="\n")
                elif source.is_dir():
                    for file in sorted(source.rglob("*")):
                        if file.is_file():
                            write_scrubbed(file, target / file.relative_to(source), user)
                else:
                    write_scrubbed(source, target, user)
    finally:
        for function, arguments, keywords in reversed(cleanups):
            function(*arguments, **keywords)
    leaks = []
    for path in sorted(destination.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if (user and user.lower() not in ("fixture", "runneradmin") and user.lower() in text.lower()) or PERSONAL_HOME_PATH.search(text):
            leaks.append(str(path))
    if leaks:
        print("these recordings still contain a local account or home path: " + ", ".join(leaks), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
