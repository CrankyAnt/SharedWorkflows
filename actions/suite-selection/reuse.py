"""The push lookup, the reuse key and the result store of the suite selection.

The push lookup finds the pull request of a pushed commit through ``commits/<sha>/pulls``, reads the structured
``tested-state`` artifact of that pull request's last successful code run of the calling workflow, and verifies the
tested merge commit from its parents through ``git/commits/<merge>``. It decides the case of the push: its tree equals
the passed tested tree, its pull request was behind (checking the restored default-branch record to tell whether the
landing base passed), the selection runs over the pull request's files, or over the push's own changed files. The
shared lookup reads the shared CI digests directly from the restored default-branch record without log scraping or a
fixed walk limit. A missing, evicted or unreadable record or digest counts as changed and emits a counted fallback
notice by reason. On an identical or behind merge, the digests that the pull request was tested under decide in place
of the base branch's digests, and unreadable tested digests count as changed. The reuse key of a unit holds the
content of its reads in the tested tree, its command, the versions its setups pin, the runner label of its job, the
content of every unrecorded input that selects it (``unrecorded.py``) and the shared CI digests of its platform, tools
and language steps.
``store`` writes the record of the passed results of a pull-request run or, with ``--default-branch``, the structured
default-branch record (version, run_id, commit, tree, digests, passed) for a passed default-branch run.
``suite_selection.py`` runs the commands; ``docs/maintaining.md`` documents them.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from revisions import ALL_ZERO, commit_id, git
from suite_selection import module_name

if TYPE_CHECKING:
    from suite_selection import IgnoreRules

DIGEST_STEP = "Take the shared CI digests"  # the step of plan / suite-selection.yml that prints them
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
FULL_SHA = re.compile(r"[0-9a-f]{40}")
DEFAULT_RECORD_VERSION = 1
TESTED_STATE_VERSION = 1
TESTED_STATE_ARTIFACT = "tested-state"
TESTED_STATE_FILE = "tested-state.json"
BRANCH_EVENTS = ("push", "repository_dispatch")
CASES = ("identical", "behind", "pull-request", "own")
FAILING_JOB_RESULTS = ("failure", "cancelled", "timed_out")
API_TIMEOUT = 20  # seconds per request


def fallback_notice(reason: str, detail: str) -> str:
    """GitHub Actions notice emitted whenever selection or lookup falls back to running more."""
    return f"::notice title=ci-fallback::{reason}: {detail}"


class ApiError(Exception):
    """A request to the GitHub API failed; the message names the request."""


class RecordError(Exception):
    """A lookup, a record or a passed-entry file cannot be read."""


def parse_digests(value: object) -> dict[str, str] | None:
    """Validate a shared CI digests mapping (JSON string or dict of non-empty name to 64-hex SHA-256).
    An unreadable or malformed value returns None."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    if (not isinstance(value, dict) or not value
            or not all(isinstance(k, str) and k and isinstance(v, str) and HEX_DIGEST.fullmatch(v) for k, v in value.items())):
        return None
    return {k: value[k] for k in sorted(value)}


def moved_digest_names(current: dict[str, str], other: dict[str, str]) -> set[str]:
    """Digest names whose value differs between ``current`` and ``other``; a name absent on either side counts as moved."""
    return {name for name in set(current) | set(other) if current.get(name) != other.get(name)}


@dataclass(frozen=True)
class Lookup:
    case: str  # one of CASES
    head: str  # the pushed commit
    landing_base: str | None  # the commit the push landed on, or None when it is unknown
    reason: str
    pull_request: int | None = None
    tested_merge: str | None = None
    tested_base: str | None = None
    tested_run: int | None = None  # the passed pull-request run of an identical or behind merge
    tested_digests: dict[str, str] | None = None  # the shared CI digests that run took
    landing_base_passed: bool | None = None  # for behind: whether the landing base has a passed default-branch record
    fallback: str | None = None  # machine-readable fallback reason when selecting more

    def document(self) -> dict:
        return {"case": self.case, "head": self.head, "landing_base": self.landing_base, "reason": self.reason,
                "pull_request": self.pull_request, "tested_merge": self.tested_merge, "tested_base": self.tested_base,
                "tested_run": self.tested_run, "tested_digests": self.tested_digests,
                "landing_base_passed": self.landing_base_passed, "fallback": self.fallback}


def read_lookup(path: Path) -> Lookup:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RecordError(f"the push lookup {path} cannot be read: {error}") from error
    if not isinstance(raw, dict) or raw.get("case") not in CASES or not isinstance(raw.get("head"), str) or not raw["head"]:
        raise RecordError(f"the push lookup {path} must name a case and the pushed commit")
    if raw["case"] == "behind" and not (isinstance(raw.get("tested_base"), str) and isinstance(raw.get("landing_base"), str)):
        raise RecordError(f"the push lookup {path} of a behind pull request must name the tested base and the landing base")
    passed_base = raw.get("landing_base_passed")
    fallback = raw.get("fallback")
    return Lookup(raw["case"], raw["head"], raw.get("landing_base") or None, str(raw.get("reason", "")), raw.get("pull_request"),
                  raw.get("tested_merge"), raw.get("tested_base"),
                  raw.get("tested_run") if isinstance(raw.get("tested_run"), int) and not isinstance(raw.get("tested_run"), bool) else None,
                  parse_digests(raw.get("tested_digests")),
                  passed_base if isinstance(passed_base, bool) else None,
                  fallback if isinstance(fallback, str) and fallback else None)


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
        self.request_id: str | None = None  # the x-github-request-id of the last answer of the API

    def _get(self, path: str) -> tuple[bytes, str | None]:
        """The body of a response, or the location of a redirect."""
        request = urllib.request.Request(self.base + path, headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        self.calls += 1
        self.request_id = None
        try:
            with self.opener.open(request, timeout=API_TIMEOUT) as response:
                self.request_id = response.headers.get("X-GitHub-Request-Id")
                return response.read(), None
        except urllib.error.HTTPError as error:
            self.request_id = error.headers.get("X-GitHub-Request-Id") if error.headers is not None else None
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

    def bytes(self, path: str) -> bytes:
        """A raw body; a redirect, such as that of an artifact archive, is followed once without the token."""
        body, location = self._get(path)
        if location is not None:
            try:
                with urllib.request.urlopen(urllib.parse.urljoin(self.base + path, location), timeout=API_TIMEOUT) as response:
                    body = response.read()
            except (urllib.error.URLError, OSError) as error:
                if isinstance(error, urllib.error.HTTPError):
                    error.close()
                raise ApiError(f"GET {path} failed after its redirect: {error}") from error
        return body

    def text(self, path: str) -> str:
        """A text body; a redirect is followed once without the token."""
        return self.bytes(path).decode("utf-8", errors="replace")


# --- the push lookup and structured tested state --------------------------------------------------------------


def write_tested_state(root: Path, pull_request: int, run_id: int, run_attempt: int,
                       digests: dict[str, str] | None, output: Path) -> dict:
    """Write the structured tested-state document of a pull-request merge checkout."""
    tested_merge = commit_id(root, "HEAD")
    tested_base = commit_id(root, "HEAD^1")
    head = commit_id(root, "HEAD^2")
    tree_result = git(root, "rev-parse", "HEAD^{tree}")
    tree = tree_result.stdout.strip() if tree_result.returncode == 0 else ""
    if not (tested_merge and tested_base and head and FULL_SHA.fullmatch(tree)):
        raise RecordError("HEAD must be a pull-request merge commit with two parents and a readable tree")
    state = {
        "version": TESTED_STATE_VERSION,
        "pull_request": pull_request,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "head": head,
        "tested_base": tested_base,
        "tested_merge": tested_merge,
        "tree": tree,
        "digests": digests,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return state


def read_tested_state_artifact(api: Api, run_id: int, pull_request: int, head: str) -> tuple[dict | None, str | None, str]:
    """Download and validate the ``tested-state`` artifact of ``run_id``.
    Returns ``(state, fallback_reason, detail)``."""
    listed = api.json(f"actions/runs/{run_id}/artifacts?name={TESTED_STATE_ARTIFACT}")
    artifacts = listed.get("artifacts") if isinstance(listed, dict) else None
    chosen = next(
        (item for item in (artifacts or []) if isinstance(item, dict)
         and item.get("name") == TESTED_STATE_ARTIFACT
         and not item.get("expired")
         and isinstance(item.get("id"), int) and not isinstance(item.get("id"), bool)),
        None,
    )
    if chosen is None:
        return None, "missing-pr-record", f"run {run_id} of pull request #{pull_request} has no active `{TESTED_STATE_ARTIFACT}` artifact"
    archive_bytes = api.bytes(f"actions/artifacts/{chosen['id']}/zip")
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            raw = json.loads(archive.read(TESTED_STATE_FILE).decode("utf-8"))
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
        return None, "unreadable-pr-record", f"the `{TESTED_STATE_ARTIFACT}` artifact of run {run_id} of pull request #{pull_request} cannot be read: {error}"
    if (not isinstance(raw, dict)
            or raw.get("version") != TESTED_STATE_VERSION or isinstance(raw.get("version"), bool)
            or raw.get("pull_request") != pull_request or isinstance(raw.get("pull_request"), bool)
            or raw.get("head") != head
            or not isinstance(raw.get("tested_merge"), str) or not FULL_SHA.fullmatch(raw["tested_merge"])
            or not isinstance(raw.get("tested_base"), str) or not FULL_SHA.fullmatch(raw["tested_base"])
            or not isinstance(raw.get("tree"), str) or not FULL_SHA.fullmatch(raw["tree"])):
        return None, "unreadable-pr-record", f"the `{TESTED_STATE_ARTIFACT}` artifact of run {run_id} of pull request #{pull_request} has an invalid structure"
    return raw, None, ""


def landing_base(root: Path, sha: str, before: str | None) -> str | None:
    """The commit the push landed on: the previous head of the push, or the first parent of the commit."""
    if before and not ALL_ZERO.match(before):
        commit = commit_id(root, before)
        if commit is not None:
            return commit
    return commit_id(root, f"{sha}^1")


def lookup(root: Path, api: Api, workflow: str, sha: str, before: str | None = None, run_attempt: int = 1,
           default_record: Path | None = None) -> Lookup:
    """The case of a pushed commit. ``workflow`` is the file name of the calling workflow, whose last successful
    non-edited pull-request run uploaded a structured ``tested-state`` artifact. For a behind merge, ``default_record``
    decides whether the landing base has passed. Never reads job logs and never skips on a failed request."""
    landing = landing_base(root, sha, before)
    if run_attempt > 1:
        return Lookup("own", sha, landing, f"attempt {run_attempt} is a re-run, which selects over the push's own changed files")
    try:
        pulls = api.json(f"commits/{sha}/pulls")
    except ApiError as error:
        return Lookup("own", sha, landing, f"the pull request of {sha} could not be looked up: {error}", fallback="push-lookup-api-error")
    if not isinstance(pulls, list) or not pulls:
        return Lookup("own", sha, landing, f"{sha} belongs to no pull request")
    pull = next((item for item in pulls if isinstance(item, dict) and item.get("merge_commit_sha") == sha), pulls[0])
    number, head = pull.get("number"), (pull.get("head") or {}).get("sha")
    if not isinstance(number, int) or isinstance(number, bool) or not isinstance(head, str):
        return Lookup("own", sha, landing, f"the pull request of {sha} names no head commit", fallback="unreadable-pr-record")

    def fallback_result(reason: str, fb: str, **found) -> Lookup:
        return Lookup("pull-request", sha, landing, reason, number, **found, fallback=fb)

    try:
        runs_doc = api.json(f"actions/workflows/{urllib.parse.quote(workflow)}/runs?head_sha={head}&event=pull_request&status=success&per_page=20")
        raw_runs = runs_doc.get("workflow_runs") if isinstance(runs_doc, dict) else None
        runs = [item for item in (raw_runs or []) if isinstance(item, dict)
                and isinstance(item.get("id"), int) and not isinstance(item.get("id"), bool)
                and item.get("conclusion") != "cancelled"]
        if not runs:
            return fallback_result(f"pull request #{number} has no successful run of `{workflow}` on its head {head}", "no-passed-pr-run")
        chosen_run: int | None = None
        state: dict | None = None
        last_reason = ""
        for item in runs:
            candidate_state, state_fb, detail = read_tested_state_artifact(api, item["id"], number, head)
            if candidate_state is not None:
                chosen_run, state = item["id"], candidate_state
                break
            if state_fb == "unreadable-pr-record":
                return fallback_result(detail, state_fb)
            last_reason = detail
        if state is None or chosen_run is None:
            return fallback_result(last_reason or f"pull request #{number} has no `{TESTED_STATE_ARTIFACT}` artifact", "missing-pr-record")
        full, base, tree = state["tested_merge"], state["tested_base"], state["tree"]
        merge = api.json(f"git/commits/{full}")
    except ApiError as error:
        return fallback_result(f"the tested merge commit of pull request #{number} could not be read: {error}", "push-lookup-api-error")

    parents = [parent.get("sha") for parent in merge.get("parents") or []] if isinstance(merge, dict) else []
    merge_tree = (merge.get("tree") or {}).get("sha") if isinstance(merge, dict) else None
    if not isinstance(merge, dict) or merge.get("sha") != full or parents != [base, head] or merge_tree != tree:
        return fallback_result(f"the tested merge commit {full} of run {chosen_run} does not verify as the merge of {head} into {base} with tree {tree}",
                               "unreadable-pr-record")
    digests = parse_digests(state.get("digests"))
    found = dict(tested_merge=full, tested_base=base, tested_run=chosen_run, tested_digests=digests)
    pushed_tree = git(root, "rev-parse", f"{sha}^{{tree}}")
    if pushed_tree.returncode == 0 and pushed_tree.stdout.strip() == tree:
        return Lookup("identical", sha, landing,
                      f"the tree of {sha} equals the tested tree of {full}, which passed in run {chosen_run} of pull request #{number}",
                      number, **found)
    if landing is not None and base != landing and git(root, "merge-base", "--is-ancestor", base, landing).returncode == 0:
        default_rec, default_fb, default_detail = read_default_record(default_record)
        if default_rec is not None and default_rec.commit == landing:
            return Lookup("behind", sha, landing,
                          f"pull request #{number} was tested on {base} and landed on {landing}, which passed in run {default_rec.run_id}",
                          number, **found, landing_base_passed=True)
        if default_rec is not None:
            return Lookup("behind", sha, landing,
                          f"pull request #{number} was tested on {base} and landed on {landing}, which has not passed (last passed default-branch commit is {default_rec.commit} in run {default_rec.run_id})",
                          number, **found, landing_base_passed=False, fallback="unpassed-landing-base")
        return Lookup("behind", sha, landing,
                      f"pull request #{number} was tested on {base} and landed on {landing}, and {default_detail}",
                      number, **found, landing_base_passed=False, fallback=default_fb)
    return fallback_result(f"the tree of {sha} differs from the tested tree of {full}, and its tested base {base} is no earlier commit of the landing base",
                           "tested-tree-differs", tested_merge=full, tested_base=base)


@dataclass(frozen=True)
class SharedLookup:
    """The shared CI digests that a run took, and those from the last passed default-branch record; ``previous`` is
    None when that record or its digests are missing, evicted or unreadable."""

    digests: dict[str, str]
    previous: dict[str, str] | None
    reason: str
    run: int | None = None
    commit: str | None = None
    fallback: str | None = None
    log: tuple[str, ...] = field(default=(), compare=False)  # the lines the lookup logs; not in the document

    def moved_digests(self, previous: dict[str, str] | None = None, *, use_given: bool = False) -> frozenset[str] | None:
        """The digest names that differ from ``previous`` (or ``self.previous``), or None when no readable previous
        digests exist (meaning every digest counts as moved). A digest present on either side and missing on the other
        counts as moved."""
        compared = previous if use_given else self.previous
        if compared is None:
            return None
        return frozenset(moved_digest_names(self.digests, compared))

    @property
    def moved(self) -> bool:
        moved = self.moved_digests()
        return moved is None or bool(moved)

    def document(self) -> dict:
        return {"digests": self.digests, "previous": self.previous, "reason": self.reason,
                "run": self.run, "commit": self.commit, "fallback": self.fallback}


def read_shared_lookup(path: Path) -> SharedLookup:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RecordError(f"the shared lookup {path} cannot be read: {error}") from error
    digests = parse_digests(raw.get("digests")) if isinstance(raw, dict) else None
    if digests is None:
        raise RecordError(f"the shared lookup {path} must name the shared CI digests")
    previous = parse_digests(raw.get("previous"))
    run = raw.get("run")
    commit = raw.get("commit")
    fallback = raw.get("fallback")
    return SharedLookup(digests, previous, str(raw.get("reason", "")),
                        run if isinstance(run, int) and not isinstance(run, bool) else None,
                        commit if isinstance(commit, str) and commit else None,
                        fallback if isinstance(fallback, str) and fallback else None)


def shared_lookup(digests: dict[str, str], default_record: Path | None) -> SharedLookup:
    """Compare ``digests`` with the shared CI digests stored in the restored default-branch record, with no log
    scraping and no fixed walk limit. A missing, evicted or unreadable record or digest returns ``previous=None``
    and records a fallback reason so selection runs more."""
    rec, fb, detail = read_default_record(default_record)
    if rec is None:
        return SharedLookup(digests, None, detail, run=None, commit=None, fallback=fb,
                            log=(f"No digests to compare with: {detail}.",))
    if rec.digests is None:
        why = f"the default-branch record of run {rec.run_id} on {rec.commit} holds no readable shared CI digests"
        return SharedLookup(digests, None, why, run=rec.run_id, commit=rec.commit, fallback="unreadable-default-branch-digests",
                            log=(f"No digests to compare with: {why}.",))
    compared = "equal" if rec.digests == digests else "differ from"
    why = f"the shared CI digests {compared} those of run {rec.run_id}, the last passed default-branch record on {rec.commit}"
    return SharedLookup(digests, rec.digests, why, run=rec.run_id, commit=rec.commit, fallback=None,
                        log=(f"Read default-branch record of run {rec.run_id} on {rec.commit}.",))


def uses_tested_digests(found: Lookup | None) -> bool:
    """Whether ``found`` relies on the pull request's tested run and therefore compares against its tested digests."""
    return found is not None and found.case in ("identical", "behind")


def tested_digests(found: Lookup | None) -> dict[str, str] | None:
    """The shared CI digests that the passed pull-request run of an identical or behind merge took."""
    return found.tested_digests if uses_tested_digests(found) else None


def tested_reason(found: Lookup, digests: dict[str, str]) -> str:
    if found.tested_digests is None:
        who = f"run {found.tested_run} of pull request #{found.pull_request}" if found.tested_run is not None else f"pull request #{found.pull_request}"
        return f"the shared CI digests of {who} could not be read"
    if found.tested_digests == digests:
        return f"the tested tree took the same shared CI digests in run {found.tested_run} of pull request #{found.pull_request}"
    return f"the shared CI digests differ from those of run {found.tested_run} of pull request #{found.pull_request}"


def shared_markdown(found: SharedLookup, push: Lookup | None = None) -> str:
    if push is not None and uses_tested_digests(push):
        if push.tested_digests is None:
            text = "Every suite runs: " + tested_reason(push, found.digests)
        else:
            moved = found.moved_digests(push.tested_digests, use_given=True)
            if not moved:
                prefix = "No suite runs: " if push.case == "identical" else "The selection runs as usual: "
            else:
                prefix = f"Moved digests ({', '.join(f'`{name}`' for name in sorted(moved))}): "
            text = prefix + tested_reason(push, found.digests)
            text += f", which decides in place of the base branch, where {found.reason}"
    else:
        moved = found.moved_digests()
        if moved is None:
            prefix = "Every suite runs: "
        elif moved:
            prefix = f"Moved digests ({', '.join(f'`{name}`' for name in sorted(moved))}): "
        else:
            prefix = "The selection runs as usual: "
        text = prefix + found.reason
    return f"## Shared CI digests\n\n{text}.\n"


def lookup_markdown(found: Lookup) -> str:
    if found.case == "behind" and found.landing_base_passed is not True:
        prefix = "Everything the newer base can affect runs: "
    else:
        text = {"identical": "No suite runs: ", "behind": "Only units that both sides affect run: ",
                "pull-request": "The selection runs over the pull request's files: ", "own": "The selection runs over the push's own changed files: "}
        prefix = text[found.case]
    return f"## Push lookup\n\n{prefix}{found.reason}.\n"


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
    """Whether ``path``, as ``ExistenceRead.path_on`` gives it, is in the tree as ``kind``."""
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

    def reads(self, patterns, memberships, existence, lookups=(), *, system: str) -> dict:
        """The content of a unit's reads: the paths its patterns match with their objects, the names its memberships
        see, the value of its existence reads, and per directory in which it looks up modules the names that it looks
        up there with the names in it that carry one of them. ``lookups`` holds (directory, module names) pairs;
        ``system`` is the operating system of the unit's suite, which decides the path an existence read names."""
        files = sorted({path for pattern in patterns for path in self.matching(pattern)})
        return {"patterns": sorted(set(patterns)), "files": [[path, self.entries[path]] for path in files],
                "memberships": [[membership, membership_names(membership, self.files, self.matches)] for membership in sorted(set(memberships))],
                "existence": sorted([read.path, read.kind, exists_in(read.path_on(system), read.kind, self.files)] for read in set(existence)),
                "lookups": [[directory, sorted(names), [name for name in membership_names(directory, self.files, self.matches)
                                                         if module_name(name) in names]] for directory, names in sorted(lookups)]}

    def entry_files(self, entries) -> list:
        """The entry files of a unit with their objects; an entry file that the tested tree lacks has none."""
        return [[entry, self.entries.get(entry)] for entry in entries]

    def ignore_content(self, rules: IgnoreRules) -> dict:
        """The content of the ignore rules: the tracked ``.gitignore`` files and the tracked paths that they match, with
        their objects."""
        return {"files": [[path, self.entries[path]] for path in sorted(rules.files) if path in self.entries],
                "matched": [[path, self.entries[path]] for path in sorted(self.entries) if rules.ignored(path)]}


def content_digest(document: object) -> str:
    """The SHA-256 of a JSON document, independent of the order of its keys."""
    return hashlib.sha256(json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def unit_context(command, setups, versions: dict[str, str], label: str, shared_digests: dict[str, str] | None = None, *,
                 ignore_content: dict, unrecorded: dict | None = None) -> str:
    """The part of the reuse key that the stored lists do not decide: the digest of the command, the versions the setups
    pin, the runner label, the shared CI digests of the platform, tools and language steps the unit uses, the content
    of the ignore rules and the content of the unit's unrecorded inputs. ``store`` takes it from ``select`` to key a
    unit under the lists with applied additions."""
    return content_digest({"command": list(command or ()), "setups": {setup: versions.get(setup, "") for setup in setups},
                           "image": label, "digests": dict(sorted((shared_digests or {}).items())),
                           "ignore_content": ignore_content, "unrecorded": unrecorded or {}})


def unit_key(reads: dict, context: str) -> str:
    """The reuse key: the content of the reads, as ``Tree.reads`` gives it, with the unit's context (``unit_context``)."""
    return content_digest({"reads": reads, "context": context})


@dataclass(frozen=True)
class DefaultBranchRecord:
    """Structured record written by a passed default-branch run in the default branch's cache scope."""

    version: int
    run_id: int
    commit: str
    tree: str
    digests: dict[str, str] | None
    passed: dict[str, int]

    def document(self) -> dict:
        return {
            "version": self.version,
            "run_id": self.run_id,
            "commit": self.commit,
            "tree": self.tree,
            "digests": self.digests,
            "passed": dict(sorted(self.passed.items())),
        }


def read_default_record(path: Path | None) -> tuple[DefaultBranchRecord | None, str | None, str]:
    """Read and validate a default-branch record; returns ``(record, fallback_reason, detail)``.
    Never raises on a missing, evicted or unreadable record so callers always fall back to running more."""
    if path is None or not path.is_file():
        where = f" {path}" if path is not None else ""
        return None, "missing-default-branch-record", f"the default-branch record{where} is missing or evicted"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        return None, "unreadable-default-branch-record", f"the default-branch record {path} cannot be read: {error}"
    if not isinstance(raw, dict):
        return None, "unreadable-default-branch-record", f"the default-branch record {path} must be a JSON object"
    version = raw.get("version")
    run_id = raw.get("run_id")
    commit = raw.get("commit")
    tree = raw.get("tree")
    passed = raw.get("passed")
    if (version != DEFAULT_RECORD_VERSION or isinstance(version, bool)
            or not isinstance(run_id, int) or isinstance(run_id, bool)
            or not isinstance(commit, str) or not FULL_SHA.fullmatch(commit)
            or not isinstance(tree, str) or not FULL_SHA.fullmatch(tree)
            or not isinstance(passed, dict)
            or not all(isinstance(k, str) and k and isinstance(v, int) and not isinstance(v, bool) for k, v in passed.items())):
        return None, "unreadable-default-branch-record", f"the default-branch record {path} has an invalid structure"
    digests = parse_digests(raw.get("digests")) if raw.get("digests") is not None else None
    return DefaultBranchRecord(version, run_id, commit, tree, digests, dict(passed)), None, ""


def read_record(path: Path | None) -> dict[str, int]:
    """The passed results of a record: unit key to the run it passed in; a missing file holds none."""
    if path is None or not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RecordError(f"the result record {path} cannot be read: {error}") from error
    passed = raw.get("passed") if isinstance(raw, dict) else None
    if not isinstance(passed, dict) or not all(isinstance(key, str) and isinstance(run, int) and not isinstance(run, bool) for key, run in passed.items()):
        raise RecordError(f"the result record {path} must hold passed, an object from unit key to run id")
    return passed


def read_pr_record_safe(path: Path | None) -> tuple[dict[str, int], str | None, str]:
    """Read a pull-request result record without failing selection when it is unreadable."""
    if path is None or not path.is_file():
        return {}, None, ""
    try:
        return read_record(path), None, ""
    except RecordError as error:
        return {}, "unreadable-pr-results", str(error)


def has_cancelled_job(needs: dict) -> bool:
    """Whether any job in ``needs`` was cancelled."""
    return any(isinstance(job, dict) and job.get("result") == "cancelled" for job in needs.values())


def has_failing_job(needs: dict) -> bool:
    """Whether any job in ``needs`` failed, was cancelled, or timed out."""
    return any(isinstance(job, dict) and job.get("result") in FAILING_JOB_RESULTS for job in needs.values())


# --- the store ------------------------------------------------------------------------------------------------


def read_passed(directory: Path) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """The entry files that passed in a run's jobs, from every JSON file under the directory: those without additions,
    and those that ``recording.py keep`` marked with ``additions: true``."""
    passed, with_additions = set(), set()
    for path in sorted(directory.rglob("*.json")) if directory.is_dir() else ():
        try:
            items = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            raise RecordError(f"the passed entry files {path} cannot be read: {error}") from error
        if not isinstance(items, list) or not all(isinstance(item, dict) and isinstance(item.get("suite"), str)
                                                  and isinstance(item.get("entry"), str) and isinstance(item.get("additions", False), bool)
                                                  for item in items):
            raise RecordError(f"{path} must be a list of objects with a suite and an entry, and optionally additions true")
        for item in items:
            (with_additions if item.get("additions") is True else passed).add((item["suite"], item["entry"]))
    return passed, with_additions


def selection_job_outputs(needs: dict, selection_job: str) -> dict | None:
    """The ``outputs`` dict of ``selection_job`` when it ran ``select`` to completion, or None otherwise. Accepts a
    failed job whose ``plan`` output is non-empty and whose ``selection`` output is ``success``, so a failed light
    hygiene check in ``plan`` does not drop passed pull-request results; ``store_default_branch`` rejects any
    non-``success`` job before calling ``store``."""
    selection = needs.get(selection_job)
    if not isinstance(selection, dict):
        return None
    result = selection.get("result")
    outputs = selection.get("outputs") if isinstance(selection.get("outputs"), dict) else {}
    if result == "success":
        return outputs
    if result == "failure" and outputs.get("selection") == "success" and isinstance(outputs.get("plan"), str) and outputs.get("plan"):
        return outputs
    return None


def selection_outputs(needs: dict, selection_job: str) -> tuple[dict | None, dict | None]:
    """The outputs ``units`` and ``reused`` of the job that ran ``select``; None for both when it did not succeed
    (unless ``result`` is ``failure`` with a non-empty ``plan`` output and ``selection == 'success'``) or its outputs
    cannot be read."""
    outputs = selection_job_outputs(needs, selection_job)
    if outputs is None:
        return None, None
    try:
        units = json.loads(outputs.get("units") or "{}")
        reused = json.loads(outputs.get("reused") or "{}")
    except json.JSONDecodeError:
        return None, None
    if not isinstance(units, dict) or not isinstance(reused, dict):
        return None, None
    return units, reused


def reused_unit_outputs(needs: dict, selection_job: str) -> dict:
    """The output ``reused-units`` of the job that ran ``select``: each reused unit by its key with what ``store``
    needs to key it again under applied additions, or empty when absent or unreadable."""
    outputs = selection_job_outputs(needs, selection_job)
    if outputs is None:
        return {}
    try:
        reused_units = json.loads(outputs.get("reused-units") or "{}")
    except json.JSONDecodeError:
        return {}
    return reused_units if isinstance(reused_units, dict) else {}


def store(needs: dict, passed: set[tuple[str, str]], run_id: int, selection_job: str = "select", *,
          with_additions: set[tuple[str, str]] = frozenset(), applied: dict[str, str] | None = None) -> dict[str, int] | None:
    """The record of a run: the results ``select`` reused, and every unit that ran and passed with all its entry
    files. ``selection_job`` names the job of the needs context that ran ``select``. Accepts a failed ``plan`` job
    whose ``plan`` output is non-empty and whose ``selection`` output is ``success``; returns None when the selection
    is not accepted or any job was cancelled, so that nothing is stored.

    In a run whose checks reported additions, ``with_additions`` holds the passed entry files that the additions name,
    and ``applied`` maps the key of each running or reused unit to its key under the suite list with every addition of
    the run applied. Every running unit all of whose entry files passed, with or without additions, is then also stored
    under that key, and a unit with additions or without a stored list only under it: the applied lists cover all that
    it read, and its own key does not. Every reused unit whose key is in ``applied`` is stored under its applied key as
    well, keeping the run id it originally passed in."""
    if has_cancelled_job(needs):
        return None
    units, reused = selection_outputs(needs, selection_job)
    if units is None or reused is None:
        return None
    record = {key: run for key, run in reused.items() if isinstance(run, int) and not isinstance(run, bool)}
    if applied is not None:
        for key, run in list(record.items()):
            if key in applied:
                record[applied[key]] = run
    for key, unit in units.items():
        if not isinstance(unit, dict) or not unit.get("entries"):
            continue
        pairs = [(unit.get("suite"), entry) for entry in unit["entries"]]
        if not unit.get("unlisted") and HEX_DIGEST.fullmatch(key) and all(pair in passed for pair in pairs):
            record[key] = run_id
        if applied is not None and key in applied and all(pair in passed or pair in with_additions for pair in pairs):
            record[applied[key]] = run_id
    return record


def store_default_branch(needs: dict, passed: set[tuple[str, str]], run_id: int, commit: str, tree: str,
                         digests: dict[str, str] | None, selection_job: str = "plan") -> DefaultBranchRecord | None:
    """Build the default-branch record for a passed default-branch run, or None when any job failed or was cancelled."""
    selection = needs.get(selection_job)
    if not isinstance(selection, dict) or selection.get("result") != "success" or has_failing_job(needs):
        return None
    passed_map = store(needs, passed, run_id, selection_job)
    if passed_map is None:
        passed_map = {}
    return DefaultBranchRecord(DEFAULT_RECORD_VERSION, run_id, commit, tree, digests, passed_map)


def environment_api() -> Api:
    return Api(os.environ.get("GITHUB_API_URL", "https://api.github.com"), os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_TOKEN"))
