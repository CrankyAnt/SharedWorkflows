#!/usr/bin/env python3
"""Write the standard-library module names of the shared CI: the union of ``sys.stdlib_module_names`` over the given
interpreters, one of each Python version that the shared CI supports, with those versions, to
``stdlib_module_names.json`` beside this script or to ``--output``. ``docs/maintaining.md`` says when to renew it.

    python3 stdlib_names.py [--output <file>] <interpreter> ...
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

NAMES = Path(__file__).resolve().parent / "stdlib_module_names.json"
QUERY = "import json, sys; print(json.dumps(['%d.%d' % sys.version_info[:2], sorted(sys.stdlib_module_names)]))"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=NAMES, help="the file to write (default: stdlib_module_names.json beside this script)")
    parser.add_argument("interpreters", nargs="+", help="one Python interpreter of each supported version")
    options = parser.parse_args(argv)
    versions: set[str] = set()
    names: set[str] = set()
    for interpreter in options.interpreters:
        version, found = json.loads(subprocess.run([interpreter, "-I", "-c", QUERY], capture_output=True, text=True, check=True).stdout)
        versions.add(version)
        names.update(found)
    ordered = sorted(versions, key=lambda version: tuple(int(part) for part in version.split(".")))
    options.output.write_text(json.dumps({"versions": ordered, "names": sorted(names)}, indent=2) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
