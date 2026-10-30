from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.publication_commitlint import CommitlintMixin
from tests.publication_fixtures import PublicationFixture, REPOSITORY_ROOT

RESOURCE_DIRECTORY = REPOSITORY_ROOT / "actions" / "publication-checks"


class PublicationCommitlintTests(CommitlintMixin, PublicationFixture):
    def test_commitlint_accepts_and_rejects_representative_titles(self) -> None:
        self._prepare_toolchain()
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            configuration = self.tool_directory / "commitlint.config.cjs"
            configuration.write_bytes((RESOURCE_DIRECTORY / "commitlint.config.cjs").read_bytes())
            repository = self._create_repository(temporary_path / "history")
            for message in ["feat: add publication checks", "docs: update CONTEXT capture", "fix!: reject unsafe configuration", "feat: change config\n\nBREAKING CHANGE: existing titles fail."]:
                self._run_commitlint(message, configuration, repository, 0, True)
            for message in ["Revert \"feat: add publication checks\"", "Revert \"feat: support v1.2\""]:
                self._run_commitlint(message, configuration, repository, 0, True)
            for message in ["Feature: add publication checks", "fix: Reject stale state", "feat: 1 add publication checks", "feat: add publication checks.", "Revert publication checks", "Revert \"Feature: add publication checks\"", "Revert \"feat: support v1.2.\""]:
                self._run_commitlint(message, configuration, repository, 1, True)
            self._run_commitlint("fix: retain hosting reference (#123)", configuration, repository, 1, True)
            self._run_commitlint("fix: retain hosting reference (#123)", configuration, repository, 0, False)


if __name__ == "__main__":
    unittest.main()
