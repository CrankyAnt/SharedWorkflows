"""Fixture consumers behind a stand-in for the GitHub API, a fixture consumer kit with invented templates and a fixture
adoption table, for the tests of the sync program."""

from __future__ import annotations

import base64
import copy
import hashlib
import http.server
import io
import json
import os
import re
import subprocess
import sys
import threading
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

from tests import temporary_support
from tests.consumer_checks_fixtures import CALLERS, SHARED, SYNC_BRANCH, WORKFLOWS, template
from tests.pipeline_fixtures import CONFIGURATION, REPOSITORY_ROOT, PipelineFixture, write

SYNC_FOLDER = REPOSITORY_ROOT / "actions" / "sync"
SCRIPT = SYNC_FOLDER / "sync.py"
sys.path.insert(0, str(SYNC_FOLDER))
import sync as sync_program  # noqa: E402

OWNER = "example-owner"
CONSUMER = f"{OWNER}/consumer"
OTHER = f"{OWNER}/other-consumer"
MANAGEMENT = f"{OWNER}/management"
CI_GUIDE = "docs/agents/ci.md"
CI_GUIDE_TEXT = f"# CI\n\nThe catalogue of {SHARED} at v1 holds every rule.\n"
DEPENDABOT = ".github/dependabot.yml"
APP_TOKEN = "token-of-the-app"
OWNER_TOKEN = "token-of-the-owner"
RUN_TOKEN = "token-of-the-run"
NOW = "2026-03-10T12:00:00Z"
RULESET = json.loads((REPOSITORY_ROOT / "actions" / "repository-ruleset" / "ruleset-main-template.json").read_text(encoding="utf-8"))
MERGE_SETTINGS = json.loads((SYNC_FOLDER / "merge-settings.json").read_text(encoding="utf-8"))
REQUIRED_CHECK = "ci / verdict"


def expected_ruleset() -> dict:
    """The ruleset of a consumer that adopted the pipeline: the template with ``ci / verdict`` as its one required check,
    written out here from the template's other fields."""
    ruleset = copy.deepcopy(RULESET)
    for rule in ruleset["rules"]:
        if rule["type"] == "required_status_checks":
            rule["parameters"]["required_status_checks"] = [{"context": REQUIRED_CHECK, "integration_id": 15368}]
    return ruleset


def digest(document: object) -> str:
    return hashlib.sha1(json.dumps(document, sort_keys=True).encode()).hexdigest()


class Repository:
    """One repository of the stand-in: its Git objects and refs, settings, rulesets, pull requests and issues."""

    def __init__(self, full_name: str, files: dict[str, str]) -> None:
        self.full_name, self.default_branch, self.archived = full_name, "main", False
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict] = {}
        self.refs: dict[str, str] = {}
        self.settings = dict(MERGE_SETTINGS)
        self.rulesets: list[dict] = [{"id": 7, **expected_ruleset()}]
        self.pulls: list[dict] = []
        self.issues: list[dict] = []
        self.issue_comments: dict[int, list[dict]] = {}
        self.auto_merge_allowed = True
        self.refs["heads/main"] = self.commit(self.tree(files), [], "initial")

    def tree(self, files: dict[str, str]) -> str:
        sha = digest(files)
        self.trees[sha] = dict(files)
        return sha

    def commit(self, tree: str, parents: list[str], message: str) -> str:
        sha = digest({"tree": tree, "parents": parents, "message": message})
        self.commits[sha] = {"tree": tree, "parents": parents, "message": message}
        return sha

    def files(self, branch: str = "main") -> dict[str, str]:
        return self.trees[self.commits[self.refs[f"heads/{branch}"]]["tree"]]

    def push(self, files: dict[str, str | None], branch: str = "main") -> None:
        """Commit changed files onto a branch, as a push of the repository's own work does."""
        tip = self.refs[f"heads/{branch}"]
        merged = {**self.files(branch), **{path: text for path, text in files.items() if text is not None}}
        for path, text in files.items():
            if text is None:
                merged.pop(path, None)
        self.refs[f"heads/{branch}"] = self.commit(self.tree(merged), [tip], "work")

    def pull_request(self, branch: str, created_at: str, auto_merge: bool = True) -> dict:
        number = 30 + len(self.pulls) + 1
        pull = {"number": number, "node_id": f"PR_{number}", "html_url": f"https://github.example/{self.full_name}/pull/{number}", "state": "open",
                "head": {"ref": branch}, "base": {"ref": self.default_branch}, "created_at": created_at, "title": "", "body": "", "merged": False,
                "auto_merge": {"merge_method": "squash"} if auto_merge else None}
        self.pulls.append(pull)
        return pull

    def issue(self, title: str, body: str = "", labels: tuple[str, ...] = (), state: str = "open", pull_request: bool = False) -> dict:
        number = 100 + len(self.issues) + 1
        issue = {"number": number, "title": title, "body": body, "state": state, "labels": [{"name": label} for label in labels],
                 "closed_at": NOW if state == "closed" else None,
                 "html_url": f"https://github.example/{self.full_name}/issues/{number}", **({"pull_request": {"url": "x"}} if pull_request else {})}
        self.issues.append(issue)
        return issue


@dataclass(frozen=True)
class Write:
    method: str
    path: str
    body: dict
    token: str


class GitHubStandIn:
    """A local HTTP server that answers as the GitHub API does for the repositories it holds: the App's installation,
    contents, Git objects and refs, pull requests, issues, rulesets, settings and the auto-merge mutation. The token of
    the App may not change settings or rulesets, as an App without Administration may not, and reads no bypass list.
    Every request that writes is recorded."""

    def __init__(self) -> None:
        self.repositories: dict[str, Repository] = {}
        self.installed: list[str] = []
        self.installation_total_count: int | None = None
        self.now = NOW
        self.writes: list[Write] = []
        self.requests: list[tuple[str, str, str]] = []
        self.refused: dict[str, int] = {}
        self.snapshot_errors: dict[str, list[dict]] = {}
        self.on_request = None
        stand_in = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def answer(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode()) if length else {}
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                stand_in.requests.append((method, self.path, token))
                if method != "GET" and not (self.path == "/graphql" and not str(body.get("query", "")).lstrip().startswith("mutation")):
                    stand_in.writes.append(Write(method, self.path, body, token))
                code, document = stand_in.route(method, self.path, body, token)
                if stand_in.on_request is not None:
                    stand_in.on_request(method, self.path)
                payload = json.dumps(document).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:
                self.answer("GET")

            def do_POST(self) -> None:
                self.answer("POST")

            def do_PATCH(self) -> None:
                self.answer("PATCH")

            def do_PUT(self) -> None:
                self.answer("PUT")

            def log_message(self, *arguments) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def repository(self, full_name: str, files: dict[str, str], installed: bool = True) -> Repository:
        self.repositories[full_name] = Repository(full_name, files)
        if installed:
            self.installed.append(full_name)
        return self.repositories[full_name]

    def writes_to(self, full_name: str) -> list[Write]:
        return [item for item in self.writes if item.path.startswith(f"/repos/{full_name}/") or item.path == f"/repos/{full_name}"]

    # --- the routes -------------------------------------------------------------------------------------------------

    def route(self, method: str, address: str, body: dict, token: str) -> tuple[int, object]:
        parts = urlsplit(address)
        path, query = unquote(parts.path), {name: values[0] for name, values in parse_qs(parts.query).items()}
        for prefix, code in self.refused.items():
            if path.startswith(prefix):
                return code, {"message": "refused"}
        if path == "/installation/repositories":
            page, size = int(query.get("page", 1)), int(query.get("per_page", 30))
            names = self.installed[(page - 1) * size:page * size]
            total = len(self.installed) if self.installation_total_count is None else self.installation_total_count
            return 200, {"total_count": total, "repositories": [self.summary(self.repositories[name]) for name in names]}
        if path == "/graphql":
            return self.graphql(body)
        match = re.fullmatch(r"/repos/([^/]+/[^/]+)(?:/(.*))?", path)
        repository = self.repositories.get(match.group(1)) if match else None
        if repository is None:
            return 404, {"message": "Not Found"}
        rest = match.group(2) or ""
        administers = token != APP_TOKEN
        if rest == "":
            if method == "PATCH":
                if not administers:
                    return 403, {"message": "Resource not accessible by integration"}
                repository.settings.update(body)
            return 200, self.summary(repository)
        if rest.startswith("contents/"):
            return self.contents(repository, rest.removeprefix("contents/"), query.get("ref", repository.default_branch))
        if rest.startswith("git/ref/"):
            sha = repository.refs.get(rest.removeprefix("git/ref/"))
            return (200, {"object": {"sha": sha, "type": "commit"}}) if sha else (404, {"message": "Not Found"})
        if rest.startswith("git/commits/"):
            commit = repository.commits.get(rest.removeprefix("git/commits/"))
            return (200, {"tree": {"sha": commit["tree"]}, "parents": [{"sha": sha} for sha in commit["parents"]]}) if commit else (404, {"message": "Not Found"})
        if rest.startswith("git/blobs/"):
            blob_sha = rest.removeprefix("git/blobs/")
            for tree in repository.trees.values():
                for text in tree.values():
                    if digest(text) == blob_sha:
                        return 200, {"content": base64.encodebytes(text.encode()).decode(), "encoding": "base64"}
            return 404, {"message": "Not Found"}
        if rest == "git/trees" and method == "POST":
            files = dict(repository.trees[body["base_tree"]])
            for entry in body["tree"]:
                if "content" in entry:
                    files[entry["path"]] = entry["content"]
                else:
                    files.pop(entry["path"], None)
            return 201, {"sha": repository.tree(files)}
        if rest == "git/commits" and method == "POST":
            return 201, {"sha": repository.commit(body["tree"], body["parents"], body["message"])}
        if rest == "git/refs" and method == "POST":
            name = body["ref"].removeprefix("refs/")
            if name in repository.refs:
                return 422, {"message": "Reference already exists"}
            repository.refs[name] = body["sha"]
            return 201, {"ref": body["ref"]}
        if rest.startswith("git/refs/") and method == "PATCH":
            repository.refs[rest.removeprefix("git/refs/")] = body["sha"]
            return 200, {"ref": rest}
        if rest == "pulls":
            if method == "POST":
                pull = repository.pull_request(body["head"], NOW, auto_merge=False)
                pull.update(title=body["title"], body=body.get("body", ""), base={"ref": body["base"]})
                return 201, pull
            head = query.get("head", "")
            return 200, [pull for pull in repository.pulls if pull["state"] == query.get("state", "open") and f"{repository.full_name.split('/')[0]}:{pull['head']['ref']}" == head]
        pull_match = re.fullmatch(r"pulls/(\d+)", rest)
        if pull_match and method == "GET":
            number = int(pull_match.group(1))
            pull = next((item for item in repository.pulls if item["number"] == number), None)
            return (200, pull) if pull is not None else (404, {"message": "Not Found"})
        if rest == "issues":
            if method == "POST":
                issue = repository.issue(body["title"], body.get("body", ""))
                issue["user"] = {"login": "github-actions[bot]" if token == RUN_TOKEN else OWNER}
                return 201, issue
            found = [issue for issue in repository.issues
                     if query.get("state", "open") in ("all", issue["state"])
                     and ("labels" not in query or query["labels"] in [label["name"] for label in issue["labels"]])]
            page, size = int(query.get("page", 1)), int(query.get("per_page", 30))
            return 200, found[(page - 1) * size:page * size]
        issue_match = re.fullmatch(r"issues/(\d+)(/comments)?", rest)
        if issue_match:
            number = int(issue_match.group(1))
            issue = next((item for item in repository.issues if item["number"] == number), None)
            if issue is None:
                return 404, {"message": "Not Found"}
            if issue_match.group(2):
                if method == "POST":
                    comments = repository.issue_comments.setdefault(number, [])
                    comment = {"id": len(comments) + 1, "body": body["body"], "created_at": self.now,
                               "user": {"login": "github-actions[bot]" if token == RUN_TOKEN else OWNER}}
                    comments.append(comment)
                    return 201, comment
                page, size = int(query.get("page", 1)), int(query.get("per_page", 30))
                return 200, repository.issue_comments.get(number, [])[(page - 1) * size:page * size]
            if method == "PATCH":
                if "state" in body and body["state"] != issue["state"]:
                    issue["closed_at"] = self.now if body["state"] == "closed" else None
                issue.update(body)
            return 200, issue
        if rest == "rulesets":
            if method == "POST":
                if not administers:
                    return 403, {"message": "Resource not accessible by integration"}
                repository.rulesets.append({"id": 40 + len(repository.rulesets), **body})
                return 201, repository.rulesets[-1]
            return 200, [{key: ruleset[key] for key in ("id", "name", "target", "enforcement")} for ruleset in repository.rulesets]
        if rest.startswith("rulesets/"):
            number = int(rest.removeprefix("rulesets/"))
            ruleset = next((item for item in repository.rulesets if item["id"] == number), None)
            if ruleset is None:
                return 404, {"message": "Not Found"}
            if method == "PUT":
                if not administers:
                    return 403, {"message": "Resource not accessible by integration"}
                ruleset.clear()
                ruleset.update({"id": number, **body})
            return 200, ruleset if administers else {key: value for key, value in ruleset.items() if key != "bypass_actors"}
        return 404, {"message": "Not Found"}

    def summary(self, repository: Repository) -> dict:
        return {"full_name": repository.full_name, "default_branch": repository.default_branch, "archived": repository.archived, **repository.settings}

    def contents(self, repository: Repository, path: str, ref: str) -> tuple[int, object]:
        files = repository.files(ref)
        if path in files:
            return 200, {"type": "file", "path": path, "encoding": "base64", "content": base64.encodebytes(files[path].encode()).decode()}
        under = sorted({item[len(path) + 1:].split("/", 1)[0] for item in files if item.startswith(path + "/")})
        if not under:
            return 404, {"message": "Not Found"}
        return 200, [{"name": name, "path": f"{path}/{name}", "type": "file" if f"{path}/{name}" in files else "dir"} for name in under]

    def graphql(self, body: dict) -> tuple[int, object]:
        query = body.get("query", "")
        variables = body.get("variables") or {}
        if "defaultBranchRef" in query:
            full_name = f"{variables['owner']}/{variables['name']}"
            repository = self.repositories.get(full_name)
            if repository is None:
                return 200, {"data": {"repository": None}, "errors": [{"message": "Could not resolve to a Repository"}]}
            tip = repository.refs.get(f"heads/{repository.default_branch}")
            commit = repository.commits.get(tip) if tip else None
            if not tip or not commit:
                return 200, {"data": {"repository": {"defaultBranchRef": None}}}
            files = repository.trees[commit["tree"]]
            target: dict[str, object] = {"oid": tip, "tree": {"oid": commit["tree"]}}
            errors: list[dict[str, object]] = []
            for alias, var in re.findall(r"(\w+):\s*file\(path:\s*\$(\w+)\)", query):
                file_path = variables[var]
                if file_path in files:
                    target[alias] = {"oid": digest(files[file_path]), "object": {"text": files[file_path]}}
                else:
                    prefix = file_path + "/"
                    under = sorted({item[len(prefix):].split("/", 1)[0] for item in files if item.startswith(prefix)})
                    if under:
                        entries = []
                        for name in under:
                            full = f"{file_path}/{name}"
                            if full in files:
                                entries.append({"name": name, "type": "blob", "oid": digest(files[full]), "object": {"text": files[full]}})
                            else:
                                entries.append({"name": name, "type": "tree", "oid": digest(full), "object": None})
                        target[alias] = {"type": "tree", "object": {"entries": entries}}
                    else:
                        target[alias] = None
                        errors.append({
                            "type": "NOT_FOUND",
                            "path": ["repository", "defaultBranchRef", "target", alias],
                            "message": f"Could not resolve file for path '{file_path}'.",
                        })
            errors.extend(self.snapshot_errors.get(full_name, []))
            pulls = [
                {
                    "number": pull["number"],
                    "id": pull["node_id"],
                    "url": pull["html_url"],
                    "createdAt": pull["created_at"],
                    "headRepository": {"nameWithOwner": repository.full_name},
                    "autoMergeRequest": {"mergeMethod": pull["auto_merge"]["merge_method"].upper()} if pull.get("auto_merge") else None,
                }
                for pull in repository.pulls
                if pull["state"] == "open" and pull["head"]["ref"] == variables.get("branch")
            ]
            result: dict[str, object] = {
                "defaultBranchRef": {"name": repository.default_branch, "target": target},
                "pullRequests": {"nodes": pulls},
            }
            if "issues(" in query:
                label = variables.get("label")
                issues = [
                    {"number": issue["number"], "title": issue["title"], "url": issue["html_url"]}
                    for issue in repository.issues
                    if issue["state"] == "open" and "pull_request" not in issue
                    and (label is None or label in [item["name"] for item in issue["labels"]])
                ]
                result["issues"] = {"pageInfo": {"hasNextPage": False}, "nodes": issues}
            response: dict[str, object] = {"data": {"repository": result}}
            if errors:
                response["errors"] = errors
            return 200, response
        if "enablePullRequestAutoMerge" not in query:
            return 200, {"errors": [{"message": "unknown operation"}]}
        for repository in self.repositories.values():
            for pull in repository.pulls:
                if pull["node_id"] == variables["pullRequest"]:
                    if not repository.auto_merge_allowed:
                        return 200, {"errors": [{"message": "Auto merge is not allowed for this repository"}]}
                    pull["auto_merge"] = {"merge_method": variables["method"].lower()}
                    return 200, {"data": {"enablePullRequestAutoMerge": {"pullRequest": {"number": pull["number"]}}}}
        return 200, {"errors": [{"message": "Could not resolve to a node"}]}


@dataclass(frozen=True)
class Synced:
    status: int
    stdout: str
    stderr: str
    summary: str


def consumer_files() -> dict[str, str]:
    """The files of a consumer that matches the fixture kit: the three fixed callers, the CI guide and the
    smallest configuration file."""
    files = {f"{WORKFLOWS}/{name}": template(name) for name in CALLERS}
    files.update({CI_GUIDE: CI_GUIDE_TEXT, CONFIGURATION: "version: 1\n"})
    return files


class SyncFixture(PipelineFixture):
    """A fixture kit with invented templates, a stand-in for the GitHub API and the sync program's command line."""

    @temporary_support.measured("sync")
    def setUp(self) -> None:
        super().setUp()
        self.kit = self.directory / "kit"
        for name in CALLERS:
            write(self.kit, name, template(name))
        write(self.kit, CI_GUIDE, CI_GUIDE_TEXT)
        write(self.kit, "fixed-callers.json", json.dumps({"previous": {}, "sync-branch": SYNC_BRANCH}))
        self.adoption = self.directory / "adoption.json"
        self.adoption.write_text(json.dumps({"shared-workflows": {"fixture-floor.yml": "fixture-floor", "fixture-test.yml": "fixture-test",
                                                                  "fixture-later.yml": "fixture-later"}}), encoding="utf-8")
        self.api = GitHubStandIn()
        self.addCleanup(self.api.close)

    def consumer(self, files: dict[str, str | None] | None = None, name: str = CONSUMER) -> Repository:
        """A consumer that the App is installed on; ``files`` replaces a file of a matching consumer or removes it."""
        held = consumer_files()
        for path, text in (files or {}).items():
            if text is None:
                held.pop(path)
            else:
                held[path] = text
        return self.api.repository(name, held)

    def arguments_of(self, command: str, *arguments: str) -> tuple[list[str], Path]:
        summary = self.directory / "summary.md"
        summary.unlink(missing_ok=True)
        options = ["--api-url", self.api.url, "--summary", str(summary)]
        if command not in {"failure", "success"}:
            options += ["--kit", str(self.kit), "--schemas", str(self.schemas), "--adoption", str(self.adoption), "--now", NOW]
        return [command, *options, *arguments], summary

    def environment(self, token: str) -> dict[str, str]:
        kept = {name: value for name, value in os.environ.items() if name not in ("GH_TOKEN", "GITHUB_TOKEN")}
        return {**kept, "GH_TOKEN": token, "GITHUB_TOKEN": RUN_TOKEN}

    def run_sync(self, command: str = "sync", *arguments: str, token: str = APP_TOKEN) -> Synced:
        """Run a command of the sync program in this process, through its command line."""
        argv, summary = self.arguments_of(command, *arguments)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.environment(token), clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
            status = sync_program.main(argv)
        return Synced(status, stdout.getvalue(), stderr.getvalue(), summary.read_text(encoding="utf-8") if summary.exists() else "")

    def start(self, command: str = "sync", *arguments: str, token: str = APP_TOKEN) -> Synced:
        """Start the sync program as a process of its own."""
        argv, summary = self.arguments_of(command, *arguments)
        completed = subprocess.run([sys.executable, str(SCRIPT), *argv], capture_output=True, text=True, encoding="utf-8", env=self.environment(token))
        return Synced(completed.returncode, completed.stdout, completed.stderr, summary.read_text(encoding="utf-8") if summary.exists() else "")

    def local(self, *arguments: str, command: str = "sync") -> Synced:
        """Run the sync's local mode on the fixture consumer with the owner's own token."""
        options = ("--local",) if command == "sync" else ()
        return self.run_sync(command, *options, "--repository", CONSUMER, *arguments, token=OWNER_TOKEN)
