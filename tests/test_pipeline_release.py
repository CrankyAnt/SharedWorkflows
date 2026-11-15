"""The release part ``github-release`` on fixture consumers with fixture tags and signatures: which tags it publishes
for, why it publishes nothing, and what it gives the jobs that build and publish."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest

from tests.ci_policy_support import read_workflow
from tests.pipeline_fixtures import REPOSITORY_ROOT, git
from tests.release_fixtures import (CHANGES, CONFIGURATION, CONSUMER, FULL_CHANGELOG, OWNER, PART, SIGNERS, SSH_KEYGEN, TOKEN, GitHubStandIn, Outcome, ReleaseFixture,
                                    configuration, release)
from tests.workflow_commands import script_invocations

WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "release.yml"


class NothingToPublishTests(ReleaseFixture):
    """A run that is no release: it publishes nothing and passes."""

    def nothing(self, outcome, reason: str) -> None:
        self.assertEqual(0, outcome.status, outcome.stdout + outcome.stderr)
        self.assertEqual((False, reason), (outcome.part["run"], outcome.part["reason"]))
        self.assertEqual({"run", "reason"}, set(outcome.part), "a run that publishes nothing gives the jobs nothing to build or publish")
        self.assertIn("Nothing is published", outcome.summary)
        self.assertIn(reason, outcome.summary)

    def test_a_started_plan_for_a_tag_without_release_configuration_publishes_nothing_and_passes(self) -> None:
        self.commit({CONFIGURATION: "version: 1\n"})
        self.tag("v1.0.0", annotated=False)
        outcome = self.start_program("plan", "--ref", "refs/tags/v1.0.0")
        self.nothing(outcome, f"`{CONFIGURATION}` does not switch the part `{PART}` on")

    def test_a_repository_without_a_configuration_file_publishes_nothing_and_passes(self) -> None:
        self.commit({"README.md": "A consumer.\n"})
        self.tag("v1.0.0", annotated=False)
        self.nothing(self.plan("refs/tags/v1.0.0"), f"`{CONFIGURATION}` does not exist")

    def test_a_run_that_is_not_for_a_tag_publishes_nothing_and_passes(self) -> None:
        self.commit({CONFIGURATION: configuration()})
        for ref in ("refs/heads/main", "refs/pull/3/merge", ""):
            with self.subTest(ref=ref):
                self.nothing(self.plan(ref), "the run is not for a tag")

    def test_a_tag_that_does_not_match_a_configured_pattern_publishes_nothing_and_passes(self) -> None:
        self.commit({CONFIGURATION: configuration(tags='["v*", "release/*"]')})
        for tag in ("1.0.0", "candidate", "nightly/v1", "release/2026/1"):
            with self.subTest(tag=tag):
                self.tag(tag, annotated=False)
                self.nothing(self.plan(f"refs/tags/{tag}"), f"the tag `{tag}` matches none of the configured patterns `v*` and `release/*`")

    def test_a_configuration_file_that_does_not_match_its_schema_fails_with_every_mismatch(self) -> None:
        self.commit({CONFIGURATION: f"version: 1\nparts:\n  {PART}:\n    tags: v*\n    secret: TOKEN\n"})
        self.tag("v1.0.0", annotated=False)
        outcome = self.plan("refs/tags/v1.0.0")
        self.assertEqual(1, outcome.status)
        self.assertNotIn("plan", outcome.outputs)
        for line in (f"parts.{PART}.tags: expected a list, found a string", f"parts.{PART}.allowed-signers: is required", f"parts.{PART}.files: is required",
                     f"parts.{PART}.secret: is not a setting of the part {PART}"):
            self.assertIn(line, outcome.summary)


@unittest.skipUnless(SSH_KEYGEN, "a signed fixture tag needs ssh-keygen")
class SignatureTests(ReleaseFixture):
    """A matching tag publishes only with a valid signature of a key that the default branch allows."""

    def setUp(self) -> None:
        super().setUp()
        self.maintainer, self.maintainer_line = self.key("maintainer")
        self.commit({CONFIGURATION: configuration(), SIGNERS: self.maintainer_line, "dist/tool.zip": "the artefact\n"})
        self.publish_main()

    def failed(self, outcome, reason: str) -> None:
        self.assertEqual(1, outcome.status, outcome.stdout + outcome.stderr)
        self.assertNotIn("plan", outcome.outputs, "a failed plan gives the later jobs nothing to run on")
        self.assertIn("**Failed.** Nothing is published: ", outcome.summary)
        self.assertIn(reason, outcome.summary)
        self.assertIn(reason, outcome.stdout)

    def test_a_started_plan_for_a_matching_tag_with_a_valid_signature_publishes(self) -> None:
        commit = self.repository_head()
        self.tag("v1.0.0", key=self.maintainer)
        outcome = self.start_program("plan", "--ref", "refs/tags/v1.0.0")
        self.assertEqual(0, outcome.status, outcome.stdout + outcome.stderr)
        part = outcome.part
        self.assertEqual((True, "v1.0.0", commit), (part["run"], part["tag"], part["commit"]))
        self.assertEqual("the tag `v1.0.0` matches `v*` and carries a valid signature of an allowed key", part["reason"])
        self.assertIn("The tag `v1.0.0` is released", outcome.summary)
        self.assertIn(commit, outcome.summary)

    def test_a_matching_tag_without_a_signature_publishes_nothing_and_fails_with_the_reason(self) -> None:
        self.tag("v1.0.0", annotated=True)
        self.failed(self.plan("refs/tags/v1.0.0"), "the tag `v1.0.0` carries no valid signature of a key in `.github/allowed_signers` on the default branch `main`")

    def test_a_matching_lightweight_tag_publishes_nothing_and_fails_with_the_reason(self) -> None:
        self.tag("v1.0.0", annotated=False)
        self.failed(self.plan("refs/tags/v1.0.0"), "the tag `v1.0.0` is not an annotated tag, so it carries no signature")

    def test_a_matching_tag_signed_with_a_key_that_is_not_allowed_publishes_nothing_and_fails(self) -> None:
        stranger, _ = self.key("stranger")
        self.tag("v1.0.0", key=stranger)
        self.failed(self.plan("refs/tags/v1.0.0"), "the tag `v1.0.0` carries no valid signature of a key in `.github/allowed_signers` on the default branch `main`")

    def test_a_tagged_commit_that_allows_its_own_key_does_not_approve_its_tag(self) -> None:
        stranger, stranger_line = self.key("stranger")
        self.commit({SIGNERS: self.maintainer_line + stranger_line})  # on the tagged commit only: origin/main stays behind
        self.tag("v1.0.0", key=stranger)
        self.failed(self.plan("refs/tags/v1.0.0"), "the tag `v1.0.0` carries no valid signature")
        self.publish_main()
        self.assertEqual(0, self.plan("refs/tags/v1.0.0").status, "once the default branch allows the key, the tag is released")

    def test_a_default_branch_without_the_allowed_signers_file_fails_with_the_reason(self) -> None:
        self.commit({SIGNERS: None})
        self.publish_main()
        self.tag("v1.0.0", key=self.maintainer)
        self.failed(self.plan("refs/tags/v1.0.0"), "the default branch `main` holds no file `.github/allowed_signers`")

    def test_a_checkout_without_the_default_branch_fails_with_the_reason(self) -> None:
        self.tag("v1.0.0", key=self.maintainer)
        self.failed(self.plan("refs/tags/v1.0.0", "--default-branch", "trunk"), "the checkout holds no default branch `origin/trunk`")

    def test_a_tag_that_does_not_exist_in_the_checkout_fails_with_the_reason(self) -> None:
        self.failed(self.plan("refs/tags/v9.9.9"), "the checkout holds no tag `v9.9.9`")

    def test_a_signed_tag_that_matches_no_pattern_publishes_nothing_and_passes(self) -> None:
        self.tag("candidate", key=self.maintainer)
        outcome = self.plan("refs/tags/candidate")
        self.assertEqual((0, False), (outcome.status, outcome.part["run"]))


@unittest.skipUnless(SSH_KEYGEN, "a signed fixture tag needs ssh-keygen")
class BuildPlanTests(ReleaseFixture):
    """What the plan gives the job that collects the files and the job that publishes them."""

    def released(self, text: str) -> dict:
        maintainer, line = self.key(f"maintainer-{len(list(self.keys.iterdir()))}")
        self.commit({CONFIGURATION: text, SIGNERS: line})
        self.publish_main()
        tag = f"v1.0.{len(list(self.keys.iterdir()))}"
        self.tag(tag, key=maintainer)
        outcome = self.plan(f"refs/tags/{tag}")
        self.assertEqual(0, outcome.status, outcome.summary)
        return outcome.part

    def test_without_a_script_the_tracked_files_are_published_as_they_are_from_the_default_runner(self) -> None:
        part = self.released(configuration(files="[dist/tool.zip, docs/manual.pdf]"))
        self.assertEqual(("", "ubuntu-26.04", [], "dist/tool.zip\ndocs/manual.pdf"), (part["script"], part["runner"], part["setups"], part["files"]))
        self.assertEqual({"tags": ["v*"], "allowed-signers": SIGNERS, "files": ["dist/tool.zip", "docs/manual.pdf"]}, part["config"])

    def test_a_script_runs_after_its_setups_in_the_listed_order_on_the_named_runner(self) -> None:
        part = self.released(configuration("    script: ci/build-release.sh\n    setups: [dotnet-10, python-3.14]\n    runner-label: windows-2025\n"))
        self.assertEqual(("ci/build-release.sh", "windows-2025"), (part["script"], part["runner"]))
        self.assertEqual([{"tool": "dotnet", "version": "10.0.x"}, {"tool": "python", "version": "3.14"}], part["setups"])

    def test_the_title_is_the_tag_unless_the_configuration_gives_one(self) -> None:
        self.assertRegex(self.released(configuration())["title"], r"^v1\.0\.\d+$")
        self.assertRegex(self.released(configuration('    title: "Tool {version} ({tag})"\n'))["title"], r"^Tool 1\.0\.\d+ \(v1\.0\.\d+\)$")

    def test_the_plan_names_the_file_of_the_release_notes_and_no_file_without_the_setting(self) -> None:
        self.assertEqual("", self.released(configuration())["notes"])
        self.commit({"docs/notes.md": "## Changes\n\nA fix.\n"})
        part = self.released(configuration("    notes: docs/notes.md\n"))
        self.assertEqual(("docs/notes.md", "docs/notes.md"), (part["notes"], part["config"]["notes"]))

    def test_the_summary_of_the_plan_names_the_release_notes(self) -> None:
        maintainer, line = self.key()
        self.commit({CONFIGURATION: configuration("    notes: NOTES.md\n"), SIGNERS: line, "NOTES.md": "A fix.\n"})
        self.publish_main()
        self.tag("v1.0.0", key=maintainer)
        self.assertIn("The release notes are the file `NOTES.md` of the tagged commit.", self.plan("refs/tags/v1.0.0").summary)

    def test_release_notes_that_the_tagged_commit_does_not_hold_or_that_github_cannot_take_fail_the_plan(self) -> None:
        maintainer, line = self.key()
        self.commit({CONFIGURATION: configuration("    notes: docs/notes.md\n"), SIGNERS: line})
        self.publish_main()
        cases = {"v3.0.0": (lambda: None, "holds no file `docs/notes.md`; the release notes are a tracked file of that commit."),
                 "v3.0.1": (lambda: (self.repository / "docs").mkdir() or (self.repository / "docs" / "notes.md").write_bytes(b"caf\xe9\n"), "is not UTF-8 text."),
                 "v3.0.2": (lambda: (self.repository / "docs" / "notes.md").write_text("x" * 125001, encoding="utf-8"), "holds 125001 characters; a release takes at most 125000.")}
        for tag, (prepare, reason) in cases.items():
            with self.subTest(reason=reason):
                prepare()
                commit = self.commit({})
                self.tag(tag, key=maintainer)
                outcome = self.plan(f"refs/tags/{tag}")
                self.assertEqual(1, outcome.status, outcome.summary)
                self.assertNotIn("plan", outcome.outputs)
                self.assertIn(f"`{commit}`", outcome.summary)
                self.assertIn(reason, outcome.summary)
        (self.repository / "docs" / "notes.md").write_text("é" * 125000, encoding="utf-8")
        self.commit({})
        self.tag("v3.0.3", key=maintainer)
        self.assertEqual(0, self.plan("refs/tags/v3.0.3").status, "the limit counts characters, not bytes")

    def test_release_notes_come_from_the_tagged_commit_and_not_from_a_later_one(self) -> None:
        maintainer, line = self.key()
        self.commit({CONFIGURATION: configuration("    notes: NOTES.md\n"), SIGNERS: line})
        self.publish_main()
        self.tag("v4.0.0", key=maintainer)
        self.commit({"NOTES.md": "Written after the tag.\n"})
        outcome = self.plan("refs/tags/v4.0.0")
        self.assertEqual(1, outcome.status)
        self.assertIn("holds no file `NOTES.md`", outcome.summary)

    def test_the_plan_lists_the_earlier_matching_tags_that_the_released_commit_reaches_nearest_first(self) -> None:
        maintainer, line = self.key()
        self.commit({CONFIGURATION: configuration(tags='["v*", "release/*"]'), SIGNERS: line})
        self.publish_main()
        self.tag("v0.9.0", annotated=False)
        self.commit({"a.txt": "a\n"})
        self.tag("release/0.9.5")
        self.commit({"b.txt": "b\n"})
        self.tag("v1.0.0", key=maintainer)
        self.tag("candidate")  # matches no pattern
        git(self.repository, "checkout", "--quiet", "-b", "side")
        self.commit({"side.txt": "side\n"})
        self.tag("v1.5.0")  # not reachable from the released commit
        git(self.repository, "checkout", "--quiet", "main")
        self.commit({"c.txt": "c\n"})
        self.tag("nightly")
        self.tag("v1.1.0", key=maintainer)
        self.tag("v1.1.0-rc", annotated=False)  # on the released commit itself, so not earlier
        part = self.plan("refs/tags/v1.1.0").part
        self.assertEqual(["v1.0.0", "release/0.9.5", "v0.9.0"], part["earlier-tags"])
        self.assertEqual(["release/0.9.5", "v0.9.0"], self.plan("refs/tags/v1.0.0").part["earlier-tags"])
        git(self.repository, "tag", "-d", "v0.9.0", "release/0.9.5")
        self.assertEqual([], self.plan("refs/tags/v1.0.0").part["earlier-tags"], "a first release has no earlier tag")

    def test_a_setting_of_the_script_s_job_without_a_script_and_two_files_of_one_name_fail_the_plan(self) -> None:
        for text, reason in ((configuration("    setups: [python-3.14]\n"), f"parts.{PART}.setups: names setups for `script`, which the part does not state"),
                             (configuration("    runner-label: windows-2025\n"), f"parts.{PART}.runner-label: names the runner of `script`, which the part does not state"),
                             (configuration(files="[dist/tool.zip, other/tool.zip]"), f"parts.{PART}.files: `dist/tool.zip` and `other/tool.zip` share the file name `tool.zip`"),
                             (configuration("    runner-label: self-hosted\n    script: ci/build.sh\n"), f"parts.{PART}.runner-label: does not match the pattern"),
                             (configuration("    script: ../build.sh\n"), f"parts.{PART}.script: does not match the pattern"),
                             (configuration(files="[../tool.zip]"), f"parts.{PART}.files[0]: does not match the pattern"),
                             (configuration("    notes: /etc/notes.md\n"), f"parts.{PART}.notes: does not match the pattern"),
                             (configuration(tags="[]"), f"parts.{PART}.tags: needs at least 1 item")):
            with self.subTest(reason=reason):
                self.commit({CONFIGURATION: text})
                self.tag(f"v2.0.{len(reason)}{len(text)}", annotated=False)
                outcome = self.plan(f"refs/tags/v2.0.{len(reason)}{len(text)}")
                self.assertEqual(1, outcome.status)
                self.assertIn(reason, outcome.summary)


class PublishTests(ReleaseFixture):
    """The publishing command on a plan and a folder of collected files, against the stand-in for the GitHub API."""

    COMMIT = "1" * 40

    def setUp(self) -> None:
        super().setUp()
        self.github = GitHubStandIn()
        self.addCleanup(self.github.close)
        self.files = self.directory / "release-files"
        self.files.mkdir()
        (self.files / "tool.zip").write_bytes(b"PK\x03\x04 the archive")
        (self.files / "manual.pdf").write_bytes(b"%PDF the manual")
        self.github.hold_tag("v1.2.0", self.COMMIT)

    def publish(self, part: dict | None = None, start: bool = False) -> Outcome:
        document = {"parts": {PART: {"run": True, "reason": "x", "tag": "v1.2.0", "commit": self.COMMIT, "title": "Tool 1.2.0",
                                     "files": "dist/tool.zip\ndocs/manual.pdf", **(part or {})}}}
        path = self.directory / "plan.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        run = self.start_program if start else self.run_program
        return run("publish", "--plan", str(path), "--files", str(self.files), "--repository", CONSUMER, "--api-url", self.github.url)

    def test_a_started_publish_creates_the_release_of_the_tag_with_the_configured_files(self) -> None:
        outcome = self.publish(start=True)
        self.assertEqual(0, outcome.status, outcome.stdout + outcome.stderr)
        self.assertEqual(1, len(self.github.published))
        published = self.github.published[0]
        self.assertEqual(("v1.2.0", "Tool 1.2.0", False), (published["tag_name"], published["name"], published["draft"]))
        self.assertEqual(f"## What's Changed\n* feat: add the export in {CHANGES}/pull/1\n\n{FULL_CHANGELOG}", published["body"])
        self.assertEqual({"tool.zip": b"PK\x03\x04 the archive", "manual.pdf": b"%PDF the manual"}, self.github.assets[published["id"]])
        self.assertIn(published["html_url"], outcome.summary)
        for name in ("tool.zip", "manual.pdf"):
            self.assertIn(f"`{name}`", outcome.summary)

    def test_the_release_stays_a_draft_until_every_file_is_uploaded(self) -> None:
        self.assertEqual(0, self.publish().status)
        steps = [(request.method, request.path.rsplit("/", 2)[-1] if "assets" in request.path else "", request.document.get("draft") if request.method != "GET" and "assets" not in request.path else None)
                 for request in self.github.requests]
        self.assertEqual([("GET", "", None), ("POST", "", None), ("GET", "", None), ("GET", "", None), ("GET", "", None), ("POST", "", True), ("POST", "assets", None),
                          ("POST", "assets", None), ("GET", "", None), ("GET", "", None), ("PATCH", "", False)], steps)

    def test_every_request_carries_the_token_of_the_run_and_each_file_goes_up_as_bytes_under_its_file_name(self) -> None:
        self.assertEqual(0, self.publish().status)
        for request in self.github.requests:
            self.assertEqual(f"Bearer {TOKEN}", request.headers["authorization"], request.path)
        uploads = [request for request in self.github.requests if "assets" in request.path]
        self.assertEqual([["tool.zip"], ["manual.pdf"]], [request.query["name"] for request in uploads])
        self.assertEqual({"application/octet-stream"}, {request.headers["content-type"] for request in uploads})

    def test_a_missing_file_publishes_nothing_and_fails_before_any_request(self) -> None:
        (self.files / "manual.pdf").unlink()
        outcome = self.publish()
        self.assertEqual(1, outcome.status)
        self.assertEqual([], self.github.requests)
        self.assertIn("**Failed.** Nothing is published: the file `docs/manual.pdf` is not among the collected files", outcome.summary)

    def test_a_tag_that_already_has_a_release_publishes_nothing_and_fails(self) -> None:
        self.assertEqual(0, self.publish().status)
        outcome = self.publish()
        self.assertEqual(1, outcome.status)
        self.assertEqual(1, len(self.github.releases))
        self.assertIn("a release of the tag `v1.2.0` exists", outcome.summary)

    def test_an_upload_that_github_refuses_leaves_a_draft_and_fails_with_the_reason(self) -> None:
        self.github.refuse[("POST", "/upload/")] = 422
        outcome = self.publish()
        self.assertEqual(1, outcome.status)
        self.assertEqual([], self.github.published)
        self.assertEqual([True], [item["draft"] for item in self.github.releases.values()])
        self.assertIn("uploading `tool.zip` failed: HTTP 422", outcome.summary)
        self.assertIn("draft", outcome.summary)

    def test_without_release_notes_the_body_is_the_filtered_list_that_github_generates_for_the_tag_at_the_verified_commit(self) -> None:
        self.github.generated = self.changes(f"* feat: add the export by @{OWNER} in {CHANGES}/pull/1", f"* docs: describe the export by @{OWNER} in {CHANGES}/pull/2")
        for part in ({}, {"notes": ""}):
            with self.subTest(part=part):
                self.github.releases.clear()
                self.github.requests.clear()
                self.assertEqual(0, self.publish(part).status)
                self.assertEqual(self.changes(f"* feat: add the export in {CHANGES}/pull/1"), self.github.published[0]["body"])
                self.assertEqual([{"tag_name": "v1.2.0", "target_commitish": self.COMMIT}], self.github.generate_requests())
                self.assertEqual([], [request.path for request in self.github.requests if "/contents/" in request.path])
                order = [(request.method, request.path.removeprefix(f"/repos/{CONSUMER}")) for request in self.github.requests[:6]]
                self.assertEqual([("GET", "/releases/tags/v1.2.0"), ("POST", "/releases/generate-notes"), ("GET", ""), ("GET", "/git/ref/tags/v1.2.0")], order[:4],
                                 "the tag is still read last before the release is created")
                self.assertEqual(("POST", "/releases"), order[5])

    # --- the previous release

    def test_the_request_names_the_nearest_earlier_tag_with_a_published_release_as_the_previous_tag(self) -> None:
        for number in range(150):  # more releases than one page holds
            self.github.hold_release(f"tool-{number}")
        self.github.hold_release("v1.0.0")
        self.github.hold_release("v1.1.0")
        for number in range(120):
            self.github.hold_release(f"later-{number}")
        self.github.hold_release("v1.1.1", draft=True)
        outcome = self.publish({"earlier-tags": ["v1.1.2", "v1.1.1", "v1.1.0", "v1.0.0"]})
        self.assertEqual(0, outcome.status, outcome.summary)
        self.assertEqual([{"tag_name": "v1.2.0", "target_commitish": self.COMMIT, "previous_tag_name": "v1.1.0"}], self.github.generate_requests())
        self.assertIn("The list of changes starts after the release of the tag `v1.1.0`.", outcome.summary)
        pages = [request.query for request in self.github.requests if request.method == "GET" and request.path == f"/repos/{CONSUMER}/releases"]
        self.assertEqual([{"per_page": ["100"], "page": [str(page)]} for page in (1, 2, 3)], pages)

    def test_without_an_earlier_tag_that_has_a_published_release_the_request_names_no_previous_tag_and_the_run_publishes(self) -> None:
        self.github.hold_release("v1.1.0", draft=True)
        self.github.hold_release("v0.1.0")  # a release of a tag that the released commit does not reach
        for earlier in ([], ["v1.1.0", "v1.0.0"]):
            with self.subTest(earlier=earlier):
                self.github.requests.clear()
                outcome = self.publish({"earlier-tags": earlier})
                self.assertEqual(0, outcome.status, outcome.summary)
                self.assertEqual([{"tag_name": "v1.2.0", "target_commitish": self.COMMIT}], self.github.generate_requests())
                self.assertIn("The list of changes names no previous release.", outcome.summary)
                looked_up = [request for request in self.github.requests if request.path == f"/repos/{CONSUMER}/releases" and request.method == "GET"]
                self.assertEqual(1 if earlier else 0, len(looked_up), "only an earlier tag is looked up among the releases")
                self.github.releases = {number: item for number, item in self.github.releases.items() if item["tag_name"] != "v1.2.0"}

    # --- the filter

    def changes(self, *lines: str, contributors: tuple[str, ...] = (), full: str = FULL_CHANGELOG) -> str:
        """A body in the shape that GitHub generates."""
        blocks = ["## What's Changed\n" + "\n".join(lines)] if lines else []
        blocks += ["## New Contributors\n" + "\n".join(contributors)] if contributors else []
        return "\n\n".join([*blocks, full])

    def body(self, generated: str) -> str:
        """The body of the release that the run publishes when GitHub generates this body."""
        self.github.releases.clear()
        self.github.generated = generated
        outcome = self.publish()
        self.assertEqual(0, outcome.status, outcome.summary)
        return self.github.published[0]["body"]

    def test_a_line_whose_title_has_a_shown_type_stays_with_or_without_a_scope(self) -> None:
        titles = ["feat: add the export", "fix(cli): handle an empty file", "perf: start faster", "revert: undo the export", "Feat(api): add a field", "FIX: a crash"]
        generated = self.changes(*[f"* {title} by @{OWNER} in {CHANGES}/pull/{number}" for number, title in enumerate(titles)])
        self.assertEqual(self.changes(*[f"* {title} in {CHANGES}/pull/{number}" for number, title in enumerate(titles)]), self.body(generated))

    def test_a_line_whose_title_has_a_hidden_type_without_a_breaking_mark_goes(self) -> None:
        hidden = ["docs: describe it", "style: format it", "refactor: move it", "test: cover it", "build: pin it", "ci: speed it up", "chore: tidy it",
                  "chore(deps): bump it", "DOCS(guide): fix a word", "Ci: retry", "docs:no space after the colon"]
        generated = self.changes(f"* feat: keep me by @{OWNER} in {CHANGES}/pull/100", *[f"* {title} by @{OWNER} in {CHANGES}/pull/{number}" for number, title in enumerate(hidden)])
        self.assertEqual(self.changes(f"* feat: keep me in {CHANGES}/pull/100"), self.body(generated))

    def test_a_line_whose_title_of_a_hidden_type_marks_a_breaking_change_stays(self) -> None:
        titles = ["refactor!: drop the old flag", "chore(api)!: remove the endpoint", "ci!: require a newer runner"]
        generated = self.changes(*[f"* {title} by @{OWNER} in {CHANGES}/pull/{number}" for number, title in enumerate(titles)])
        self.assertEqual(self.changes(*[f"* {title} in {CHANGES}/pull/{number}" for number, title in enumerate(titles)]), self.body(generated))

    def test_a_line_whose_title_starts_with_no_conventional_commits_type_stays(self) -> None:
        titles = ["Add a command", "deps: bump the parser", 'Revert "feat: add the export"', "Update README.md", "documentation: a word that is no type"]
        generated = self.changes(*[f"* {title} by @{OWNER} in {CHANGES}/pull/{number}" for number, title in enumerate(titles)])
        self.assertEqual(self.changes(*[f"* {title} in {CHANGES}/pull/{number}" for number, title in enumerate(titles)]), self.body(generated))

    def test_a_line_whose_every_author_is_a_bot_goes_whatever_its_title(self) -> None:
        generated = self.changes(f"* feat: keep me by @{OWNER} in {CHANGES}/pull/1",
                                 f"* Bump the parser from 1 to 2 by @dependabot[bot] in {CHANGES}/pull/2",
                                 f"* feat!: a generated feature by @github-actions[bot] and @dependabot[bot] in {CHANGES}/pull/3",
                                 f"* fix: a bot and the owner by @renovate[bot] and @{OWNER} in {CHANGES}/pull/4")
        self.assertEqual(self.changes(f"* feat: keep me in {CHANGES}/pull/1", f"* fix: a bot and the owner in {CHANGES}/pull/4"), self.body(generated))

    def test_a_line_of_the_owner_or_copilot_alone_names_no_author(self) -> None:
        generated = self.changes(f"* feat: by the owner by @{OWNER} in {CHANGES}/pull/1", f"* fix: by Copilot by @Copilot in {CHANGES}/pull/2",
                                 f"* fix: together by @{OWNER} with @Copilot in {CHANGES}/pull/3", f"* fix: the owner in another case by @Example-Owner in {CHANGES}/pull/4")
        self.assertEqual(self.changes(f"* feat: by the owner in {CHANGES}/pull/1", f"* fix: by Copilot in {CHANGES}/pull/2", f"* fix: together in {CHANGES}/pull/3",
                                      f"* fix: the owner in another case in {CHANGES}/pull/4"), self.body(generated))

    def test_a_line_with_an_external_contributor_names_that_contributor_alone(self) -> None:
        generated = self.changes(f"* feat: alone by @outsider in {CHANGES}/pull/1", f"* fix: shared by @outsider and @{OWNER} in {CHANGES}/pull/2",
                                 f"* fix: with help by @{OWNER} with @Copilot and @outsider in {CHANGES}/pull/3",
                                 f"* perf: many by @outsider, @dependabot[bot], @another and @{OWNER} in {CHANGES}/pull/4")
        self.assertEqual(self.changes(f"* feat: alone by @outsider in {CHANGES}/pull/1", f"* fix: shared by @outsider in {CHANGES}/pull/2",
                                      f"* fix: with help by @outsider in {CHANGES}/pull/3", f"* perf: many by @outsider and @another in {CHANGES}/pull/4"), self.body(generated))

    def test_in_a_repository_of_an_organisation_every_person_is_an_external_contributor(self) -> None:
        self.github.owner = "example-organisation"
        generated = self.changes(f"* feat: by a member by @{OWNER} in {CHANGES}/pull/1", f"* fix: by Copilot by @Copilot in {CHANGES}/pull/2",
                                 contributors=(f"* @{OWNER} made their first contribution in {CHANGES}/pull/1",))
        self.assertEqual(self.changes(f"* feat: by a member by @{OWNER} in {CHANGES}/pull/1", f"* fix: by Copilot in {CHANGES}/pull/2",
                                      contributors=(f"* @{OWNER} made their first contribution in {CHANGES}/pull/1",)), self.body(generated))

    def test_a_title_that_holds_an_author_and_an_address_itself_stays_whole(self) -> None:
        generated = self.changes(f"* feat: credit by @someone in {CHANGES}/pull/9 by @outsider in {CHANGES}/pull/1",
                                 f"* feat: credit by @{OWNER} in {CHANGES}/pull/9 by @dependabot[bot] in {CHANGES}/pull/2",
                                 f"* docs: x by @outsider in {CHANGES}/pull/9 and feat: y by @{OWNER} in {CHANGES}/pull/3")
        self.assertEqual(self.changes(f"* feat: credit by @someone in {CHANGES}/pull/9 by @outsider in {CHANGES}/pull/1"), self.body(generated))

    def test_new_contributors_keep_only_external_contributors_and_the_heading_goes_without_one(self) -> None:
        lines = (f"* feat: one by @outsider in {CHANGES}/pull/1", f"* feat: two by @{OWNER} in {CHANGES}/pull/2")
        newcomers = (f"* @{OWNER} made their first contribution in {CHANGES}/pull/2", f"* @dependabot[bot] made their first contribution in {CHANGES}/pull/3",
                     f"* @Copilot made their first contribution in {CHANGES}/pull/4")
        kept = (f"* feat: one by @outsider in {CHANGES}/pull/1", f"* feat: two in {CHANGES}/pull/2")
        self.assertEqual(self.changes(*kept), self.body(self.changes(*lines, contributors=newcomers)))
        external = f"* @outsider made their first contribution in {CHANGES}/pull/1"
        self.assertEqual(self.changes(*kept, contributors=(external,)), self.body(self.changes(*lines, contributors=(newcomers[0], external, *newcomers[1:]))))

    def test_under_category_headings_each_line_is_filtered_and_an_empty_category_goes(self) -> None:
        generated = "\n".join(["## What's Changed", "### Features", f"* feat: add the export by @{OWNER} in {CHANGES}/pull/1",
                               f"* docs: describe the export by @{OWNER} in {CHANGES}/pull/2", "### Maintenance", f"* chore: tidy by @{OWNER} in {CHANGES}/pull/3",
                               f"* Bump the parser by @dependabot[bot] in {CHANGES}/pull/4", "### Other Changes", f"* Add a command by @outsider in {CHANGES}/pull/5",
                               "", "", FULL_CHANGELOG])
        self.assertEqual("\n".join(["## What's Changed", "### Features", f"* feat: add the export in {CHANGES}/pull/1", "### Other Changes",
                                    f"* Add a command by @outsider in {CHANGES}/pull/5", "", FULL_CHANGELOG]), self.body(generated))

    def test_a_line_without_the_shape_of_a_change_line_stays_as_it_is(self) -> None:
        generated = self.changes(f"* feat: add the export by @{OWNER} in {CHANGES}/pull/1", "* A line that names no author", "Some text of a category.")
        self.assertEqual(self.changes(f"* feat: add the export in {CHANGES}/pull/1", "* A line that names no author", "Some text of a category."), self.body(generated))

    def test_without_a_change_line_that_stays_the_body_says_that_no_user_facing_change_is_listed(self) -> None:
        empty = f"No user-facing changes are listed for this release.\n\n{FULL_CHANGELOG}"
        hidden = self.changes(f"* docs: describe it by @{OWNER} in {CHANGES}/pull/1", f"* Bump the parser by @dependabot[bot] in {CHANGES}/pull/2",
                              contributors=(f"* @dependabot[bot] made their first contribution in {CHANGES}/pull/2",))
        first = f"**Full Changelog**: {CHANGES}/commits/v1.2.0"
        for name, generated, expected in (("every line hidden", hidden, empty), ("no change line", FULL_CHANGELOG, empty),
                                          ("no change line and no previous tag", first, f"No user-facing changes are listed for this release.\n\n{first}")):
            with self.subTest(answer=name):
                self.assertEqual(expected, self.body(generated))

    def test_the_line_full_changelog_is_the_last_line_in_both_of_its_forms(self) -> None:
        for full in (FULL_CHANGELOG, f"**Full Changelog**: {CHANGES}/commits/v1.2.0"):
            with self.subTest(full=full):
                body = self.body(self.changes(f"* feat: add the export by @outsider in {CHANGES}/pull/1",
                                              contributors=(f"* @outsider made their first contribution in {CHANGES}/pull/1",), full=full))
                self.assertEqual(full, body.splitlines()[-1])
                self.assertEqual(1, body.count("**Full Changelog**"))

    def test_a_body_beyond_the_limit_loses_change_lines_from_the_end_and_says_that_the_list_is_shortened(self) -> None:
        lines = [f"* feat: change {number:04} with a long description of what it does for the people who use the tool by @outsider in {CHANGES}/pull/{number}"
                 for number in range(1, 1501)]
        body = self.body(self.changes(*lines))
        self.assertLessEqual(len(body), 125000)
        self.assertGreater(len(body), 124000, "only as many lines go as the limit needs")
        listed = [line for line in body.splitlines() if line.startswith("* ")]
        self.assertEqual(lines[:len(listed)], listed, "the first change lines stay, in their order")
        self.assertLess(len(listed), len(lines))
        self.assertTrue(body.startswith(f"## What's Changed\n{lines[0]}\n"))
        self.assertTrue(body.endswith(f"{listed[-1]}\n\nThe list is shortened to its first {len(listed)} of 1500 changes to fit the length that GitHub accepts for a release."
                                      f"\n\n{FULL_CHANGELOG}"))
        self.assertEqual(self.changes(*lines[:3]), self.body(self.changes(*lines[:3])), "a body within the limit is not shortened")

    def test_a_request_for_the_list_or_the_previous_release_that_fails_fails_the_run_before_the_release_is_created(self) -> None:
        cases = {"the notes refused": ({("POST", f"/repos/{CONSUMER}/releases/generate-notes"): 403}, None, "generating the release notes of the tag `v1.2.0` failed: HTTP 403."),
                 "the notes absent": ({("POST", f"/repos/{CONSUMER}/releases/generate-notes"): 404}, None, "generating the release notes of the tag `v1.2.0` failed: HTTP 404."),
                 "an answer without a body": ({}, "", "generating the release notes of the tag `v1.2.0` failed: GitHub's answer holds no body."),
                 "the releases refused": ({("GET", f"/repos/{CONSUMER}/releases$"): 500}, None, "reading the releases of the repository failed: HTTP 500."),
                 "the repository refused": ({("GET", f"/repos/{CONSUMER}$"): 500}, None, "reading the owner of the repository failed: HTTP 500.")}
        for name, (refuse, generated, reason) in cases.items():
            with self.subTest(case=name):
                self.github.refuse = refuse
                self.github.generated = None if generated == "" else self.changes(f"* feat: add the export by @{OWNER} in {CHANGES}/pull/1")
                self.github.requests.clear()
                outcome = self.publish({"earlier-tags": ["v1.1.0"]})
                self.assertEqual(1, outcome.status, outcome.summary)
                self.assertEqual({}, self.github.releases, "no release is created, not even a draft")
                self.assertEqual([], [request for request in self.github.requests if request.path.startswith(f"/repos/{CONSUMER}/git/")], "the tag is not read")
                self.assertIn(f"**Failed.** Nothing is published: {reason}", outcome.summary)

    def test_a_plan_whose_earlier_tags_are_not_a_list_of_tags_is_refused(self) -> None:
        for earlier in ("v1.1.0", [1], [""]):
            with self.subTest(earlier=earlier):
                self.assertEqual(2, self.publish({"earlier-tags": earlier}).status)
        self.assertEqual([], self.github.requests)

    def test_the_named_file_at_the_verified_commit_becomes_the_release_notes(self) -> None:
        notes = "## Changes\n\n- A fix, with an é.\n" + "A long line of the notes. " * 40 + "\n"
        self.github.contents[(self.COMMIT, "docs/release notes.md")] = notes.encode("utf-8")
        self.github.contents[("2" * 40, "docs/release notes.md")] = b"The notes of another commit.\n"
        outcome = self.publish({"notes": "docs/release notes.md"})
        self.assertEqual(0, outcome.status, outcome.summary)
        self.assertEqual(notes, self.github.published[0]["body"], "the body is the file's content and nothing else")
        self.assertEqual([], self.github.generate_requests(), "no list is generated")
        read = [request for request in self.github.requests if "/contents/" in request.path]
        self.assertEqual([(f"/repos/{CONSUMER}/contents/docs/release%20notes.md", {"ref": [self.COMMIT]})], [(request.path, request.query) for request in read])
        order = [request.path.removeprefix(f"/repos/{CONSUMER}/").split("/")[0] for request in self.github.requests[:5]]
        self.assertEqual(["releases", "contents", "git", "git", "releases"], order, "the tag is still read last before the release is created")

    def test_release_notes_that_cannot_be_read_publish_nothing_and_fail(self) -> None:
        what = f"reading the release notes `NOTES.md` at commit `{self.COMMIT}` failed"
        cases = {"absent": (None, f"{what}: the commit holds no such file."),
                 "a folder": ([{"type": "file", "name": "inside.md"}], f"{what}: GitHub's answer holds no content of a file."),
                 "too large for an answer with content": ({"type": "file", "encoding": "none", "content": ""}, f"{what}: GitHub's answer holds no content of a file."),
                 "not UTF-8": (b"caf\xe9\n", f"the file `NOTES.md` of the release notes at commit `{self.COMMIT}` is not UTF-8 text."),
                 "too long": (b"x" * 125001, "holds 125001 characters; a release takes at most 125000.")}
        for name, (held, reason) in cases.items():
            with self.subTest(notes=name):
                self.github.contents = {} if held is None else {(self.COMMIT, "NOTES.md"): held}
                outcome = self.publish({"notes": "NOTES.md"})
                self.assertEqual(1, outcome.status)
                self.assertEqual({}, self.github.releases, "no release is created, not even a draft")
                self.assertIn(f"**Failed.** Nothing is published: {reason}" if name in ("absent", "a folder") else reason, outcome.summary)
        self.github.contents = {}
        self.github.refuse = {("GET", f"/repos/{CONSUMER}/contents/"): 403}
        outcome = self.publish({"notes": "NOTES.md"})
        self.assertEqual((1, {}), (outcome.status, self.github.releases))
        self.assertIn(f"{what}: HTTP 403.", outcome.summary)

    def test_the_tag_is_read_again_right_before_the_release_is_created(self) -> None:
        self.assertEqual(0, self.publish().status)
        requests = [(request.method, request.path.removeprefix(f"/repos/{CONSUMER}/")) for request in self.github.requests]
        tag_object = self.github.tags["v1.2.0"]["sha"]
        created = requests.index(("POST", "releases"))
        self.assertEqual([("GET", "git/ref/tags/v1.2.0"), ("GET", f"git/tags/{tag_object}"), ("POST", "releases")], requests[created - 2:created + 1])
        self.assertEqual(("GET", "releases/tags/v1.2.0"), requests[0])

    def test_a_tag_that_moved_after_its_signature_was_verified_publishes_nothing_and_fails(self) -> None:
        moved = "2" * 40
        for name, annotated in (("an annotated tag", True), ("a lightweight tag", False)):
            with self.subTest(tag=name):
                self.github.hold_tag("v1.2.0", moved, annotated)
                outcome = self.publish()
                self.assertEqual(1, outcome.status)
                self.assertEqual({}, self.github.releases, "no release is created, not even a draft")
                self.assertEqual([], [request for request in self.github.requests if request.method != "GET" and not request.path.endswith("/generate-notes")])
                self.assertIn(f"**Failed.** Nothing is published: the tag `v1.2.0` moved after its signature was verified: it names the commit `{moved}`, "
                              f"not the commit `{self.COMMIT}` whose signature was verified.", outcome.summary)

    def test_the_tag_is_read_once_more_right_before_the_request_that_publishes(self) -> None:
        self.assertEqual(0, self.publish().status)
        requests = [(request.method, request.path.removeprefix(f"/repos/{CONSUMER}/")) for request in self.github.requests]
        tag_object = self.github.tags["v1.2.0"]["sha"]
        self.assertEqual([("GET", "git/ref/tags/v1.2.0"), ("GET", f"git/tags/{tag_object}"), ("PATCH", "releases/1")], requests[-3:])

    def test_a_tag_that_moves_after_the_draft_is_created_publishes_nothing_and_deletes_the_draft(self) -> None:
        moved = "2" * 40
        cases = {"moved": (lambda: self.github.hold_tag("v1.2.0", moved), f"the tag `v1.2.0` moved after its signature was verified: it names the commit `{moved}`"),
                 "removed": (lambda: self.github.tags.pop("v1.2.0", None), "the tag `v1.2.0` no longer exists")}
        for name, (change, reason) in cases.items():
            with self.subTest(tag=name):
                self.github.hold_tag("v1.2.0", self.COMMIT)
                self.github.requests.clear()
                self.github.after_upload = change
                outcome = self.publish()
                self.assertEqual(1, outcome.status)
                self.assertEqual(({}, {}), (self.github.releases, self.github.assets), "the draft is gone, with the files it held")
                writes = [(request.method, "assets" if "assets" in request.path else "release") for request in self.github.requests
                          if request.method != "GET" and not request.path.endswith("/generate-notes")]
                self.assertEqual([("POST", "release"), ("POST", "assets"), ("POST", "assets"), ("DELETE", "release")], writes, "no request publishes the draft")
                self.assertIn(f"**Failed.** Nothing is published: {reason}", outcome.summary)
                self.assertIn("The draft of the release was deleted.", outcome.summary)

    def test_a_draft_that_cannot_be_deleted_stays_unpublished_and_the_summary_says_so(self) -> None:
        self.github.after_upload = lambda: self.github.hold_tag("v1.2.0", "2" * 40)
        self.github.refuse[("DELETE", f"/repos/{CONSUMER}/releases/")] = 403
        outcome = self.publish()
        self.assertEqual(1, outcome.status)
        self.assertEqual(([], [True]), (self.github.published, [item["draft"] for item in self.github.releases.values()]))
        self.assertIn("The draft of the release could not be deleted (HTTP 403); it is not published, so remove it before this run publishes again.", outcome.summary)

    def test_a_tag_that_was_removed_after_its_signature_was_verified_publishes_nothing_and_fails(self) -> None:
        del self.github.tags["v1.2.0"]
        outcome = self.publish()
        self.assertEqual(1, outcome.status)
        self.assertEqual({}, self.github.releases)
        self.assertIn(f"the tag `v1.2.0` no longer exists; it named the commit `{self.COMMIT}` whose signature was verified.", outcome.summary)

    def test_a_tag_that_cannot_be_read_publishes_nothing_and_fails(self) -> None:
        for name, refuse, reason in (("the ref", ("GET", f"/repos/{CONSUMER}/git/ref/"), "reading the tag `v1.2.0` failed: HTTP 500."),
                                     ("the tag object", ("GET", f"/repos/{CONSUMER}/git/tags/"), "reading the tag `v1.2.0` failed: HTTP 500.")):
            with self.subTest(unreadable=name):
                self.github.refuse = {refuse: 500}
                outcome = self.publish()
                self.assertEqual(1, outcome.status)
                self.assertEqual({}, self.github.releases)
                self.assertIn(reason, outcome.summary)
        self.github.refuse = {}
        self.github.tags["v1.2.0"] = {"type": "tree", "sha": "3" * 40}
        outcome = self.publish()
        self.assertEqual((1, {}), (outcome.status, self.github.releases))
        self.assertIn("reading the tag `v1.2.0` failed: GitHub's answer names no commit.", outcome.summary)

    def test_a_tag_with_a_slash_is_read_under_its_own_name(self) -> None:
        self.github.hold_tag("release/1.2.0", self.COMMIT)
        self.assertEqual(0, self.publish({"tag": "release/1.2.0"}).status)
        self.assertIn(("GET", f"/repos/{CONSUMER}/git/ref/tags/release/1.2.0"), [(request.method, request.path) for request in self.github.requests])

    def test_a_plan_that_does_not_publish_is_refused(self) -> None:
        outcome = self.publish({"run": False})
        self.assertEqual(2, outcome.status)
        self.assertEqual([], self.github.requests)


class InvocationTests(unittest.TestCase):
    """The release workflow's invocations of the release program, parsed with the program's own parser."""

    def test_the_workflow_starts_the_plan_and_then_the_publishing_with_options_that_the_program_accepts(self) -> None:
        invocations = script_invocations(WORKFLOW, "release.py")
        self.assertEqual(["plan", "publish"], [tokens[0] for tokens in invocations])
        planning, publishing = (release.parser().parse_args(tokens) for tokens in invocations)
        self.assertEqual((".", str(release.plan.SCHEMAS)), (planning.root, planning.schemas), "the workflow reads the schema of its own revision")
        for name in ("ref", "default_branch", "github_output", "summary"):
            self.assertEqual("x", getattr(planning, name), name)
        self.assertEqual(("x/release-plan.json", "x/release-files", "x"), (publishing.plan, publishing.files, publishing.summary))


@unittest.skipUnless(shutil.which("bash"), "the job's commands run in Bash")
class CollectCommandTests(ReleaseFixture):
    """The commands of the job that collects the files, run as the job runs them, on a fixture consumer."""

    def run_step(self, name: str, **environment: str) -> subprocess.CompletedProcess:
        step = next(step for step in read_workflow(WORKFLOW)[1]["jobs"]["github-release-files"]["steps"] if step.get("name") == name)
        self.assertEqual(sorted(environment), sorted(step["env"]))
        return subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]], cwd=self.repository, capture_output=True, text=True, encoding="utf-8",
                              env={**os.environ, **environment})

    def collect(self, files: str) -> subprocess.CompletedProcess:
        self.collected = self.directory / "release-files"
        return self.run_step("Collect the files of the release", FILES=files, COLLECTED=self.collected.as_posix())

    def test_each_named_file_is_collected_under_its_file_name(self) -> None:
        self.commit({"dist/tool.zip": "the archive\n", "docs/user guide.pdf": "the guide\n", "README.md": "not released\n"})
        completed = self.collect("dist/tool.zip\ndocs/user guide.pdf")
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertEqual(["tool.zip", "user guide.pdf"], sorted(path.name for path in self.collected.iterdir()))
        self.assertEqual("the archive\n", (self.collected / "tool.zip").read_text(encoding="utf-8"))

    def test_a_named_file_that_does_not_exist_fails_the_command_and_is_named(self) -> None:
        self.commit({"dist/tool.zip": "the archive\n"})
        completed = self.collect("dist/tool.zip\ndist/absent.zip")
        self.assertEqual(1, completed.returncode)
        self.assertIn("::error::The file dist/absent.zip of the release does not exist.", completed.stdout)

    def test_a_file_whose_name_starts_with_a_dash_is_collected_as_a_file(self) -> None:
        self.commit({"-rf": "a file\n"})
        self.assertEqual(0, self.collect("-rf").returncode)
        self.assertEqual(["-rf"], [path.name for path in self.collected.iterdir()])

    def test_a_script_that_fails_ends_its_command_with_its_exit_status(self) -> None:
        self.commit({"ci/build.sh": "echo building\nexit 4\n"})
        completed = self.run_step("Run the script of the repository that writes the files", SCRIPT="ci/build.sh")
        self.assertEqual((4, "building\n"), (completed.returncode, completed.stdout))


@unittest.skipUnless(SSH_KEYGEN and shutil.which("bash"), "a signed fixture tag needs ssh-keygen, and the job's commands run in Bash")
class FixtureReleaseTests(ReleaseFixture):
    """A release of a fixture consumer from the pushed tag to the published files: the plan, then the commands of the
    job that collects the files, then the publishing, as the workflow chains them."""

    def setUp(self) -> None:
        super().setUp()
        self.github = GitHubStandIn()
        self.addCleanup(self.github.close)
        self.maintainer, line = self.key()
        self.commit({SIGNERS: line, "ci/build-release.sh": "mkdir -p dist\nprintf 'built from %s\\n' \"$(cat VERSION)\" > dist/tool.txt\n", "VERSION": "1.4.0\n", "LICENSE.md": "A licence.\n",
                     CONFIGURATION: configuration('    script: ci/build-release.sh\n    title: "Tool {version}"\n', files="[dist/tool.txt, LICENSE.md]")})
        self.publish_main()

    def release_run(self, tag: str) -> tuple[Outcome, Outcome | None]:
        """The run of the release workflow for a pushed tag: the plan, and the publishing when the plan decides on it."""
        planned = self.start_program("plan", "--ref", f"refs/tags/{tag}")
        if planned.status != 0 or not planned.part["run"]:
            return planned, None
        part = planned.part
        jobs = read_workflow(WORKFLOW)[1]["jobs"]
        collected = self.directory / f"release-files-{tag}"
        git(self.repository, "checkout", "--quiet", "--detach", part["commit"])
        for step in jobs["github-release-files"]["steps"]:
            values = {"SCRIPT": part["script"], "FILES": part["files"], "COLLECTED": collected.as_posix()}
            if "run" in step and (step["name"].startswith("Collect") or part["script"]):
                completed = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]], cwd=self.repository, capture_output=True, text=True, encoding="utf-8",
                                           env={**os.environ, **{name: values[name] for name in step["env"]}})
                self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        path = self.directory / "release-plan.json"
        path.write_text(planned.outputs["plan"], encoding="utf-8")
        self.github.hold_tag(tag, git(self.repository, "rev-parse", f"refs/tags/{tag}^{{commit}}"))
        if part["notes"]:  # GitHub holds the file as the tagged commit has it, whatever the job's script did to the working tree
            shown = subprocess.run(["git", "-C", str(self.repository), "cat-file", "blob", f"{part['commit']}:{part['notes']}"], capture_output=True, check=True)
            self.github.contents[(part["commit"], part["notes"])] = shown.stdout
        return planned, self.start_program("publish", "--plan", str(path), "--files", str(collected), "--repository", CONSUMER, "--api-url", self.github.url)

    def test_a_matching_tag_with_a_valid_signature_publishes_the_built_and_the_tracked_file(self) -> None:
        self.tag("v1.4.0", key=self.maintainer)
        planned, published = self.release_run("v1.4.0")
        self.assertEqual((0, 0), (planned.status, published.status), published.stdout + published.stderr)
        self.assertEqual([("v1.4.0", "Tool 1.4.0")], [(item["tag_name"], item["name"]) for item in self.github.published])
        self.assertEqual({"tool.txt": b"built from 1.4.0\n", "LICENSE.md": b"A licence.\n"}, self.github.assets[self.github.published[0]["id"]])

    def test_the_files_come_from_the_commit_of_the_tag_and_not_from_a_later_one(self) -> None:
        self.tag("v1.4.0", key=self.maintainer)
        self.commit({"VERSION": "2.0.0\n"})
        _, published = self.release_run("v1.4.0")
        self.assertEqual(0, published.status, published.stdout + published.stderr)
        self.assertEqual(b"built from 1.4.0\n", self.github.assets[self.github.published[0]["id"]]["tool.txt"])

    def test_the_release_notes_are_the_tracked_file_of_the_tagged_commit_whatever_the_script_writes(self) -> None:
        self.commit({"NOTES.md": "## 1.4.0\n\nThe tracked notes.\n", "ci/build-release.sh": "mkdir -p dist\necho built > dist/tool.txt\necho 'Notes of the script.' > NOTES.md\n",
                     CONFIGURATION: configuration("    script: ci/build-release.sh\n    notes: NOTES.md\n", files="[dist/tool.txt]")})
        self.tag("v1.4.0", key=self.maintainer)
        self.commit({"NOTES.md": "Notes of a later commit.\n"})
        planned, published = self.release_run("v1.4.0")
        self.assertEqual((0, 0), (planned.status, published.status), published.stdout + published.stderr)
        self.assertEqual("## 1.4.0\n\nThe tracked notes.\n", self.github.published[0]["body"])

    def test_the_list_of_changes_starts_after_the_nearest_earlier_tag_with_a_published_release(self) -> None:
        self.tag("v1.2.0", key=self.maintainer)
        self.commit({"VERSION": "1.3.0\n"})
        self.tag("v1.3.0", key=self.maintainer)
        self.commit({"VERSION": "1.3.5\n"})
        self.tag("candidate", key=self.maintainer)  # matches no pattern of tags
        self.github.hold_release("candidate")
        self.commit({"VERSION": "1.4.0\n"})
        self.tag("v1.4.0", key=self.maintainer)
        self.github.hold_release("v1.2.0")
        self.github.hold_release("v1.3.0", draft=True)
        planned, published = self.release_run("v1.4.0")
        self.assertEqual((0, 0), (planned.status, published.status), published.stdout + published.stderr)
        self.assertEqual([{"tag_name": "v1.4.0", "target_commitish": planned.part["commit"], "previous_tag_name": "v1.2.0"}], self.github.generate_requests())
        self.github.releases.clear()
        self.github.hold_release("v1.2.0")
        self.github.hold_release("v1.3.0")
        self.github.requests.clear()
        planned, published = self.release_run("v1.4.0")
        self.assertEqual(0, published.status, published.stdout + published.stderr)
        self.assertEqual("v1.3.0", self.github.generate_requests()[0]["previous_tag_name"])

    def test_a_tag_that_is_moved_to_a_later_commit_while_the_run_collects_its_files_is_not_released(self) -> None:
        self.tag("v1.4.0", key=self.maintainer)
        verified = self.repository_head()
        hold_tag = self.github.hold_tag
        later = self.commit({"VERSION": "6.6.6\n"})
        # The run finds the tag on GitHub where a push moved it after the plan verified its signature.
        self.github.hold_tag = lambda tag, _commit: hold_tag(tag, later)
        planned, published = self.release_run("v1.4.0")
        self.assertEqual((0, 1), (planned.status, published.status), published.stdout + published.stderr)
        self.assertEqual(verified, planned.part["commit"])
        self.assertEqual({}, self.github.releases)
        self.assertIn(f"the tag `v1.4.0` moved after its signature was verified: it names the commit `{later}`", published.summary)

    def test_a_tag_that_matches_no_pattern_and_a_matching_tag_without_a_valid_signature_reach_github_with_no_request(self) -> None:
        stranger, _ = self.key("stranger")
        self.tag("candidate", key=self.maintainer)
        self.tag("v1.5.0", annotated=True)
        self.tag("v1.6.0", key=stranger)
        for tag, status in (("candidate", 0), ("v1.5.0", 1), ("v1.6.0", 1)):
            with self.subTest(tag=tag):
                planned, published = self.release_run(tag)
                self.assertEqual((status, None), (planned.status, published))
        self.assertEqual([], self.github.requests)


if __name__ == "__main__":
    unittest.main()
