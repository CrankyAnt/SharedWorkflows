"""The pipeline's invocations of the plan program and of the hygiene scripts, parsed with the scripts' own parsers, and
the decision of its hygiene job in a consumer, which holds no pipeline of its own."""

from __future__ import annotations

import subprocess
import sys
import unittest

from tests.pipeline_fixtures import CONFIGURATION, REPOSITORY_ROOT, PipelineFixture, plan_program, read_outputs
from tests.workflow_commands import script_invocations

WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "pipeline.yml"
HYGIENE_FOLDER = REPOSITORY_ROOT / "actions" / "repository-hygiene"

sys.path.insert(0, str(HYGIENE_FOLDER))
import hygiene  # noqa: E402
import install_tools  # noqa: E402

sys.path.insert(0, str(REPOSITORY_ROOT / "actions" / "pipeline"))
import consumer_checks  # noqa: E402


class PipelineInvocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.invocations = script_invocations(WORKFLOW, "plan.py")

    def test_the_pipeline_starts_the_plan_and_then_the_verdict_with_options_that_the_program_accepts(self) -> None:
        self.assertEqual(["plan", "verdict"], [tokens[0] for tokens in self.invocations])
        for tokens in self.invocations:
            with self.subTest(command=tokens[0]):
                plan_program.parser().parse_args(tokens)

    def test_the_plan_gets_the_change_and_the_run_and_writes_its_outputs_and_its_summary(self) -> None:
        options = plan_program.parser().parse_args(self.invocations[0])
        self.assertEqual(".", options.root)
        self.assertEqual(str(plan_program.SCHEMAS), options.schemas, "the pipeline reads the schema of its own revision")
        for name in ("base", "head", "workflow", "event", "pull_request", "github_output", "summary"):
            self.assertEqual("x", getattr(options, name), name)
        text = WORKFLOW.read_text(encoding="utf-8")
        for binding in ("BASE: ${{ github.event_name == 'pull_request' && github.event.pull_request.base.sha || github.event.before }}",
                        "HEAD: ${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}",
                        "COMPARE: ${{ github.event_name == 'pull_request' && 'merge-base' || 'direct' }}",
                        "WORKFLOW: ${{ github.workflow }}", "EVENT: ${{ github.event_name }}",
                        "PULL_REQUEST: ${{ github.event.pull_request.number }}", "RUN_ID: ${{ github.run_id }}",
                        '--github-output "$GITHUB_OUTPUT" --summary "$GITHUB_STEP_SUMMARY"'):
            self.assertIn(binding, text)

    def test_the_verdict_gets_the_needs_context_and_writes_its_summary(self) -> None:
        options = plan_program.parser().parse_args(self.invocations[1])
        self.assertEqual(("x/needs.json", "x"), (options.needs, options.summary))


class ConsumerCheckInvocationTests(unittest.TestCase):
    def test_the_hygiene_job_starts_the_three_consumer_checks_with_options_that_the_program_accepts(self) -> None:
        invocations = script_invocations(WORKFLOW, "consumer_checks.py")
        self.assertEqual(["callers", "register", "dependabot"], [tokens[0] for tokens in invocations])
        for tokens in invocations:
            with self.subTest(command=tokens[0]):
                options = consumer_checks.parser().parse_args(tokens)
                self.assertEqual(("x", "x", "x", "."), (options.event, options.author, options.summary, options.root))
                self.assertEqual((str(consumer_checks.KIT), str(plan_program.SCHEMAS)), (options.kit, options.schemas),
                                 "the checks read the templates and the schema of the pipeline's own revision")


class HygieneInvocationTests(PipelineFixture):
    def test_every_hygiene_step_of_the_pipeline_names_a_subcommand_and_options_the_script_accepts(self) -> None:
        invocations = script_invocations(WORKFLOW, "hygiene.py")
        self.assertEqual(["changes", "coverage", "links", "actionlint", "zizmor", "reuse", "gitleaks"], [tokens[0] for tokens in invocations])
        parser = hygiene.build_parser()
        for tokens in invocations:
            events = (["--lookup", "x"], ["--base", "x", "--head", "x", "--compare", "direct"]) if tokens[0] == "changes" else ([],)
            for event in events:
                with self.subTest(step=tokens[0], event=event):
                    parser.parse_args([tokens[0], *event, *[token for token in tokens[1:] if token != "$@"]])
        install = script_invocations(WORKFLOW, "install_tools.py")
        self.assertEqual(1, len(install))
        arguments = install_tools.build_parser().parse_args(install[0])
        self.assertEqual(("linux-x64", ["gitleaks"]), (arguments.platform, arguments.tools))

    def test_the_decision_of_the_hygiene_job_runs_in_a_consumer_that_holds_no_pipeline_of_its_own(self) -> None:
        base = self.commit({CONFIGURATION: "version: 1\n", "README.md": "A consumer.\n", ".github/workflows/ci.yml": "name: CI\non: pull_request\njobs: {}\n"})
        head = self.commit({"docs/guide.md": "A guide.\n"})
        tokens = next(tokens for tokens in script_invocations(WORKFLOW, "hygiene.py") if tokens[0] == "changes")
        definitions = [token for token in tokens[1:] if token not in ("$@", "x") and not token.startswith("--github-output") and not token.startswith("--summary")]
        outputs = self.directory / "hygiene-output"
        completed = subprocess.run([sys.executable, str(HYGIENE_FOLDER / "hygiene.py"), "changes", "--root", str(self.repository), "--base", base, "--head", head,
                                    "--compare", "merge-base", *definitions, "--github-output", str(outputs)], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        decided = read_outputs(outputs)
        self.assertEqual(("true", "false", "false"), (decided["markdown"], decided["actionlint"], decided["packages"]))


if __name__ == "__main__":
    unittest.main()
