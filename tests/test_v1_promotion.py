"""The automatic and the manual v1 move: the tag read against a mocked API, the decision and the preparation over local
fixture repositories, and the leased tag push."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tests.workflow_commands import script_invocations

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RESOURCES = REPOSITORY_ROOT / "actions" / "repository-hygiene"
SCRIPT = RESOURCES / "promote_v1.py"
SPEC = importlib.util.spec_from_file_location("promote_v1", SCRIPT)
assert SPEC and SPEC.loader
promote = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = promote  # dataclasses resolve annotations through sys.modules
SPEC.loader.exec_module(promote)

GIT_IDENTITY = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false"]
SURFACE = {
    "pins": "ci/pins.json",
    "updater_branch": "chore/update-ci-pins",
    "paths": [".github/workflows/checks.yml", ".github/workflows/hygiene.yml", "ci"],
}
APP = "pins-updater[bot]"
OWNER = "CrankyAnt"
PASSED = {"validate.yml": "success", "repository-hygiene-caller.yml": "success"}


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", *GIT_IDENTITY, "-C", str(repo), *arguments], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {result.stderr}")
    return result.stdout.strip()


def pull_request(commit: str, *, author: str = APP, branch: str = "chore/update-ci-pins", merged_by: str = OWNER, merged: bool = True) -> dict:
    return {"number": 7, "merged": merged, "merge_commit_sha": commit, "user": {"login": author, "type": "Bot"}, "head": {"ref": branch}, "merged_by": {"login": merged_by}}


class Fixture:
    """A bare origin with v1 at the first commit, and a clone where further commits are made."""

    def __init__(self, root: Path) -> None:
        self.origin = root / "origin.git"
        self.repo = root / "clone"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True, capture_output=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)], check=True, capture_output=True)
        git(self.repo, "switch", "-q", "-c", "main")
        self.write({"README.md": "fixture\n", "ci/pins.json": '{"version": "1"}\n', ".github/workflows/hygiene.yml": "name: hygiene\n", "ci/checks/check.cjs": "// 1\n", "src/app.py": "print(1)\n"})
        self.v1 = self.commit("feat: baseline")
        git(self.repo, "tag", "v1", self.v1)
        git(self.repo, "push", "-q", "origin", "main", "refs/tags/v1")

    def write(self, files: dict[str, str]) -> None:
        for relative, content in files.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")

    def commit(self, message: str, files: dict[str, str] | None = None) -> str:
        if files:
            self.write(files)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", message)
        return git(self.repo, "rev-parse", "HEAD")

    def remote_v1(self) -> str:
        return git(self.repo, "ls-remote", "origin", "refs/tags/v1").split()[0]


class Api:
    """A mocked GitHub API: the ref and tag objects of a repository, and a record of every request."""

    REPOSITORY = "CrankyAnt/SharedWorkflows"

    def __init__(self) -> None:
        self.responses: dict[str, dict] = {}
        self.requests: list[str] = []

    def ref(self, sha: str, kind: str) -> None:
        self.responses[promote.REF_URL.format(repository=self.REPOSITORY)] = {"ref": "refs/tags/v1", "object": {"sha": sha, "type": kind}}

    def tag_object(self, sha: str, target: str, kind: str = "commit") -> None:
        self.responses[promote.TAG_OBJECT_URL.format(repository=self.REPOSITORY, sha=sha)] = {"sha": sha, "tag": "v1", "object": {"sha": target, "type": kind}}

    def runs(self, workflow: str, commit: str, runs: list[dict]) -> None:
        self.responses[promote.RUNS_URL.format(repository=self.REPOSITORY, workflow=workflow, commit=commit)] = {"total_count": len(runs), "workflow_runs": runs}

    def fetch(self, url: str) -> bytes | None:
        self.requests.append(url)
        if url in self.responses:
            return json.dumps(self.responses[url]).encode("utf-8")
        return None


class TagReadTests(unittest.TestCase):
    COMMIT, TAG_OBJECT, OUTER = "a" * 40, "b" * 40, "c" * 40

    def setUp(self) -> None:
        self.api = Api()

    def test_a_lightweight_tag_names_its_commit_as_the_object(self) -> None:
        self.api.ref(self.COMMIT, "commit")
        self.assertEqual((self.COMMIT, self.COMMIT), promote.read_tag(Api.REPOSITORY, self.api.fetch))
        self.assertEqual([promote.REF_URL.format(repository=Api.REPOSITORY)], self.api.requests)

    def test_an_annotated_tag_is_peeled_to_its_commit_and_keeps_the_tag_object_for_the_lease(self) -> None:
        self.api.ref(self.TAG_OBJECT, "tag")
        self.api.tag_object(self.TAG_OBJECT, self.COMMIT)
        self.assertEqual((self.COMMIT, self.TAG_OBJECT), promote.read_tag(Api.REPOSITORY, self.api.fetch))
        self.assertEqual(2, len(self.api.requests))
        self.api.ref(self.OUTER, "tag")
        self.api.tag_object(self.OUTER, self.TAG_OBJECT, "tag")
        self.assertEqual((self.COMMIT, self.OUTER), promote.read_tag(Api.REPOSITORY, self.api.fetch), "a tag of a tag peels through")

    def test_a_missing_tag_reads_as_empty_and_is_reported_without_failing(self) -> None:
        self.assertEqual(("", ""), promote.read_tag(Api.REPOSITORY, self.api.fetch))
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        output = Path(temporary.name) / "output.txt"
        with mock.patch.object(promote, "fetch", self.api.fetch), contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(0, promote.main(["read-tag", "--repository", Api.REPOSITORY, "--output", str(output)]))
        self.assertIn("no v1 tag exists yet.", stdout.getvalue())
        self.assertEqual("v1=\nv1_object=\n", output.read_text(encoding="utf-8"))
        self.api.ref(self.TAG_OBJECT, "tag")
        self.api.tag_object(self.TAG_OBJECT, self.COMMIT)
        with mock.patch.object(promote, "fetch", self.api.fetch), contextlib.redirect_stdout(io.StringIO()) as stdout:
            self.assertEqual(0, promote.main(["read-tag", "--repository", Api.REPOSITORY, "--output", str(output)]))
        self.assertIn(f"v1 names {self.COMMIT} (tag object {self.TAG_OBJECT}).", stdout.getvalue())
        self.assertEqual(f"v1=\nv1_object=\nv1={self.COMMIT}\nv1_object={self.TAG_OBJECT}\n", output.read_text(encoding="utf-8"))

    def test_an_unreadable_tag_object_fails_instead_of_reading_as_a_lightweight_tag(self) -> None:
        self.api.ref(self.TAG_OBJECT, "tag")
        with self.assertRaises(RuntimeError):
            promote.read_tag(Api.REPOSITORY, self.api.fetch)


class DecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Fixture(Path(self.temporary.name))
        self.ci_calls = 0

    def decide(self, commit: str, pr: dict | None, v1: str | None = None, app: str = APP, ci: dict | None = None) -> promote.Decision:
        def conclusions() -> dict:
            self.ci_calls += 1
            return PASSED if ci is None else ci

        return promote.decide(self.fixture.repo, commit, self.fixture.v1 if v1 is None else v1, pr, app, OWNER, SURFACE, ci=conclusions)

    def test_a_pins_only_updater_pull_request_merged_by_the_owner_moves_v1(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n'})
        decision = self.decide(commit, pull_request(commit))
        self.assertTrue(decision.move, decision.reason)
        self.assertEqual((self.fixture.v1, commit), (decision.old, decision.new))
        self.assertIn(f"`v1` moves from `{self.fixture.v1}` to `{commit}`", decision.summary())
        self.assertIn("the CI of this commit on main passed", decision.summary())
        self.assertEqual(1, self.ci_calls)

    def test_the_ci_of_the_commit_on_main_must_pass_before_v1_moves(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n'})
        for ci, fragment in [
            ({"validate.yml": "failure", "repository-hygiene-caller.yml": "success"}, "the CI of this commit on main did not pass: validate.yml concluded failure."),
            ({"validate.yml": "success", "repository-hygiene-caller.yml": "cancelled"}, "the CI of this commit on main did not pass: repository-hygiene-caller.yml concluded cancelled."),
            ({"validate.yml": "timed_out", "repository-hygiene-caller.yml": "success"}, "the CI of this commit on main did not pass: validate.yml concluded timed_out."),
            ({"validate.yml": None, "repository-hygiene-caller.yml": "success"}, "the CI of this commit on main had not completed when the wait ended: validate.yml."),
            ({"validate.yml": None, "repository-hygiene-caller.yml": None}, "the CI of this commit on main had not completed when the wait ended: validate.yml, repository-hygiene-caller.yml."),
        ]:
            with self.subTest(ci=ci):
                decision = self.decide(commit, pull_request(commit), ci=ci)
                self.assertFalse(decision.move)
                self.assertEqual(fragment, decision.reason)
                self.assertEqual(f"## v1 promotion\n\n`v1` stays at `{self.fixture.v1}`: {fragment} A manual promotion is needed for this commit.\n", decision.summary())

    def test_ci_runs_that_could_not_be_read_when_the_wait_ended_keep_v1_with_the_error(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n'})

        def unreadable() -> dict:
            raise promote.CiReadError("HTTPError: HTTP Error 502: Bad Gateway")

        decision = promote.decide(self.fixture.repo, commit, self.fixture.v1, pull_request(commit), APP, OWNER, SURFACE, ci=unreadable)
        self.assertFalse(decision.move)
        reason = "the CI runs of this commit on main could not be read when the wait ended: HTTPError: HTTP Error 502: Bad Gateway."
        self.assertEqual(f"## v1 promotion\n\n`v1` stays at `{self.fixture.v1}`: {reason} A manual promotion is needed for this commit.\n", decision.summary())

    def test_the_ci_is_read_only_after_the_other_conditions_hold(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n', "src/app.py": "print(3)\n"})
        self.assertFalse(self.decide(commit, pull_request(commit)).move)
        self.assertEqual(0, self.ci_calls, "a commit that fails another condition does not wait for its CI")

    def test_other_commits_between_v1_and_the_update_are_fine_when_they_leave_the_shared_surface_alone(self) -> None:
        self.fixture.commit("feat: product change", {"src/app.py": "print(2)\n", "docs/notes.md": "x\n"})
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n'})
        self.assertTrue(self.decide(commit, pull_request(commit)).move)

    def test_a_commit_that_changes_any_other_file_keeps_v1(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n', "src/app.py": "print(3)\n"})
        decision = self.decide(commit, pull_request(commit))
        self.assertFalse(decision.move)
        self.assertIn("the commit changes ci/pins.json, src/app.py, not only ci/pins.json", decision.reason)
        # The workflow learns the App's login only for pins-only commits; the reason still names the files.
        self.assertIn("the commit changes ci/pins.json, src/app.py", self.decide(commit, pull_request(commit), app="").reason)
        self.assertIn("A manual promotion is needed for this commit.", decision.summary())
        self.assertIn(f"`v1` stays at `{self.fixture.v1}`", decision.summary())

    def test_a_pull_request_that_is_not_the_app_updater_pull_request_keeps_v1(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n'})
        for pr, fragment in [
            (None, "no merged pull request landed this commit"),
            (pull_request(commit, author="someone"), "opened by someone, not by the updater App pins-updater[bot]"),
            (pull_request(commit, branch="feat/other"), "came from feat/other, not from the updater branch chore/update-ci-pins"),
            (pull_request(commit, merged_by="colleague"), "merged by colleague, not by the repository owner CrankyAnt"),
            (pull_request(commit, merged=False), "not the merged pull request that produced it"),
            (pull_request("0" * 40), "not the merged pull request that produced it"),
        ]:
            with self.subTest(fragment=fragment):
                decision = self.decide(commit, pr)
                self.assertFalse(decision.move)
                self.assertIn(fragment, decision.reason)
        decision = self.decide(commit, pull_request(commit), app="")
        self.assertFalse(decision.move, "without a known App login nothing moves")
        self.assertIn("not by the updater App (unknown)", decision.reason)

    def test_a_shared_surface_change_between_v1_and_the_commit_needs_a_manual_promotion(self) -> None:
        self.fixture.commit("feat: change the shared workflow", {".github/workflows/hygiene.yml": "name: hygiene 2\n"})
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n'})
        decision = self.decide(commit, pull_request(commit))
        self.assertFalse(decision.move)
        self.assertIn("the shared surface between v1 and this commit also differs in .github/workflows/hygiene.yml", decision.reason)
        self.assertIn("A manual promotion is needed", decision.summary())
        self.fixture.commit("feat: change a resource", {"ci/checks/check.cjs": "// 2\n"})
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "3"}\n'})
        self.assertIn("ci/checks/check.cjs", self.decide(commit, pull_request(commit)).reason)

    def test_a_missing_or_unrelated_v1_keeps_v1(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n'})
        self.assertIn("no v1 tag exists yet", self.decide(commit, pull_request(commit), v1="").reason)
        self.assertIn("is not available in this checkout", self.decide(commit, pull_request(commit), v1="1" * 40).reason)
        git(self.fixture.repo, "switch", "-q", "-c", "elsewhere", self.fixture.v1)
        stray = self.fixture.commit("feat: stray", {"src/other.py": "x\n"})
        git(self.fixture.repo, "switch", "-q", "main")
        self.assertIn("is not an ancestor of this commit", self.decide(commit, pull_request(commit), v1=stray).reason)

    def test_a_stay_with_an_identical_shared_surface_needs_no_promotion(self) -> None:
        commit = self.fixture.commit("feat: product change", {"src/app.py": "print(2)\n", "docs/notes.md": "x\n"})
        for pr, name in [(None, "no merged pull request"), (pull_request(commit, author="someone", branch="feat/product"), "a merged pull request outside the shared surface")]:
            with self.subTest(name=name):
                decision = self.decide(commit, pr)
                self.assertFalse(decision.move)
                self.assertEqual((self.fixture.v1, commit), (decision.old, decision.new))
                self.assertEqual(f"## v1 promotion\n\n`v1` stays at `{self.fixture.v1}`: the shared surface is identical between `v1` and this commit. No promotion is needed for this commit.\n", decision.summary())

    def test_an_identical_shared_surface_names_the_tag_object_of_an_annotated_v1(self) -> None:
        commit = self.fixture.commit("feat: product change", {"src/app.py": "print(2)\n"})
        tag_object = "b" * 40
        decision = promote.decide(self.fixture.repo, commit, self.fixture.v1, None, APP, OWNER, SURFACE, tag_object, ci=lambda: PASSED)
        self.assertEqual((False, tag_object, commit), (decision.move, decision.old, decision.new))
        self.assertEqual(f"## v1 promotion\n\n`v1` stays at `{tag_object}`: the shared surface is identical between `v1` and this commit. No promotion is needed for this commit.\n", decision.summary())

    def test_an_identical_shared_surface_needs_no_promotion_when_v1_is_not_an_ancestor(self) -> None:
        git(self.fixture.repo, "switch", "-q", "-c", "elsewhere", self.fixture.v1)
        stray = self.fixture.commit("feat: stray", {"src/other.py": "x\n"})
        git(self.fixture.repo, "switch", "-q", "main")
        commit = self.fixture.commit("feat: product change", {"src/app.py": "print(2)\n"})
        decision = self.decide(commit, None, v1=stray)
        self.assertFalse(decision.move)
        self.assertEqual((stray, commit), (decision.old, decision.new))
        self.assertTrue(decision.summary().endswith("No promotion is needed for this commit.\n"), decision.summary())

    def test_a_stay_with_a_changed_shared_surface_still_asks_for_a_manual_promotion(self) -> None:
        pins = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n'})
        workflow = self.fixture.commit("feat: change the shared workflow", {".github/workflows/hygiene.yml": "name: hygiene 2\n"})
        for commit, name in [(pins, "the pins file alone"), (workflow, "a shared workflow")]:
            with self.subTest(name=name):
                decision = self.decide(commit, None)
                self.assertFalse(decision.move)
                self.assertEqual((self.fixture.v1, commit), (decision.old, decision.new))
                self.assertEqual(f"## v1 promotion\n\n`v1` stays at `{self.fixture.v1}`: no merged pull request landed this commit. A manual promotion is needed for this commit.\n", decision.summary())

    def test_a_stay_without_a_usable_v1_still_asks_for_a_manual_promotion(self) -> None:
        commit = self.fixture.commit("feat: product change", {"src/app.py": "print(2)\n"})
        for v1, target in [("", "no v1 tag"), ("1" * 40, "1" * 40)]:
            with self.subTest(v1=v1):
                decision = self.decide(commit, None, v1=v1)
                self.assertFalse(decision.move)
                self.assertEqual((v1, commit), (decision.old, decision.new))
                self.assertEqual(f"## v1 promotion\n\n`v1` stays at `{target}`: no merged pull request landed this commit. A manual promotion is needed for this commit.\n", decision.summary())

    def test_a_stay_whose_comparison_fails_still_asks_for_a_manual_promotion(self) -> None:
        unknown = "0" * 40
        decision = self.decide(unknown, None)
        self.assertEqual((False, self.fixture.v1, unknown), (decision.move, decision.old, decision.new))
        self.assertEqual(f"## v1 promotion\n\n`v1` stays at `{self.fixture.v1}`: no merged pull request landed this commit. A manual promotion is needed for this commit.\n", decision.summary())

    def test_the_cli_writes_outputs_and_the_summary(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n'})
        root = Path(self.temporary.name)
        (root / "pr.json").write_text(json.dumps(pull_request(commit)), encoding="utf-8")
        (root / "surface.json").write_text(json.dumps(SURFACE), encoding="utf-8")
        output, summary = root / "output.txt", root / "summary.md"
        command = [sys.executable, str(SCRIPT), "decide", "--repository-root", str(self.fixture.repo), "--commit", commit, "--v1", self.fixture.v1,
                   "--pull-request", str(root / "pr.json"), "--app-login", APP, "--owner", OWNER, "--repository", Api.REPOSITORY, "--surface", str(root / "surface.json"),
                   "--output", str(output), "--summary", str(summary)]
        api = Api()
        for workflow in promote.CI_WORKFLOWS:
            api.runs(workflow, commit, [{"id": 1, "status": "completed", "conclusion": "success"}])
        with mock.patch.object(promote, "fetch", api.fetch), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, promote.main(command[2:]))
        self.assertEqual(f"move=true\nold={self.fixture.v1}\nnew={commit}\n", output.read_text(encoding="utf-8"))
        self.assertIn("the CI of this commit on main passed", summary.read_text(encoding="utf-8"))
        self.assertEqual([promote.RUNS_URL.format(repository=Api.REPOSITORY, workflow=workflow, commit=commit) for workflow in promote.CI_WORKFLOWS], api.requests)
        result = subprocess.run([*command[:-4], "--pull-request", str(root / "missing.json")], capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("no merged pull request landed this commit", result.stdout)
        self.assertIn("A manual promotion is needed", result.stdout)

    def test_the_cli_reports_an_identical_shared_surface(self) -> None:
        commit = self.fixture.commit("feat: product change", {"src/app.py": "print(2)\n"})
        root = Path(self.temporary.name)
        (root / "surface.json").write_text(json.dumps(SURFACE), encoding="utf-8")
        output, summary = root / "output.txt", root / "summary.md"
        command = [sys.executable, str(SCRIPT), "decide", "--repository-root", str(self.fixture.repo), "--commit", commit, "--v1", self.fixture.v1,
                   "--pull-request", str(root / "missing.json"), "--app-login", APP, "--owner", OWNER, "--repository", Api.REPOSITORY, "--surface", str(root / "surface.json"),
                   "--output", str(output), "--summary", str(summary)]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        expected = f"## v1 promotion\n\n`v1` stays at `{self.fixture.v1}`: the shared surface is identical between `v1` and this commit. No promotion is needed for this commit.\n"
        self.assertEqual(expected, summary.read_text(encoding="utf-8"))
        self.assertIn(expected, result.stdout)
        self.assertNotIn("A manual promotion is needed", result.stdout)
        self.assertEqual(f"move=false\nold={self.fixture.v1}\nnew={commit}\n", output.read_text(encoding="utf-8"))


class CiWaitTests(unittest.TestCase):
    COMMIT = "d" * 40

    def setUp(self) -> None:
        self.api = Api()
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def wait(self, fetcher=None, timeout: float = 600, interval: float = 30, request_timeout: float = 5) -> dict:
        return promote.wait_for_ci(Api.REPOSITORY, self.COMMIT, fetcher or self.api.fetch, timeout=timeout, interval=interval,
                                   request_timeout=request_timeout, clock=lambda: self.now, sleep=self.sleep)

    def test_the_newest_push_run_on_main_of_each_workflow_gives_its_conclusion_and_no_job_is_read(self) -> None:
        # A validation run whose verdict job was skipped for an identical tree concludes success; only the run's conclusion is read.
        self.api.runs("validate.yml", self.COMMIT, [{"id": 9, "status": "completed", "conclusion": "success"}, {"id": 4, "status": "completed", "conclusion": "failure"}])
        self.api.runs("repository-hygiene-caller.yml", self.COMMIT, [{"id": 3, "status": "completed", "conclusion": "failure"}, {"id": 8, "status": "completed", "conclusion": "success"}])
        conclusions = self.wait()
        self.assertEqual(PASSED, conclusions)
        self.assertIsNone(promote.ci_stay_reason(conclusions))
        self.assertEqual([], self.sleeps)
        self.assertEqual([promote.RUNS_URL.format(repository=Api.REPOSITORY, workflow=workflow, commit=self.COMMIT) for workflow in promote.CI_WORKFLOWS], self.api.requests)
        for fragment in ["/actions/workflows/{workflow}/runs?", "head_sha={commit}", "event=push", "branch=main"]:
            self.assertIn(fragment, promote.RUNS_URL)

    def test_the_wait_polls_until_both_runs_completed(self) -> None:
        polls = iter([
            ([], [{"id": 8, "status": "in_progress", "conclusion": None}]),
            ([{"id": 9, "status": "queued", "conclusion": None}], [{"id": 8, "status": "completed", "conclusion": "success"}]),
            ([{"id": 9, "status": "completed", "conclusion": "success"}], [{"id": 8, "status": "completed", "conclusion": "success"}]),
        ])

        def polling_fetch(url: str) -> bytes | None:
            if "/validate.yml/" in url:
                validate, hygiene = next(polls)
                self.api.runs("validate.yml", self.COMMIT, validate)
                self.api.runs("repository-hygiene-caller.yml", self.COMMIT, hygiene)
            return self.api.fetch(url)

        self.assertEqual(PASSED, self.wait(polling_fetch))
        self.assertEqual([30, 30], self.sleeps)

    def test_a_run_that_concluded_without_success_ends_the_wait_without_waiting_for_the_other(self) -> None:
        for conclusion in ["failure", "cancelled", "timed_out"]:
            with self.subTest(conclusion=conclusion):
                self.sleeps.clear()
                self.api.runs("validate.yml", self.COMMIT, [{"id": 9, "status": "in_progress", "conclusion": None}])
                self.api.runs("repository-hygiene-caller.yml", self.COMMIT, [{"id": 8, "status": "completed", "conclusion": conclusion}])
                conclusions = self.wait()
                self.assertEqual({"validate.yml": None, "repository-hygiene-caller.yml": conclusion}, conclusions)
                self.assertEqual([], self.sleeps)
                self.assertEqual(f"the CI of this commit on main did not pass: repository-hygiene-caller.yml concluded {conclusion}.", promote.ci_stay_reason(conclusions))

    def test_runs_that_have_not_completed_within_the_bound_stay_unfinished(self) -> None:
        self.api.runs("validate.yml", self.COMMIT, [{"id": 9, "status": "in_progress", "conclusion": None}])
        conclusions = self.wait(timeout=90, interval=30, request_timeout=5)
        self.assertEqual({"validate.yml": None, "repository-hygiene-caller.yml": None}, conclusions)
        self.assertEqual([30, 30, 20], self.sleeps, "the last poll starts when two full request timeouts still end at the bound")
        self.assertEqual("the CI of this commit on main had not completed when the wait ended: validate.yml, repository-hygiene-caller.yml.", promote.ci_stay_reason(conclusions))

    def test_a_failed_read_counts_as_not_completed_and_polling_goes_on(self) -> None:
        failures = iter([urllib.error.URLError("temporary failure in name resolution"), TimeoutError("timed out")])
        self.api.runs("validate.yml", self.COMMIT, [{"id": 9, "status": "completed", "conclusion": "success"}])
        self.api.runs("repository-hygiene-caller.yml", self.COMMIT, [{"id": 8, "status": "completed", "conclusion": "success"}])

        def flaky_fetch(url: str) -> bytes | None:
            failure = next(failures, None)
            if failure:
                raise failure
            return self.api.fetch(url)

        self.assertEqual(PASSED, self.wait(flaky_fetch))
        self.assertEqual([30, 30], self.sleeps)

    def test_a_read_that_still_fails_on_the_last_poll_names_its_error(self) -> None:
        error = urllib.error.HTTPError(promote.RUNS_URL, 502, "Bad Gateway", {}, None)  # type: ignore[arg-type]
        self.addCleanup(error.close)

        def failing_fetch(url: str) -> bytes | None:
            raise error

        with self.assertRaises(promote.CiReadError) as raised:
            self.wait(failing_fetch, timeout=90, interval=30, request_timeout=5)
        self.assertEqual("HTTPError: HTTP Error 502: Bad Gateway", str(raised.exception))
        self.assertEqual([30, 30, 20], self.sleeps)

    def test_the_worst_case_of_the_wait_ends_within_the_decide_job_timeout(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "promote-v1.yml").read_text(encoding="utf-8")
        job_timeout = int(re.search(r"(?m)^  decide:\n    runs-on: ubuntu-slim\n    timeout-minutes: (\d+)$", workflow).group(1)) * 60
        for outcome in ["pending", "timeout"]:
            with self.subTest(outcome=outcome):
                self.now = 0.0

                def slow_fetch(url: str) -> bytes | None:
                    # Every request takes its full timeout before it answers or fails.
                    self.now += promote.REQUEST_TIMEOUT
                    if outcome == "timeout":
                        raise TimeoutError("timed out")
                    return json.dumps({"workflow_runs": [{"id": 1, "status": "in_progress", "conclusion": None}]}).encode("utf-8")

                try:
                    promote.wait_for_ci(Api.REPOSITORY, self.COMMIT, slow_fetch, clock=lambda: self.now, sleep=self.sleep)
                except promote.CiReadError:
                    self.assertEqual("timeout", outcome)
                self.assertLessEqual(self.now, promote.CI_TIMEOUT)
        # The checkout, the facts step with its tag reads and the App token get the rest of the job's time, under the 15-minute limit of ubuntu-slim.
        tag_reads = 2 * promote.REQUEST_TIMEOUT
        self.assertGreaterEqual(job_timeout - promote.CI_TIMEOUT - tag_reads, 3 * 60)
        self.assertLess(job_timeout, 15 * 60)


class ToolDocumentTests(unittest.TestCase):
    def test_the_automatic_move_section_states_the_constants_and_the_job_timeout(self) -> None:
        tool = (REPOSITORY_ROOT / "docs" / "maintaining.md").read_text(encoding="utf-8")
        section = tool.split("#### Automatic v1 move\n", 1)[1].split("\n#### ", 1)[0]
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "promote-v1.yml").read_text(encoding="utf-8")
        job_timeout = re.search(r"(?m)^  decide:\n    runs-on: ubuntu-slim\n    timeout-minutes: (\d+)$", workflow).group(1)
        figures = {
            r"polls both runs every (\d+) seconds": str(promote.CI_INTERVAL),
            r"Each request times out after (\d+) seconds": str(promote.REQUEST_TIMEOUT),
            r"ends within (\d+) minutes of the start of the wait": str(promote.CI_TIMEOUT // 60),
            r"`timeout-minutes: (\d+)`": job_timeout,
        }
        self.assertEqual(0, promote.CI_TIMEOUT % 60)
        for pattern, value in figures.items():
            with self.subTest(pattern=pattern):
                self.assertEqual([value], re.findall(pattern, section))
        self.assertIn("15-minute limit of `ubuntu-slim` ([GitHub changelog, 2026-01-22](https://github.blog/changelog/2026-01-22-1-vcpu-linux-runner-now-generally-available-in-github-actions/))", section)

    def test_the_manual_procedures_give_prepare_the_question_and_promote(self) -> None:
        tool = (REPOSITORY_ROOT / "docs" / "maintaining.md").read_text(encoding="utf-8")
        for heading in ["Promote v1 by hand", "Roll v1 back"]:
            with self.subTest(section=heading):
                section = tool.split(f"#### {heading}\n", 1)[1].split("\n### ", 1)[0].split("\n#### ", 1)[0]
                self.assertIn("python3 actions/repository-hygiene/promote_v1.py prepare", section)
                self.assertRegex(section, r"Ask the user the (question as printed|printed question)")
                self.assertIn("printed `promote` command" if heading == "Roll v1 back" else "promote_v1.py promote --old <object> --new <commit>", section)
                self.assertNotIn("git push", section, "the tag push goes through promote")
        for subcommand in ["prepare", "promote"]:
            self.assertIn(f"python3 promote_v1.py {subcommand} ", promote.__doc__)
            self.assertIn(f"``{subcommand}``", promote.__doc__)
        parser = promote.build_parser()
        for tokens in (["prepare"], ["prepare", "--commit", "a" * 40], ["promote", "--old=", "--new", "a" * 40]):
            parser.parse_args(tokens)


class WorkflowInvocationTests(unittest.TestCase):
    def test_the_promotion_workflow_invokes_the_script_with_subcommands_and_options_it_accepts(self) -> None:
        invocations = script_invocations(REPOSITORY_ROOT / ".github" / "workflows" / "promote-v1.yml", "promote_v1.py")
        self.assertEqual([tokens[0] for tokens in invocations], ["read-tag", "decide", "move"])
        for tokens in invocations:
            with self.subTest(step=tokens[0]):
                promote.build_parser().parse_args(tokens)


class MoveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Fixture(Path(self.temporary.name))

    def refuse_pushes(self) -> None:
        """Make the origin refuse every push, the way a tag rule or a missing permission does, so no lease is involved."""
        hook = self.fixture.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'fixture rule refuses this push' >&2\nexit 1\n", encoding="utf-8", newline="\n")
        hook.chmod(0o755)

    def move_cli(self, old: str, new: str) -> tuple[int, str, str]:
        summary = Path(self.temporary.name) / "summary.md"
        result = subprocess.run([sys.executable, str(SCRIPT), "move", "--repository-root", str(self.fixture.repo), f"--old={old}", "--new", new, "--summary", str(summary)],
                                capture_output=True, text=True, check=False)
        return result.returncode, summary.read_text(encoding="utf-8"), result.stdout + result.stderr

    def test_an_annotated_tag_is_leased_on_its_tag_object(self) -> None:
        git(self.fixture.repo, "tag", "-f", "-a", "-m", "annotated v1", "v1", self.fixture.v1)
        git(self.fixture.repo, "push", "-q", "--force", "origin", "refs/tags/v1")
        tag_object = self.fixture.remote_v1()
        self.assertNotEqual(tag_object, self.fixture.v1)
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n'})
        git(self.fixture.repo, "push", "-q", "origin", "main")
        decision = promote.decide(self.fixture.repo, commit, self.fixture.v1, pull_request(commit), APP, OWNER, SURFACE, tag_object, ci=lambda: PASSED)
        self.assertTrue(decision.move, decision.reason)
        self.assertEqual(tag_object, decision.old, "the lease names the object the remote ref holds")
        self.assertFalse(promote.move(self.fixture.repo, "origin", self.fixture.v1, commit)[0], "a lease on the peeled commit is rejected")
        self.assertTrue(promote.move(self.fixture.repo, "origin", decision.old, decision.new)[0])
        self.assertEqual(commit, self.fixture.remote_v1())

    def test_the_move_records_old_and_new_sha(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n'})
        git(self.fixture.repo, "push", "-q", "origin", "main")
        moved, summary = promote.move(self.fixture.repo, "origin", self.fixture.v1, commit)
        self.assertTrue(moved, summary)
        self.assertEqual(commit, self.fixture.remote_v1())
        self.assertIn(f"`v1` moved from `{self.fixture.v1}` to `{commit}`.", summary)

    def test_a_tag_that_moved_after_the_decision_rejects_the_push_and_keeps_its_newer_target(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n'})
        git(self.fixture.repo, "push", "-q", "origin", "main")
        observed = self.fixture.v1
        newer = self.fixture.commit("chore: another promotion", {"src/app.py": "print(9)\n"})
        git(self.fixture.repo, "push", "-q", "origin", "main")
        git(self.fixture.repo, "push", "-q", "--force", "origin", f"{newer}:refs/tags/v1")
        code, text, output = self.move_cli(observed, commit)
        self.assertEqual(1, code, output)
        self.assertEqual(newer, self.fixture.remote_v1(), "the newer target survives")
        self.assertIn(f"The push of `v1` to `{commit}` was rejected; the tag no longer names `{observed}` and keeps its newer target.", text)
        self.assertIn("stale info", text, "the summary keeps git's error")

    def test_a_push_that_fails_while_v1_keeps_its_object_says_so_and_not_that_the_tag_moved(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n'})
        git(self.fixture.repo, "push", "-q", "origin", "main")
        self.refuse_pushes()
        code, text, output = self.move_cli(self.fixture.v1, commit)
        self.assertEqual(1, code, output)
        self.assertEqual(self.fixture.v1, self.fixture.remote_v1())
        self.assertIn(f"The push of `v1` to `{commit}` failed; `v1` still names `{self.fixture.v1}`.", text)
        outcome = text.partition("```")[0]
        self.assertNotIn("rejected", outcome)
        self.assertNotIn("newer target", outcome)
        self.assertNotIn("no longer names", outcome)
        self.assertIn("fixture rule refuses this push", text, "the summary keeps git's error")

    def test_a_failed_first_creation_without_a_remote_tag_says_that_no_v1_exists(self) -> None:
        git(self.fixture.repo, "push", "-q", "origin", ":refs/tags/v1")
        self.refuse_pushes()
        code, text, output = self.move_cli("", self.fixture.v1)
        self.assertEqual(1, code, output)
        self.assertIn(f"The push of `v1` to `{self.fixture.v1}` failed; no `v1` exists.", text)
        self.assertNotIn("rejected", text.partition("```")[0])
        self.assertIn("fixture rule refuses this push", text, "the summary keeps git's error")

    def test_a_failed_push_whose_remote_tag_cannot_be_read_again_leaves_the_state_unknown(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n'})
        git(self.fixture.repo, "push", "-q", "origin", "main")
        self.refuse_pushes()
        summary = Path(self.temporary.name) / "summary.md"

        def unreadable(root: Path, remote: str) -> str:
            raise RuntimeError("git ls-remote failed: network down")

        with mock.patch.object(promote, "remote_object", unreadable):
            code = promote.main(["move", "--repository-root", str(self.fixture.repo), "--old", self.fixture.v1, "--new", commit, "--summary", str(summary)])
        self.assertEqual(1, code)
        text = summary.read_text(encoding="utf-8")
        self.assertIn(f"The push of `v1` to `{commit}` failed and `refs/tags/v1` could not be read again, so the state of `v1` is unknown.", text)
        self.assertIn("`git ls-remote origin refs/tags/v1`", text)
        self.assertIn("network down", text)
        self.assertNotIn("rejected", text.partition("```")[0])
        self.assertIn("fixture rule refuses this push", text, "the summary keeps git's error")


RAN = {workflow: promote.CiRun("completed", "success") for workflow in promote.CI_WORKFLOWS}
CLEAN = promote.ScanResult(True, "no leaks found")
COMMAND = "python3 actions/repository-hygiene/promote_v1.py promote"


class ManualMove(unittest.TestCase):
    """A fixture whose remote ``v1`` names the baseline, with the CI read and the Gitleaks scan injected."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = Fixture(self.root)
        self.surface_file = self.root / "surface.json"
        self.surface_file.write_text(json.dumps(SURFACE), encoding="utf-8")
        self.ci_reads: list[str] = []
        self.scans: list[str] = []
        self.ci_result: dict = dict(RAN)
        self.scan_result = CLEAN

    def ci(self, repository: str, commit: str) -> dict:
        self.ci_reads.append(commit)
        return self.ci_result

    def scan(self, root: Path, remote: str, commit: str) -> promote.ScanResult:
        self.scans.append(commit)
        return self.scan_result

    def run_main(self, *arguments: str) -> tuple[int, str]:
        patches = [mock.patch.object(promote, "SURFACE_PATH", self.surface_file), mock.patch.object(promote, "github_ci", self.ci),
                   mock.patch.object(promote, "gitleaks_scan", self.scan), mock.patch.object(promote, "github_repository", lambda root, remote: Api.REPOSITORY)]
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
        self.assertIn("validate.yml: success", output)
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
                self.ci_result = {"validate.yml": promote.CiRun(status, None), "repository-hygiene-caller.yml": promote.CiRun("completed", "success")}
                code, output = self.prepare()
                self.assert_refused(code, output, f"CI is still running: validate.yml has not completed ({status}); run prepare again once it has.")
                self.assertNotIn("did not pass", output, "a running CI is not a failure")
                self.assertEqual(1, len(self.ci_reads))

    def test_ci_that_did_not_pass_or_has_no_run_is_refused_with_the_workflow_and_its_conclusion(self) -> None:
        self.shared_change()
        for runs, reason in [
            ({"validate.yml": promote.CiRun("completed", "failure")}, "the CI of the target on main did not pass: validate.yml concluded failure."),
            ({"repository-hygiene-caller.yml": promote.CiRun("completed", "cancelled")}, "the CI of the target on main did not pass: repository-hygiene-caller.yml concluded cancelled."),
            ({"validate.yml": promote.CiRun(None, None)}, "the CI of the target on main did not pass: validate.yml has no push run on main for the target."),
        ]:
            with self.subTest(reason=reason):
                self.ci_result = {**RAN, **runs}
                code, output = self.prepare()
                self.assert_refused(code, output, reason)

    def test_ci_that_cannot_be_read_is_refused(self) -> None:
        self.shared_change()

        def unreadable(repository: str, commit: str) -> dict:
            raise RuntimeError("gh api failed: HTTP 502")

        self.ci = unreadable  # type: ignore[method-assign]
        code, output = self.prepare()
        self.assert_refused(code, output, "the CI runs of the target on main could not be read: gh api failed: HTTP 502")

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


class ManualMoveToolTests(unittest.TestCase):
    def completed(self, stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")

    def test_the_ci_read_runs_gh_api_once_per_workflow_with_its_own_authentication(self) -> None:
        commit = "e" * 40
        body = json.dumps({"workflow_runs": [{"id": 2, "status": "in_progress", "conclusion": None}, {"id": 1, "status": "completed", "conclusion": "failure"}]})
        with mock.patch.object(promote.subprocess, "run", return_value=self.completed(body)) as run:
            runs = promote.github_ci(Api.REPOSITORY, commit)
        self.assertEqual({workflow: promote.CiRun("in_progress", None) for workflow in promote.CI_WORKFLOWS}, runs)
        expected = [mock.call(["gh", "api", "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28",
                               f"repos/{Api.REPOSITORY}/actions/workflows/{workflow}/runs?head_sha={commit}&event=push&branch=main&per_page=100"],
                              capture_output=True, text=True, check=False) for workflow in promote.CI_WORKFLOWS]
        self.assertEqual(expected, run.call_args_list, "no env and no token reach gh")
        with mock.patch.object(promote.subprocess, "run", return_value=self.completed(json.dumps({"workflow_runs": []}))):
            self.assertEqual({workflow: promote.CiRun(None, None) for workflow in promote.CI_WORKFLOWS}, promote.github_ci(Api.REPOSITORY, commit))
        with mock.patch.object(promote.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="HTTP 502")):
            with self.assertRaisesRegex(RuntimeError, "HTTP 502"):
                promote.github_ci(Api.REPOSITORY, commit)

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
