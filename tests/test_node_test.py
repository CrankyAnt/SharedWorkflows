"""The script of the part node-test, started as a process with stand-ins for the ``node`` and ``npm`` commands: it checks
the project before Node is installed, checks the Node version against an exact one in ``.nvmrc``, installs the exact npm
of ``packageManager`` and checks its version, runs ``npm ci``, the build when the project defines one and ``npm test``
from the project's directory, and names the cause of every failure."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path

from tests import temporary_support

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "actions" / "node-test" / "node_test.py"
PROJECT = "web/shop"
BUNDLED = "10.9.4"
NODE = "v24.22.2"
HASH = "sha512.0a1b2c3d4e5f"

# The stand-in takes the name of the command it stands in for, node or npm, before the command's arguments. ``node
# --version`` logs ``node --version`` and prints STAND_IN_NODE. For npm, the stand-in appends its working directory, its
# arguments and the npm settings of its environment to the log.
# ``npm install --global npm@<version>`` writes the version to STAND_IN_STATE, and ``npm --version`` prints that version,
# or BUNDLED, the npm of the installed Node, before any installation; STAND_IN_REPORTED replaces what it prints. Every
# command ends with the exit code that the environment names for it: STAND_IN_INSTALL, STAND_IN_VERSION, STAND_IN_CI,
# STAND_IN_BUILD for ``npm run build`` and STAND_IN_TEST.
STAND_IN = """\
import os, sys

tool, arguments = sys.argv[1], sys.argv[2:]
with open(os.environ["STAND_IN_LOG"], "a", encoding="utf-8") as log:
    shown = ("node " if tool == "node" else "") + " ".join(arguments)
    log.write(os.getcwd() + "|" + shown + "|" + os.environ.get("npm_config_update_notifier", "") + " " + os.environ.get("npm_config_fund", "") + "\\n")
state = os.environ["STAND_IN_STATE"]
if tool == "node":
    print(os.environ["STAND_IN_NODE"])
    sys.exit(0)
if arguments == ["--version"]:
    name = "VERSION"
    installed = open(state, encoding="utf-8").read() if os.path.exists(state) else os.environ["STAND_IN_BUNDLED"]
    print(os.environ.get("STAND_IN_REPORTED", installed))
elif arguments[:2] == ["install", "--global"]:
    name = "INSTALL"
    if not int(os.environ.get("STAND_IN_INSTALL", "0")):
        with open(state, "w", encoding="utf-8") as stream:
            stream.write(arguments[2].removeprefix("npm@"))
elif arguments == ["run", "build"]:
    name = "BUILD"
else:
    name = arguments[0].upper()
if name != "VERSION":
    print("stand-in output of npm " + " ".join(arguments))
sys.exit(int(os.environ.get("STAND_IN_" + name, "0")))
"""


def manifest(**fields: object) -> dict:
    """A package.json with the scripts build and test, changed by ``fields``; a field set to None is left out."""
    document: dict = {"name": "shop", "version": "1.0.0", "scripts": {"build": "tsc", "test": "node --test"}, **fields}
    return {key: item for key, item in document.items() if item is not None}


class NodeTestFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = temporary_support.Directory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name).resolve()
        self.repository = self.directory / "consumer"
        self.project = self.repository / PROJECT
        self.project.mkdir(parents=True)
        self.write_project(manifest(packageManager="npm@11.6.2"))
        self.tools = self.directory / "tools"
        self.tools.mkdir()
        self.log = self.directory / "npm.log"
        self.state = self.directory / "npm-version"
        (self.tools / "stand_in.py").write_text(STAND_IN, encoding="utf-8")
        for tool in ("node", "npm"):
            if os.name == "nt":
                (self.tools / f"{tool}.cmd").write_text(f'@"{sys.executable}" "%~dp0stand_in.py" {tool} %*\n', encoding="utf-8")
            else:
                command = self.tools / tool
                command.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{self.tools}/stand_in.py" {tool} "$@"\n', encoding="utf-8")
                command.chmod(command.stat().st_mode | stat.S_IXUSR)

    def write_project(self, document: object, *, nvmrc: bool = True, node: str = "24") -> None:
        (self.project / "package.json").write_text(json.dumps(document) if not isinstance(document, str) else document, encoding="utf-8")
        if nvmrc:
            (self.project / ".nvmrc").write_text(f"{node}\n", encoding="utf-8")
        else:
            (self.project / ".nvmrc").unlink(missing_ok=True)

    def run_script(self, command: str = "test", working_directory: str = PROJECT, *, path: str | None = None, reported: str | None = None,
                   node: str = NODE, **exit_codes: int) -> subprocess.CompletedProcess[str]:
        environment = {**os.environ, "PATH": str(self.tools) + os.pathsep + os.environ["PATH"] if path is None else path,
                       "STAND_IN_LOG": str(self.log), "STAND_IN_STATE": str(self.state), "STAND_IN_BUNDLED": BUNDLED, "STAND_IN_NODE": node}
        for name in [name for name in environment if name.lower().startswith("npm_config_") or name.startswith("STAND_IN_REPORTED")]:
            environment.pop(name)
        if reported is not None:
            environment["STAND_IN_REPORTED"] = reported
        environment.update({f"STAND_IN_{name.upper()}": str(code) for name, code in exit_codes.items()})
        return subprocess.run([sys.executable, str(SCRIPT), command, "--root", str(self.repository), "--working-directory", working_directory],
                              capture_output=True, text=True, encoding="utf-8", env=environment, check=False)

    def commands(self) -> list[str]:
        """The arguments of each node and npm command, after checking that each ran from the project's directory with the script's settings."""
        lines = self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []
        for line in lines:
            directory, _, settings = line.split("|")
            self.assertEqual((str(self.project), "false false"), (directory, settings), line)
        return [line.split("|")[1] for line in lines]

    def assert_failed_before_any_command(self, completed: subprocess.CompletedProcess[str], message: str) -> None:
        self.assertEqual(2, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual([], self.commands())
        self.assertIn(f"::error title=node-test::{message}", completed.stderr)


class InstallBuildAndTestTests(NodeTestFixture):
    def test_it_installs_the_npm_of_package_manager_checks_its_version_and_then_runs_ci_build_and_test_in_that_order(self) -> None:
        completed = self.run_script()
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual(["node --version", "install --global npm@11.6.2", "--version", "ci", "run build", "test"], self.commands())
        self.assertIn("npm 11.6.2, the packageManager of web/shop/package.json", completed.stdout)
        for line in ("+ npm install --global npm@11.6.2", "+ npm ci", "+ npm run build", "+ npm test"):
            self.assertIn(line, completed.stdout)
        self.assertIn("stand-in output of npm test", completed.stdout, "the output of the commands reaches the job's log")

    def test_without_package_manager_it_keeps_the_npm_of_the_installed_node(self) -> None:
        self.write_project(manifest())
        completed = self.run_script()
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual(["node --version", "--version", "ci", "run build", "test"], self.commands())
        self.assertIn(f"npm {BUNDLED}, the npm of the installed Node", completed.stdout)

    def test_without_a_build_script_it_skips_the_build_and_still_installs_and_tests(self) -> None:
        self.write_project(manifest(packageManager="npm@11.6.2", scripts={"test": "node --test", "lint": "eslint ."}))
        completed = self.run_script()
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual(["node --version", "install --global npm@11.6.2", "--version", "ci", "test"], self.commands())
        self.assertNotIn("run build", completed.stdout)

    def test_a_project_at_the_repository_root_runs_from_the_root(self) -> None:
        self.project = self.repository
        self.write_project(manifest())
        completed = self.run_script(working_directory=".")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual(["node --version", "--version", "ci", "run build", "test"], self.commands())

    def test_it_prints_the_node_version_and_accepts_it_when_nvmrc_names_the_same_exact_version(self) -> None:
        for named in ("24.22.2", "v24.22.2", "24.22.2\n# comment"):
            with self.subTest(named=named):
                self.log.unlink(missing_ok=True)
                self.write_project(manifest(), node=named)
                completed = self.run_script()
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                self.assertIn(f"Node {NODE}", completed.stdout)

    def test_a_node_version_other_than_the_exact_one_of_nvmrc_fails_before_any_npm_command(self) -> None:
        for named, reported, shown in (("24.22.3", NODE, NODE), ("v24.22.3", NODE, NODE), ("24.22.2", "", "nothing")):
            with self.subTest(named=named, reported=reported):
                self.log.unlink(missing_ok=True)
                self.write_project(manifest(packageManager="npm@11.6.2"), node=named)
                completed = self.run_script(node=reported)
                self.assertEqual(1, completed.returncode, completed.stdout + completed.stderr)
                self.assertEqual(["node --version"], self.commands())
                self.assertIn(f"::error title=node-test::node --version reports {shown}, but the .nvmrc of {PROJECT} names {named.removeprefix('v')}", completed.stderr)

    def test_an_nvmrc_that_names_no_exact_version_is_not_compared(self) -> None:
        for named in ("24", "24.22", "lts/krypton", "lts/*", "node"):
            with self.subTest(named=named):
                self.log.unlink(missing_ok=True)
                self.write_project(manifest(), node=named)
                completed = self.run_script(node="v24.0.0")
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
                self.assertIn("Node v24.0.0", completed.stdout)

    def test_a_corepack_hash_after_the_npm_version_installs_that_version_and_is_not_verified(self) -> None:
        self.write_project(manifest(packageManager=f"npm@11.6.2+{HASH}"))
        completed = self.run_script()
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual(["node --version", "install --global npm@11.6.2", "--version", "ci", "run build", "test"], self.commands())
        self.assertIn(f"npm 11.6.2, the packageManager of {PROJECT}/package.json; its hash {HASH} is not verified", completed.stdout)

    def test_a_failing_test_fails_the_script(self) -> None:
        completed = self.run_script(test=1)
        self.assertEqual(1, completed.returncode)
        self.assertEqual(["node --version", "install --global npm@11.6.2", "--version", "ci", "run build", "test"], self.commands())
        self.assertIn(f"::error title=node-test::the tests of {PROJECT} failed", completed.stderr)

    def test_a_failed_build_fails_the_script_and_runs_no_test(self) -> None:
        completed = self.run_script(build=1)
        self.assertEqual(1, completed.returncode)
        self.assertEqual(["node --version", "install --global npm@11.6.2", "--version", "ci", "run build"], self.commands())
        self.assertIn(f"::error title=node-test::the script build of {PROJECT} failed", completed.stderr)

    def test_a_failed_npm_ci_fails_the_script_and_names_the_lockfile(self) -> None:
        completed = self.run_script(ci=1)
        self.assertEqual(1, completed.returncode)
        self.assertEqual(["node --version", "install --global npm@11.6.2", "--version", "ci"], self.commands())
        self.assertIn(f"::error title=node-test::npm ci failed in {PROJECT}; a missing package-lock.json, or one that does not match package.json, fails it too",
                      completed.stderr)

    def test_a_failed_installation_of_npm_fails_the_script_before_npm_ci(self) -> None:
        completed = self.run_script(install=1)
        self.assertEqual(1, completed.returncode)
        self.assertEqual(["node --version", "install --global npm@11.6.2"], self.commands())
        self.assertIn(f"::error title=node-test::the installation of npm 11.6.2, the packageManager of {PROJECT}/package.json, failed", completed.stderr)

    def test_an_npm_that_reports_another_version_after_the_installation_fails_the_script_before_npm_ci(self) -> None:
        for reported, shown in (("11.6.1", "11.6.1"), ("", "nothing")):
            with self.subTest(reported=reported):
                self.log.unlink(missing_ok=True)
                completed = self.run_script(reported=reported)
                self.assertEqual(1, completed.returncode)
                self.assertEqual(["node --version", "install --global npm@11.6.2", "--version"], self.commands())
                self.assertIn(f"::error title=node-test::npm --version reports {shown} after the installation of npm 11.6.2", completed.stderr)

    def test_an_npm_version_that_fails_counts_as_no_version(self) -> None:
        completed = self.run_script(version=1)
        self.assertEqual(1, completed.returncode)
        self.assertIn("npm --version reports nothing after the installation of npm 11.6.2", completed.stderr)

    def test_a_job_without_node_or_npm_fails_with_a_message_that_says_so(self) -> None:
        for missing, kept in (("node", "npm"), ("npm", "node")):
            with self.subTest(missing=missing):
                only = self.directory / f"only-{kept}"
                only.mkdir()
                for path in self.tools.iterdir():
                    if path.stem in (kept, "stand_in"):
                        (only / path.name).write_bytes(path.read_bytes())
                        (only / path.name).chmod(path.stat().st_mode)
                completed = self.run_script(path=str(only))
                self.assertEqual(2, completed.returncode)
                self.assertIn(f"::error title=node-test::{missing} is not on PATH", completed.stderr)
                self.assertEqual([], self.commands())


class CheckTests(NodeTestFixture):
    def test_check_runs_no_command_and_needs_no_npm(self) -> None:
        empty = self.directory / "empty"
        empty.mkdir()
        completed = self.run_script("check", path=str(empty))
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual([], self.commands())
        self.assertIn(f"{PROJECT} is a Node project that node-test can run with npm 11.6.2.", completed.stdout)

    def test_check_names_no_npm_version_without_package_manager(self) -> None:
        self.write_project(manifest())
        completed = self.run_script("check")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertIn(f"{PROJECT} is a Node project that node-test can run.", completed.stdout)


class ProjectErrorTests(NodeTestFixture):
    """Each project that the job cannot run fails both commands before any npm command, with a message that names the cause."""

    def assert_both_commands_fail(self, message: str, working_directory: str = PROJECT) -> None:
        for command in ("check", "test"):
            with self.subTest(command=command):
                self.log.unlink(missing_ok=True)
                self.assert_failed_before_any_command(self.run_script(command, working_directory), message)

    def test_a_directory_without_package_json_fails(self) -> None:
        (self.project / "package.json").unlink()
        self.assert_both_commands_fail(f"the working directory {PROJECT} holds no package.json")

    def test_a_directory_without_nvmrc_fails(self) -> None:
        self.write_project(manifest(), nvmrc=False)
        self.assert_both_commands_fail(f"the working directory {PROJECT} holds no .nvmrc, the file that names the Node version the job installs")

    def test_a_package_manager_other_than_npm_fails(self) -> None:
        for manager, name in (("pnpm@9.12.0", "pnpm"), ("yarn@4.5.1", "yarn"), ("bun@1.1.30", "bun"), ("@scope/x@1", "@scope/x"),
                              ("@yarnpkg/cli@4.5.1+sha512.ab12", "@yarnpkg/cli")):
            with self.subTest(manager=manager):
                self.write_project(manifest(packageManager=manager))
                self.assert_both_commands_fail(f"the packageManager {manager} of {PROJECT}/package.json names the package manager {name}; node-test runs npm only")

    def test_a_package_manager_without_an_exact_npm_version_fails(self) -> None:
        for manager in ("npm", "npm@", "npm@11", "npm@11.6", "npm@latest", "npm@^11.6.2", "npm@11.6.x", "npm@11.6.2-beta.1", "npm@11.6.2+sha512", "npm@11.6.2+sha512.xyz", "npm@11.6.2+md5.0a1b",
                        "npm@11.6.2+sha512.0a1b+sha512.0a1b", "@11.6.2", 11, None):
            with self.subTest(manager=manager):
                document = manifest(packageManager=manager) if manager is not None else {**manifest(), "packageManager": None}
                self.write_project(document)
                self.assert_both_commands_fail(f"the packageManager {json.dumps(manager)} of {PROJECT}/package.json names no exact npm version; "
                                               "state npm@<major>.<minor>.<patch>, or no packageManager for the npm of the installed Node")

    def test_a_package_json_without_a_test_script_fails(self) -> None:
        for scripts in ({"build": "tsc"}, {}, None, ["test"]):
            with self.subTest(scripts=scripts):
                document = manifest(scripts=scripts) if scripts is not None else {"name": "shop"}
                self.write_project(document)
                self.assert_both_commands_fail(f"the package.json of {PROJECT} defines no script test")

    def test_a_package_json_that_cannot_be_read_fails(self) -> None:
        self.write_project("{ not JSON")
        self.assert_both_commands_fail(f"the package.json of {PROJECT} cannot be read: ")
        self.write_project(["not", "an", "object"])
        self.assert_both_commands_fail(f"the package.json of {PROJECT} holds no JSON object")

    def test_a_working_directory_that_is_no_directory_of_the_repository_fails(self) -> None:
        (self.directory / "outside").mkdir()
        (self.directory / "outside" / "package.json").write_text(json.dumps(manifest()), encoding="utf-8")
        (self.directory / "outside" / ".nvmrc").write_text("24\n", encoding="utf-8")
        for working_directory in ("web/missing", f"{PROJECT}/package.json", "../outside"):
            with self.subTest(working_directory=working_directory):
                self.assert_both_commands_fail(f"the working directory {working_directory} is no directory of the repository", working_directory)


if __name__ == "__main__":
    unittest.main()
