from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.publication_commitlint import CommitlintMixin
from tests.publication_fixtures import PublicationFixture, REPOSITORY_ROOT

RESOURCE_DIRECTORY = REPOSITORY_ROOT / "actions" / "publication-checks"


class PublicationCommitlintTests(CommitlintMixin, PublicationFixture):
    def test_commit_range_skips_only_genuine_merge_commits(self) -> None:
        self._prepare_toolchain()
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            configuration = self.tool_directory / "commitlint.config.cjs"
            configuration.write_bytes((RESOURCE_DIRECTORY / "commitlint.config.cjs").read_bytes())
            checker = temporary_path / "check-commits.cjs"
            checker.write_bytes((RESOURCE_DIRECTORY / "check-commits.cjs").read_bytes())
            self._assert_invalid_middle_commit_fails(temporary_path / "invalid-middle", configuration, checker)
            self._assert_genuine_merge_commit_passes(temporary_path / "merge", configuration, checker)
            self._assert_invalid_side_commit_fails(temporary_path / "invalid-side", configuration, checker)
            self._assert_single_parent_merge_message_fails(temporary_path / "spoof", configuration, checker)


if __name__ == "__main__":
    unittest.main()
