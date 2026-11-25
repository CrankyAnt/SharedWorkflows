from __future__ import annotations

import unittest
from pathlib import Path

from tests import temporary_support
from tests.ci_policy_support import read_workflow
from tests.publication_commitlint import CommitlintMixin
from tests.publication_fixtures import PublicationFixture, REPOSITORY_ROOT

RESOURCE_DIRECTORY = REPOSITORY_ROOT / "actions" / "publication-checks"
AUTOMATION = REPOSITORY_ROOT / ".github" / "workflows" / "automation.yml"


class PublicationCommitlintTests(CommitlintMixin, PublicationFixture):
    def test_commit_range_skips_only_genuine_merge_commits(self) -> None:
        self._prepare_toolchain()
        with temporary_support.Directory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            configuration = self.tool_directory / "commitlint.config.cjs"
            configuration.write_bytes((RESOURCE_DIRECTORY / "commitlint.config.cjs").read_bytes())
            checker = temporary_path / "check-commits.cjs"
            checker.write_bytes((RESOURCE_DIRECTORY / "check-commits.cjs").read_bytes())
            self._assert_invalid_middle_commit_fails(temporary_path / "invalid-middle", configuration, checker)
            self._assert_genuine_merge_commit_passes(temporary_path / "merge", configuration, checker)
            self._assert_invalid_side_commit_fails(temporary_path / "invalid-side", configuration, checker)
            self._assert_single_parent_merge_message_fails(temporary_path / "spoof", configuration, checker)

    def test_many_commits_are_linted_in_one_process_from_one_git_read(self) -> None:
        self._prepare_toolchain()
        with temporary_support.Directory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            repository = temporary_path / "many"
            self._initialize_repository(repository)
            self._commit(repository, "feat: create baseline")
            base = self._head(repository)
            for index in range(15):
                self._commit(repository, f"chore: commit number {index}")
            self._run_git(repository, "checkout", "-b", "topic")
            self._commit(repository, "fix: topic commit")
            self._run_git(repository, "checkout", "main")
            self._commit(repository, "docs: main commit")
            self._run_git(repository, "merge", "--no-ff", "topic", "-m", "Merge topic branch")

            configuration = self.tool_directory / "commitlint.config.cjs"
            configuration.write_bytes((RESOURCE_DIRECTORY / "commitlint.config.cjs").read_bytes())
            checker = temporary_path / "check-commits.cjs"
            checker.write_bytes((RESOURCE_DIRECTORY / "check-commits.cjs").read_bytes())

            spawn_log = temporary_path / "spawns.log"
            probe = temporary_path / "probe.cjs"
            probe.write_text(
                'const cp = require("node:child_process");\n'
                'const fs = require("node:fs");\n'
                'const log = process.env.SPAWN_LOG;\n'
                'for (const name of ["spawn", "spawnSync", "execFile", "execFileSync"]) {\n'
                '  const orig = cp[name];\n'
                '  cp[name] = function(cmd, ...rest) {\n'
                '    fs.appendFileSync(log, String(cmd) + "\\n");\n'
                '    return orig.call(this, cmd, ...rest);\n'
                '  };\n'
                '}\n',
                encoding="utf-8",
            )
            environment = self.environment.copy()
            environment["COMMITLINT_CLI"] = str(self.commitlint_cli)
            environment["COMMITLINT_CONFIG"] = str(configuration)
            environment["GITHUB_WORKSPACE"] = str(repository)
            environment["SPAWN_LOG"] = str(spawn_log)
            import subprocess
            result = subprocess.run(["node", "--require", str(probe), str(checker), base, "HEAD"],
                                    capture_output=True, text=True, env=environment, cwd=repository)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            spawns = spawn_log.read_text(encoding="utf-8").splitlines() if spawn_log.exists() else []
            self.assertEqual(["git"], spawns, f"expected one git read and zero child linter processes, got {spawns}")

    def test_commit_check_fails_when_linter_cannot_start(self) -> None:
        import subprocess
        with temporary_support.Directory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            repository = temporary_path / "broken-linter"
            self._initialize_repository(repository)
            self._commit(repository, "feat: create baseline")
            base = self._head(repository)
            self._commit(repository, "feat: valid commit")
            checker = temporary_path / "check-commits.cjs"
            checker.write_bytes((RESOURCE_DIRECTORY / "check-commits.cjs").read_bytes())

            for label, cli_value in (
                ("missing", str(temporary_path / "does-not-exist" / "cli.js")),
                ("empty", ""),
            ):
                with self.subTest(case=label):
                    environment = self.environment.copy()
                    environment["COMMITLINT_CLI"] = cli_value
                    environment["COMMITLINT_CONFIG"] = str(RESOURCE_DIRECTORY / "commitlint.config.cjs")
                    environment["GITHUB_WORKSPACE"] = str(repository)
                    result = subprocess.run(["node", str(checker), base, "HEAD"], capture_output=True, text=True, env=environment, cwd=repository)
                    self.assertNotEqual(0, result.returncode, f"expected failure when COMMITLINT_CLI is {label}")
                    self.assertTrue(result.stderr.strip(), f"expected diagnostic on stderr when COMMITLINT_CLI is {label}")

    def test_the_app_commit_of_the_additions_workflow_is_a_conventional_commit(self) -> None:
        self._prepare_toolchain()
        _, workflow = read_workflow(AUTOMATION)
        subject = workflow["jobs"]["additions"]["env"]["SUBJECT"].replace("${{ github.event.inputs.run-id || github.event.workflow_run.id }}", "18234567890")
        self.assertEqual("chore: apply the recorded additions of run 18234567890", subject)
        with temporary_support.Directory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            self._initialize_repository(repository)
            configuration = self.tool_directory / "commitlint.config.cjs"
            configuration.write_bytes((RESOURCE_DIRECTORY / "commitlint.config.cjs").read_bytes())
            self._run_commitlint(f"{subject}\n\n[dependabot skip]\n", configuration, repository, 0, False)


if __name__ == "__main__":
    unittest.main()
