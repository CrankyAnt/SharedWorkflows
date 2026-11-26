"""The manual v1 move: preparation and promotion over local fixture repositories, the update of the management
repository's caller with the start of the sync behind a stand-in for the ``gh`` CLI, and reach checks."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from tests.hygiene_v1_promotion_support import (
    APP,
    Api,
    FixtureTestCase,
    OWNER,
    REPOSITORY_ROOT,
    RESOURCES,
    SCRIPT,
    SURFACE,
    git,
    promote,
    pull_request,
)

MANAGEMENT = "CrankyAnt/ManagementFixture"
RAN = promote.CiRun("completed", "success")
CLEAN = promote.ScanResult(True, "no leaks found")
COMMAND = "python3 actions/repository-hygiene/promote_v1.py promote"


class ManualMove(FixtureTestCase):
    """A fixture whose remote ``v1`` names the baseline, with the CI read and the Gitleaks scan injected."""

    def setUp(self) -> None:
        super().setUp()
        self.surface_file = self.root / "surface.json"
        self.surface_file.write_text(json.dumps(SURFACE), encoding="utf-8")
        self.ci_reads: list[str] = []
        self.scans: list[str] = []
        self.ci_result: promote.CiRun = RAN
        self.scan_result = CLEAN

    def repository(self, root: Path, remote: str) -> str:
        return Api.REPOSITORY

    def ci(self, repository: str, commit: str) -> promote.CiRun:
        self.ci_reads.append(commit)
        return self.ci_result

    def scan(self, root: Path, remote: str, commit: str) -> promote.ScanResult:
        self.scans.append(commit)
        return self.scan_result

    def run_main(self, *arguments: str) -> tuple[int, str]:
        patches = [mock.patch.object(promote, "SURFACE_PATH", self.surface_file), mock.patch.object(promote, "github_verdict", self.ci),
                   mock.patch.object(promote, "gitleaks_scan", self.scan), mock.patch.object(promote, "github_repository", lambda root, remote: self.repository(root, remote))]
        with contextlib.ExitStack() as stack:
            for patch in patches:
                stack.enter_context(patch)
            stdout = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            code = promote.main([arguments[0], "--repository-root", str(self.fixture.repo), *arguments[1:]])
        return code, stdout.getvalue()

    def prepare(self, *arguments: str) -> tuple[int, str]:
        return self.run_main("prepare", *arguments)

    def shared_change(self, content: str = "name: hygiene 2\n") -> str:
        commit = self.fixture.commit("feat: change the shared workflow", {".github/workflows/hygiene.yml": content, "src/app.py": f"print({len(content)})\n"})
        git(self.fixture.repo, "push", "-q", "origin", "main")
        return commit

    def command(self, old: str, new: str) -> str:
        old_option = f"--old {old}" if old else "--old="
        return f"{COMMAND} {old_option} --new {new} --repository-root {shlex.quote(str(self.fixture.repo))}"

    def question(self, output: str) -> str:
        lines = [line for line in output.splitlines() if line.startswith("Question: ")]
        self.assertEqual(1, len(lines), output)
        return lines[0]

    def assert_refused(self, code: int, output: str, reason: str) -> None:
        self.assertEqual(1, code, output)
        self.assertIn(reason, output)
        self.assertNotIn("Question: ", output)
        self.assertNotIn(COMMAND, output)


def blob_of(content: bytes) -> str:
    """The Git blob of a file's content, as the contents API names it under ``sha``."""
    return hashlib.sha1(b"blob %d\0" % len(content) + content, usedforsecurity=False).hexdigest()


class GhStandIn:
    """A stand-in for the ``gh`` CLI in front of the GitHub API. It answers the CI runs of this repository, holds the
    caller that the management repository has on its ``main``, and it would list other repositories of the owner, hand
    out their workflow files and accept an event for each; every call is recorded."""

    OTHERS = ("CrankyAnt/Listing", "CrankyAnt/BlockList")
    CALLER = ("on:\n  repository_dispatch:\n    types: [dependabot-merged, v1-moved]\njobs:\n  select:\n"
              "    uses: CrankyAnt/SharedWorkflows/.github/workflows/suite-selection.yml@v1\n")
    INSTALLED = f"repos/{MANAGEMENT}/contents/.github/workflows/sync.yml"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.options: list[dict] = []
        self.refuse_dispatch = False
        self.refuse_read = False
        self.refuse_write = False
        self.installed: bytes | None = None
        self.at_call = lambda: None

    def refused(self, arguments: list[str], path: str, status: int, message: str) -> subprocess.CompletedProcess:
        """What ``gh api`` gives for a refused request: the API's answer on stdout and a line that names the address."""
        return subprocess.CompletedProcess(["gh", *arguments], 1, stdout=json.dumps({"message": message, "status": str(status)}),
                                           stderr=f"gh: {message} (HTTP {status}) (https://api.github.com/{path})")

    def __call__(self, arguments: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(arguments))
        self.at_call()
        if self.refuse_dispatch and any(argument.endswith("/dispatches") for argument in arguments):
            return subprocess.CompletedProcess(["gh", *arguments], 1, stdout="", stderr=f"HTTP 404: Not Found (https://api.github.com/{arguments[3]})")
        target = arguments[-1]
        if arguments == ["api", f"{self.INSTALLED}?ref=main"]:
            if self.refuse_read:
                return self.refused(arguments, self.INSTALLED, 502, "Server Error")
            if self.installed is None:
                return self.refused(arguments, self.INSTALLED, 404, "Not Found")
            stdout = json.dumps({"type": "file", "path": ".github/workflows/sync.yml", "sha": blob_of(self.installed)})
        elif arguments[:4] == ["api", "--method", "PUT", self.INSTALLED]:
            fields = dict(argument.split("=", 1) for argument in arguments[5::2])
            held = blob_of(self.installed) if self.installed is not None else None
            if self.refuse_write or fields.get("sha") != held or fields.get("branch") != "main":
                return self.refused(arguments, self.INSTALLED, 409, "Conflict")
            self.installed = base64.b64decode(fields["content"], validate=True)
            stdout = json.dumps({"content": {"sha": blob_of(self.installed)}, "commit": {"message": fields["message"]}})
        elif arguments[:2] == ["repo", "list"]:
            stdout = "".join(f"{name}\n" for name in self.OTHERS)
        elif arguments[:2] == ["api", "graphql"]:
            stdout = json.dumps({"data": {"repository": {"object": {"entries": [{"name": "validation.yml", "type": "blob", "mode": 0o100644}]}}}})
        elif "/check-runs" in target:
            stdout = json.dumps({"check_runs": [{"id": 1, "name": "ci / verdict", "status": "completed", "conclusion": "success", "app": {"id": 15368}}]})
        elif "/contents/" in target:
            stdout = self.CALLER
        else:
            stdout = ""
        return subprocess.CompletedProcess(["gh", *arguments], 0, stdout=stdout, stderr="")


class GhMove(ManualMove):
    """A manual move with the ``gh`` stand-in in place of the CLI."""

    def setUp(self) -> None:
        super().setUp()
        self.gh = GhStandIn()

    def run_script(self, *arguments: str) -> tuple[int, str]:
        """Run the script with the ``gh`` stand-in in place of the CLI and the Gitleaks scan injected."""
        real_run = subprocess.run

        def run(command, *rest, **options):
            if command[0] != "gh":
                return real_run(command, *rest, **options)
            self.gh.options.append(options)
            return self.gh(list(command[1:]))

        with mock.patch.object(promote, "SURFACE_PATH", self.surface_file), mock.patch.object(promote, "gitleaks_scan", self.scan), \
                mock.patch.object(promote, "github_repository", lambda root, remote: Api.REPOSITORY), mock.patch.object(promote.subprocess, "run", run), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            root = [] if arguments[0] == "read-tag" else ["--repository-root", str(self.fixture.repo)]
            code = promote.main([arguments[0], *root, *arguments[1:]])
        return code, stdout.getvalue()


class PrepareTests(ManualMove):
    def test_a_promotion_prints_the_question_and_the_promote_command(self) -> None:
        self.fixture.commit("feat: change a resource", {"ci/checks/check.cjs": "// 2\n"})
        target = self.shared_change()
        code, output = self.prepare()
        self.assertEqual(0, code, output)
        question = self.question(output)
        for fragment in [self.fixture.v1, target, "promotion", ".github/workflows/hygiene.yml", "ci/checks/check.cjs", "changes the later runs of every caller"]:
            self.assertIn(fragment, question)
        self.assertNotIn("src/app.py", question, "only shared-surface files are listed")
        lines = output.splitlines()
        self.assertEqual(self.command(self.fixture.v1, target), lines[-1])
        self.assertLess(lines.index(question), len(lines) - 1, "the command follows the question")
        self.assertIn("feat: change the shared workflow", output, "the commits between are reported")
        self.assertIn("ci / verdict: success", output)
        self.assertIn("Gitleaks scan of the tag push: no findings", output)
        self.assertEqual([target], self.ci_reads, "the CI is read once, without waiting")
        self.assertEqual([target], self.scans)
        parsed = promote.build_parser().parse_args(shlex.split(lines[-1])[2:])
        self.assertEqual((self.fixture.v1, target), (parsed.old, parsed.new))

    def test_an_annotated_v1_names_its_tag_object_as_old_and_its_commit_in_the_question(self) -> None:
        git(self.fixture.repo, "tag", "-f", "-a", "-m", "annotated v1", "v1", self.fixture.v1)
        git(self.fixture.repo, "push", "-q", "--force", "origin", "refs/tags/v1")
        tag_object = self.fixture.remote_v1()
        target = self.shared_change()
        code, output = self.prepare()
        self.assertEqual(0, code, output)
        self.assertIn(f"from {self.fixture.v1} to {target}", self.question(output))
        self.assertEqual(self.command(tag_object, target), output.splitlines()[-1])

    def test_a_target_that_is_an_ancestor_of_v1_is_a_rollback(self) -> None:
        later = self.shared_change()
        git(self.fixture.repo, "push", "-q", "--force", "origin", f"{later}:refs/tags/v1")
        code, output = self.prepare("--commit", self.fixture.v1)
        self.assertEqual(0, code, output)
        question = self.question(output)
        self.assertIn("rollback", question)
        self.assertIn(f"from {later} to {self.fixture.v1}", question)
        self.assertIn(".github/workflows/hygiene.yml", question)
        self.assertEqual(self.command(later, self.fixture.v1), output.splitlines()[-1])

    def test_without_a_remote_v1_the_move_is_the_first_creation_with_an_empty_old(self) -> None:
        git(self.fixture.repo, "push", "-q", "origin", ":refs/tags/v1")
        target = self.shared_change()
        code, output = self.prepare()
        self.assertEqual(0, code, output)
        question = self.question(output)
        self.assertIn("first creation", question)
        self.assertIn(target, question)
        self.assertIn(".github/workflows/hygiene.yml", question)
        command = output.splitlines()[-1]
        self.assertEqual(self.command("", target), command)
        self.assertEqual("", promote.build_parser().parse_args(shlex.split(command)[2:]).old)

    def test_the_remote_v1_counts_and_a_local_v1_tag_is_never_read(self) -> None:
        target = self.shared_change()
        git(self.fixture.repo, "tag", "-f", "v1", target)
        code, output = self.prepare()
        self.assertEqual(0, code, output)
        self.assertIn(f"from {self.fixture.v1} to {target}", self.question(output))
        self.assertIn(f"Current v1: {self.fixture.v1}", output)
        self.assertEqual(target, git(self.fixture.repo, "rev-parse", "refs/tags/v1"), "the local tag stays as it was")

    def test_without_commit_the_target_is_the_head_of_origin_main_after_the_fetch(self) -> None:
        other = self.root / "other"
        subprocess.run(["git", "clone", "-q", str(self.fixture.origin), str(other)], check=True, capture_output=True)
        (other / "ci" / "pins.json").write_text('{"version": "9"}\n', encoding="utf-8", newline="\n")
        (other / ".github" / "workflows" / "hygiene.yml").write_text("name: other\n", encoding="utf-8", newline="\n")
        git(other, "commit", "-q", "-am", "feat: change the shared workflow elsewhere")
        git(other, "push", "-q", "origin", "main")
        head = git(other, "rev-parse", "HEAD")
        self.assertNotEqual(head, git(self.fixture.repo, "rev-parse", "origin/main"), "the clone has not fetched the new head yet")
        code, output = self.prepare()
        self.assertEqual(0, code, output)
        self.assertIn(f"Target: {head}", output)
        self.assertEqual(self.command(self.fixture.v1, head), output.splitlines()[-1])

    def test_a_target_that_is_not_on_origin_main_is_refused(self) -> None:
        local = self.fixture.commit("feat: unpushed change", {".github/workflows/hygiene.yml": "name: local\n"})
        code, output = self.prepare("--commit", local)
        self.assert_refused(code, output, f"the target {local} is not on origin/main.")

    def test_a_target_unrelated_to_v1_is_refused(self) -> None:
        git(self.fixture.repo, "switch", "-q", "-c", "elsewhere", self.fixture.v1)
        stray = self.fixture.commit("feat: stray", {"ci/checks/check.cjs": "// stray\n"})
        git(self.fixture.repo, "push", "-q", "--force", "origin", f"{stray}:refs/tags/v1")
        git(self.fixture.repo, "switch", "-q", "main")
        git(self.fixture.repo, "branch", "-q", "-D", "elsewhere")
        target = self.shared_change()
        code, output = self.prepare()
        self.assert_refused(code, output, f"the target {target} is neither a descendant nor an ancestor of the current v1 commit {stray}.")

    def test_ci_that_has_not_completed_is_refused_as_still_running_without_waiting(self) -> None:
        self.shared_change()
        for status in ["queued", "in_progress"]:
            with self.subTest(status=status):
                self.ci_reads.clear()
                self.ci_result = promote.CiRun(status, None)
                code, output = self.prepare()
                self.assert_refused(code, output, f"CI is still running: `ci / verdict` has not completed ({status}); run prepare again once it has.")
                self.assertNotIn("did not pass", output, "a running CI is not a failure")
                self.assertEqual(1, len(self.ci_reads))

    def test_ci_that_did_not_pass_or_has_no_run_is_refused_with_the_verdict_and_its_conclusion(self) -> None:
        self.shared_change()
        for run, reason in [
            (promote.CiRun("completed", "failure"), "the verdict `ci / verdict` of the target did not pass: concluded failure."),
            (promote.CiRun("completed", "cancelled"), "the verdict `ci / verdict` of the target did not pass: concluded cancelled."),
            (promote.CiRun(None, None), "the verdict `ci / verdict` of the target did not pass: no `ci / verdict` check run exists for the target."),
        ]:
            with self.subTest(reason=reason):
                self.ci_result = run
                code, output = self.prepare()
                self.assert_refused(code, output, reason)

    def test_ci_that_cannot_be_read_is_refused(self) -> None:
        self.shared_change()

        def unreadable(repository: str, commit: str) -> promote.CiRun:
            raise RuntimeError("gh api failed: HTTP 502")

        self.ci = unreadable  # type: ignore[method-assign]
        code, output = self.prepare()
        self.assert_refused(code, output, "the verdict `ci / verdict` of the target could not be read: gh api failed: HTTP 502")

    def test_the_promotion_question_states_the_shared_digests_the_move_changes(self) -> None:
        pins_v1 = json.dumps({"node": {"version": "24.0.0"}, "tools": {"gitleaks": {"version": "8.1.0"}}, "python": {"ruff": {"version": "0.1.0"}}}) + "\n"
        pipeline_yml = (
            "name: Pipeline\non: workflow_call\njobs:\n"
            "  plan:\n    steps:\n"
            "      - name: Dependabot coverage\n        id: coverage\n        run: coverage\n"
            "      - name: Relative links\n        id: links\n        run: links\n"
            "  custom:\n    steps:\n      - run: exit 0\n"
            "  dotnet-test:\n    steps:\n      - uses: $/actions/dotnet-test\n"
            "  node-test:\n    steps:\n      - uses: $/actions/node-test\n"
            "  python-test:\n    steps:\n      - uses: $/actions/python-tests\n"
            "  python-verify:\n    steps:\n      - uses: $/actions/python-tests\n"
            "  repository-hygiene:\n    steps:\n      - run: hygiene\n"
        )
        self.fixture.v1 = self.fixture.commit("feat: baseline digests", {
            "ci/pins.json": pins_v1,
            ".github/workflows/pipeline.yml": pipeline_yml,
            "actions/pipeline/timeouts.json": json.dumps({"custom": 15, "dotnet-test": 15, "node-test": 15, "python-test": 15, "python-verify": 15}) + "\n",
            "actions/recording/recording.py": "# rec 1\n",
            "actions/recording/etw-consumer/Program.cs": "// etw 1\n",
            "actions/python-language-step/python_language_step.py": "# py 1\n",
            "actions/python-tests/python_tests.py": "# py tests 1\n",
            "actions/dotnet-test/dotnet_test.py": "# dotnet 1\n",
            "actions/node-test/node_test.py": "# node 1\n",
        })
        git(self.fixture.repo, "push", "-q", "--force", "origin", "main", f"{self.fixture.v1}:refs/tags/v1")
        pins_v2 = json.dumps({"node": {"version": "24.0.0"}, "tools": {"gitleaks": {"version": "8.2.0"}}, "python": {"ruff": {"version": "0.2.0"}}}) + "\n"
        target = self.fixture.commit("feat: bump gitleaks, ruff, etw and dotnet-test", {
            ".github/workflows/hygiene.yml": "name: hygiene 2\n",
            "ci/pins.json": pins_v2,
            "actions/recording/etw-consumer/Program.cs": "// etw 2\n",
            "actions/dotnet-test/dotnet_test.py": "# dotnet 2\n",
            "actions/pipeline/release.py": "# release 2\n",
            "actions/dependabot-auto-merge/consumer-kit/README.md": "# Dependabot auto-merge 2\n",
        })
        git(self.fixture.repo, "push", "-q", "origin", "main")
        code, output = self.prepare()
        self.assertEqual(0, code, output)
        expected = "language:python, part:dotnet-test, platform:windows, tool:gitleaks, tool:ruff"
        self.assertIn(f"Changed shared digests: {expected}", output)
        question = self.question(output)
        self.assertIn(f"It changes the shared digests {expected}.", question)
        self.assertNotIn("platform:linux", question, "a Windows-only change does not move platform:linux")
        self.assertNotIn("part:node-test", question, "an unchanged part does not move its part digest")
        self.assertNotIn("part:python-test", question, "a consumer-kit README or another part folder's file moves no part digest")
        self.assertNotIn("part:custom", question)
        self.assertNotIn("tool:node", question)
        decision = promote.decide(self.fixture.repo, target, self.fixture.v1, SURFACE)
        self.assertFalse(decision.follow)
        self.assertIn(f"It changes the shared digests {expected}.", decision.question)

    def test_a_gitleaks_finding_is_refused(self) -> None:
        self.shared_change()
        self.scan_result = promote.ScanResult(False, "leaks found: 1")
        code, output = self.prepare()
        self.assert_refused(code, output, "the Gitleaks scan of the tag push reports a finding")
        self.assertIn("leaks found: 1", output)

    def test_an_identical_shared_surface_needs_no_move(self) -> None:
        self.fixture.commit("feat: product change", {"src/app.py": "print(2)\n"})
        git(self.fixture.repo, "push", "-q", "origin", "main")
        for arguments in [(), ("--commit", self.fixture.v1)]:
            with self.subTest(arguments=arguments):
                code, output = self.prepare(*arguments)
                self.assert_refused(code, output, "the shared surface is identical at the current v1 and the target: no move is needed.")


class PromoteTests(ManualMove):
    def promote_v1(self, old: str, new: str) -> tuple[int, str]:
        return self.run_main("promote", "--old", old, "--new", new)

    def test_a_promote_under_the_lease_moves_v1_and_names_the_old_and_the_new_commit(self) -> None:
        target = self.shared_change()
        code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(0, code, output)
        self.assertEqual(target, self.fixture.remote_v1())
        self.assertIn(f"v1 moved from {self.fixture.v1} to {target}.", output)
        self.assertEqual([target], self.scans)

    def test_a_promote_of_an_annotated_v1_names_its_old_commit(self) -> None:
        git(self.fixture.repo, "tag", "-f", "-a", "-m", "annotated v1", "v1", self.fixture.v1)
        git(self.fixture.repo, "push", "-q", "--force", "origin", "refs/tags/v1")
        tag_object = self.fixture.remote_v1()
        git(self.fixture.repo, "tag", "-d", "v1")
        target = self.shared_change()
        code, output = self.promote_v1(tag_object, target)
        self.assertEqual(0, code, output)
        self.assertIn(f"v1 moved from {self.fixture.v1} to {target}.", output)

    def test_the_first_creation_pushes_under_an_empty_lease(self) -> None:
        git(self.fixture.repo, "push", "-q", "origin", ":refs/tags/v1")
        target = self.shared_change()
        code, output = self.run_main("promote", "--old=", "--new", target)
        self.assertEqual(0, code, output)
        self.assertEqual(target, self.fixture.remote_v1())
        self.assertIn(f"v1 was created at {target}.", output)

    def test_a_tag_that_moved_after_prepare_rejects_the_push_and_keeps_its_newer_target(self) -> None:
        target = self.shared_change()
        newer = self.shared_change("name: hygiene 3\n")
        git(self.fixture.repo, "push", "-q", "--force", "origin", f"{newer}:refs/tags/v1")
        code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(1, code, output)
        self.assertEqual(newer, self.fixture.remote_v1(), "the newer target survives")
        self.assertIn("v1 moved after prepare and keeps its newer target", output)
        self.assertIn("Start again at prepare.", output)

    def test_a_rejected_push_whose_remote_cannot_be_read_again_still_says_to_start_again_at_prepare(self) -> None:
        target = self.shared_change()
        newer = self.shared_change("name: hygiene 3\n")
        git(self.fixture.repo, "push", "-q", "--force", "origin", f"{newer}:refs/tags/v1")

        def unreadable(root: Path, remote: str) -> str:
            raise RuntimeError("git ls-remote failed: network down")

        with mock.patch.object(promote, "remote_object", unreadable):
            code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(1, code, output)
        self.assertIn("Start again at prepare.", output)
        self.assertEqual(newer, self.fixture.remote_v1())

    def test_an_old_object_that_is_not_available_here_is_named_as_an_object(self) -> None:
        target = self.shared_change()
        with mock.patch.object(promote, "peel", lambda root, sha: sha), mock.patch.object(promote, "has_commit", lambda root, sha: False):
            code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(0, code, output)
        self.assertIn(f"v1 moved from the object {self.fixture.v1} to {target}.", output)

    def test_a_missing_gitleaks_pushes_nothing(self) -> None:
        target = self.shared_change()
        self.scan_result = promote.ScanResult(False, "Gitleaks is not installed; the push waits until it is.")
        code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(1, code, output)
        self.assertEqual(self.fixture.v1, self.fixture.remote_v1())
        self.assertIn("Gitleaks is not installed", output)
        code, output = self.prepare()
        self.assert_refused(code, output, "the Gitleaks scan of the tag push reports a finding or could not run.")
        self.assertIn("Gitleaks is not installed", output)

    def test_a_gitleaks_finding_pushes_nothing(self) -> None:
        target = self.shared_change()
        self.scan_result = promote.ScanResult(False, "leaks found: 1")
        code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(1, code, output)
        self.assertEqual(self.fixture.v1, self.fixture.remote_v1())
        self.assertIn("The Gitleaks scan of the tag push reports a finding or could not run; nothing was pushed.", output)
        self.assertIn("leaks found: 1", output)

    def test_promote_rechecks_the_verdict_before_pushing(self) -> None:
        target = self.shared_change()
        for label, run, expected in (
            ("failed", promote.CiRun("completed", "failure"), "did not pass: concluded failure"),
            ("running", promote.CiRun("in_progress", None), "has not completed (in_progress)"),
            ("missing", promote.CiRun(None, None), "no `ci / verdict` check run exists"),
        ):
            with self.subTest(verdict=label):
                self.ci_result = run
                code, output = self.promote_v1(self.fixture.v1, target)
                self.assertEqual(1, code, output)
                self.assertEqual(self.fixture.v1, self.fixture.remote_v1())
                self.assertIn(expected, output)
                self.assertIn("Nothing was pushed.", output)


class OtherRepositoryTests(GhMove):
    """Apart from the caller update and the start of the sync in the management repository, no subcommand asks GitHub
    about another repository or sends one an event, and none names another repository in what it prints or writes."""

    def assert_names_only_this_repository(self, text: str) -> None:
        self.assertEqual(set(), set(re.findall(r"CrankyAnt/[\w.-]+", text)) - {Api.REPOSITORY}, text)
        for other in GhStandIn.OTHERS:
            self.assertNotIn(other.partition("/")[2], text)

    def assert_asked_only_about_this_repository(self) -> None:
        for call in self.gh.calls:
            self.assertEqual(["api"], call[:1], "no listing of repositories")
            self.assertNotIn("--method", call, "no request that writes")
            self.assertTrue(call[-1].startswith(f"repos/{Api.REPOSITORY}/commits/"), call)

    def test_a_promotion_a_rollback_and_the_first_creation_send_no_event_and_ask_nothing_about_another_repository(self) -> None:
        later = self.shared_change()
        code, output = self.run_script("promote", "--old", self.fixture.v1, "--new", later)
        self.assertEqual(0, code, output)
        self.assertEqual(later, self.fixture.remote_v1())
        code, rollback = self.run_script("promote", "--old", later, "--new", self.fixture.v1)
        self.assertEqual(0, code, rollback)
        self.assertIn(f"v1 moved from {later} to {self.fixture.v1}.", rollback)
        git(self.fixture.repo, "push", "-q", "origin", ":refs/tags/v1")
        code, creation = self.run_script("promote", "--old=", "--new", later)
        self.assertEqual(0, code, creation)
        self.assertIn(f"v1 was created at {later}.", creation)
        self.assertEqual(3, len(self.gh.calls), "promote calls gh only to recheck the verdict of the target commit")
        self.assert_asked_only_about_this_repository()
        for text in (output, rollback, creation):
            self.assert_names_only_this_repository(text)
            self.assertNotIn("v1-moved", text)

    def test_prepare_reads_only_the_ci_of_this_repository_and_its_question_promises_no_event(self) -> None:
        self.shared_change()
        code, output = self.run_script("prepare")
        self.assertEqual(0, code, output)
        self.assertEqual(1, len(self.gh.calls))
        self.assert_asked_only_about_this_repository()
        self.assert_names_only_this_repository(output)
        question = self.question(output)
        self.assertIn("changes the later runs of every caller", question)
        self.assertNotIn("v1-moved", output)
        self.assertNotIn("consumer", question)

    def test_a_refused_prepare_and_a_refused_promote_name_no_other_repository(self) -> None:
        target = self.shared_change()
        self.scan_result = promote.ScanResult(False, "leaks found: 1")
        code, refused = self.run_script("prepare")
        self.assertEqual(1, code, refused)
        code, unpushed = self.run_script("promote", "--old", self.fixture.v1, "--new", target)
        self.assertEqual(1, code, unpushed)
        self.scan_result = CLEAN
        newer = self.shared_change("name: hygiene 3\n")
        git(self.fixture.repo, "push", "-q", "--force", "origin", f"{newer}:refs/tags/v1")
        code, rejected = self.run_script("promote", "--old", self.fixture.v1, "--new", target)
        self.assertEqual(1, code, rejected)
        for text in (refused, unpushed, rejected):
            self.assert_names_only_this_repository(text)
        self.assert_asked_only_about_this_repository()

    def test_the_automatic_move_names_no_other_repository_in_its_log_lines_outputs_or_summary(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n'})
        git(self.fixture.repo, "push", "-q", "origin", "main")
        api = Api()
        api.ref(self.fixture.v1, "commit")
        api.check_runs(commit, [{"id": 1, "name": "ci / verdict", "status": "completed", "conclusion": "success", "app": {"id": 15368}}])
        files = {name: self.root / name for name in ("tag-output", "output", "decide-summary", "await-output", "await-summary", "move-summary")}
        surface = ["--surface", str(self.surface_file)]
        with mock.patch.object(promote, "fetch", api.fetch):
            code, tag_log = self.run_script("read-tag", "--repository", Api.REPOSITORY, "--output", str(files["tag-output"]))
            self.assertEqual(0, code, tag_log)
            request = self.root / "pull-request.json"
            request.write_text(json.dumps(pull_request(commit)), encoding="utf-8")
            code, decide_log = self.run_script("decide", "--commit", commit, "--v1", self.fixture.v1, "--pull-request", str(request), "--app-login", APP, "--owner", OWNER,
                                               *surface, "--output", str(files["output"]), "--summary", str(files["decide-summary"]))
            self.assertEqual(0, code, decide_log)
            self.assertIn("follow=true\n", files["output"].read_text(encoding="utf-8"))
            self.assertEqual(self.fixture.v1, self.fixture.remote_v1(), "v1 waits for the CI on main")
            code = promote.main(["await", "--commit", commit, "--old", self.fixture.v1, "--repository", Api.REPOSITORY,
                                 "--output", str(files["await-output"]), "--summary", str(files["await-summary"])])
            self.assertEqual(0, code)
            self.assertEqual(f"move=true\nold={self.fixture.v1}\nnew={commit}\n", files["await-output"].read_text(encoding="utf-8"))
            code, move_log = self.run_script("move", "--repository", Api.REPOSITORY, "--old", self.fixture.v1, "--new", commit, "--summary", str(files["move-summary"]))
        self.assertEqual(0, code, move_log)
        self.assertEqual(commit, self.fixture.remote_v1())
        self.assertEqual([], self.gh.calls)
        own = f"https://api.github.com/repos/{Api.REPOSITORY}/"
        self.assertTrue(api.requests)
        self.assertTrue(all(url.startswith(own) for url in api.requests), f"the automatic move reads no other repository: {api.requests}")
        for text in (tag_log, decide_log, move_log, *(path.read_text(encoding="utf-8") for path in files.values() if path.exists())):
            self.assert_names_only_this_repository(text)

    def test_the_program_reaches_only_the_repository_it_is_given_and_the_management_repository(self) -> None:
        # tests/test_ci_policy_hygiene_promotion.py holds that promote-v1.yml gives it only the repository of the run.
        program = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual({"{repository}"}, set(re.findall(r"\brepos/([^/\s\"']+)/", program)), "every API path takes the repository it is given")
        self.assertNotIn("CrankyAnt/", program, "the program names no repository by its slug")
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "promote-v1.yml").read_text(encoding="utf-8")
        self.assertNotIn("--remote ", re.sub(r"--remote promote\b", "", workflow), "the workflow's one remote is the repository of the run")
        # The requests about another repository: the read of the caller, its one write and the one start of an event,
        # the sync, all in the repository that the local Git configuration of the checkout names.
        self.assertEqual(['"POST"', '"PUT"'], sorted(re.findall(r'"--method", ("[A-Z]+")', program)), "one start and one write, and nothing is deleted")
        self.assertNotIn("DELETE", program)


class SyncStartTests(GhMove):
    """A move by hand that changes the template class brings the management repository's caller in line with the caller
    at the commit that ``v1`` names and starts the sync there, right after the move; ``caller`` repeats both."""

    CALLER_PATH = "actions/sync/caller.yml"
    READ = ["api", f"repos/{MANAGEMENT}/contents/.github/workflows/sync.yml?ref=main"]
    DISPATCH = ["api", "--method", "POST", f"repos/{MANAGEMENT}/actions/workflows/sync.yml/dispatches", "-f", "ref=main"]
    FIRST = b"name: Sync\non:\n  workflow_dispatch:\n"
    SECOND = b"name: Sync\non:\n  schedule:\n    - cron: \"1 2 * * *\"\n  workflow_dispatch:\n"

    without_caller: str

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.without_caller = cls.template.v1
        commit = cls.template.commit("feat: change the sync caller", {cls.CALLER_PATH: cls.FIRST.decode("utf-8")})
        git(cls.template.repo, "push", "-q", "origin", "main")
        git(cls.template.repo, "push", "-q", "--force", "origin", f"{commit}:refs/tags/v1")
        cls.template.v1 = commit

    def setUp(self) -> None:
        super().setUp()
        self.surface_file.write_text(json.dumps({**SURFACE, "paths": [*SURFACE["paths"], "actions/sync"], "classes": {**SURFACE["classes"], "actions/sync": "template"}}),
                                     encoding="utf-8")
        self.gh.installed = self.FIRST
        self.moved_at_call: list[str] = []
        self.gh.at_call = lambda: self.moved_at_call.append(git(self.fixture.repo, "ls-remote", "origin", "refs/tags/v1").partition("\t")[0])
        self.recovery = f"python3 actions/repository-hygiene/promote_v1.py caller --repository-root {shlex.quote(str(self.fixture.repo))}"

    def management(self) -> None:
        """Name the management repository in the local Git configuration of the checkout, where alone it is named."""
        git(self.fixture.repo, "config", "--local", "sharedworkflows.managementRepository", MANAGEMENT)

    def template_change(self, content: str = "name: caller\n") -> str:
        """A change in the template class that leaves the sync caller as it is."""
        commit = self.fixture.commit("feat: change a template", {"ci/kit/caller.yml": content})
        git(self.fixture.repo, "push", "-q", "origin", "main")
        return commit

    def caller_change(self, content: bytes) -> str:
        commit = self.fixture.commit("feat: change the sync caller", {self.CALLER_PATH: content.decode("utf-8")})
        git(self.fixture.repo, "push", "-q", "origin", "main")
        return commit

    def write(self, held: bytes, content: bytes) -> list[str]:
        """The one request that replaces the caller whose content was ``held`` with ``content``, on ``main``."""
        return ["api", "--method", "PUT", f"repos/{MANAGEMENT}/contents/.github/workflows/sync.yml", "-f", "message=chore: bring the sync caller in line with v1",
                "-f", f"content={base64.b64encode(content).decode('ascii')}", "-f", "branch=main", "-f", f"sha={blob_of(held)}"]

    def assert_names_no_management_repository(self, text: str) -> None:
        self.assertNotIn(MANAGEMENT, text)
        self.assertNotIn("ManagementFixture", text)
        self.assertNotIn("api.github.com", text, "nothing that gh returns is printed")
        self.assertNotIn("HTTP", text, "nothing that gh returns is printed")

    def promote_v1(self, old: str, new: str) -> tuple[int, str]:
        code, output = self.run_script("promote", "--old", old, "--new", new)
        self.assert_names_no_management_repository(output)
        return code, output

    def test_the_real_caller_is_tracked_at_the_path_the_program_reads_and_is_in_the_template_class(self) -> None:
        self.assertEqual(self.CALLER_PATH, promote.CALLER_PATH)
        self.assertEqual(self.CALLER_PATH, subprocess.run(["git", "-C", str(REPOSITORY_ROOT), "ls-files", "--", promote.CALLER_PATH], capture_output=True, text=True,
                                                          check=True).stdout.strip())
        surface = promote.load_surface(RESOURCES / "shared-surface.json")
        self.assertEqual("template", promote.class_of(promote.CALLER_PATH, surface["classes"]))
        self.assertNotIn("consumer-kit", promote.CALLER_PATH)

    def management_calls(self) -> list[list[str]]:
        return [call for call in self.gh.calls if "/check-runs" not in call[-1]]

    def test_an_equal_caller_is_read_from_main_left_as_it_is_and_the_sync_starts_right_after_the_move(self) -> None:
        self.management()
        target = self.template_change()
        code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(0, code, output)
        self.assertEqual([self.READ, self.DISPATCH], self.management_calls(), "one read of the caller on main, no commit, one workflow_dispatch of sync.yml on main")
        self.assertEqual([self.fixture.v1, target, target], self.moved_at_call, "verdict is rechecked before v1 moves, then the caller is read and the sync starts")
        self.assertEqual(self.FIRST, self.gh.installed)
        self.assertIn(f"v1 moved from {self.fixture.v1} to {target}.", output)
        self.assertIn("The move changes the template class: the management repository's caller equals the caller at v1 and stays as it is, and the sync was started.", output)

    def test_a_changed_caller_is_written_from_the_git_object_of_the_target_before_the_sync_starts(self) -> None:
        self.management()
        target = self.caller_change(self.SECOND)
        # The working tree holds other bytes than the commit: the program reads the Git object.
        (self.fixture.repo / self.CALLER_PATH).write_bytes(self.SECOND.replace(b"\n", b"\r\n") + b"# local edit\r\n")
        code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(0, code, output)
        self.assertEqual([self.READ, self.write(self.FIRST, self.SECOND), self.DISPATCH], self.management_calls(), "read, one write on main under the blob that was read, then the start")
        self.assertEqual([self.fixture.v1, target, target, target], self.moved_at_call, "verdict is rechecked before v1 moves, then the management requests run")
        self.assertEqual(self.SECOND, self.gh.installed)
        self.assertIn("The move changes the template class: the management repository's caller was replaced with the caller at v1, and the sync was started.", output)

    def test_a_stale_or_hand_changed_caller_is_repaired_when_the_move_leaves_the_caller_alone(self) -> None:
        self.management()
        for held in (b"name: an older caller\n", self.FIRST + b"# changed by hand\n"):
            with self.subTest(held=held):
                self.gh.calls.clear()
                self.gh.installed = held
                old = self.fixture.remote_v1()
                target = self.template_change(f"name: caller {len(held)}\n")
                self.assertEqual(promote.caller_blob(self.fixture.repo, old), promote.caller_blob(self.fixture.repo, target), "the caller is the same at both commits")
                code, output = self.promote_v1(old, target)
                self.assertEqual(0, code, output)
                self.assertEqual([self.READ, self.write(held, self.FIRST), self.DISPATCH], self.management_calls())
                self.assertEqual(self.FIRST, self.gh.installed)

    def test_a_named_repository_without_a_caller_gets_none_and_no_sync_starts(self) -> None:
        self.management()
        self.gh.installed = None
        target = self.template_change()
        code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(1, code, output)
        self.assertEqual([self.READ], self.management_calls(), "a caller is replaced, never created")
        self.assertIsNone(self.gh.installed)
        self.assertIn("the management repository's caller could not be read, so nothing was written and no sync was started", output)

    def test_a_rollback_brings_the_caller_back_to_the_one_at_the_rollback_target(self) -> None:
        self.management()
        later = self.caller_change(self.SECOND)
        git(self.fixture.repo, "push", "-q", "--force", "origin", f"{later}:refs/tags/v1")
        self.gh.installed = self.SECOND
        code, output = self.promote_v1(later, self.fixture.v1)
        self.assertEqual(0, code, output)
        self.assertEqual(self.fixture.v1, self.fixture.remote_v1())
        self.assertEqual([self.READ, self.write(self.SECOND, self.FIRST), self.DISPATCH], self.management_calls())
        self.assertEqual(self.FIRST, self.gh.installed, "the caller of the rollback target, not the newer one")

    def test_the_first_creation_brings_the_caller_in_line_and_starts_the_sync_too(self) -> None:
        self.management()
        later = self.caller_change(self.SECOND)
        git(self.fixture.repo, "push", "-q", "origin", ":refs/tags/v1")
        code, output = self.run_script("promote", "--old=", "--new", later)
        self.assertEqual(0, code, output)
        self.assertIn(f"v1 was created at {later}. The move changes the template class: the management repository's caller was replaced with the caller at v1, "
                      "and the sync was started.", output)
        self.assertEqual([self.READ, self.write(self.FIRST, self.SECOND), self.DISPATCH], self.management_calls())

    def test_a_move_that_leaves_the_template_class_alone_reads_writes_and_starts_nothing(self) -> None:
        self.management()
        self.gh.installed = b"name: an older caller\n"
        target = self.shared_change()
        code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(0, code, output)
        self.assertEqual([], self.management_calls())
        self.assertNotIn("sync", output)
        self.assertNotIn("caller", output)

    def test_without_a_named_management_repository_a_move_that_keeps_the_caller_says_that_no_sync_started(self) -> None:
        target = self.template_change()
        code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(0, code, output)
        self.assertEqual(target, self.fixture.remote_v1())
        self.assertEqual([], self.management_calls())
        self.assertIn("The move changes the template class; the sync was not started: this checkout names no management repository "
                      "(`git config --local sharedworkflows.managementRepository <owner>/<name>`).", output)

    def test_without_the_old_commit_and_a_management_repository_a_caller_at_the_target_may_have_changed(self) -> None:
        target = self.template_change()
        with mock.patch.object(promote, "moved_files", lambda root, old, new, paths: None):
            code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(1, code, output)
        self.assertEqual([], self.management_calls())
        self.assertIn("Whether the move changes the template class is unknown here and may change the sync caller, but this checkout names no management repository", output)

    def test_without_a_named_management_repository_a_promote_that_changes_the_caller_fails_after_the_move(self) -> None:
        target = self.caller_change(self.SECOND)
        code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(1, code, output)
        self.assertEqual(target, self.fixture.remote_v1(), "promote was run past prepare, and v1 moved")
        self.assertEqual([], self.management_calls())
        self.assertIn("this checkout names no management repository, so its caller stays behind and no sync was started: name it with "
                      f"`git config --local sharedworkflows.managementRepository <owner>/<name>` and run `{self.recovery}`.", output)

    def test_a_caller_that_cannot_be_read_starts_no_sync_and_names_the_command_that_repeats(self) -> None:
        self.management()
        self.gh.refuse_read = True
        target = self.caller_change(self.SECOND)
        code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(1, code, output)
        self.assertEqual(target, self.fixture.remote_v1(), "v1 moved")
        self.assertEqual([self.READ], self.management_calls(), "no write and no start after a failed read")
        self.assertEqual(self.FIRST, self.gh.installed)
        self.assertIn(f"v1 moved from {self.fixture.v1} to {target}.", output)
        self.assertIn("the management repository's caller could not be read, so nothing was written and no sync was started; "
                      f"repeat the caller update and the start of the sync with `{self.recovery}`.", output)

    def test_an_answer_of_the_read_that_names_no_blob_counts_as_a_failed_read(self) -> None:
        self.management()
        target = self.template_change()
        for stdout in ("", "not json", "[]", json.dumps({"type": "file"}), json.dumps({"sha": ""})):
            with self.subTest(stdout=stdout), mock.patch.object(promote, "gh_quiet", return_value=subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")):
                self.assertIsNone(promote.installed_caller(MANAGEMENT))
        with mock.patch.object(promote.subprocess, "run", side_effect=FileNotFoundError("gh")):
            self.assertIsNone(promote.installed_caller(MANAGEMENT), "a missing gh is a failed read")
        with mock.patch.object(promote, "gh_quiet", return_value=None):
            done, text = promote.reconcile_caller(self.fixture.repo, MANAGEMENT, target, self.recovery)
        self.assertFalse(done)
        self.assertIn("could not be read", text)

    def test_a_caller_that_cannot_be_written_starts_no_sync_and_names_the_command_that_repeats(self) -> None:
        self.management()
        self.gh.refuse_write = True
        target = self.caller_change(self.SECOND)
        code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(1, code, output)
        self.assertEqual(target, self.fixture.remote_v1(), "v1 moved")
        self.assertEqual([self.READ, self.write(self.FIRST, self.SECOND)], self.management_calls(), "no start after a failed write")
        self.assertEqual(self.FIRST, self.gh.installed)
        self.assertIn("the management repository's caller differs from the caller at v1 and could not be replaced, so no sync was started; "
                      f"repeat the caller update and the start of the sync with `{self.recovery}`.", output)

    def test_a_target_without_the_caller_writes_nothing_deletes_nothing_and_starts_no_sync(self) -> None:
        self.management()
        self.assertEqual("", promote.caller_blob(self.fixture.repo, self.without_caller))
        code, output = self.promote_v1(self.fixture.v1, self.without_caller)
        self.assertEqual(1, code, output)
        self.assertEqual(self.without_caller, self.fixture.remote_v1(), "the rollback moved v1")
        self.assertEqual([], self.management_calls(), "no read, no write, no deletion and no start")
        self.assertEqual(self.FIRST, self.gh.installed, "the management repository keeps its caller")
        self.assertIn("The move changes the template class: that commit holds no caller at actions/sync/caller.yml, so nothing was written to the management "
                      "repository, its caller stays as it is and no sync was started.", output)

    def test_a_failed_start_of_the_sync_fails_the_command_after_the_caller_update_and_names_the_command_that_repeats(self) -> None:
        self.management()
        self.gh.refuse_dispatch = True
        target = self.caller_change(self.SECOND)
        code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(1, code, output)
        self.assertEqual(target, self.fixture.remote_v1(), "v1 moved")
        self.assertEqual([self.READ, self.write(self.FIRST, self.SECOND), self.DISPATCH], self.management_calls())
        self.assertIn(f"v1 moved from {self.fixture.v1} to {target}.", output)
        self.assertIn("the management repository's caller was replaced with the caller at v1, but the start of the sync failed; "
                      f"repeat the caller update and the start of the sync with `{self.recovery}`.", output)

    def test_a_push_that_did_not_move_v1_reads_writes_and_starts_nothing(self) -> None:
        self.management()
        target = self.caller_change(self.SECOND)
        newer = self.caller_change(self.SECOND + b"# newer\n")
        git(self.fixture.repo, "push", "-q", "--force", "origin", f"{newer}:refs/tags/v1")
        code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(1, code, output)
        self.assertEqual(newer, self.fixture.remote_v1())
        self.assertEqual([], self.management_calls())
        self.assertEqual(self.FIRST, self.gh.installed)

    def test_caller_repeats_the_update_and_the_start_against_the_remote_v1_without_moving_it(self) -> None:
        self.management()
        self.gh.refuse_write = True
        target = self.caller_change(self.SECOND)
        code, output = self.promote_v1(self.fixture.v1, target)
        self.assertEqual(1, code, output)
        self.assertEqual(self.FIRST, self.gh.installed, "the failed promote left the older caller")
        # The checkout moves on: main and a local v1 tag name a newer caller than the remote v1.
        newer = self.caller_change(self.SECOND + b"# newer\n")
        git(self.fixture.repo, "tag", "-f", "v1", newer)
        self.gh.refuse_write, self.gh.refuse_dispatch = False, True
        self.gh.calls.clear()
        self.scans.clear()
        code, output = self.run_script("caller")
        self.assert_names_no_management_repository(output)
        self.assertEqual(1, code, output)
        self.assertEqual([self.READ, self.write(self.FIRST, self.SECOND), self.DISPATCH], self.gh.calls, "the caller at the remote v1, not at main or the local tag")
        self.assertIn(f"v1 names {target}: the management repository's caller was replaced with the caller at v1, but the start of the sync failed; "
                      f"repeat the caller update and the start of the sync with `{self.recovery}`.", output)
        self.gh.refuse_dispatch = False
        self.gh.calls.clear()
        code, output = self.run_script("caller")
        self.assert_names_no_management_repository(output)
        self.assertEqual(0, code, output)
        self.assertEqual([self.READ, self.DISPATCH], self.gh.calls, "the caller is equal by now, and the start is tried again")
        self.assertEqual(f"v1 names {target}: the management repository's caller equals the caller at v1 and stays as it is, and the sync was started.\n", output)
        self.assertEqual(self.SECOND, self.gh.installed)
        self.assertEqual(target, self.fixture.remote_v1(), "caller never moves v1")
        self.assertEqual([], self.scans, "caller pushes nothing, so it scans nothing")

    def test_caller_peels_an_annotated_v1_and_takes_the_remote_it_is_given(self) -> None:
        self.management()
        self.gh.installed = b"name: an older caller\n"
        git(self.fixture.repo, "tag", "-f", "-a", "-m", "annotated v1", "v1", self.fixture.v1)
        git(self.fixture.repo, "push", "-q", "--force", "origin", "refs/tags/v1")
        tag_object = self.fixture.remote_v1()
        git(self.fixture.repo, "tag", "-d", "v1")
        git(self.fixture.repo, "remote", "rename", "origin", "upstream")
        self.gh.at_call = lambda: None
        code, output = self.run_script("caller", "--remote", "upstream")
        self.assertEqual(0, code, output)
        self.assertIn(f"v1 names {self.fixture.v1}:", output)
        self.assertEqual(self.FIRST, self.gh.installed)
        self.assertEqual(tag_object, git(self.fixture.repo, "ls-remote", "upstream", "refs/tags/v1").split()[0])

    def test_caller_without_a_management_repository_or_without_a_remote_v1_fails_before_any_request(self) -> None:
        code, output = self.run_script("caller")
        self.assertEqual(1, code, output)
        self.assertIn(f"v1 names {self.fixture.v1}. Nothing was read, written or started: this checkout names no management repository", output)
        self.management()
        git(self.fixture.repo, "push", "-q", "origin", ":refs/tags/v1")
        code, output = self.run_script("caller")
        self.assertEqual(1, code, output)
        self.assertEqual("No remote v1 exists, so no caller was read or written and no sync was started.\n", output)
        self.assertEqual([], self.management_calls())

    def test_caller_at_a_v1_without_the_caller_writes_nothing_and_starts_no_sync(self) -> None:
        self.management()
        git(self.fixture.repo, "push", "-q", "--force", "origin", f"{self.without_caller}:refs/tags/v1")
        code, output = self.run_script("caller")
        self.assertEqual(1, code, output)
        self.assertEqual([], self.management_calls())
        self.assertEqual(self.FIRST, self.gh.installed)
        self.assertIn("that commit holds no caller at actions/sync/caller.yml", output)

    def test_every_request_about_the_management_repository_runs_gh_with_its_own_login(self) -> None:
        self.management()
        target = self.caller_change(self.SECOND)
        with mock.patch.object(promote, "fetch", side_effect=AssertionError("the GitHub API with GH_TOKEN is not read")):
            code, output = self.promote_v1(self.fixture.v1, target)
            self.assertEqual(0, code, output)
            code, output = self.run_script("caller")
            self.assertEqual(0, code, output)
        self.assertEqual(5, len(self.management_calls()), "a read, a write and a start, then a read and a start")
        self.assertEqual([{"capture_output": True, "text": True, "check": False}] * 6, self.gh.options, "no env and no token reach gh, and its output is captured")
        for call in self.gh.calls:
            self.assertEqual("api", call[0])
            self.assertFalse([argument for argument in call if "token" in argument.lower() or argument.startswith("Authorization")], call)

    def test_no_output_names_the_management_repository_or_prints_what_gh_returns(self) -> None:
        self.management()
        target = self.caller_change(self.SECOND)
        outputs = []
        for refusal in ("refuse_read", "refuse_write", "refuse_dispatch"):
            setattr(self.gh, refusal, True)
            code, output = self.run_script("caller") if outputs else self.run_script("promote", "--old", self.fixture.v1, "--new", target)
            self.assertEqual(1, code, output)
            setattr(self.gh, refusal, False)
            outputs.append(output)
        code, prepared = self.run_script("prepare", "--commit", self.fixture.v1)
        self.assertEqual(0, code, prepared)
        code, done = self.run_script("caller")
        self.assertEqual(0, code, done)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.gh.refuse_read = True
            code, failed = self.run_script("caller")
        self.assertEqual("", stderr.getvalue(), "nothing goes to stderr either")
        for text in (*outputs, prepared, done, failed):
            with self.subTest(text=text):
                self.assert_names_no_management_repository(text)
                self.assertNotIn("Server Error", text)
                self.assertNotIn("Conflict", text)
                self.assertNotIn("Not Found", text)

    def test_prepare_refuses_a_move_that_changes_the_caller_while_the_checkout_names_no_management_repository(self) -> None:
        target = self.caller_change(self.SECOND)
        reason = ("the move changes the sync caller, actions/sync/caller.yml, and this checkout names no management repository, whose caller would stay behind: "
                  "name it with `git config --local sharedworkflows.managementRepository <owner>/<name>` and run prepare again.")
        code, output = self.run_script("prepare")
        self.assert_refused(code, output, reason)
        self.assertIn("Sync caller: changes between the current v1 and the target", output)
        code, output = self.run_script("prepare", "--commit", self.without_caller)
        self.assert_refused(code, output, reason)
        self.management()
        code, output = self.run_script("prepare")
        self.assertEqual(0, code, output)
        self.assertEqual(self.command(self.fixture.v1, target), output.splitlines()[-1])
        self.assert_names_no_management_repository(output)
        self.assertEqual([], [call for call in self.gh.calls if MANAGEMENT in " ".join(call)], "prepare asks nothing about the management repository")

    def test_prepare_refuses_the_first_creation_with_a_caller_while_the_checkout_names_no_management_repository(self) -> None:
        git(self.fixture.repo, "push", "-q", "origin", ":refs/tags/v1")
        code, output = self.run_script("prepare")
        self.assert_refused(code, output, "the move changes the sync caller, actions/sync/caller.yml, and this checkout names no management repository")

    def test_prepare_names_the_classes_the_caller_and_the_sync_in_its_report_and_its_question(self) -> None:
        self.fixture.commit("feat: change a resource", {"ci/checks/check.cjs": "// 2\n"})
        self.template_change()
        code, output = self.run_script("prepare")
        self.assertEqual(0, code, output)
        self.assertIn("  ci/checks/check.cjs (read class)\n  ci/kit/caller.yml (template class)\n", output)
        self.assertIn("Sync caller: the same at the current v1 and the target\n", output)
        self.assertIn("Caller and sync after the move: nothing: this checkout names no management repository "
                      "(`git config --local sharedworkflows.managementRepository <owner>/<name>`)", output)
        question = self.question(output)
        self.assertIn("the shared-surface files ci/checks/check.cjs (read class); ci/kit/caller.yml (template class)?", question)
        self.assertIn("It changes the template class, so right after it the management repository's caller is replaced when it differs from the caller at the target, "
                      "and the sync starts.", question)
        self.management()
        code, output = self.run_script("prepare")
        self.assertEqual(0, code, output)
        self.assertIn("Caller and sync after the move: the management repository's caller is replaced when it differs from the caller at the target, then the sync starts",
                      output)
        self.assert_names_no_management_repository(output)
        self.assertEqual(2, len(self.gh.calls), "prepare reads no caller, writes none and starts no sync")

    def test_prepare_of_a_move_outside_the_template_class_names_no_sync_and_no_caller(self) -> None:
        self.shared_change()
        code, output = self.run_script("prepare")
        self.assertEqual(0, code, output)
        self.assertIn("  .github/workflows/hygiene.yml (write class)\n", output)
        self.assertNotIn("sync", output.lower())
        self.assertNotIn("Sync caller", output)
        self.assertNotIn("Caller and sync", output)
        self.assertNotIn("management repository", output)


class ManualMoveToolTests(unittest.TestCase):
    def completed(self, stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")

    def test_the_verdict_read_runs_gh_api_once_with_its_own_authentication(self) -> None:
        commit = "e" * 40
        body = json.dumps({"check_runs": [
            {"id": 2, "name": "ci / verdict", "status": "in_progress", "conclusion": None, "app": {"id": 15368}},
            {"id": 1, "name": "ci / verdict", "status": "completed", "conclusion": "failure", "app": {"id": 15368}},
        ]})
        with mock.patch.object(promote.subprocess, "run", return_value=self.completed(body)) as run:
            verdict = promote.github_verdict(Api.REPOSITORY, commit)
        self.assertEqual(promote.CiRun("in_progress", None), verdict)
        run.assert_called_once_with(
            ["gh", "api", "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28",
             f"repos/{Api.REPOSITORY}/commits/{commit}/check-runs?check_name=ci%20%2F%20verdict&per_page=100"],
            capture_output=True, text=True, check=False,
        )
        with mock.patch.object(promote.subprocess, "run", return_value=self.completed(json.dumps({"check_runs": []}))):
            self.assertEqual(promote.CiRun(None, None), promote.github_verdict(Api.REPOSITORY, commit))
        with mock.patch.object(promote.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="HTTP 502")):
            with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
                promote.github_verdict(Api.REPOSITORY, commit)

    def test_the_scan_runs_the_push_gate_command_of_this_repository(self) -> None:
        commit, root = "f" * 40, Path("repo")
        with mock.patch.object(promote.subprocess, "run", return_value=self.completed("no leaks found")) as run:
            self.assertEqual(promote.ScanResult(True, "no leaks found"), promote.gitleaks_scan(root, "origin", commit))
        run.assert_called_once_with(["gitleaks", "git", f"--log-opts={commit} --not --remotes=origin", "--redact", "--no-banner", "--no-color",
                                     "--config", ".gitleaks.toml", "--baseline-path", ".gitleaks-baseline.json"],
                                    cwd=root, capture_output=True, text=True, check=False)
        with mock.patch.object(promote.subprocess, "run", return_value=self.completed("leaks found: 1", returncode=1)):
            self.assertFalse(promote.gitleaks_scan(root, "origin", commit).passed)
        with mock.patch.object(promote.subprocess, "run", side_effect=FileNotFoundError("gitleaks")):
            result = promote.gitleaks_scan(root, "origin", commit)
        self.assertEqual((False, "Gitleaks is not installed; the push waits until it is."), (result.passed, result.output))

    def test_the_repository_comes_from_the_remote_url(self) -> None:
        for url in ["https://github.com/CrankyAnt/SharedWorkflows.git", "https://github.com/CrankyAnt/SharedWorkflows", "git@github.com:CrankyAnt/SharedWorkflows.git",
                    "ssh://git@github.com/CrankyAnt/SharedWorkflows.git"]:
            with self.subTest(url=url), mock.patch.object(promote, "git", return_value=url):
                self.assertEqual(Api.REPOSITORY, promote.github_repository(Path("."), "origin"))
        with mock.patch.object(promote, "git", return_value="/srv/origin.git"), self.assertRaisesRegex(RuntimeError, "not a GitHub repository"):
            promote.github_repository(Path("."), "origin")


class TokenTests(ManualMove):
    def test_neither_subcommand_reads_gh_token_or_hands_a_token_to_a_process(self) -> None:
        target = self.shared_change()
        real_run = subprocess.run
        calls: list[tuple[list[str], dict]] = []

        def spy(arguments, *rest, **options):
            calls.append((list(arguments), options))
            return real_run(arguments, *rest, **options)

        environment = {key: value for key, value in os.environ.items() if key != "GH_TOKEN"}
        with mock.patch.dict(os.environ, environment, clear=True), mock.patch.object(promote.subprocess, "run", spy), \
                mock.patch.object(promote, "fetch", side_effect=AssertionError("the GitHub API with GH_TOKEN is not read")):
            code, output = self.prepare()
            self.assertEqual(0, code, output)
            code, output = self.run_main("promote", "--old", self.fixture.v1, "--new", target)
            self.assertEqual(0, code, output)
        self.assertTrue(calls)
        for arguments, options in calls:
            self.assertNotIn("env", options, arguments)
            self.assertFalse([argument for argument in arguments if "token" in argument.lower()], arguments)


if __name__ == "__main__":
    unittest.main()
