"""The push lookup, the reuse key and the result store of select, on fixture repositories with recorded API responses."""

from __future__ import annotations

import http.server
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests import temporary_support
from tests.suite_selection_fixtures import force_add, git, write

SHARED_FOLDER = Path(__file__).resolve().parents[1] / "actions"
TOOL = SHARED_FOLDER / "suite-selection"
sys.path.insert(0, str(TOOL))

import reuse
import suite_selection

SUITES = {
    "always": [],
    "suites": [
        {"name": "pr-only", "entries": ["tests/test_pr.py"], "paths": ["tests/test_pr.py", "src/feature.py"],
         "command": ["python3", "-m", "unittest"], "setups": ["python"]},
        {"name": "both", "entries": ["tests/test_both.py"], "paths": ["tests/test_both.py", "src/feature.py", "src/moved.py"],
         "command": ["python3", "-m", "unittest"], "setups": ["python"]},
        {"name": "main-only", "entries": ["tests/test_main.py"], "paths": ["tests/test_main.py", "src/moved.py"],
         "command": ["python3", "-m", "unittest"], "setups": ["python"]},
        {"name": "per-entry", "entries": ["tests/test_one.py", "tests/test_two.py"], "paths": ["tests/**", "src/**"],
         "entry_selection": True, "entry_paths": {"tests/test_one.py": ["tests/test_one.py", "src/feature.py"],
                                                  "tests/test_two.py": ["tests/test_two.py", "src/other.py"]},
         "command": ["python3", "-m", "unittest"], "setups": ["python"], "entry_setups": {"tests/test_two.py": ["node"]}},
        {"name": "windows", "entries": ["tests/test_win.py"], "paths": ["tests/test_win.py", "src/feature.py"],
         "command": ["python", "-m", "unittest"], "setups": ["python"], "os": "windows"},
    ],
}


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


class FixtureRepository(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = temporary_support.Directory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "core.autocrlf", "false")
        for path in ("tests/test_pr.py", "tests/test_both.py", "tests/test_main.py", "tests/test_one.py", "tests/test_two.py",
                     "tests/test_win.py", "src/feature.py", "src/moved.py", "src/other.py", "README.md"):
            write(self.root, path, path + "\n")
        write(self.root, "suites.json", json.dumps(SUITES, indent=2) + "\n")
        self.base = self.commit("base")

    def commit(self, message: str, files: dict[str, str] | None = None) -> str:
        for path, text in (files or {}).items():
            write(self.root, path, text)
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "--allow-empty", "-m", message)
        return git(self.root, "rev-parse", "HEAD")

    def select(self, *arguments: str) -> tuple[dict[str, str], str, str]:
        outputs, summary = self.root.parent / "outputs.txt", self.root.parent / "summary.md"
        outputs.unlink(missing_ok=True)
        summary.unlink(missing_ok=True)
        printed = io.StringIO()
        with redirect_stdout(printed):
            code = suite_selection.main(["select", "--root", str(self.root), "--suites", str(self.root / "suites.json"),
                                         "--json-output", str(outputs), "--summary", str(summary), *arguments])
        self.assertEqual(code, 0, printed.getvalue())
        return dict(line.split("=", 1) for line in outputs.read_text(encoding="utf-8").splitlines()), printed.getvalue(), summary.read_text(encoding="utf-8")


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
                self.assertEqual({expected_fallback: 1}, json.loads(unit_decisions.read_text(encoding="utf-8"))["fallbacks"])
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


class ReuseTests(FixtureRepository):
    """Later heads of a pull request reuse passed results whose exact key did not change."""

    LABELS = ("--runner-label", "linux=ubuntu-26.04", "--runner-label", "windows=windows-2025", "--setup-version", "python=3.13")

    def setUp(self) -> None:
        super().setUp()
        self.record = self.root.parent / "record.json"

    def head(self, files: dict[str, str]) -> str:
        return self.commit("head", files)

    def select_head(self, head: str, *extra: str, labels: tuple[str, ...] = LABELS) -> dict[str, str]:
        git(self.root, "checkout", "-q", head)
        outputs, _, _ = self.select("--base", self.base, "--head", head, "--compare", "merge-base", "--results", str(self.record), *labels, *extra)
        return outputs

    def store(self, outputs: dict[str, str], passed: list[tuple[str, str]] | dict[str, list[dict]], run: int, result: str = "success",
              job: str = "select", additions: Path | None = None) -> bool:
        """Store a run: ``passed`` is a list of passed entry files of one job, or the passed list each job uploaded by
        artifact name; ``additions`` the folder with the run's downloaded ``recording-additions-*`` artifacts."""
        needs = self.root.parent / "needs.json"
        needs.write_text(json.dumps({job: {"result": result, "outputs": outputs}, "linux": {"result": "success", "outputs": {}}}), encoding="utf-8")
        directory = self.root.parent / f"passed-{run}"
        if directory.exists():
            temporary_support.remove_tree(directory)
        lists = passed if isinstance(passed, dict) else {"suite-results-shard-1": [{"suite": suite, "entry": entry} for suite, entry in passed]}
        for name, items in lists.items():
            (directory / name).mkdir(parents=True)
            (directory / name / "passed.json").write_text(json.dumps(items), encoding="utf-8")
        github_output = self.root.parent / "store-output.txt"
        github_output.unlink(missing_ok=True)
        with redirect_stdout(io.StringIO()):
            code = suite_selection.main(["store", "--needs", str(needs), "--passed", str(directory), "--run-id", str(run),
                                         *(["--selection-job", job] if job != "select" else []),
                                         *(["--additions", str(additions), "--root", str(self.root)] if additions is not None else []),
                                         "--output", str(self.record), "--github-output", str(github_output)])
        self.assertEqual(code, 0)
        return github_output.read_text(encoding="utf-8") == "stored=true\n"

    RECORDING = str(SHARED_FOLDER / "recording" / "recording.py")
    NO_READS = {"paths": [], "existence_reads": [], "directory_memberships": [], "lookup_directories": [], "module_lookups": {}}

    def job_with_additions(self, run: int, job: str, passed: list[tuple[str, str]], changes: list[dict]) -> tuple[list[dict], Path]:
        """One job of a run whose check reported ``changes``, in the shape check writes them: its additions artifact under
        the run's additions folder, and its passed list as ``keep`` leaves it."""
        artifact = self.root.parent / f"additions-{run}" / f"recording-additions-{job}"
        artifact.mkdir(parents=True, exist_ok=True)
        document = artifact / "additions.json"
        document.write_text(json.dumps({"kind": "additions", "suite_list": "suites.json", "suites": changes}), encoding="utf-8")
        listed = self.root.parent / f"passed-{run}-{job}.json"
        listed.write_text(json.dumps([{"suite": suite, "entry": entry} for suite, entry in passed]), encoding="utf-8")
        completed = subprocess.run([sys.executable, self.RECORDING, "keep", "--additions", str(document), str(listed)], capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)
        return json.loads(listed.read_text(encoding="utf-8")), artifact.parent

    def app_commit(self, *documents: Path) -> str:
        """The App's additions commit: the additions applied to the suite list, and no other file."""
        before = git(self.root, "rev-parse", "HEAD")
        completed = subprocess.run([sys.executable, self.RECORDING, "apply", "--root", str(self.root), "--suites", str(self.root / "suites.json"),
                                    *map(str, documents)], capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)
        app = self.commit("chore: apply the recorded additions")
        self.assertEqual("suites.json", git(self.root, "diff", "--name-only", before, app))
        return app

    @staticmethod
    def runs(outputs: dict[str, str]) -> list[str]:
        return sorted(name for name, result in json.loads(outputs["suites"]).items() if result == "run")

    @staticmethod
    def running(outputs: dict[str, str]) -> list[list[str]]:
        return sorted(sorted(unit["entries"]) for unit in json.loads(outputs["units"]).values())

    def make_both_read_the_suite_list(self) -> None:
        """Make `both` read the suite list, as the entry files of this repository that read tests/suites.json do."""
        suites = json.loads(json.dumps(SUITES))
        suites["suites"][1]["paths"].append("suites.json")
        git(self.root, "checkout", "-q", self.base)
        self.base = self.commit("a suite that reads the suite list", {"suites.json": json.dumps(suites, indent=2) + "\n"})

    def first_run_with_additions(self, run: int) -> tuple[str, Path]:
        """A first head whose run passes every unit, with additions for the entry file `tests/test_one.py` of the suite
        selected per entry file and for the pooled suite `pr-only`; it returns the head and the additions folder."""
        first = self.head({"src/feature.py": "feature v2\n"})
        outputs = self.select_head(first)
        reads = self.NO_READS | {"paths": ["README.md"]}
        kept, additions = self.job_with_additions(run, "shard-1", self.ALL, [
            {"suite": "per-entry", "create": False, "recorded_entries": ["tests/test_one.py"], "entries": {"tests/test_one.py": reads}},
            {"suite": "pr-only", "create": False, "recorded_entries": ["tests/test_pr.py"], "reads": reads}])
        self.assertEqual([("per-entry", "tests/test_one.py"), ("pr-only", "tests/test_pr.py")],
                         sorted((item["suite"], item["entry"]) for item in kept if item.get("additions")), "keep marks the entry files with additions")
        self.assertTrue(self.store(outputs, {"suite-results-shard-1": kept}, run, additions=additions))
        return first, additions

    ALL = [("pr-only", "tests/test_pr.py"), ("both", "tests/test_both.py"), ("per-entry", "tests/test_one.py"), ("windows", "tests/test_win.py")]

    def first_head_passes(self) -> str:
        first = self.head({"src/feature.py": "feature v2\n"})
        outputs = self.select_head(first)
        self.assertEqual(json.loads(outputs["reused"]), {})
        self.assertEqual(sorted(unit["suite"] for unit in json.loads(outputs["units"]).values()), ["both", "per-entry", "pr-only", "windows"])
        self.assertTrue(self.store(outputs, self.ALL, 9001))
        return first

    def test_a_later_head_reuses_the_passed_results_whose_key_matches_exactly(self) -> None:
        self.first_head_passes()
        later = self.head({"README.md": "a change no suite reads\n"})
        outputs = self.select_head(later)
        self.assertEqual(json.loads(outputs["suites"]), {name: "skip" for name in ("pr-only", "both", "main-only", "per-entry", "windows")})
        self.assertEqual(sorted(json.loads(outputs["reused"]).values()), [9001] * 4)
        self.assertEqual((outputs["shards"], outputs["windows"], json.loads(outputs["units"])), ("[]", "", {}))
        _, printed, _ = self.select("--base", self.base, "--head", later, "--compare", "merge-base", "--results", str(self.record), *self.LABELS)
        self.assertIn("pr-only: skip (reuses the passed result of run 9001)", printed)

    def test_a_change_to_any_single_part_of_the_key_makes_the_unit_run(self) -> None:
        first = self.first_head_passes()
        runs = lambda outputs: sorted(name for name, result in json.loads(outputs["suites"]).items() if result == "run")
        git(self.root, "checkout", "-q", first)
        self.assertEqual(runs(self.select_head(self.head({"tests/test_pr.py": "a changed read\n"}))), ["pr-only"], "the content of a read")
        git(self.root, "checkout", "-q", first)
        changed_command = json.loads(json.dumps(SUITES))
        changed_command["suites"][1]["command"] = ["python3", "-m", "unittest", "-v"]
        self.assertEqual(runs(self.select_head(self.head({"suites.json": json.dumps(changed_command)}))), ["both"], "the command")
        git(self.root, "checkout", "-q", first)
        self.assertEqual(runs(self.select_head(first, labels=self.LABELS[:4] + ("--setup-version", "python=3.14"))),
                         ["both", "per-entry", "pr-only", "windows"], "the version a setup pins")
        self.assertEqual(runs(self.select_head(first, labels=self.LABELS[:3] + ("windows=windows-2026",) + self.LABELS[4:])),
                         ["windows"], "the runner label")

    def test_a_moved_pin_of_a_pinned_tool_makes_only_the_units_that_state_it_run(self) -> None:
        pins = self.root.parent / "pins.json"

        def select_pinned(head: str, version: str) -> dict[str, str]:
            pins.write_text(json.dumps({"node": {"version": version}}), encoding="utf-8")
            return self.select_head(head, "--pinned-tool", "node", "--pins", str(pins))

        first = self.head({"src/feature.py": "feature v2\n", "src/other.py": "other v2\n"})
        outputs = select_pinned(first, "24.9.0")
        self.assertTrue(self.store(outputs, self.ALL + [("per-entry", "tests/test_two.py")], 9001))
        later = self.head({"README.md": "a change no suite reads\n"})
        same = select_pinned(later, "24.9.0")
        self.assertEqual((same["shards"], same["windows"]), ("[]", ""), "the same pin reuses every passed result")
        moved = select_pinned(later, "24.10.0")
        self.assertEqual([unit["entries"] for unit in json.loads(moved["units"]).values()], [["tests/test_two.py"]],
                         "only the entry file that states node runs again")
        self.assertEqual(sorted(json.loads(moved["reused"]).values()), [9001] * 4)
        self.assertEqual(json.loads(moved["shards"])[0]["tools"], {"node": "24.10.0"})

    def test_an_entry_file_of_a_suite_selected_per_entry_file_is_a_unit_of_its_own(self) -> None:
        self.first_head_passes()
        later = self.head({"src/other.py": "other v2\n"})
        outputs = self.select_head(later)
        self.assertEqual(json.loads(outputs["entries"])["per-entry"], ["tests/test_two.py"])
        self.assertEqual([unit["entries"] for unit in json.loads(outputs["units"]).values()], [["tests/test_two.py"]])

    def test_a_changed_step_of_the_calling_workflow_counts_as_a_read_of_its_suite(self) -> None:
        workflow = ("name: Validate\non: [pull_request]\njobs:\n  select:\n    runs-on: ubuntu-latest\n    steps:\n"
                    "      - run: python suites.py\n        id: select\n"
                    "      - run: python -m unittest tests/test_main.py\n        if: steps.select.outputs.main-only == 'run'\n")
        write(self.root, ".github/workflows/validate.yml", workflow)
        self.base = self.commit("the workflow")
        arguments = ("--workflow", ".github/workflows/validate.yml")
        first = self.head({".github/workflows/validate.yml": workflow.replace("unittest tests", "unittest -v tests")})
        outputs = self.select_head(first, *arguments)
        self.assertEqual(json.loads(outputs["suites"])["main-only"], "run")
        self.assertTrue(self.store(outputs, [("main-only", "tests/test_main.py")], 9002))
        self.assertEqual(json.loads(self.select_head(self.head({"README.md": "unread\n"}), *arguments)["suites"])["main-only"], "skip")
        second = self.head({".github/workflows/validate.yml": workflow.replace("unittest tests", "unittest -v -b tests")})
        self.assertEqual(json.loads(self.select_head(second, *arguments)["suites"])["main-only"], "run")

    def test_the_key_holds_the_tracked_gitignore_files_and_the_tracked_paths_that_they_match(self) -> None:
        runs = lambda outputs: sorted(name for name, result in json.loads(outputs["suites"]).items() if result == "run")
        every = ["both", "main-only", "per-entry", "pr-only", "windows"]
        for label, first, later in (
                ("a tracked .gitignore file", {".gitignore": "*.tmp\n"}, {".gitignore": "*.tmp\n*.out\n"}),
                ("a tracked path that the rules match", {".gitignore": "*.tmp\n", "notes.tmp": "first\n"}, {"notes.tmp": "second\n"})):
            with self.subTest(part=label):
                git(self.root, "checkout", "-q", self.base)
                self.record.unlink(missing_ok=True)
                force_add(self.root, first)
                outputs = self.select_head(self.head({}))
                self.assertEqual(every, runs(outputs))
                self.assertTrue(self.store(outputs, self.ALL + [("main-only", "tests/test_main.py")], 9010))
                outputs = self.select_head(self.head(later))
                self.assertEqual(every, runs(outputs), "the change still runs every unit")
                self.assertEqual({}, json.loads(outputs["reused"]), "and reuses no result")

    def test_the_key_holds_the_names_that_carry_a_module_lookup_in_each_lookup_directory(self) -> None:
        suites = json.loads(json.dumps(SUITES))
        suites["suites"][0]["lookup_directories"] = ["src"]
        suites["module_lookups"] = {"src": ["helper"]}
        git(self.root, "checkout", "-q", self.base)
        self.base = self.commit("module lookups", {"suites.json": json.dumps(suites)})
        self.first_head_passes()
        first = git(self.root, "rev-parse", "HEAD")
        reused = lambda outputs: sorted(unit for unit in json.loads(outputs["suites"]) if json.loads(outputs["suites"])[unit] == "skip")
        self.assertIn("pr-only", reused(self.select_head(self.head({"src/notes.txt": "no module name\n"}))))
        git(self.root, "checkout", "-q", first)
        outputs = self.select_head(self.head({"src/helper.py": "a module that the suite looks up\n"}))
        self.assertEqual("run", json.loads(outputs["suites"])["pr-only"], "a new module under a lookup name reuses no result")

    def test_a_re_run_reuses_no_earlier_result(self) -> None:
        self.first_head_passes()
        later = self.head({"README.md": "unread\n"})
        outputs = self.select_head(later, "--run-attempt", "2")
        self.assertEqual(json.loads(outputs["reused"]), {})
        self.assertEqual(sorted(name for name, result in json.loads(outputs["suites"]).items() if result == "run"),
                         ["both", "per-entry", "pr-only", "windows"])

    def test_the_store_reads_the_selection_from_the_job_that_the_caller_names(self) -> None:
        outputs = self.select_head(self.head({"src/feature.py": "feature v2\n"}))
        self.assertTrue(self.store(outputs, self.ALL, 9020, job="suite-selection"), "the pipeline's selection job carries the part's name")
        self.assertEqual(len(json.loads(self.record.read_text(encoding="utf-8"))["passed"]), 4)
        self.record.unlink()
        needs = self.root.parent / "needs.json"
        needs.write_text(json.dumps({"select": {"result": "success", "outputs": outputs}}), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            suite_selection.main(["store", "--needs", str(needs), "--selection-job", "suite-selection", "--passed", str(self.root.parent / "passed-9020"),
                                  "--run-id", "9021", "--output", str(self.record)])
        self.assertFalse(self.record.exists(), "a needs context without the named job stores nothing")

    def test_only_passed_units_are_stored_and_a_failed_selection_stores_nothing(self) -> None:
        first = self.head({"src/feature.py": "feature v2\n"})
        outputs = self.select_head(first)
        self.assertTrue(self.store(outputs, [("pr-only", "tests/test_pr.py"), ("per-entry", "tests/test_one.py")], 9003))
        stored = json.loads(self.record.read_text(encoding="utf-8"))["passed"]
        units = json.loads(outputs["units"])
        self.assertEqual(sorted(units[key]["suite"] for key in stored), ["per-entry", "pr-only"])
        later = self.select_head(self.head({"README.md": "unread\n"}))
        self.assertEqual(sorted(name for name, result in json.loads(later["suites"]).items() if result == "run"), ["both", "windows"])
        self.assertTrue(self.store(later, [("both", "tests/test_both.py")], 9004))
        carried = json.loads(self.record.read_text(encoding="utf-8"))["passed"]
        self.assertEqual(sorted(carried.values()), [9003, 9003, 9004], "the reused results stay with the run they passed in")
        self.record.unlink()
        self.assertFalse(self.store(outputs, self.ALL, 9005, result="failure"))
        self.assertFalse(self.record.exists())

    def test_a_later_head_that_does_not_apply_the_additions_reuses_the_unmarked_passes_and_runs_the_entry_files_with_additions(self) -> None:
        self.first_run_with_additions(9007)
        later = self.select_head(self.head({"README.md": "a change no suite reads\n"}))
        self.assertEqual(["per-entry", "pr-only"], self.runs(later))
        self.assertEqual([9007, 9007], sorted(json.loads(later["reused"]).values()))
        self.assertEqual([["tests/test_one.py"], ["tests/test_pr.py"]], self.running(later), "a pass with additions counts only under the applied lists")

    def test_a_later_head_that_only_applies_the_additions_reuses_the_passes_of_the_units_with_additions(self) -> None:
        self.make_both_read_the_suite_list()
        _, additions = self.first_run_with_additions(9008)
        app = self.app_commit(*additions.rglob("additions.json"))
        later = self.select_head(app)
        self.assertEqual(["both"], self.runs(later), "only the reader of the suite list runs")
        self.assertEqual([["tests/test_both.py"]], self.running(later))
        self.assertEqual([9008] * 3, sorted(json.loads(later["reused"]).values()))
        _, printed, _ = self.select("--base", self.base, "--head", app, "--compare", "merge-base", "--results", str(self.record), *self.LABELS)
        for line in ("pr-only: skip (reuses the passed result of run 9008)", "per-entry: skip (reuses the passed result of run 9008)",
                     "windows: skip (reuses the passed result of run 9008)"):
            self.assertIn(line, printed)

    def test_a_pooled_suite_split_over_jobs_with_additions_in_each_reuses_its_pass_on_the_head_that_applies_them(self) -> None:
        pooled = json.loads(json.dumps(SUITES))
        pooled["suites"][0]["entries"] = ["tests/test_pr.py", "tests/test_main.py"]
        git(self.root, "checkout", "-q", self.base)
        self.base = self.commit("a pooled suite of two entry files", {"suites.json": json.dumps(pooled, indent=2) + "\n"})

        def run(number: int, shard_2_kept: bool) -> tuple[str, list[Path]]:
            git(self.root, "checkout", "-q", self.base)
            self.record.unlink(missing_ok=True)
            first = self.head({"src/feature.py": f"feature v{number}\n"})
            outputs = self.select_head(first)
            one, additions = self.job_with_additions(number, "shard-1", [("pr-only", "tests/test_pr.py")], [
                {"suite": "pr-only", "create": False, "recorded_entries": ["tests/test_pr.py"], "reads": self.NO_READS | {"paths": ["README.md"]}}])
            two, _ = self.job_with_additions(number, "shard-2", [("pr-only", "tests/test_main.py")], [
                {"suite": "pr-only", "create": False, "recorded_entries": ["tests/test_main.py"], "reads": self.NO_READS | {"paths": ["src/other.py"]}}])
            lists = {"suite-results-shard-1": one} | ({"suite-results-shard-2": two} if shard_2_kept else {})
            self.assertTrue(self.store(outputs, lists, number, additions=additions))
            return first, sorted(additions.rglob("additions.json"))

        _, documents = run(9030, shard_2_kept=True)
        later = self.select_head(self.app_commit(*documents))
        self.assertNotIn("pr-only", self.runs(later), "the pooled suite reuses its pass under the additions of both jobs")
        self.assertIn(9030, json.loads(later["reused"]).values())
        _, documents = run(9031, shard_2_kept=False)
        later = self.select_head(self.app_commit(*documents))
        self.assertIn(["tests/test_main.py", "tests/test_pr.py"], self.running(later), "a job that uploaded no passed list stores no pass of the suite")

    def test_a_head_that_applies_the_additions_and_changes_a_file_a_unit_read_runs_that_unit(self) -> None:
        _, additions = self.first_run_with_additions(9009)
        self.app_commit(*additions.rglob("additions.json"))
        later = self.select_head(self.head({"README.md": "a changed read of the entry files with additions\n"}))
        self.assertEqual(["per-entry", "pr-only"], self.runs(later))
        self.assertEqual([["tests/test_one.py"], ["tests/test_pr.py"]], self.running(later))

    def test_a_head_whose_stored_list_differs_from_the_list_with_the_additions_applied_runs_the_unit(self) -> None:
        first, _ = self.first_run_with_additions(9011)
        reads = self.NO_READS | {"paths": ["README.md"]}
        for label, changes, expected in (
                ("only part of the additions", [{"suite": "per-entry", "create": False, "recorded_entries": ["tests/test_one.py"],
                                                 "entries": {"tests/test_one.py": reads}}], [["tests/test_pr.py"]]),
                ("a read beyond the additions", [{"suite": "per-entry", "create": False, "recorded_entries": ["tests/test_one.py"],
                                                  "entries": {"tests/test_one.py": self.NO_READS | {"paths": ["README.md", "src/moved.py"]}}},
                                                 {"suite": "pr-only", "create": False, "recorded_entries": ["tests/test_pr.py"], "reads": reads}],
                 [["tests/test_one.py"]])):
            with self.subTest(head=label):
                other = self.root.parent / f"other-{len(changes)}.json"
                other.write_text(json.dumps({"kind": "additions", "suite_list": "suites.json", "suites": changes}), encoding="utf-8")
                later = self.select_head(self.app_commit(other))
                self.assertEqual(expected, self.running(later))
                git(self.root, "checkout", "-q", first)

    def test_a_unit_whose_stored_patterns_match_the_suite_list_runs_on_the_head_that_applies_the_additions(self) -> None:
        self.make_both_read_the_suite_list()
        _, additions = self.first_run_with_additions(9012)
        later = self.select_head(self.app_commit(*additions.rglob("additions.json")))
        self.assertEqual(["both"], self.runs(later))
        self.assertEqual(["both"], [unit["suite"] for unit in json.loads(later["units"]).values()], "its pass under the old list is not reused")

    def test_a_job_with_a_recording_error_or_a_failed_keep_stores_none_of_its_passes(self) -> None:
        first = self.head({"src/feature.py": "feature v2\n"})
        outputs = self.select_head(first)
        reads = self.NO_READS | {"paths": ["README.md"]}
        _, additions = self.job_with_additions(9013, "shard-1", self.ALL, [
            {"suite": "per-entry", "create": False, "recorded_entries": ["tests/test_one.py"], "entries": {"tests/test_one.py": reads}}])
        # The job uploaded no passed list: its check failed without additions, or its keep failed.
        self.assertTrue(self.store(outputs, {}, 9013, additions=additions))
        self.assertEqual({}, json.loads(self.record.read_text(encoding="utf-8"))["passed"])
        later = self.select_head(self.app_commit(*additions.rglob("additions.json")))
        self.assertEqual({}, json.loads(later["reused"]))
        self.assertEqual(["both", "per-entry", "pr-only", "windows"], self.runs(later))

    def test_a_marked_pass_whose_additions_artifact_is_missing_is_not_stored_under_the_applied_lists(self) -> None:
        first = self.head({"src/feature.py": "feature v2\n"})
        outputs = self.select_head(first)
        reads = self.NO_READS | {"paths": ["README.md"]}
        # Shard 1 marked tests/test_one.py, but the upload of its additions artifact failed: only shard 2's document is there.
        kept, lost = self.job_with_additions(9016, "shard-1", self.ALL, [
            {"suite": "per-entry", "create": False, "recorded_entries": ["tests/test_one.py"], "entries": {"tests/test_one.py": reads}}])
        temporary_support.remove_tree(lost / "recording-additions-shard-1")
        _, additions = self.job_with_additions(9016, "shard-2", [("main-only", "tests/test_main.py")], [
            {"suite": "pr-only", "create": False, "recorded_entries": ["tests/test_pr.py"], "reads": reads}])
        self.assertTrue(self.store(outputs, {"suite-results-shard-1": kept}, 9016, additions=additions))
        units = json.loads(outputs["units"])
        stored = json.loads(self.record.read_text(encoding="utf-8"))["passed"]
        self.assertEqual(4, len(stored), "the unmarked passes, and pr-only also under the applied lists")
        self.assertNotIn(["tests/test_one.py"], [units[key]["entries"] for key in stored if key in units])
        self.assertEqual(9016, stored.get(next(key for key, unit in units.items() if unit["suite"] == "pr-only")))

    def test_a_pass_with_additions_is_never_stored_under_the_key_of_the_lists_without_them(self) -> None:
        first = self.head({"src/feature.py": "feature v2\n"})
        outputs = self.select_head(first)
        kept, _ = self.job_with_additions(9014, "shard-1", self.ALL, [
            {"suite": "pr-only", "create": False, "recorded_entries": ["tests/test_pr.py"], "reads": self.NO_READS | {"paths": ["README.md"]}}])
        self.assertTrue(self.store(outputs, {"suite-results-shard-1": kept}, 9014))
        units = json.loads(outputs["units"])
        self.assertEqual(["both", "per-entry", "windows"], sorted(units[key]["suite"] for key in json.loads(self.record.read_text(encoding="utf-8"))["passed"]),
                         "without the additions folder the marked pass is not stored")

    def test_a_re_run_of_the_head_that_applies_the_additions_reuses_nothing(self) -> None:
        _, additions = self.first_run_with_additions(9015)
        app = self.app_commit(*additions.rglob("additions.json"))
        later = self.select_head(app, "--run-attempt", "2")
        self.assertEqual({}, json.loads(later["reused"]))
        self.assertEqual(["both", "per-entry", "pr-only", "windows"], self.runs(later))

    def test_a_suite_split_over_jobs_is_stored_only_when_every_entry_file_passed(self) -> None:
        both = json.loads(json.dumps(SUITES))
        both["suites"][0]["entries"] = ["tests/test_pr.py", "tests/test_main.py"]
        first = self.head({"suites.json": json.dumps(both), "src/feature.py": "feature v2\n"})
        outputs = self.select_head(first)
        self.assertTrue(self.store(outputs, [("pr-only", "tests/test_pr.py")], 9006))
        units = json.loads(outputs["units"])
        self.assertNotIn("pr-only", [units[key]["suite"] for key in json.loads(self.record.read_text(encoding="utf-8"))["passed"]])

    def test_a_pass_of_a_new_suite_is_stored_under_its_applied_key_and_reused_after_additions(self) -> None:
        with_new = json.loads(json.dumps(SUITES))
        with_new["suites"].append({
            "name": "brand-new",
            "entries": ["tests/test_new.py"],
            "command": ["python3", "-m", "unittest"],
            "setups": ["python"],
        })
        first = self.head({"suites.json": json.dumps(with_new, indent=2) + "\n", "tests/test_new.py": "new test\n"})
        outputs = self.select_head(first)
        self.assertIn("brand-new", self.runs(outputs))
        self.assertIn("brand-new", [unit["suite"] for unit in json.loads(outputs["units"]).values()],
                      "a new suite without paths is emitted in units so store --additions can key it")
        # Without additions, a pass of an unlisted suite is never stored under its sentinel key.
        self.assertTrue(self.store(outputs, [("brand-new", "tests/test_new.py")], 9040))
        self.assertEqual({}, json.loads(self.record.read_text(encoding="utf-8"))["passed"])
        self.record.unlink()

        reads = self.NO_READS | {"paths": ["tests/test_new.py", "src/feature.py"]}
        kept, additions = self.job_with_additions(9041, "shard-1", [("brand-new", "tests/test_new.py")], [
            {"suite": "brand-new", "create": True, "recorded_entries": ["tests/test_new.py"], "reads": reads}])
        self.assertTrue(self.store(outputs, {"suite-results-shard-1": kept}, 9041, additions=additions))

        # Before additions are applied, a later head still runs the new suite.
        unapplied = self.select_head(self.head({"README.md": "unread\n"}))
        self.assertIn("brand-new", self.runs(unapplied))
        git(self.root, "checkout", "-q", first)

        # Once additions are applied, the next run takes over the pass from run 9041.
        app = self.app_commit(*additions.rglob("additions.json"))
        later = self.select_head(app)
        self.assertNotIn("brand-new", self.runs(later))
        self.assertIn(9041, json.loads(later["reused"]).values())

        # Changing one of its recorded reads makes it run again.
        changed = self.select_head(self.head({"src/feature.py": "feature changed\n"}))
        self.assertIn("brand-new", self.runs(changed))

    def test_a_pass_of_a_new_entry_file_is_stored_under_its_applied_key_and_reused_after_additions(self) -> None:
        with_entry = json.loads(json.dumps(SUITES))
        with_entry["suites"][3]["entries"].append("tests/test_three.py")
        first = self.head({"suites.json": json.dumps(with_entry, indent=2) + "\n", "tests/test_three.py": "third test\n"})
        outputs = self.select_head(first)
        self.assertEqual(["tests/test_three.py"], json.loads(outputs["entries"])["per-entry"])
        self.assertIn(["tests/test_three.py"], self.running(outputs),
                      "a new entry file without a stored list is emitted in units so store --additions can key it")

        reads = self.NO_READS | {"paths": ["tests/test_three.py", "src/other.py"]}
        kept, additions = self.job_with_additions(9042, "shard-1", [("per-entry", "tests/test_three.py")], [
            {"suite": "per-entry", "create": False, "recorded_entries": ["tests/test_three.py"],
             "entries": {"tests/test_three.py": reads}}])
        self.assertTrue(self.store(outputs, {"suite-results-shard-1": kept}, 9042, additions=additions))

        # Before additions are applied, a later head still runs the new entry file.
        unapplied = self.select_head(self.head({"README.md": "unread\n"}))
        self.assertEqual(["tests/test_three.py"], json.loads(unapplied["entries"])["per-entry"])
        git(self.root, "checkout", "-q", first)

        # Once additions are applied, the next run takes over the pass from run 9042.
        app = self.app_commit(*additions.rglob("additions.json"))
        later = self.select_head(app)
        self.assertNotIn("per-entry", self.runs(later))
        self.assertIn(9042, json.loads(later["reused"]).values())

        # Changing one of its recorded reads makes it run again.
        changed = self.select_head(self.head({"src/other.py": "other changed\n"}))
        self.assertIn("tests/test_three.py", json.loads(changed["entries"])["per-entry"])

    def test_a_unit_reused_in_an_additions_run_is_rekeyed_under_the_applied_lists(self) -> None:
        suites = json.loads(json.dumps(SUITES))
        suites["suites"][0]["lookup_directories"] = ["src"]
        suites["module_lookups"] = {"src": ["alpha"]}
        git(self.root, "checkout", "-q", self.base)
        self.base = self.commit("legacy module lookup", {"suites.json": json.dumps(suites, indent=2) + "\n"})

        # Run 9050 passes pr-only (and the other suites touched by src/feature.py).
        first = self.head({"src/feature.py": "feature v2\n"})
        first_outputs = self.select_head(first)
        self.assertTrue(self.store(first_outputs, self.ALL, 9050))

        # Run 9051 touches src/other.py: pr-only is reused from 9050, while test_two.py runs and reports
        # a new module lookup in src/, which changes pr-only's key under the applied suite list.
        second = self.head({"src/other.py": "other v2\n"})
        second_outputs = self.select_head(second)
        self.assertNotIn("pr-only", self.runs(second_outputs))
        self.assertIn(9050, json.loads(second_outputs["reused"]).values())
        reads = self.NO_READS | {"lookup_directories": ["src"], "module_lookups": {"src": ["beta"]}}
        kept, additions = self.job_with_additions(9051, "shard-1", [("per-entry", "tests/test_two.py")], [
            {"suite": "per-entry", "create": False, "recorded_entries": ["tests/test_two.py"],
             "entries": {"tests/test_two.py": reads}}])
        self.assertTrue(self.store(second_outputs, {"suite-results-shard-1": kept}, 9051, additions=additions))

        # After applying additions, pr-only must still be reused from run 9050 (re-keyed under applied lists).
        app = self.app_commit(*additions.rglob("additions.json"))
        later = self.select_head(app)
        self.assertEqual([], self.runs(later), "both the running unit with additions and the reused unit take over their passes")
        self.assertEqual([9050, 9050, 9050, 9050, 9051], sorted(json.loads(later["reused"]).values()))

    def test_adding_a_module_lookup_name_leaves_the_keys_of_units_that_did_not_look_it_up_unchanged(self) -> None:
        suites = json.loads(json.dumps(SUITES))
        suites["suites"][0]["lookup_directories"] = ["src"]
        suites["suites"][0]["module_lookups"] = {"src": ["alpha"]}
        suites["suites"][3]["lookup_directories"] = ["src"]
        suites["suites"][3]["entry_lookup_directories"] = {"tests/test_one.py": ["src"], "tests/test_two.py": ["src"]}
        suites["suites"][3]["module_lookups"] = {"src": ["alpha"]}
        suites["suites"][3]["entry_module_lookups"] = {"tests/test_one.py": {"src": ["alpha"]}, "tests/test_two.py": {}}
        suites["module_lookups"] = {"src": ["alpha"]}
        git(self.root, "checkout", "-q", self.base)
        self.base = self.commit("per-unit module lookups", {"suites.json": json.dumps(suites, indent=2) + "\n"})

        first = self.head({"src/feature.py": "feature v2\n", "src/other.py": "other v2\n"})
        first_outputs = self.select_head(first)
        self.assertTrue(self.store(first_outputs, self.ALL + [("per-entry", "tests/test_two.py")], 9060))

        # Add "beta" to top-level module_lookups["src"] and only to tests/test_two.py's entry_module_lookups.
        updated = json.loads(json.dumps(suites))
        updated["module_lookups"]["src"] = ["alpha", "beta"]
        updated["suites"][3]["module_lookups"]["src"] = ["alpha", "beta"]
        updated["suites"][3]["entry_module_lookups"]["tests/test_two.py"] = {"src": ["beta"]}
        later = self.head({"suites.json": json.dumps(updated, indent=2) + "\n"})
        later_outputs = self.select_head(later)
        self.assertEqual(["per-entry"], self.runs(later_outputs), "units that did not look up beta keep their reuse keys")
        self.assertEqual(["tests/test_two.py"], json.loads(later_outputs["entries"])["per-entry"])
        self.assertEqual([9060] * 4, sorted(json.loads(later_outputs["reused"]).values()))

        # Adding src/beta.py affects only tests/test_two.py, not pr-only or tests/test_one.py.
        with_beta = self.head({"src/beta.py": "beta module\n"})
        beta_outputs = self.select_head(with_beta)
        self.assertEqual(["per-entry"], self.runs(beta_outputs))
        self.assertEqual(["tests/test_two.py"], json.loads(beta_outputs["entries"])["per-entry"])


class DefaultBranchRecordTests(FixtureRepository):
    """Passed default-branch runs store a structured record that pull requests restore for exact-key takeover."""

    LABELS = ("--runner-label", "linux=ubuntu-26.04", "--runner-label", "windows=windows-2025", "--setup-version", "python=3.13")
    DIGESTS = {"language:python": "3" * 64, "platform:linux": "a" * 64, "platform:windows": "1" * 64, "tool:gitleaks": "2" * 64}
    ALL_PASSED = [
        ("pr-only", "tests/test_pr.py"),
        ("both", "tests/test_both.py"),
        ("main-only", "tests/test_main.py"),
        ("per-entry", "tests/test_one.py"),
        ("per-entry", "tests/test_two.py"),
        ("windows", "tests/test_win.py"),
    ]

    def setUp(self) -> None:
        super().setUp()
        self.default_record = self.root.parent / "default-record.json"
        self.pr_record = self.root.parent / "pr-record.json"

    def write_shared_lookup(self, digests: dict[str, str], previous: dict[str, str] | None = None, run: int | None = None) -> Path:
        path = self.root.parent / "shared-lookup.json"
        payload = {"digests": digests, "previous": previous, "reason": "fixture", "run": run}
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def store_default(self, outputs: dict[str, str], passed: list[tuple[str, str]], run: int, commit: str,
                      digests: dict[str, str] | None = None, *, extra_jobs: dict[str, dict] | None = None) -> bool:
        needs = self.root.parent / "default-needs.json"
        jobs = {"plan": {"result": "success", "outputs": outputs}, "suite-selection-jobs": {"result": "success", "outputs": {}}}
        if extra_jobs:
            jobs.update(extra_jobs)
        needs.write_text(json.dumps(jobs), encoding="utf-8")
        directory = self.root.parent / f"default-passed-{run}"
        if directory.exists():
            temporary_support.remove_tree(directory)
        (directory / "suite-results-shard-1").mkdir(parents=True)
        (directory / "suite-results-shard-1" / "passed.json").write_text(
            json.dumps([{"suite": s, "entry": e} for s, e in passed]), encoding="utf-8")
        tree = git(self.root, "rev-parse", f"{commit}^{{tree}}")
        github_output = self.root.parent / "default-store-output.txt"
        github_output.unlink(missing_ok=True)
        digests_arg = json.dumps(digests, sort_keys=True, separators=(",", ":")) if digests is not None else ""
        with redirect_stdout(io.StringIO()):
            code = suite_selection.main([
                "store", "--default-branch", "--needs", str(needs), "--selection-job", "plan",
                "--passed", str(directory), "--run-id", str(run), "--commit", commit, "--tree", tree,
                "--digests", digests_arg, "--output", str(self.default_record), "--github-output", str(github_output),
            ])
        self.assertEqual(code, 0)
        return github_output.read_text(encoding="utf-8") == "stored=true\n"

    def test_a_passed_default_branch_run_stores_passed_keys_tested_tree_and_digests(self) -> None:
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps({"case": "own", "head": self.base, "landing_base": None, "reason": "first push"}), encoding="utf-8")
        shared = self.write_shared_lookup(self.DIGESTS, previous=None)
        outputs, _, _ = self.select("--lookup", str(lookup), "--shared-lookup", str(shared),
                                    "--default-record", str(self.default_record), *self.LABELS)
        self.assertTrue(self.store_default(outputs, self.ALL_PASSED, 7001, self.base, self.DIGESTS))
        stored = json.loads(self.default_record.read_text(encoding="utf-8"))
        self.assertEqual(1, stored["version"])
        self.assertEqual(7001, stored["run_id"])
        self.assertEqual(self.base, stored["commit"])
        self.assertEqual(git(self.root, "rev-parse", f"{self.base}^{{tree}}"), stored["tree"])
        self.assertEqual(self.DIGESTS, stored["digests"])
        self.assertEqual(6, len(stored["passed"]))
        self.assertEqual({7001}, set(stored["passed"].values()))

    def test_a_cancelled_or_failed_run_stores_nothing(self) -> None:
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps({"case": "own", "head": self.base, "landing_base": None, "reason": "first push"}), encoding="utf-8")
        shared = self.write_shared_lookup(self.DIGESTS, previous=None)
        outputs, _, _ = self.select("--lookup", str(lookup), "--shared-lookup", str(shared),
                                    "--default-record", str(self.default_record), *self.LABELS)
        for bad_result in ("cancelled", "failure", "timed_out"):
            with self.subTest(default_branch_job_result=bad_result):
                self.default_record.unlink(missing_ok=True)
                self.assertFalse(self.store_default(outputs, self.ALL_PASSED, 7002, self.base, self.DIGESTS,
                                                    extra_jobs={"repository-hygiene": {"result": bad_result, "outputs": {}}}))
                self.assertFalse(self.default_record.exists())
        # A cancelled pull-request run also stores nothing.
        needs = self.root.parent / "cancelled-pr-needs.json"
        needs.write_text(json.dumps({
            "plan": {"result": "success", "outputs": outputs},
            "suite-selection-jobs": {"result": "cancelled", "outputs": {}},
        }), encoding="utf-8")
        directory = self.root.parent / "cancelled-pr-passed"
        (directory / "suite-results-shard-1").mkdir(parents=True, exist_ok=True)
        (directory / "suite-results-shard-1" / "passed.json").write_text(
            json.dumps([{"suite": "pr-only", "entry": "tests/test_pr.py"}]), encoding="utf-8")
        self.pr_record.unlink(missing_ok=True)
        github_output = self.root.parent / "cancelled-pr-store-output.txt"
        with redirect_stdout(io.StringIO()):
            code = suite_selection.main([
                "store", "--needs", str(needs), "--selection-job", "plan", "--passed", str(directory),
                "--run-id", "7003", "--output", str(self.pr_record), "--github-output", str(github_output),
            ])
        self.assertEqual(0, code)
        self.assertEqual("stored=false\n", github_output.read_text(encoding="utf-8"))
        self.assertFalse(self.pr_record.exists())

    def test_a_pull_request_takes_over_from_the_default_branch_record_only_when_inputs_are_identical(self) -> None:
        # Default branch passes at base under DIGESTS.
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps({"case": "own", "head": self.base, "landing_base": None, "reason": "first push"}), encoding="utf-8")
        shared_first = self.write_shared_lookup(self.DIGESTS, previous=None)
        outputs, _, _ = self.select("--lookup", str(lookup), "--shared-lookup", str(shared_first),
                                    "--default-record", str(self.default_record), *self.LABELS)
        self.assertTrue(self.store_default(outputs, self.ALL_PASSED, 7010, self.base, self.DIGESTS))

        # A PR edits src/feature.py, then reverts it in a second commit on the branch? Even simpler: PR changes
        # an unread file and forces selection via a shared digest move on a different platform, OR PR changes
        # src/feature.py (different inputs -> runs) while a suite whose inputs match main's record is taken over.
        # Here we force every suite to be considered (via previous=None in shared-lookup, simulating a PR that
        # selected suites whose inputs match main's record) and change src/feature.py on the PR:
        # main-only and per-entry:test_two.py have identical inputs to main (taken over from run 7010),
        # while pr-only, both, per-entry:test_one.py, and windows changed src/feature.py (must run!).
        pr_head = self.commit("pr change", {"src/feature.py": "feature v2\n"})
        shared_missing = self.write_shared_lookup(self.DIGESTS, previous=None)
        pr_outputs, printed, _ = self.select(
            "--base", self.base, "--head", pr_head, "--compare", "merge-base",
            "--shared-lookup", str(shared_missing), "--results", str(self.pr_record),
            "--default-record", str(self.default_record), *self.LABELS,
        )
        self.assertEqual({"pr-only": "run", "both": "run", "main-only": "skip", "per-entry": "run", "windows": "run"},
                         json.loads(pr_outputs["suites"]), printed)
        self.assertEqual(["tests/test_one.py"], json.loads(pr_outputs["entries"])["per-entry"])
        self.assertEqual({7010}, set(json.loads(pr_outputs["reused"]).values()))
        self.assertEqual(2, len(json.loads(pr_outputs["reused"])), "only main-only and per-entry:test_two.py match main's record")
        self.assertIn("main-only: skip (reuses the passed result of run 7010)", printed)

        # Changing the digest or runner label invalidates takeover even for main-only.
        moved = dict(self.DIGESTS, **{"platform:linux": "f" * 64})
        shared_moved = self.write_shared_lookup(moved, previous=self.DIGESTS, run=7010)
        moved_outputs, _, _ = self.select(
            "--base", self.base, "--head", pr_head, "--compare", "merge-base",
            "--shared-lookup", str(shared_moved), "--results", str(self.pr_record),
            "--default-record", str(self.default_record), *self.LABELS,
        )
        self.assertEqual("run", json.loads(moved_outputs["suites"])["main-only"])
        self.assertEqual({}, json.loads(moved_outputs["reused"]))

        # A manual re-run (--run-attempt 2) takes over nothing from the default-branch record.
        rerun_outputs, _, _ = self.select(
            "--base", self.base, "--head", pr_head, "--compare", "merge-base",
            "--shared-lookup", str(shared_missing), "--results", str(self.pr_record),
            "--default-record", str(self.default_record), "--run-attempt", "2", *self.LABELS,
        )
        self.assertEqual("run", json.loads(rerun_outputs["suites"])["main-only"])
        self.assertEqual({}, json.loads(rerun_outputs["reused"]))


class WholeSuiteKeyTests(unittest.TestCase):
    """The reuse key of a suite selected as a whole holds each entry file with its content."""

    SUITES = {"always": [], "suites": [{"name": "unit", "entries": ["tests/test_*.py"], "paths": ["lib/a.py", "tests/test_a.py"]}]}

    def key(self, files: dict[str, str]) -> str:
        suite_list = suite_selection.resolve_entries(suite_selection.parse_suite_list(json.dumps(self.SUITES)), set(files))
        units = suite_selection.reuse_units(suite_selection.every_suite(suite_list, "test"), suite_list, reuse.Tree(files, suite_selection.matches),
                                            {}, {}, ignore_content={})
        self.assertEqual([unit.entries for unit in units], [tuple(sorted(file for file in files if file.startswith("tests/")))])
        return units[0].key

    def test_the_key_changes_with_the_content_of_an_entry_file_that_the_stored_lists_do_not_match(self) -> None:
        files = {"lib/a.py": "a", "tests/test_a.py": "a", "tests/test_new.py": "new", "stray.txt": "stray"}
        key = self.key(files)
        self.assertNotEqual(self.key(files | {"tests/test_new.py": "changed"}), key)
        self.assertNotEqual(self.key({path: content for path, content in files.items() if path != "tests/test_new.py"}), key,
                            "a tree without the entry file has another key")
        self.assertEqual(self.key(files | {"stray.txt": "changed"}), key, "a file that is no read and no entry file leaves the key")


class SharedDigestTests(unittest.TestCase):
    """The digest of the shared recording step, taken from a copy of the shared CI's part folders as a job fetches them."""

    def setUp(self) -> None:
        self.temporary = temporary_support.Directory()
        self.addCleanup(self.temporary.cleanup)
        self.shared_folder = Path(self.temporary.name) / "shared-ci"
        for part in ("recording", "suite-selection", "python-language-step", "repository-hygiene"):
            shutil.copytree(SHARED_FOLDER / part, self.shared_folder / part, ignore=shutil.ignore_patterns("__pycache__"))

    def digest(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(self.shared_folder / "recording" / "recording.py"), "digest", *arguments],
                              capture_output=True, text=True)

    def taken(self) -> dict[str, str]:
        completed = self.digest()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        (raw,) = re.findall(r"^shared CI digests (\{[^\r\n]+\})$", completed.stdout, re.MULTILINE)
        digests = json.loads(raw)
        self.assertIsInstance(digests, dict)
        for name, value in digests.items():
            self.assertRegex(value, r"^[0-9a-f]{64}$", f"{name} must be a SHA-256 hex digest")
        return digests

    def test_the_digests_separate_platforms_tools_language_steps_and_exclude_selection_code(self) -> None:
        first = self.taken()
        self.assertEqual(first, self.taken(), "the same files give the same digests")
        self.assertLessEqual({"platform:linux", "platform:windows", "tool:node", "tool:gitleaks", "language:python"}, set(first))

        # Windows-only recording files change only platform:windows.
        for changed in ("recording/etw-consumer/Program.cs", "recording/etw-consumer/EtwConsumer.csproj",
                        "recording/etw-consumer/global.json", "recording/etw-consumer/packages.lock.json",
                        "recording/etw-consumer/build/EtwConsumer.dll", "recording/etw_consumer_build.py"):
            with self.subTest(windows_only=changed):
                path = self.shared_folder / changed
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                after = self.taken()
                path.write_bytes(original)
                self.assertNotEqual(first["platform:windows"], after["platform:windows"])
                self.assertEqual({k: v for k, v in first.items() if k != "platform:windows"},
                                 {k: v for k, v in after.items() if k != "platform:windows"})

        # Common recording files change both platform:linux and platform:windows, and no tool or language-step digest.
        for changed in ("recording/action.yml", "recording/recording.py", "suite-selection/suite_list.py",
                        "suite-selection/recording_modules.py"):
            with self.subTest(common_recording=changed):
                path = self.shared_folder / changed
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                after = self.taken()
                path.write_bytes(original)
                self.assertNotEqual(first["platform:linux"], after["platform:linux"])
                self.assertNotEqual(first["platform:windows"], after["platform:windows"])
                self.assertEqual({k: v for k, v in first.items() if not k.startswith("platform:")},
                                 {k: v for k, v in after.items() if not k.startswith("platform:")})

        # Selection code changes no digest at all.
        for changed in ("suite-selection/suite_selection.py", "suite-selection/ci_definition.py",
                        "suite-selection/revisions.py", "suite-selection/reuse.py", "suite-selection/unrecorded.py",
                        "suite-selection/action.yml", "repository-hygiene/hygiene.py"):
            with self.subTest(selection_or_unrelated=changed):
                path = self.shared_folder / changed
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                after = self.taken()
                path.write_bytes(original)
                self.assertEqual(first, after)

        # A change to the Python language step changes only language:python.
        for changed in ("python-language-step/python_language_step.py", "python-language-step/action.yml"):
            with self.subTest(language_step=changed):
                path = self.shared_folder / changed
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                after = self.taken()
                path.write_bytes(original)
                self.assertNotEqual(first["language:python"], after["language:python"])
                self.assertEqual({k: v for k, v in first.items() if k != "language:python"},
                                 {k: v for k, v in after.items() if k != "language:python"})

        # Changing one pinned tool in pins.json changes only that tool's digest (and language:python for ruff/mypy).
        pins_path = self.shared_folder / "repository-hygiene" / "pins.json"
        original_pins = pins_path.read_text(encoding="utf-8")
        pins = json.loads(original_pins)
        pins["tools"]["gitleaks"]["version"] = "9.9.9"
        pins_path.write_text(json.dumps(pins), encoding="utf-8")
        after_gitleaks = self.taken()
        self.assertNotEqual(first["tool:gitleaks"], after_gitleaks["tool:gitleaks"])
        self.assertEqual({k: v for k, v in first.items() if k != "tool:gitleaks"},
                         {k: v for k, v in after_gitleaks.items() if k != "tool:gitleaks"})

        pins = json.loads(original_pins)
        pins["node"]["version"] = "99.0.0"
        pins_path.write_text(json.dumps(pins), encoding="utf-8")
        after_node = self.taken()
        self.assertNotEqual(first["tool:node"], after_node["tool:node"])
        self.assertEqual({k: v for k, v in first.items() if k != "tool:node"},
                         {k: v for k, v in after_node.items() if k != "tool:node"})

        pins = json.loads(original_pins)
        pins["python"]["ruff"]["version"] = "9.9.9"
        pins_path.write_text(json.dumps(pins), encoding="utf-8")
        after_ruff = self.taken()
        self.assertNotEqual(first["tool:ruff"], after_ruff["tool:ruff"])
        self.assertNotEqual(first["language:python"], after_ruff["language:python"])
        self.assertEqual({k: v for k, v in first.items() if k not in ("tool:ruff", "language:python")},
                         {k: v for k, v in after_ruff.items() if k not in ("tool:ruff", "language:python")})
        pins_path.write_text(original_pins, encoding="utf-8")

        # New file in recording/ enters both platform digests; __pycache__ is ignored.
        write(self.shared_folder, "recording/toolchain-rules.json", "{}\n")
        added = self.taken()
        self.assertNotEqual(first["platform:linux"], added["platform:linux"])
        self.assertNotEqual(first["platform:windows"], added["platform:windows"])
        write(self.shared_folder, "recording/__pycache__/recording.cpython-313.pyc", "cache\n")
        self.assertEqual(added, self.taken(), "a bytecode cache is not part of any digest")

    def test_the_digest_follows_the_shared_workflow_of_the_suite_jobs(self) -> None:
        without = self.taken()
        workflow = self.shared_folder.parent / ".github" / "workflows" / "suite-jobs.yml"
        write(workflow.parent, workflow.name, "name: Suite jobs\n")
        first = self.taken()
        self.assertNotEqual(without["platform:linux"], first["platform:linux"], "the workflow that runs a consumer's entry files is part of the platform digests")
        self.assertNotEqual(without["platform:windows"], first["platform:windows"])
        write(workflow.parent, workflow.name, "name: Suite jobs\njobs: {}\n")
        after = self.taken()
        self.assertNotEqual(first["platform:linux"], after["platform:linux"])
        self.assertNotEqual(first["platform:windows"], after["platform:windows"])
        write(workflow.parent, "other.yml", "name: Other\n")
        write(workflow.parent, workflow.name, "name: Suite jobs\n")
        self.assertEqual(first, self.taken(), "another workflow is not part of it")

    PIPELINE = ("name: Pipeline\non: workflow_call\njobs:\n  plan:\n    runs-on: ubuntu-26.04\n\n"
                "  custom:\n    needs: plan\n    steps:\n      - run: bash -- \"$SCRIPT\"\n\n"
                "  dotnet-test:\n    needs: plan\n    steps:\n      - uses: $/actions/dotnet-test\n\n"
                "  node-test:\n    needs: plan\n    steps:\n      - uses: $/actions/node-test\n\n"
                "  python-test:\n    needs: plan\n    steps:\n      - uses: $/actions/python-tests\n\n"
                "  python-verify:\n    needs: plan\n    steps:\n      - uses: $/actions/python-tests\n")
    PARTS = ("part:custom", "part:dotnet-test", "part:node-test", "part:python-test", "part:python-verify")

    def assert_moves_only(self, before: dict[str, str], after: dict[str, str], moved: set[str]) -> None:
        self.assertEqual(moved, {name for name in set(before) | set(after) if before.get(name) != after.get(name)})

    def test_part_digests_track_each_pipeline_part_s_shared_code_independently(self) -> None:
        for part in ("pipeline", "dotnet-test", "node-test", "python-tests"):
            shutil.copytree(SHARED_FOLDER / part, self.shared_folder / part, ignore=shutil.ignore_patterns("__pycache__"))
        workflow = self.shared_folder.parent / ".github" / "workflows" / "pipeline.yml"
        write(workflow.parent, workflow.name, self.PIPELINE)
        first = self.taken()
        for key in self.PARTS:
            self.assertIn(key, first)
        # Changing dotnet-test changes only part:dotnet-test.
        dotnet_script = self.shared_folder / "dotnet-test" / "dotnet_test.py"
        original = dotnet_script.read_bytes()
        dotnet_script.write_bytes(original + b"\n")
        self.assert_moves_only(first, self.taken(), {"part:dotnet-test"})
        dotnet_script.write_bytes(original)
        # Changing the job of one part in pipeline.yml changes only that part's digest.
        write(workflow.parent, workflow.name, self.PIPELINE.replace("uses: $/actions/node-test", "uses: $/actions/node-test\n        with: {a: b}"))
        self.assert_moves_only(first, self.taken(), {"part:node-test"})
        # Changing another job, or the blank lines between jobs, changes no part digest.
        write(workflow.parent, workflow.name, self.PIPELINE.replace("runs-on: ubuntu-26.04\n", "runs-on: ubuntu-26.04\n    timeout-minutes: 5\n\n\n"))
        self.assert_moves_only(first, self.taken(), set())
        # Changing the lines before jobs: changes every part digest and no platform, tool or language digest.
        write(workflow.parent, workflow.name, self.PIPELINE.replace("name: Pipeline\n", "name: Pipeline v2\n"))
        self.assert_moves_only(first, self.taken(), set(self.PARTS))
        write(workflow.parent, workflow.name, self.PIPELINE)

    def test_a_part_job_that_the_pipeline_workflow_does_not_hold_as_expected_fails_the_digest(self) -> None:
        workflow = self.shared_folder.parent / ".github" / "workflows" / "pipeline.yml"
        for name, text in (("missing", self.PIPELINE.replace("  node-test:\n", "  node-tests:\n")),
                           ("indented", self.PIPELINE.replace("  node-test:\n", "    node-test:\n"))):
            with self.subTest(case=name):
                write(workflow.parent, workflow.name, text)
                completed = self.digest()
                self.assertNotEqual(0, completed.returncode)
                self.assertIn("the job of the part node-test cannot be found in ../.github/workflows/pipeline.yml", completed.stdout + completed.stderr)

    def test_part_digests_leave_out_what_no_part_job_runs(self) -> None:
        for part in ("pipeline", "dotnet-test", "node-test", "python-tests"):
            shutil.copytree(SHARED_FOLDER / part, self.shared_folder / part, ignore=shutil.ignore_patterns("__pycache__"))
        workflow = self.shared_folder.parent / ".github" / "workflows" / "pipeline.yml"
        write(workflow.parent, workflow.name, self.PIPELINE)
        first = self.taken()
        for relative in ("pipeline/consumer-kit/README.md", "pipeline/schema/README.md", "pipeline/release.py", "pipeline/plan.py",
                         "python-tests/consumer-kit/README.md", "dotnet-test/consumer-kit/README.md",
                         "python-language-step/consumer-kit/README.md", "python-language-step/consumer-kit/python-language-step.yml"):
            with self.subTest(file=relative):
                path = self.shared_folder / relative
                original = path.read_bytes() if path.is_file() else None
                write(self.shared_folder, relative, "changed\n")
                self.assert_moves_only(first, self.taken(), set())
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)

    def test_the_digest_is_the_same_whichever_order_a_platform_sorts_the_shared_files_in(self) -> None:
        # Windows orders paths case-insensitively and Linux by code point, so the same revision lists its shared
        # files in two orders: recording/etw-consumer/EtwConsumer.csproj comes after build/ on Windows, before it on Linux.
        script = (
            "import json, sys; from pathlib import PurePosixPath, PureWindowsPath\n"
            f"sys.path.insert(0, {str(self.shared_folder / 'recording')!r})\n"
            "import recording\n"
            "files = recording.platform_files('windows')\n"
            "for flavour in (PurePosixPath, PureWindowsPath):\n"
            "    order = sorted(files, key=lambda path: flavour(recording.shared_name(path)))\n"
            "    print(' '.join(recording.shared_name(path) for path in order))\n"
            "    print(recording.files_digest(order))\n"
        )
        completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        linux_order, linux_digest, windows_order, windows_digest = completed.stdout.splitlines()
        self.assertNotEqual(linux_order, windows_order, "the shared files sort differently on the two platforms")
        self.assertEqual(linux_digest, windows_digest)
        self.assertEqual(self.taken()["platform:windows"], linux_digest)

    def test_expect_fails_on_a_digest_that_differs_from_the_one_select_took(self) -> None:
        digests = self.taken()
        expected = json.dumps(digests, sort_keys=True, separators=(",", ":"))
        output = Path(self.temporary.name) / "output.txt"
        self.assertEqual(self.digest("--expect", expected, "--github-output", str(output)).returncode, 0)
        self.assertEqual(f"shared-digests={expected}\n", output.read_text(encoding="utf-8"))
        moved_map = dict(digests, **{"platform:linux": "0" * 64})
        moved = self.digest("--expect", json.dumps(moved_map, sort_keys=True, separators=(",", ":")))
        self.assertEqual(moved.returncode, 1)
        self.assertIn("platform:linux", moved.stderr)
        self.assertIn("v1 moved during the run", moved.stderr)
        bad = self.digest("--expect", "not-json")
        self.assertEqual(bad.returncode, 1)


class SharedDigestScopeTests(unittest.TestCase):
    """The digests that recording.py takes in a job and those that promote_v1.py takes from a commit name the same
    digests over the same files, on a committed copy of the shared CI's part folders."""

    PARTS = ("recording", "suite-selection", "python-language-step", "repository-hygiene", "pipeline", "dotnet-test", "node-test", "python-tests")

    def setUp(self) -> None:
        self.temporary = temporary_support.Directory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        for part in self.PARTS:
            shutil.copytree(SHARED_FOLDER / part, self.repo / "actions" / part, ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("pipeline.yml", "suite-jobs.yml"):
            target = self.repo / ".github" / "workflows" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(SHARED_FOLDER.parent / ".github" / "workflows" / name, target)

    def scopes(self) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        """The scopes of recording.py on the copy's files and of promote_v1.py on its commit, both by repository path."""
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A", "-f", ".")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "fixture")
        actions = self.repo / "actions"
        script = (
            "import json, sys\n"
            f"sys.path[:0] = [{str(actions / 'recording')!r}, {str(actions / 'suite-selection')!r}, {str(actions / 'repository-hygiene')!r}]\n"
            "import recording, promote_v1\n"
            "def by_path(name):\n"
            "    return name[3:] if name.startswith('../') else name if name.startswith('pins:') else 'actions/' + name\n"
            "print(json.dumps({name: sorted(by_path(entry) for entry in entries) for name, entries in recording.shared_digest_scopes().items()}))\n"
            f"print(json.dumps(promote_v1.commit_digest_scopes(promote_v1.Path({str(self.repo)!r}), 'HEAD')))\n"
        )
        completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        recorded, promoted = completed.stdout.splitlines()
        return json.loads(recorded), json.loads(promoted)

    def test_both_take_the_same_digests_over_the_same_files(self) -> None:
        recorded, promoted = self.scopes()
        self.assertEqual(sorted(recorded), sorted(promoted))
        for name in recorded:
            with self.subTest(digest=name):
                self.assertEqual(recorded[name], promoted[name])
        for name in ("platform:linux", "platform:windows", "tool:gitleaks", "language:python", "part:custom", "part:dotnet-test"):
            self.assertIn(name, recorded)
        self.assertFalse([entry for entries in recorded.values() for entry in entries if "/consumer-kit/" in entry],
                         "no digest covers a consumer kit, which no job runs")

    def test_a_consumer_kit_file_moves_no_digest_of_a_commit(self) -> None:
        self.scopes()
        before = git(self.repo, "rev-parse", "HEAD").strip()
        for relative in ("actions/python-language-step/consumer-kit/README.md", "actions/python-tests/consumer-kit/README.md"):
            write(self.repo, relative, "changed\n")
        git(self.repo, "add", "-A", "-f", ".")
        git(self.repo, "commit", "-q", "-m", "kit")
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(self.repo / 'actions' / 'repository-hygiene')!r})\n"
            "import promote_v1\n"
            f"print(promote_v1.moved_digests(promote_v1.Path({str(self.repo)!r}), {before!r}, 'HEAD'))\n"
        )
        completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual("[]", completed.stdout.strip())

    def part_job_texts(self, workflow: str, jobs: list[str]) -> tuple[dict, dict]:
        actions = self.repo / "actions"
        script = (
            "import json, sys\n"
            f"sys.path[:0] = [{str(actions / 'recording')!r}, {str(actions / 'suite-selection')!r}, {str(actions / 'repository-hygiene')!r}]\n"
            "import recording, promote_v1\n"
            "workflow, jobs = json.loads(sys.stdin.read())\n"
            "print(json.dumps({job: recording.part_job_text(workflow, job) for job in jobs}))\n"
            "print(json.dumps({job: promote_v1.part_job_text(workflow, job) for job in jobs}))\n"
        )
        completed = subprocess.run([sys.executable, "-c", script], input=json.dumps([workflow, jobs]), capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        recorded, promoted = completed.stdout.splitlines()
        return json.loads(recorded), json.loads(promoted)

    def test_both_read_the_same_job_text_and_a_comment_inside_a_job_keeps_the_rest_of_it(self) -> None:
        pipeline = (self.repo / ".github" / "workflows" / "pipeline.yml").read_text(encoding="utf-8")
        parts = ["custom", "dotnet-test", "node-test", "python-test", "python-verify"]
        recorded, promoted = self.part_job_texts(pipeline, parts)
        for part in parts:
            with self.subTest(part=part):
                self.assertIsNotNone(recorded[part])
                self.assertEqual(recorded[part], promoted[part])
        workflow = ("name: Pipeline\njobs:\n  plan:\n    runs-on: ubuntu-26.04\n\n"
                    "  node-test:\n    needs: plan\n  # a comment at the job's indentation\n    steps:\n      - uses: $/actions/node-test\n\n"
                    "  # a comment between jobs\n\n  python-test:\n    needs: plan\n")
        recorded, promoted = self.part_job_texts(workflow, ["node-test", "python-test"])
        self.assertEqual(recorded, promoted)
        self.assertEqual("name: Pipeline\n  node-test:\n    needs: plan\n  # a comment at the job's indentation\n    steps:\n"
                         "      - uses: $/actions/node-test\n", recorded["node-test"])
        self.assertEqual("name: Pipeline\n  python-test:\n    needs: plan\n", recorded["python-test"])

    def test_promote_v1_fails_on_a_commit_whose_pipeline_workflow_does_not_hold_a_part_job_as_expected(self) -> None:
        workflow = self.repo / ".github" / "workflows" / "pipeline.yml"
        workflow.write_text(workflow.read_text(encoding="utf-8").replace("\n  dotnet-test:\n", "\n   dotnet-test:\n", 1), encoding="utf-8")
        git(self.repo, "init", "-q")
        git(self.repo, "add", "-A", "-f", ".")
        git(self.repo, "commit", "-q", "-m", "fixture")
        script = (
            "import sys\n"
            f"sys.path.insert(0, {str(self.repo / 'actions' / 'repository-hygiene')!r})\n"
            "import promote_v1\n"
            "try:\n"
            f"    promote_v1.commit_digests(promote_v1.Path({str(self.repo)!r}), 'HEAD')\n"
            "except promote_v1.DigestScopeError as error:\n"
            "    print(error)\n"
            "else:\n"
            "    print('no error')\n"
        )
        completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("the job of the part dotnet-test cannot be found in .github/workflows/pipeline.yml", completed.stdout)

    def test_a_folder_added_to_one_side_only_breaks_the_tie(self) -> None:
        recording_script = self.repo / "actions" / "recording" / "recording.py"
        text = recording_script.read_text(encoding="utf-8")
        recording_script.write_text(text.replace('"custom": (),', '"custom": (SHARED_FOLDER / "pipeline",),', 1), encoding="utf-8")
        recorded, promoted = self.scopes()
        self.assertNotEqual(recorded["part:custom"], promoted["part:custom"])
        promote_script = self.repo / "actions" / "repository-hygiene" / "promote_v1.py"
        text = promote_script.read_text(encoding="utf-8")
        promote_script.write_text(text.replace('LANGUAGE_STEP_FOLDERS = {"python": "actions/python-language-step"}',
                                               'LANGUAGE_STEP_FOLDERS = {"python": "actions/python-language-step", "node": "actions/node-test"}', 1),
                                  encoding="utf-8")
        recorded, promoted = self.scopes()
        self.assertIn("language:node", promoted)
        self.assertNotIn("language:node", recorded)


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


class DispatchCommitTests(FixtureRepository):
    """A dependabot-merged run stands in for a push on the default branch only with a payload commit on that branch."""

    def setUp(self) -> None:
        super().setUp()
        self.earlier = self.commit("earlier", {"src/feature.py": "feature v2\n"})
        self.tip = self.commit("tip", {"src/other.py": "other v2\n"})
        git(self.root, "checkout", "-q", "-b", "side", self.earlier)
        self.side = self.commit("side", {"src/moved.py": "moved v2\n"})
        git(self.root, "checkout", "-q", "main")
        git(self.root, "update-ref", "refs/remotes/origin/main", self.tip)

    def check(self, payload: str) -> tuple[int, str, str]:
        printed, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(printed), mock.patch.object(sys, "stderr", errors):
            code = suite_selection.main(["check-dispatch", "--root", str(self.root), "--sha", payload, "--branch", "main"])
        return code, printed.getvalue(), errors.getvalue()

    def test_a_payload_commit_on_the_default_branch_passes_at_its_tip_and_before_it(self) -> None:
        for commit in (self.tip, self.earlier, self.base):
            with self.subTest(commit=commit):
                code, printed, errors = self.check(commit)
                self.assertEqual((code, errors), (0, ""))
                self.assertEqual(f"client_payload.sha {commit} is on `origin/main`, the default branch.\n", printed)

    def test_a_payload_commit_off_the_default_branch_fails_with_the_commit_and_the_reason(self) -> None:
        code, printed, errors = self.check(self.side)
        self.assertEqual((code, printed), (1, ""))
        self.assertEqual(f"client_payload.sha {self.side} is not on the default branch: `origin/main` does not reach it.\n", errors)

    def test_a_malformed_payload_fails_with_the_value(self) -> None:
        for payload in (self.tip[:7], self.tip.upper(), self.tip + "0", "main", "HEAD", f"{self.tip}^", ""):
            with self.subTest(payload=payload):
                code, printed, errors = self.check(payload)
                self.assertEqual((code, printed), (1, ""))
                self.assertEqual(f"client_payload.sha '{payload}' is malformed: it is not a full 40-character commit ID in lowercase hexadecimal.\n", errors)

    def test_a_payload_absent_from_the_checkout_fails_with_the_value(self) -> None:
        tree = git(self.root, "rev-parse", f"{self.tip}^{{tree}}")
        for payload in ("0123456789abcdef0123456789abcdef01234567", tree):
            with self.subTest(payload=payload):
                code, printed, errors = self.check(payload)
                self.assertEqual((code, printed), (1, ""))
                self.assertEqual(f"client_payload.sha {payload} is absent from this job's checkout: it names no commit there.\n", errors)

    def test_a_default_branch_that_the_job_did_not_fetch_fails(self) -> None:
        git(self.root, "update-ref", "-d", "refs/remotes/origin/main")
        code, printed, errors = self.check(self.tip)
        self.assertEqual((code, printed), (1, ""))
        self.assertEqual(f"client_payload.sha {self.tip} cannot be checked: `origin/main`, the default branch, is not in this job's checkout.\n", errors)


if __name__ == "__main__":
    unittest.main()
