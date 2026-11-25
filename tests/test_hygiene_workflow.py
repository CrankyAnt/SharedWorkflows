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
            if tokens[0] in ("changes", "gitleaks", "reuse"):
                self.assertEqual("$@", tokens[1], "the step takes event-specific options from the shell block before it")
                if tokens[0] == "changes":
                    events = (["--lookup", "x"], ["--base", "x", "--head", "x", "--compare", "direct"], ["--base", "x", "--head", "x", "--compare", "direct", "--results", "x"])
                elif tokens[0] == "gitleaks":
                    events = (["--lookup", "x"], ["--base", "x", "--head", "x", "--compare", "direct"], ["--lookup", "x", "--record-marker", "x", "--github-output", "x"])
                else:
                    events = ([], ["--record-marker", "x", "--github-output", "x"])
                for event in events:
                    with self.subTest(step=tokens[0], event=event):
                        parser.parse_args([tokens[0], *event, *tokens[2:]])
                continue
            with self.subTest(step=tokens[0]):
                parser.parse_args(tokens)
        install = script_invocations(REPOSITORY_ROOT / ".github" / "workflows" / "repository-hygiene.yml", "install_tools.py")
        self.assertEqual(len(install), 5)
        key_args = install_tools.build_parser().parse_args(install[0])
        self.assertTrue(key_args.print_cache_key)
        self.assertEqual((key_args.platform, key_args.tools), ("linux-x64", ["gitleaks"]))
        expected_tools = [["actionlint", "shellcheck"], ["zizmor"], ["reuse"], ["gitleaks"]]
        for tokens, tools in zip(install[1:], expected_tools, strict=True):
            arguments = install_tools.build_parser().parse_args(tokens)
            self.assertEqual((arguments.platform, arguments.tools), ("linux-x64", tools))


if __name__ == "__main__":
    unittest.main()
