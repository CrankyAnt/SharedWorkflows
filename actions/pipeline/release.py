#!/usr/bin/env python3
"""Decide whether a run of the release workflow publishes a release, and publish it.

    python3 release.py preflight --ref REF --sha SHA [--schemas DIR] [--repository OWNER/NAME] [--api-url URL] [--github-output FILE] [--summary FILE]
    python3 release.py plan      --ref REF [--root DIR] [--schemas DIR] [--default-branch NAME] [--repository OWNER/NAME] [--api-url URL] [--github-output FILE] [--summary FILE]
    python3 release.py publish   --plan FILE --files DIR [--repository OWNER/NAME] [--api-url URL] [--summary FILE]

``plan`` reads ``.github/ci-config.yml`` of the checkout, which holds the tagged commit, and decides for the part
``github-release`` whether the run publishes. It publishes nothing and passes when the run is not for a tag, when the
repository has no configuration file or does not state the part, and when the tag matches none of the patterns of
``tags``. For a matching tag it reads the allowed-signers file from the default branch as the checkout holds it,
``origin/<default branch>``, and verifies the tag's SSH signature against it with ``git verify-tag``. A tag that is not
annotated, carries no signature or carries one that no allowed key made fails the command, as does a configuration
file that does not match its schema; the summary says why.

The output ``plan`` is one JSON document with the part under ``parts``: ``run`` and ``reason``, and for a run that
publishes ``tag``, ``commit`` (the commit that the verified tag names, which the later jobs check out), ``title``,
``config``, ``script`` (the hook, empty without one), ``notes`` (the file of the release notes, empty without one), ``runner``, ``setups`` (the tool and the version of each named
setup, in the listed order), ``files`` (the files to publish, one per line) and ``earlier-tags`` (the tags that match a
pattern of ``tags`` and name a commit that the released commit reaches, other than that commit itself, nearest first).

``notes`` of the part names a tracked file whose content becomes the release notes, the body of the release. ``plan``
fails when the commit that the verified tag names holds no such file, or one that is not UTF-8 text of at most
125000 characters, and ``publish`` reads the file from GitHub at that commit, so a script of the repository cannot
change it.

Without ``notes`` the body is the list of merged pull requests that GitHub generates for the tag
(``releases/generate-notes`` with ``target_commitish`` the verified commit), filtered. The list starts at the previous
release: the first tag of ``earlier-tags`` that has a published release, which the request names as
``previous_tag_name``; without one the request names none. The filter reads each change line from its end. A line goes
when its title has the type ``docs``, ``style``, ``refactor``, ``test``, ``build``, ``ci`` or ``chore`` without ``!``,
in any case and with or without a scope, or when every author is a bot, a login that ends in ``[bot]``; every other
line stays. A line keeps "by" only for its external contributors, the authors that are not the repository owner as
GitHub reports it, not a bot and not ``Copilot``. "New Contributors" keeps the lines of external contributors, and a
heading without lines goes, a category heading of the repository's ``.github/release.yml`` included. A line that does
not have the shape of a change line stays as it is. Without a change line the body is the sentence "No user-facing
changes are listed for this release." and the line "Full Changelog", which is always the last line. A body of more than
125000 characters loses change lines from the end of the list until it fits, and says so in one sentence above "Full
Changelog".

``publish`` reads that document from a file and publishes the release through the GitHub API with ``GITHUB_TOKEN``: it
creates a draft for the tag, uploads each file of ``files`` from the folder under its file name, and then publishes the
draft. It fails, and publishes nothing, when a file is missing or a release of the tag exists, and before it creates
anything when a request for the release notes, the previous release or the owner fails or GitHub answers without a
body. It reads the tag from
GitHub again twice: right before it creates the draft, and right before the request that publishes the draft, after
the uploads. A tag that no longer names ``commit``, the commit whose signature ``plan`` verified, or that no longer
exists, fails the command and publishes nothing; at the second reading the command also deletes the draft.

The exit code is 0 when the command passed, 1 when it failed and 2 when an input cannot be read.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

PART_FOLDER = Path(__file__).resolve().parent
sys.path.insert(0, str(PART_FOLDER))
import plan  # noqa: E402

PART = "github-release"
TAG_PREFIX = "refs/tags/"
DEFAULT_RUNNER = "ubuntu-26.04"
VERDICT_CHECK = "ci / verdict"
GITHUB_ACTIONS_APP_ID = 15368
API_TIMEOUT = 60
NOTES_LIMIT = 125000  # the characters that GitHub accepts as the body of a release
TAG_DEPTH = 8  # how many tag objects a tag may chain before it names its commit
RELEASES_PAGE = 100  # the releases that one request lists, the most that GitHub gives
HIDDEN_TYPES = frozenset({"docs", "style", "refactor", "test", "build", "ci", "chore"})
NO_CHANGES = "No user-facing changes are listed for this release."
FULL_CHANGELOG = "**Full Changelog**"
CONTRIBUTORS_HEADING = "## New Contributors"
LOGIN = r"@[^\s,]+"
# A change line as GitHub generates it, read from its end: the greedy title leaves the authors and the address of the last
# " by ... in <address>", so a title that holds such words itself stays whole.
CHANGE_LINE = re.compile(rf"\* (?P<title>.+) by (?P<authors>{LOGIN}(?:(?:, and |, | and | with ){LOGIN})*) in (?P<address>\S+)")
CONTRIBUTOR_LINE = re.compile(rf"\* (?P<login>{LOGIN}) made their first contribution in (?P<address>\S+)")
COMMIT_TYPE = re.compile(r"(?P<type>[A-Za-z]+)(?:\([^()]*\))?(?P<breaking>!)?:")


class ReleaseError(Exception):
    """The run cannot publish and fails; the message says why."""


class NoRelease(Exception):
    """The outcome "no release for this ref": the run publishes nothing and passes; the message says why."""


# --- the plan -------------------------------------------------------------------------------------------------------


def release_configuration(root: Path, schemas: Path) -> tuple[dict, plan.SchemaVersion]:
    """The values of the part in the configuration file of the checkout, with the schema version that the file matches."""
    path = root / plan.CONFIGURATION
    if not path.is_file():
        raise NoRelease(f"`{plan.CONFIGURATION}` does not exist")
    try:
        configuration, version = plan.checked_configuration(path.read_text(encoding="utf-8"), plan.schema_versions(schemas))
    except plan.PlanError as error:
        raise ReleaseError(f"`{plan.CONFIGURATION}` cannot be used:\n" + "\n".join(f"- {line}" for line in str(error).splitlines())) from error
    parts = plan.stated_parts(configuration)
    if PART not in parts:
        raise NoRelease(f"`{plan.CONFIGURATION}` does not switch the part `{PART}` on")
    return parts[PART], version


def quoted(items: list[str]) -> str:
    return plan.and_list([f"`{item}`" for item in items])


def preflight_match(schemas: Path, ref: str, sha: str, repository: str, api_url: str, token: str | None) -> str:
    """Decide before cloning whether ``ref`` matches a configured release part; returns the matching tag name,
    raises NoRelease when nothing is published and ReleaseError when the configuration cannot be read or used."""
    if not ref.startswith(TAG_PREFIX) or len(ref) == len(TAG_PREFIX):
        raise NoRelease("the run is not for a tag")
    tag = ref.removeprefix(TAG_PREFIX)
    if not repository or not sha:
        raise ReleaseError("the repository or commit SHA is not set, so the configuration file cannot be read before cloning.")
    repository_url = f"{api_url.rstrip('/')}/repos/{repository}"
    what = f"reading `{plan.CONFIGURATION}` at `{sha}`"
    try:
        status, document = request("GET", f"{repository_url}/contents/{urllib.parse.quote(plan.CONFIGURATION, safe='/')}?{urllib.parse.urlencode({'ref': sha})}", token)
    except ReleaseError as error:
        raise ReleaseError(f"{what} failed: {error}.") from error
    if status == 404:
        raise NoRelease(f"`{plan.CONFIGURATION}` does not exist")
    if not isinstance(document, dict) or document.get("type") != "file" or document.get("encoding") != "base64" or not isinstance(document.get("content"), str):
        raise ReleaseError(f"{what} failed: GitHub's answer holds no content of a file.")
    try:
        text = base64.b64decode(document["content"], validate=False).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as error:
        raise ReleaseError(f"{what} failed: `{plan.CONFIGURATION}` is not valid UTF-8.") from error
    try:
        configuration, _ = plan.checked_configuration(text, plan.schema_versions(schemas))
    except plan.PlanError as error:
        raise ReleaseError(f"`{plan.CONFIGURATION}` cannot be used:\n" + "\n".join(f"- {line}" for line in str(error).splitlines())) from error
    parts = plan.stated_parts(configuration)
    if PART not in parts:
        raise NoRelease(f"`{plan.CONFIGURATION}` does not switch the part `{PART}` on")
    values = parts[PART]
    patterns = [plan.pattern_expression(pattern) for pattern in values["tags"]]
    if not any(expression.fullmatch(tag) for expression in patterns):
        raise NoRelease(f"the tag `{tag}` matches none of the configured patterns {quoted(values['tags'])}")
    return tag


def make_plan(root: Path, schemas: Path, ref: str, default_branch: str, repository: str = "", api_url: str = "https://api.github.com",
              token: str | None = None) -> dict:
    """The decision for a run that publishes; NoRelease for a run that is no release and ReleaseError for one that fails."""
    if not ref.startswith(TAG_PREFIX) or len(ref) == len(TAG_PREFIX):
        raise NoRelease("the run is not for a tag")
    tag = ref.removeprefix(TAG_PREFIX)
    values, version = release_configuration(root, schemas)
    patterns = [plan.pattern_expression(pattern) for pattern in values["tags"]]
    if not any(expression.fullmatch(tag) for expression in patterns):
        raise NoRelease(f"the tag `{tag}` matches none of the configured patterns {quoted(values['tags'])}")
    problems = value_problems(values)
    if problems:
        raise ReleaseError(f"`{plan.CONFIGURATION}` cannot be used:\n" + "\n".join(f"- {line}" for line in problems))
    tag_object, commit = verified_tag(root, tag, values["allowed-signers"], default_branch)
    if not repository:
        raise ReleaseError(f"the repository is not set, so the verdict `{VERDICT_CHECK}` of commit `{commit}` cannot be checked.")
    check_verdict(f"{api_url.rstrip('/')}/repos/{repository}", commit, token)
    if "notes" in values:
        tracked_notes(root, commit, values["notes"])
    return {
        "run": True,
        "reason": f"the tag `{tag}` matches {quoted([pattern for pattern, expression in zip(values['tags'], patterns) if expression.fullmatch(tag)][:1])} "
                  "and carries a valid signature of an allowed key",
        "tag": tag,
        "tag-object": tag_object,
        "commit": commit,
        "title": values.get("title", "{tag}").replace("{tag}", tag).replace("{version}", tag.removeprefix("v")),
        "config": values,
        "script": values.get("script", ""),
        "notes": values.get("notes", ""),
        "runner": values.get("runner-label", DEFAULT_RUNNER),
        "setups": [version.setups[setup] for setup in values.get("setups", [])],
        "files": "\n".join(values["files"]),
        "earlier-tags": earlier_tags(root, commit, patterns),
    }


def earlier_tags(root: Path, commit: str, patterns: list[re.Pattern]) -> list[str]:
    """The tags that match a pattern and name a commit that the released commit reaches, other than the released commit
    itself, nearest first: in the topological order of their commits, so no tag comes before a tag on a later commit
    of its own history, and on one commit the newest tag first. ``publish`` takes the first that has a published release
    as the previous release."""
    logged = plan.git(root, "log", "--topo-order", "--decorate=full", "--decorate-refs=refs/tags/", "--format=%H%x00%D", commit)
    created = plan.git(root, "for-each-ref", "--sort=-creatordate", "--format=%(refname:strip=2)", "refs/tags/")
    if logged.returncode != 0 or created.returncode != 0:
        raise ReleaseError(f"the tags that the commit `{commit}` reaches cannot be read: {(logged.stderr or created.stderr).strip()}")
    newest = {tag: index for index, tag in enumerate(created.stdout.splitlines())}
    found: list[str] = []
    for line in logged.stdout.splitlines():
        held, _, decorations = line.partition("\0")
        if held == commit or not decorations:
            continue
        tags = [name.removeprefix("tag: refs/tags/") for name in decorations.split(", ") if name.startswith("tag: refs/tags/")]
        found += sorted((tag for tag in tags if any(pattern.fullmatch(tag) for pattern in patterns)), key=lambda tag: newest.get(tag, len(newest)))
    return found


def value_problems(values: dict) -> list[str]:
    """What the schema cannot say about the part's values: a setting of the script's job without a script, a file name
    that two files share, and a path that the later steps cannot read as one line."""
    problems = []
    if "script" not in values:
        problems += [f"parts.{PART}.{key}: names {what} `script`, which the part does not state"
                     for key, what in (("setups", "setups for"), ("runner-label", "the runner of")) if key in values]
    names: dict[str, str] = {}
    for path in values["files"]:
        name = PurePosixPath(path).name
        if any(ord(character) < 32 for character in path):
            problems.append(f"parts.{PART}.files: a path holds a control character")
        elif name in names:
            problems.append(f"parts.{PART}.files: `{names[name]}` and `{path}` share the file name `{name}`")
        names.setdefault(name, path)
    return problems


def verified_tag(root: Path, tag: str, signers: str, default_branch: str) -> tuple[str, str]:
    """The verified tag object SHA and the commit it names, once its header names ``tag`` and its signature is one of a
    key in the allowed-signers file of the default branch; a ReleaseError says why it is not."""
    ref = f"{TAG_PREFIX}{tag}"
    kind = plan.git(root, "cat-file", "-t", ref)
    if kind.returncode != 0:
        raise ReleaseError(f"the checkout holds no tag `{tag}`.")
    if kind.stdout.strip() != "tag":
        raise ReleaseError(f"the tag `{tag}` is not an annotated tag, so it carries no signature.")
    tag_object_result = plan.git(root, "rev-parse", "--verify", "--quiet", ref)
    if tag_object_result.returncode != 0 or not tag_object_result.stdout.strip():
        raise ReleaseError(f"the checkout holds no tag `{tag}`.")
    tag_object = tag_object_result.stdout.strip()
    raw_tag = plan.git(root, "cat-file", "tag", tag_object)
    if raw_tag.returncode != 0:
        raise ReleaseError(f"the tag object `{tag_object}` of tag `{tag}` could not be read.")
    header_tag = None
    for line in raw_tag.stdout.split("\n\n", 1)[0].splitlines():
        if line.startswith("tag "):
            header_tag = line.removeprefix("tag ")
            break
    if header_tag != tag:
        raise ReleaseError(f"the tag object `{tag_object}` names the tag `{header_tag or ''}`, not `{tag}`.")
    branch = f"refs/remotes/origin/{default_branch}"
    if plan.git(root, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}").returncode != 0:
        raise ReleaseError(f"the checkout holds no default branch `origin/{default_branch}`, whose allowed-signers file decides.")
    shown = plan.git(root, "show", f"{branch}:{signers}")
    if shown.returncode != 0:
        raise ReleaseError(f"the default branch `{default_branch}` holds no file `{signers}` with the keys that may sign a release tag.")
    with tempfile.TemporaryDirectory() as directory:
        allowed = Path(directory) / "allowed_signers"
        allowed.write_text(shown.stdout, encoding="utf-8", newline="\n")
        verified = plan.git(root, "-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={allowed.as_posix()}", "-c", "gpg.ssh.revocationFile=", "verify-tag", tag_object)
    if verified.returncode != 0:
        detail = " ".join(line.strip() for line in (verified.stderr or "").splitlines() if line.strip())
        raise ReleaseError(f"the tag `{tag}` carries no valid signature of a key in `{signers}` on the default branch `{default_branch}`."
                           + (f" Git says: {detail}" if detail else ""))
    commit = plan.git(root, "rev-parse", "--verify", "--quiet", f"{tag_object}^{{commit}}")
    if commit.returncode != 0:
        raise ReleaseError(f"the tag `{tag}` names no commit.")
    return tag_object, commit.stdout.strip()


def notes_text(data: bytes, path: str, commit: str) -> str:
    """The release notes that the file holds; a ReleaseError when GitHub cannot take them as the body of a release."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ReleaseError(f"the file `{path}` of the release notes at commit `{commit}` is not UTF-8 text.") from error
    if len(text) > NOTES_LIMIT:
        raise ReleaseError(f"the file `{path}` of the release notes at commit `{commit}` holds {len(text)} characters; a release takes at most {NOTES_LIMIT}.")
    return text


def tracked_notes(root: Path, commit: str, path: str) -> str:
    """The release notes in the tracked file of the commit; a ReleaseError when the commit holds no such file."""
    shown = subprocess.run(["git", "-C", str(root), "cat-file", "blob", f"{commit}:{path}"], capture_output=True, check=False)
    if shown.returncode != 0:
        raise ReleaseError(f"parts.{PART}.notes: the commit `{commit}` that the tag names holds no file `{path}`; the release notes are a tracked file of that commit.")
    return notes_text(shown.stdout, path, commit)


def append(path: str | None, text: str) -> None:
    plan.append(path, text)


def run_preflight(options: argparse.Namespace) -> int:
    title = "## Release"
    try:
        tag = preflight_match(Path(options.schemas), options.ref, options.sha, options.repository, options.api_url, os.environ.get("GITHUB_TOKEN"))
    except NoRelease as no_release:
        print(f"Nothing is published: {no_release}.")
        append(options.github_output, "match=false\nplan=" + json.dumps({"parts": {PART: {"run": False, "reason": str(no_release)}}}, separators=(",", ":")) + "\n")
        append(options.summary, f"{title}\n\nNothing is published: {no_release}.\n")
        return 0
    except ReleaseError as error:
        print(f"The release failed: {error}")
        append(options.summary, f"{title}\n\n**Failed.** Nothing is published: {error}\n")
        return 1
    except plan.SchemaError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2
    print(f"The tag {tag} matches the release configuration.")
    append(options.github_output, "match=true\n")
    return 0


def run_plan(options: argparse.Namespace) -> int:
    title = "## Release"
    try:
        decision = make_plan(Path(options.root), Path(options.schemas), options.ref, options.default_branch,
                             options.repository, options.api_url, os.environ.get("GITHUB_TOKEN"))
    except NoRelease as no_release:
        print(f"Nothing is published: {no_release}.")
        append(options.github_output, "plan=" + json.dumps({"parts": {PART: {"run": False, "reason": str(no_release)}}}, separators=(",", ":")) + "\n")
        append(options.summary, f"{title}\n\nNothing is published: {no_release}.\n")
        return 0
    except ReleaseError as error:
        print(f"The release failed: {error}")
        append(options.summary, f"{title}\n\n**Failed.** Nothing is published: {error}\n")
        return 1
    except plan.SchemaError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2
    print(f"The tag {decision['tag']} is released at commit {decision['commit']}.")
    append(options.github_output, "plan=" + json.dumps({"parts": {PART: decision}}, separators=(",", ":")) + "\n")
    lines = [title, "", f"The tag `{decision['tag']}` is released at commit `{decision['commit']}`: {decision['reason']}.", "",
             "The script `" + decision["script"] + "` writes the files on `" + decision["runner"] + "`." if decision["script"]
             else "The files are tracked files of the tagged commit.", "", *[f"- `{path}`" for path in decision["config"]["files"]],
             *(["", f"The release notes are the file `{decision['notes']}` of the tagged commit."] if decision["notes"] else [])]
    append(options.summary, "\n".join(lines) + "\n")
    return 0


# --- the release ----------------------------------------------------------------------------------------------------


def request(method: str, url: str, token: str | None, body: bytes | None = None, content_type: str = "application/json") -> tuple[int, object]:
    """The status and the document of one request to GitHub; a 404 is an answer, any other failure a ReleaseError."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = content_type
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method=method), timeout=API_TIMEOUT) as response:  # noqa: S310 - the API host of the run
            return response.status, json.loads(response.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return 404, None
        raise ReleaseError(f"HTTP {error.code}") from error
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise ReleaseError(str(getattr(error, "reason", error))) from error


def step(what: str, method: str, url: str, token: str | None, body: bytes | None = None, content_type: str = "application/json") -> object:
    """The document of a request that must succeed; a ReleaseError names the step that failed."""
    try:
        status, document = request(method, url, token, body, content_type)
    except ReleaseError as error:
        raise ReleaseError(f"{what} failed: {error}.") from error
    if status == 404:
        raise ReleaseError(f"{what} failed: HTTP 404.")
    return document


def check_verdict(repository_url: str, commit: str, token: str | None) -> None:
    """Fail unless ``commit`` has a completed, passed ``ci / verdict`` check run from GitHub Actions."""
    what = f"reading the verdict `{VERDICT_CHECK}` of commit `{commit}`"
    query = urllib.parse.urlencode({"check_name": VERDICT_CHECK, "per_page": 100})
    document = step(what, "GET", f"{repository_url}/commits/{commit}/check-runs?{query}", token)
    runs = document.get("check_runs") if isinstance(document, dict) else None
    if not isinstance(runs, list):
        raise ReleaseError(f"{what} failed: GitHub's answer holds no check runs.")
    matching = [item for item in runs if isinstance(item, dict) and item.get("name") == VERDICT_CHECK
                and (not isinstance(item.get("app"), dict) or item["app"].get("id") == GITHUB_ACTIONS_APP_ID)]
    run = max(matching, key=lambda item: item.get("id", 0), default=None)
    if run is None:
        raise ReleaseError(f"the commit `{commit}` has no completed `{VERDICT_CHECK}` check run.")
    if run.get("status") != "completed":
        raise ReleaseError(f"the `{VERDICT_CHECK}` check run of commit `{commit}` has not completed ({run.get('status')}).")
    if run.get("conclusion") != "success":
        raise ReleaseError(f"the `{VERDICT_CHECK}` check run of commit `{commit}` concluded `{run.get('conclusion')}`, not `success`.")


def check_tag(repository_url: str, tag: str, tag_object: str, commit: str, token: str | None) -> None:
    """Fail unless ``refs/tags/<tag>`` still names the verified tag object, whose header names ``tag`` and which peels
    to ``commit``."""
    what = f"reading the tag `{tag}`"
    verified = f"the commit `{commit}` whose signature was verified"
    again = "A run for the tag as it is now verifies its signature again."
    try:
        status, document = request("GET", f"{repository_url}/git/ref/tags/{urllib.parse.quote(tag, safe='/')}", token)
    except ReleaseError as error:
        raise ReleaseError(f"{what} failed: {error}.") from error
    if status == 404:
        raise ReleaseError(f"the tag `{tag}` no longer exists; it named {verified}. {again}")
    ref_obj = document.get("object") if isinstance(document, dict) else None
    if not isinstance(ref_obj, dict) or not isinstance(ref_obj.get("sha"), str) or ref_obj.get("type") not in ("commit", "tag"):
        raise ReleaseError(f"{what} failed: GitHub's answer names no commit.")
    if ref_obj["type"] != "tag" or ref_obj["sha"] != tag_object:
        raise ReleaseError(f"the tag `{tag}` moved after its signature was verified: it names {ref_obj['type']} `{ref_obj['sha']}`, "
                           f"not the verified tag object `{tag_object}`. {again}")
    tag_doc = step(what, "GET", f"{repository_url}/git/tags/{tag_object}", token)
    if not isinstance(tag_doc, dict):
        raise ReleaseError(f"{what} failed: GitHub's answer names no commit.")
    if tag_doc.get("tag") != tag:
        raise ReleaseError(f"the tag object `{tag_object}` names the tag `{tag_doc.get('tag') or ''}`, not `{tag}`. {again}")
    current: object = tag_doc
    for _ in range(TAG_DEPTH):
        target = current.get("object") if isinstance(current, dict) else None
        if not isinstance(target, dict) or not isinstance(target.get("sha"), str) or target.get("type") not in ("commit", "tag"):
            break
        if target["type"] == "commit":
            if target["sha"] != commit:
                raise ReleaseError(f"the tag `{tag}` moved after its signature was verified: it names the commit `{target['sha']}`, not {verified}. {again}")
            return
        current = step(what, "GET", f"{repository_url}/git/tags/{target['sha']}", token)
    raise ReleaseError(f"{what} failed: GitHub's answer names no commit.")


def published_notes(repository_url: str, commit: str, path: str, token: str | None) -> str:
    """The release notes in the file that the repository holds at the commit, read from GitHub."""
    what = f"reading the release notes `{path}` at commit `{commit}`"
    try:
        status, document = request("GET", f"{repository_url}/contents/{urllib.parse.quote(path, safe='/')}?{urllib.parse.urlencode({'ref': commit})}", token)
    except ReleaseError as error:
        raise ReleaseError(f"{what} failed: {error}.") from error
    if status == 404:
        raise ReleaseError(f"{what} failed: the commit holds no such file.")
    if not isinstance(document, dict) or document.get("type") != "file" or document.get("encoding") != "base64" or not isinstance(document.get("content"), str):
        raise ReleaseError(f"{what} failed: GitHub's answer holds no content of a file.")
    try:
        data = base64.b64decode(document["content"], validate=False)
    except (binascii.Error, ValueError) as error:
        raise ReleaseError(f"{what} failed: GitHub's answer holds no content of a file.") from error
    return notes_text(data, path, commit)


def previous_release(repository_url: str, earlier: list[str], token: str | None) -> str | None:
    """The previous release: the first of the earlier tags, nearest first, that has a published release; None without one."""
    if not earlier:
        return None
    what = "reading the releases of the repository"
    published: set[str] = set()
    page = 1
    while True:
        listed = step(what, "GET", f"{repository_url}/releases?{urllib.parse.urlencode({'per_page': RELEASES_PAGE, 'page': page})}", token)
        if not isinstance(listed, list):
            raise ReleaseError(f"{what} failed: GitHub's answer holds no list of releases.")
        published.update(item["tag_name"] for item in listed if isinstance(item, dict) and item.get("draft") is False and isinstance(item.get("tag_name"), str))
        if len(listed) < RELEASES_PAGE or earlier[0] in published:
            break
        page += 1
    return next((tag for tag in earlier if tag in published), None)


def repository_owner(repository_url: str, token: str | None) -> str:
    """The login of the owner of the repository, as GitHub reports it."""
    what = "reading the owner of the repository"
    document = step(what, "GET", repository_url, token)
    owner = document.get("owner") if isinstance(document, dict) else None
    if not isinstance(owner, dict) or not isinstance(owner.get("login"), str) or not owner["login"]:
        raise ReleaseError(f"{what} failed: GitHub's answer names no owner.")
    return owner["login"]


def generated_notes(repository_url: str, tag: str, commit: str, previous: str | None, token: str | None) -> str:
    """The list of the merged pull requests that GitHub generates for the tag, from the previous release when there is one."""
    what = f"generating the release notes of the tag `{tag}`"
    request_body = {"tag_name": tag, "target_commitish": commit, **({"previous_tag_name": previous} if previous else {})}
    document = step(what, "POST", f"{repository_url}/releases/generate-notes", token, json.dumps(request_body).encode("utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("body"), str) or not document["body"].strip():
        raise ReleaseError(f"{what} failed: GitHub's answer holds no body.")
    return document["body"]


def shown_title(title: str) -> bool:
    """Whether a change line with this title stays: a hidden type without a breaking mark goes, any other title stays."""
    typed = COMMIT_TYPE.match(title)
    return not typed or typed["type"].lower() not in HIDDEN_TYPES or bool(typed["breaking"])


def bot(login: str) -> bool:
    """Whether the login is a bot's: one that ends in ``[bot]``."""
    return login.lower().endswith("[bot]")


def external(login: str, owner: str) -> bool:
    """Whether the author is an external contributor: not the owner, not a bot and not Copilot. Logins ignore case."""
    name = login.removeprefix("@").lower()
    return name != owner.lower() and not bot(name) and name != "copilot"


def authors_text(logins: list[str]) -> str:
    return logins[0] if len(logins) == 1 else ", ".join(logins[:-1]) + " and " + logins[-1]


def filtered_entries(body: str, owner: str) -> tuple[list[tuple[str, str]], str | None]:
    """The lines of the generated body after the filter, each with its kind, and the line "Full Changelog"."""
    entries: list[tuple[str, str]] = []
    full_changelog = None
    section = ""
    for line in body.replace("\r\n", "\n").split("\n"):
        if line.startswith(FULL_CHANGELOG):
            full_changelog = line
        elif line.startswith("## "):
            section = line.strip()
            entries.append(("section", line))
        elif line.startswith("### "):
            entries.append(("category", line))
        elif not line.strip():
            entries.append(("blank", ""))
        elif section == CONTRIBUTORS_HEADING and (contributor := CONTRIBUTOR_LINE.fullmatch(line)):
            if external(contributor["login"], owner):
                entries.append(("line", line))
        elif section != CONTRIBUTORS_HEADING and (change := CHANGE_LINE.fullmatch(line)):
            logins = re.findall(LOGIN, change["authors"])
            if all(bot(login) for login in logins) or not shown_title(change["title"]):
                continue
            kept = [login for login in logins if external(login, owner)]
            entries.append(("change", f"* {change['title']}" + (f" by {authors_text(kept)}" if kept else "") + f" in {change['address']}"))
        else:
            entries.append(("line", line))
    return entries, full_changelog


def rendered(entries: list[tuple[str, str]], full_changelog: str | None, shown: int, notice: str | None) -> str:
    """The body with the first ``shown`` change lines, without the headings that hold no line, and with the sentence
    that the list is shortened and the line "Full Changelog" at the end."""
    kept: list[tuple[str, str]] = []
    changes = 0
    for kind, text in entries:
        if kind == "change":
            changes += 1
            if changes > shown:
                continue
        kept.append((kind, text))
    # A heading goes when no line stands under it before the next heading of its level or a higher one.
    lines: list[str] = []
    for index, (kind, text) in enumerate(kept):
        if kind in ("section", "category"):
            ends = ("section",) if kind == "section" else ("section", "category")
            holds_line = False
            for next_kind, _ in kept[index + 1:]:
                if next_kind in ends or next_kind in ("line", "change"):
                    holds_line = next_kind in ("line", "change")
                    break
            if not holds_line:
                continue
        lines.append(text)
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")
    tail = [part for part in (notice, full_changelog) if part]
    return "\n\n".join(part for part in (text, *tail) if part)


def release_body(generated: str, owner: str) -> str:
    """The body of the release from the list that GitHub generated: the filtered lines, or the sentence that no change is
    listed, shortened from the end of the list to the length that GitHub accepts."""
    entries, full_changelog = filtered_entries(generated, owner)
    total = sum(kind == "change" for kind, _ in entries)
    if total == 0:
        return "\n\n".join(part for part in (NO_CHANGES, full_changelog) if part)
    body = rendered(entries, full_changelog, total, None)
    if len(body) <= NOTES_LIMIT:
        return body

    def shortened(shown: int) -> str:
        sentence = f"The list is shortened to its first {shown} of {total} changes to fit the length that GitHub accepts for a release."
        return rendered(entries, full_changelog, shown, sentence)

    low, high = 0, total - 1  # the most change lines that fit lies in [low, high]
    while low < high:
        middle = (low + high + 1) // 2
        if len(shortened(middle)) <= NOTES_LIMIT:
            low = middle
        else:
            high = middle - 1
    if low == 0:
        raise ReleaseError(f"the release notes that GitHub generated do not fit the {NOTES_LIMIT} characters of a release, not even with one change line.")
    return shortened(low)


def delete_draft(draft_url: str, token: str | None) -> str:
    """Delete the draft of a release that must not be published, with its uploaded files; the sentence that says what became of it."""
    try:
        status, _ = request("DELETE", draft_url, token)
    except ReleaseError as error:
        return f"The draft of the release could not be deleted ({error}); it is not published, so remove it before this run publishes again."
    if status == 404:
        return "The draft of the release no longer exists."
    return "The draft of the release was deleted."


def publish(part: dict, files: Path, repository: str, api_url: str, token: str | None) -> tuple[str, list[str], str]:
    """Publish the release of the plan's tag with its files; the address of the release, the names of its files and the
    sentence that says where the body comes from."""
    tag = part["tag"]
    paths = [path for path in str(part["files"]).splitlines() if path]
    collected = {path: files / PurePosixPath(path).name for path in paths}
    for path, held in collected.items():
        if not held.is_file():
            raise ReleaseError(f"the file `{path}` is not among the collected files.")
    repository_url = f"{api_url.rstrip('/')}/repos/{repository}"
    releases = f"{repository_url}/releases"
    try:
        status, _ = request("GET", f"{releases}/tags/{urllib.parse.quote(tag, safe='')}", token)
    except ReleaseError as error:
        raise ReleaseError(f"looking up the release of the tag `{tag}` failed: {error}.") from error
    if status != 404:
        raise ReleaseError(f"a release of the tag `{tag}` exists. A release is published once; remove it before this run publishes again.")
    if part.get("notes"):
        body = published_notes(repository_url, part["commit"], str(part["notes"]), token)
        source = f"The release notes are the file `{part['notes']}` of the tagged commit."
    else:
        previous = previous_release(repository_url, part.get("earlier-tags", []), token)
        generated = generated_notes(repository_url, tag, part["commit"], previous, token)
        body = release_body(generated, repository_owner(repository_url, token))
        source = (f"The list of changes starts after the release of the tag `{previous}`." if previous
                  else "The list of changes names no previous release.")
    check_verdict(repository_url, part["commit"], token)
    check_tag(repository_url, tag, part["tag-object"], part["commit"], token)
    draft = step(f"creating the release of the tag `{tag}`", "POST", releases, token,
                 json.dumps({"tag_name": tag, "name": part.get("title") or tag, "body": body, "draft": True}).encode("utf-8"))
    if not isinstance(draft, dict) or not isinstance(draft.get("upload_url"), str) or "id" not in draft:
        raise ReleaseError(f"creating the release of the tag `{tag}` failed: GitHub's answer names no release.")
    upload, draft_id = draft["upload_url"].split("{", 1)[0], draft["id"]
    kept = "The release stays a draft, which is not published; remove it before this run publishes again."
    try:
        for held in collected.values():
            step(f"uploading `{held.name}`", "POST", f"{upload}?{urllib.parse.urlencode({'name': held.name})}", token, held.read_bytes(), "application/octet-stream")
    except ReleaseError as error:
        raise ReleaseError(f"{error} {kept}") from error
    try:
        # The uploads take time: the tag is read once more, right before the request that publishes.
        check_tag(repository_url, tag, part["tag-object"], part["commit"], token)
    except ReleaseError as error:
        raise ReleaseError(f"{error} {delete_draft(f'{releases}/{draft_id}', token)}") from error
    try:
        published = step(f"publishing the release of the tag `{tag}`", "PATCH", f"{releases}/{draft_id}", token, json.dumps({"draft": False}).encode("utf-8"))
    except ReleaseError as error:
        raise ReleaseError(f"{error} {kept}") from error
    address = published.get("html_url") if isinstance(published, dict) else None
    return str(address or draft.get("html_url") or ""), [held.name for held in collected.values()], source


def run_publish(options: argparse.Namespace) -> int:
    try:
        document = json.loads(Path(options.plan).read_text(encoding="utf-8"))
        part = document["parts"][PART]
        if part.get("run") is not True or not all(isinstance(part.get(key), str) and part[key] for key in ("tag", "tag-object", "commit", "files")):
            raise ValueError("the plan does not publish")
        earlier = part.get("earlier-tags", [])
        if not isinstance(earlier, list) or not all(isinstance(tag, str) and tag for tag in earlier):
            raise ValueError("earlier-tags is not a list of tags")
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"::error::The plan {options.plan} cannot be used: {error}", file=sys.stderr)
        return 2
    title = "## Release"
    try:
        address, names, source = publish(part, Path(options.files), options.repository, options.api_url, os.environ.get("GITHUB_TOKEN"))
    except ReleaseError as error:
        print(f"The release failed: {error}")
        append(options.summary, f"{title}\n\n**Failed.** Nothing is published: {error}\n")
        return 1
    print(f"Published the release of the tag {part['tag']}: {address}")
    append(options.summary, "\n".join([title, "", f"Published the release of the tag `{part['tag']}`: {address}", "", *[f"- `{name}`" for name in names], "", source]) + "\n")
    return 0


# --- the command line -----------------------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    main_parser = argparse.ArgumentParser(description="Decide whether a run of the release workflow publishes a release, and publish it.")
    commands = main_parser.add_subparsers(dest="command", required=True)
    preflighting = commands.add_parser("preflight", help="decide before cloning whether the ref matches a configured release part")
    preflighting.add_argument("--schemas", default=str(plan.SCHEMAS), help="the schema folder of the configuration file")
    preflighting.add_argument("--ref", required=True, help="the ref of the run, such as refs/tags/v1.0.0")
    preflighting.add_argument("--sha", default="", help="the commit SHA at which the configuration file is read")
    preflighting.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""), help="the repository of the run, as OWNER/NAME")
    preflighting.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"), help="the address of the GitHub API")
    preflighting.add_argument("--github-output", help="the file that receives the match and plan outputs")
    preflighting.add_argument("--summary", help="the file that receives the Markdown summary")
    planning = commands.add_parser("plan", help="decide from the configuration file, the tag, its signature and the verdict whether the run publishes")
    planning.add_argument("--root", default=".", help="the repository's checkout, with its full history and its tags")
    planning.add_argument("--schemas", default=str(plan.SCHEMAS), help="the schema folder of the configuration file")
    planning.add_argument("--ref", required=True, help="the ref of the run, such as refs/tags/v1.0.0")
    planning.add_argument("--default-branch", default="main", help="the repository's default branch, which holds the allowed-signers file")
    planning.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""), help="the repository of the run, as OWNER/NAME")
    planning.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"), help="the address of the GitHub API")
    planning.add_argument("--github-output", help="the file that receives the output plan")
    planning.add_argument("--summary", help="the file that receives the Markdown summary")
    publishing = commands.add_parser("publish", help="publish the release of the plan's tag with the collected files")
    publishing.add_argument("--plan", required=True, help="a file with the output plan of the command plan")
    publishing.add_argument("--files", required=True, help="the folder that holds each file to publish under its file name")
    publishing.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""), help="the repository of the run, as OWNER/NAME")
    publishing.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"), help="the address of the GitHub API")
    publishing.add_argument("--summary", help="the file that receives the Markdown summary")
    return main_parser


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    return {"preflight": run_preflight, "plan": run_plan, "publish": run_publish}[options.command](options)


if __name__ == "__main__":
    sys.exit(main())
