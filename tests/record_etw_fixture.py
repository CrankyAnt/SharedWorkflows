"""Record the neutral event files that the recorded-event-file tests read, on Windows with administrator rights.

    python -m tests.record_etw_fixture <destination> [<run> ...]

The classification fixture's Windows entry files run twice under a live ETW session, and the entry files of the
linked-worktree fixture and of the Node fixture once, each run in a full recording on an interpreter of the fixture's own
with the lookup report, whose settings, units, lookup reports, neutral event file and consumer log land in
``<destination>/classification/first`` and ``second``, in ``<destination>/linked`` and in ``<destination>/node``. Naming
runs, such as ``node`` or ``classification/first``, records only those. Copy the contents of ``<destination>`` over
``tests/fixtures/recording_etw`` to renew the recordings.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from tests.recording_fixtures import RECORDING_FILES, entry_command, etw_available
from tests.test_recording_classification import ENTRIES, LINKED_ENTRIES, WINDOWS_FILES, classification_fixture, linked_worktree_fixture
from tests.test_recording_etw import NODE_ENTRIES, node_fixture


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    if not etw_available():
        print("a live ETW session needs Windows, dotnet and administrator rights", file=sys.stderr)
        return 2
    destination = Path(argv[0]).resolve()
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
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                else:
                    shutil.copyfile(source, target)
    finally:
        for function, arguments, keywords in reversed(cleanups):
            function(*arguments, **keywords)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
