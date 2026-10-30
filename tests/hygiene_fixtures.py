from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

GIT_IDENTITY = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false"]


def tools_directory() -> Path | None:
    """The directory with the pinned tools, or None when the test should look on PATH."""
    configured = os.environ.get("HYGIENE_TOOLS")
    return Path(configured) if configured else None


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", *GIT_IDENTITY, "-C", str(repo), *arguments], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {result.stderr}")
    return result.stdout.strip()


def fixture_repository(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "feat: fixture")
    return root


def tools_available(*names: str) -> bool:
    directory = tools_directory()
    if directory is not None:
        return all((directory / name).is_file() or (directory / f"{name}.exe").is_file() for name in names)
    return all(shutil.which(name) for name in names)
