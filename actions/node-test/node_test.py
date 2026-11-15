#!/usr/bin/env python3
"""Check a Node project, and install its npm version and run its install, build and test, in the job of the part node-test.

    python3 node_test.py check --working-directory DIR [--root ROOT]
    python3 node_test.py test --working-directory DIR [--root ROOT]

``--working-directory`` is the project's directory, relative to ``--root``, the repository root by default.

``check`` runs before the job installs Node and runs no command. It fails when the directory is no directory of the
repository, holds no ``package.json`` or no ``.nvmrc``, when ``package.json`` cannot be read, when its ``packageManager``
names another package manager than npm or no exact version ``npm@<major>.<minor>.<patch>``, which may carry a Corepack
hash such as ``+sha512.<hex>``, or when it defines no script ``test``.

``test`` runs after the job installed the Node version of ``.nvmrc``. It makes the same checks, and then, from the
project's directory: it prints ``node --version`` and fails when ``.nvmrc`` names an exact version
``[v]<major>.<minor>.<patch>`` and Node reports another. For ``packageManager: npm@<version>`` it installs exactly that npm
with ``npm install --global npm@<version>``, without verifying a Corepack hash, and fails unless ``npm --version`` then
reports that version; without ``packageManager`` it keeps the npm of the installed Node. It then runs ``npm ci``,
``npm run build`` when ``package.json`` defines the script ``build``, and ``npm test``, and stops at the first command
that fails.

The exit code is 0 when the checks and every command passed, 1 when a command failed or Node or npm reports another
version than the expected one, and 2 when a check failed or ``node`` or ``npm`` is not on PATH.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ENVIRONMENT = {"npm_config_update_notifier": "false", "npm_config_fund": "false"}
# The name of a package manager, which may be a scoped package (@scope/name), and what follows its last part's @.
PACKAGE_MANAGER = re.compile(r"(?P<name>@?[^@]+)@(?P<version>.*)")
EXACT_NPM_VERSION = re.compile(r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)(?:\+(?P<hash>sha(?:1|224|256|384|512)\.[0-9a-fA-F]+))?")
EXACT_NODE_VERSION = re.compile(r"v?(?P<version>[0-9]+\.[0-9]+\.[0-9]+)")


class ProjectError(Exception):
    """A project that the job cannot run, with the message that names the cause."""


@dataclass(frozen=True)
class Project:
    directory: Path
    node_version: str | None
    npm_version: str | None
    npm_hash: str | None
    build: bool


def error(message: str) -> None:
    print(f"::error title=node-test::{message}", file=sys.stderr, flush=True)


def read_project(root: Path, working_directory: str) -> Project:
    """The project in ``working_directory`` under ``root``, after the checks that need no command."""
    directory = (root / working_directory).resolve()
    if not directory.is_dir() or not directory.is_relative_to(root):
        raise ProjectError(f"the working directory {working_directory} is no directory of the repository")
    manifest_path = directory / "package.json"
    if not manifest_path.is_file():
        raise ProjectError(f"the working directory {working_directory} holds no package.json")
    nvmrc = directory / ".nvmrc"
    if not nvmrc.is_file():
        raise ProjectError(f"the working directory {working_directory} holds no .nvmrc, the file that names the Node version the job installs")
    try:
        named_node = (nvmrc.read_text(encoding="utf-8").strip().splitlines() or [""])[0].strip()
    except (ValueError, OSError) as problem:
        raise ProjectError(f"the .nvmrc of {working_directory} cannot be read: {problem}") from problem
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as problem:
        raise ProjectError(f"the package.json of {working_directory} cannot be read: {problem}") from problem
    if not isinstance(manifest, dict):
        raise ProjectError(f"the package.json of {working_directory} holds no JSON object")
    node = EXACT_NODE_VERSION.fullmatch(named_node)
    npm_version = npm_hash = None
    if "packageManager" in manifest:
        manager = manifest["packageManager"]
        match = PACKAGE_MANAGER.fullmatch(manager) if isinstance(manager, str) else None
        if match is not None and match["name"] != "npm":
            raise ProjectError(f"the packageManager {manager} of {working_directory}/package.json names the package manager {match['name']}; "
                               "node-test runs npm only")
        exact = EXACT_NPM_VERSION.fullmatch(match["version"]) if match is not None else None
        if exact is None:
            raise ProjectError(f"the packageManager {json.dumps(manager)} of {working_directory}/package.json names no exact npm version; "
                               "state npm@<major>.<minor>.<patch>, or no packageManager for the npm of the installed Node")
        npm_version, npm_hash = exact["version"], exact["hash"]
    scripts = manifest.get("scripts", {})
    if not isinstance(scripts, dict) or "test" not in scripts:
        raise ProjectError(f"the package.json of {working_directory} defines no script test")
    return Project(directory, node["version"] if node else None, npm_version, npm_hash, "build" in scripts)


def command(tool: str, arguments: list[str], directory: Path, *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    """One command of ``tool`` from ``directory`` with the npm settings of this script."""
    return subprocess.run([tool, *arguments], cwd=directory, env={**os.environ, **ENVIRONMENT}, capture_output=capture, text=True, check=False)


def run(npm: str, arguments: list[str], directory: Path) -> int:
    print("+ npm " + " ".join(arguments), flush=True)
    return command(npm, arguments, directory).returncode


def reported_version(tool: str, directory: Path) -> str:
    """What ``<tool> --version`` prints, or the empty string when it fails."""
    completed = command(tool, ["--version"], directory, capture=True)
    return completed.stdout.strip() if completed.returncode == 0 else ""


def install_build_and_test(project: Project, working_directory: str) -> int:
    node = shutil.which("node")
    npm = shutil.which("npm")
    if node is None or npm is None:
        error(f"{'node' if node is None else 'npm'} is not on PATH")
        return 2
    reported_node = reported_version(node, project.directory)
    print(f"Node {reported_node or '(unknown version)'}", flush=True)
    if project.node_version is not None and reported_node.removeprefix("v") != project.node_version:
        error(f"node --version reports {reported_node or 'nothing'}, but the .nvmrc of {working_directory} names {project.node_version}")
        return 1
    if project.npm_version is not None:
        if run(npm, ["install", "--global", f"npm@{project.npm_version}"], project.directory) != 0:
            error(f"the installation of npm {project.npm_version}, the packageManager of {working_directory}/package.json, failed")
            return 1
        npm = shutil.which("npm") or npm
        reported = reported_version(npm, project.directory)
        if reported != project.npm_version:
            error(f"npm --version reports {reported or 'nothing'} after the installation of npm {project.npm_version}")
            return 1
        unverified = "" if project.npm_hash is None else f"; its hash {project.npm_hash} is not verified"
        print(f"npm {reported}, the packageManager of {working_directory}/package.json{unverified}", flush=True)
    else:
        print(f"npm {reported_version(npm, project.directory) or '(unknown version)'}, the npm of the installed Node", flush=True)
    if run(npm, ["ci"], project.directory) != 0:
        error(f"npm ci failed in {working_directory}; a missing package-lock.json, or one that does not match package.json, fails it too")
        return 1
    if project.build and run(npm, ["run", "build"], project.directory) != 0:
        error(f"the script build of {working_directory} failed")
        return 1
    if run(npm, ["test"], project.directory) != 0:
        error(f"the tests of {working_directory} failed")
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The command line of this script; the part's tests parse the step's invocations with it."""
    parser = argparse.ArgumentParser(description="Check a Node project, or install its npm and run its install, build and test.")
    parser.add_argument("command", choices=["check", "test"], help="check before the job installs Node; test after it")
    parser.add_argument("--working-directory", required=True, help="the project's directory, relative to --root")
    parser.add_argument("--root", type=Path, default=Path("."), help="the repository's checkout")
    return parser


def main(argv: list[str] | None = None) -> int:
    options = build_parser().parse_args(argv)
    try:
        project = read_project(options.root.resolve(), options.working_directory)
    except ProjectError as problem:
        error(str(problem))
        return 2
    if options.command == "check":
        print(f"{options.working_directory} is a Node project that node-test can run"
              f"{'' if project.npm_version is None else f' with npm {project.npm_version}'}.", flush=True)
        return 0
    return install_build_and_test(project, options.working_directory)


if __name__ == "__main__":
    sys.exit(main())
