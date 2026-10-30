from __future__ import annotations

import os
import subprocess
from pathlib import Path


class CommitlintMixin:
    def _prepare_toolchain(self) -> None:
        installed = os.environ.get("PUBLICATION_CHECKS_TOOLS")
        if not installed:
            self.skipTest("provision the locked toolchain and set PUBLICATION_CHECKS_TOOLS for commitlint tests")
        self.tool_directory = Path(installed)
        self.commitlint_cli = self.tool_directory / "node_modules" / "@commitlint" / "cli" / "lib" / "cli.js"
        self.assertTrue(self.commitlint_cli.is_file(), "the locked commitlint toolchain must already be installed")

    def _run_commitlint(self, message: str, configuration: Path, repository: Path, expected_returncode: int, is_title: bool) -> None:
        environment = self.environment.copy()
        if is_title:
            environment["COMMITLINT_CONTEXT"] = "pull-request-title"
        result = subprocess.run(["node", str(self.commitlint_cli), "--config", str(configuration), "--cwd", str(repository)], input=message, text=True, capture_output=True, env=environment)
        self.assertEqual(expected_returncode, result.returncode, result.stdout + result.stderr)

    def _run_checker(self, checker: Path, configuration: Path, repository: Path, base: str) -> subprocess.CompletedProcess[str]:
        environment = self.environment.copy()
        environment["COMMITLINT_CLI"] = str(self.commitlint_cli)
        environment["COMMITLINT_CONFIG"] = str(configuration)
        environment["GITHUB_WORKSPACE"] = str(repository)
        return subprocess.run(["node", str(checker), base, "HEAD"], capture_output=True, text=True, env=environment, cwd=repository)

    def _assert_invalid_middle_commit_fails(self, repository: Path, configuration: Path, checker: Path) -> None:
        self._initialize_repository(repository)
        self._commit(repository, "feat: create baseline")
        base = self._head(repository)
        self._commit(repository, "Feature: reject middle commit")
        self._commit(repository, "fix: retain full history")
        result = self._run_checker(checker, configuration, repository, base)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("Checking commit", result.stdout)

    def _assert_genuine_merge_commit_passes(self, repository: Path, configuration: Path, checker: Path) -> None:
        self._initialize_repository(repository)
        self._commit(repository, "feat: create baseline")
        base = self._head(repository)
        self._run_git(repository, "checkout", "-b", "topic")
        self._commit(repository, "fix: retain topic history")
        self._run_git(repository, "checkout", "main")
        self._commit(repository, "docs: retain main history")
        self._run_git(repository, "merge", "--no-ff", "topic", "-m", "Merge topic branch")
        result = self._run_checker(checker, configuration, repository, base)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("Skipping merge commit", result.stdout)

    def _assert_single_parent_merge_message_fails(self, repository: Path, configuration: Path, checker: Path) -> None:
        self._initialize_repository(repository)
        self._commit(repository, "feat: create baseline")
        base = self._head(repository)
        self._commit(repository, "Merge topic branch")
        result = self._run_checker(checker, configuration, repository, base)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("Checking commit", result.stdout)
        self.assertNotIn("Skipping merge commit", result.stdout)

    def _assert_invalid_side_commit_fails(self, repository: Path, configuration: Path, checker: Path) -> None:
        self._initialize_repository(repository)
        self._commit(repository, "feat: create baseline")
        base = self._head(repository)
        self._run_git(repository, "checkout", "-b", "topic")
        self._commit(repository, "Feature: reject side commit")
        self._run_git(repository, "checkout", "main")
        self._commit(repository, "docs: retain main history")
        self._run_git(repository, "merge", "--no-ff", "topic", "-m", "Merge topic branch")
        result = self._run_checker(checker, configuration, repository, base)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("Skipping merge commit", result.stdout)
        self.assertIn("Checking commit", result.stdout)
