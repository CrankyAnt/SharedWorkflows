#!/usr/bin/env python3
"""Run the shared Python language step in the consumer's job that installs Python.

    python3 python_language_step.py [--root DIR] [--tools DIR [--install PINS]] [--format-check]

``ruff check`` runs with the consumer's ruff configuration, or ruff's default rules without one. ``mypy`` runs only when
the consumer configures it at the root: a ``mypy.ini`` or ``.mypy.ini``, a ``[tool.mypy]`` table in ``pyproject.toml`` or
a ``[mypy]`` section in ``setup.cfg``; it checks the configured ``files``, or the root without them. ``--format-check``
adds ``ruff format --check``. ``--tools`` names the directory that holds the pinned tools; without it the tools come
from PATH. ``--install`` first installs the tools the step needs there from the pins file, with the interpreter that runs
the step. Every check runs; the exit code is 1 when one fails and 2 when a tool is missing or cannot be installed.
"""

from __future__ import annotations

import argparse
import configparser
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository-hygiene"))
import install_tools  # noqa: E402
from pins import PinsError, load_pins  # noqa: E402

# The files mypy reads its configuration from, in mypy's order of precedence.
MYPY_CONFIGURATIONS = ("mypy.ini", ".mypy.ini", "pyproject.toml", "setup.cfg")


class ToolMissing(RuntimeError):
    """A required executable is not where the tools directory or PATH should hold it."""


def executable(name: str, tools: Path | None) -> Path:
    if tools is not None:
        for candidate in (tools / name, tools / f"{name}.exe"):
            if candidate.is_file():
                return candidate
        raise ToolMissing(f"{name} is missing from {tools}")
    found = shutil.which(name)
    if not found:
        raise ToolMissing(f"{name} is not on PATH")
    return Path(found)


def mypy_configuration(root: Path) -> dict | None:
    """The consumer's mypy options at the root, or None when the consumer does not configure mypy there."""
    for name in MYPY_CONFIGURATIONS:
        path = root / name
        if not path.is_file():
            continue
        if name == "pyproject.toml":
            table = tomllib.loads(path.read_text(encoding="utf-8")).get("tool", {}).get("mypy")
            if isinstance(table, dict):
                return table
            continue
        parser = configparser.ConfigParser(interpolation=None)
        parser.read(path, encoding="utf-8")
        if parser.has_section("mypy"):
            return dict(parser["mypy"])
        if name != "setup.cfg":
            return {}
    return None


def run(command: list[str], root: Path) -> int:
    print("+ " + " ".join(command), flush=True)
    return subprocess.run(command, cwd=root, check=False).returncode


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the shared Python language step.")
    parser.add_argument("--root", type=Path, default=Path("."), help="the consumer's repository root")
    parser.add_argument("--tools", type=Path, help="the directory with the pinned tools; PATH without it")
    parser.add_argument("--install", type=Path, metavar="PINS", help="install the pinned tools the step needs into --tools from this pins file first")
    parser.add_argument("--format-check", action="store_true", help="also run ruff format --check")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(argv)
    if options.install is not None and options.tools is None:
        parser.error("--install needs --tools")
    root = options.root.resolve()
    mypy_options = mypy_configuration(root)
    if options.install is not None:
        try:
            install_tools.install(load_pins(options.install), "windows-x64" if os.name == "nt" else "linux-x64", options.tools,
                                  tools=["ruff"] if mypy_options is None else ["ruff", "mypy"])
        except (PinsError, install_tools.InstallError) as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
    try:
        ruff = executable("ruff", options.tools)
        mypy = executable("mypy", options.tools) if mypy_options is not None else None
    except ToolMissing as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    commands = [[str(ruff), "check", "--no-cache", "."]]
    if options.format_check:
        commands.append([str(ruff), "format", "--check", "--no-cache", "."])
    if mypy is not None:
        # The consumer's interpreter, so that mypy finds the packages the job installed; its files list, or the root.
        commands.append([str(mypy), "--python-executable", sys.executable, *([] if mypy_options.get("files") else ["."])])
    failed = [command for command in commands if run(command, root) != 0]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
