"""A fixture consumer with release tags, SSH signing keys and a stand-in for the GitHub API, for the tests of the release
program of the part ``github-release``."""

from __future__ import annotations

import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from tests.pipeline_fixtures import CONFIGURATION, PART_FOLDER, SCHEMAS, PipelineFixture, git, read_outputs

SCRIPT = PART_FOLDER / "release.py"
import release  # noqa: E402  (tests.pipeline_fixtures puts the part folder on the module path)

PART = "github-release"
CONSUMER = "example-owner/consumer"
SIGNERS = ".github/allowed_signers"
TOKEN = "fixture-token"
# What a test needs to make a signed tag: the tests that sign skip without it.
SSH_KEYGEN = shutil.which("ssh-keygen")


def configuration(settings: str = "", tags: str = '["v*"]', files: str = "[dist/tool.zip]") -> str:
    """A configuration file with the release part; ``settings`` holds further lines of the part, each indented four spaces."""
    return f"version: 1\nparts:\n  {PART}:\n    tags: {tags}\n    allowed-signers: {SIGNERS}\n    files: {files}\n{settings}"


@dataclass(frozen=True)
class Outcome:
    status: int
    stdout: str
    stderr: str
    outputs: dict[str, str]
    summary: str

    @property
    def part(self) -> dict:
        return json.loads(self.outputs["plan"])["parts"][PART]


@dataclass
class Request:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes = b""
    query: dict = field(default_factory=dict)

    @property
    def document(self) -> dict:
        return json.loads(self.body.decode("utf-8"))


class GitHubStandIn:
    """A local HTTP server that answers the release requests of the fixture consumer as the GitHub API does, keeps the
    releases and their assets, and records every request."""

    def __init__(self) -> None:
        self.releases: dict[int, dict] = {}
        self.assets: dict[int, dict[str, bytes]] = {}
        self.requests: list[Request] = []
        self.refuse: dict[tuple[str, str], int] = {}  # (method, start of the path) -> status
        stand_in = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def answer(self, code: int, document: object) -> None:
                payload = json.dumps(document).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def handle_request(self) -> None:
                address = urlsplit(self.path)
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                request = Request(self.command, address.path, {name.lower(): value for name, value in self.headers.items()}, body, parse_qs(address.query))
                stand_in.requests.append(request)
                for (method, start), status in stand_in.refuse.items():
                    if method == self.command and address.path.startswith(start):
                        return self.answer(status, {"message": "refused"})
                prefix = f"/repos/{CONSUMER}/releases"
                upload = f"/upload/repos/{CONSUMER}/releases/"
                if self.command == "GET" and address.path.startswith(prefix + "/tags/"):
                    tag = address.path.removeprefix(prefix + "/tags/")
                    found = [item for item in stand_in.releases.values() if item["tag_name"] == tag and not item["draft"]]
                    return self.answer(200, found[0]) if found else self.answer(404, {"message": "Not Found"})
                if self.command == "POST" and address.path == prefix:
                    number = len(stand_in.releases) + 1
                    stand_in.releases[number] = {"id": number, **request.document, "html_url": f"https://github.invalid/{CONSUMER}/releases/{number}",
                                                 "upload_url": f"{stand_in.url}{upload}{number}/assets{{?name,label}}"}
                    stand_in.assets[number] = {}
                    return self.answer(201, stand_in.releases[number])
                if self.command == "POST" and address.path.startswith(upload):
                    number = int(address.path.removeprefix(upload).split("/")[0])
                    name = request.query["name"][0]
                    stand_in.assets[number][name] = body
                    return self.answer(201, {"name": name, "size": len(body)})
                if self.command == "PATCH" and address.path.startswith(prefix + "/"):
                    number = int(address.path.removeprefix(prefix + "/"))
                    stand_in.releases[number].update(request.document)
                    return self.answer(200, stand_in.releases[number])
                return self.answer(404, {"message": "Not Found"})

            do_GET = do_POST = do_PATCH = handle_request

            def log_message(self, *arguments) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def published(self) -> list[dict]:
        return [item for item in self.releases.values() if not item["draft"]]


class ReleaseFixture(PipelineFixture):
    """A fixture consumer whose default branch ``main`` is also its remote-tracking branch ``origin/main``, as a checkout
    with the full history has it, checked against the real schema."""

    def setUp(self) -> None:
        super().setUp()
        self.schemas = SCHEMAS
        self.keys = self.directory / "keys"
        self.keys.mkdir()

    def key(self, name: str = "maintainer") -> tuple[Path, str]:
        """A new SSH key pair: the path of its private key and its line for an allowed-signers file."""
        path = self.keys / name
        subprocess.run([SSH_KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(path)], check=True, capture_output=True)
        kind, public = path.with_suffix(".pub").read_text(encoding="utf-8").split()[:2]
        return path, f"{name}@example.invalid {kind} {public}\n"

    def repository_head(self) -> str:
        return git(self.repository, "rev-parse", "HEAD")

    def publish_main(self) -> None:
        """Make the default branch's tip what the checkout holds as ``origin/main``."""
        git(self.repository, "update-ref", "refs/remotes/origin/main", "refs/heads/main")

    def tag(self, name: str, key: Path | None = None, annotated: bool = True, commit: str = "HEAD") -> None:
        """A tag on the commit: signed with the key, annotated without a signature, or lightweight."""
        if key is not None:
            git(self.repository, "-c", "gpg.format=ssh", "-c", f"user.signingkey={key.as_posix()}", "tag", "-s", "-m", f"Release {name}", name, commit)
        elif annotated:
            git(self.repository, "-c", "tag.gpgSign=false", "tag", "-a", "-m", f"Release {name}", name, commit)
        else:
            git(self.repository, "tag", name, commit)

    def arguments(self, command: str, *arguments: str) -> tuple[list[str], Path, Path]:
        outputs, summary = self.directory / "github-output", self.directory / "summary.md"
        for path in (outputs, summary):
            path.unlink(missing_ok=True)
        options = ["--root", str(self.repository), "--schemas", str(self.schemas), "--github-output", str(outputs)] if command == "plan" else []
        return [command, *options, "--summary", str(summary), *arguments], outputs, summary

    def run_program(self, command: str, *arguments: str) -> Outcome:
        """Run a command of the release program in this process, through its command line."""
        argv, outputs, summary = self.arguments(command, *arguments)
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr), mock.patch.dict(os.environ, {"GITHUB_TOKEN": TOKEN}):
            status = release.main(argv)
        return Outcome(status, stdout.getvalue(), stderr.getvalue(), read_outputs(outputs), summary.read_text(encoding="utf-8") if summary.exists() else "")

    def start_program(self, command: str, *arguments: str) -> Outcome:
        """Start the release program as a process of its own."""
        argv, outputs, summary = self.arguments(command, *arguments)
        completed = subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True, encoding="utf-8", env={**os.environ, "GITHUB_TOKEN": TOKEN})
        return Outcome(completed.returncode, completed.stdout, completed.stderr, read_outputs(outputs), summary.read_text(encoding="utf-8") if summary.exists() else "")

    def plan(self, ref: str, *arguments: str) -> Outcome:
        return self.run_program("plan", "--ref", ref, *arguments)


__all__ = ["CONFIGURATION", "CONSUMER", "PART", "SIGNERS", "SSH_KEYGEN", "TOKEN", "GitHubStandIn", "Outcome", "ReleaseFixture", "configuration", "release"]
