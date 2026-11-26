"""The automatic v1 move: the tag read against a mocked API, the decision by promotion class, the wait for the CI on
``main`` over local fixture repositories, the deploy-key setup, and the leased tag push."""

from __future__ import annotations

import contextlib
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
from tests.hygiene_v1_promotion_support import (
    APP,
    Api,
    Fixture,
    FixtureTestCase,
    OWNER,
    PASSED,
    REPOSITORY_ROOT,
    RESOURCES,
    SCRIPT,
    SURFACE,
    git,
    promote,
    pull_request,
)
from tests.workflow_commands import script_invocations


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


class DecisionTests(FixtureTestCase):
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


class PinsExceptionTests(FixtureTestCase):
    """The pins file is in the write class, and one commit that changes it still moves automatically: the merge commit of
    the updater App's pull request from the updater branch, merged by the repository owner, that changes only the pins file."""

    PINS = {"ci/pins.json": '{"version": "2"}\n'}

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
                fixture = Fixture.from_template(self.template, temporary_support.make_directory(self.addCleanup))
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


class MoveTests(FixtureTestCase):
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


if __name__ == "__main__":
    unittest.main()
