"""Fixture consumers behind a stand-in for the GitHub Actions and Issues APIs, for the tests of the measurement command."""

from __future__ import annotations

import datetime
import http.server
import io
import json
import os
import re
import sys
import threading
import unittest
import zipfile
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

from tests import temporary_support
from tests.pipeline_fixtures import REPOSITORY_ROOT

MEASUREMENT_FOLDER = REPOSITORY_ROOT / "actions" / "measurement"
SCRIPT = MEASUREMENT_FOLDER / "measurement.py"

OWNER = "example-owner"
CONSUMER = f"{OWNER}/consumer"
OTHER = f"{OWNER}/other-consumer"
MANAGEMENT = f"{OWNER}/management"
APP_TOKEN = "token-of-the-measurement-app"
RUN_TOKEN = "token-of-the-run"
NOW = "2026-04-01T12:00:00Z"


def plan_zip(plan: dict, filename: str = "ci-plan.json") -> bytes:
    """Pack a JSON plan document into an in-memory zip archive, as GitHub Actions artifact downloads return."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, json.dumps(plan, separators=(",", ":")) + "\n")
    return buffer.getvalue()


def store_zip(store: dict | str | bytes, filename: str = "measurement-records.json") -> bytes:
    """Pack a measurement store payload into an in-memory zip archive."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if isinstance(store, bytes):
            archive.writestr(filename, store)
        elif isinstance(store, str):
            archive.writestr(filename, store.encode("utf-8"))
        else:
            archive.writestr(filename, json.dumps(store, separators=(",", ":")) + "\n")
    return buffer.getvalue()


def _parse_iso(text: str) -> datetime.datetime:
    if len(text) == 10:
        return datetime.datetime.fromisoformat(f"{text}T00:00:00+00:00")
    parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def _matches_created_filter(created_at: str, expr: str) -> bool:
    value = _parse_iso(created_at)
    if expr.startswith(">="):
        bound = _parse_iso(expr[2:])
        return value >= bound
    if ".." in expr:
        start_raw, end_raw = expr.split("..", 1)
        if start_raw and start_raw != "*":
            if value < _parse_iso(start_raw):
                return False
        if end_raw and end_raw != "*":
            end_bound = _parse_iso(end_raw)
            if len(end_raw) == 10:
                end_bound = end_bound + datetime.timedelta(days=1) - datetime.timedelta(microseconds=1)
            if value > end_bound:
                return False
        return True
    return True


class MeasuredRepository:
    """One repository of the stand-in: its workflow runs, jobs, plan artifacts, store artifacts and issues."""

    def __init__(self, full_name: str, *, archived: bool = False, has_workflow: bool = True) -> None:
        self.full_name = full_name
        self.default_branch = "main"
        self.archived = archived
        self.has_workflow = has_workflow
        self.runs: list[dict] = []
        self.jobs: dict[int, list[dict]] = {}
        self.attempts: dict[tuple[int, int], dict] = {}
        self.attempt_jobs: dict[tuple[int, int], list[dict]] = {}
        self.artifacts: dict[int, list[dict]] = {}
        self.repo_artifacts: list[dict] = []
        self.artifact_zips: dict[int, bytes] = {}
        self.issues: list[dict] = []
        self.issue_comments: dict[int, list[dict]] = {}
        self._next_run = 1000
        self._next_artifact = 5000

    def _build_jobs(
        self,
        run_id: int,
        created_at: str,
        *,
        plan_completed_at: str | None,
        verdict_completed_at: str | None,
        verdict_status: str,
        verdict_conclusion: str | None,
        verdict_job_name: str | None,
        extra_jobs: list[dict] | None,
        id_offset: int = 0,
    ) -> list[dict]:
        run_jobs: list[dict] = [
            {
                "id": run_id * 100 + id_offset + 1,
                "name": "ci / plan",
                "status": "completed",
                "conclusion": "success",
                "started_at": created_at,
                "completed_at": plan_completed_at or created_at,
            },
        ]
        if extra_jobs:
            for index, extra in enumerate(extra_jobs, start=2):
                run_jobs.append({"id": run_id * 100 + id_offset + index, **extra})
        if verdict_job_name is not None:
            run_jobs.append({
                "id": run_id * 100 + id_offset + 90,
                "name": verdict_job_name,
                "status": verdict_status,
                "conclusion": verdict_conclusion,
                "started_at": verdict_completed_at or created_at,
                "completed_at": verdict_completed_at,
            })
        return run_jobs

    def add_run(
        self,
        created_at: str,
        *,
        verdict_completed_at: str | None = None,
        plan_completed_at: str | None = None,
        status: str = "completed",
        conclusion: str | None = "success",
        verdict_status: str = "completed",
        verdict_conclusion: str | None = "success",
        verdict_job_name: str | None = "ci / verdict",
        event: str = "pull_request",
        run_attempt: int = 1,
        plan: dict | None = None,
        plan_expired: bool = False,
        plan_created_at: str | None = None,
        extra_artifacts: list[tuple[dict, str, bool]] | None = None,
        extra_jobs: list[dict] | None = None,
        attempt_1: dict | None = None,
        workflow: str = "ci.yml",
        updated_at: str | None = None,
        head_branch: str = "main",
    ) -> int:
        run_id = self._next_run
        self._next_run += 1
        run_doc = {
            "id": run_id,
            "path": f".github/workflows/{workflow}",
            "event": event,
            "status": status,
            "conclusion": conclusion,
            "run_attempt": run_attempt,
            "head_branch": head_branch,
            "created_at": created_at,
            "updated_at": updated_at or verdict_completed_at or created_at,
            "html_url": f"https://github.example/{self.full_name}/actions/runs/{run_id}",
        }
        self.runs.append(run_doc)
        latest_jobs = self._build_jobs(
            run_id,
            created_at,
            plan_completed_at=plan_completed_at,
            verdict_completed_at=verdict_completed_at,
            verdict_status=verdict_status,
            verdict_conclusion=verdict_conclusion,
            verdict_job_name=verdict_job_name,
            extra_jobs=extra_jobs,
        )
        self.jobs[run_id] = latest_jobs
        self.attempts[(run_id, run_attempt)] = dict(run_doc)
        self.attempt_jobs[(run_id, run_attempt)] = latest_jobs
        if attempt_1 is not None:
            attempt_status = attempt_1.get("status", "completed")
            attempt_conclusion = attempt_1.get("conclusion", "failure")
            attempt_verdict_at = attempt_1.get("verdict_completed_at")
            self.attempts[(run_id, 1)] = {
                **run_doc,
                "run_attempt": 1,
                "status": attempt_status,
                "conclusion": attempt_conclusion,
                "updated_at": attempt_1.get("updated_at") or attempt_verdict_at or created_at,
            }
            self.attempt_jobs[(run_id, 1)] = self._build_jobs(
                run_id,
                created_at,
                plan_completed_at=attempt_1.get("plan_completed_at"),
                verdict_completed_at=attempt_verdict_at,
                verdict_status=attempt_1.get("verdict_status", "completed"),
                verdict_conclusion=attempt_1.get("verdict_conclusion", attempt_conclusion),
                verdict_job_name=attempt_1.get("verdict_job_name", "ci / verdict"),
                extra_jobs=attempt_1.get("extra_jobs"),
                id_offset=50,
            )
        artifacts: list[dict] = []
        if plan is not None:
            artifact_id = self._next_artifact
            self._next_artifact += 1
            artifacts.append({
                "id": artifact_id,
                "name": "ci-plan",
                "expired": plan_expired,
                "created_at": plan_created_at or created_at,
                "archive_download_url": f"/repos/{self.full_name}/actions/artifacts/{artifact_id}/zip",
                "workflow_run": {"id": run_id, "head_branch": head_branch},
            })
            self.artifact_zips[artifact_id] = plan_zip(plan)
        for extra_plan, art_created_at, art_expired in extra_artifacts or ():
            artifact_id = self._next_artifact
            self._next_artifact += 1
            artifacts.append({
                "id": artifact_id,
                "name": "ci-plan",
                "expired": art_expired,
                "created_at": art_created_at,
                "archive_download_url": f"/repos/{self.full_name}/actions/artifacts/{artifact_id}/zip",
                "workflow_run": {"id": run_id, "head_branch": head_branch},
            })
            self.artifact_zips[artifact_id] = plan_zip(extra_plan)
        self.artifacts[run_id] = artifacts
        return run_id

    def add_store_artifact(
        self,
        payload: dict | str | bytes,
        *,
        created_at: str,
        expired: bool = False,
        head_branch: str = "main",
        name: str = "measurement-records",
        raw_bytes: bool = False,
    ) -> int:
        artifact_id = self._next_artifact
        self._next_artifact += 1
        self.repo_artifacts.append({
            "id": artifact_id,
            "name": name,
            "expired": expired,
            "created_at": created_at,
            "archive_download_url": f"/repos/{self.full_name}/actions/artifacts/{artifact_id}/zip",
            "workflow_run": {"id": artifact_id * 10, "head_branch": head_branch},
        })
        if raw_bytes:
            assert isinstance(payload, bytes)
            self.artifact_zips[artifact_id] = payload
        else:
            self.artifact_zips[artifact_id] = store_zip(payload)
        return artifact_id

    def issue(
        self,
        title: str,
        body: str = "",
        state: str = "open",
        *,
        closed_at: str | None = None,
        updated_at: str | None = None,
    ) -> dict:
        number = 100 + len(self.issues) + 1
        entry = {
            "number": number,
            "title": title,
            "body": body,
            "state": state,
            "closed_at": closed_at,
            "updated_at": updated_at or closed_at or NOW,
            "html_url": f"https://github.example/{self.full_name}/issues/{number}",
        }
        self.issues.append(entry)
        return entry


@dataclass(frozen=True)
class Write:
    method: str
    path: str
    body: dict
    token: str


class MeasurementStandIn:
    """A local HTTP server answering GitHub installation, Actions runs/jobs/artifacts and Issues endpoints."""

    def __init__(self) -> None:
        self.repositories: dict[str, MeasuredRepository] = {}
        self.installed: list[str] = []
        self.installation_total_count: int | None = None
        self.now = NOW
        self.writes: list[Write] = []
        self.requests: list[tuple[str, str, str]] = []
        self.refused: dict[str, int] = {}
        self.created_cap: int | None = 1000
        self.ratelimit_remaining: int | None = 4500
        self.on_request: Callable[[str, str, dict[str, str]], tuple[int, str, bytes, dict[str, str]] | None] | None = None
        self._lock = threading.Lock()
        stand_in = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def answer(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                with stand_in._lock:
                    stand_in.requests.append((method, self.path, token))
                    if method != "GET":
                        stand_in.writes.append(Write(method, self.path, body, token))
                    code, content_type, payload, extra_headers = stand_in.route(method, self.path, body, token)
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                if stand_in.ratelimit_remaining is not None and "X-RateLimit-Remaining" not in extra_headers:
                    self.send_header("X-RateLimit-Remaining", str(stand_in.ratelimit_remaining))
                for header_name, header_value in extra_headers.items():
                    self.send_header(header_name, header_value)
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                self.answer("GET")

            def do_POST(self) -> None:
                self.answer("POST")

            def do_PATCH(self) -> None:
                self.answer("PATCH")

            def log_message(self, *arguments) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def repository(self, full_name: str, *, installed: bool = True, archived: bool = False, has_workflow: bool = True) -> MeasuredRepository:
        repo = MeasuredRepository(full_name, archived=archived, has_workflow=has_workflow)
        self.repositories[full_name] = repo
        if installed and full_name not in self.installed:
            self.installed.append(full_name)
        return repo

    def route(self, method: str, address: str, body: dict, token: str) -> tuple[int, str, bytes, dict[str, str]]:
        parts = urlsplit(address)
        path, query = unquote(parts.path), {name: values[0] for name, values in parse_qs(parts.query).items()}
        if self.on_request is not None:
            intercepted = self.on_request(method, path, query)
            if intercepted is not None:
                return intercepted
        for prefix, code in self.refused.items():
            if path.startswith(prefix):
                return self._json(code, {"message": "refused"})
        if path == "/installation/repositories":
            page, size = int(query.get("page", 1)), int(query.get("per_page", 30))
            names = self.installed[(page - 1) * size:page * size]
            items = [{"full_name": self.repositories[n].full_name, "default_branch": self.repositories[n].default_branch, "archived": self.repositories[n].archived} for n in names]
            total = len(self.installed) if self.installation_total_count is None else self.installation_total_count
            return self._json(200, {"total_count": total, "repositories": items})
        match = re.fullmatch(r"/repos/([^/]+/[^/]+)(?:/(.*))?", path)
        repo = self.repositories.get(match.group(1)) if match else None
        if repo is None:
            return self._json(404, {"message": "Not Found"})
        rest = match.group(2) or ""
        if rest == "":
            return self._json(200, {"full_name": repo.full_name, "default_branch": repo.default_branch, "archived": repo.archived})
        wf_match = re.fullmatch(r"actions/workflows/([^/]+)/runs", rest)
        if wf_match:
            if not repo.has_workflow:
                return self._json(404, {"message": "Not Found"})
            workflow = wf_match.group(1)
            matching = [r for r in repo.runs if r["path"] == f".github/workflows/{workflow}"]
            if "created" in query:
                matching = [r for r in matching if _matches_created_filter(r["created_at"], query["created"])]
            total_count = len(matching)
            page, size = int(query.get("page", 1)), int(query.get("per_page", 30))
            if "created" in query and self.created_cap is not None:
                start = (page - 1) * size
                if start >= self.created_cap:
                    return self._json(200, {"total_count": total_count, "workflow_runs": []})
                matching = matching[:self.created_cap]
            return self._json(200, {"total_count": total_count, "workflow_runs": matching[(page - 1) * size:page * size]})
        run_match = re.fullmatch(r"actions/runs/(\d+)", rest)
        if run_match:
            run_id = int(run_match.group(1))
            for run in repo.runs:
                if run["id"] == run_id:
                    return self._json(200, run)
            return self._json(404, {"message": "Not Found"})
        attempt_match = re.fullmatch(r"actions/runs/(\d+)/attempts/(\d+)", rest)
        if attempt_match:
            run_id, attempt = int(attempt_match.group(1)), int(attempt_match.group(2))
            if (run_id, attempt) not in repo.attempts:
                return self._json(404, {"message": "Not Found"})
            return self._json(200, repo.attempts[(run_id, attempt)])
        attempt_jobs_match = re.fullmatch(r"actions/runs/(\d+)/attempts/(\d+)/jobs", rest)
        if attempt_jobs_match:
            run_id, attempt = int(attempt_jobs_match.group(1)), int(attempt_jobs_match.group(2))
            if (run_id, attempt) not in repo.attempt_jobs:
                return self._json(404, {"message": "Not Found"})
            jobs = repo.attempt_jobs[(run_id, attempt)]
            page, size = int(query.get("page", 1)), int(query.get("per_page", 30))
            return self._json(200, {"total_count": len(jobs), "jobs": jobs[(page - 1) * size:page * size]})
        jobs_match = re.fullmatch(r"actions/runs/(\d+)/jobs", rest)
        if jobs_match:
            run_id = int(jobs_match.group(1))
            if run_id not in repo.jobs:
                return self._json(404, {"message": "Not Found"})
            jobs = repo.jobs[run_id]
            page, size = int(query.get("page", 1)), int(query.get("per_page", 30))
            return self._json(200, {"total_count": len(jobs), "jobs": jobs[(page - 1) * size:page * size]})
        if rest == "actions/artifacts":
            items = list(repo.repo_artifacts)
            for run_artifacts in repo.artifacts.values():
                items.extend(run_artifacts)
            if "name" in query:
                items = [a for a in items if a["name"] == query["name"]]
            page, size = int(query.get("page", 1)), int(query.get("per_page", 30))
            return self._json(200, {"total_count": len(items), "artifacts": items[(page - 1) * size:page * size]})
        artifacts_match = re.fullmatch(r"actions/runs/(\d+)/artifacts", rest)
        if artifacts_match:
            run_id = int(artifacts_match.group(1))
            if run_id not in repo.artifacts:
                return self._json(404, {"message": "Not Found"})
            items = repo.artifacts[run_id]
            if "name" in query:
                items = [a for a in items if a["name"] == query["name"]]
            page, size = int(query.get("page", 1)), int(query.get("per_page", 30))
            return self._json(200, {"total_count": len(items), "artifacts": items[(page - 1) * size:page * size]})
        zip_match = re.fullmatch(r"actions/artifacts/(\d+)/zip", rest)
        if zip_match:
            artifact_id = int(zip_match.group(1))
            if artifact_id not in repo.artifact_zips:
                return self._json(404, {"message": "Not Found"})
            return 200, "application/zip", repo.artifact_zips[artifact_id], {}
        if rest == "issues":
            if method == "POST":
                issue = repo.issue(body["title"], body.get("body", ""), updated_at=self.now)
                issue["user"] = {"login": "github-actions[bot]" if token == RUN_TOKEN else OWNER}
                return self._json(201, issue)
            found = [i for i in repo.issues if query.get("state", "open") in ("all", i["state"])]
            if "since" in query:
                since_dt = _parse_iso(query["since"])
                filtered: list[dict] = []
                for item in found:
                    ts = item.get("updated_at") or item.get("closed_at")
                    if ts is None or _parse_iso(ts) >= since_dt:
                        filtered.append(item)
                found = filtered
            page, size = int(query.get("page", 1)), int(query.get("per_page", 30))
            return self._json(200, found[(page - 1) * size:page * size])
        issue_match = re.fullmatch(r"issues/(\d+)(/comments)?", rest)
        if issue_match:
            number = int(issue_match.group(1))
            issue = next((item for item in repo.issues if item["number"] == number), None)
            if issue is None:
                return self._json(404, {"message": "Not Found"})
            if issue_match.group(2):
                if method == "POST":
                    comments = repo.issue_comments.setdefault(number, [])
                    comment = {"id": len(comments) + 1, "body": body["body"], "created_at": self.now,
                               "user": {"login": "github-actions[bot]" if token == RUN_TOKEN else OWNER}}
                    comments.append(comment)
                    return self._json(201, comment)
                page, size = int(query.get("page", 1)), int(query.get("per_page", 30))
                return self._json(200, repo.issue_comments.get(number, [])[(page - 1) * size:page * size])
            if method == "PATCH":
                if "state" in body and body["state"] != issue["state"]:
                    issue["closed_at"] = self.now if body["state"] == "closed" else None
                issue.update(body, updated_at=self.now)
            return self._json(200, issue)
        return self._json(404, {"message": "Not Found"})

    @staticmethod
    def _json(code: int, document: object, headers: dict[str, str] | None = None) -> tuple[int, str, bytes, dict[str, str]]:
        return code, "application/json", json.dumps(document).encode("utf-8"), headers or {}


@dataclass(frozen=True)
class Outcome:
    status: int
    stdout: str
    stderr: str
    summary: str
    report: dict | None


class MeasurementFixture(unittest.TestCase):
    """Base test case providing a GitHub API stand-in and invocation helper for ``measurement.py``."""

    def setUp(self) -> None:
        self.stand_in = MeasurementStandIn()
        self.addCleanup(self.stand_in.close)
        directory = temporary_support.Directory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.management = self.stand_in.repository(MANAGEMENT, installed=False, has_workflow=False)

    def run_measurement(self, *arguments: str, tokens: dict[str, str] | None = None) -> Outcome:
        if str(MEASUREMENT_FOLDER) not in sys.path:
            sys.path.insert(0, str(MEASUREMENT_FOLDER))
        import measurement as measurement_program

        summary_file = self.directory / "summary.md"
        report_file = self.directory / "report.json"
        for f in (summary_file, report_file):
            if f.exists():
                f.unlink()
        subcommand = arguments[0] if arguments else ""
        argv = [*arguments, "--summary", str(summary_file)]
        if subcommand in ("measure", "failure", "success"):
            argv += ["--api-url", self.stand_in.url]
        if subcommand in ("measure", "report"):
            argv += ["--json-output", str(report_file)]
        if subcommand == "measure":
            if "--now" not in arguments:
                argv += ["--now", NOW]
            if "--max-rps" not in arguments:
                argv += ["--max-rps", "0"]
            if "--min-secondary-wait" not in arguments:
                argv += ["--min-secondary-wait", "0"]
        env = {"GH_TOKEN": APP_TOKEN, "GITHUB_TOKEN": RUN_TOKEN, "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        if tokens is not None:
            env.update(tokens)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(stdout), redirect_stderr(stderr):
            status = measurement_program.main(argv)
        summary = summary_file.read_text(encoding="utf-8") if summary_file.is_file() else ""
        report = json.loads(report_file.read_text(encoding="utf-8")) if report_file.is_file() else None
        return Outcome(status, stdout.getvalue(), stderr.getvalue(), summary, report)
