"""The maintenance document keeps the question for every move of v1 but the automatic one and links every recurring task from its overview."""

from __future__ import annotations

import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT


PUBLICATION = REPOSITORY_ROOT / "docs" / "maintaining.md"


class PublicationDocumentTests(unittest.TestCase):
    def test_the_maintenance_document_keeps_the_question_for_every_move_of_v1_but_the_automatic_one(self) -> None:
        publication = PUBLICATION.read_text(encoding="utf-8")
        self.assertIn("chore/update-ci-pins", publication)
        self.assertIn("promote-v1.yml", publication)
        self.assertIn("Every other creation or move of `v1` still needs the question.", publication)

    def test_the_task_overview_links_every_recurring_task_once(self) -> None:
        publication = PUBLICATION.read_text(encoding="utf-8")
        maintaining = publication.split("\n## Maintaining the shared parts\n", 1)[1].split("\n## ", 1)[0]
        overview = maintaining.split("\n### Recurring tasks\n", 1)[1].split("\n### ", 1)[0]
        policy = {"Recurring tasks", "The v1 channel", "Who moves v1", "Automatic v1 move"}
        tasks = [anchor(heading) for heading in re.findall(r"(?m)^####? (.+)$", maintaining) if heading not in policy]
        linked = re.findall(r"\]\(#([^)]+)\)", overview)
        self.assertEqual(sorted(tasks), sorted(linked), "the overview under Recurring tasks links each task section once")


def anchor(heading: str) -> str:
    """The anchor GitHub gives a heading that no earlier heading repeats."""
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")
