from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class PublicationFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary_directory = tempfile.TemporaryDirectory()
        cls.temporary_path = Path(cls.temporary_directory.name)
        cls.product_checkout = cls.temporary_path / "product-checkout"
        cls.product_checkout.mkdir()
        (cls.product_checkout / ".npmrc").write_text("registry=https://invalid.example.test/\n", encoding="utf-8")
        cls.tool_directory = cls.temporary_path / "publication-checks-tools"
        cls.tool_directory.mkdir()
        cls.environment = os.environ.copy()
        cls.environment["GITHUB_WORKSPACE"] = str(cls.product_checkout)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary_directory.cleanup()

    def _initialize_repository(self, repository: Path) -> None:
        repository.mkdir()
        (repository / ".npmrc").write_text("registry=https://invalid.example.test/\n", encoding="utf-8")
        self._run_git(repository, "init", "-b", "main")
        self._run_git(repository, "config", "user.email", "test@example.invalid")
        self._run_git(repository, "config", "user.name", "Test User")

    def _create_repository(self, repository: Path) -> Path:
        self._initialize_repository(repository)
        return repository

    def _commit(self, repository: Path, message: str) -> None:
        self._run_git(repository, "commit", "--allow-empty", "-m", message)

    def _head(self, repository: Path) -> str:
        return self._run_git(repository, "rev-parse", "HEAD").stdout.strip()

    def _run_git(self, repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *arguments], cwd=repository, check=True, capture_output=True, text=True)
