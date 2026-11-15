"""Promote ``v1``: automatically for a commit on the default branch whose changes stay in the read class, after its CI
on ``main``, and by hand for every other move.

    python3 promote_v1.py read-tag --repository OWNER/NAME [--output GITHUB_OUTPUT]
    python3 promote_v1.py decide --commit SHA --v1 SHA [--v1-object SHA] [--pull-request FILE] [--app-login LOGIN] [--owner LOGIN]
                                 [--repository-root DIR] [--surface shared-surface.json] [--output GITHUB_OUTPUT] [--summary FILE]
    python3 promote_v1.py await  --commit SHA --old OBJECT --repository OWNER/NAME [--output GITHUB_OUTPUT] [--summary FILE]
    python3 promote_v1.py move   --old SHA --new SHA [--deploy-key VARIABLE] [--remote origin] [--repository-root DIR] [--summary FILE]
    python3 promote_v1.py prepare [--commit SHA] [--remote origin] [--repository-root DIR]
    python3 promote_v1.py promote --old OBJECT --new SHA [--remote origin] [--repository-root DIR]
    python3 promote_v1.py caller  [--remote origin] [--repository-root DIR]

``shared-surface.json`` gives every path of the shared surface one promotion class under ``classes``; a file takes the
class of the longest recorded path that holds it. ``decide`` compares the shared surface at the current ``v1`` with the
commit: when every changed file is in the read class and ``v1`` is an ancestor of the commit, ``v1`` follows the commit.
When a changed file is in the write class or the template class, ``v1`` stays and the summary states the question for
a promotion by hand; when the shared surface is identical, the summary says that no promotion is needed; any other
stay gives the reason and asks for a manual promotion. Who made the commit does not count, so a merged Dependabot
pull request moves ``v1`` only when its changes stay in the read class.

One change outside the read class still moves ``v1``: the pins update. The pins file is in the write class, because
a workflow that writes installs the pinned tools. When it is the only changed file outside the read class, ``v1``
follows if the commit is the merge commit of a merged pull request that the updater App opened
from the updater's branch, that the repository owner merged, and that changes only the pins file. ``--pull-request``
is the JSON of that pull request as the GitHub API returns it, ``--app-login`` the App's login and ``--owner`` the
owner's. No other file and no other pull request takes this exception.

``await`` waits for the newest push runs on ``main`` of the CI workflows for the commit, read through the GitHub API
with the token in ``GH_TOKEN`` from this repository alone; ``v1`` follows only when every one concluded ``success``.
"Automatic v1 move" in ``docs/maintaining.md`` gives the polling interval and the bounds.

``read-tag`` reads ``refs/tags/v1`` through the GitHub API with the token in ``GH_TOKEN``, because the checkout
holds no credentials, and writes ``v1`` and ``v1_object`` as outputs, both empty when the tag does not exist.
``--v1`` is the commit the tag names and ``--v1-object`` the object the remote ref holds (the same for a lightweight
tag, the tag object for an annotated one); ``move`` pushes with ``--force-with-lease`` on that object, so a tag that
moved after the decision read it rejects the push.

``move`` pushes over SSH. ``--deploy-key`` names the environment variable that holds the
private deploy key of the push: for the length of the subcommand the key lies in a file that only the user of the
process reads, Git offers that key and no other identity, and it accepts ``github.com`` only under the host key pinned
here, :data:`GITHUB_HOST_KEY`. Without the option, Git pushes with what its environment gives it.

``prepare`` and ``promote`` carry every manual move of ``v1``: ``prepare`` reports the facts of the move, the
promotion class of every changed file among them, and prints the question for the user with the ``promote`` command
below it, or refuses, and ``promote`` pushes the tag after a yes. Right after a move that changes the template class,
``promote`` brings the sync caller of the management repository in line with ``v1`` and starts the sync there, through
the ``gh`` CLI and its own login: it reads the blob of ``.github/workflows/sync.yml`` on that repository's ``main``,
compares it with the blob of :data:`CALLER_PATH` in the Git object of the commit that ``v1`` names, replaces the file
with one commit only when the two differ, and then starts ``sync.yml`` with ``workflow_dispatch``. A read or a write
that fails starts no sync, and a commit without a caller at that path writes nothing. ``caller`` repeats the caller
update and the start against the remote ``v1`` without moving the tag. Only the local Git configuration of the
checkout names the management repository, under ``sharedworkflows.managementRepository``; nothing that these
subcommands print names it, and nothing that ``gh`` returns about it is printed. Beyond that read, that write and
that start they ask GitHub about no other repository and send none an event. "Promote v1 by hand" in
``docs/maintaining.md`` states what ``prepare`` reports, when it refuses and what ``promote`` and ``caller`` do.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Mapping

SURFACE_PATH = Path(__file__).resolve().parent / "shared-surface.json"
TAG = "refs/tags/v1"
CLASSES = ("read", "write", "template")
READ, TEMPLATE = "read", "template"
REF_URL = "https://api.github.com/repos/{repository}/git/ref/tags/v1"
TAG_OBJECT_URL = "https://api.github.com/repos/{repository}/git/tags/{sha}"
RUNS_URL = "https://api.github.com/repos/{repository}/actions/workflows/{workflow}/runs?head_sha={commit}&event=push&branch=main&per_page=100"
API_HOST = "https://api.github.com/"
GITHUB_REMOTE = re.compile(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?$")
GITLEAKS_OPTIONS = ("--config", ".gitleaks.toml", "--baseline-path", ".gitleaks-baseline.json")
SCRIPT_COMMAND = "python3 actions/repository-hygiene/promote_v1.py"
PROMOTION, ROLLBACK, FIRST_CREATION = "promotion", "rollback", "first creation"
SCAN_REFUSAL = "the Gitleaks scan of the tag push reports a finding or could not run"
CI_WORKFLOWS = ("ci.yml", "repository-hygiene-caller.yml", "python-tests-caller.yml")
# GitHub's Ed25519 host key, as https://api.github.com/meta lists it under ssh_keys.
GITHUB_HOST_KEY = "github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
MANAGEMENT_KEY = "sharedworkflows.managementRepository"
MANAGEMENT_SETTING = f"`git config --local {MANAGEMENT_KEY} <owner>/<name>`"
NO_MANAGEMENT = f"not started: this checkout names no management repository ({MANAGEMENT_SETTING})"
SYNC_WORKFLOW = "sync.yml"
# The sync caller in this repository, and the path it is installed at on the management repository's main.
CALLER_PATH = "actions/sync/caller.yml"
INSTALLED_CALLER = f".github/workflows/{SYNC_WORKFLOW}"
CALLER_MESSAGE = "chore: bring the sync caller in line with v1"
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
    """Whether ``v1`` follows the commit once its CI on ``main`` passed; ``question`` is the question for a promotion by
    hand when the commit's changes leave the read class."""

    follow: bool
    reason: str
    old: str
    new: str
    surface_identical: bool = False
    question: str = ""

    def summary(self) -> str:
        if self.follow:
            return f"## v1 promotion\n\n`v1` follows `{self.new}` once its CI on main passed: {self.reason}\n"
        target = self.old or "no v1 tag"
        if self.surface_identical:
            return f"## v1 promotion\n\n`v1` stays at `{target}`: the shared surface is identical between `v1` and this commit. No promotion is needed for this commit.\n"
        if self.question:
            return (f"## v1 promotion\n\n`v1` stays at `{target}`: {self.reason} A promotion by hand carries this commit.\n\n{self.question}\n\n"
                    f"`prepare` asks the question with the facts of the move:\n\n```text\n{SCRIPT_COMMAND} prepare --commit {self.new}\n```\n")
        return f"## v1 promotion\n\n`v1` stays at `{target}`: {self.reason} A manual promotion is needed for this commit.\n"


@dataclass(frozen=True)
class Outcome:
    """Whether ``v1`` follows the commit after the wait for its CI on ``main``."""

    move: bool
    reason: str
    old: str
    new: str

    def summary(self) -> str:
        if self.move:
            return f"## v1 promotion\n\n`v1` moves from `{self.old}` to `{self.new}`: {self.reason}\n"
        return f"## v1 promotion\n\n`v1` stays at `{self.old or 'no v1 tag'}`: {self.reason} A manual promotion is needed for this commit.\n"


class DeployKeyError(RuntimeError):
    """The environment variable that ``--deploy-key`` names holds no key."""


@contextlib.contextmanager
def deploy_key(variable: str) -> Iterator[None]:
    """Let every Git command that this process starts inside the block reach ``github.com`` over SSH with the deploy key
    that the environment variable ``variable`` holds, with no other identity and only under :data:`GITHUB_HOST_KEY`.

    The key lies in a file that only the user of this process reads and that the end of the block removes. Inside the
    block the variable itself is unset, so no command that this process starts holds the key in its environment.
    """
    value = os.environ.get(variable, "")
    if not value.strip():
        raise DeployKeyError(f"the environment variable {variable} holds no deploy key.")
    before = os.environ.get("GIT_SSH_COMMAND")
    with tempfile.TemporaryDirectory(prefix="promote-v1-", dir=os.environ.get("RUNNER_TEMP") or None) as directory:
        key, hosts = Path(directory) / "key", Path(directory) / "known-hosts"
        with os.fdopen(os.open(key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as handle:
            handle.write(value.rstrip("\n").encode("utf-8") + b"\n")
        hosts.write_bytes(f"{GITHUB_HOST_KEY}\n".encode("ascii"))
        os.environ["GIT_SSH_COMMAND"] = shlex.join(["ssh", "-F", "/dev/null", "-i", key.as_posix(), "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
                                                    "-o", f"UserKnownHostsFile={hosts.as_posix()}", "-o", "StrictHostKeyChecking=yes"])
        del os.environ[variable]
        try:
            yield
        finally:
            os.environ[variable] = value
            if before is None:
                del os.environ["GIT_SSH_COMMAND"]
            else:
                os.environ["GIT_SSH_COMMAND"] = before


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
        run = max(runs, key=lambda item: item["id"], default=None)
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
    texts = ("pins", "updater_branch")
    if not all(isinstance(surface.get(key), str) for key in texts) or not isinstance(surface.get("paths"), list) or not isinstance(surface.get("classes"), dict):
        raise ValueError(f"{path} must hold {', '.join(texts)}, paths and classes")
    return surface


def class_of(path: str, classes: Mapping[str, str]) -> str | None:
    """The promotion class of a path: that of the longest recorded path that holds it, None without one."""
    holders = [item for item in classes if path == item or path.startswith(item + "/")]
    value = classes[max(holders, key=len)] if holders else None
    return value if value in CLASSES else None


def by_class(files: list[str], classes: Mapping[str, str]) -> dict[str | None, list[str]]:
    """The files per promotion class, in the order read, write, template; the key None holds the files without a class."""
    grouped: dict[str | None, list[str]] = {}
    for name in (*CLASSES, None):
        members = [path for path in files if class_of(path, classes) == name]
        if members:
            grouped[name] = members
    return grouped


def class_text(grouped: Mapping[str | None, list[str]]) -> str:
    """The files of a move with their promotion classes, as one phrase."""
    return "; ".join(f"{', '.join(members)} ({f'{name} class' if name else 'no promotion class'})" for name, members in grouped.items())


def move_question(v1: str, target: str, kind: str, grouped: Mapping[str | None, list[str]]) -> str:
    """The question of a move of ``v1`` by hand, which names the changed shared-surface files with their classes."""
    files = class_text(grouped)
    if kind == FIRST_CREATION:
        move_text = f"Create v1 at {target}, the first creation, which carries the shared-surface files {files}?"
    else:
        move_text = f"Move v1 from {v1} to {target}, a {kind} that changes the shared-surface files {files}?"
    sync = (" It changes the template class, so right after it the management repository's caller is replaced when it differs from the caller at the "
            "target, and the sync starts.") if TEMPLATE in grouped else ""
    return f"Question: {move_text} The move changes the later runs of every caller.{sync} Answer yes or no."


def has_commit(root: Path, sha: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True, check=False).returncode == 0


def is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    return subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, descendant], capture_output=True, check=False).returncode == 0


def surface_matches_v1(root: Path, v1: str, commit: str, paths: list[str]) -> bool:
    """Whether the shared surface paths hold the same content at ``v1`` and ``commit``; False when ``v1`` is empty, absent or the comparison fails."""
    if not v1 or not has_commit(root, v1):
        return False
    try:
        return not changed_files(root, v1, commit, paths)
    except RuntimeError:
        return False


def pins_refusal(root: Path, commit: str, pull_request: dict | None, app_login: str, owner: str, surface: dict) -> str | None:
    """Why ``commit`` is not the pins update that moves automatically, or None when it is: the merge commit of a merged
    pull request that the updater App opened from the updater branch and the repository owner merged, changing only the pins file."""
    pins = surface["pins"]
    if pull_request is None:
        return "no merged pull request landed this commit"
    if pull_request.get("merged") is not True or pull_request.get("merge_commit_sha") != commit:
        return "the pull request found for this commit is not the merged pull request that produced it"
    try:
        commit_files = changed_files(root, f"{commit}^", commit)
    except RuntimeError as error:
        return f"the commit's changes could not be read: {error}"
    if commit_files != [pins]:
        return f"the commit changes {', '.join(commit_files) or 'nothing'}, not only {pins}"
    author = (pull_request.get("user") or {}).get("login")
    if not app_login or author != app_login:
        return f"the pull request was opened by {author or 'an unknown account'}, not by the updater App {app_login or '(unknown)'}"
    branch = (pull_request.get("head") or {}).get("ref")
    if branch != surface["updater_branch"]:
        return f"the pull request came from {branch or 'an unknown branch'}, not from the updater branch {surface['updater_branch']}"
    merged_by = (pull_request.get("merged_by") or {}).get("login")
    if not owner or merged_by != owner:
        return f"the pull request was merged by {merged_by or 'an unknown account'}, not by the repository owner {owner or '(unknown)'}"
    return None


def decide(root: Path, commit: str, v1: str, surface: dict, v1_object: str = "", *, pull_request: dict | None = None, app_login: str = "",
           owner: str = "") -> Decision:
    """Whether ``v1`` follows ``commit`` once its CI on ``main`` passed: when its changes compared with ``v1`` stay in the read class, or
    when the pins file is the only change outside it and the commit is the updater's pins update that the owner merged."""
    lease = v1_object or v1
    if surface_matches_v1(root, v1, commit, surface["paths"]):
        return Decision(False, "the shared surface is identical between v1 and this commit.", lease, commit, surface_identical=True)
    stay = lambda reason, question="": Decision(False, reason, lease, commit, question=question)  # noqa: E731
    if not v1:
        return stay("no v1 tag exists yet.")
    if not has_commit(root, v1):
        return stay(f"the current v1 commit {v1} is not available in this checkout.")
    if not is_ancestor(root, v1, commit):
        return stay(f"the current v1 commit {v1} is not an ancestor of this commit.")
    try:
        surface_files = changed_files(root, v1, commit, surface["paths"])
    except RuntimeError as error:
        return stay(f"the shared surface could not be compared: {error}")
    grouped = by_class(surface_files, surface["classes"])
    if None in grouped:
        return stay(f"the changes compared with v1 reach {', '.join(grouped[None])}, which the shared surface gives no promotion class.")
    outside = {name: members for name, members in grouped.items() if name != READ}
    follows = f"`v1` follows from `{lease}` once the CI of this commit on main passed."
    if [path for members in outside.values() for path in members] == [surface["pins"]]:
        # The one exception outside the read class: the reviewed pins update. Nothing else in the write class takes it.
        refusal = pins_refusal(root, commit, pull_request, app_login, owner, surface)
        if refusal is None:
            return Decision(True, f"a pins-only pull request of the updater App from {surface['updater_branch']}, merged by the repository owner, landed, and the "
                                  f"shared surface otherwise stays in the read class compared with `v1`. {follows}", lease, commit)
        return stay(f"the changes compared with v1 leave the read class: {class_text(outside)}. The pins file moves automatically only with the updater's "
                    f"pull request, and {refusal}.", move_question(v1, commit, PROMOTION, grouped))
    if outside:
        return stay(f"the changes compared with v1 leave the read class: {class_text(outside)}.", move_question(v1, commit, PROMOTION, grouped))
    return Decision(True, f"its changes compared with `v1` stay in the read class: {', '.join(surface_files)}. {follows}", lease, commit)


def follow(old: str, new: str, *, ci: Callable[[], Conclusions]) -> Outcome:
    """Whether ``v1`` follows the commit to ``new``; ``ci`` gives the conclusions of the CI on ``main``."""
    try:
        reason = ci_stay_reason(ci())
    except CiReadError as error:
        reason = f"the CI runs of this commit on main could not be read when the wait ended: {error}."
    if reason:
        return Outcome(False, reason, old, new)
    return Outcome(True, "the CI of this commit on main passed.", old, new)


def leased_push(root: Path, remote: str, old: str, new: str) -> subprocess.CompletedProcess:
    """Push ``new`` to ``refs/tags/v1`` only while the remote ref holds ``old``; an empty ``old`` requires that no tag exists."""
    return subprocess.run(
        ["git", "-C", str(root), "push", f"--force-with-lease={TAG}:{old}", remote, f"{new}:{TAG}"],
        capture_output=True, text=True, check=False,
    )


def move(root: Path, remote: str, old: str, new: str) -> tuple[bool, str]:
    """Push ``new`` to ``v1`` only while the remote tag still names ``old``; after a failed push, read the remote tag again
    to tell a lease rejection from a push that failed while ``v1`` kept ``old``."""
    result = leased_push(root, remote, old, new)
    if result.returncode == 0:
        return True, f"## v1 promotion\n\n`v1` moved from `{old}` to `{new}`.\n"
    try:
        held = remote_object(root, remote)
    except RuntimeError as error:
        outcome = (f"The push of `v1` to `{new}` failed and `refs/tags/v1` could not be read again, so the state of `v1` is unknown. "
                   f"`git ls-remote {remote} refs/tags/v1` reads it.\n\n{error}")
    else:
        if held != old:
            outcome = f"The push of `v1` to `{new}` was rejected; the tag no longer names `{old}` and keeps its newer target."
        elif old:
            outcome = f"The push of `v1` to `{new}` failed; `v1` still names `{old}`."
        else:
            outcome = f"The push of `v1` to `{new}` failed; no `v1` exists."
    return False, f"## v1 promotion\n\n{outcome}\n\n```\n{result.stderr.strip()}\n```\n"


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
    classes: Mapping[str, str] = field(default_factory=dict)
    management: bool = False
    caller_changes: bool = False

    @property
    def grouped(self) -> dict[str | None, list[str]]:
        return by_class(self.surface_files, self.classes)

    def report(self) -> str:
        current = f"{self.v1} (object {self.v1_object})" if self.v1 else "none: no remote v1 exists"
        lines = [f"Current v1: {current}", f"Target: {self.target}", f"Kind of move: {self.kind or 'none: the target and v1 are unrelated'}",
                 "Changed shared-surface files:",
                 *([f"  {path} ({name} class)" if name else f"  {path} (no promotion class)" for name, members in self.grouped.items() for path in members] or ["  (none)"]),
                 "Commits between:", *(f"  {commit}" for commit in self.commits or ["(none)"]), "CI on main of the target:"]
        for workflow in CI_WORKFLOWS:
            run = self.ci.get(workflow)
            state = "not read" if run is None else "no run" if run.status is None else run.conclusion if run.status == "completed" else f"running ({run.status})"
            lines.append(f"  {workflow}: {state}")
        lines.append(f"Gitleaks scan of the tag push: {'no findings' if self.scan.passed else 'did not pass'}")
        if not self.scan.passed:
            lines.extend(f"  {line}" for line in self.scan.output.splitlines())
        if TEMPLATE in self.grouped:
            follows = ("the management repository's caller is replaced when it differs from the caller at the target, then the sync starts" if self.management
                       else f"nothing: this checkout names no management repository ({MANAGEMENT_SETTING})")
            lines.append(f"Sync caller: {'changes between' if self.caller_changes else 'the same at'} the current v1 and the target")
            lines.append(f"Caller and sync after the move: {follows}")
        return "\n".join(lines)

    def question(self) -> str:
        return move_question(self.v1, self.target, self.kind, self.grouped)

    def command(self, remote: str, root: Path | None) -> str:
        old = f"--old {shlex.quote(self.v1_object)}" if self.v1_object else "--old="
        return script_command(f"promote {old} --new {self.target}", remote, root)


def script_command(subcommand: str, remote: str, root: Path | None) -> str:
    """The command line of a subcommand of this script, with the remote and the repository root that it was given."""
    parts = [f"{SCRIPT_COMMAND} {subcommand}"]
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
    management = bool(management_repository(root))
    caller_changes = caller_blob(root, v1) != caller_blob(root, target)
    if caller_changes and not management:
        refusals.append(f"the move changes the sync caller, {CALLER_PATH}, and this checkout names no management repository, whose caller would stay "
                        f"behind: name it with {MANAGEMENT_SETTING} and run prepare again.")
    return Preparation(v1, v1_object, target, kind, surface_files, commits, runs, scanned, refusals, surface["classes"], management, caller_changes)


def management_repository(root: Path) -> str:
    """OWNER/NAME of the management repository, which only the local Git configuration of this checkout names; empty without it."""
    result = subprocess.run(["git", "-C", str(root), "config", "--local", "--get", MANAGEMENT_KEY], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def gh_quiet(*arguments: str) -> subprocess.CompletedProcess | None:
    """Run the ``gh`` CLI with its own login, for a request about the management repository; None when it is not
    installed. What it returns can name that repository, so no caller of this function prints any of it."""
    try:
        return subprocess.run(["gh", *arguments], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None


def caller_blob(root: Path, commit: str) -> str:
    """The blob of the sync caller in the Git object of ``commit``; empty when the commit holds no file at
    :data:`CALLER_PATH`, is empty or is not available here. The working tree is never read."""
    if not commit:
        return ""
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "--verify", "--quiet", f"{commit}:{CALLER_PATH}"], capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def installed_caller(repository: str) -> str | None:
    """The blob of the caller that the management repository holds on its ``main``; None when the read failed or the
    repository holds no file there, so that a caller is only ever replaced and never created in a repository that the
    checkout names by mistake."""
    result = gh_quiet("api", f"repos/{repository}/contents/{INSTALLED_CALLER}?ref=main")
    if result is None:
        return None
    try:
        body = json.loads(result.stdout)
    except ValueError:
        return None
    if result.returncode != 0 or not isinstance(body, dict):
        return None
    sha = body.get("sha")
    return sha if isinstance(sha, str) and sha else None


def write_caller(root: Path, repository: str, blob: str, installed: str) -> bool:
    """Put the content of ``blob`` at the caller's path on the management repository's ``main``, as one commit that
    replaces the file whose blob is ``installed``."""
    content = subprocess.run(["git", "-C", str(root), "cat-file", "blob", blob], capture_output=True, check=False)
    if content.returncode != 0:
        return False
    fields = ["-f", f"message={CALLER_MESSAGE}", "-f", f"content={base64.b64encode(content.stdout).decode('ascii')}", "-f", "branch=main", "-f", f"sha={installed}"]
    result = gh_quiet("api", "--method", "PUT", f"repos/{repository}/contents/{INSTALLED_CALLER}", *fields)
    return result is not None and result.returncode == 0


def start_sync(repository: str) -> bool:
    """Start the sync in the management repository with ``workflow_dispatch`` on its ``main``, through the ``gh`` CLI."""
    result = gh_quiet("api", "--method", "POST", f"repos/{repository}/actions/workflows/{SYNC_WORKFLOW}/dispatches", "-f", "ref=main")
    return result is not None and result.returncode == 0


def reconcile_caller(root: Path, repository: str, commit: str, recovery: str) -> tuple[bool, str]:
    """Bring the management repository's caller in line with the caller in the Git object of ``commit``, the commit
    that ``v1`` names, and start the sync: whether all of it was done, and what to print. The file is written only
    when its blob differs, and the sync starts only after a caller that is known to equal the one at ``v1``."""
    blob = caller_blob(root, commit)
    if not blob:
        return False, (f"that commit holds no caller at {CALLER_PATH}, so nothing was written to the management repository, its caller stays as it is "
                       f"and no sync was started")
    repeat = f"repeat the caller update and the start of the sync with `{recovery}`"
    installed = installed_caller(repository)
    if installed is None:
        return False, f"the management repository's caller could not be read, so nothing was written and no sync was started; {repeat}"
    if installed == blob:
        state = "the management repository's caller equals the caller at v1 and stays as it is"
    elif write_caller(root, repository, blob, installed):
        state = "the management repository's caller was replaced with the caller at v1"
    else:
        return False, f"the management repository's caller differs from the caller at v1 and could not be replaced, so no sync was started; {repeat}"
    if not start_sync(repository):
        return False, f"{state}, but the start of the sync failed; {repeat}"
    return True, f"{state}, and the sync was started"


def caller(root: Path, remote: str, recovery: str) -> tuple[bool, str]:
    """Repeat the caller update and the start of the sync against the commit that the remote ``v1`` names, without
    moving the tag."""
    fetch_remote(root, remote)
    v1_object = remote_object(root, remote)
    if not v1_object:
        return False, "No remote v1 exists, so no caller was read or written and no sync was started."
    commit = peel(root, v1_object)
    repository = management_repository(root)
    if not repository:
        return False, f"v1 names {commit}. Nothing was read, written or started: this checkout names no management repository ({MANAGEMENT_SETTING})."
    done, text = reconcile_caller(root, repository, commit, recovery)
    return done, f"v1 names {commit}: {text}."


def moved_files(root: Path, old_commit: str, new_commit: str, paths: list[str]) -> list[str] | None:
    """The shared-surface files that a move from ``old_commit`` to ``new_commit`` changes, every file of the surface for
    the first creation; None when the old commit is not available here."""
    if not old_commit:
        return [line for line in git(root, "ls-tree", "-r", "--name-only", new_commit, "--", *paths).splitlines() if line]
    if not has_commit(root, old_commit):
        return None
    return changed_files(root, old_commit, new_commit, paths)


def sync_after_move(root: Path, old_commit: str, new_commit: str, surface: dict, recovery: str) -> tuple[bool, str]:
    """After a move that changed the template class, bring the management repository's caller in line with ``v1`` and
    start the sync; whether nothing is left to do, and what to print."""
    files = moved_files(root, old_commit, new_commit, surface["paths"])
    if files is not None and TEMPLATE not in by_class(files, surface["classes"]):
        return True, ""
    changed = "The move changes the template class" if files is not None else "Whether the move changes the template class is unknown here"
    repository = management_repository(root)
    if not repository:
        new_caller = caller_blob(root, new_commit)
        if files is None:
            # Without the old commit its caller is unknown: only a new commit without a caller rules a change out.
            caller_text = " and may change the sync caller" if new_caller else ""
        else:
            caller_text = " and the sync caller" if caller_blob(root, old_commit) != new_caller else ""
        if not caller_text:
            return True, f" {changed}; the sync was {NO_MANAGEMENT}."
        return False, (f" {changed}{caller_text}, but this checkout names no management repository, so its caller stays behind and no sync was "
                       f"started: name it with {MANAGEMENT_SETTING} and run `{recovery}`.")
    done, text = reconcile_caller(root, repository, new_commit, recovery)
    return done, f" {changed}: {text}."


def promote(root: Path, remote: str, old: str, new: str, surface: dict, *, scan: Callable[[str], ScanResult],
            recovery: str = f"{SCRIPT_COMMAND} caller") -> tuple[bool, str]:
    """Scan, then push ``new`` to ``v1`` under the lease on ``old``, the object ``prepare`` reported, and right after a
    move that changes the template class bring the management repository's caller in line with ``v1`` and start the
    sync. False with a pushed tag means that the caller update or the start is still to do; ``recovery`` is the
    command that repeats both."""
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
    done, sync = sync_after_move(root, old_commit, new_commit, surface, recovery)
    if not old_commit:
        return done, f"v1 was created at {new_commit}.{sync}"
    if not has_commit(root, old_commit):
        old_commit = f"the object {old}"
    return done, f"v1 moved from {old_commit} to {new_commit}. A later rollback moves v1 back to {old_commit}.{sync}"


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
    decide_parser.add_argument("--pull-request", type=Path, help="JSON of the merged pull request that landed the commit; omitted when none did")
    decide_parser.add_argument("--app-login", default="", help="the login of the updater App, for the pins update")
    decide_parser.add_argument("--owner", default="", help="the login of the repository owner, for the pins update")
    decide_parser.add_argument("--surface", type=Path, default=SURFACE_PATH)
    decide_parser.add_argument("--output", type=Path)
    decide_parser.add_argument("--summary", type=Path)
    key_help = "the environment variable that holds the private deploy key of the push; the push then goes over SSH with that key alone"
    await_parser = subcommands.add_parser("await", help="wait for the CI on main and decide whether v1 follows the commit")
    await_parser.add_argument("--commit", required=True)
    await_parser.add_argument("--old", required=True, help="the object refs/tags/v1 held at the decision")
    await_parser.add_argument("--repository", required=True, help="OWNER/NAME of the repository whose CI runs of the commit are read")
    await_parser.add_argument("--output", type=Path)
    await_parser.add_argument("--summary", type=Path)
    move_parser = subcommands.add_parser("move")
    move_parser.add_argument("--deploy-key", default="", metavar="VARIABLE", help=key_help)
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
    caller_parser = subcommands.add_parser("caller", help="bring the management repository's sync caller in line with the remote v1 and start the sync")
    caller_parser.add_argument("--remote", default="origin")
    caller_parser.add_argument("--repository-root", type=Path)
    return parser


def manual_move(arguments: argparse.Namespace) -> int:
    """``prepare``, ``promote`` or ``caller``: exit 0 only for a printed question and command, for a pushed tag whose
    caller update and sync, when the move needs them, were done or have no management repository and no changed caller,
    or for a caller that equals the one at ``v1`` with the sync started."""
    root = (arguments.repository_root or Path(".")).resolve()
    remote = arguments.remote
    scan = lambda commit: gitleaks_scan(root, remote, commit)  # noqa: E731
    recovery = script_command("caller", remote, arguments.repository_root)
    try:
        if arguments.command == "caller":
            done, text = caller(root, remote, recovery)
            print(text)
            return 0 if done else 1
        if arguments.command == "promote":
            moved, text = promote(root, remote, arguments.old, arguments.new, load_surface(SURFACE_PATH), scan=scan, recovery=recovery)
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
    arguments = build_parser().parse_args(argv)
    variable = getattr(arguments, "deploy_key", "")
    if not variable:
        return run(arguments)
    try:
        with deploy_key(variable):
            return run(arguments)
    except DeployKeyError as error:
        print(f"{arguments.command} stopped: {error}")
        return 1


def run(arguments: argparse.Namespace) -> int:
    """The subcommand that the command line names."""
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
        decision = decide(arguments.repository_root.resolve(), arguments.commit, arguments.v1, load_surface(arguments.surface), arguments.v1_object,
                          pull_request=pull_request, app_login=arguments.app_login, owner=arguments.owner)
        print(decision.summary())
        _append(arguments.summary, decision.summary())
        _append(arguments.output, f"follow={str(decision.follow).lower()}\nold={decision.old}\nnew={decision.new}\n")
        return 0
    if arguments.command == "await":
        outcome = follow(arguments.old, arguments.commit, ci=lambda: wait_for_ci(arguments.repository, arguments.commit, fetch))
        print(outcome.summary())
        _append(arguments.summary, outcome.summary())
        _append(arguments.output, f"move={str(outcome.move).lower()}\nold={outcome.old}\nnew={outcome.new}\n")
        return 0
    if arguments.command in ("prepare", "promote", "caller"):
        return manual_move(arguments)
    moved, summary = move(arguments.repository_root.resolve(), arguments.remote, arguments.old, arguments.new)
    print(summary)
    _append(arguments.summary, summary)
    return 0 if moved else 1


if __name__ == "__main__":
    raise SystemExit(main())
