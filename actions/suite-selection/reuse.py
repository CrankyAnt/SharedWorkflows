"""The push lookup, the reuse key and the result store of the suite selection.

The push lookup finds the pull request of a pushed commit through ``commits/<sha>/pulls``, reads the tested merge commit
from the ``actions/checkout`` log of that pull request's last successful run of the calling workflow, and verifies it
from its parents. It decides the case of the push: its tree equals the passed tested tree, its pull request was behind,
the selection runs over the pull request's files, or over the push's own changed files. The shared lookup reads the
digest of the shared recording step that the last passed run on the base branch took. The reuse key of a unit holds
the content of its reads in the tested tree, its command, the versions its setups pin, the runner label of its job, the
content of every unrecorded input that selects it (``unrecorded.py``) and the digest of the shared recording step.
``store`` writes the record of the passed results of a run, from the ``needs`` context of the closing job and the
passed entry files of each job. ``suite_selection.py`` runs the commands; ``docs/maintaining.md`` documents them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from revisions import ALL_ZERO, commit_id, git
from suite_selection import module_name

if TYPE_CHECKING:
    from suite_selection import IgnoreRules

LOG_LINE = re.compile(r"HEAD is now at ([0-9a-f]{7,40}) Merge ([0-9a-f]{40}) into ([0-9a-f]{40})")
FETCH_LINE = re.compile(r"\+([0-9a-f]{40}):refs/remotes/pull/([0-9]+)/merge\b")  # git/commits answers only a full id
DIGEST_LINE = re.compile(r"shared CI digest ([0-9a-f]{64})\b")  # printed by ``recording.py digest``
DIGEST_STEP = "Take the digest of the shared recording step"  # the step of suite-selection.yml that prints it
BRANCH_RUNS = 20  # the successful runs of the base branch that the shared lookup reads
BRANCH_EVENTS = ("push", "repository_dispatch")
CASES = ("identical", "behind", "pull-request", "own")
API_TIMEOUT = 20  # seconds per request


class ApiError(Exception):
    """A request to the GitHub API failed; the message names the request."""


class RecordError(Exception):
    """A lookup, a record or a passed-entry file cannot be read."""


@dataclass(frozen=True)
class Lookup:
    case: str  # one of CASES
    head: str  # the pushed commit
    landing_base: str | None  # the commit the push landed on, or None when it is unknown
    reason: str
    pull_request: int | None = None
    tested_merge: str | None = None
    tested_base: str | None = None

    def document(self) -> dict:
        return {"case": self.case, "head": self.head, "landing_base": self.landing_base, "reason": self.reason,
                "pull_request": self.pull_request, "tested_merge": self.tested_merge, "tested_base": self.tested_base}


def read_lookup(path: Path) -> Lookup:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RecordError(f"the push lookup {path} cannot be read: {error}") from error
    if not isinstance(raw, dict) or raw.get("case") not in CASES or not isinstance(raw.get("head"), str) or not raw["head"]:
        raise RecordError(f"the push lookup {path} must name a case and the pushed commit")
    if raw["case"] == "behind" and not (isinstance(raw.get("tested_base"), str) and isinstance(raw.get("landing_base"), str)):
        raise RecordError(f"the push lookup {path} of a behind pull request must name the tested base and the landing base")
    return Lookup(raw["case"], raw["head"], raw.get("landing_base") or None, str(raw.get("reason", "")), raw.get("pull_request"),
                  raw.get("tested_merge"), raw.get("tested_base"))


# --- the GitHub API -------------------------------------------------------------------------------------------


class _KeepRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


class Api:
    """GET requests to the repository's REST API with the job's token."""

    def __init__(self, url: str, repository: str, token: str | None) -> None:
        self.base = url.rstrip("/") + f"/repos/{repository}/"
        self.token = token
        self.opener = urllib.request.build_opener(_KeepRedirect)
        self.calls = 0

    def _get(self, path: str) -> tuple[bytes, str | None]:
        """The body of a response, or the location of a redirect."""
        request = urllib.request.Request(self.base + path, headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        self.calls += 1
        try:
            with self.opener.open(request, timeout=API_TIMEOUT) as response:
                return response.read(), None
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in (301, 302, 303, 307, 308) and error.headers.get("Location"):
                return b"", error.headers["Location"]
            raise ApiError(f"GET {path} answered {error.code}") from error
        except (urllib.error.URLError, OSError) as error:
            raise ApiError(f"GET {path} failed: {error}") from error

    def json(self, path: str):
        body, location = self._get(path)
        if location is not None:
            raise ApiError(f"GET {path} redirected")
        try:
            return json.loads(body)
        except json.JSONDecodeError as error:
            raise ApiError(f"GET {path} returned no JSON") from error

    def text(self, path: str) -> str:
        """A text body; a redirect, such as that of a job log, is followed once without the token."""
        body, location = self._get(path)
        if location is not None:
            try:
                with urllib.request.urlopen(urllib.parse.urljoin(self.base + path, location), timeout=API_TIMEOUT) as response:
                    body = response.read()
            except (urllib.error.URLError, OSError) as error:
                raise ApiError(f"GET {path} failed after its redirect: {error}") from error
        return body.decode("utf-8", errors="replace")


# --- the push lookup ------------------------------------------------------------------------------------------


def landing_base(root: Path, sha: str, before: str | None) -> str | None:
    """The commit the push landed on: the previous head of the push, or the first parent of the commit."""
    if before and not ALL_ZERO.match(before):
        commit = commit_id(root, before)
        if commit is not None:
            return commit
    return commit_id(root, f"{sha}^1")


def lookup(root: Path, api: Api, workflow: str, sha: str, before: str | None = None, run_attempt: int = 1) -> Lookup:
    """The case of a pushed commit. ``workflow`` is the file name of the calling workflow, whose last successful
    pull-request run names the tested merge commit. A failed request never skips: it falls back to a selection."""
    landing = landing_base(root, sha, before)
    if run_attempt > 1:
        return Lookup("own", sha, landing, f"attempt {run_attempt} is a re-run, which selects over the push's own changed files")
    try:
        pulls = api.json(f"commits/{sha}/pulls")
    except ApiError as error:
        return Lookup("own", sha, landing, f"the pull request of {sha} could not be looked up: {error}")
    if not isinstance(pulls, list) or not pulls:
        return Lookup("own", sha, landing, f"{sha} belongs to no pull request")
    pull = next((item for item in pulls if isinstance(item, dict) and item.get("merge_commit_sha") == sha), pulls[0])
    number, head = pull.get("number"), (pull.get("head") or {}).get("sha")
    if not isinstance(number, int) or not isinstance(head, str):
        return Lookup("own", sha, landing, f"the pull request of {sha} names no head commit")

    def fallback(reason: str, **found) -> Lookup:
        return Lookup("pull-request", sha, landing, reason, number, **found)

    try:
        runs = api.json(f"actions/workflows/{urllib.parse.quote(workflow)}/runs?head_sha={head}&event=pull_request&status=success&per_page=1")
        run = (runs.get("workflow_runs") or [None])[0] if isinstance(runs, dict) else None
        if not isinstance(run, dict) or not isinstance(run.get("id"), int):
            return fallback(f"pull request #{number} has no successful run of `{workflow}` on its head {head}")
        jobs = api.json(f"actions/runs/{run['id']}/jobs?per_page=100")
        job = next((item for item in (jobs.get("jobs") or []) if isinstance(item, dict)
                    and any("actions/checkout" in str(step.get("name", "")) for step in item.get("steps") or [])), None)
        if job is None:
            return fallback(f"run {run['id']} of pull request #{number} has no job with a checkout")
        log = api.text(f"actions/jobs/{job['id']}/logs")
        match = next((found for found in LOG_LINE.finditer(log) if found.group(2) == head), None)
        if match is None:
            return fallback(f"the checkout log of run {run['id']} names no tested merge commit of {head}")
        short, base = match.group(1), match.group(3)
        full = next((found.group(1) for found in FETCH_LINE.finditer(log)
                     if found.group(1).startswith(short) and found.group(2) == str(number)), None)
        if full is None:
            return fallback(f"the checkout log of run {run['id']} holds no full id of the tested merge commit {short} of {head}")
        merge = api.json(f"git/commits/{full}")
    except ApiError as error:
        return fallback(f"the tested merge commit of pull request #{number} could not be read: {error}")
    parents = [parent.get("sha") for parent in merge.get("parents") or []] if isinstance(merge, dict) else []
    tree = (merge.get("tree") or {}).get("sha") if isinstance(merge, dict) else None
    if not isinstance(merge, dict) or merge.get("sha") != full or parents != [base, head] or not isinstance(tree, str):
        return fallback(f"the tested merge commit {full} of run {run['id']} does not verify as the merge of {head} into {base} with a tree")
    found = dict(tested_merge=full, tested_base=base)
    pushed_tree = git(root, "rev-parse", f"{sha}^{{tree}}")
    if pushed_tree.returncode == 0 and pushed_tree.stdout.strip() == tree:
        return Lookup("identical", sha, landing, f"the tree of {sha} equals the tested tree of {full}, which passed in run {run['id']} of pull request #{number}",
                      number, **found)
    if landing is not None and base != landing and git(root, "merge-base", "--is-ancestor", base, landing).returncode == 0:
        return Lookup("behind", sha, landing, f"pull request #{number} was tested on {base} and landed on {landing}", number, **found)
    return fallback(f"the tree of {sha} differs from the tested tree of {full}, and its tested base {base} is no earlier commit of the landing base", **found)


@dataclass(frozen=True)
class SharedLookup:
    """The digest of the shared recording step that a run took, and the one the last passed run on its base branch
    took; ``previous`` is None when that run or its digest cannot be found."""

    digest: str
    previous: str | None
    reason: str
    run: int | None = None

    @property
    def moved(self) -> bool:
        return self.previous != self.digest

    def document(self) -> dict:
        return {"digest": self.digest, "previous": self.previous, "reason": self.reason, "run": self.run}


def read_shared_lookup(path: Path) -> SharedLookup:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RecordError(f"the shared lookup {path} cannot be read: {error}") from error
    digest = raw.get("digest") if isinstance(raw, dict) else None
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RecordError(f"the shared lookup {path} must name the digest of the shared recording step")
    previous = raw.get("previous")
    return SharedLookup(digest, previous if isinstance(previous, str) else None, str(raw.get("reason", "")),
                         raw.get("run") if isinstance(raw.get("run"), int) else None)


def shared_lookup(api: Api, workflow: str, branch: str, digest: str) -> SharedLookup:
    """The digest that the last passed push or dispatch run of the calling workflow on the base branch took, read from the
    log of its job with the digest step. A run, a step or a log line that cannot be found leaves ``previous`` empty, so
    that every suite runs."""
    try:
        runs = api.json(f"actions/workflows/{urllib.parse.quote(workflow)}/runs?branch={urllib.parse.quote(branch)}&status=success&per_page={BRANCH_RUNS}")
        run = next((item for item in (runs.get("workflow_runs") or [] if isinstance(runs, dict) else [])
                    if isinstance(item, dict) and item.get("event") in BRANCH_EVENTS and isinstance(item.get("id"), int)), None)
        if run is None:
            return SharedLookup(digest, None, f"no push or dispatch run of `{workflow}` on `{branch}` passed among its last {BRANCH_RUNS} successful runs")
        jobs = api.json(f"actions/runs/{run['id']}/jobs?per_page=100")
        job = next((item for item in (jobs.get("jobs") or [] if isinstance(jobs, dict) else []) if isinstance(item, dict)
                    and any(step.get("name") == DIGEST_STEP for step in item.get("steps") or [] if isinstance(step, dict))), None)
        if job is None:
            return SharedLookup(digest, None, f"run {run['id']} on `{branch}` took no digest of the shared recording step", run["id"])
        match = DIGEST_LINE.search(api.text(f"actions/jobs/{job['id']}/logs"))
    except ApiError as error:
        return SharedLookup(digest, None, f"the last passed run on `{branch}` could not be read: {error}")
    if match is None:
        return SharedLookup(digest, None, f"the log of run {run['id']} on `{branch}` names no digest of the shared recording step", run["id"])
    previous = match.group(1)
    comparison = "equals" if previous == digest else "differs from"
    return SharedLookup(digest, previous, f"the shared recording step {comparison} that of run {run['id']}, the last passed run on `{branch}`", run["id"])


def shared_markdown(found: SharedLookup) -> str:
    text = "Every suite runs: " if found.moved else "The selection runs as usual: "
    return f"## Shared recording step\n\n{text}{found.reason}. This run's digest: `{found.digest}`.\n"


def lookup_markdown(found: Lookup) -> str:
    text = {"identical": "No suite runs: ", "behind": "Only units that both sides affect run: ",
            "pull-request": "The selection runs over the pull request's files: ", "own": "The selection runs over the push's own changed files: "}
    return f"## Push lookup\n\n{text[found.case]}{found.reason}.\n"


# --- the reuse key --------------------------------------------------------------------------------------------


def tree_entries(root: Path, revision: str = "HEAD") -> dict[str, str]:
    """Every tracked path of a revision with its mode and object id."""
    listing = git(root, "ls-tree", "-r", "-z", "--full-tree", revision)
    if listing.returncode != 0:
        raise RecordError(f"the tested tree {revision} could not be read: {listing.stderr.strip()}")
    entries = {}
    for item in listing.stdout.split("\0"):
        if item:
            meta, path = item.split("\t", 1)
            mode, _, object_id = meta.split(" ")
            entries[path] = f"{mode} {object_id}"
    return entries


def membership_names(membership: str, files: frozenset[str], matches) -> list[str]:
    """The names a directory membership or a glob membership pattern sees in a set of tracked files."""
    if any(character in membership for character in ("*", "?")):
        directories = {path[:index] for path in files for index, char in enumerate(path) if char == "/"}
        candidates = directories if membership.endswith("/") else set(files) | directories
        return sorted(path for path in candidates if matches(membership.rstrip("/"), path))
    prefix = "" if membership in ("", ".") else membership.strip("/") + "/"
    return sorted({path[len(prefix):].split("/", 1)[0] for path in files if path.startswith(prefix)})


def exists_in(path: str, kind: str, files: frozenset[str]) -> bool:
    path = path.replace("\\", "/").strip("/")
    file = path in files
    directory = path == "." or any(name.startswith(path + "/") for name in files)
    return file if kind == "file" else directory if kind == "directory" else file or directory


class Tree:
    """The tested tree, with each pattern's matching paths computed once."""

    def __init__(self, entries: dict[str, str], matches) -> None:
        self.entries = entries
        self.files = frozenset(entries)
        self.matches = matches
        self._matching: dict[str, tuple[str, ...]] = {}

    def matching(self, pattern: str) -> tuple[str, ...]:
        if pattern not in self._matching:
            self._matching[pattern] = tuple(path for path in sorted(self.entries) if self.matches(pattern, path))
        return self._matching[pattern]

    def reads(self, patterns, memberships, existence, lookups=()) -> dict:
        """The content of a unit's reads: the paths its patterns match with their objects, the names its memberships
        see, the value of its existence reads, and per directory in which it looks up modules the names that it looks
        up there with the names in it that carry one of them. ``lookups`` holds (directory, module names) pairs."""
        files = sorted({path for pattern in patterns for path in self.matching(pattern)})
        return {"patterns": sorted(set(patterns)), "files": [[path, self.entries[path]] for path in files],
                "memberships": [[membership, membership_names(membership, self.files, self.matches)] for membership in sorted(set(memberships))],
                "existence": sorted([read.path, read.kind, exists_in(read.path, read.kind, self.files)] for read in set(existence)),
                "lookups": [[directory, sorted(names), [name for name in membership_names(directory, self.files, self.matches)
                                                         if module_name(name) in names]] for directory, names in sorted(lookups)]}

    def ignore_content(self, rules: IgnoreRules) -> dict:
        """The content of the ignore rules: the tracked ``.gitignore`` files and the tracked paths that they match, with
        their objects."""
        return {"files": [[path, self.entries[path]] for path in sorted(rules.files) if path in self.entries],
                "matched": [[path, self.entries[path]] for path in sorted(self.entries) if rules.ignored(path)]}


def content_digest(document: object) -> str:
    """The SHA-256 of a JSON document, independent of the order of its keys."""
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def unit_key(reads: dict, command, setups, versions: dict[str, str], label: str, shared_digest: str = "", *,
             ignore_content: dict, unrecorded: dict | None = None) -> str:
    """The reuse key: the content of the reads, the command, the versions the setups pin, the runner label, the digest
    of the shared recording step, the content of the ignore rules and the content of the unit's unrecorded inputs."""
    return content_digest({"reads": reads, "command": list(command or ()), "setups": {setup: versions.get(setup, "") for setup in setups},
                           "image": label, "shared_digest": shared_digest, "ignore_content": ignore_content, "unrecorded": unrecorded or {}})


def read_record(path: Path | None) -> dict[str, int]:
    """The passed results of a record: unit key to the run it passed in; a missing file holds none."""
    if path is None or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RecordError(f"the result record {path} cannot be read: {error}") from error
    passed = raw.get("passed") if isinstance(raw, dict) else None
    if not isinstance(passed, dict) or not all(isinstance(key, str) and isinstance(run, int) for key, run in passed.items()):
        raise RecordError(f"the result record {path} must hold passed, an object from unit key to run id")
    return passed


# --- the store ------------------------------------------------------------------------------------------------


def passed_entries(directory: Path) -> set[tuple[str, str]]:
    """The entry files that passed in a run's jobs, from every JSON file under the directory."""
    passed = set()
    for path in sorted(directory.rglob("*.json")) if directory.is_dir() else ():
        try:
            items = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            raise RecordError(f"the passed entry files {path} cannot be read: {error}") from error
        if not isinstance(items, list) or not all(isinstance(item, dict) and isinstance(item.get("suite"), str)
                                                  and isinstance(item.get("entry"), str) for item in items):
            raise RecordError(f"{path} must be a list of objects with a suite and an entry")
        passed.update((item["suite"], item["entry"]) for item in items)
    return passed


def store(needs: dict, passed: set[tuple[str, str]], run_id: int) -> dict[str, int] | None:
    """The record of a run: the results ``select`` reused, and every unit that ran and passed with all its entry
    files. None when the selection did not succeed, so that nothing is stored."""
    selection = needs.get("select")
    if not isinstance(selection, dict) or selection.get("result") != "success":
        return None
    outputs = selection.get("outputs") if isinstance(selection.get("outputs"), dict) else {}
    try:
        units = json.loads(outputs.get("units") or "{}")
        reused = json.loads(outputs.get("reused") or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(units, dict) or not isinstance(reused, dict):
        return None
    record = {key: run for key, run in reused.items() if isinstance(run, int)}
    for key, unit in units.items():
        if isinstance(unit, dict) and unit.get("entries") and all((unit.get("suite"), entry) in passed for entry in unit["entries"]):
            record[key] = run_id
    return record


def environment_api() -> Api:
    return Api(os.environ.get("GITHUB_API_URL", "https://api.github.com"), os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN"))
