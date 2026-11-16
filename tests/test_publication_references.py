"""The reference check fails on a reference to a repository of the owner that is not in the owner's public list, in the
commit messages and in the pull request's title and body as they are now."""

from __future__ import annotations

import http.server
import json
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tests.publication_fixtures import PublicationFixture, REPOSITORY_ROOT

CHECKER = REPOSITORY_ROOT / "actions" / "publication-checks" / "check-references.cjs"
OWNER = "example-owner"
REPOSITORY = f"{OWNER}/fixture"
NUMBER = 7
LIST_PATH = f"/users/{OWNER}/repos"
PULL_PATH = f"/repos/{REPOSITORY}/pulls/{NUMBER}"
NOT_PUBLIC = "reference to a repository of the owner that is not public"


class PublicRepositoryList:
    """A local HTTP server that answers the owner's repository list in pages and the pull request as it is now, and
    records the path and the headers of every request. A name is a public repository; a dict is a list entry as given.
    ``status`` and ``body`` replace the answer of the list, ``pull_status`` and ``pull_body`` that of the pull request.
    ``refusals`` are answers ``(status, headers, body)`` that the list gives, one per request and in order, before it
    answers as usual."""

    def __init__(self, names: list[str | dict], status: int = 200, body: bytes | None = None, pull_status: int = 200,
                 pull_body: bytes | None = None, refusals: list[tuple[int, dict[str, str], bytes]] | None = None) -> None:
        self.names = names
        self.refusals = list(refusals or [])
        self.pull_request: dict = {"number": NUMBER, "title": "", "body": ""}
        self.requests: list[tuple[str, dict[str, str]]] = []
        stand_in = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                stand_in.requests.append((self.path, {name.lower(): value for name, value in self.headers.items()}))
                address = urlsplit(self.path)
                query = parse_qs(address.query)
                headers: dict[str, str] = {}
                if address.path == PULL_PATH:
                    code, payload = pull_status, pull_body if pull_body is not None else json.dumps(stand_in.pull_request).encode()
                elif address.path == LIST_PATH and stand_in.refusals:
                    code, headers, payload = stand_in.refusals.pop(0)
                elif address.path == LIST_PATH:
                    code, payload = status, body
                    if payload is None:
                        size = int(query.get("per_page", ["30"])[0])
                        page = int(query.get("page", ["1"])[0])
                        entries = [name if isinstance(name, dict) else {"name": name, "private": False, "visibility": "public"}
                                   for name in stand_in.names[(page - 1) * size:page * size]]
                        payload = json.dumps(entries).encode()
                else:
                    code, payload = 404, b"{}"
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *arguments) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def requests_of(self, path: str) -> list[tuple[str, dict[str, str]]]:
        return [(requested, headers) for requested, headers in self.requests if urlsplit(requested).path == path]

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

    def _check(self, api: PublicRepositoryList, title: str = "feat: add a check", body: str | None = "", repository: dict | None = None,
               event_title: str = "feat: the title when the run started", event_body: str | None = "The body when the run started.",
               **extra: str) -> subprocess.CompletedProcess[str]:
        """Run the check on a pull request whose title and body are ``title`` and ``body`` now, and whose event still
        holds ``event_title`` and ``event_body``."""
        api.pull_request = {"number": NUMBER, "title": title, "body": body}
        event = self.directory / "event.json"
        document = {"number": NUMBER, "pull_request": {"number": NUMBER, "title": event_title, "body": event_body},
                    "repository": {"private": False} if repository is None else repository}
        event.write_text(json.dumps(document), encoding="utf-8")
        environment = self.environment.copy()
        for name in ("GITHUB_TOKEN", "GH_TOKEN"):
            environment.pop(name, None)
        environment.update(GITHUB_API_URL=api.url, GITHUB_REPOSITORY_OWNER=OWNER, GITHUB_REPOSITORY=REPOSITORY, GITHUB_EVENT_PATH=str(event))
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
        result = self._check(api)
        output = result.stdout + result.stderr
        self.assertEqual(1, result.returncode, output)
        self.assertIn(f"commit {commit}, line 4, column 12: {NOT_PUBLIC}", output)
        self.assertIn(f"commit {merge}, line 1, column 16: {NOT_PUBLIC}", output, "a merge commit's message is read too")
        self.assertNotIn("hiddenproject", output.lower(), "the log does not repeat the reference")

    def test_a_pull_request_title_or_body_that_references_a_repository_outside_the_public_list_fails_by_position(self) -> None:
        api = self._list(["PublicTool"])
        result = self._check(api, title=f"feat: follow {OWNER}/HiddenProject")
        output = result.stdout + result.stderr
        self.assertEqual(1, result.returncode, output)
        self.assertIn(f"pull request title, line 1, column 14: {NOT_PUBLIC}", output)
        result = self._check(api, body=f"## Summary\r\n\r\nSee https://github.com/{OWNER.upper()}/hiddenproject/issues/3 and {OWNER}/Other-Hidden.Thing.")
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
        result = self._check(api, title=f"feat: follow {OWNER}/PublicTool", body=body)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        result = self._check(api, body=None)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_the_list_is_read_in_pages_with_the_job_s_token(self) -> None:
        api = self._list([f"filler-{index}" for index in range(100)] + ["PublicTool"])
        result = self._check(api, body=f"See {OWNER}/PublicTool.", GITHUB_TOKEN="token-for-the-test")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        listing = api.requests_of(LIST_PATH)
        self.assertEqual(["1", "2"], [parse_qs(urlsplit(path).query)["page"][0] for path, _ in listing])
        for _, headers in listing:
            self.assertEqual("Bearer token-for-the-test", headers.get("authorization"))
            self.assertNotIn("cookie", headers)

    def test_without_the_job_s_token_the_list_is_read_without_credentials(self) -> None:
        api = self._list(["PublicTool"])
        result = self._check(api, body=f"See {OWNER}/PublicTool.", GH_TOKEN="token-for-the-test")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        listing = api.requests_of(LIST_PATH)
        self.assertEqual(1, len(listing))
        for _, headers in listing:
            self.assertNotIn("authorization", headers, "without GITHUB_TOKEN the list is read without a login")
            self.assertNotIn("cookie", headers)

    def test_only_an_entry_that_is_not_private_and_has_public_visibility_counts_as_public(self) -> None:
        api = self._list([
            "PublicTool",
            {"name": "NoVisibility", "private": False},
            {"name": "PrivateTool", "private": True, "visibility": "private"},
            {"name": "PrivateButPublicVisibility", "private": True, "visibility": "public"},
            {"name": "InternalTool", "private": False, "visibility": "internal"},
            {"name": "NoPrivateField", "visibility": "public"},
        ])
        for name in ("PublicTool", "NoVisibility"):
            with self.subTest(name):
                result = self._check(api, body=f"See {OWNER}/{name}.", GITHUB_TOKEN="token-for-the-test")
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        for name in ("PrivateTool", "PrivateButPublicVisibility", "InternalTool", "NoPrivateField"):
            with self.subTest(name):
                result = self._check(api, body=f"See {OWNER}/{name}.", GITHUB_TOKEN="token-for-the-test")
                output = result.stdout + result.stderr
                self.assertEqual(1, result.returncode, output)
                self.assertIn(f"pull request body, line 1, column 5: {NOT_PUBLIC}", output)

    def test_a_refused_page_is_requested_again_and_then_passes(self) -> None:
        for status in (403, 429):
            with self.subTest(status):
                api = self._list(["PublicTool"], refusals=[(status, {"retry-after": "0"}, b'{"message": "secondary rate limit"}')])
                result = self._check(api, body=f"See {OWNER}/PublicTool.")
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)
                listing = api.requests_of(LIST_PATH)
                self.assertEqual(2, len(listing))

    def test_a_page_refused_on_every_attempt_fails_after_three_requests(self) -> None:
        message = "the list of public repositories of the owner cannot be read"
        cases = [
            ("retry-after", 403, {"retry-after": "0"}),
            ("reset", 429, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"}),
            ("no hint", 403, {}),
        ]
        for name, status, headers in cases:
            with self.subTest(name):
                refusal = (status, headers, b'{"message": "API rate limit exceeded"}')
                api = self._list(["PublicTool"], refusals=[refusal] * 3)
                started = time.monotonic()
                result = self._check(api, body=f"See {OWNER}/PublicTool.")
                elapsed = time.monotonic() - started
                output = result.stdout + result.stderr
                self.assertEqual(1, result.returncode, output)
                self.assertIn(message, output)
                self.assertIn(f"HTTP {status}", output)
                self.assertEqual(3, len(api.requests_of(LIST_PATH)))
                self.assertLess(elapsed, 10, "a short retry stays short")

    def test_a_required_wait_beyond_sixty_seconds_fails_without_a_further_request(self) -> None:
        cases = [
            ("retry-after", {"retry-after": "61"}),
            ("reset", {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(time.time()) + 3600)}),
        ]
        for name, headers in cases:
            with self.subTest(name):
                api = self._list(["PublicTool"], refusals=[(403, headers, b'{"message": "API rate limit exceeded"}')])
                started = time.monotonic()
                result = self._check(api, body=f"See {OWNER}/PublicTool.")
                output = result.stdout + result.stderr
                self.assertEqual(1, result.returncode, output)
                self.assertIn("the list of public repositories of the owner cannot be read: HTTP 403", output)
                self.assertEqual(1, len(api.requests_of(LIST_PATH)))
                self.assertLess(time.monotonic() - started, 10)

    def test_another_error_status_is_not_requested_again(self) -> None:
        api = self._list(["PublicTool"], refusals=[(500, {"retry-after": "0"}, b'{"message": "Server Error"}')])
        result = self._check(api, body=f"See {OWNER}/PublicTool.")
        output = result.stdout + result.stderr
        self.assertEqual(1, result.returncode, output)
        self.assertIn("the list of public repositories of the owner cannot be read: HTTP 500", output)
        self.assertEqual(1, len(api.requests_of(LIST_PATH)))

    def test_a_failed_read_logs_github_s_message_and_the_rate_limit_headers_without_the_token_or_the_texts(self) -> None:
        headers = {
            "x-ratelimit-limit": "60",
            "x-ratelimit-remaining": "0",
            "x-ratelimit-used": "60",
            "x-ratelimit-reset": str(int(time.time()) + 3600),
            "x-ratelimit-resource": "core",
            "retry-after": "120",
        }
        api = self._list(["PublicTool"], refusals=[(403, headers, b'{"message": "API rate limit exceeded for 203.0.113.7."}')])
        self._commit(self.repository, f"fix: repair the loader\n\nAs {OWNER}/HiddenProject does.")
        result = self._check(api, title=f"feat: follow {OWNER}/PublicTool", body=f"See {OWNER}/SecretTool.", GITHUB_TOKEN="token-for-the-test")
        output = result.stdout + result.stderr
        self.assertEqual(1, result.returncode, output)
        self.assertIn("HTTP 403", output)
        self.assertIn("API rate limit exceeded for 203.0.113.7.", output)
        for name, value in headers.items():
            self.assertIn(f"{name}: {value}", output)
        self.assertNotIn("token-for-the-test", output)
        for text in ("hiddenproject", "publictool", "secrettool", "repair the loader"):
            self.assertNotIn(text, output.lower())

    def test_a_failed_read_logs_only_the_headers_the_answer_carried(self) -> None:
        api = self._list(["PublicTool"], refusals=[(500, {"x-ratelimit-remaining": "42"}, b"<html>")])
        result = self._check(api, body=f"See {OWNER}/PublicTool.")
        output = result.stdout + result.stderr
        self.assertEqual(1, result.returncode, output)
        self.assertIn("x-ratelimit-remaining: 42", output)
        for name in ("x-ratelimit-limit", "x-ratelimit-used", "x-ratelimit-reset", "x-ratelimit-resource", "retry-after"):
            self.assertNotIn(name, output)

    def test_the_title_and_the_body_are_read_as_they_are_now_with_the_job_s_token(self) -> None:
        api = self._list(["PublicTool"])
        stale = f"feat: follow {OWNER}/HiddenProject"
        result = self._check(api, title="feat: follow the public tool", body="Corrected.", event_title=stale, event_body=f"See {OWNER}/HiddenProject.",
                             GITHUB_TOKEN="token-for-the-test")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual([PULL_PATH], [urlsplit(path).path for path, _ in api.requests], "a corrected pull request passes on a re-run")
        self.assertEqual("Bearer token-for-the-test", api.requests[0][1]["authorization"])
        result = self._check(api, title=stale, body=f"See {OWNER}/HiddenProject.", event_title="feat: a title without a reference", event_body="")
        output = result.stdout + result.stderr
        self.assertEqual(1, result.returncode, output)
        self.assertIn(f"pull request title, line 1, column 14: {NOT_PUBLIC}", output)
        self.assertIn(f"pull request body, line 1, column 5: {NOT_PUBLIC}", output)

    def test_a_pull_request_that_cannot_be_read_fails_with_a_clear_message_and_without_its_texts(self) -> None:
        message = "the pull request's current title and body cannot be read"
        refused = self._list([])
        refused.close()
        cases = [
            ("forbidden", self._list(["PublicTool"], pull_status=403, pull_body=b'{"message": "Resource not accessible"}'), "HTTP 403"),
            ("no JSON", self._list(["PublicTool"], pull_body=b"<html>"), "not the expected JSON"),
            ("no title", self._list(["PublicTool"], pull_body=b'{"number": 7, "body": "text"}'), "not the expected JSON"),
            ("unreachable", refused, "request failed"),
        ]
        for name, api, reason in cases:
            with self.subTest(name):
                result = self._check(api, event_title=f"feat: follow {OWNER}/HiddenProject", event_body=f"See {OWNER}/PublicTool.")
                output = result.stdout + result.stderr
                self.assertEqual(1, result.returncode, output)
                self.assertIn(message, output)
                self.assertIn(reason, output)
                self.assertNotIn(NOT_PUBLIC, output, "an unread pull request is no finding about a reference")
                self.assertEqual([], api.requests_of(LIST_PATH), "the texts of the event are not checked in its place")
                for text in ("hiddenproject", "publictool"):
                    self.assertNotIn(text, output.lower())

    def test_texts_without_a_reference_to_the_owner_pass_without_reading_the_list(self) -> None:
        api = self._list([], status=500)
        self._commit(self.repository, "fix: repair the loader\n\nAs other-owner/HiddenProject does.")
        result = self._check(api, body="No reference here.")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual([], api.requests_of(LIST_PATH))

    def test_a_list_that_cannot_be_read_fails_with_a_clear_message(self) -> None:
        body = f"See {OWNER}/PublicTool."
        message = "the list of public repositories of the owner cannot be read"
        cases = [
            ("rate limited", self._list(["PublicTool"], status=403, body=b'{"message": "API rate limit exceeded"}'), "HTTP 403"),
            ("no JSON", self._list([], body=b"<html>"), "not the expected JSON"),
            ("no list", self._list([], body=b'{"name": "PublicTool"}'), "not the expected JSON"),
        ]
        for name, api, reason in cases:
            with self.subTest(name):
                result = self._check(api, body=body)
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
        result = self._check(api, title=f"feat: follow {OWNER}/HiddenProject", body=body, repository={"private": True})
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("Skipping the reference check: this repository is private, so its texts are not public.", result.stdout)
        self.assertEqual([], api.requests, "a private repository reads neither the pull request nor the list")
        for name, repository in (("public", {"private": False}), ("visibility unknown", {}), ("not a boolean", {"private": "true"})):
            with self.subTest(name):
                result = self._check(api, body=body, repository=repository)
                output = result.stdout + result.stderr
                self.assertEqual(1, result.returncode, output)
                self.assertIn(f"pull request body, line 1, column 5: {NOT_PUBLIC}", output)
                self.assertNotIn("Skipping", output)

    def test_a_run_without_the_pull_request_or_the_owner_fails(self) -> None:
        api = self._list(["PublicTool"])
        result = self._check(api, GITHUB_REPOSITORY_OWNER="")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("GITHUB_REPOSITORY_OWNER", result.stderr)
        result = self._check(api, GITHUB_EVENT_PATH=str(self.directory / "absent.json"))
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("pull request event", result.stderr)
        result = self._check(api, GITHUB_REPOSITORY="")
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("GITHUB_REPOSITORY names no repository", result.stderr)


if __name__ == "__main__":
    unittest.main()
