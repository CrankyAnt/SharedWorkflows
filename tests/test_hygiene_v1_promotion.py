"""The automatic and the manual v1 move: the tag read against a mocked API, the decision by promotion class, the wait
for the CI on ``main`` and the preparation over local fixture repositories, the leased tag push, and the update of the
management repository's caller with the start of the sync behind a stand-in for the ``gh`` CLI."""

from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shlex
import subprocess
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from tests import temporary_support
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
MANAGEMENT = "CrankyAnt/ManagementFixture"
# A read workflow and a write workflow, a part folder in the read class and the templates in it.
SURFACE = {
    "pins": "ci/pins.json",
    "updater_branch": "chore/update-ci-pins",
    "paths": [".github/workflows/checks.yml", ".github/workflows/hygiene.yml", "ci"],
    "classes": {".github/workflows/checks.yml": "read", ".github/workflows/hygiene.yml": "write", "ci": "read", "ci/kit": "template", "ci/pins.json": "write"},
}
APP = "pins-updater[bot]"
OWNER = "CrankyAnt"
PASSED = "success"


def git(repo: Path, *arguments: str) -> str:
    result = subprocess.run(["git", *GIT_IDENTITY, "-C", str(repo), *arguments], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {result.stderr}")
    return result.stdout.strip()


def pull_request(commit: str, *, author: str = APP, branch: str = "chore/update-ci-pins", merged_by: str = OWNER, merged: bool = True) -> dict:
    return {"number": 7, "merged": merged, "merge_commit_sha": commit, "user": {"login": author, "type": "Bot"}, "head": {"ref": branch}, "merged_by": {"login": merged_by}}


class Fixture:
    """A bare origin with v1 at the first commit, and a clone where further commits are made."""

    @temporary_support.measured("v1-promotion")
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

    def check_runs(self, commit: str, runs: list[dict]) -> None:
        self.responses[promote.CHECK_RUNS_URL.format(repository=self.REPOSITORY, commit=commit)] = {"total_count": len(runs), "check_runs": runs}

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
        temporary = temporary_support.Directory()
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
        self.temporary = temporary_support.Directory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Fixture(Path(self.temporary.name))

    def decide(self, commit: str, v1: str | None = None, surface: dict = SURFACE) -> promote.Decision:
        return promote.decide(self.fixture.repo, commit, self.fixture.v1 if v1 is None else v1, surface)

    def test_a_commit_whose_changes_compared_with_v1_stay_in_the_read_class_is_followed_by_v1(self) -> None:
        self.fixture.commit("feat: change a resource", {"ci/checks/check.cjs": "// 2\n"})
        commit = self.fixture.commit("feat: change the read workflow", {".github/workflows/checks.yml": "name: checks\n"})
        decision = self.decide(commit)
        self.assertTrue(decision.follow, decision.reason)
        self.assertEqual((self.fixture.v1, commit), (decision.old, decision.new))
        self.assertEqual(f"## v1 promotion\n\n`v1` follows `{commit}` once its CI on main passed: its changes compared with `v1` stay in the read class: "
                         f".github/workflows/checks.yml, ci/checks/check.cjs. `v1` follows from `{self.fixture.v1}` once the CI of this commit on main passed.\n",
                         decision.summary())

    def test_other_commits_between_v1_and_the_commit_are_fine_when_they_leave_the_shared_surface_alone(self) -> None:
        self.fixture.commit("feat: product change", {"src/app.py": "print(2)\n", "docs/notes.md": "x\n"})
        commit = self.fixture.commit("feat: change a resource", {"ci/checks/check.cjs": "// 2\n", "src/app.py": "print(3)\n"})
        self.assertTrue(self.decide(commit).follow)

    def test_a_commit_that_changes_the_write_class_keeps_v1_and_states_the_question_for_a_promotion_by_hand(self) -> None:
        commit = self.fixture.commit("feat: change the write workflow", {".github/workflows/hygiene.yml": "name: hygiene 2\n"})
        decision = self.decide(commit)
        self.assertFalse(decision.follow)
        self.assertEqual("the changes compared with v1 leave the read class: .github/workflows/hygiene.yml (write class).", decision.reason)
        question = (f"Question: Move v1 from {self.fixture.v1} to {commit}, a promotion that changes the shared-surface files "
                    ".github/workflows/hygiene.yml (write class)? The move changes the later runs of every caller. It changes no shared digest. Answer yes or no.")
        self.assertEqual(f"## v1 promotion\n\n`v1` stays at `{self.fixture.v1}`: {decision.reason} A promotion by hand carries this commit.\n\n{question}\n\n"
                         "`prepare` asks the question with the facts of the move:\n\n```text\n"
                         f"python3 actions/repository-hygiene/promote_v1.py prepare --commit {commit}\n```\n", decision.summary())

    def test_a_commit_that_changes_the_template_class_keeps_v1_and_its_question_names_the_sync(self) -> None:
        commit = self.fixture.commit("feat: change a template", {"ci/kit/caller.yml": "name: caller\n", "ci/checks/check.cjs": "// 2\n"})
        decision = self.decide(commit)
        self.assertFalse(decision.follow)
        self.assertEqual("the changes compared with v1 leave the read class: ci/kit/caller.yml (template class).", decision.reason)
        self.assertIn("that changes the shared-surface files ci/checks/check.cjs (read class); ci/kit/caller.yml (template class)? "
                      "The move changes the later runs of every caller. It changes no shared digest. It changes the template class, so right after it the management repository's caller is "
                      "replaced when it differs from the caller at the target, and the sync starts. Answer yes or no.",
                      decision.summary())

    def test_an_earlier_change_outside_the_read_class_keeps_a_later_read_class_commit_from_moving(self) -> None:
        self.fixture.commit("feat: change the write workflow", {".github/workflows/hygiene.yml": "name: hygiene 2\n"})
        commit = self.fixture.commit("feat: change a resource", {"ci/checks/check.cjs": "// 2\n"})
        decision = self.decide(commit)
        self.assertFalse(decision.follow, "the changes are compared with v1, not with the commit before")
        self.assertIn(".github/workflows/hygiene.yml (write class)", decision.reason)
        self.assertIn("ci/checks/check.cjs (read class); .github/workflows/hygiene.yml (write class)?", decision.question)

    def test_a_merged_dependabot_pull_request_moves_v1_only_when_its_changes_stay_in_the_read_class(self) -> None:
        # Dependabot's merge commit is a commit on main like any other: what it changes decides, not who made it.
        read = self.fixture.commit("build(deps): bump the action of the read workflow", {".github/workflows/checks.yml": "uses: actions/checkout@v8\n"})
        self.assertTrue(self.decide(read).follow)
        write = self.fixture.commit("build(deps): bump the action of the write workflow", {".github/workflows/hygiene.yml": "uses: actions/checkout@v8\n"})
        decision = self.decide(write)
        self.assertFalse(decision.follow)
        self.assertIn(".github/workflows/hygiene.yml (write class)", decision.reason)
        self.assertIn("Question: Move v1 from", decision.summary())

    def test_a_changed_path_without_a_promotion_class_keeps_v1(self) -> None:
        surface = {**SURFACE, "paths": [*SURFACE["paths"], "extra"]}
        commit = self.fixture.commit("feat: a new part", {"extra/step.py": "x = 1\n", "ci/checks/check.cjs": "// 2\n"})
        decision = self.decide(commit, surface=surface)
        self.assertFalse(decision.follow)
        self.assertEqual("the changes compared with v1 reach extra/step.py, which the shared surface gives no promotion class.", decision.reason)
        self.assertTrue(decision.summary().endswith("A manual promotion is needed for this commit.\n"), decision.summary())
        unknown = {**SURFACE, "classes": {**SURFACE["classes"], "ci": "safe"}}
        self.assertFalse(self.decide(commit, surface=unknown).follow, "a class that is none of the three is no class")

    def test_a_missing_or_unrelated_v1_keeps_v1(self) -> None:
        commit = self.fixture.commit("feat: change a resource", {"ci/checks/check.cjs": "// 2\n"})
        self.assertIn("no v1 tag exists yet", self.decide(commit, v1="").reason)
        self.assertIn("is not available in this checkout", self.decide(commit, v1="1" * 40).reason)
        git(self.fixture.repo, "switch", "-q", "-c", "elsewhere", self.fixture.v1)
        stray = self.fixture.commit("feat: stray", {"ci/checks/check.cjs": "// stray\n"})
        git(self.fixture.repo, "switch", "-q", "main")
        decision = self.decide(commit, v1=stray)
        self.assertFalse(decision.follow)
        self.assertIn("is not an ancestor of this commit", decision.reason)
        self.assertTrue(decision.summary().endswith("A manual promotion is needed for this commit.\n"), decision.summary())

    def test_a_stay_with_an_identical_shared_surface_needs_no_promotion(self) -> None:
        commit = self.fixture.commit("feat: product change", {"src/app.py": "print(2)\n", "docs/notes.md": "x\n"})
        decision = self.decide(commit)
        self.assertFalse(decision.follow)
        self.assertEqual((self.fixture.v1, commit), (decision.old, decision.new))
        self.assertEqual(f"## v1 promotion\n\n`v1` stays at `{self.fixture.v1}`: the shared surface is identical between `v1` and this commit. No promotion is needed for this commit.\n", decision.summary())

    def test_an_identical_shared_surface_names_the_tag_object_of_an_annotated_v1(self) -> None:
        commit = self.fixture.commit("feat: product change", {"src/app.py": "print(2)\n"})
        tag_object = "b" * 40
        decision = promote.decide(self.fixture.repo, commit, self.fixture.v1, SURFACE, tag_object)
        self.assertEqual((False, tag_object, commit), (decision.follow, decision.old, decision.new))
        self.assertEqual(f"## v1 promotion\n\n`v1` stays at `{tag_object}`: the shared surface is identical between `v1` and this commit. No promotion is needed for this commit.\n", decision.summary())

    def test_an_identical_shared_surface_needs_no_promotion_when_v1_is_not_an_ancestor(self) -> None:
        git(self.fixture.repo, "switch", "-q", "-c", "elsewhere", self.fixture.v1)
        stray = self.fixture.commit("feat: stray", {"src/other.py": "x\n"})
        git(self.fixture.repo, "switch", "-q", "main")
        commit = self.fixture.commit("feat: product change", {"src/app.py": "print(2)\n"})
        decision = self.decide(commit, v1=stray)
        self.assertFalse(decision.follow)
        self.assertEqual((stray, commit), (decision.old, decision.new))
        self.assertTrue(decision.summary().endswith("No promotion is needed for this commit.\n"), decision.summary())

    def test_a_stay_without_a_usable_v1_asks_for_a_manual_promotion(self) -> None:
        commit = self.fixture.commit("feat: product change", {"src/app.py": "print(2)\n"})
        for v1, target, reason in [("", "no v1 tag", "no v1 tag exists yet."), ("1" * 40, "1" * 40, f"the current v1 commit {'1' * 40} is not available in this checkout.")]:
            with self.subTest(v1=v1):
                decision = self.decide(commit, v1=v1)
                self.assertFalse(decision.follow)
                self.assertEqual((v1, commit), (decision.old, decision.new))
                self.assertEqual(f"## v1 promotion\n\n`v1` stays at `{target}`: {reason} A manual promotion is needed for this commit.\n", decision.summary())

    def test_a_stay_whose_comparison_fails_asks_for_a_manual_promotion(self) -> None:
        unknown = "0" * 40
        decision = self.decide(unknown)
        self.assertEqual((False, self.fixture.v1, unknown), (decision.follow, decision.old, decision.new))
        self.assertTrue(decision.summary().endswith("A manual promotion is needed for this commit.\n"), decision.summary())

    def command(self, commit: str, root: Path) -> list[str]:
        (root / "surface.json").write_text(json.dumps(SURFACE), encoding="utf-8")
        return [sys.executable, str(SCRIPT), "decide", "--repository-root", str(self.fixture.repo), "--commit", commit, "--v1", self.fixture.v1,
                "--surface", str(root / "surface.json"), "--output", str(root / "output.txt"), "--summary", str(root / "summary.md")]

    def test_the_cli_writes_outputs_and_the_summary_and_reads_no_ci(self) -> None:
        commit = self.fixture.commit("feat: change a resource", {"ci/checks/check.cjs": "// 2\n"})
        root = Path(self.temporary.name)
        environment = {key: value for key, value in os.environ.items() if key != "GH_TOKEN"}
        result = subprocess.run(self.command(commit, root), capture_output=True, text=True, check=False, env=environment)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(f"follow=true\nold={self.fixture.v1}\nnew={commit}\n", (root / "output.txt").read_text(encoding="utf-8"))
        summary = (root / "summary.md").read_text(encoding="utf-8")
        self.assertIn(f"`v1` follows `{commit}` once its CI on main passed", summary)
        self.assertIn(summary, result.stdout)

    def test_the_cli_states_the_question_of_a_commit_outside_the_read_class(self) -> None:
        commit = self.fixture.commit("feat: change the write workflow", {".github/workflows/hygiene.yml": "name: hygiene 2\n"})
        root = Path(self.temporary.name)
        result = subprocess.run(self.command(commit, root), capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual(f"follow=false\nold={self.fixture.v1}\nnew={commit}\n", (root / "output.txt").read_text(encoding="utf-8"))
        summary = (root / "summary.md").read_text(encoding="utf-8")
        self.assertIn(f"Question: Move v1 from {self.fixture.v1} to {commit}, a promotion", summary)
        self.assertIn("Answer yes or no.", summary)

    def test_the_cli_reports_an_identical_shared_surface(self) -> None:
        commit = self.fixture.commit("feat: product change", {"src/app.py": "print(2)\n"})
        root = Path(self.temporary.name)
        result = subprocess.run(self.command(commit, root), capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        expected = f"## v1 promotion\n\n`v1` stays at `{self.fixture.v1}`: the shared surface is identical between `v1` and this commit. No promotion is needed for this commit.\n"
        self.assertEqual(expected, (root / "summary.md").read_text(encoding="utf-8"))
        self.assertIn(expected, result.stdout)
        self.assertNotIn("A manual promotion is needed", result.stdout)
        self.assertEqual(f"follow=false\nold={self.fixture.v1}\nnew={commit}\n", (root / "output.txt").read_text(encoding="utf-8"))


class PinsExceptionTests(unittest.TestCase):
    """The pins file is in the write class, and one commit that changes it still moves automatically: the merge commit of
    the updater App's pull request from the updater branch, merged by the repository owner, that changes only the pins file."""

    PINS = {"ci/pins.json": '{"version": "2"}\n'}

    def setUp(self) -> None:
        self.temporary = temporary_support.Directory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Fixture(Path(self.temporary.name))

    def decide(self, commit: str, pr: dict | None, app: str = APP, owner: str = OWNER) -> promote.Decision:
        return promote.decide(self.fixture.repo, commit, self.fixture.v1, SURFACE, pull_request=pr, app_login=app, owner=owner)

    def test_the_pins_pull_request_of_the_updater_app_merged_by_the_owner_is_followed_by_v1(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", self.PINS)
        decision = self.decide(commit, pull_request(commit))
        self.assertTrue(decision.follow, decision.reason)
        self.assertEqual(f"## v1 promotion\n\n`v1` follows `{commit}` once its CI on main passed: a pins-only pull request of the updater App from chore/update-ci-pins, "
                         f"merged by the repository owner, landed, and the shared surface otherwise stays in the read class compared with `v1`. `v1` follows from "
                         f"`{self.fixture.v1}` once the CI of this commit on main passed.\n", decision.summary())

    def test_read_class_changes_between_v1_and_the_pins_commit_do_not_keep_it_from_moving(self) -> None:
        self.fixture.commit("feat: change a resource", {"ci/checks/check.cjs": "// 2\n", "src/app.py": "print(2)\n"})
        commit = self.fixture.commit("chore: update CI tool pins", self.PINS)
        self.assertTrue(self.decide(commit, pull_request(commit)).follow)

    def test_a_pins_commit_without_the_pull_request_facts_is_a_question_like_any_write_class_change(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", self.PINS)
        decision = promote.decide(self.fixture.repo, commit, self.fixture.v1, SURFACE)
        self.assertFalse(decision.follow)
        self.assertEqual("the changes compared with v1 leave the read class: ci/pins.json (write class). The pins file moves automatically only with the updater's "
                         "pull request, and no merged pull request landed this commit.", decision.reason)
        self.assertIn("Question: Move v1 from", decision.summary())

    def test_each_condition_of_the_pins_exception_is_needed(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", self.PINS)
        for name, pr, app, owner, fragment in [
            ("no pull request", None, APP, OWNER, "no merged pull request landed this commit"),
            ("a Dependabot pull request", pull_request(commit, author="dependabot[bot]", branch="dependabot/pip/pins"), APP, OWNER,
             "opened by dependabot[bot], not by the updater App pins-updater[bot]"),
            ("another author", pull_request(commit, author="someone"), APP, OWNER, "opened by someone, not by the updater App pins-updater[bot]"),
            ("another branch", pull_request(commit, branch="feat/other"), APP, OWNER, "came from feat/other, not from the updater branch chore/update-ci-pins"),
            ("another merger", pull_request(commit, merged_by="colleague"), APP, OWNER, "merged by colleague, not by the repository owner CrankyAnt"),
            ("merged by the App itself", pull_request(commit, merged_by=APP), APP, OWNER, "merged by pins-updater[bot], not by the repository owner CrankyAnt"),
            ("not merged", pull_request(commit, merged=False), APP, OWNER, "not the merged pull request that produced it"),
            ("another merge commit", pull_request("0" * 40), APP, OWNER, "not the merged pull request that produced it"),
            ("an unknown App login", pull_request(commit), "", OWNER, "not by the updater App (unknown)"),
            ("an unknown owner", pull_request(commit), APP, "", "not by the repository owner (unknown)"),
        ]:
            with self.subTest(name=name):
                decision = self.decide(commit, pr, app, owner)
                self.assertFalse(decision.follow)
                self.assertIn(fragment, decision.reason)
                self.assertIn("ci/pins.json (write class)", decision.reason)
                self.assertIn("Question: Move v1 from", decision.summary(), "the stay is the question of any write-class change")

    def test_the_exception_covers_the_pins_file_alone(self) -> None:
        for name, files, expected in [
            ("the pins file and a product file", {**self.PINS, "src/app.py": "print(3)\n"}, "the commit changes ci/pins.json, src/app.py, not only ci/pins.json"),
            ("the pins file and a read-class file", {**self.PINS, "ci/checks/check.cjs": "// 3\n"}, "the commit changes ci/checks/check.cjs, ci/pins.json, not only ci/pins.json"),
            ("the pins file and the write workflow", {**self.PINS, ".github/workflows/hygiene.yml": "name: hygiene 2\n"}, ".github/workflows/hygiene.yml, ci/pins.json (write class)"),
            ("the write workflow alone", {".github/workflows/hygiene.yml": "name: hygiene 3\n"}, ".github/workflows/hygiene.yml (write class)"),
            ("a template alone", {"ci/kit/caller.yml": "name: caller\n"}, "ci/kit/caller.yml (template class)"),
        ]:
            with self.subTest(name=name):
                fixture = Fixture(temporary_support.make_directory(self.addCleanup))
                commit = fixture.commit("chore: update CI tool pins", files)
                decision = promote.decide(fixture.repo, commit, fixture.v1, SURFACE, pull_request=pull_request(commit), app_login=APP, owner=OWNER)
                self.assertFalse(decision.follow, "the updater's pull request moves nothing but a pins-only commit")
                self.assertIn(expected, decision.reason)

    def test_another_write_class_change_between_v1_and_the_pins_commit_keeps_v1(self) -> None:
        self.fixture.commit("feat: change the write workflow", {".github/workflows/hygiene.yml": "name: hygiene 2\n"})
        commit = self.fixture.commit("chore: update CI tool pins", self.PINS)
        decision = self.decide(commit, pull_request(commit))
        self.assertFalse(decision.follow)
        self.assertEqual("the changes compared with v1 leave the read class: .github/workflows/hygiene.yml, ci/pins.json (write class).", decision.reason)

    def test_a_merged_dependabot_pull_request_in_the_write_class_stays_a_question_with_every_fact_of_the_updater(self) -> None:
        commit = self.fixture.commit("build(deps): bump the action of the write workflow", {".github/workflows/hygiene.yml": "uses: actions/checkout@v8\n"})
        decision = self.decide(commit, pull_request(commit))
        self.assertFalse(decision.follow, "the exception names the pins file, not the pull request")

    def test_the_cli_takes_the_pull_request_the_app_login_and_the_owner(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", self.PINS)
        root = Path(self.temporary.name)
        (root / "surface.json").write_text(json.dumps(SURFACE), encoding="utf-8")
        (root / "pr.json").write_text(json.dumps(pull_request(commit)), encoding="utf-8")
        base = [sys.executable, str(SCRIPT), "decide", "--repository-root", str(self.fixture.repo), "--commit", commit, "--v1", self.fixture.v1,
                "--surface", str(root / "surface.json")]
        for name, extra, expected in [
            ("the updater's pull request", ["--pull-request", str(root / "pr.json"), "--app-login", APP, "--owner", OWNER], "follow=true"),
            ("a missing pull request file", ["--pull-request", str(root / "missing.json"), "--app-login", APP, "--owner", OWNER], "follow=false"),
            ("no App login", ["--pull-request", str(root / "pr.json"), "--owner", OWNER], "follow=false"),
            ("no facts at all", [], "follow=false"),
        ]:
            with self.subTest(name=name):
                output = root / "output.txt"
                output.unlink(missing_ok=True)
                result = subprocess.run([*base, *extra, "--output", str(output)], capture_output=True, text=True, check=False)
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                self.assertEqual(f"{expected}\nold={self.fixture.v1}\nnew={commit}\n", output.read_text(encoding="utf-8"))

    def test_the_exception_names_the_pins_file_and_the_updater_branch_of_the_shared_surface(self) -> None:
        surface = json.loads((RESOURCES / "shared-surface.json").read_text(encoding="utf-8"))
        self.assertEqual(("actions/repository-hygiene/pins.json", "chore/update-ci-pins"), (surface["pins"], surface["updater_branch"]))
        self.assertEqual("write", promote.class_of(surface["pins"], surface["classes"]), "the exception, not the read class, moves a pins update")
        elsewhere = {**SURFACE, "pins": "ci/checks/check.cjs"}
        commit = self.fixture.commit("chore: update CI tool pins", self.PINS)
        decision = promote.decide(self.fixture.repo, commit, self.fixture.v1, elsewhere, pull_request=pull_request(commit), app_login=APP, owner=OWNER)
        self.assertFalse(decision.follow, "only the file that the record names as pins takes the exception")


class FollowTests(unittest.TestCase):
    """Whether ``v1`` follows the commit: the completed ``ci / verdict`` check run for it, in this repository."""

    COMMIT, OLD = "d" * 40, "a" * 40

    def setUp(self) -> None:
        self.api = Api()
        self.api.check_runs(self.COMMIT, [{"id": 1, "name": "ci / verdict", "status": "completed", "conclusion": "success", "app": {"id": 15368}}])
        self.temporary = temporary_support.Directory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def follow(self, *, expected_code: int = 0) -> tuple[str, str, str]:
        """Run ``await`` through its command line; the output file, the summary and what it printed."""
        output, summary = self.root / "output.txt", self.root / "summary.md"
        for path in (output, summary):
            path.unlink(missing_ok=True)
        real_wait = promote.wait_for_verdict

        def wait(*arguments, **options) -> str | None:
            return real_wait(*arguments, clock=lambda: self.now, sleep=self.sleep, **options)

        with mock.patch.object(promote, "fetch", self.api.fetch), mock.patch.object(promote, "wait_for_verdict", wait), contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = promote.main(["await", "--commit", self.COMMIT, "--old", self.OLD, "--repository", Api.REPOSITORY, "--output", str(output), "--summary", str(summary)])
        self.assertEqual(expected_code, code)
        return output.read_text(encoding="utf-8"), summary.read_text(encoding="utf-8"), stdout.getvalue()

    def assert_stays(self, reason: str) -> None:
        output, summary, _ = self.follow(expected_code=1)
        self.assertEqual(f"move=false\nold={self.OLD}\nnew={self.COMMIT}\n", output)
        self.assertEqual(f"## v1 promotion\n\n`v1` stays at `{self.OLD}`: {reason} A manual promotion is needed for this commit.\n", summary)

    def test_v1_follows_once_the_verdict_concluded_success_and_no_other_repository_is_read(self) -> None:
        output, summary, printed = self.follow()
        self.assertEqual(f"move=true\nold={self.OLD}\nnew={self.COMMIT}\n", output)
        self.assertEqual(f"## v1 promotion\n\n`v1` moves from `{self.OLD}` to `{self.COMMIT}`: the verdict `ci / verdict` of this commit passed.\n", summary)
        self.assertIn(summary, printed)
        self.assertEqual([], self.sleeps)
        self.assertEqual([promote.CHECK_RUNS_URL.format(repository=Api.REPOSITORY, commit=self.COMMIT)],
                         self.api.requests, "the wait reads the verdict check run of this repository and nothing else")

    def test_a_verdict_that_did_not_pass_fails_await_and_keeps_v1_with_the_reason(self) -> None:
        for conclusion in ("failure", "cancelled", "timed_out", "skipped"):
            with self.subTest(conclusion=conclusion):
                self.setUp()
                self.api.check_runs(self.COMMIT, [{"id": 1, "name": "ci / verdict", "status": "completed", "conclusion": conclusion, "app": {"id": 15368}}])
                self.assert_stays(f"the verdict `ci / verdict` of this commit did not pass: concluded {conclusion}.")

    def test_the_newest_verdict_decides_when_a_run_was_repeated(self) -> None:
        self.api.check_runs(self.COMMIT, [
            {"id": 1, "name": "ci / verdict", "status": "completed", "conclusion": "success", "app": {"id": 15368}},
            {"id": 2, "name": "ci / verdict", "status": "completed", "conclusion": "failure", "app": {"id": 15368}},
        ])
        self.assert_stays("the verdict `ci / verdict` of this commit did not pass: concluded failure.")

    def test_a_verdict_from_another_app_is_ignored(self) -> None:
        self.api.check_runs(self.COMMIT, [{"id": 9, "name": "ci / verdict", "status": "completed", "conclusion": "success", "app": {"id": 99999}}])
        self.assert_stays("the verdict `ci / verdict` of this commit had not completed when the wait ended.")

    def test_a_verdict_that_has_not_completed_or_does_not_exist_when_the_wait_ends_fails_await_and_keeps_v1(self) -> None:
        for name, runs in (("running", [{"id": 1, "name": "ci / verdict", "status": "in_progress", "conclusion": None, "app": {"id": 15368}}]), ("no run", [])):
            with self.subTest(name=name):
                self.now, self.sleeps = 0.0, []
                self.api.check_runs(self.COMMIT, runs)
                self.assert_stays("the verdict `ci / verdict` of this commit had not completed when the wait ended.")
                self.assertEqual(promote.CI_TIMEOUT - promote.REQUEST_TIMEOUT, sum(self.sleeps), "a poll reads the verdict check run")

    def test_runs_that_could_not_be_read_on_the_last_poll_fail_await_and_keep_v1_with_the_reason(self) -> None:
        def unreachable(url: str) -> bytes | None:
            raise urllib.error.URLError("temporary failure in name resolution")

        self.api.fetch = unreachable  # type: ignore[method-assign]
        self.assert_stays("the verdict `ci / verdict` of this commit could not be read when the wait ended: URLError: <urlopen error temporary failure in name resolution>.")

    def test_runs_that_could_not_be_read_when_the_wait_ended_keep_v1_with_the_error(self) -> None:
        def unreadable() -> str | None:
            raise promote.CiReadError("HTTPError: HTTP Error 502: Bad Gateway")

        outcome = promote.follow(self.OLD, self.COMMIT, ci=unreadable)
        self.assertFalse(outcome.move)
        self.assertEqual("the verdict `ci / verdict` of this commit could not be read when the wait ended: HTTPError: HTTP Error 502: Bad Gateway.", outcome.reason)


class CiWaitTests(unittest.TestCase):
    COMMIT = "d" * 40

    def setUp(self) -> None:
        self.api = Api()
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def wait(self, fetcher=None, timeout: float = 600, interval: float = 30, request_timeout: float = 5) -> str | None:
        return promote.wait_for_verdict(Api.REPOSITORY, self.COMMIT, fetcher or self.api.fetch, timeout=timeout, interval=interval,
                                        request_timeout=request_timeout, clock=lambda: self.now, sleep=self.sleep)

    def test_the_newest_verdict_check_run_gives_its_conclusion(self) -> None:
        self.api.check_runs(self.COMMIT, [
            {"id": 9, "name": "ci / verdict", "status": "completed", "conclusion": "success", "app": {"id": 15368}},
            {"id": 4, "name": "ci / verdict", "status": "completed", "conclusion": "failure", "app": {"id": 15368}},
        ])
        conclusion = self.wait()
        self.assertEqual(PASSED, conclusion)
        self.assertIsNone(promote.verdict_stay_reason(conclusion))
        self.assertEqual([], self.sleeps)
        self.assertEqual([promote.CHECK_RUNS_URL.format(repository=Api.REPOSITORY, commit=self.COMMIT)], self.api.requests)
        for fragment in ["/commits/{commit}/check-runs?", "check_name=ci%20%2F%20verdict"]:
            self.assertIn(fragment, promote.CHECK_RUNS_URL)

    def test_the_wait_polls_until_the_verdict_completed(self) -> None:
        polls = iter([
            [],
            [{"id": 9, "name": "ci / verdict", "status": "in_progress", "conclusion": None, "app": {"id": 15368}}],
            [{"id": 9, "name": "ci / verdict", "status": "completed", "conclusion": "success", "app": {"id": 15368}}],
        ])

        def polling_fetch(url: str) -> bytes | None:
            self.api.check_runs(self.COMMIT, next(polls))
            return self.api.fetch(url)

        self.assertEqual(PASSED, self.wait(polling_fetch))
        self.assertEqual([30, 30], self.sleeps)

    def test_a_verdict_that_concluded_without_success_ends_the_wait_at_once(self) -> None:
        for conclusion in ["failure", "cancelled", "timed_out"]:
            with self.subTest(conclusion=conclusion):
                self.sleeps.clear()
                self.api.check_runs(self.COMMIT, [{"id": 8, "name": "ci / verdict", "status": "completed", "conclusion": conclusion, "app": {"id": 15368}}])
                got = self.wait()
                self.assertEqual(conclusion, got)
                self.assertEqual([], self.sleeps)
                self.assertEqual(f"the verdict `ci / verdict` of this commit did not pass: concluded {conclusion}.", promote.verdict_stay_reason(got))

    def test_a_verdict_that_has_not_completed_within_the_bound_stays_unfinished(self) -> None:
        self.api.check_runs(self.COMMIT, [{"id": 9, "name": "ci / verdict", "status": "in_progress", "conclusion": None, "app": {"id": 15368}}])
        conclusion = self.wait(timeout=95, interval=30, request_timeout=5)
        self.assertIsNone(conclusion)
        self.assertEqual([30, 30, 30], self.sleeps, "the last poll starts when a full request timeout still ends at the bound")
        self.assertEqual("the verdict `ci / verdict` of this commit had not completed when the wait ended.", promote.verdict_stay_reason(conclusion))

    def test_a_failed_read_counts_as_not_completed_and_polling_goes_on(self) -> None:
        failures = iter([urllib.error.URLError("temporary failure in name resolution"), TimeoutError("timed out")])
        self.api.check_runs(self.COMMIT, [{"id": 9, "name": "ci / verdict", "status": "completed", "conclusion": "success", "app": {"id": 15368}}])

        def flaky_fetch(url: str) -> bytes | None:
            failure = next(failures, None)
            if failure:
                raise failure
            return self.api.fetch(url)

        self.assertEqual(PASSED, self.wait(flaky_fetch))
        self.assertEqual([30, 30], self.sleeps)

    def test_a_read_that_still_fails_on_the_last_poll_names_its_error(self) -> None:
        error = urllib.error.HTTPError(promote.CHECK_RUNS_URL, 502, "Bad Gateway", {}, None)  # type: ignore[arg-type]
        self.addCleanup(error.close)

        def failing_fetch(url: str) -> bytes | None:
            raise error

        with self.assertRaises(promote.CiReadError) as raised:
            self.wait(failing_fetch, timeout=95, interval=30, request_timeout=5)
        self.assertEqual("HTTPError: HTTP Error 502: Bad Gateway", str(raised.exception))
        self.assertEqual([30, 30, 30], self.sleeps)

    def test_the_worst_case_of_the_wait_ends_within_the_await_job_timeout(self) -> None:
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "promote-v1.yml").read_text(encoding="utf-8")
        job_timeout = int(re.search(r"(?m)^  await:\n    needs: decide\n    if: needs\.decide\.outputs\.follow == 'true'\n    runs-on: ubuntu-slim\n    timeout-minutes: (\d+)$", workflow).group(1)) * 60
        for outcome in ["pending", "timeout"]:
            with self.subTest(outcome=outcome):
                self.now = 0.0

                def slow_fetch(url: str) -> bytes | None:
                    # Every request takes its full timeout before it answers or fails.
                    self.now += promote.REQUEST_TIMEOUT
                    if outcome == "timeout":
                        raise TimeoutError("timed out")
                    return json.dumps({"check_runs": [{"id": 1, "name": "ci / verdict", "status": "in_progress", "conclusion": None, "app": {"id": 15368}}]}).encode("utf-8")

                try:
                    promote.wait_for_verdict(Api.REPOSITORY, self.COMMIT, slow_fetch, clock=lambda: self.now, sleep=self.sleep)
                except promote.CiReadError:
                    self.assertEqual("timeout", outcome)
                self.assertLessEqual(self.now, promote.CI_TIMEOUT)
        # The checkout and the summary get the rest of the job's time, under the 15-minute limit of ubuntu-slim.
        self.assertGreaterEqual(job_timeout - promote.CI_TIMEOUT, 3 * 60)
        self.assertLess(job_timeout, 15 * 60)


class ToolDocumentTests(unittest.TestCase):
    def test_the_automatic_move_section_states_the_constants_and_the_job_timeout(self) -> None:
        tool = (REPOSITORY_ROOT / "docs" / "maintaining.md").read_text(encoding="utf-8")
        section = tool.split("#### Automatic v1 move\n", 1)[1].split("\n#### ", 1)[0]
        workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "promote-v1.yml").read_text(encoding="utf-8")
        job_timeout = re.search(r"(?m)^  await:\n    needs: decide\n    if: needs\.decide\.outputs\.follow == 'true'\n    runs-on: ubuntu-slim\n    timeout-minutes: (\d+)$", workflow).group(1)
        figures = {
            r"polls the runs every (\d+) seconds": str(promote.CI_INTERVAL),
            r"Each request times out after (\d+) seconds": str(promote.REQUEST_TIMEOUT),
            r"ends within (\d+) minutes of the start of the wait": str(promote.CI_TIMEOUT // 60),
            r"`timeout-minutes: (\d+)`": job_timeout,
        }
        self.assertEqual(0, promote.CI_TIMEOUT % 60)
        for pattern, value in figures.items():
            with self.subTest(pattern=pattern):
                self.assertEqual([value], re.findall(pattern, section))
        self.assertIn("15-minute limit of `ubuntu-slim` ([GitHub changelog, 2026-01-22](https://github.blog/changelog/2026-01-22-1-vcpu-linux-runner-now-generally-available-in-github-actions/))", section)

    def test_the_publication_policy_and_the_maintenance_procedure_state_the_promotion_rules(self) -> None:
        policy = (REPOSITORY_ROOT / "docs" / "agents" / "publication.md").read_text(encoding="utf-8")
        questions = policy.split("**One-way actions that require a user question:**", 1)[1].split("**Standalone PR merge:**", 1)[0]
        for text in ("every promotion that changes the write class or the template class of the shared surface", "every rollback", "the first creation",
                     "every move that changes a check name", "also replaces the management repository's sync caller", "and starts the sync there",
                     "the answer to the question covers that write and that start",
                     "The automatic move of `promote-v1.yml` is not one", "stay in the read class",
                     "after the commit's CI on `main` passed",
                     "A merged Dependabot pull request moves `v1` only that way",
                     "a pins-only pull request that the updater App opened from `chore/update-ci-pins` and the repository owner merged"):
            with self.subTest(document="publication.md", text=text):
                self.assertIn(text, questions)
        tool = (REPOSITORY_ROOT / "docs" / "maintaining.md").read_text(encoding="utf-8")
        channel = tool.split("### The v1 channel\n", 1)[1].split("\n### ", 1)[0]
        surface = json.loads((RESOURCES / "shared-surface.json").read_text(encoding="utf-8"))
        for heading in ("#### Who moves v1", "#### Promotion classes", "#### Automatic v1 move", "#### Promote v1 by hand", "#### Roll v1 back"):
            with self.subTest(heading=heading):
                self.assertEqual(1, channel.count(f"{heading}\n"))
        classes = channel.split("#### Promotion classes\n", 1)[1].split("\n#### ", 1)[0]
        self.assertEqual(["read", "write", "template"], re.findall(r"(?m)^\| (read|write|template) \|", classes), "one row per class")
        for text in ("a Dependabot pull request that merged itself moves `v1` only when its changes stay in the read class",
                     "states the question for a promotion by hand",
                     "starts `sync.yml` in the management repository with `workflow_dispatch`", "git config --local sharedworkflows.managementRepository <owner>/<name>",
                     "replaces the file with one commit only when the two blobs differ", "promote_v1.py caller",
                     "the move replaces the management repository's caller when it differs from the caller at the target",
                     "the merge commit of a merged pull request that the updater App opened from `chore/update-ci-pins`", "the repository owner's account merged it",
                     "the commit changes only `pins.json`", "No other file of the write class and no other pull request takes this exception"):
            with self.subTest(document="maintaining.md", text=text):
                self.assertIn(text, channel)
        self.assertNotIn("#### The proof", channel)
        self.assertEqual(["pins", "updater_branch", "paths", "classes"], list(surface))

    def test_the_manual_procedures_give_prepare_the_question_and_promote(self) -> None:
        tool = (REPOSITORY_ROOT / "docs" / "maintaining.md").read_text(encoding="utf-8")
        for heading in ["Promote v1 by hand", "Roll v1 back"]:
            with self.subTest(section=heading):
                section = tool.split(f"#### {heading}\n", 1)[1].split("\n### ", 1)[0].split("\n#### ", 1)[0]
                self.assertIn("python3 actions/repository-hygiene/promote_v1.py prepare", section)
                self.assertRegex(section, r"Ask the user the (question as printed|printed question)")
                self.assertIn("printed `promote` command" if heading == "Roll v1 back" else "promote_v1.py promote --old <object> --new <commit>", section)
                self.assertNotIn("git push", section, "the tag push goes through promote")
        for subcommand in ["prepare", "promote", "caller"]:
            self.assertIn(f"python3 promote_v1.py {subcommand} ", promote.__doc__)
            self.assertIn(f"``{subcommand}``", promote.__doc__)
        for text in ("brings the sync caller of the management repository in line with ``v1`` and starts the sync there", "replaces the file\nwith one commit only when the two differ",
                     "nothing that ``gh`` returns about it is printed"):
            self.assertIn(text, promote.__doc__)
        parser = promote.build_parser()
        for tokens in (["prepare"], ["prepare", "--commit", "a" * 40], ["promote", "--old=", "--new", "a" * 40], ["caller"], ["caller", "--remote", "upstream"]):
            parser.parse_args(tokens)


# GitHub's Ed25519 host key, as https://api.github.com/meta lists it under ssh_keys.
GITHUB_HOST_KEY = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
KEY_VARIABLE = "FIXTURE_DEPLOY_KEY"
KEY_TEXT = "-----BEGIN FIXTURE KEY-----\nno key\n-----END FIXTURE KEY-----"


class DeployKeyTests(unittest.TestCase):
    """``--deploy-key`` names the variable that holds the key; every push of the subcommand then goes over SSH with that
    key alone and a pinned host key, and nothing of it outlives the subcommand."""

    PUSHES = {"move": ("move", (True, "moved\n"), ["move", "--repository", Api.REPOSITORY, "--old", "a" * 40, "--new", "b" * 40])}

    def setUp(self) -> None:
        self.temporary = temporary_support.Directory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.seen: dict = {}
        self.result: object = None

    def push(self, *_arguments: object, **_options: object) -> object:
        """Stands in for the function that pushes, and records what a Git command would find at that moment."""
        command = shlex.split(os.environ["GIT_SSH_COMMAND"])
        key = Path(command[command.index("-i") + 1])
        hosts = Path(next(word for word in command if word.startswith("UserKnownHostsFile=")).split("=", 1)[1])
        self.seen = {"command": command, "key": key, "hosts": hosts, "key text": key.read_bytes(), "hosts text": hosts.read_bytes(),
                     "mode": key.stat().st_mode & 0o777, "variable": os.environ.get(KEY_VARIABLE)}
        return self.result

    def run_push(self, subcommand: str, environment: dict[str, str]) -> int:
        function, self.result, arguments = self.PUSHES[subcommand]
        self.output = io.StringIO()
        with mock.patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), **environment}), mock.patch.object(promote, function, side_effect=self.push), \
                contextlib.redirect_stdout(self.output):
            os.environ.pop("GIT_SSH_COMMAND", None)
            code = promote.main([*arguments, "--deploy-key", KEY_VARIABLE])
            self.after = dict(os.environ)
        return code

    def test_each_push_runs_with_the_named_key_alone_and_the_pinned_host_key(self) -> None:
        self.assertEqual(GITHUB_HOST_KEY, promote.GITHUB_HOST_KEY)
        for subcommand in self.PUSHES:
            with self.subTest(subcommand=subcommand):
                self.assertEqual(0, self.run_push(subcommand, {KEY_VARIABLE: KEY_TEXT}))
                key, hosts = self.seen["key"], self.seen["hosts"]
                self.assertEqual(["ssh", "-F", "/dev/null", "-i", key.as_posix(), "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                                  "-o", f"UserKnownHostsFile={hosts.as_posix()}", "-o", "StrictHostKeyChecking=yes"], self.seen["command"],
                                 "no configuration file, no agent and no other identity; an unknown host key ends the connection")
                self.assertEqual((KEY_TEXT + "\n").encode(), self.seen["key text"])
                self.assertEqual((GITHUB_HOST_KEY + "\n").encode(), self.seen["hosts text"], "github.com is the only host, under its pinned key")
                self.assertIsNone(self.seen["variable"], "no command that the program starts holds the key in its environment")
                self.assertEqual(self.root, key.parent.parent, "the key lies under the runner's temporary directory")
                if os.name == "posix":
                    self.assertEqual(0o600, self.seen["mode"], "the key file is readable by the job's user only")
                self.assertFalse(key.parent.exists(), "the key file is removed when the subcommand ends")
                self.assertNotIn("GIT_SSH_COMMAND", self.after)
                self.assertEqual(KEY_TEXT, self.after[KEY_VARIABLE])
                self.assertNotIn("FIXTURE KEY", self.output.getvalue(), "the key is never printed")

    def test_two_pushes_of_one_job_lie_in_two_files(self) -> None:
        self.run_push("move", {KEY_VARIABLE: KEY_TEXT})
        first = self.seen["key"]
        self.run_push("move", {KEY_VARIABLE: KEY_TEXT})
        self.assertNotEqual(first, self.seen["key"])

    def test_the_key_file_is_removed_when_the_push_raises(self) -> None:
        def refuse(*_arguments: object, **_options: object) -> object:
            self.push()
            raise OSError("the fixture push broke")
        with mock.patch.dict(os.environ, {"RUNNER_TEMP": str(self.root), KEY_VARIABLE: KEY_TEXT}), mock.patch.object(promote, "move", side_effect=refuse):
            os.environ.pop("GIT_SSH_COMMAND", None)
            with self.assertRaises(OSError):
                promote.main(["move", "--repository", Api.REPOSITORY, "--old", "a" * 40, "--new", "b" * 40, "--deploy-key", KEY_VARIABLE])
        self.assertFalse(self.seen["key"].parent.exists())

    def test_a_variable_without_a_key_stops_the_subcommand_before_any_push(self) -> None:
        for name, environment in (("unset", {}), ("empty", {KEY_VARIABLE: ""}), ("blank", {KEY_VARIABLE: " \n"})):
            with self.subTest(variable=name):
                self.seen = {}
                with mock.patch.dict(os.environ):
                    os.environ.pop(KEY_VARIABLE, None)
                    self.assertEqual(1, self.run_push("move", environment))
                self.assertEqual({}, self.seen, "nothing was pushed")
                self.assertEqual(f"move stopped: the environment variable {KEY_VARIABLE} holds no deploy key.\n", self.output.getvalue())

    def test_without_the_option_git_keeps_its_own_environment(self) -> None:
        def moved(*_arguments: object, **_options: object) -> tuple[bool, str]:
            self.seen = {"command": os.environ["GIT_SSH_COMMAND"]}
            return True, "moved\n"
        with mock.patch.dict(os.environ, {"GIT_SSH_COMMAND": "ssh -o Fixture=yes"}), mock.patch.object(promote, "move", side_effect=moved), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, promote.main(["move", "--repository", Api.REPOSITORY, "--old", "a" * 40, "--new", "b" * 40]))
        self.assertEqual("ssh -o Fixture=yes", self.seen["command"])


class WorkflowInvocationTests(unittest.TestCase):
    def test_the_promotion_workflow_invokes_the_script_with_subcommands_and_options_it_accepts(self) -> None:
        invocations = script_invocations(REPOSITORY_ROOT / ".github" / "workflows" / "promote-v1.yml", "promote_v1.py")
        self.assertEqual([tokens[0] for tokens in invocations], ["read-tag", "decide", "await", "move"])
        for tokens in invocations:
            with self.subTest(step=tokens[0]):
                promote.build_parser().parse_args(tokens)

    def test_the_subcommands_and_the_option_of_the_proof_are_gone(self) -> None:
        for tokens in (["candidate", "--new", "a" * 40], ["start-proof", "--candidate", "a" * 40, "--work", "work"],
                       ["await", "--commit", "a" * 40, "--old", "b" * 40, "--proof-commit", "c" * 40, "--repository", Api.REPOSITORY]):
            with self.subTest(command=tokens[0]), contextlib.redirect_stderr(io.StringIO()) as stderr, self.assertRaises(SystemExit) as raised:
                promote.main(tokens)
            self.assertEqual(2, raised.exception.code)
            expected = "unrecognized arguments: --proof-commit" if tokens[0] == "await" else f"argument command: invalid choice: '{tokens[0]}'"
            self.assertIn(expected, stderr.getvalue())


class MoveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = temporary_support.Directory()
        self.addCleanup(self.temporary.cleanup)
        self.fixture = Fixture(Path(self.temporary.name))

    def refuse_pushes(self) -> None:
        """Make the origin refuse every push, the way a tag rule or a missing permission does, so no lease is involved."""
        hook = self.fixture.origin / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'fixture rule refuses this push' >&2\nexit 1\n", encoding="utf-8", newline="\n")
        hook.chmod(0o755)

    def move_cli(self, old: str, new: str, *, verdict: promote.CiRun | None = None) -> tuple[int, str, str]:
        summary = Path(self.temporary.name) / "summary.md"
        run = verdict if verdict is not None else promote.CiRun("completed", "success")
        with mock.patch.object(promote, "verdict_run", return_value=run), contextlib.redirect_stdout(io.StringIO()) as stdout:
            code = promote.main(["move", "--repository", Api.REPOSITORY, "--repository-root", str(self.fixture.repo), f"--old={old}", "--new", new, "--summary", str(summary)])
        return code, summary.read_text(encoding="utf-8"), stdout.getvalue()

    def test_an_annotated_tag_is_leased_on_its_tag_object(self) -> None:
        git(self.fixture.repo, "tag", "-f", "-a", "-m", "annotated v1", "v1", self.fixture.v1)
        git(self.fixture.repo, "push", "-q", "--force", "origin", "refs/tags/v1")
        tag_object = self.fixture.remote_v1()
        self.assertNotEqual(tag_object, self.fixture.v1)
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n'})
        git(self.fixture.repo, "push", "-q", "origin", "main")
        decision = promote.decide(self.fixture.repo, commit, self.fixture.v1, SURFACE, tag_object, pull_request=pull_request(commit), app_login=APP, owner=OWNER)
        self.assertTrue(decision.follow, decision.reason)
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

        with mock.patch.object(promote, "remote_object", unreadable), mock.patch.object(promote, "verdict_run", return_value=promote.CiRun("completed", "success")):
            code = promote.main(["move", "--repository", Api.REPOSITORY, "--repository-root", str(self.fixture.repo), "--old", self.fixture.v1, "--new", commit, "--summary", str(summary)])
        self.assertEqual(1, code)
        text = summary.read_text(encoding="utf-8")
        self.assertIn(f"The push of `v1` to `{commit}` failed and `refs/tags/v1` could not be read again, so the state of `v1` is unknown.", text)
        self.assertIn("`git ls-remote origin refs/tags/v1`", text)
        self.assertIn("network down", text)
        self.assertNotIn("rejected", text.partition("```")[0])
        self.assertIn("fixture rule refuses this push", text, "the summary keeps git's error")

    def test_move_rechecks_the_verdict_at_the_tag_write_and_refuses_without_a_passed_verdict(self) -> None:
        commit = self.fixture.commit("chore: update CI tool pins", {"ci/pins.json": '{"version": "2"}\n'})
        git(self.fixture.repo, "push", "-q", "origin", "main")
        for label, verdict, expected in (
            ("failed verdict", promote.CiRun("completed", "failure"), "did not pass: concluded failure"),
            ("running verdict", promote.CiRun("in_progress", None), "has not completed (in_progress)"),
            ("missing verdict", promote.CiRun(None, None), "no `ci / verdict` check run exists"),
        ):
            with self.subTest(case=label):
                code, text, _ = self.move_cli(self.fixture.v1, commit, verdict=verdict)
                self.assertEqual(1, code)
                self.assertEqual(self.fixture.v1, self.fixture.remote_v1(), "nothing is pushed when the verdict recheck fails")
                self.assertIn(expected, text)
        summary = Path(self.temporary.name) / "summary-unreadable.md"
        with mock.patch.object(promote, "verdict_run", side_effect=OSError("network down")), contextlib.redirect_stdout(io.StringIO()):
            code = promote.main(["move", "--repository", Api.REPOSITORY, "--repository-root", str(self.fixture.repo), "--old", self.fixture.v1, "--new", commit, "--summary", str(summary)])
        self.assertEqual(1, code)
        self.assertEqual(self.fixture.v1, self.fixture.remote_v1())
        self.assertIn("could not be read", summary.read_text(encoding="utf-8"))


RAN = promote.CiRun("completed", "success")
CLEAN = promote.ScanResult(True, "no leaks found")
COMMAND = "python3 actions/repository-hygiene/promote_v1.py promote"


class ManualMove(unittest.TestCase):
    """A fixture whose remote ``v1`` names the baseline, with the CI read and the Gitleaks scan injected."""

    def setUp(self) -> None:
        self.temporary = temporary_support.Directory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.fixture = Fixture(self.root)
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
        self.fixture.v1 = self.fixture.commit("feat: baseline digests", {
            "ci/pins.json": pins_v1,
            "actions/recording/recording.py": "# rec 1\n",
            "actions/recording/etw-consumer/Program.cs": "// etw 1\n",
            "actions/python-language-step/python_language_step.py": "# py 1\n",
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
            "actions/python-tests/consumer-kit/README.md": "# Python tests 2\n",
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

    def setUp(self) -> None:
        super().setUp()
        self.surface_file.write_text(json.dumps({**SURFACE, "paths": [*SURFACE["paths"], "actions/sync"], "classes": {**SURFACE["classes"], "actions/sync": "template"}}),
                                     encoding="utf-8")
        self.without_caller = self.fixture.v1
        self.fixture.v1 = self.caller_change(self.FIRST)
        git(self.fixture.repo, "push", "-q", "--force", "origin", f"{self.fixture.v1}:refs/tags/v1")
        self.gh.installed = self.FIRST
        self.moved_at_call: list[str] = []
        self.gh.at_call = lambda: self.moved_at_call.append(git(self.fixture.repo, "ls-remote", "origin", "refs/tags/v1").partition("	")[0])
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
