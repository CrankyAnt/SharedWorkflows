"""Fixture repositories and data for selection tests."""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

GIT_IDENTITY = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false"]


def write(root: Path, relative: str, text: str = "") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(["git", *GIT_IDENTITY, "-C", str(repo), *arguments], capture_output=True, text=True, check=True)
    return completed.stdout.strip()


def force_add(repo: Path, files: dict[str, str]) -> None:
    """Write the files and stage each with ``git add -f``, also when the ignore rules match it."""
    for path, text in files.items():
        write(repo, path, text)
        git(repo, "add", "-f", path)


def fixture_repository(root: Path) -> None:
    """Sources under src/, checks under checks/, scripts under scripts/, data in several top-level folders."""
    write(root, "src/app/__init__.py")
    write(root, "src/app/paths.py", """\
        from pathlib import Path

        BASE = Path(__file__).resolve().parents[2]
        DATA = BASE / "data" / "table.csv"


        def listing():
            return sorted((BASE / "guides").iterdir())
        """)
    write(root, "checks/test_app.py", """\
        import importlib.util
        import subprocess
        import sys
        from pathlib import Path

        ROOT = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(ROOT / "src"))
        from app.paths import DATA, listing

        SETTINGS = ROOT / "config" / "settings.toml"


        def assets():
            for name in ("a.txt", "b.txt"):
                yield (ROOT / "assets" / name).read_text()


        def load():
            spec = importlib.util.spec_from_file_location("loader", ROOT / "scripts" / "loader.py")
            return spec


        def run():
            subprocess.run([sys.executable, str(ROOT / "scripts" / "run.py")], check=True)
            subprocess.run(["pwsh", "-NoProfile", "-File", str(ROOT / "scripts" / "helper.ps1")], check=True)
            text = DATA.read_text() + SETTINGS.read_text()
            return text, listing()
        """)
    write(root, "scripts/loader.py", """\
        from pathlib import Path

        HERE = Path(__file__).resolve().parent
        RULES = (HERE / "loader.json").read_text()
        """)
    write(root, "scripts/run.py", """\
        import shutil
        import sys
        from pathlib import Path

        ROOT = Path(__file__).resolve().parents[1]
        shutil.copytree(ROOT / "templates", Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "out")
        """)
    write(root, "scripts/helper.ps1", """\
        $repo = Split-Path -Parent $PSScriptRoot
        Get-Content -LiteralPath (Join-Path $repo 'config\\helper.ini')
        """)
    write(root, "checks/suite.ps1", """\
        $ErrorActionPreference = 'Stop'
        $repo = Split-Path -Parent $PSScriptRoot
        $assets = Join-Path $repo 'assets'
        foreach ($name in @('one.txt', 'two.txt')) {
            Copy-Item -LiteralPath (Join-Path $assets $name) -Destination $env:TEMP
        }
        Get-ChildItem -LiteralPath (Join-Path $repo 'templates') -Recurse | Out-Null
        & python (Join-Path $repo 'scripts\\run.py')
        & (Join-Path $repo 'scripts\\helper.ps1')
        """)
    write(root, "checks/unlisted.py", """\
        import os
        from pathlib import Path

        ROOT = Path(__file__).resolve().parents[1]
        for name in os.listdir(os.environ["ASSETS"]):
            print((ROOT / "assets" / name).stat())
        """)
    write(root, "checks/beta.py", "from pathlib import Path\n")
    for relative in ("data/table.csv", "config/settings.toml", "config/helper.ini", "assets/a.txt", "assets/b.txt",
                     "assets/one.txt", "assets/two.txt", "templates/page.html", "templates/nested/part.html", "scripts/loader.json",
                     "guides/guide.md", "ci/pipeline.yml", "src/old.txt", "gone.txt", "stray.txt"):
        write(root, relative, relative + "\n")


FIXTURE_SUITES = {
    "always": ["ci/**", "checks/suites.json"],
    "suites": [
        {"name": "alpha", "entries": ["checks/test_app.py"], "paths": ["guides/**", "checks/test_app.py"]},
        {"name": "beta", "entries": ["checks/beta.py"], "paths": ["src/old.txt"]},
        {"name": "gamma", "entries": ["checks/suite.ps1"], "paths": ["gone.txt", "assets/*.txt"]},
        {"name": "delta", "entries": ["checks/unlisted.py"]},
    ],
}


DELTA_REASON = "runs on every change: no stored path list"


class FixtureRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        fixture_repository(self.root)

    def write_templates(self) -> None:
        for name in ("a.md", "b.md", "c.md"):
            write(self.root, f"data/templates/{name}", name + "\n")


FIXTURE_UNIT = """\
name: Unit

on:
  workflow_call:
    inputs:
      suites:
        type: string
        required: true

jobs:
  unit:
    if: fromJSON(inputs.suites).alpha == 'run'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@0000000000000000000000000000000000000000 # v1.0.0
      - uses: ./tooling/setup
        with:
          settings: config\\settings.toml
      - name: Run
        working-directory: ./checks
        run: |
          cp "$GITHUB_WORKSPACE/ci/pipeline.yml" "$RUNNER_TEMP/"
          python -m unittest
"""
