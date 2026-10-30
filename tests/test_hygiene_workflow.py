from __future__ import annotations

import sys
import unittest
from pathlib import Path

from tests.workflow_commands import script_invocations

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

RESOURCES = REPOSITORY_ROOT / "actions" / "repository-hygiene"

sys.path.insert(0, str(RESOURCES))
import hygiene  # noqa: E402
import install_tools  # noqa: E402


class WorkflowInvocationTests(unittest.TestCase):
    def test_every_hygiene_step_names_a_subcommand_and_options_the_script_accepts(self) -> None:
        invocations = script_invocations(REPOSITORY_ROOT / ".github" / "workflows" / "repository-hygiene.yml", "hygiene.py")
        self.assertEqual([tokens[0] for tokens in invocations], ["changes", "coverage", "links", "actionlint", "zizmor", "reuse", "gitleaks", "record"])
        parser = hygiene.build_parser()
        for tokens in invocations:
            if tokens[0] == "changes":
                self.assertEqual("$@", tokens[1], "the decision takes the push lookup or the pull request's commits from the case before it")
                for event in (["--lookup", "x"], ["--base", "x", "--head", "x", "--compare", "direct"], ["--base", "x", "--head", "x", "--compare", "direct", "--results", "x"]):
                    with self.subTest(step="changes", event=event):
                        parser.parse_args([tokens[0], *event, *tokens[2:]])
                continue
            with self.subTest(step=tokens[0]):
                parser.parse_args(tokens)
        install = script_invocations(REPOSITORY_ROOT / ".github" / "workflows" / "repository-hygiene.yml", "install_tools.py")
        self.assertEqual(len(install), 1)
        arguments = install_tools.build_parser().parse_args(install[0])
        self.assertEqual((arguments.platform, arguments.tools), ("linux-x64", ["gitleaks"]), "Gitleaks always; each linter only for its selected inputs")


if __name__ == "__main__":
    unittest.main()
