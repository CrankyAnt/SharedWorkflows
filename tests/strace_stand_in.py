"""A stand-in for ``strace`` on the ``PATH`` of a recording fixture, so that the tests of the recording step start no tracer.

The recording step runs it as ``strace <options> -o <trace> -e trace=<calls> -- <command> ...``. It appends its arguments
to ``calls.jsonl`` in the directory that ``STRACE_STAND_IN`` names, runs the command and exits with its exit code. It then
writes the stored strace recording that ``commands.json`` in that directory names for the command, with the paths of this
run in place of the tokens and with only the lines of the traced calls, or an empty recording for a command that it
names none for, and the stored lookup reports beside it into the directory that ``SHARED_LOOKUPS`` names. "Renew the
stored strace recordings" in ``docs/maintaining.md`` describes the stored recordings.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

COMMANDS = Path(__file__).resolve().parent / "fixtures" / "recording_strace" / "commands"
BASE, TMPDIR, PYTHONPYCACHEPREFIX = "${BASE}", "${TMPDIR}", "${PYTHONPYCACHEPREFIX}"
# The tokens of a stored strace recording, in the order in which the recorded paths they stand for are replaced: the
# temporary directory and the bytecode cache of a command lie inside the fixture's base directory.
TOKENS = (TMPDIR, PYTHONPYCACHEPREFIX, BASE)


def tokenize(text: str, paths: dict[str, str]) -> str:
    """``text`` with each recorded path of ``paths``, by token, replaced by its token."""
    for token in TOKENS:
        if token in paths:
            text = text.replace(paths[token], token)
    return text


def detokenize(text: str, paths: dict[str, str]) -> str:
    """``text`` with each token of ``paths`` replaced by the path of this run."""
    for token in TOKENS:
        if token in paths:
            text = text.replace(token, paths[token])
    return text


def parse_line(line: str) -> tuple[str, str, str | None]:
    """The process id, the call text and the system call of a strace line; the call of a resumed line is the resumed
    call, and a signal or exit line has none."""
    pid, _, body = line.partition(" ")
    body = body.strip()
    if body.startswith("<... "):
        return pid, body, body[len("<... "):].split(" ", 1)[0]
    paren = body.find("(")
    return pid, body, body[:paren] if paren > 0 and body[:paren].isidentifier() else None


def main(arguments: list[str]) -> int:
    stand_in = Path(os.environ["STRACE_STAND_IN"])
    with (stand_in / "calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(arguments) + "\n")
    separator = arguments.index("--")
    options, command = arguments[:separator], arguments[separator + 1:]
    trace = Path(options[options.index("-o") + 1])
    traced = set(options[options.index("-e") + 1].removeprefix("trace=").replace("?", "").split(","))
    code = subprocess.run(command, check=False).returncode
    name = json.loads((stand_in / "commands.json").read_text(encoding="utf-8")).get(json.dumps(command))
    text = ""
    if name is not None:
        paths = {TMPDIR: os.environ["TMPDIR"], PYTHONPYCACHEPREFIX: os.environ["PYTHONPYCACHEPREFIX"], BASE: str(stand_in.parent)}
        text = detokenize((COMMANDS / f"{name}.strace").read_text(encoding="utf-8"), paths)
    lines = [line for line in text.splitlines(keepends=True) if parse_line(line)[2] in traced or parse_line(line)[2] is None]
    trace.write_text("".join(lines), encoding="utf-8")
    reports = COMMANDS / f"{name}.lookups"
    if name is not None and reports.is_dir():
        for report in sorted(reports.iterdir()):
            (Path(os.environ["SHARED_LOOKUPS"]) / report.name).write_text(detokenize(report.read_text(encoding="utf-8"), paths), encoding="utf-8")
    return code if code >= 0 else 128 - code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
