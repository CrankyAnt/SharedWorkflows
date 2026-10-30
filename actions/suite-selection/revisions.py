"""The Git calls that the selection and the reuse of passed results share: running Git in a checkout and naming a commit."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ALL_ZERO = re.compile(r"^0{40}$|^0{64}$")


def git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Git with the arguments in the checkout at root, its output captured as text and its exit code left to the caller."""
    return subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True, encoding="utf-8", check=False)


def commit_id(root: Path, revision: str) -> str | None:
    """The full id of the commit a revision names, or None when the checkout lacks it."""
    found = git(root, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
    return found.stdout.strip() if found.returncode == 0 else None
