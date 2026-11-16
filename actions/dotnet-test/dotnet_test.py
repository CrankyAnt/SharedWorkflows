#!/usr/bin/env python3
"""Build a .NET test project or solution and run its tests, or its console test program, in the job that installed
the .NET SDK.

    python3 dotnet_test.py --project PATH [--console-runner] [--root DIR]

``--project`` is the test project or the solution, as a path relative to ``--root``, the repository root by default.
The script runs ``dotnet build <project>`` and, after a build that passed, ``dotnet test <project> --no-build`` with
the TRX logger, both from the root, with the ``dotnet`` on PATH, without its logo and without telemetry. The TRX
reports go to a directory of their own under ``RUNNER_TEMP``, or the system's temporary directory without it, which the
script removes afterwards. A run of the tests that passed fails all the same when its reports count no executed test:
the project holds no test, is no test project, or its test runner writes no TRX report for ``--logger trx``.

With ``--console-runner`` the project is a console test program, a project without a test framework whose exit code is
the result of its tests. After a build that passed, the script runs ``dotnet run --project <project> --no-build`` in
place of ``dotnet test``, asks for no report and makes no check that a test ran.

The exit code is 0 when both commands passed and, without ``--console-runner``, a test ran, 1 when the build, the tests
or the console test program failed or no test ran, and 2 when the project is no file under the root or ``dotnet`` is
not on PATH.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from pathlib import Path

ENVIRONMENT = {"DOTNET_NOLOGO": "1", "DOTNET_CLI_TELEMETRY_OPTOUT": "1"}


def error(message: str) -> None:
    print(f"::error title=dotnet-test::{message}", file=sys.stderr, flush=True)


def run(dotnet: str, arguments: list[str], root: Path) -> int:
    print("+ dotnet " + " ".join(arguments), flush=True)
    return subprocess.run([dotnet, *arguments], cwd=root, env={**os.environ, **ENVIRONMENT}, check=False).returncode


def executed_tests(results: Path) -> int:
    """The executed tests that the TRX reports under ``results`` count together; a report that cannot be read counts none."""
    executed = 0
    for report in sorted(results.rglob("*.trx")):
        try:
            counters = [element for element in ElementTree.parse(report).iter() if element.tag.rsplit("}", 1)[-1] == "Counters"]
            executed += sum(int(element.get("executed", "0")) for element in counters)
        except (ElementTree.ParseError, ValueError, OSError) as problem:
            print(f"The test report {report.name} cannot be read: {problem}", flush=True)
    return executed


def run_console_program(dotnet: str, root: Path, project: str) -> int:
    """Run the built console test program; its exit code is the result of its tests."""
    code = run(dotnet, ["run", "--project", project, "--no-build"], root)
    if code != 0:
        error(f"the console test program {project} failed: it ended with exit code {code}")
        return 1
    print("The console test program passed.", flush=True)
    return 0


def build_and_test(root: Path, project: str, console_runner: bool = False) -> int:
    path = (root / project).resolve()
    if not path.is_file() or not path.is_relative_to(root):
        error(f"the project {project} is no file of the repository")
        return 2
    dotnet = shutil.which("dotnet")
    if dotnet is None:
        error("dotnet is not on PATH")
        return 2
    if run(dotnet, ["build", project], root) != 0:
        error(f"the build of {project} failed")
        return 1
    if console_runner:
        return run_console_program(dotnet, root, project)
    results = Path(tempfile.mkdtemp(prefix="dotnet-test-results-", dir=os.environ.get("RUNNER_TEMP") or None))
    try:
        if run(dotnet, ["test", project, "--no-build", "--logger", "trx", "--results-directory", str(results)], root) != 0:
            error(f"the tests of {project} failed")
            return 1
        executed = executed_tests(results)
    finally:
        shutil.rmtree(results, ignore_errors=True)
    if executed == 0:
        error(f"no test ran in {project}: dotnet test passed, but its TRX reports count no executed test; "
              "check that the configured project is a test project or a solution with one, and that it holds a test")
        return 1
    print(f"{executed} test{'' if executed == 1 else 's'} ran.", flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The command line of this script; the part's tests parse the step's invocation with it."""
    parser = argparse.ArgumentParser(description="Build a .NET test project or solution and run its tests, or its console test program.")
    parser.add_argument("--project", required=True, help="the test project or the solution, relative to --root")
    parser.add_argument("--console-runner", action="store_true",
                        help="the project is a console test program: run it with dotnet run, and take its exit code as the result")
    parser.add_argument("--root", type=Path, default=Path("."), help="the repository's checkout")
    return parser


def main(argv: list[str] | None = None) -> int:
    options = build_parser().parse_args(argv)
    return build_and_test(options.root.resolve(), options.project, options.console_runner)


if __name__ == "__main__":
    sys.exit(main())
