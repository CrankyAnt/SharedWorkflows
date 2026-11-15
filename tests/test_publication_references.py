"""The reference check fails on a reference to a repository of the owner that is not in the owner's public list."""

from __future__ import annotations

import http.server
import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tests.publication_fixtures import PublicationFixture, REPOSITORY_ROOT

CHECKER = REPOSITORY_ROOT / "actions" / "publication-checks" / "check-references.cjs"
OWNER = "example-owner"
LIST_PATH = f"/users/{OWNER}/repos"
NOT_PUBLIC = "reference to a repository of the owner that is not public"


class PublicRepositoryList:
    """A local HTTP server that answers the owner's public repository list in pages, as GitHub does without a login,
    and records the path and the headers of every request."""

    def __init__(self, names: list[str], status: int = 200, body: bytes | None = None) -> None:
        self.names = names
        self.requests: list[tuple[str, dict[str, str]]] = []
        stand_in = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                stand_in.requests.append((self.path, {name.lower(): value for name, value in self.headers.items()}))
                address = urlsplit(self.path)
                query = parse_qs(address.query)
                payload = body
                if payload is None:
                    size = int(query.get("per_page", ["30"])[0])
                    page = int(query.get("page", ["1"])[0])
                    listed = stand_in.names[(page - 1) * size:page * size] if address.path == LIST_PATH else []
                    payload = json.dumps([{"name": name, "private": False} for name in listed]).encode()
                self.send_response(status if address.path == LIST_PATH else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *arguments) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class PublicationReferenceTests(PublicationFixture):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.repository = self.directory / "repository"
        self._initialize_repository(self.repository)
        self._commit(self.repository, "feat: create baseline")
        self.base = self._head(self.repository)

    def _list(self, names: list[str], **options) -> PublicRepositoryList:
        stand_in = PublicRepositoryList(names, **options)
        self.addCleanup(stand_in.close)
        return stand_in

    def _check(self, api_url: str, title: str = "feat: add a check", body: str | None = "", repository: dict | None = None, **extra: str) -> subprocess.CompletedProcess[str]:
        event = self.directory / "event.json"
        document = {"pull_request": {"title": title, "body": body}, "repository": {"private": False} if repository is None else repository}
        event.write_text(json.dumps(document), encoding="utf-8")
        environment = self.environment.copy()
        for name in ("GITHUB_TOKEN", "GH_TOKEN"):
            environment.pop(name, None)
        environment.update(GITHUB_API_URL=api_url, GITHUB_REPOSITORY_OWNER=OWNER, GITHUB_EVENT_PATH=str(event))
        environment.update(extra)
        return subprocess.run(["node", str(CHECKER), self.base, "HEAD"], capture_output=True, text=True, env=environment, cwd=self.repository)

    def test_a_commit_message_that_references_a_repository_outside_the_public_list_fails_by_position(self) -> None:
        api = self._list(["PublicTool"])
        self._commit(self.repository, f"fix: repair the loader\n\nThe caller follows\nthe one of {OWNER}/HiddenProject today.")
        commit = self._head(self.repository)
        self._run_git(self.repository, "checkout", "-b", "topic")
        self._commit(self.repository, "docs: describe the loader")
        self._run_git(self.repository, "checkout", "main")
        self._commit(self.repository, "docs: describe the caller")
        self._run_git(self.repository, "merge", "--no-ff", "topic", "-m", f"Merge topic of {OWNER}/HiddenProject")
        merge = self._head(self.repository)
        result = self._check(api.url)
        output = result.stdout + result.stderr
        self.assertEqual(1, result.returncode, output)
        self.assertIn(f"commit {commit}, line 4, column 12: {NOT_PUBLIC}", output)
        self.assertIn(f"commit {merge}, line 1, column 16: {NOT_PUBLIC}", output, "a merge commit's message is read too")
        self.assertNotIn("hiddenproject", output.lower(), "the log does not repeat the reference")

    def test_a_pull_request_title_or_body_that_references_a_repository_outside_the_public_list_fails_by_position(self) -> None:
        api = self._list(["PublicTool"])
        result = self._check(api.url, title=f"feat: follow {OWNER}/HiddenProject")
        output = result.stdout + result.stderr
        self.assertEqual(1, result.returncode, output)
        self.assertIn(f"pull request title, line 1, column 14: {NOT_PUBLIC}", output)
        result = self._check(api.url, body=f"## Summary\r\n\r\nSee https://github.com/{OWNER.upper()}/hiddenproject/issues/3 and {OWNER}/Other-Hidden.Thing.")
        output = result.stdout + result.stderr
        self.assertEqual(1, result.returncode, output)
        self.assertIn(f"pull request body, line 3, column 24: {NOT_PUBLIC}", output)
        self.assertIn(f"pull request body, line 3, column 65: {NOT_PUBLIC}", output)
        for name in ("hiddenproject", "other-hidden"):
            self.assertNotIn(name, output.lower(), "the log does not repeat the reference")

    def test_references_to_public_repositories_of_the_owner_and_to_other_owners_pass(self) -> None:
        api = self._list(["PublicTool", "Second.Tool"])
        self._commit(self.repository, f"fix: repair the loader\n\nAs {OWNER}/PublicTool does, and other-owner/HiddenProject, and not-{OWNER}/HiddenProject.")
        body = "\n".join([
            f"Uses {OWNER}/publictool/.github/workflows/check.yml@v1 and {OWNER.upper()}/PublicTool#12.",
            f"Clone https://github.com/{OWNER}/PublicTool.git, read {OWNER}/Second.Tool, then {OWNER}/PublicTool.",
            f"A path such as {OWNER}/ or {OWNER}/... names no repository.",
        ])
        result = self._check(api.url, title=f"feat: follow {OWNER}/PublicTool", body=body)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        result = self._check(api.url, body=None)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_the_public_list_is_read_in_pages_without_credentials(self) -> None:
        api = self._list([f"filler-{index}" for index in range(100)] + ["PublicTool"])
        result = self._check(api.url, body=f"See {OWNER}/PublicTool.", GITHUB_TOKEN="token-for-the-test", GH_TOKEN="token-for-the-test")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual([LIST_PATH, LIST_PATH], [urlsplit(path).path for path, _ in api.requests])
        self.assertEqual(["1", "2"], [parse_qs(urlsplit(path).query)["page"][0] for path, _ in api.requests])
        for _, headers in api.requests:
            self.assertNotIn("authorization", headers, "the list is read without a login")
            self.assertNotIn("cookie", headers)

    def test_texts_without_a_reference_to_the_owner_pass_without_reading_the_list(self) -> None:
        api = self._list([], status=500)
        self._commit(self.repository, "fix: repair the loader\n\nAs other-owner/HiddenProject does.")
        result = self._check(api.url, body="No reference here.")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual([], api.requests)

    def test_a_list_that_cannot_be_read_fails_with_a_clear_message(self) -> None:
        body = f"See {OWNER}/PublicTool."
        message = "the list of public repositories of the owner cannot be read"
        refused = self._list([])
        refused.close()
        cases = [
            ("rate limited", self._list(["PublicTool"], status=403, body=b'{"message": "API rate limit exceeded"}').url, "HTTP 403"),
            ("no JSON", self._list([], body=b"<html>").url, "not the expected JSON"),
            ("no list", self._list([], body=b'{"name": "PublicTool"}').url, "not the expected JSON"),
            ("unreachable", refused.url, "request failed"),
        ]
        for name, url, reason in cases:
            with self.subTest(name):
                result = self._check(url, body=body)
                output = result.stdout + result.stderr
                self.assertEqual(1, result.returncode, output)
                self.assertIn(message, output)
                self.assertIn(reason, output)
                self.assertNotIn(NOT_PUBLIC, output, "an unread list is no finding about a reference")
                self.assertNotIn("publictool", output.lower())

    def test_a_private_repository_skips_the_check_with_a_message_and_any_other_repository_runs_it(self) -> None:
        api = self._list(["PublicTool"])
        self._commit(self.repository, f"fix: repair the loader\n\nAs {OWNER}/HiddenProject does.")
        body = f"See {OWNER}/HiddenProject."
        result = self._check(api.url, title=f"feat: follow {OWNER}/HiddenProject", body=body, repository={"private": True})
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("Skipping the reference check: this repository is private, so its texts are not public.", result.stdout)
        self.assertEqual([], api.requests, "a private repository does not read the list")
        for name, repository in (("public", {"private": False}), ("visibility unknown", {}), ("not a boolean", {"private": "true"})):
            with self.subTest(name):
                result = self._check(api.url, body=body, repository=repository)
                output = result.stdout + result.stderr
                self.assertEqual(1, result.returncode, output)
                self.assertIn(f"pull request body, line 1, column 5: {NOT_PUBLIC}", output)
                self.assertNotIn("Skipping", output)

    def test_a_run_without_the_pull_request_or_the_owner_fails(self) -> None:
        api = self._list(["PublicTool"])
        result = self._check(api.url, GITHUB_REPOSITORY_OWNER="")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("GITHUB_REPOSITORY_OWNER", result.stderr)
        result = self._check(api.url, GITHUB_EVENT_PATH=str(self.directory / "absent.json"))
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("pull request event", result.stderr)


if __name__ == "__main__":
    unittest.main()
