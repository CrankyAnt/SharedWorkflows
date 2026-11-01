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
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tests.suite_selection_fixtures import force_add, git, write

SHARED_FOLDER = Path(__file__).resolve().parents[1] / "actions"
TOOL = SHARED_FOLDER / "suite-selection"
sys.path.insert(0, str(TOOL))

import reuse
import suite_selection

# A recorded excerpt of the checkout step's log of a pull-request run (job 122831541139), with the commits of the
# fixture in place of the recorded ones.
CHECKOUT_LOG = """\
2026-10-28T13:01:58.3224379Z ##[group]Fetching the repository
2026-10-28T13:01:58.3227927Z [command]/usr/bin/git -c protocol.version=2 fetch --no-tags --prune --no-recurse-submodules origin +refs/heads/*:refs/remotes/origin/* +refs/tags/*:refs/tags/* +{merge}:refs/remotes/pull/{number}/merge
2026-10-28T13:01:59.5831460Z ##[endgroup]
2026-10-28T13:01:59.5843798Z ##[group]Checking out the ref
2026-10-28T13:01:59.5846273Z [command]/usr/bin/git checkout --progress --force refs/remotes/pull/{number}/merge
2026-10-28T13:01:59.6554185Z Note: switching to 'refs/remotes/pull/{number}/merge'.
2026-10-28T13:01:59.6589815Z HEAD is now at {short} Merge {head} into {base}
2026-10-28T13:01:59.6597525Z ##[endgroup]
"""

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


class FixtureRepository(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
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
    """The push lookup with recorded API and log responses, and select over its result."""

    NUMBER = 7
    RUN = 4100
    JOB = 4200

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

    def record(self, *, log: bool = True, parents: list[str] | None = None, pulls: list | None = None) -> None:
        short = self.merge[:7]
        self.api.json(f"commits/{self.pushed}/pulls", pulls if pulls is not None else
                      [{"number": self.NUMBER, "state": "closed", "merge_commit_sha": self.pushed, "head": {"sha": self.pr_head, "ref": "topic"}}])
        self.api.json(f"actions/workflows/validate.yml/runs?head_sha={self.pr_head}&event=pull_request&status=success&per_page=1",
                      {"total_count": 1, "workflow_runs": [{"id": self.RUN, "head_sha": self.pr_head, "event": "pull_request", "conclusion": "success"}]})
        self.api.json(f"actions/runs/{self.RUN}/jobs?per_page=100", {"total_count": 2, "jobs": [
            {"id": self.JOB - 1, "name": "validation / validation", "steps": [{"name": "Set up job"}]},
            {"id": self.JOB, "name": "select / select", "steps": [{"name": "Set up job"}, {"name": "Run actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"}]}]})
        self.api.log(self.JOB, CHECKOUT_LOG.format(merge=self.merge, number=self.NUMBER, short=short, head=self.pr_head, base=self.base) if log
                     else "2026-10-28T13:01:59.6597525Z ##[endgroup]\n")
        tree = git(self.root, "rev-parse", f"{self.merge}^{{tree}}")
        self.api.json(f"git/commits/{self.merge}", {"sha": self.merge, "tree": {"sha": tree},
                                               "parents": [{"sha": sha} for sha in (parents or [self.base, self.pr_head])]})

    def lookup(self, sha: str, before: str, attempt: int = 1) -> dict:
        output = self.root.parent / "lookup.json"
        environment = {"GITHUB_API_URL": self.api.url, "GITHUB_REPOSITORY": "owner/repository", "GITHUB_TOKEN": "token-for-the-test"}
        with mock.patch.dict(os.environ, environment), redirect_stdout(io.StringIO()):
            code = suite_selection.main(["lookup", "--root", str(self.root), "--sha", sha, "--before", before,
                                         "--workflow", "owner/repository/.github/workflows/validate.yml@refs/heads/main",
                                         "--run-attempt", str(attempt), "--output", str(output)])
        self.assertEqual(code, 0)
        return json.loads(output.read_text(encoding="utf-8"))

    def squash_onto(self, base: str) -> str:
        git(self.root, "reset", "-q", "--hard", base)
        return self.commit("feat: the feature (#7)", {"src/feature.py": "feature v2\n"})

    def test_the_lookup_finds_the_pull_request_reads_the_tested_merge_from_the_checkout_log_and_verifies_it(self) -> None:
        self.pushed = self.squash_onto(self.base)
        self.record()
        found = self.lookup(self.pushed, self.base)
        self.assertEqual(found["case"], "identical", found)
        self.assertEqual((found["pull_request"], found["tested_merge"], found["tested_base"]), (self.NUMBER, self.merge, self.base))
        paths = [path for path, _ in self.api.requests]
        self.assertEqual(paths, [f"/repos/owner/repository/commits/{self.pushed}/pulls",
                                 f"/repos/owner/repository/actions/workflows/validate.yml/runs?head_sha={self.pr_head}&event=pull_request&status=success&per_page=1",
                                 f"/repos/owner/repository/actions/runs/{self.RUN}/jobs?per_page=100",
                                 f"/repos/owner/repository/actions/jobs/{self.JOB}/logs", f"/storage/{self.JOB}",
                                 f"/repos/owner/repository/git/commits/{self.merge}"])
        tokens = dict(self.api.requests)
        self.assertEqual(tokens[f"/repos/owner/repository/commits/{self.pushed}/pulls"], "Bearer token-for-the-test")
        self.assertIsNone(tokens[f"/storage/{self.JOB}"], "the redirected log is read without the token")

    def test_the_recorded_git_database_answers_only_a_full_commit_id(self) -> None:
        self.api.json(f"git/commits/{self.merge[:7]}", {"sha": self.merge})
        self.api.json(f"git/commits/{self.merge}", {"sha": self.merge})
        api = reuse.Api(self.api.url, "owner/repository", None)
        self.assertEqual(api.json(f"git/commits/{self.merge}"), {"sha": self.merge})
        with self.assertRaisesRegex(reuse.ApiError, "answered 404"):
            api.json(f"git/commits/{self.merge[:7]}")

    def test_a_log_without_the_full_id_of_the_tested_merge_falls_back_to_the_pull_request_s_files(self) -> None:
        self.pushed = self.squash_onto(self.base)
        self.record()
        log = CHECKOUT_LOG.format(merge=self.merge, number=self.NUMBER, short=self.merge[:7], head=self.pr_head, base=self.base)
        fetch = log.splitlines()[1]
        variants = {"another full id": log.replace(f"+{self.merge}:", f"+{self.pr_head}:"),
                    "another pull request": log.replace(f"/pull/{self.NUMBER}/merge\n", f"/pull/{self.NUMBER + 1}/merge\n", 1),
                    "no fetch line": log.replace(fetch + "\n", "")}
        for variant, text in variants.items():
            with self.subTest(variant):
                self.assertNotEqual(text, log)
                self.api.log(self.JOB, text)
                self.api.requests.clear()
                found = self.lookup(self.pushed, self.base)
                self.assertEqual((found["case"], found["pull_request"], found["tested_merge"]), ("pull-request", self.NUMBER, None), found)
                self.assertIn(f"holds no full id of the tested merge commit {self.merge[:7]}", found["reason"])
                self.assertNotIn("/git/commits/", " ".join(path for path, _ in self.api.requests))
                lookup = self.root.parent / "lookup.json"
                lookup.write_text(json.dumps(found), encoding="utf-8")
                _, _, summary = self.select("--lookup", str(lookup))
                self.assertIn(f"The selection runs over the pull request's files: {found['reason']}.", summary)

    def test_a_merge_commit_whose_parents_do_not_verify_falls_back_to_the_pull_request_s_files(self) -> None:
        self.pushed = self.squash_onto(self.base)
        self.record(parents=[self.base, self.base])
        found = self.lookup(self.pushed, self.base)
        self.assertEqual(found["case"], "pull-request")
        self.assertIn("does not verify", found["reason"])

    def test_a_push_whose_tree_equals_the_passed_tested_tree_runs_no_suite(self) -> None:
        self.pushed = self.squash_onto(self.base)
        self.record()
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps(self.lookup(self.pushed, self.base)), encoding="utf-8")
        outputs, printed, summary = self.select("--lookup", str(lookup), "--language-step", "python")
        self.assertEqual(json.loads(outputs["suites"]), {name: "skip" for name in ("pr-only", "both", "main-only", "per-entry", "windows")})
        self.assertEqual((outputs["shards"], outputs["windows"], outputs["language-steps"]), ("[]", "", '{"python":"skip"}'))
        self.assertIn(f"the tree equals the tested tree that passed on pull request #{self.NUMBER}", printed)
        self.assertIn("## Push lookup\n\nNo suite runs: ", summary)

    def test_the_lookup_of_an_identical_tree_names_the_digest_its_pull_request_run_took(self) -> None:
        self.pushed = self.squash_onto(self.base)
        self.record()
        found = self.lookup(self.pushed, self.base)
        self.assertEqual((found["case"], found["tested_run"], found["tested_digest"]), ("identical", self.RUN, None),
                         "a run whose jobs take no digest names none")
        digest = "c" * 64
        jobs = {"total_count": 2, "jobs": [
            {"id": self.JOB - 1, "name": "validation / validation", "steps": [{"name": "Set up job"}]},
            {"id": self.JOB, "name": "select / select", "steps": [{"name": "Set up job"}, {"name": "Run actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"},
                                                                  {"name": "Take the digest of the shared recording step"}]}]}
        self.api.json(f"actions/runs/{self.RUN}/jobs?per_page=100", jobs)
        log = CHECKOUT_LOG.format(merge=self.merge, number=self.NUMBER, short=self.merge[:7], head=self.pr_head, base=self.base)
        self.api.log(self.JOB, log + f"2026-10-28T13:02:03.0000000Z shared CI digest {digest}\n")
        self.api.requests.clear()
        found = self.lookup(self.pushed, self.base)
        self.assertEqual((found["case"], found["tested_run"], found["tested_digest"]), ("identical", self.RUN, digest), found)
        self.assertEqual(sum(path.endswith(f"/jobs/{self.JOB}/logs") for path, _ in self.api.requests), 1, "the checkout job's log is read once")
        jobs["jobs"][1]["steps"].pop()
        jobs["jobs"][0]["steps"].append({"name": "Take the digest of the shared recording step"})
        self.api.json(f"actions/runs/{self.RUN}/jobs?per_page=100", jobs)
        self.api.log(self.JOB - 1, f"2026-10-28T13:02:03.0000000Z shared CI digest {digest}\n")
        self.api.log(self.JOB, log)
        self.assertEqual(self.lookup(self.pushed, self.base)["tested_digest"], digest, "the digest comes from the job that took it")
        self.api.responses.pop(f"/storage/{self.JOB - 1}")
        found = self.lookup(self.pushed, self.base)
        self.assertEqual((found["case"], found["tested_digest"]), ("identical", None), "a log that cannot be read names no digest")

    def test_a_push_whose_pull_request_was_behind_runs_only_the_units_that_both_sides_affect(self) -> None:
        moved = self.commit("main moves", {"src/moved.py": "moved v2\n"})
        self.pushed = self.squash_onto(moved)
        self.record()
        found = self.lookup(self.pushed, moved)
        self.assertEqual((found["case"], found["tested_base"], found["landing_base"]), ("behind", self.base, moved), found)
        self.assertEqual(found["tested_merge"], self.merge)
        self.assertIn(f"/repos/owner/repository/git/commits/{self.merge}", [path for path, _ in self.api.requests])
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps(found), encoding="utf-8")
        outputs, printed, _ = self.select("--lookup", str(lookup))
        self.assertEqual(json.loads(outputs["suites"]), {"pr-only": "skip", "both": "run", "main-only": "skip", "per-entry": "skip", "windows": "skip"})
        self.assertIn("both: run (both sides: `src/feature.py` matches `src/feature.py`; `src/moved.py` matches `src/moved.py`)", printed)
        self.assertEqual([entry["entry"] for shard in json.loads(outputs["shards"]) for entry in shard["entries"]], ["tests/test_both.py"])

    def test_without_the_log_the_push_selects_over_the_pull_request_s_files(self) -> None:
        moved = self.commit("main moves", {"src/moved.py": "moved v2\n"})
        self.pushed = self.squash_onto(moved)
        self.record(log=False)
        found = self.lookup(self.pushed, moved)
        self.assertEqual((found["case"], found["pull_request"], found["landing_base"]), ("pull-request", self.NUMBER, moved), found)
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps(found), encoding="utf-8")
        outputs, printed, _ = self.select("--lookup", str(lookup))
        self.assertEqual(json.loads(outputs["suites"]), {"pr-only": "run", "both": "run", "main-only": "skip", "per-entry": "run", "windows": "run"})
        self.assertIn("Changed files: src/feature.py\n", printed)

    def test_a_push_without_a_pull_request_selects_over_its_own_changed_files(self) -> None:
        self.pushed = self.commit("direct push", {"src/moved.py": "moved v3\n"})
        self.record(pulls=[])
        found = self.lookup(self.pushed, self.base)
        self.assertEqual((found["case"], found["pull_request"]), ("own", None), found)
        self.assertEqual(len(self.api.requests), 1)
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps(found), encoding="utf-8")
        outputs, _, _ = self.select("--lookup", str(lookup))
        self.assertEqual(json.loads(outputs["suites"]), {"pr-only": "skip", "both": "run", "main-only": "run", "per-entry": "skip", "windows": "skip"})

    def test_a_dependabot_merged_run_starts_its_lookup_from_the_payload_commit(self) -> None:
        self.pushed = self.squash_onto(self.base)
        later = self.commit("a later commit on the default branch", {"README.md": "later\n"})
        self.record()
        found = self.lookup(self.pushed, "")
        self.assertEqual((found["case"], found["head"], found["landing_base"]), ("identical", self.pushed, self.base))
        self.assertEqual(self.api.requests[0][0], f"/repos/owner/repository/commits/{self.pushed}/pulls")
        self.assertNotIn(later, " ".join(path for path, _ in self.api.requests))

    def test_a_re_run_of_a_push_makes_no_lookup_and_selects_over_its_own_changed_files(self) -> None:
        self.pushed = self.squash_onto(self.base)
        self.record()
        found = self.lookup(self.pushed, self.base, attempt=2)
        self.assertEqual((found["case"], found["landing_base"]), ("own", self.base), found)
        self.assertEqual(self.api.requests, [])
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps(found), encoding="utf-8")
        outputs, _, _ = self.select("--lookup", str(lookup))
        self.assertEqual(json.loads(outputs["suites"])["pr-only"], "run")

    def test_an_unreachable_api_never_skips(self) -> None:
        self.pushed = self.squash_onto(self.base)
        self.api.close()
        found = self.lookup(self.pushed, self.base)
        self.assertEqual(found["case"], "own")
        self.assertIn("could not be looked up", found["reason"])


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

    def store(self, outputs: dict[str, str], passed: list[tuple[str, str]], run: int, result: str = "success") -> bool:
        needs = self.root.parent / "needs.json"
        needs.write_text(json.dumps({"select": {"result": result, "outputs": outputs}, "linux": {"result": "success", "outputs": {}}}), encoding="utf-8")
        directory = self.root.parent / f"passed-{run}"
        (directory / "suite-results-shard-1").mkdir(parents=True, exist_ok=True)
        (directory / "suite-results-shard-1" / "passed.json").write_text(json.dumps([{"suite": suite, "entry": entry} for suite, entry in passed]), encoding="utf-8")
        github_output = self.root.parent / "store-output.txt"
        github_output.unlink(missing_ok=True)
        with redirect_stdout(io.StringIO()):
            code = suite_selection.main(["store", "--needs", str(needs), "--passed", str(directory), "--run-id", str(run),
                                         "--output", str(self.record), "--github-output", str(github_output)])
        self.assertEqual(code, 0)
        return github_output.read_text(encoding="utf-8") == "stored=true\n"

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

    def test_a_later_head_reuses_the_passes_that_keep_left_in_and_runs_the_entry_files_with_additions_again(self) -> None:
        outputs = self.select_head(self.head({"src/feature.py": "feature v2\n"}))
        passed, additions = self.root.parent / "passed.json", self.root.parent / "additions.json"
        passed.write_text(json.dumps([{"suite": suite, "entry": entry} for suite, entry in self.ALL]), encoding="utf-8")
        # The additions of one job, in the shape check writes them: an entry file of a suite selected per entry file, and a
        # pooled suite.
        additions.write_text(json.dumps({"kind": "additions", "suite_list": "suites.json", "suites": [
            {"suite": "per-entry", "create": False, "recorded_entries": ["tests/test_one.py"], "entries": {"tests/test_one.py": {"paths": ["README.md"]}}},
            {"suite": "pr-only", "create": False, "recorded_entries": ["tests/test_pr.py"], "reads": {"paths": ["README.md"]}}]}), encoding="utf-8")
        completed = subprocess.run([sys.executable, str(SHARED_FOLDER / "recording" / "recording.py"), "keep", "--additions", str(additions), str(passed)],
                                   capture_output=True, text=True)
        self.assertEqual(0, completed.returncode, completed.stderr)
        kept = [(record["suite"], record["entry"]) for record in json.loads(passed.read_text(encoding="utf-8"))]
        self.assertEqual([("both", "tests/test_both.py"), ("windows", "tests/test_win.py")], kept)
        self.assertTrue(self.store(outputs, kept, 9007))
        later = self.select_head(self.head({"README.md": "a change no suite reads\n"}))
        self.assertEqual(["per-entry", "pr-only"], sorted(name for name, result in json.loads(later["suites"]).items() if result == "run"))
        self.assertEqual([9007, 9007], sorted(json.loads(later["reused"]).values()))
        self.assertEqual([["tests/test_one.py"], ["tests/test_pr.py"]], sorted(unit["entries"] for unit in json.loads(later["units"]).values()))

    def test_a_suite_split_over_jobs_is_stored_only_when_every_entry_file_passed(self) -> None:
        both = json.loads(json.dumps(SUITES))
        both["suites"][0]["entries"] = ["tests/test_pr.py", "tests/test_main.py"]
        first = self.head({"suites.json": json.dumps(both), "src/feature.py": "feature v2\n"})
        outputs = self.select_head(first)
        self.assertTrue(self.store(outputs, [("pr-only", "tests/test_pr.py")], 9006))
        units = json.loads(outputs["units"])
        self.assertNotIn("pr-only", [units[key]["suite"] for key in json.loads(self.record.read_text(encoding="utf-8"))["passed"]])


class SharedDigestTests(unittest.TestCase):
    """The digest of the shared recording step, taken from a copy of the shared CI's part folders as a job fetches them."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.shared_folder = Path(self.temporary.name) / "shared-ci"
        for part in ("recording", "suite-selection"):
            shutil.copytree(SHARED_FOLDER / part, self.shared_folder / part, ignore=shutil.ignore_patterns("__pycache__"))
        write(self.shared_folder, "repository-hygiene/hygiene.py", "print('a part the recording step does not load')\n")

    def digest(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(self.shared_folder / "recording" / "recording.py"), "digest", *arguments],
                              capture_output=True, text=True)

    def taken(self) -> str:
        completed = self.digest()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        (digest,) = re.findall(r"^shared CI digest ([0-9a-f]{64})$", completed.stdout, re.MULTILINE)
        return digest

    def test_the_digest_follows_the_recording_step_the_modules_it_loads_and_the_etw_consumer(self) -> None:
        first = self.taken()
        self.assertEqual(first, self.taken(), "the same files give the same digest")
        for changed in ("recording/action.yml", "recording/recording.py", "suite-selection/suite_selection.py",
                        "suite-selection/recording_modules.py", "suite-selection/revisions.py", "recording/etw-consumer/build/EtwConsumer.dll"):
            with self.subTest(changed=changed):
                path = self.shared_folder / changed
                original = path.read_bytes()
                path.write_bytes(original + b"\n")
                self.assertNotEqual(first, self.taken())
                path.write_bytes(original)
        write(self.shared_folder, "recording/toolchain-rules.json", "{}\n")
        added = self.taken()
        self.assertNotEqual(first, added, "a new file of the recording step's folder is part of it")
        write(self.shared_folder, "repository-hygiene/hygiene.py", "print('changed')\n")
        write(self.shared_folder, "suite-selection/action.yml", "name: changed\n")
        write(self.shared_folder, "suite-selection/reuse.py", "print('imported only inside the functions of select, the lookup and the store')\n")
        write(self.shared_folder, "recording/__pycache__/recording.cpython-313.pyc", "cache\n")
        self.assertEqual(added, self.taken(), "a part or module it does not load and a bytecode cache are not part of it")

    def test_the_digest_is_the_same_whichever_order_a_platform_sorts_the_shared_files_in(self) -> None:
        # Windows orders paths case-insensitively and Linux by code point, so the same revision lists its shared
        # files in two orders: recording/etw-consumer/EtwConsumer.csproj comes after build/ on Windows, before it on Linux.
        script = (
            "import sys; from pathlib import PurePosixPath, PureWindowsPath\n"
            f"sys.path.insert(0, {str(self.shared_folder / 'recording')!r})\n"
            "import recording\n"
            "files = recording.shared_files()\n"
            "for flavour in (PurePosixPath, PureWindowsPath):\n"
            "    order = sorted(files, key=lambda path: flavour(path.relative_to(recording.SHARED_FOLDER).as_posix()))\n"
            "    print(' '.join(path.relative_to(recording.SHARED_FOLDER).as_posix() for path in order))\n"
            "    recording.shared_files = lambda order=order: order\n"
            "    print(recording.shared_digest())\n"
        )
        completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        linux_order, linux_digest, windows_order, windows_digest = completed.stdout.splitlines()
        self.assertNotEqual(linux_order, windows_order, "the shared files sort differently on the two platforms")
        self.assertEqual(linux_digest, windows_digest)
        self.assertEqual(self.taken(), linux_digest)

    def test_expect_fails_on_a_digest_that_differs_from_the_one_select_took(self) -> None:
        digest = self.taken()
        output = Path(self.temporary.name) / "output.txt"
        self.assertEqual(self.digest("--expect", digest, "--github-output", str(output)).returncode, 0)
        self.assertEqual(f"shared-digest={digest}\n", output.read_text(encoding="utf-8"))
        moved = self.digest("--expect", "0" * 64)
        self.assertEqual(moved.returncode, 1)
        self.assertIn(f"has the digest {digest}, but select took {'0' * 64}: v1 moved during the run", moved.stderr)


class SharedLookupTests(FixtureRepository):
    """In a consumer, a run whose shared digest differs from that of the last passed run on its base branch runs every
    suite, and the reuse key holds the digest. The lookup walks the base branch's first-parent history and asks for the
    passed runs of each commit."""

    DIGEST = "a" * 64
    MOVED = "b" * 64
    RUN = 5100
    BRANCH_QUERY = "actions/workflows/validation.yml/runs?branch=main&status=success&per_page=20"
    ALL = ["both", "main-only", "per-entry", "pr-only", "windows"]

    def setUp(self) -> None:
        super().setUp()
        self.api = RecordedApi()
        self.addCleanup(self.api.close)
        self.record = self.root.parent / "record.json"
        self.fetched(self.base)

    def fetched(self, tip: str) -> None:
        """The base branch's tip as the job's checkout fetches it."""
        git(self.root, "update-ref", "refs/remotes/origin/main", tip)

    @staticmethod
    def runs_of(commit: str) -> str:
        return f"actions/workflows/validation.yml/runs?head_sha={commit}&status=success&per_page=100"

    def record_commit(self, commit: str, runs: list, request_id: str = "") -> None:
        self.api.json(self.runs_of(commit), {"total_count": len(runs), "workflow_runs": runs}, request_id or f"REQ-{commit[:7]}")

    def record_run(self, run: int, digest: str | None = DIGEST) -> None:
        job = run + 100
        steps = [{"name": "Set up job"}, {"name": "Take the digest of the shared recording step"}] if digest is not None else [{"name": "Set up job"}]
        self.api.json(f"actions/runs/{run}/jobs?per_page=100", {"total_count": 2, "jobs": [
            {"id": job - 1, "name": "tests / job", "steps": [{"name": "Set up job"}]}, {"id": job, "name": "select / select", "steps": steps}]},
                      f"REQ-jobs-{run}")
        self.api.log(job, f"2026-10-29T10:00:00.0000000Z shared CI digest {digest}\n" if digest is not None else "")

    @staticmethod
    def passed(run: int, commit: str, *, event: str = "push", branch: str = "main", created: str = "2026-10-29T10:00:00Z") -> dict:
        return {"id": run, "head_sha": commit, "head_branch": branch, "event": event, "created_at": created, "conclusion": "success"}

    def record_branch(self, *, digest: str | None = DIGEST, runs: list | None = None) -> None:
        self.record_commit(self.base, runs if runs is not None else [self.passed(self.RUN, self.base)])
        self.record_run(self.RUN, digest)

    def history(self, count: int) -> list[str]:
        """``count`` commits on main after the base, newest first; the second is the merge of a side branch, so that the
        walk must follow the first parent."""
        commits = []
        for number in range(count):
            if number == 1:
                git(self.root, "checkout", "-q", "-b", "side")
                self.commit("side", {"src/other.py": "side\n"})
                git(self.root, "checkout", "-q", "main")
                git(self.root, "merge", "-q", "--no-ff", "-m", "merge side", "side")
                commits.append(git(self.root, "rev-parse", "HEAD"))
            else:
                commits.append(self.commit(f"commit {number}"))
        commits.reverse()
        self.fetched(commits[0])
        return commits

    def shared_lookup(self, digest: str, pushed: str = "") -> Path:
        self.printed = io.StringIO()
        output = self.root.parent / "shared.json"
        environment = {"GITHUB_API_URL": self.api.url, "GITHUB_REPOSITORY": "owner/repository", "GITHUB_TOKEN": "token-for-the-test"}
        with mock.patch.dict(os.environ, environment), redirect_stdout(self.printed):
            code = suite_selection.main(["shared-lookup", "--root", str(self.root), "--digest", digest, "--branch", "main", "--pushed", pushed,
                                         "--workflow", "owner/repository/.github/workflows/validation.yml@refs/pull/7/merge", "--output", str(output)])
        self.assertEqual(code, 0)
        return output

    def looked_up(self, digest: str = DIGEST, pushed: str = "") -> dict:
        return json.loads(self.shared_lookup(digest, pushed).read_text(encoding="utf-8"))

    def requested(self) -> list[str]:
        return [path.removeprefix("/repos/owner/repository/") for path, _ in self.api.requests]

    def runs(self, outputs: dict[str, str]) -> list[str]:
        return sorted(name for name, result in json.loads(outputs["suites"]).items() if result == "run")

    def every_suite_runs(self, base: str) -> str:
        head = self.commit("head", {"README.md": "a change no suite reads\n"})
        outputs, printed, _ = self.select("--base", base, "--head", head, "--compare", "merge-base", "--shared-lookup", str(self.root.parent / "shared.json"))
        self.assertEqual(self.runs(outputs), self.ALL)
        return printed

    def test_the_digest_of_the_last_passed_run_on_the_base_branch_decides_whether_every_suite_runs(self) -> None:
        self.record_branch()
        head = self.commit("head", {"README.md": "a change no suite reads\n"})
        outputs, _, summary = self.select("--base", self.base, "--head", head, "--compare", "merge-base", "--shared-lookup", str(self.shared_lookup(self.DIGEST)))
        self.assertEqual(self.runs(outputs), [])
        self.assertIn("The selection runs as usual: the shared recording step equals that of run 5100", summary)
        outputs, printed, summary = self.select("--base", self.base, "--head", head, "--compare", "merge-base",
                                                "--shared-lookup", str(self.shared_lookup(self.MOVED)))
        self.assertEqual(self.runs(outputs), self.ALL)
        self.assertEqual(sorted(entry["entry"] for shard in json.loads(outputs["shards"]) for entry in shard["entries"]),
                         ["tests/test_both.py", "tests/test_main.py", "tests/test_one.py", "tests/test_pr.py", "tests/test_two.py"])
        self.assertIn("pr-only: run (every suite runs: the shared recording step differs from that of run 5100, the last passed run on `main`", printed)
        self.assertIn("## Shared recording step\n\nEvery suite runs: ", summary)
        self.assertIn(f"This run's digest: `{self.MOVED}`.", summary)
        self.assertEqual(self.requested()[:4], [self.runs_of(self.base), f"actions/runs/{self.RUN}/jobs?per_page=100",
                                                f"actions/jobs/{self.RUN + 100}/logs", f"/storage/{self.RUN + 100}"])

    def test_the_lookup_takes_the_run_of_the_newest_first_parent_commit_while_the_branch_query_answers_an_older_one(self) -> None:
        commits = self.history(3)
        self.api.json(self.BRANCH_QUERY, {"total_count": 2, "workflow_runs": [self.passed(1, self.base), self.passed(self.RUN, commits[0])]})
        self.record_run(1, self.MOVED)
        self.record_commit(commits[0], [self.passed(self.RUN, commits[0])])
        self.record_run(self.RUN)
        found = self.looked_up()
        self.assertEqual((found["run"], found["previous"]), (self.RUN, self.DIGEST), found)
        self.assertNotIn(self.BRANCH_QUERY, self.requested())
        self.assertEqual(self.requested()[0], self.runs_of(commits[0]), "the walk starts at the fetched tip of the base branch")

    def test_the_lookup_takes_the_run_of_the_newest_commit_within_the_bound_that_has_one(self) -> None:
        commits = self.history(5)
        for commit in commits[:3]:
            self.record_commit(commit, [])
        self.record_commit(commits[3], [self.passed(self.RUN, commits[3])])
        self.record_commit(commits[4], [self.passed(self.RUN + 7, commits[4])])
        self.record_run(self.RUN)
        found = self.looked_up()
        self.assertEqual(found["run"], self.RUN, found)
        self.assertEqual(self.requested()[:4], [self.runs_of(commit) for commit in commits[:4]])
        self.assertNotIn(self.runs_of(commits[4]), self.requested(), "the first commit with an accepted run decides")
        self.assertNotIn(self.runs_of(git(self.root, "rev-parse", "side")), self.requested(), "the walk follows the first parent")

    def test_among_several_accepted_runs_of_one_commit_the_newest_decides(self) -> None:
        commits = self.history(1)
        self.record_commit(commits[0], [self.passed(self.RUN - 5, commits[0], created="2026-10-29T09:00:00Z"),
                                        self.passed(self.RUN, commits[0], created="2026-10-29T11:00:00Z", event="repository_dispatch"),
                                        self.passed(self.RUN - 3, commits[0], created="2026-10-29T10:00:00Z")])
        self.record_run(self.RUN)
        self.assertEqual(self.looked_up()["run"], self.RUN)

    def test_a_run_whose_commit_branch_or_event_does_not_match_is_not_taken_and_the_log_names_it(self) -> None:
        commits = self.history(2)
        self.record_commit(commits[0], [self.passed(6001, commits[1]), self.passed(6002, commits[0], branch="topic"),
                                        self.passed(6003, commits[0], event="pull_request"), {"id": "6004", "head_sha": commits[0]},
                                        "not a run"])
        self.record_commit(commits[1], [self.passed(self.RUN, commits[1])])
        self.record_run(self.RUN)
        found = self.looked_up()
        self.assertEqual(found["run"], self.RUN, found)
        printed = self.printed.getvalue()
        self.assertIn(f"  run 6001: head_sha {commits[1]}, head_branch main, event push: not taken, its head_sha is not {commits[0]}\n", printed)
        self.assertIn(f"  run 6002: head_sha {commits[0]}, head_branch topic, event push: not taken, its head_branch is not main\n", printed)
        self.assertIn(f"  run 6003: head_sha {commits[0]}, head_branch main, event pull_request: not taken, its event is not push or repository_dispatch\n",
                      printed)
        self.assertIn(f"  run 6004: head_sha {commits[0]}, head_branch None, event None: not taken, it has no numeric id\n", printed)
        self.assertIn('  "not a run": not taken, it is no run object\n', printed)
        self.assertIn(f"  run {self.RUN}: head_sha {commits[1]}, head_branch main, event push: accepted\n", printed)

    def test_a_walk_that_reaches_its_bound_finds_no_run_and_every_suite_runs(self) -> None:
        commits = self.history(21)
        for commit in commits[:20]:
            self.record_commit(commit, [])
        self.record_commit(commits[20], [self.passed(self.RUN, commits[20])])
        self.record_run(self.RUN)
        found = self.looked_up()
        self.assertEqual((found["previous"], found["run"]), (None, None), found)
        self.assertIn("the walk reached its bound of 20 first-parent commits", found["reason"])
        self.assertEqual(self.requested(), [self.runs_of(commit) for commit in commits[:20]])
        self.assertIn("the walk reached its bound of 20 first-parent commits", self.printed.getvalue())
        self.assertIn("reached its bound", self.every_suite_runs(commits[0]))

    def test_a_walk_that_reaches_the_first_commit_finds_no_run(self) -> None:
        self.record_commit(self.base, [])
        found = self.looked_up()
        self.assertEqual(found["previous"], None)
        self.assertIn("the walk reached the first commit of `main`", found["reason"])

    def test_a_failed_request_finds_no_run_and_names_the_request(self) -> None:
        commits = self.history(2)
        for failed, reason in ((self.runs_of(commits[0]), "a request of the walk over `main` failed: "),
                               (f"actions/runs/{self.RUN}/jobs?per_page=100", f"run {self.RUN} on `main` could not be read: ")):
            with self.subTest(failed=failed):
                self.record_commit(commits[0], [])
                self.record_commit(commits[1], [self.passed(self.RUN, commits[1])])
                self.record_run(self.RUN)
                self.api.responses["/repos/owner/repository/" + failed] = (500, b'{"message": "Server Error"}', {"X-GitHub-Request-Id": "REQ-FAILED"})
                found = self.looked_up()
                self.assertEqual(found["previous"], None, found)
                self.assertIn(f"{reason}GET {failed} answered 500", found["reason"])
                self.assertIn(f"GET {failed}: x-github-request-id REQ-FAILED, failed: GET {failed} answered 500", self.printed.getvalue())
                self.every_suite_runs(commits[0])
                git(self.root, "reset", "-q", "--hard", commits[0])

    def test_every_lookup_logs_its_requests_the_runs_returned_the_start_and_the_run_taken(self) -> None:
        commits = self.history(2)
        self.record_commit(commits[0], [], "REQ-FIRST")
        self.record_commit(commits[1], [self.passed(self.RUN, commits[1]), self.passed(6001, commits[1], branch="topic")], "REQ-SECOND")
        self.record_run(self.RUN)
        self.looked_up()
        printed = self.printed.getvalue()
        self.assertIn(f"The walk starts at {commits[0]}, the tip of `origin/main` fetched in this job, and reads at most 20 first-parent commits.\n", printed)
        self.assertIn(f"GET {self.runs_of(commits[0])}: x-github-request-id REQ-FIRST, total_count 0\n", printed)
        self.assertIn(f"GET {self.runs_of(commits[1])}: x-github-request-id REQ-SECOND, total_count 2\n", printed)
        self.assertIn(f"  run {self.RUN}: head_sha {commits[1]}, head_branch main, event push: accepted\n", printed)
        self.assertIn(f"  run 6001: head_sha {commits[1]}, head_branch topic, event push: not taken, its head_branch is not main\n", printed)
        self.assertIn(f"Took run {self.RUN} of {commits[1]}: the newest accepted run of the newest first-parent commit that has one, "
                      f"2 commits into the walk.\n", printed)
        self.assertIn(f"GET actions/runs/{self.RUN}/jobs?per_page=100: x-github-request-id REQ-jobs-{self.RUN}, total_count 2\n", printed)
        self.assertIn(f"GET actions/jobs/{self.RUN + 100}/logs: x-github-request-id ", printed)
        self.assertTrue(printed.rstrip("\n").endswith("unchanged: the shared recording step equals that of run 5100, the last passed run on `main`"), printed)

    def test_a_push_walks_from_the_first_parent_of_the_pushed_commit(self) -> None:
        commits = self.history(3)
        self.record_commit(commits[0], [self.passed(self.RUN + 9, commits[0])])
        self.record_commit(commits[1], [self.passed(self.RUN, commits[1])])
        self.record_run(self.RUN)
        found = self.looked_up(pushed=commits[0])
        self.assertEqual(found["run"], self.RUN, found)
        self.assertNotIn(self.runs_of(commits[0]), self.requested(), "the pushed commit's own runs are never read")
        self.assertIn(f"The walk starts at {commits[1]}, the first parent of the pushed commit {commits[0]}, ", self.printed.getvalue())
        self.fetched(self.base)
        self.assertEqual(self.looked_up(pushed=commits[0])["run"], self.RUN, "a push ignores the fetched tip")

    def test_a_push_of_a_commit_without_a_parent_finds_no_run(self) -> None:
        found = self.looked_up(pushed=self.base)
        self.assertEqual(found["previous"], None)
        self.assertIn(f"the pushed commit {self.base} has no first parent", found["reason"])
        self.assertEqual(self.requested(), [])

    def test_a_base_branch_that_the_job_did_not_fetch_finds_no_run(self) -> None:
        git(self.root, "update-ref", "-d", "refs/remotes/origin/main")
        found = self.looked_up()
        self.assertEqual(found["previous"], None)
        self.assertIn("`origin/main` is not in this job's checkout", found["reason"])
        self.assertEqual(self.requested(), [])

    def test_a_push_whose_tree_was_tested_still_runs_every_suite_after_a_move(self) -> None:
        self.record_branch()
        lookup = self.root.parent / "lookup.json"
        lookup.write_text(json.dumps({"case": "identical", "head": self.base, "landing_base": self.base, "reason": "tested", "pull_request": 7}), encoding="utf-8")
        outputs, _, _ = self.select("--lookup", str(lookup), "--shared-lookup", str(self.shared_lookup(self.MOVED)))
        self.assertEqual(self.runs(outputs), self.ALL)
        self.assertNotIn("identical", outputs, "the verdict runs on a push that runs suites")
        outputs, _, _ = self.select("--lookup", str(lookup), "--shared-lookup", str(self.shared_lookup(self.DIGEST)))
        self.assertEqual(self.runs(outputs), [])
        self.assertEqual("true", outputs["identical"], "the verdict skips a push that runs nothing but select and hygiene")

    def test_a_push_whose_tree_passed_under_the_same_digest_runs_no_suite_after_a_move(self) -> None:
        # The pull request's first run after the move took the new digest and passed; its merge push takes the same
        # digest on the same tree, while the last passed run on the base branch still took the earlier one.
        self.record_branch()
        lookup = self.root.parent / "lookup.json"
        tested = {"case": "identical", "head": self.base, "landing_base": self.base, "reason": "tested", "pull_request": 7, "tested_run": 4100}
        lookup.write_text(json.dumps({**tested, "tested_digest": self.MOVED}), encoding="utf-8")
        outputs, printed, summary = self.select("--lookup", str(lookup), "--shared-lookup", str(self.shared_lookup(self.MOVED)))
        self.assertEqual(self.runs(outputs), [], printed)
        self.assertEqual("true", outputs["identical"], "the verdict skips a push that runs nothing but select and hygiene")
        self.assertIn("the tested tree took the same shared recording step in run 4100 of pull request #7", summary)
        lookup.write_text(json.dumps({**tested, "tested_digest": self.DIGEST}), encoding="utf-8")
        for digest in (self.MOVED, self.DIGEST):
            with self.subTest(digest=digest):
                self.record_branch(digest=digest)
                outputs, printed, _ = self.select("--lookup", str(lookup), "--shared-lookup", str(self.shared_lookup(self.MOVED)))
                self.assertEqual(self.runs(outputs), self.ALL, "a tree that passed under another shared recording step runs every suite")
                self.assertNotIn("identical", outputs)
                self.assertIn("pr-only: run (every suite runs: the shared recording step differs from that of run 4100 of pull request #7, "
                              "whose tested tree the push equals)", printed)

    def test_a_run_without_a_readable_digest_on_the_base_branch_runs_every_suite(self) -> None:
        head = self.commit("head", {"README.md": "a change no suite reads\n"})
        for case, reason in ((dict(runs=[self.passed(1, self.base, event="pull_request")]), "the walk reached the first commit of `main`"),
                             (dict(digest=None), "took no digest of the shared recording step"), ({}, "answered 404")):
            with self.subTest(reason=reason):
                self.api.responses.clear()
                if case:
                    self.record_branch(**case)
                found = json.loads(self.shared_lookup(self.DIGEST).read_text(encoding="utf-8"))
                self.assertEqual((found["digest"], found["previous"]), (self.DIGEST, None))
                self.assertIn(reason, found["reason"])
                outputs, _, _ = self.select("--base", self.base, "--head", head, "--compare", "merge-base", "--shared-lookup", str(self.root.parent / "shared.json"))
                self.assertEqual(self.runs(outputs), self.ALL)

    def test_the_reuse_key_holds_the_digest(self) -> None:
        self.record_branch()
        first = self.commit("first", {"src/feature.py": "feature v2\n"})
        arguments = ("--base", self.base, "--compare", "merge-base", "--results", str(self.record), "--runner-label", "linux=ubuntu-26.04")
        outputs, _, _ = self.select("--head", first, *arguments, "--shared-lookup", str(self.shared_lookup(self.DIGEST)))
        units = json.loads(outputs["units"])
        self.record.write_text(json.dumps({"passed": {key: 9101 for key in units}}), encoding="utf-8")
        outputs, _, _ = self.select("--head", first, *arguments, "--shared-lookup", str(self.shared_lookup(self.DIGEST)))
        self.assertEqual((self.runs(outputs), sorted(set(json.loads(outputs["reused"]).values()))), ([], [9101]))
        self.record_branch(digest=self.MOVED)
        outputs, _, _ = self.select("--head", first, *arguments, "--shared-lookup", str(self.shared_lookup(self.MOVED)))
        self.assertEqual((json.loads(outputs["reused"]), self.runs(outputs)), ({}, ["both", "per-entry", "pr-only", "windows"]),
                         "the same reads under another shared recording step reuse no pass")


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
