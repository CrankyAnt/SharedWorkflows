#!/usr/bin/env python3
"""Install dependencies, compute the pip cache key and run the tests of Python tests; the pipeline calls one subcommand per step.

    python3 python_tests.py install   --root DIR [--install VALUE]
    python3 python_tests.py cache-key --root DIR [--install VALUE] [--github-output FILE]
    python3 python_tests.py test      --root DIR

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
import hashlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path

# An extra name as the core metadata specification normalises it, and as pip accepts it between the brackets.
EXTRA_NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
# The report line of a unittest run without tests; before Python 3.12 such a run ends with 0.
NO_TESTS_LINE = re.compile(r"Ran 0 tests in ")
# The files whose presence at the root configures pytest, whatever they hold.
PYTEST_FILES = ("pytest.toml", ".pytest.toml", "pytest.ini", ".pytest.ini", "conftest.py")
PYTEST_NO_TESTS = 5  # pytest's exit code when it collected no tests
UNITTEST_NO_TESTS = 5  # unittest's exit code, from Python 3.12, when no test ran


class ProjectError(Exception):
    """A file of the project cannot be read."""


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


LOCK_AND_MANIFEST_FILES = ("pyproject.toml", "setup.py", "setup.cfg", "uv.lock", "poetry.lock", "pdm.lock", "Pipfile.lock")


def cache_key(root: Path, value: str) -> str:
    """Deterministic pip cache key for ``root`` and ``value``, hashed over Python minor version and dependency/lock files."""
    files: set[Path] = set()
    for name in LOCK_AND_MANIFEST_FILES:
        candidate = root / name
        if candidate.is_file():
            files.add(candidate.resolve())
    for candidate in root.glob("requirements*.txt"):
        if candidate.is_file():
            files.add(candidate.resolve())
    if value.endswith(".txt"):
        candidate = (root / value).resolve()
        if candidate.is_file() and candidate.is_relative_to(root):
            files.add(candidate)
    hasher = hashlib.sha256()
    py_minor = f"{sys.version_info.major}.{sys.version_info.minor}"
    hasher.update(f"python={py_minor}\ninstall={value}\n".encode("utf-8"))
    for path in sorted(files):
        rel = path.relative_to(root).as_posix()
        hasher.update(f"file={rel}\n".encode("utf-8"))
        hasher.update(path.read_bytes())
        hasher.update(b"\n")
    return f"pip-py{py_minor}-{hasher.hexdigest()[:16]}"


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
    installing = commands.add_parser("install", help="install the project's dependencies with pip")
    installing.add_argument("--root", required=True, help="the project's directory")
    installing.add_argument("--install", default="", help="a requirements file ending in .txt, or an extra of the project; empty installs nothing")
    keying = commands.add_parser("cache-key", help="print the dependency cache key for the project")
    keying.add_argument("--root", required=True, help="the project's directory")
    keying.add_argument("--install", default="", help="a requirements file ending in .txt, or an extra of the project; empty installs nothing")
    keying.add_argument("--github-output", type=Path, help="append the output key to this file")
    testing = commands.add_parser("test", help="run the project's tests with pytest or unittest")
    testing.add_argument("--root", required=True, help="the project's directory")
    return parser


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = parse_arguments(argv)
    try:
        root = Path(options.root)
        if not root.is_dir():
            error(f"the working directory {options.root} does not exist")
            return 2
        if options.command == "install":
            return install(root.resolve(), options.install)
        if options.command == "cache-key":
            key = cache_key(root.resolve(), options.install)
            print(key)
            if options.github_output is not None:
                with options.github_output.open("a", encoding="utf-8") as handle:
                    handle.write(f"key={key}\n")
            return 0
        return test(root.resolve(), options.root)
    except ProjectError as problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
