"""The push lookup and shared CI digest lookup of select, on fixture repositories with recorded API responses."""

from __future__ import annotations

import http.server
import io
import json
import os
import re
import threading
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.suite_selection_fixtures import git
from tests.suite_selection_reuse_support import (
    FixtureRepository,
    reuse,
    suite_selection,
)

FULL_ID = re.compile(r"[0-9a-f]{40}")
NOT_FOUND = (404, b'{"message": "Not Found"}', {})


class RecordedApi:
    """A local HTTP server that answers the recorded responses by path and records every request. Like GitHub, it
    answers the Git database read of a commit only for a full commit id."""

    def __init__(self) -> None:
        self.responses: dict[str, tuple[int, bytes, dict[str, str]]] = {}
        self.requests: list[tuple[str, str | None]] = []
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                api.requests.append((self.path, self.headers.get("Authorization")))
                status, body, headers = api.responses.get(self.path, NOT_FOUND)
                commit = self.path.removeprefix("/repos/owner/repository/git/commits/")
                if commit != self.path and not FULL_ID.fullmatch(commit):
                    status, body, headers = NOT_FOUND
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value.replace("{url}", api.url))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *arguments) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def json(self, path: str, document, request_id: str = "") -> None:
        headers = {"Content-Type": "application/json"}
        if request_id:
            headers["X-GitHub-Request-Id"] = request_id
        self.responses["/repos/owner/repository/" + path] = (200, json.dumps(document).encode(), headers)

    def log(self, job: int, text: str) -> None:
        # The job-log endpoint redirects to a storage URL, which must be read without the token.
        self.responses[f"/repos/owner/repository/actions/jobs/{job}/logs"] = (302, b"", {"Location": "{url}/storage/" + str(job)})
        self.responses[f"/storage/{job}"] = (200, text.encode(), {"Content-Type": "text/plain"})

    def artifact(self, run: int, artifact_id: int, name: str, files: dict[str, bytes] | bytes, *, expired: bool = False) -> None:
        """Serve a workflow run artifact list and redirected zip archive (read without token)."""
        self.json(f"actions/runs/{run}/artifacts?name={name}", {
            "total_count": 1,
            "artifacts": [{"id": artifact_id, "name": name, "expired": expired}],
        })
        if isinstance(files, bytes):
            archive = files
        else:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as zf:
                for member, content in files.items():
                    zf.writestr(member, content)
            archive = buffer.getvalue()
        self.responses[f"/repos/owner/repository/actions/artifacts/{artifact_id}/zip"] = (
            302, b"", {"Location": "{url}/artifact-storage/" + str(artifact_id)}
        )
        self.responses[f"/artifact-storage/{artifact_id}"] = (200, archive, {"Content-Type": "application/zip"})


class SharedLookupFixture(FixtureRepository):
    """A fixture repository with a default-branch record for the shared lookup, and the commands that run it and select over it."""

    DIGESTS = {"language:python": "3" * 64, "platform:linux": "a" * 64, "platform:windows": "1" * 64, "tool:gitleaks": "2" * 64}
    MOVED = {"language:python": "3" * 64, "platform:linux": "b" * 64, "platform:windows": "9" * 64, "tool:gitleaks": "2" * 64}
    RUN = 5100
    ALL = ["both", "main-only", "per-entry", "pr-only", "windows"]

    def setUp(self) -> None:
        super().setUp()
        self.record = self.root.parent / "record.json"
        self.default_record = self.root.parent / "default-record.json"

    def write_default_record(self, *, run_id: int = RUN, commit: str | None = None, digests: dict[str, str] | None = DIGESTS,
                             passed: dict[str, int] | None = None, raw_text: str | None = None) -> Path:
        if raw_text is not None:
            self.default_record.write_text(raw_text, encoding="utf-8")
            return self.default_record
        tip = commit or self.base
        tree = git(self.root, "rev-parse", f"{tip}^{{tree}}")
        document = {"version": 1, "run_id": run_id, "commit": tip, "tree": tree, "digests": digests, "passed": passed or {}}
        self.default_record.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        return self.default_record

    def shared_lookup(self, digests: dict[str, str] | None = None, default_record: Path | None = None) -> Path:
        self.printed = io.StringIO()
        output = self.root.parent / "shared.json"
        rendered = json.dumps(self.DIGESTS if digests is None else digests, sort_keys=True, separators=(",", ":"))
        record_path = self.default_record if default_record is None else default_record
        with redirect_stdout(self.printed):
            code = suite_selection.main(["shared-lookup", "--root", str(self.root), "--digests", rendered,
                                         "--default-record", str(record_path), "--output", str(output)])
        self.assertEqual(code, 0)
        return output

    def looked_up(self, digests: dict[str, str] | None = None, default_record: Path | None = None) -> dict:
        return json.loads(self.shared_lookup(digests, default_record).read_text(encoding="utf-8"))

    def runs(self, outputs: dict[str, str]) -> list[str]:
        return sorted(name for name, result in json.loads(outputs["suites"]).items() if result == "run")

    def every_suite_runs(self, base: str) -> str:
        head = self.commit("head", {"README.md": "a change no suite reads\n"})
        outputs, printed, _ = self.select("--base", base, "--head", head, "--compare", "merge-base",
                                          "--shared-lookup", str(self.root.parent / "shared.json"))
        self.assertEqual(self.runs(outputs), self.ALL)
        return printed


class PushLookupTests(FixtureRepository):
    """The push lookup with structured PR tested-state artifacts and default-branch records, without log scraping."""

    NUMBER = 7
    RUN = 4100
    ARTIFACT = 4300
    DIGESTS = {"language:python": "e" * 64, "platform:linux": "c" * 64, "platform:windows": "d" * 64}

    def setUp(self) -> None:
        super().setUp()
        self.api = RecordedApi()
        self.addCleanup(self.api.close)
        # The pull request branches from the base and changes src/feature.py; GitHub tests its merge into the base.
        git(self.root, "checkout", "-q", "-b", "topic")
        self.pr_head = self.commit("feature", {"src/feature.py": "feature v2\n"})
        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "--no-ff", "-m", f"Merge {self.pr_head} into {self.base}", "topic")
        self.merge = git(self.root, "rev-parse", "HEAD")
        git(self.root, "reset", "-q", "--hard", self.base)

    def write_default_record(self, commit: str, *, run_id: int = 5100, digests: dict[str, str] | None = None, passed: dict[str, int] | None = None) -> Path:
        path = self.root.parent / "default-record.json"
        tree = git(self.root, "rev-parse", f"{commit}^{{tree}}")
        payload = {
            "version": 1,
            "run_id": run_id,
            "commit": commit,
            "tree": tree,
            "digests": self.DIGESTS if digests is None else digests,
            "passed": passed or {},
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def record(self, *, artifact: bool = True, expired: bool = False, state_override: dict | None = None,
               raw_zip: bytes | None = None, parents: list[str] | None = None, pulls: list | None = None,
               digests: dict[str, str] | None = DIGESTS, runs: list | None = None) -> None:
        self.api.json(f"commits/{self.pushed}/pulls", pulls if pulls is not None else
                      [{"number": self.NUMBER, "state": "closed", "merge_commit_sha": self.pushed, "head": {"sha": self.pr_head, "ref": "topic"}}])
        workflow_runs = runs if runs is not None else [
            {"id": self.RUN, "head_sha": self.pr_head, "event": "pull_request", "conclusion": "success"}
        ]
        self.api.json(f"actions/workflows/validate.yml/runs?head_sha={self.pr_head}&event=pull_request&status=success&per_page=20",
                      {"total_count": len(workflow_runs), "workflow_runs": workflow_runs})
        tree = git(self.root, "rev-parse", f"{self.merge}^{{tree}}")
        if artifact:
            state = {
                "version": 1,
                "pull_request": self.NUMBER,
                "run_id": self.RUN,
                "run_attempt": 1,
                "head": self.pr_head,
                "tested_base": self.base,
                "tested_merge": self.merge,
                "tree": tree,
                "digests": digests,
            }
            if state_override:
                state.update(state_override)
            payload = raw_zip if raw_zip is not None else {"tested-state.json": json.dumps(state).encode("utf-8")}
            self.api.artifact(self.RUN, self.ARTIFACT, "tested-state", payload, expired=expired)
        else:
            self.api.json(f"actions/runs/{self.RUN}/artifacts?name=tested-state", {"total_count": 0, "artifacts": []})
        self.api.json(f"git/commits/{self.merge}", {"sha": self.merge, "tree": {"sha": tree},
                                                    "parents": [{"sha": sha} for sha in (parents or [self.base, self.pr_head])]})

    def lookup(self, sha: str, before: str, attempt: int = 1, default_record: Path | None = None) -> tuple[dict, str]:
        output = self.root.parent / "lookup.json"
        printed = io.StringIO()
        environment = {"GITHUB_API_URL": self.api.url, "GITHUB_REPOSITORY": "owner/repository", "GITHUB_TOKEN": "token-for-the-test"}
        extra = ["--default-record", str(default_record)] if default_record is not None else []
        with mock.patch.dict(os.environ, environment), redirect_stdout(printed):
            code = suite_selection.main(["lookup", "--root", str(self.root), "--sha", sha, "--before", before,
                                         "--workflow", "owner/repository/.github/workflows/validate.yml@refs/heads/main",
                                         "--run-attempt", str(attempt), *extra, "--output", str(output)])
        self.assertEqual(code, 0)
        return json.loads(output.read_text(encoding="utf-8")), printed.getvalue()

    def squash_onto(self, base: str) -> str:
        git(self.root, "reset", "-q", "--hard", base)
        return self.commit("feat: the feature (#7)", {"src/feature.py": "feature v2\n"})

    def test_write_tested_state_records_the_merge_commit_tree_and_digests(self) -> None:
        git(self.root, "checkout", "-q", self.merge)
        output = self.root.parent / "tested-state.json"
        rendered = json.dumps(self.DIGESTS, sort_keys=True, separators=(",", ":"))
        with redirect_stdout(io.StringIO()):
            code = suite_selection.main([
                "tested-state", "--root", str(self.root), "--pull-request", str(self.NUMBER),
                "--run-id", str(self.RUN), "--run-attempt", "1", "--digests", rendered, "--output", str(output),
            ])
        self.assertEqual(0, code)
        state = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual({
            "version": 1,
            "pull_request": self.NUMBER,
            "run_id": self.RUN,
            "run_attempt": 1,
            "head": self.pr_head,
            "tested_base": self.base,
            "tested_merge": self.merge,
            "tree": git(self.root, "rev-parse", f"{self.merge}^{{tree}}"),
            "digests": self.DIGESTS,
        }, state)

    def test_the_lookup_reads_the_structured_tested_state_artifact_without_reading_logs_and_verifies_the_merge(self) -> None:
        self.pushed = self.squash_onto(self.base)
        self.record()
        found, _ = self.lookup(self.pushed, self.base)
        self.assertEqual(found["case"], "identical", found)
        self.assertEqual((found["pull_request"], found["tested_merge"], found["tested_base"], found["tested_run"], found["tested_digests"]),
                         (self.NUMBER, self.merge, self.base, self.RUN, self.DIGESTS))
        self.assertIsNone(found["fallback"])
        paths = [path for path, _ in self.api.requests]
        self.assertEqual(paths, [
            f"/repos/owner/repository/commits/{self.pushed}/pulls",
            f"/repos/owner/repository/actions/workflows/validate.yml/runs?head_sha={self.pr_head}&event=pull_request&status=success&per_page=20",
            f"/repos/owner/repository/actions/runs/{self.RUN}/artifacts?name=tested-state",
            f"/repos/owner/repository/actions/artifacts/{self.ARTIFACT}/zip",
            f"/artifact-storage/{self.ARTIFACT}",
            f"/repos/owner/repository/git/commits/{self.merge}",
        ])
        self.assertFalse(any("/logs" in path or "/jobs" in path for path in paths), "push lookup reads no job logs")
        tokens = dict(self.api.requests)
        self.assertEqual(tokens[f"/repos/owner/repository/commits/{self.pushed}/pulls"], "Bearer token-for-the-test")
        self.assertIsNone(tokens[f"/artifact-storage/{self.ARTIFACT}"], "the redirected artifact archive is read without the token")

    def test_the_lookup_skips_an_edited_run_without_tested_state_and_takes_the_previous_code_run(self) -> None:
        self.pushed = self.squash_onto(self.base)
        self.record(runs=[
            {"id": self.RUN + 1, "head_sha": self.pr_head, "event": "pull_request", "conclusion": "success"},
            {"id": self.RUN, "head_sha": self.pr_head, "event": "pull_request", "conclusion": "success"},
        ])
        self.api.json(f"actions/runs/{self.RUN + 1}/artifacts?name=tested-state", {"total_count": 0, "artifacts": []})
        found, _ = self.lookup(self.pushed, self.base)
        self.assertEqual((found["case"], found["tested_run"]), ("identical", self.RUN))

    def test_the_recorded_git_database_answers_only_a_full_commit_id(self) -> None:
        self.api.json(f"git/commits/{self.merge[:7]}", {"sha": self.merge})
        self.api.json(f"git/commits/{self.merge}", {"sha": self.merge})
        api = reuse.Api(self.api.url, "owner/repository", None)
        self.assertEqual(api.json(f"git/commits/{self.merge}"), {"sha": self.merge})
        with self.assertRaisesRegex(reuse.ApiError, "answered 404"):
            api.json(f"git/commits/{self.merge[:7]}")

    def test_a_missing_expired_or_unreadable_pr_record_falls_back_with_a_counted_notice(self) -> None:
        self.pushed = self.squash_onto(self.base)
        for label, kwargs, expected_fallback in (
                ("missing artifact", {"artifact": False}, "missing-pr-record"),
                ("expired artifact", {"artifact": True, "expired": True}, "missing-pr-record"),
                ("corrupt zip", {"artifact": True, "raw_zip": b"not a zip"}, "unreadable-pr-record"),
                ("wrong pull request", {"artifact": True, "state_override": {"pull_request": self.NUMBER + 1}}, "unreadable-pr-record"),
                ("unverified parents", {"artifact": True, "parents": [self.base, self.base]}, "unreadable-pr-record")):
            with self.subTest(case=label):
                self.api.responses.clear()
                self.api.requests.clear()
                self.record(**kwargs)
                found, printed = self.lookup(self.pushed, self.base)
                self.assertEqual("pull-request", found["case"], found)
                self.assertEqual(expected_fallback, found["fallback"], found)
                self.assertIn(f"::notice title=ci-fallback::{expected_fallback}:", printed)
                lookup = self.root.parent / "lookup.json"
                lookup.write_text(json.dumps(found), encoding="utf-8")
                unit_decisions = self.root.parent / "unit-decisions.json"
                _, _, summary = self.select("--lookup", str(lookup), "--unit-decisions", str(unit_decisions))
                decisions_doc = json.loads(unit_decisions.read_text(encoding="utf-8"))
                self.assertEqual({expected_fallback: 1}, decisions_doc["fallbacks"])
                self.assertTrue(decisions_doc["shards"])
                self.assertTrue(all(set(item) == {"shard", "estimate"} for item in decisions_doc["shards"]))
                self.assertIn(f"The selection runs over the pull request's files: {found['reason']}.", summary)

    def test_a_push_whose_tree_equals_the_passed_tested_tree_runs_no_suite(self) -> None:
        self.pushed = self.squash_onto(self.base)
        self.record()
        lookup = self.root.parent / "lookup.json"
        found, _ = self.lookup(self.pushed, self.base)
        lookup.write_text(json.dumps(found), encoding="utf-8")
        outputs, printed, summary = self.select("--lookup", str(lookup), "--language-step", "python")
        self.assertEqual(json.loads(outputs["suites"]), {name: "skip" for name in ("pr-only", "both", "main-only", "per-entry", "windows")})
        self.assertEqual((outputs["shards"], outputs["windows"], outputs["language-steps"]), ("[]", "", '{"python":"skip"}'))
        self.assertIn(f"the tree equals the tested tree that passed on pull request #{self.NUMBER}", printed)
        self.assertIn("## Push lookup\n\nNo suite runs: ", summary)

    def test_a_behind_merge_onto_a_passed_newer_base_runs_only_what_both_sides_affect_and_uses_pr_digests(self) -> None:
        moved = self.commit("main moves", {"src/moved.py": "moved v2\n"})
        self.pushed = self.squash_onto(moved)
        pr_digests = dict(self.DIGESTS, **{"platform:linux": "9" * 64})
        # Main passed under old DIGESTS, while the PR was tested under pr_digests (e.g. a PR changing the recorder).
        default_rec = self.write_default_record(moved, run_id=5200, digests=self.DIGESTS)
        self.record(digests=pr_digests)
        found, printed = self.lookup(self.pushed, moved, default_record=default_rec)
        self.assertEqual(("behind", self.base, moved, True, self.RUN, pr_digests, None),
                         (found["case"], found["tested_base"], found["landing_base"], found["landing_base_passed"],
                          found["tested_run"], found["tested_digests"], found["fallback"]), found)
        self.assertNotIn("::notice title=ci-fallback::", printed)
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps(found), encoding="utf-8")
        shared = self.root.parent / "shared.json"
        with redirect_stdout(io.StringIO()):
            code = suite_selection.main(["shared-lookup", "--root", str(self.root),
                                         "--digests", json.dumps(pr_digests, sort_keys=True, separators=(",", ":")),
                                         "--default-record", str(default_rec), "--output", str(shared)])
        self.assertEqual(0, code)
        outputs, printed, _ = self.select("--lookup", str(lookup), "--shared-lookup", str(shared))
        self.assertEqual(json.loads(outputs["suites"]), {"pr-only": "skip", "both": "run", "main-only": "skip", "per-entry": "skip", "windows": "skip"},
                         "only what both the change and the passed newer base affect runs; PR tested digests decide instead of old main digests")
        self.assertIn("both: run (both sides: `src/feature.py` matches `src/feature.py`; `src/moved.py` matches `src/moved.py`)", printed)
        self.assertEqual([entry["entry"] for shard in json.loads(outputs["shards"]) for entry in shard["entries"]], ["tests/test_both.py"])

    def test_a_behind_merge_onto_an_unpassed_newer_base_takes_over_nothing_the_newer_base_can_affect(self) -> None:
        moved = self.commit("main moves", {"src/moved.py": "moved v2\n"})
        self.pushed = self.squash_onto(moved)
        self.record(digests=self.DIGESTS)
        # Default-branch record is still at self.base (moved has NOT passed yet!), or missing/evicted.
        for label, default_rec, expected_fb in (
                ("stale default record", self.write_default_record(self.base, run_id=5100), "unpassed-landing-base"),
                ("missing default record", self.root.parent / "absent-default.json", "missing-default-branch-record")):
            with self.subTest(case=label):
                found, lookup_printed = self.lookup(self.pushed, moved, default_record=default_rec)
                self.assertEqual(("behind", False, expected_fb),
                                 (found["case"], found["landing_base_passed"], found["fallback"]), found)
                self.assertIn(f"::notice title=ci-fallback::{expected_fb}:", lookup_printed)
                lookup = self.root.parent / "lookup.json"
                lookup.write_text(json.dumps(found), encoding="utf-8")
                shared = self.root.parent / "shared.json"
                with redirect_stdout(io.StringIO()):
                    suite_selection.main(["shared-lookup", "--root", str(self.root),
                                          "--digests", json.dumps(self.DIGESTS, sort_keys=True, separators=(",", ":")),
                                          "--default-record", str(default_rec), "--output", str(shared)])
                unit_decisions = self.root.parent / "unit-decisions.json"
                outputs, _, _ = self.select("--lookup", str(lookup), "--shared-lookup", str(shared),
                                            "--unit-decisions", str(unit_decisions))
                self.assertEqual({"pr-only": "skip", "both": "run", "main-only": "run", "per-entry": "skip", "windows": "skip"},
                                 json.loads(outputs["suites"]),
                                 "nothing the unpassed newer base can affect (both, main-only) is taken over")
                self.assertEqual({expected_fb: 1}, json.loads(unit_decisions.read_text(encoding="utf-8"))["fallbacks"])

    def test_a_push_without_a_pull_request_selects_over_its_own_changed_files(self) -> None:
        self.pushed = self.commit("direct push", {"src/moved.py": "moved v3\n"})
        self.record(pulls=[])
        found, _ = self.lookup(self.pushed, self.base)
        self.assertEqual((found["case"], found["pull_request"], found["fallback"]), ("own", None, None), found)
        self.assertEqual(len(self.api.requests), 1)
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps(found), encoding="utf-8")
        outputs, _, _ = self.select("--lookup", str(lookup))
        self.assertEqual(json.loads(outputs["suites"]), {"pr-only": "skip", "both": "run", "main-only": "run", "per-entry": "skip", "windows": "skip"})

    def test_a_dependabot_merged_run_starts_its_lookup_from_the_payload_commit(self) -> None:
        self.pushed = self.squash_onto(self.base)
        later = self.commit("a later commit on the default branch", {"README.md": "later\n"})
        self.record()
        found, _ = self.lookup(self.pushed, "")
        self.assertEqual((found["case"], found["head"], found["landing_base"]), ("identical", self.pushed, self.base))
        self.assertEqual(self.api.requests[0][0], f"/repos/owner/repository/commits/{self.pushed}/pulls")
        self.assertNotIn(later, " ".join(path for path, _ in self.api.requests))

    def test_a_re_run_of_a_push_makes_no_lookup_and_selects_over_its_own_changed_files(self) -> None:
        self.pushed = self.squash_onto(self.base)
        self.record()
        found, _ = self.lookup(self.pushed, self.base, attempt=2)
        self.assertEqual((found["case"], found["landing_base"]), ("own", self.base), found)
        self.assertEqual(self.api.requests, [])
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps(found), encoding="utf-8")
        outputs, _, _ = self.select("--lookup", str(lookup))
        self.assertEqual(json.loads(outputs["suites"])["pr-only"], "run")

    def test_an_unreachable_api_never_skips_and_counts_as_a_fallback(self) -> None:
        self.pushed = self.squash_onto(self.base)
        self.api.close()
        found, printed = self.lookup(self.pushed, self.base)
        self.assertEqual(("own", "push-lookup-api-error"), (found["case"], found["fallback"]))
        self.assertIn("could not be looked up", found["reason"])
        self.assertIn("::notice title=ci-fallback::push-lookup-api-error:", printed)


class SharedLookupTests(SharedLookupFixture):
    """In a consumer, shared CI digests come from the default-branch record with no log scraping and no commit-walk
    bound. Missing, evicted or unreadable records or digests fail closed and emit counted fallback notices."""

    def test_the_digest_of_the_default_branch_record_decides_whether_every_suite_runs(self) -> None:
        self.write_default_record()
        head = self.commit("head", {"README.md": "a change no suite reads\n"})
        outputs, _, summary = self.select("--base", self.base, "--head", head, "--compare", "merge-base",
                                          "--shared-lookup", str(self.shared_lookup(self.DIGESTS)))
        self.assertEqual(self.runs(outputs), [])
        self.assertIn("The selection runs as usual: the shared CI digests equal those of run 5100", summary)
        outputs, printed, summary = self.select("--base", self.base, "--head", head, "--compare", "merge-base",
                                                "--shared-lookup", str(self.shared_lookup(self.MOVED)))
        self.assertEqual(self.runs(outputs), self.ALL)
        self.assertEqual(sorted(entry["entry"] for shard in json.loads(outputs["shards"]) for entry in shard["entries"]),
                         ["tests/test_both.py", "tests/test_main.py", "tests/test_one.py", "tests/test_pr.py", "tests/test_two.py"])
        self.assertIn("pr-only: run (`platform:linux` digest moved:", printed)
        self.assertIn("## Shared CI digests\n\nMoved digests (`platform:linux`, `platform:windows`): ", summary)

    def test_a_windows_only_digest_move_runs_only_windows_suites_and_reuses_linux_units(self) -> None:
        self.write_default_record()
        head = self.commit("head", {"README.md": "a change no suite reads\n"})
        windows_moved = dict(self.DIGESTS, **{"platform:windows": "9" * 64})
        outputs, printed, _ = self.select("--base", self.base, "--head", head, "--compare", "merge-base",
                                          "--shared-lookup", str(self.shared_lookup(windows_moved)))
        self.assertEqual(self.runs(outputs), ["windows"], "only Windows suites run when only platform:windows moves")
        self.assertIn("windows: run (`platform:windows` digest moved:", printed)

        # Reuse keys: Linux units keep their keys when only platform:windows moves; Windows units get new keys.
        first = self.commit("first", {"src/feature.py": "feature v2\n"})
        arguments = ("--base", self.base, "--compare", "merge-base", "--results", str(self.record), "--runner-label", "linux=ubuntu-26.04")
        outputs, _, _ = self.select("--head", first, *arguments, "--shared-lookup", str(self.shared_lookup(self.DIGESTS)))
        units = json.loads(outputs["units"])
        self.record.write_text(json.dumps({"passed": {key: 9101 for key in units}}), encoding="utf-8")
        self.write_default_record(digests=windows_moved)
        outputs, _, _ = self.select("--head", first, *arguments, "--shared-lookup", str(self.shared_lookup(windows_moved)))
        self.assertEqual(self.runs(outputs), ["windows"], "Linux units are reused when only platform:windows changes")
        self.assertTrue(json.loads(outputs["reused"]), "Linux passes are reused")

    def test_a_pinned_tool_digest_move_runs_and_rekeys_only_units_that_use_that_tool(self) -> None:
        suites_doc = json.loads((self.root / "suites.json").read_text(encoding="utf-8"))
        for item in suites_doc["suites"]:
            if item["name"] == "pr-only":
                item["setups"] = ["gitleaks"]
        self.commit("add gitleaks setup", {"suites.json": json.dumps(suites_doc, indent=2) + "\n"})
        tip = git(self.root, "rev-parse", "HEAD")
        self.write_default_record(commit=tip, digests=self.DIGESTS)
        head = self.commit("head", {"README.md": "a change no suite reads\n"})
        gitleaks_moved = dict(self.DIGESTS, **{"tool:gitleaks": "f" * 64})
        outputs, printed, _ = self.select("--base", tip, "--head", head, "--compare", "merge-base",
                                          "--shared-lookup", str(self.shared_lookup(gitleaks_moved)))
        self.assertEqual(self.runs(outputs), ["pr-only"], "only the suite using gitleaks runs")
        self.assertIn("pr-only: run (`tool:gitleaks` digest moved:", printed)

        # Reuse keys: only the unit using gitleaks loses its cached pass when tool:gitleaks changes.
        first = self.commit("first", {"src/feature.py": "feature v2\n"})
        arguments = ("--base", tip, "--compare", "merge-base", "--results", str(self.record), "--runner-label", "linux=ubuntu-26.04")
        outputs, _, _ = self.select("--head", first, *arguments, "--shared-lookup", str(self.shared_lookup(self.DIGESTS)))
        units = json.loads(outputs["units"])
        self.record.write_text(json.dumps({"passed": {key: 9101 for key in units}}), encoding="utf-8")
        self.write_default_record(commit=tip, digests=gitleaks_moved)
        outputs, _, _ = self.select("--head", first, *arguments, "--shared-lookup", str(self.shared_lookup(gitleaks_moved)))
        self.assertEqual(self.runs(outputs), ["pr-only"], "units not using gitleaks are reused")

    def test_a_language_step_digest_move_runs_only_that_language_step_and_not_suites(self) -> None:
        self.write_default_record()
        head = self.commit("head", {"README.md": "a change no suite reads\n"})
        lang_moved = dict(self.DIGESTS, **{"language:python": "e" * 64})
        outputs, _, _ = self.select("--base", self.base, "--head", head, "--compare", "merge-base",
                                    "--language-step", "python", "--shared-lookup", str(self.shared_lookup(lang_moved)))
        self.assertEqual(self.runs(outputs), [], "no test suite runs when only language:python moves")
        self.assertEqual(json.loads(outputs["language-steps"]), {"python": "run"})
        self.assertEqual(len(json.loads(outputs["shards"])), 1, "shard 1 runs to execute the language step")

        # On an identical push whose PR ran under the old digests, a moved language step still runs and clears identical.
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps({"case": "identical", "head": self.base, "landing_base": self.base, "reason": "tested",
                                      "pull_request": 7, "tested_run": 4100, "tested_digests": self.DIGESTS}), encoding="utf-8")
        outputs, _, _ = self.select("--lookup", str(lookup), "--language-step", "python", "--shared-lookup", str(self.shared_lookup(lang_moved)))
        self.assertEqual(self.runs(outputs), [])
        self.assertEqual(json.loads(outputs["language-steps"]), {"python": "run"})
        self.assertNotIn("identical", outputs)

    def test_the_shared_lookup_has_no_commit_walk_limit_and_makes_no_api_calls(self) -> None:
        self.write_default_record(commit=self.base, digests=self.DIGESTS)
        # 25 commits later (more than the old 20-commit walk limit), the default-branch record still resolves directly.
        for index in range(25):
            self.commit(f"commit {index}")
        tip = git(self.root, "rev-parse", "HEAD")
        found = self.looked_up(self.DIGESTS)
        self.assertEqual((found["run"], found["previous"], found["commit"], found["fallback"]), (self.RUN, self.DIGESTS, self.base, None))
        head = self.commit("head", {"README.md": "a change no suite reads\n"})
        outputs, _, _ = self.select("--base", tip, "--head", head, "--compare", "merge-base",
                                    "--shared-lookup", str(self.root.parent / "shared.json"))
        self.assertEqual(self.runs(outputs), [])

    def test_missing_evicted_or_unreadable_default_branch_record_or_digests_runs_every_suite_and_counts_fallback(self) -> None:
        head = self.commit("head", {"README.md": "a change no suite reads\n"})
        cases = [
            ("missing", None, "missing-default-branch-record"),
            ("corrupt json", "{not-json", "unreadable-default-branch-record"),
            ("wrong version", json.dumps({"version": 99, "run_id": 1, "commit": self.base, "tree": self.base, "passed": {}}),
             "unreadable-default-branch-record"),
            ("no digests", "NO_DIGESTS", "unreadable-default-branch-digests"),
        ]
        for label, payload, expected_fallback in cases:
            with self.subTest(label=label):
                if self.default_record.exists():
                    self.default_record.unlink()
                if payload == "NO_DIGESTS":
                    self.write_default_record(digests=None)
                elif payload is not None:
                    self.write_default_record(raw_text=payload)
                found = self.looked_up(self.DIGESTS)
                self.assertEqual((found["digests"], found["previous"], found["fallback"]), (self.DIGESTS, None, expected_fallback))
                self.assertIn(f"::notice title=ci-fallback::{expected_fallback}:", self.printed.getvalue())
                decisions = self.root.parent / "decisions.json"
                outputs, _, summary = self.select("--base", self.base, "--head", head, "--compare", "merge-base",
                                                  "--shared-lookup", str(self.root.parent / "shared.json"),
                                                  "--unit-decisions", str(decisions))
                self.assertEqual(self.runs(outputs), self.ALL)
                self.assertEqual(json.loads(decisions.read_text(encoding="utf-8"))["fallbacks"], {expected_fallback: 1})
                self.assertIn(f"Fallbacks: `{expected_fallback}` (1).", summary)

    def test_a_push_whose_tree_passed_under_the_same_digest_runs_no_suite_after_a_move(self) -> None:
        self.write_default_record(digests=self.DIGESTS)
        lookup = self.root.parent / "lookup.json"
        tested = {"case": "identical", "head": self.base, "landing_base": self.base, "reason": "tested", "pull_request": 7, "tested_run": 4100}
        lookup.write_text(json.dumps({**tested, "tested_digests": self.MOVED}), encoding="utf-8")
        outputs, printed, summary = self.select("--lookup", str(lookup), "--shared-lookup", str(self.shared_lookup(self.MOVED)))
        self.assertEqual(self.runs(outputs), [], printed)
        self.assertEqual("true", outputs["identical"], "the verdict skips a push that runs nothing but select and hygiene")
        self.assertIn("the tested tree took the same shared CI digests in run 4100 of pull request #7", summary)

        lookup.write_text(json.dumps({**tested, "tested_digests": self.DIGESTS}), encoding="utf-8")
        for digests in (self.MOVED, self.DIGESTS):
            with self.subTest(digests=digests):
                self.write_default_record(digests=digests)
                outputs, printed, _ = self.select("--lookup", str(lookup), "--shared-lookup", str(self.shared_lookup(self.MOVED)))
                self.assertEqual(self.runs(outputs), self.ALL, "a tree that passed under other platform digests runs every suite")
                self.assertNotIn("identical", outputs)
                self.assertIn("pr-only: run (`platform:linux` digest moved:", printed)

    def test_a_push_with_unreadable_tested_digests_runs_every_suite_instead_of_falling_back_to_base_digests(self) -> None:
        # Even when the default-branch record has matching digests, missing tested_digests on an identical or behind
        # push must fail closed (never silently fall back to base-branch digests).
        self.write_default_record(digests=self.DIGESTS)
        shared_path = self.shared_lookup(self.DIGESTS)
        for case, extra in (("identical", {}),
                            ("behind", {"tested_base": self.base, "landing_base_passed": True})):
            with self.subTest(case=case):
                lookup = self.root.parent / "lookup.json"
                lookup.write_text(json.dumps({"case": case, "head": self.base, "landing_base": self.base,
                                              "reason": "tested", "pull_request": 7, "tested_run": 4100,
                                              "tested_digests": None, **extra}), encoding="utf-8")
                decisions = self.root.parent / "decisions.json"
                outputs, printed, _ = self.select("--lookup", str(lookup), "--shared-lookup", str(shared_path),
                                                  "--unit-decisions", str(decisions))
                self.assertEqual(self.runs(outputs), self.ALL)
                self.assertNotIn("identical", outputs)
                self.assertIn("::notice title=ci-fallback::unreadable-tested-digests:", printed)
                self.assertEqual(json.loads(decisions.read_text(encoding="utf-8"))["fallbacks"], {"unreadable-tested-digests": 1})

    def test_a_behind_push_compares_against_its_tested_digests_not_only_the_default_branch_record(self) -> None:
        # PR was tested under DIGESTS; v1 moved to MOVED before merge; default branch already has a passed record under MOVED.
        # The behind push must still run every suite because its own PR tested_digests are DIGESTS.
        on_main = self.commit("main moves on", {"src/other.py": "other v2\n"})
        self.write_default_record(commit=on_main, digests=self.MOVED)
        shared_path = self.shared_lookup(self.MOVED)
        git(self.root, "checkout", "-q", "-b", "topic", self.base)
        self.commit("pr changes feature", {"src/feature.py": "feature v2\n"})
        git(self.root, "checkout", "-q", "main")
        git(self.root, "merge", "-q", "--no-ff", "-m", "merge", "topic")
        landed = git(self.root, "rev-parse", "HEAD")
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps({"case": "behind", "head": landed, "landing_base": on_main, "tested_base": self.base,
                                      "reason": "behind", "pull_request": 7, "tested_run": 4100,
                                      "tested_digests": self.DIGESTS, "landing_base_passed": True}), encoding="utf-8")
        outputs, printed, _ = self.select("--lookup", str(lookup), "--shared-lookup", str(shared_path))
        self.assertEqual(self.runs(outputs), self.ALL)
        self.assertIn("`platform:linux` digest moved:", printed)

    def test_a_digest_present_on_either_side_and_missing_on_the_other_counts_as_moved(self) -> None:
        # Missing from current digests (present in previous) counts as moved and reports "differ from".
        self.write_default_record(digests=self.DIGESTS)
        without_linux = {k: v for k, v in self.DIGESTS.items() if k != "platform:linux"}
        found = self.looked_up(without_linux)
        self.assertIn("differ from", found["reason"])
        head = self.commit("head", {"README.md": "unrelated\n"})
        outputs, printed, summary = self.select("--base", self.base, "--head", head, "--compare", "merge-base",
                                                "--shared-lookup", str(self.root.parent / "shared.json"))
        self.assertEqual(self.runs(outputs), ["both", "main-only", "per-entry", "pr-only"])
        self.assertIn("`platform:linux`", summary)
        self.assertIn("`platform:linux` digest moved:", printed)

        # Same for identical push against tested_digests: present in T, missing in D.
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps({"case": "identical", "head": self.base, "landing_base": self.base, "reason": "tested",
                                      "pull_request": 7, "tested_run": 4100, "tested_digests": self.DIGESTS}), encoding="utf-8")
        outputs, printed, summary = self.select("--lookup", str(lookup), "--shared-lookup", str(self.shared_lookup(without_linux)))
        self.assertEqual(self.runs(outputs), ["both", "main-only", "per-entry", "pr-only"])
        self.assertNotIn("identical", outputs)
        self.assertIn("differ from", summary)
        self.assertIn("`platform:linux`", summary)

        # Present in D, missing in T also counts as moved.
        lookup.write_text(json.dumps({"case": "identical", "head": self.base, "landing_base": self.base, "reason": "tested",
                                      "pull_request": 7, "tested_run": 4100, "tested_digests": without_linux}), encoding="utf-8")
        outputs, printed, summary = self.select("--lookup", str(lookup), "--shared-lookup", str(self.shared_lookup(self.DIGESTS)))
        self.assertEqual(self.runs(outputs), ["both", "main-only", "per-entry", "pr-only"])
        self.assertIn("differ from", summary)

    def test_the_reuse_key_holds_the_digest(self) -> None:
        self.write_default_record(digests=self.DIGESTS)
        first = self.commit("first", {"src/feature.py": "feature v2\n"})
        arguments = ("--base", self.base, "--compare", "merge-base", "--results", str(self.record), "--runner-label", "linux=ubuntu-26.04")
        outputs, _, _ = self.select("--head", first, *arguments, "--shared-lookup", str(self.shared_lookup(self.DIGESTS)))
        units = json.loads(outputs["units"])
        self.record.write_text(json.dumps({"passed": {key: 9101 for key in units}}), encoding="utf-8")
        outputs, _, _ = self.select("--head", first, *arguments, "--shared-lookup", str(self.shared_lookup(self.DIGESTS)))
        self.assertEqual((self.runs(outputs), sorted(set(json.loads(outputs["reused"]).values()))), ([], [9101]))
        self.write_default_record(digests=self.MOVED)
        outputs, _, _ = self.select("--head", first, *arguments, "--shared-lookup", str(self.shared_lookup(self.MOVED)))
        self.assertEqual((json.loads(outputs["reused"]), self.runs(outputs)), ({}, ["both", "per-entry", "pr-only", "windows"]),
                         "the same reads under other platform digests reuse no pass")


class OffDefaultBaseLookupTests(SharedLookupFixture):
    """A pull request onto a non-default base branch still uses only the default-branch record for shared digests and
    passed-unit takeover (never trusting another pull request's run)."""

    def test_pull_request_onto_integration_branch_uses_only_the_default_branch_record(self) -> None:
        self.write_default_record(commit=self.base, digests=self.DIGESTS)
        git(self.root, "checkout", "-q", "-b", "integration")
        tip = self.commit("integration commit", {"src/other.py": "integration\n"})
        git(self.root, "checkout", "-q", "-b", "next", tip)
        head = self.commit("head", {"README.md": "a change no suite reads\n"})

        found = self.looked_up(self.DIGESTS)
        self.assertEqual((found["previous"], found["run"], found["fallback"]), (self.DIGESTS, self.RUN, None))
        outputs, _, summary = self.select("--base", tip, "--head", head, "--compare", "merge-base",
                                          "--shared-lookup", str(self.root.parent / "shared.json"))
        self.assertEqual(self.runs(outputs), [])
        self.assertIn(f"the shared CI digests equal those of run {self.RUN}", summary)

        # When the default-branch record is absent, every suite runs instead of looking up another PR's run.
        self.default_record.unlink()
        found = self.looked_up(self.DIGESTS)
        self.assertEqual((found["previous"], found["fallback"]), (None, "missing-default-branch-record"))
        outputs, _, _ = self.select("--base", tip, "--head", head, "--compare", "merge-base",
                                    "--shared-lookup", str(self.root.parent / "shared.json"))
        self.assertEqual(self.runs(outputs), self.ALL)

    def test_both_sides_preserves_undetermined_when_either_side_is_undetermined(self) -> None:
        a = suite_selection.Selection((), (), undetermined="files could not be listed")
        b = suite_selection.Selection((), (), undetermined=None)
        self.assertEqual(suite_selection.both_sides(a, b).undetermined, "files could not be listed")
        self.assertEqual(suite_selection.both_sides(b, a).undetermined, "files could not be listed")


if __name__ == "__main__":
    unittest.main()
