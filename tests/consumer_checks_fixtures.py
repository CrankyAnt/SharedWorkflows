"""A fixture consumer with fixed callers, a fixture consumer kit with invented templates and a stand-in for the GitHub
API, for the tests of the consumer checks of the pipeline's hygiene job."""

from __future__ import annotations

import http.server
import io
import json
import os
import subprocess
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tests.pipeline_fixtures import CONFIGURATION, PART_FOLDER, PipelineFixture, write

SCRIPT = PART_FOLDER / "consumer_checks.py"
import consumer_checks  # noqa: E402  (tests.pipeline_fixtures puts the part folder on the module path)

SHARED = "example-shared/workflows"
CONSUMER = "example-owner/consumer"
SYNC_BRANCH = "chore/invented-sync"
CALLERS = ("ci.yml", "automation.yml", "release.yml")
WORKFLOWS = ".github/workflows"


def template(name: str, note: str = "") -> str:
    """An invented template of a fixed caller; ``note`` makes another version of it."""
    stem = name.removesuffix(".yml")
    shared = "pipeline.yml" if name == "ci.yml" else name
    return f"name: {stem}{note}\non:\n  push:\npermissions: {{}}\njobs:\n  {stem}:\n    permissions:\n      contents: read\n    uses: {SHARED}/.github/workflows/{shared}@v1\n"


@dataclass(frozen=True)
class Checked:
    status: int
    stdout: str
    stderr: str
    summary: str


class GitHubStandIn:
    """A local HTTP server that answers the issues and the open pull requests of the fixture consumer as the GitHub API
    does, and records the path and the headers of every request."""

    def __init__(self) -> None:
        self.issues: dict[int, dict] = {}
        self.pull_requests: list[dict] = []
        self.status: int | None = None
        self.requests: list[tuple[str, dict[str, str]]] = []
        stand_in = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                stand_in.requests.append((self.path, {name.lower(): value for name, value in self.headers.items()}))
                address = urlsplit(self.path)
                query = parse_qs(address.query)
                code, document = 404, {"message": "Not Found"}
                prefix = f"/repos/{CONSUMER}/"
                if stand_in.status is not None:
                    code, document = stand_in.status, {"message": "refused"}
                elif address.path.startswith(prefix + "issues/"):
                    number = address.path.removeprefix(prefix + "issues/")
                    if number.isdigit() and int(number) in stand_in.issues:
                        code, document = 200, {"number": int(number), **stand_in.issues[int(number)]}
                elif address.path == prefix + "pulls":
                    head = query.get("head", [""])[0]
                    code, document = 200, [pull for pull in stand_in.pull_requests if query.get("state") == ["open"] and f"{CONSUMER.split('/')[0]}:{pull['head']['ref']}" == head]
                payload = json.dumps(document).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *arguments) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def issue(self, number: int, state: str = "open", pull_request: bool = False, reason: str | None = None) -> None:
        """An issue of the consumer; ``reason`` is GitHub's ``state_reason``, such as ``not_planned`` for a closed one."""
        self.issues[number] = {"state": state, "state_reason": reason, **({"pull_request": {"url": "x"}} if pull_request else {})}

    def sync_pull_request(self, number: int, branch: str = SYNC_BRANCH) -> str:
        url = f"https://github.example/{CONSUMER}/pull/{number}"
        self.pull_requests.append({"number": number, "html_url": url, "head": {"ref": branch}})
        return url

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class ConsumerFixture(PipelineFixture):
    """A fixture consumer that holds the three fixed callers of a fixture kit and the smallest configuration file."""

    def setUp(self) -> None:
        super().setUp()
        self.kit = self.directory / "kit"
        for name in CALLERS:
            write(self.kit, name, template(name))
            write(self.repository, f"{WORKFLOWS}/{name}", template(name))
        self.record(previous={})
        write(self.repository, CONFIGURATION, "version: 1\n")
        self.api = GitHubStandIn()
        self.addCleanup(self.api.close)

    def record(self, previous: dict[str, str]) -> None:
        """The record of the kit: per caller with a previous version the date of the promotion that changed it."""
        write(self.kit, "fixed-callers.json", json.dumps({"previous": previous, "sync-branch": SYNC_BRANCH}))

    def previous(self, name: str, promoted: str) -> str:
        """Give the kit a previous version of a caller, changed by a promotion on ``promoted``, and return its text."""
        text = template(name, " previous")
        write(self.kit, f"previous/{name}", text)
        self.record({name: promoted})
        return text

    def consumer(self, files: dict[str, str | None]) -> None:
        for relative, text in files.items():
            if text is None:
                (self.repository / relative).unlink()
            else:
                write(self.repository, relative, text)

    def arguments_of(self, command: str, *arguments: str, event: str = "pull_request", author: str = "example-author", repository: str = CONSUMER) -> tuple[list[str], Path]:
        summary = self.directory / "summary.md"
        summary.unlink(missing_ok=True)
        options = ["--root", str(self.repository), "--schemas", str(self.schemas), "--event", event, "--author", author,
                   "--repository", repository, "--api-url", self.api.url, "--summary", str(summary), "--kit", str(self.kit)]
        return [command, *options, *arguments], summary

    def check(self, command: str, *arguments: str, event: str = "pull_request", author: str = "example-author", repository: str = CONSUMER,
              token: str | None = "token-for-the-test") -> Checked:
        """Run a command of the consumer checks in this process, through its command line."""
        argv, summary = self.arguments_of(command, *arguments, event=event, author=author, repository=repository)
        stdout, stderr = io.StringIO(), io.StringIO()
        environment = {name: value for name, value in os.environ.items() if name != "GITHUB_TOKEN"}
        if token is not None:
            environment["GITHUB_TOKEN"] = token
        with mock.patch.dict(os.environ, environment, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
            status = consumer_checks.main(argv)
        return Checked(status, stdout.getvalue(), stderr.getvalue(), summary.read_text(encoding="utf-8") if summary.exists() else "")

    def start(self, command: str, *arguments: str, event: str = "pull_request", author: str = "example-author") -> Checked:
        """Start the consumer checks as a process of their own."""
        argv, summary = self.arguments_of(command, *arguments, event=event, author=author)
        environment = {**os.environ, "GITHUB_TOKEN": "token-for-the-test"}
        completed = subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True, encoding="utf-8", env=environment)
        return Checked(completed.returncode, completed.stdout, completed.stderr, summary.read_text(encoding="utf-8") if summary.exists() else "")
