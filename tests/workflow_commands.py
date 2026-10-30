"""The argument lists a workflow hands to a script of the shared CI, for the part's tests to parse with the script's own
parser: a workflow that invokes a script with an option its parser does not accept fails the part's suite, not
the next live run."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

# Shell variables with a value the parsers constrain; every other variable becomes a plain placeholder.
PLACEHOLDERS = {"COMPARE": "direct", "COOLDOWN_DAYS": "7", "PR_NUMBER": "1", "RUN_ATTEMPT": "1", "RUN_ID": "1", "tools": "--tool gitleaks"}
VARIABLE = re.compile(r'"\$\{?([A-Za-z_]+)\}?"|\$\{?([A-Za-z_]+)\}?')


def script_invocations(workflow: Path, script: str) -> list[list[str]]:
    """Every argument list the run lines of ``workflow`` hand to ``script`` (a file name such as ``hygiene.py``), in
    workflow order, with shell variables replaced by placeholders."""
    pattern = re.compile(rf'(?:"\$RESOURCE_PATH/|actions/[a-z-]+/){re.escape(script)}"?\s+(.*)$')
    invocations = []
    for line in workflow.read_text(encoding="utf-8").splitlines():
        match = pattern.search(line)
        if match is None:
            continue
        rest = VARIABLE.sub(lambda found: PLACEHOLDERS.get(found.group(1) or found.group(2), "x"), match.group(1))
        invocations.append(shlex.split(rest))
    return invocations
