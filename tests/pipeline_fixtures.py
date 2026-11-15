"""Fixture consumers and fixture schema folders for the tests of the pipeline's plan program."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PART_FOLDER = REPOSITORY_ROOT / "actions" / "pipeline"
SCRIPT = PART_FOLDER / "plan.py"
SCHEMAS = PART_FOLDER / "schema"
CONFIGURATION = ".github/ci-config.yml"
GIT_IDENTITY = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false"]

sys.path.insert(0, str(PART_FOLDER))
import plan as plan_program  # noqa: E402

# Invented parts of a fixture schema folder: one that names paths, one without settings and one that always runs.
FIXTURE_PARTS = {
    "fixture-test": {"description": "An invented part whose configuration may name paths.",
                     "configuration": {"type": "object", "additionalProperties": False,
                                       "properties": {"paths": {"$ref": "#/$defs/paths"}, "level": {"type": "string", "enum": ["low", "high"]}}}},
    "fixture-build": {"description": "An invented part without settings.",
                      "configuration": {"type": "object", "additionalProperties": False, "properties": {}}},
    "fixture-floor": {"description": "An invented part that always runs.", "always": True},
}


@dataclass(frozen=True)
class Outcome:
    status: int
    stdout: str
    stderr: str
    outputs: dict[str, str]
    summary: str

    @property
    def plan(self) -> dict:
        return json.loads(self.outputs["plan"])


def git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(["git", *GIT_IDENTITY, "-C", str(repository), *arguments], capture_output=True, text=True, encoding="utf-8", check=True)
    return completed.stdout.strip()


def write(root: Path, relative: str, text: str = "") -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8", newline="\n")
    return path


def read_outputs(path: Path) -> dict[str, str]:
    """The ``name=value`` lines of a ``GITHUB_OUTPUT`` file."""
    if not path.exists():
        return {}
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if line)


class PipelineFixture(unittest.TestCase):
    """A fixture consumer in a temporary Git repository and a fixture schema folder with invented parts."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.repository = self.directory / "consumer"
        self.repository.mkdir()
        git(self.repository, "init", "--quiet", "--initial-branch=main")
        self.schemas = self.schema_folder("schemas", versions=(1,))

    def schema_folder(self, name: str, versions: tuple[int, ...], parts: dict[str, dict] | None = None) -> Path:
        """A schema folder with the real base of version 1 under each of ``versions`` and the invented parts."""
        folder = self.directory / name
        for version in versions:
            target = folder / f"v{version}"
            (target / "parts").mkdir(parents=True)
            base = json.loads((SCHEMAS / "v1" / "base.json").read_text(encoding="utf-8"))
            base["properties"]["version"]["const"] = version
            (target / "base.json").write_text(json.dumps(base), encoding="utf-8")
            for part, fragment in (FIXTURE_PARTS if parts is None else parts).items():
                (target / "parts" / f"{part}.json").write_text(json.dumps(fragment), encoding="utf-8")
        return folder

    def commit(self, files: dict[str, str | None], message: str = "change") -> str:
        """Write the files, remove those whose text is None, commit all and return the commit."""
        for relative, text in files.items():
            if text is None:
                (self.repository / relative).unlink()
            else:
                write(self.repository, relative, text)
        git(self.repository, "add", "--all")
        git(self.repository, "commit", "--quiet", "--allow-empty", "-m", message)
        return git(self.repository, "rev-parse", "HEAD")

    def arguments(self, command: str, *arguments: str) -> tuple[list[str], Path, Path]:
        outputs, summary = self.directory / "github-output", self.directory / "summary.md"
        for path in (outputs, summary):
            path.unlink(missing_ok=True)
        options = ["--root", str(self.repository), "--schemas", str(self.schemas), "--github-output", str(outputs)] if command == "plan" else []
        return [command, *options, "--summary", str(summary), *arguments], outputs, summary

    def run_program(self, command: str, *arguments: str) -> Outcome:
        """Run a command of the plan program in this process, through its command line."""
        argv, outputs, summary = self.arguments(command, *arguments)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = plan_program.main(argv)
        return Outcome(status, stdout.getvalue(), stderr.getvalue(), read_outputs(outputs), summary.read_text(encoding="utf-8") if summary.exists() else "")

    def start_program(self, command: str, *arguments: str) -> Outcome:
        """Start the plan program as a process of its own."""
        argv, outputs, summary = self.arguments(command, *arguments)
        completed = subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True, encoding="utf-8")
        return Outcome(completed.returncode, completed.stdout, completed.stderr, read_outputs(outputs), summary.read_text(encoding="utf-8") if summary.exists() else "")

    def plan(self, *arguments: str) -> Outcome:
        return self.run_program("plan", *arguments)

    def verdict(self, needs: dict) -> Outcome:
        path = self.directory / "needs.json"
        path.write_text(json.dumps(needs), encoding="utf-8")
        return self.run_program("verdict", "--needs", str(path))


def copy_schemas(target: Path) -> Path:
    """A copy of the real schema folder, for a test that changes it."""
    shutil.copytree(SCHEMAS, target)
    return target
