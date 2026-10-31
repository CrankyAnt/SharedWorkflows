"""Decide whether a commit on the default branch may move ``v1`` automatically, and move it under a lease; prepare and
push a manual move of ``v1``.

    python3 promote_v1.py read-tag --repository OWNER/NAME [--output GITHUB_OUTPUT]
    python3 promote_v1.py decide --commit SHA --v1 SHA [--v1-object SHA] --app-login LOGIN --owner LOGIN --repository OWNER/NAME
                                 [--pull-request FILE] [--repository-root DIR] [--surface shared-surface.json] [--output GITHUB_OUTPUT]
                                 [--summary FILE]
    python3 promote_v1.py move   --old SHA --new SHA [--remote origin] [--repository-root DIR] [--summary FILE]
    python3 promote_v1.py prepare [--commit SHA] [--remote origin] [--repository-root DIR]
    python3 promote_v1.py promote --old OBJECT --new SHA [--remote origin] [--repository-root DIR]

``prepare`` and ``promote`` carry every manual move of ``v1``: ``prepare`` reports the facts of the move and prints the
question for the user with the ``promote`` command below it, or refuses, and ``promote`` pushes the tag after a yes.
"Promote v1 by hand" in ``docs/maintaining.md`` states what ``prepare`` reports, when it refuses and what ``promote``
does.

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
import re
import shlex
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
API_HOST = "https://api.github.com/"
GITHUB_REMOTE = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?$")
GITLEAKS_OPTIONS = ("--config", ".gitleaks.toml", "--baseline-path", ".gitleaks-baseline.json")
SCRIPT_COMMAND = "python3 actions/repository-hygiene/promote_v1.py"
PROMOTION, ROLLBACK, FIRST_CREATION = "promotion", "rollback", "first creation"
SCAN_REFUSAL = "the Gitleaks scan of the tag push reports a finding or could not run"
CI_WORKFLOWS = ("validate.yml", "repository-hygiene-caller.yml")
CI_TIMEOUT = 10 * 60
CI_INTERVAL = 30
REQUEST_TIMEOUT = 20
Fetcher = Callable[[str], bytes | None]
Conclusions = Mapping[str, str | None]


class CiReadError(RuntimeError):
    """The CI runs of the commit could not be read on the last poll before the wait ended."""


@dataclass(frozen=True)
class CiRun:
    """The newest push run on ``main`` of one CI workflow; ``status`` is None when no run exists."""

    status: str | None
    conclusion: str | None


@dataclass(frozen=True)
class ScanResult:
    """The push gate's Gitleaks scan of a tag push: whether it passed, and what it printed."""

    passed: bool
    output: str


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


def newest_runs(repository: str, commit: str, fetcher: Fetcher) -> dict[str, CiRun]:
    """The newest push run on ``main`` of each CI workflow for ``commit``."""
    newest: dict[str, CiRun] = {}
    for workflow in CI_WORKFLOWS:
        body = fetcher(RUNS_URL.format(repository=repository, workflow=workflow, commit=commit))
        runs = json.loads(body).get("workflow_runs", []) if body else []
        run = max(runs, key=lambda candidate: candidate["id"], default=None)
        newest[workflow] = CiRun(run["status"], run["conclusion"]) if run else CiRun(None, None)
    return newest


def ci_conclusions(repository: str, commit: str, fetcher: Fetcher) -> Conclusions:
    """The conclusion of the newest push run on ``main`` of each CI workflow for ``commit``; None while it has not completed or no run exists."""
    return {workflow: run.conclusion if run.status == "completed" else None for workflow, run in newest_runs(repository, commit, fetcher).items()}


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


def leased_push(root: Path, remote: str, old: str, new: str) -> subprocess.CompletedProcess:
    """Push ``new`` to ``refs/tags/v1`` only while the remote ref holds ``old``; an empty ``old`` requires that no tag exists."""
    return subprocess.run(
        ["git", "-C", str(root), "push", f"--force-with-lease={TAG}:{old}", remote, f"{new}:{TAG}"],
        capture_output=True, text=True, check=False,
    )


def move(root: Path, remote: str, old: str, new: str) -> tuple[bool, str]:
    """Push ``new`` to ``v1`` only while the remote tag still names ``old``."""
    result = leased_push(root, remote, old, new)
    if result.returncode != 0:
        return False, f"## v1 promotion\n\nThe push of `v1` to `{new}` was rejected; the tag no longer names `{old}` and keeps its newer target.\n\n```\n{result.stderr.strip()}\n```\n"
    return True, f"## v1 promotion\n\n`v1` moved from `{old}` to `{new}`.\n"


def gh_fetch(url: str) -> bytes | None:
    """The body of a GitHub API response, read through the authentication of the ``gh`` CLI."""
    result = subprocess.run(["gh", "api", "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28", url.removeprefix(API_HOST)],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"gh api failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout.encode("utf-8")


def github_ci(repository: str, commit: str) -> dict[str, CiRun]:
    """The newest push run on ``main`` of each CI workflow for ``commit``, read once through ``gh``."""
    return newest_runs(repository, commit, gh_fetch)


def github_repository(root: Path, remote: str) -> str:
    """OWNER/NAME of the GitHub repository behind ``remote``."""
    url = git(root, "remote", "get-url", remote)
    match = GITHUB_REMOTE.match(url)
    if match is None:
        raise RuntimeError(f"the remote {remote} ({url}) is not a GitHub repository.")
    return match.group(1)


def gitleaks_scan(root: Path, remote: str, commit: str) -> ScanResult:
    """The push gate's Gitleaks scan of the commits a tag push of ``commit`` would send, after the remote was fetched."""
    try:
        result = subprocess.run(["gitleaks", "git", f"--log-opts={commit} --not --remotes={remote}", "--redact", "--no-banner", "--no-color", *GITLEAKS_OPTIONS],
                                cwd=root, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return ScanResult(False, "Gitleaks is not installed; the push waits until it is.")
    return ScanResult(result.returncode == 0, "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part))


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, descendant], capture_output=True, check=False).returncode == 0


def fetch_remote(root: Path, remote: str) -> None:
    """Refresh the remote-tracking refs of ``remote`` and fetch the objects of its ``v1``, without touching a local tag."""
    git(root, "fetch", "-q", "--prune", "--no-tags", remote)
    subprocess.run(["git", "-C", str(root), "fetch", "-q", "--no-tags", remote, TAG], capture_output=True, check=False)


def remote_object(root: Path, remote: str) -> str:
    """The object ``refs/tags/v1`` of ``remote`` holds, empty when the tag does not exist; a local tag is never read."""
    for line in git(root, "ls-remote", remote, TAG).splitlines():
        sha, _, ref = line.partition("\t")
        if ref == TAG:
            return sha
    return ""


def peel(root: Path, sha: str) -> str:
    """The commit ``sha`` names, or ``sha`` itself when it cannot be peeled here."""
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else sha


def ci_refusals(runs: Mapping[str, CiRun]) -> list[str]:
    refusals = []
    for workflow, run in runs.items():
        if run.status is None:
            refusals.append(f"the CI of the target on main did not pass: {workflow} has no push run on main for the target.")
        elif run.status != "completed":
            refusals.append(f"CI is still running: {workflow} has not completed ({run.status}); run prepare again once it has.")
        elif run.conclusion != "success":
            refusals.append(f"the CI of the target on main did not pass: {workflow} concluded {run.conclusion}.")
    return refusals


@dataclass(frozen=True)
class Preparation:
    """The facts of a manual move of ``v1`` and the reasons it is refused; it may be asked only without refusals."""

    v1: str
    v1_object: str
    target: str
    kind: str
    surface_files: list[str]
    commits: list[str]
    ci: Mapping[str, CiRun]
    scan: ScanResult
    refusals: list[str]

    def report(self) -> str:
        current = f"{self.v1} (object {self.v1_object})" if self.v1 else "none: no remote v1 exists"
        lines = [f"Current v1: {current}", f"Target: {self.target}", f"Kind of move: {self.kind or 'none: the target and v1 are unrelated'}",
                 "Changed shared-surface files:", *(f"  {path}" for path in self.surface_files or ["(none)"]),
                 "Commits between:", *(f"  {commit}" for commit in self.commits or ["(none)"]), "CI on main of the target:"]
        for workflow in CI_WORKFLOWS:
            run = self.ci.get(workflow)
            state = "not read" if run is None else "no run" if run.status is None else run.conclusion if run.status == "completed" else f"running ({run.status})"
            lines.append(f"  {workflow}: {state}")
        lines.append(f"Gitleaks scan of the tag push: {'no findings' if self.scan.passed else 'did not pass'}")
        if not self.scan.passed:
            lines.extend(f"  {line}" for line in self.scan.output.splitlines())
        return "\n".join(lines)

    def question(self) -> str:
        files = ", ".join(self.surface_files)
        if self.kind == FIRST_CREATION:
            move_text = f"Create v1 at {self.target}, the first creation, which carries the shared-surface files {files}?"
        else:
            move_text = f"Move v1 from {self.v1} to {self.target}, a {self.kind} that changes the shared-surface files {files}?"
        return f"Question: {move_text} The move changes the later runs of every caller. Answer yes or no."

    def command(self, remote: str, root: Path | None) -> str:
        old = f"--old {shlex.quote(self.v1_object)}" if self.v1_object else "--old="
        parts = [f"{SCRIPT_COMMAND} promote {old} --new {self.target}"]
        if remote != "origin":
            parts.append(f"--remote {shlex.quote(remote)}")
        if root is not None:
            parts.append(f"--repository-root {shlex.quote(str(root))}")
        return " ".join(parts)


def prepare(root: Path, remote: str, commit: str, surface: dict, *, ci: Callable[[str], Mapping[str, CiRun]], scan: Callable[[str], ScanResult]) -> Preparation:
    """Fetch ``remote`` and gather the facts of moving ``v1`` to ``commit``, the head of its ``main`` when empty.

    ``ci`` reads the CI runs of the target once and ``scan`` runs the push gate's Gitleaks scan of the tag push.
    """
    fetch_remote(root, remote)
    v1_object = remote_object(root, remote)
    v1 = peel(root, v1_object) if v1_object else ""
    main_ref = f"refs/remotes/{remote}/main"
    target = git(root, "rev-parse", "--verify", f"{commit or main_ref}^{{commit}}")
    refusals = []
    if not is_ancestor(root, target, main_ref):
        refusals.append(f"the target {target} is not on {remote}/main.")
    paths = surface["paths"]
    commits: list[str] = []
    if not v1:
        kind = FIRST_CREATION
        surface_files = [line for line in git(root, "ls-tree", "-r", "--name-only", target, "--", *paths).splitlines() if line]
    else:
        kind = PROMOTION if is_ancestor(root, v1, target) else ROLLBACK if is_ancestor(root, target, v1) else ""
        if not kind:
            refusals.append(f"the target {target} is neither a descendant nor an ancestor of the current v1 commit {v1}.")
            surface_files = changed_files(root, v1, target, paths) if has_commit(root, v1) else []
        else:
            surface_files = changed_files(root, v1, target, paths)
            span = f"{v1}..{target}" if kind == PROMOTION else f"{target}..{v1}"
            commits = [line for line in git(root, "log", "--format=%h %s", span).splitlines() if line]
        if kind and not surface_files:
            refusals.append("the shared surface is identical at the current v1 and the target: no move is needed.")
    runs: Mapping[str, CiRun] = {}
    try:
        runs = ci(target)
    except (OSError, ValueError, RuntimeError) as error:
        refusals.append(f"the CI runs of the target on main could not be read: {error}")
    else:
        refusals.extend(ci_refusals(runs))
    scanned = scan(target)
    if not scanned.passed:
        refusals.append(f"{SCAN_REFUSAL}.")
    return Preparation(v1, v1_object, target, kind, surface_files, commits, runs, scanned, refusals)


def promote(root: Path, remote: str, old: str, new: str, *, scan: Callable[[str], ScanResult]) -> tuple[bool, str]:
    """Scan, then push ``new`` to ``v1`` under the lease on ``old``, the object ``prepare`` reported."""
    fetch_remote(root, remote)
    new_commit = git(root, "rev-parse", "--verify", f"{new}^{{commit}}")
    scanned = scan(new_commit)
    if not scanned.passed:
        return False, f"{SCAN_REFUSAL[0].upper()}{SCAN_REFUSAL[1:]}; nothing was pushed.\n{scanned.output}"
    old_commit = peel(root, old) if old else ""
    result = leased_push(root, remote, old, new_commit)
    if result.returncode != 0:
        try:
            held = remote_object(root, remote)
        except RuntimeError:
            held = None
        if held == old:
            return False, f"The push of v1 to {new_commit} failed; v1 still holds {old or 'nothing'}.\n{result.stderr.strip()}"
        return False, (f"The push of v1 to {new_commit} was rejected: refs/tags/v1 no longer holds {old or 'nothing'}, so v1 moved after prepare "
                       f"and keeps its newer target. Start again at prepare.\n{result.stderr.strip()}")
    if not old_commit:
        return True, f"v1 was created at {new_commit}."
    if not has_commit(root, old_commit):
        old_commit = f"the object {old}"
    return True, f"v1 moved from {old_commit} to {new_commit}. A later rollback moves v1 back to {old_commit}."


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
    prepare_parser = subcommands.add_parser("prepare", help="gather the facts of a manual move of v1 and print the question and the promote command")
    prepare_parser.add_argument("--commit", default="", help="the target commit; defaults to the head of <remote>/main after the fetch")
    prepare_parser.add_argument("--remote", default="origin")
    prepare_parser.add_argument("--repository-root", type=Path)
    promote_parser = subcommands.add_parser("promote", help="push the target to v1 under the lease that prepare printed")
    promote_parser.add_argument("--old", required=True, help="the object refs/tags/v1 held at prepare, empty for the first creation")
    promote_parser.add_argument("--new", required=True)
    promote_parser.add_argument("--remote", default="origin")
    promote_parser.add_argument("--repository-root", type=Path)
    return parser


def manual_move(arguments: argparse.Namespace) -> int:
    """``prepare`` or ``promote``: exit 0 only for a printed question and command, or for a pushed tag."""
    root = (arguments.repository_root or Path(".")).resolve()
    remote = arguments.remote
    scan = lambda commit: gitleaks_scan(root, remote, commit)  # noqa: E731
    try:
        if arguments.command == "promote":
            moved, text = promote(root, remote, arguments.old, arguments.new, scan=scan)
            print(text)
            return 0 if moved else 1
        preparation = prepare(root, remote, arguments.commit, load_surface(SURFACE_PATH), scan=scan,
                              ci=lambda commit: github_ci(github_repository(root, remote), commit))
    except RuntimeError as error:
        print(f"{arguments.command} stopped: {error}")
        return 1
    print(preparation.report())
    if preparation.refusals:
        print("\nprepare refuses; no question is asked:")
        print("\n".join(f"  - {reason}" for reason in preparation.refusals))
        return 1
    print(f"\n{preparation.question()}\n\nAfter a yes, run:\n{preparation.command(remote, arguments.repository_root)}")
    return 0


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
    if arguments.command in ("prepare", "promote"):
        return manual_move(arguments)
    moved, summary = move(arguments.repository_root.resolve(), arguments.remote, arguments.old, arguments.new)
    print(summary)
    _append(arguments.summary, summary)
    return 0 if moved else 1


if __name__ == "__main__":
    raise SystemExit(main())
