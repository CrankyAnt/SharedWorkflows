"""Decide whether a commit on the default branch may move ``v1`` automatically, and move it under a lease.

    python3 promote_v1.py read-tag --repository OWNER/NAME [--output GITHUB_OUTPUT]
    python3 promote_v1.py decide --commit SHA --v1 SHA [--v1-object SHA] --app-login LOGIN --owner LOGIN --repository OWNER/NAME
                                 [--pull-request FILE] [--repository-root DIR] [--surface shared-surface.json] [--output GITHUB_OUTPUT]
                                 [--summary FILE]
    python3 promote_v1.py move   --old SHA --new SHA [--remote origin] [--repository-root DIR] [--summary FILE]

``read-tag`` reads ``refs/tags/v1`` through the GitHub API with the token in ``GH_TOKEN``, because the checkout
holds no credentials, and writes ``v1`` and ``v1_object`` as outputs, both empty when the tag does not exist.
``v1`` moves only when the commit landed from a merged pull request that the App opened from the updater's
branch, the repository owner merged it, the commit changes only the pins file, and the shared surface between
the current ``v1`` and the commit differs only in the pins file. When those hold, ``decide`` waits for the push runs
on ``main`` of ``validate.yml`` and ``repository-hygiene-caller.yml`` for the commit, read through the GitHub API with
the token in ``GH_TOKEN``, and ``v1`` moves only when both concluded ``success``; "Automatic v1 move" in
``docs/maintaining.md`` gives the polling interval and the bounds. ``--pull-request`` is the JSON of the merged pull
request as the GitHub API returns it. ``--v1`` is the commit the tag names and ``--v1-object`` the object the
remote ref holds (the same for a lightweight tag, the tag object for an annotated one); ``move`` pushes with
``--force-with-lease`` on that object, so a tag that moved after the decision read it rejects the push. When ``v1`` stays
and the shared surface is identical between ``v1`` and the commit, the summary says that no promotion is needed;
any other stay gives the reason and asks for a manual promotion.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

SURFACE_PATH = Path(__file__).resolve().parent / "shared-surface.json"
TAG = "refs/tags/v1"
REF_URL = "https://api.github.com/repos/{repository}/git/ref/tags/v1"
TAG_OBJECT_URL = "https://api.github.com/repos/{repository}/git/tags/{sha}"
RUNS_URL = "https://api.github.com/repos/{repository}/actions/workflows/{workflow}/runs?head_sha={commit}&event=push&branch=main&per_page=100"
CI_WORKFLOWS = ("validate.yml", "repository-hygiene-caller.yml")
CI_TIMEOUT = 10 * 60
CI_INTERVAL = 30
REQUEST_TIMEOUT = 20
Fetcher = Callable[[str], bytes | None]
Conclusions = Mapping[str, str | None]


class CiReadError(RuntimeError):
    """The CI runs of the commit could not be read on the last poll before the wait ended."""


@dataclass(frozen=True)
class Decision:
    move: bool
    reason: str
    old: str
    new: str
    surface_identical: bool = False

    def summary(self) -> str:
        if self.move:
            return f"## v1 promotion\n\n`v1` moves from `{self.old}` to `{self.new}`: {self.reason}\n"
        target = self.old or "no v1 tag"
        if self.surface_identical:
            return f"## v1 promotion\n\n`v1` stays at `{target}`: the shared surface is identical between `v1` and this commit. No promotion is needed for this commit.\n"
        return f"## v1 promotion\n\n`v1` stays at `{target}`: {self.reason} A manual promotion is needed for this commit.\n"


def git(root: Path, *arguments: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(arguments)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def fetch(url: str) -> bytes | None:
    """The body of a GitHub API response with the token in ``GH_TOKEN``, or None when the resource does not exist."""
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {os.environ['GH_TOKEN']}", "X-GitHub-Api-Version": "2022-11-28"}
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # noqa: S310 - fixed API host
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def read_tag(repository: str, fetcher: Fetcher) -> tuple[str, str]:
    """The commit ``v1`` names and the object ``refs/tags/v1`` holds, both empty when the tag does not exist."""
    body = fetcher(REF_URL.format(repository=repository))
    if body is None:
        return "", ""
    target = json.loads(body)["object"]
    v1_object = target["sha"]
    while target["type"] == "tag":
        peeled = fetcher(TAG_OBJECT_URL.format(repository=repository, sha=target["sha"]))
        if peeled is None:
            raise RuntimeError(f"the tag object {target['sha']} of {TAG} could not be read")
        target = json.loads(peeled)["object"]
    return target["sha"], v1_object


def ci_conclusions(repository: str, commit: str, fetcher: Fetcher) -> Conclusions:
    """The conclusion of the newest push run on ``main`` of each CI workflow for ``commit``; None while it has not completed or no run exists."""
    conclusions: dict[str, str | None] = {}
    for workflow in CI_WORKFLOWS:
        body = fetcher(RUNS_URL.format(repository=repository, workflow=workflow, commit=commit))
        runs = json.loads(body).get("workflow_runs", []) if body else []
        newest = max(runs, key=lambda run: run["id"], default=None)
        conclusions[workflow] = newest["conclusion"] if newest and newest["status"] == "completed" else None
    return conclusions


def wait_for_ci(repository: str, commit: str, fetcher: Fetcher, timeout: float = CI_TIMEOUT, interval: float = CI_INTERVAL,
                request_timeout: float = REQUEST_TIMEOUT, clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], None] = time.sleep) -> Conclusions:
    """Poll the CI of ``commit`` until every run completed or one completed without success.

    A poll starts only while it ends within ``timeout`` seconds even when each of its requests takes ``request_timeout``.
    A failed read counts as not completed; when the last poll failed, :class:`CiReadError` names its error.
    """
    deadline = clock() + timeout - len(CI_WORKFLOWS) * request_timeout
    conclusions: Conclusions = dict.fromkeys(CI_WORKFLOWS)
    while True:
        error = ""
        try:
            conclusions = ci_conclusions(repository, commit, fetcher)
        except (OSError, ValueError) as caught:
            error = f"{type(caught).__name__}: {caught}"
        else:
            values = conclusions.values()
            if all(value is not None for value in values) or any(value not in (None, "success") for value in values):
                return conclusions
        remaining = deadline - clock()
        if remaining <= 0:
            if error:
                raise CiReadError(error)
            return conclusions
        sleep(min(interval, remaining))


def ci_stay_reason(conclusions: Conclusions) -> str | None:
    """Why ``v1`` stays given the CI conclusions, or None when every run concluded ``success``."""
    failed = [f"{workflow} concluded {conclusion}" for workflow, conclusion in conclusions.items() if conclusion not in (None, "success")]
    if failed:
        return f"the CI of this commit on main did not pass: {', '.join(failed)}."
    pending = [workflow for workflow, conclusion in conclusions.items() if conclusion is None]
    if pending:
        return f"the CI of this commit on main had not completed when the wait ended: {', '.join(pending)}."
    return None


def changed_files(root: Path, base: str, head: str, paths: list[str] | None = None) -> list[str]:
    scope = ["--", *paths] if paths else []
    output = git(root, "diff", "--name-only", base, head, *scope)
    return [line for line in output.splitlines() if line]


def load_surface(path: Path = SURFACE_PATH) -> dict:
    surface = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(surface.get("pins"), str) or not isinstance(surface.get("updater_branch"), str) or not isinstance(surface.get("paths"), list):
        raise ValueError(f"{path} must hold pins, updater_branch and paths")
    return surface


def has_commit(root: Path, sha: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True, check=False).returncode == 0


def surface_matches_v1(root: Path, v1: str, commit: str, paths: list[str]) -> bool:
    """Whether the shared surface paths hold the same content at ``v1`` and ``commit``; False when ``v1`` is empty, absent or the comparison fails."""
    if not v1 or not has_commit(root, v1):
        return False
    try:
        return not changed_files(root, v1, commit, paths)
    except RuntimeError:
        return False


def decide(root: Path, commit: str, v1: str, pull_request: dict | None, app_login: str, owner: str, surface: dict, v1_object: str = "", *,
           ci: Callable[[], Conclusions]) -> Decision:
    """Move or stay; ``ci`` gives the CI conclusions of ``commit`` and is called only after every other condition holds."""
    pins = surface["pins"]
    lease = v1_object or v1
    stay = lambda reason: Decision(False, reason, lease, commit, surface_matches_v1(root, v1, commit, surface["paths"]))  # noqa: E731
    if pull_request is None:
        return stay("no merged pull request landed this commit.")
    if pull_request.get("merged") is not True or pull_request.get("merge_commit_sha") != commit:
        return stay("the pull request found for this commit is not the merged pull request that produced it.")
    try:
        commit_files = changed_files(root, f"{commit}^", commit)
    except RuntimeError as error:
        return stay(f"the commit's changes could not be read: {error}")
    if commit_files != [pins]:
        return stay(f"the commit changes {', '.join(commit_files) or 'nothing'}, not only {pins}.")
    author = (pull_request.get("user") or {}).get("login")
    if not app_login or author != app_login:
        return stay(f"the pull request was opened by {author or 'an unknown account'}, not by the updater App {app_login or '(unknown)'}.")
    branch = (pull_request.get("head") or {}).get("ref")
    if branch != surface["updater_branch"]:
        return stay(f"the pull request came from {branch or 'an unknown branch'}, not from the updater branch {surface['updater_branch']}.")
    merged_by = (pull_request.get("merged_by") or {}).get("login")
    if merged_by != owner:
        return stay(f"the pull request was merged by {merged_by or 'an unknown account'}, not by the repository owner {owner}.")
    if not v1:
        return stay("no v1 tag exists yet.")
    if not has_commit(root, v1):
        return stay(f"the current v1 commit {v1} is not available in this checkout.")
    if subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", v1, commit], capture_output=True, check=False).returncode != 0:
        return stay(f"the current v1 commit {v1} is not an ancestor of this commit.")
    try:
        surface_files = changed_files(root, v1, commit, surface["paths"])
    except RuntimeError as error:
        return stay(f"the shared surface could not be compared: {error}")
    others = [path for path in surface_files if path != pins]
    if others:
        return stay(f"the shared surface between v1 and this commit also differs in {', '.join(others)}.")
    try:
        reason = ci_stay_reason(ci())
    except CiReadError as error:
        return stay(f"the CI runs of this commit on main could not be read when the wait ended: {error}.")
    if reason:
        return stay(reason)
    return Decision(True, "a pins-only updater pull request merged by the repository owner landed, the shared surface otherwise matches the current v1, and the CI of this commit on main passed.", lease, commit)


def move(root: Path, remote: str, old: str, new: str) -> tuple[bool, str]:
    """Push ``new`` to ``v1`` only while the remote tag still names ``old``."""
    result = subprocess.run(
        ["git", "-C", str(root), "push", f"--force-with-lease={TAG}:{old}", remote, f"{new}:{TAG}"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        return False, f"## v1 promotion\n\nThe push of `v1` to `{new}` was rejected; the tag no longer names `{old}` and keeps its newer target.\n\n```\n{result.stderr.strip()}\n```\n"
    return True, f"## v1 promotion\n\n`v1` moved from `{old}` to `{new}`.\n"


def _append(path: Path | None, text: str) -> None:
    if path:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)


def build_parser() -> argparse.ArgumentParser:
    """The command line of this script; the part's tests parse the workflow's invocations with it."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subcommands = parser.add_subparsers(dest="command", required=True)
    tag_parser = subcommands.add_parser("read-tag")
    tag_parser.add_argument("--repository", required=True, help="OWNER/NAME of the repository whose v1 tag is read")
    tag_parser.add_argument("--output", type=Path)
    decide_parser = subcommands.add_parser("decide")
    decide_parser.add_argument("--repository-root", type=Path, default=Path("."))
    decide_parser.add_argument("--commit", required=True)
    decide_parser.add_argument("--v1", default="", help="the commit the remote v1 tag names, empty when it does not exist")
    decide_parser.add_argument("--v1-object", default="", help="the object the remote refs/tags/v1 holds; defaults to --v1")
    decide_parser.add_argument("--pull-request", type=Path, help="JSON of the merged pull request; omitted when none landed the commit")
    decide_parser.add_argument("--app-login", default="")
    decide_parser.add_argument("--owner", required=True)
    decide_parser.add_argument("--repository", required=True, help="OWNER/NAME of the repository whose CI runs of the commit are read")
    decide_parser.add_argument("--surface", type=Path, default=SURFACE_PATH)
    decide_parser.add_argument("--output", type=Path)
    decide_parser.add_argument("--summary", type=Path)
    move_parser = subcommands.add_parser("move")
    move_parser.add_argument("--repository-root", type=Path, default=Path("."))
    move_parser.add_argument("--remote", default="origin")
    move_parser.add_argument("--old", required=True)
    move_parser.add_argument("--new", required=True)
    move_parser.add_argument("--summary", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.command == "read-tag":
        v1, v1_object = read_tag(arguments.repository, fetch)
        print(f"v1 names {v1} (tag object {v1_object})." if v1 else "no v1 tag exists yet.")
        _append(arguments.output, f"v1={v1}\nv1_object={v1_object}\n")
        return 0
    if arguments.command == "decide":
        pull_request = None
        if arguments.pull_request and arguments.pull_request.is_file():
            loaded = json.loads(arguments.pull_request.read_text(encoding="utf-8"))
            pull_request = loaded if isinstance(loaded, dict) and loaded else None
        decision = decide(arguments.repository_root.resolve(), arguments.commit, arguments.v1, pull_request, arguments.app_login, arguments.owner,
                          load_surface(arguments.surface), arguments.v1_object, ci=lambda: wait_for_ci(arguments.repository, arguments.commit, fetch))
        print(decision.summary())
        _append(arguments.summary, decision.summary())
        _append(arguments.output, f"move={str(decision.move).lower()}\nold={decision.old}\nnew={decision.new}\n")
        return 0
    moved, summary = move(arguments.repository_root.resolve(), arguments.remote, arguments.old, arguments.new)
    print(summary)
    _append(arguments.summary, summary)
    return 0 if moved else 1


if __name__ == "__main__":
    raise SystemExit(main())
