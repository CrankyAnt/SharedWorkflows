"""Shared fixture, API stand-in and base test class for the v1 promotion program tests."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests import temporary_support

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RESOURCES = REPOSITORY_ROOT / "actions" / "repository-hygiene"
SCRIPT = RESOURCES / "promote_v1.py"
SPEC = importlib.util.spec_from_file_location("promote_v1", SCRIPT)
assert SPEC and SPEC.loader
promote = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promote  # dataclasses resolve annotations through sys.modules
SPEC.loader.exec_module(promote)

GIT_IDENTITY = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false"]
# A read workflow and a write workflow, a part folder in the read class and the templates in it.
SURFACE = {
    "pins": "ci/pins.json",
    "updater_branch": "chore/update-ci-pins",
    "paths": [".github/workflows/checks.yml", ".github/workflows/hygiene.yml", "ci"],
    "classes": {".github/workflows/checks.yml": "read", ".github/workflows/hygiene.yml": "write", "ci": "read", "ci/kit": "template", "ci/pins.json": "write"},
}
APP = "pins-updater[bot]"
OWNER = "CrankyAnt"
PASSED = "success"


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", *GIT_IDENTITY, "-C", str(repo), *arguments], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {result.stderr}")
    return result.stdout.strip()


def pull_request(commit: str, *, author: str = APP, branch: str = "chore/update-ci-pins", merged_by: str = OWNER, merged: bool = True) -> dict:
    return {"number": 7, "merged": merged, "merge_commit_sha": commit, "user": {"login": author, "type": "Bot"}, "head": {"ref": branch}, "merged_by": {"login": merged_by}}


class Fixture:
    """A bare origin with v1 at the first commit, and a clone where further commits are made."""

    @temporary_support.measured("v1-promotion")
    def __init__(self, root: Path) -> None:
        self.origin = root / "origin.git"
        self.repo = root / "clone"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True, capture_output=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)], check=True, capture_output=True)
        git(self.repo, "switch", "-q", "-c", "main")
        self.write({"README.md": "fixture\n", "ci/pins.json": '{"version": "1"}\n', ".github/workflows/hygiene.yml": "name: hygiene\n", "ci/checks/check.cjs": "// 1\n", "src/app.py": "print(1)\n"})
        self.v1 = self.commit("feat: baseline")
        git(self.repo, "tag", "v1", self.v1)
        git(self.repo, "push", "-q", "origin", "main", "refs/tags/v1")

    @classmethod
    @temporary_support.measured("v1-promotion")
    def from_template(cls, template: Fixture, root: Path) -> Fixture:
        """Clone ``template`` by copying its origin and working clone into ``root`` without running Git."""
        fixture = cls.__new__(cls)
        fixture.origin = root / "origin.git"
        fixture.repo = root / "clone"
        shutil.copytree(template.origin, fixture.origin)
        shutil.copytree(template.repo, fixture.repo)
        config = fixture.repo / ".git" / "config"
        lines = config.read_text(encoding="utf-8").splitlines(keepends=True)
        in_origin = False
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("["):
                in_origin = stripped == '[remote "origin"]'
            elif in_origin and stripped.split("=", 1)[0].strip() == "url":
                indent = line[:len(line) - len(line.lstrip())]
                ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                lines[index] = f"{indent}url = {fixture.origin.as_posix()}{ending}"
                break
        else:
            raise AssertionError(f'missing [remote "origin"] url in {config}')
        config.write_text("".join(lines), encoding="utf-8")
        fixture.v1 = template.v1
        return fixture

    def write(self, files: dict[str, str]) -> None:
        for relative, content in files.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")

    def commit(self, message: str, files: dict[str, str] | None = None) -> str:
        if files:
            self.write(files)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def remote_v1(self) -> str:
        return git(self.repo, "ls-remote", "origin", "refs/tags/v1").split()[0]


class FixtureTestCase(unittest.TestCase):
    """A test case with a class-level ``Fixture`` template copied into an isolated directory for each test."""

    template: Fixture

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        template_dir = temporary_support.Directory()
        cls.addClassCleanup(template_dir.cleanup)
        cls.template = Fixture(Path(template_dir.name))

    def setUp(self) -> None:
        super().setUp()
        self.temporary = temporary_support.Directory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = Fixture.from_template(self.template, self.root)


class Api:
    """A mocked GitHub API: the ref and tag objects of a repository, and a record of every request."""

    REPOSITORY = "CrankyAnt/SharedWorkflows"

    def __init__(self) -> None:
        self.responses: dict[str, dict] = {}
        self.requests: list[str] = []

    def ref(self, sha: str, kind: str) -> None:
        self.responses[promote.REF_URL.format(repository=self.REPOSITORY)] = {"ref": "refs/tags/v1", "object": {"sha": sha, "type": kind}}

    def tag_object(self, sha: str, target: str, kind: str = "commit") -> None:
        self.responses[promote.TAG_OBJECT_URL.format(repository=self.REPOSITORY, sha=sha)] = {"sha": sha, "tag": "v1", "object": {"sha": target, "type": kind}}

    def check_runs(self, commit: str, runs: list[dict]) -> None:
        self.responses[promote.CHECK_RUNS_URL.format(repository=self.REPOSITORY, commit=commit)] = {"total_count": len(runs), "check_runs": runs}

    def fetch(self, url: str) -> bytes | None:
        self.requests.append(url)
        if url in self.responses:
            return json.dumps(self.responses[url]).encode("utf-8")
        return None
