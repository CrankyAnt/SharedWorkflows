"""The automatic v1 move: the tag read against a mocked API, the decision over a local fixture repository and the leased tag push."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import re
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
        root = Path(self.temporary.name)
        summary = root / "summary.md"
        result = subprocess.run([sys.executable, str(SCRIPT), "move", "--repository-root", str(self.fixture.repo), "--old", observed, "--new", commit, "--summary", str(summary)], capture_output=True, text=True, check=False)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertEqual(newer, self.fixture.remote_v1(), "the newer target survives")
        self.assertIn(f"The push of `v1` to `{commit}` was rejected; the tag no longer names `{observed}`", summary.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
