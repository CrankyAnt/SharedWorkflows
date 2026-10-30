"""The maintenance document keeps the question for every move of v1 but the automatic one."""

from __future__ import annotations

import unittest

from tests.ci_policy_support import REPOSITORY_ROOT


PUBLICATION = REPOSITORY_ROOT / "docs" / "maintaining.md"


class PublicationDocumentTests(unittest.TestCase):
    def test_the_maintenance_document_keeps_the_question_for_every_move_of_v1_but_the_automatic_one(self) -> None:
        publication = PUBLICATION.read_text(encoding="utf-8")
        self.assertIn("chore/update-ci-pins", publication)
        self.assertIn("promote-v1.yml", publication)
        self.assertIn("Every other creation or move of `v1` still needs the question.", publication)
