#!/usr/bin/env python3
"""Decide, install and run the tests of Python tests; the shared workflow calls one subcommand per step.

    python3 python_tests.py decide  [--root DIR] --base SHA --head SHA [--compare merge-base|direct] [--github-output FILE] [--summary FILE]
    python3 python_tests.py decide  [--root DIR] --lookup FILE [--github-output FILE] [--summary FILE]
    python3 python_tests.py install --root DIR [--install VALUE]
    python3 python_tests.py test    --root DIR

``decide`` writes ``run`` as ``true`` or ``false``. The tests do not run when every changed file ends in ``.md`` in
any case, or when the push lookup of ``suite_selection.py lookup`` names the case ``identical``: the pushed tree passed
on its pull request. Every other change runs them, as do changed files that cannot be determined and a change of no
file. A pull request compares from the merge base of ``--base`` and ``--head``; a push compares from the landing base
of the lookup to the pushed commit. A renamed file counts under its old and its new path.

``install`` runs pip on the interpreter that runs the script, in ``--root``: a value ending in ``.txt`` is a
requirements file (``pip install -r``), any other value an extra of the project (``pip install ".[<extra>]"``); an
empty value installs nothing.

``test`` runs ``python -m pytest`` in ``--root`` when the project there configures pytest, otherwise ``python -m
unittest discover``, on the interpreter that runs the script. It fails with a message when pytest is configured but
not installed and when the runner finds no tests. The exit code is 0 when the step passed, 1 when it failed and 2 for
an input that cannot be read.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ALL_ZERO = re.compile(r"0{40}")
LOOKUP_CASES = ("identical", "behind", "pull-request", "own")
# An extra name as the core metadata specification normalises it, and as pip accepts it between the brackets.
EXTRA_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
# The report line of a unittest run without tests; before Python 3.12 such a run ends with 0.
NO_TESTS_LINE = re.compile(r"Ran 0 tests in ")
# The files whose presence at the root configures pytest, whatever they hold.
PYTEST_FILES = ("pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "conftest.py")
PYTEST_NO_TESTS = 5  # pytest's exit code when it collected no tests
UNITTEST_NO_TESTS = 5  # unittest's exit code, from Python 3.12, when no test ran


class ProjectError(Exception):
    """A file of the project or the push lookup cannot be read."""


# --- decide ---------------------------------------------------------------------------------------------------


def git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *arguments], cwd=root, capture_output=True, text=True, encoding="utf-8", check=False)


def changed_files(root: Path, base: str | None, head: str, compare: str) -> list[str] | None:
    """The files that differ between the commits, or None when they cannot be determined. ``compare`` is ``merge-base``
    for a pull request and ``direct`` for a push; a deleted or renamed file counts under each of its paths."""
    if not base or ALL_ZERO.fullmatch(base) or not head:
        return None
    for commit in (base, head):
        if git(root, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}").returncode != 0:
            return None
    start = base
    if compare == "merge-base":
        merge_base = git(root, "merge-base", base, head)
        if merge_base.returncode != 0:
            return None
        start = merge_base.stdout.strip()
    listing = git(root, "diff", "--name-only", "--no-renames", "-z", start, head)
    if listing.returncode != 0:
        return None
    return sorted({item for item in listing.stdout.split("\0") if item})


def read_lookup(path: Path) -> dict:
    """The push lookup that ``suite_selection.py lookup`` wrote."""
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProjectError(f"the push lookup {path} cannot be read: {error}") from error
    if not isinstance(found, dict) or found.get("case") not in LOOKUP_CASES or not isinstance(found.get("head"), str) or not found["head"]:
        raise ProjectError(f"the push lookup {path} must name a case and the pushed commit")
    return found


def decision(changed: list[str] | None) -> tuple[bool, str]:
    """Whether the tests run over these changed files, with the reason."""
    if changed is None:
        return True, "the changed files could not be determined"
    if not changed:
        return True, "no file changed"
    other = next((path for path in changed if not path.lower().endswith(".md")), None)
    if other is not None:
        return True, f"`{other}` is no Markdown file"
    return False, "every changed file ends in `.md`"


def decide(root: Path, base: str | None, head: str | None, compare: str, lookup: Path | None, output: Path | None, summary: Path | None) -> int:
    heading = None
    changed: list[str] | None = None
    if lookup is None:
        changed = changed_files(root, base, head or "", compare)
        run, reason = decision(changed)
    else:
        found = read_lookup(lookup)
        heading = f"Push lookup: `{found['case']}`, {found.get('reason', '')}."
        if found["case"] == "identical":
            run, reason = False, f"the tree already passed `tests / tests` on pull request #{found.get('pull_request')}"
        else:
            changed = changed_files(root, found.get("landing_base") or None, found["head"], "direct")
            run, reason = decision(changed)
    value = "true" if run else "false"
    print(f"run={value}: {reason}")
    if output is not None:
        with output.open("a", encoding="utf-8") as handle:
            handle.write(f"run={value}\n")
    if summary is not None:
        lines = ["## Python tests for this change", ""]
        if heading is not None:
            lines += [heading, ""]
        if changed is not None:
            lines += [f"{len(changed)} changed file{'' if len(changed) == 1 else 's'}.", ""]
        lines.append(f"The tests {'run' if run else 'do not run'}: {reason}.")
        with summary.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return 0


# --- install --------------------------------------------------------------------------------------------------


def install_command(value: str, python: Path) -> list[str] | None:
    """The pip command that installs ``value`` from the project's directory, or None for an empty value."""
    if not value:
        return None
    base = [str(python), "-m", "pip", "install", "--disable-pip-version-check"]
    if value.endswith(".txt"):
        return [*base, "-r", value]
    if not EXTRA_NAME.fullmatch(value):
        raise ProjectError(f"install {value!r} is no requirements file ending in .txt and no extra name")
    return [*base, f".[{value}]"]


def show(command: list[str]) -> None:
    print("+ " + " ".join(command), flush=True)


def run_command(command: list[str], root: Path) -> int:
    show(command)
    return subprocess.run(command, cwd=root, check=False).returncode


def install(root: Path, value: str) -> int:
    command = install_command(value, Path(sys.executable))
    if command is None:
        print("No install value: nothing is installed.")
        return 0
    return run_command(command, root)


# --- test -----------------------------------------------------------------------------------------------------


def pytest_configured(root: Path) -> bool:
    """Whether the project at ``root`` configures pytest: a ``[tool.pytest]`` table in ``pyproject.toml``, the native
    one of pytest 9 or its ``[tool.pytest.ini_options]``; a ``pytest.toml``, ``.pytest.toml``, ``pytest.ini`` or
    ``.pytest.ini``; a ``[pytest]`` section in ``tox.ini``; a ``[tool:pytest]`` section in ``setup.cfg``; or a
    ``conftest.py`` at the root."""
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            table = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}).get("pytest")
        except (tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
            raise ProjectError(f"{pyproject} cannot be read: {error}") from error
        if isinstance(table, dict):
            return True
    if any((root / name).is_file() for name in PYTEST_FILES):
        return True
    for name, section in (("tox.ini", "pytest"), ("setup.cfg", "tool:pytest")):
        path = root / name
        if path.is_file() and re.search(rf"(?m)^[ \t]*\[{re.escape(section)}\][ \t]*$", path.read_text(encoding="utf-8", errors="replace")):
            return True
    return False


def no_tests(runner: str, code: int, reported_none: bool) -> bool:
    """Whether a run of ``runner`` with this exit code found no tests; ``reported_none`` says that unittest printed
    its report line of no tests."""
    if runner == "pytest":
        return code == PYTEST_NO_TESTS
    return code == UNITTEST_NO_TESTS or (code == 0 and reported_none)


def run_tests(command: list[str], root: Path) -> tuple[int, bool]:
    """Run the test runner and pass its output on; the exit code and whether unittest reported a run of no tests."""
    show(command)
    process = subprocess.Popen(command, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    assert process.stdout is not None
    reported_none = False
    for line in process.stdout:
        sys.stdout.write(line)
        reported_none = reported_none or bool(NO_TESTS_LINE.match(line))
    sys.stdout.flush()
    return process.wait(), reported_none


def error(message: str) -> None:
    print(f"::error title=Python tests::{message}", file=sys.stderr, flush=True)


def test(root: Path, name: str) -> int:
    python = sys.executable
    if pytest_configured(root):
        probe = subprocess.run([python, "-c", "import importlib.util, sys; sys.exit(importlib.util.find_spec('pytest') is None)"],
                               cwd=root, check=False)
        if probe.returncode != 0:
            error(f"pytest is not installed: {name} configures pytest, but the installed dependencies do not provide it; "
                  "list pytest in the requirements file or the extra that the input install names")
            return 1
        runner, command = "pytest", [python, "-m", "pytest"]
    else:
        runner, command = "unittest", [python, "-m", "unittest", "discover"]
    code, reported_none = run_tests(command, root)
    if no_tests(runner, code, reported_none):
        error(f"{runner} found no tests in {name}")
        return 1
    return 0 if code == 0 else 1


# --- the command line -----------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """The command line of this script; the part's tests parse the workflow's invocations with it."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    deciding = commands.add_parser("decide", help="decide whether the tests run for the change")
    deciding.add_argument("--root", type=Path, default=Path("."), help="the checkout")
    deciding.add_argument("--base", help="the base commit of a pull request, or the previous head of a push")
    deciding.add_argument("--head", help="the head commit")
    deciding.add_argument("--compare", choices=("merge-base", "direct"), default="direct", help="compare from the merge base or from the base itself")
    deciding.add_argument("--lookup", type=Path, help="the push lookup of a push or dispatch run, in place of --base and --head")
    deciding.add_argument("--github-output", type=Path, help="append the output run to this file")
    deciding.add_argument("--summary", type=Path, help="append the Markdown summary to this file")
    installing = commands.add_parser("install", help="install the project's dependencies with pip")
    installing.add_argument("--root", required=True, help="the project's directory")
    installing.add_argument("--install", default="", help="a requirements file ending in .txt, or an extra of the project; empty installs nothing")
    testing = commands.add_parser("test", help="run the project's tests with pytest or unittest")
    testing.add_argument("--root", required=True, help="the project's directory")
    return parser


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = build_parser()
    options = parser.parse_args(argv)
    if options.command == "decide":
        if options.lookup is not None and (options.base is not None or options.head is not None):
            parser.error("decide takes --lookup without --base and --head")
        if options.lookup is None and (options.base is None or options.head is None):
            parser.error("decide needs either --lookup or both --base and --head")
    return options


def main(argv: list[str] | None = None) -> int:
    options = parse_arguments(argv)
    try:
        if options.command == "decide":
            return decide(options.root.resolve(), options.base, options.head, options.compare, options.lookup, options.github_output, options.summary)
        root = Path(options.root)
        if not root.is_dir():
            error(f"the working directory {options.root} does not exist")
            return 2
        if options.command == "install":
            return install(root.resolve(), options.install)
        return test(root.resolve(), options.root)
    except ProjectError as problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
