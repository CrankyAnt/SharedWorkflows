#!/usr/bin/env python3
"""Build the ETW consumer from its committed source, or check that the committed build belongs to that source.

``build`` publishes the consumer with the SDK that ``etw-consumer/global.json`` pins, from the locked packages, into
``etw-consumer/build``. ``check`` publishes it the same way into a temporary directory and compares it file by file and
byte by byte with the committed build; it fails and names every file that differs, is missing or is extra. Both build
without build servers and node reuse, so that the build runs inside the recorded process tree. The build is
byte-reproducible on Windows only, so both commands run there.

    python etw_consumer_build.py build
    python etw_consumer_build.py check
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parent / "etw-consumer"
BUILD = SOURCE / "build"
PROJECT = SOURCE / "EtwConsumer.csproj"


def publish(target: Path, work: Path) -> None:
    command = ["dotnet", "publish", str(PROJECT), "--configuration", "Release", "--artifacts-path", str(work), "--output", str(target),
               "--nologo", "-p:RestoreLockedMode=true", "-p:TreatWarningsAsErrors=true",
               "--disable-build-servers", "-nodeReuse:false", "-p:UseSharedCompilation=false"]
    completed = subprocess.run(command, cwd=SOURCE, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"dotnet publish failed with exit code {completed.returncode}")


def files(directory: Path) -> set[str]:
    return {path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()}


def differences(committed: Path, rebuilt: Path) -> list[str]:
    old, new = files(committed), files(rebuilt)
    problems = [f"missing from the committed build: {name}" for name in sorted(new - old)]
    problems += [f"not produced by the source: {name}" for name in sorted(old - new)]
    problems += [f"differs from the rebuild: {name}" for name in sorted(old & new)
                 if not filecmp.cmp(committed / name, rebuilt / name, shallow=False)]
    return problems


def run_build() -> int:
    with tempfile.TemporaryDirectory(prefix="etw-consumer-") as work:
        rebuilt = Path(work) / "build"
        publish(rebuilt, Path(work) / "artifacts")
        shutil.rmtree(BUILD, ignore_errors=True)
        shutil.copytree(rebuilt, BUILD)
    print(f"Built the ETW consumer into {BUILD}.")
    return 0


def run_check() -> int:
    with tempfile.TemporaryDirectory(prefix="etw-consumer-") as work:
        rebuilt = Path(work) / "build"
        publish(rebuilt, Path(work) / "artifacts")
        problems = differences(BUILD, rebuilt)
    if problems:
        print("The committed ETW consumer build does not belong to its source. Run "
              "`python actions/recording/etw_consumer_build.py build` on Windows and commit etw-consumer/build:")
        for problem in problems:
            print(f"  {problem}")
        return 1
    print(f"The committed ETW consumer build equals a rebuild of its source ({len(files(BUILD))} files).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("build", help="publish the consumer into etw-consumer/build")
    commands.add_parser("check", help="compare the committed build with a rebuild of the source")
    options = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if shutil.which("dotnet") is None:
        print("dotnet is not on PATH", file=sys.stderr)
        return 2
    return run_build() if options.command == "build" else run_check()


if __name__ == "__main__":
    raise SystemExit(main())
