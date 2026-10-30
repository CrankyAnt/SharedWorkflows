from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from tests.hygiene_fixtures import REPOSITORY_ROOT, tools_directory

RESOURCES = REPOSITORY_ROOT / "actions" / "repository-hygiene"


def hygiene(check: str, root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(RESOURCES / "hygiene.py"), check, "--root", str(root), *arguments]
    directory = tools_directory()
    if check not in ("coverage", "changes", "links", "record") and directory is not None:  # the subcommands that run no tool take no --tools
        command += ["--tools", str(directory)]
    return subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
