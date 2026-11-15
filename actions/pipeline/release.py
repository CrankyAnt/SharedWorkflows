#!/usr/bin/env python3
"""Decide whether a run of the release workflow publishes a release, and publish it.

    python3 release.py plan    --ref REF [--root DIR] [--schemas DIR] [--default-branch NAME] [--github-output FILE] [--summary FILE]
    python3 release.py publish --plan FILE --files DIR [--repository OWNER/NAME] [--api-url URL] [--summary FILE]

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
setup, in the listed order) and ``files`` (the files to publish, one per line).

``notes`` of the part names a tracked file whose content becomes the release notes, the body of the release. ``plan``
fails when the commit that the verified tag names holds no such file, or one that is not UTF-8 text of at most
125000 characters, and ``publish`` reads the file from GitHub at that commit, so a script of the repository cannot
change it. Without ``notes`` the body is one fixed sentence that names the tag and its commit.

``publish`` reads that document from a file and publishes the release through the GitHub API with ``GITHUB_TOKEN``: it
creates a draft for the tag, uploads each file of ``files`` from the folder under its file name, and then publishes the
draft. It fails, and publishes nothing, when a file is missing or a release of the tag exists. It reads the tag from
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
API_TIMEOUT = 60
NOTES_LIMIT = 125000  # the characters that GitHub accepts as the body of a release
TAG_DEPTH = 8  # how many tag objects a tag may chain before it names its commit


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


def make_plan(root: Path, schemas: Path, ref: str, default_branch: str) -> dict:
    """The decision for a run that publishes; NoRelease for a run that is no release and ReleaseError for one that fails."""
    if not ref.startswith(TAG_PREFIX) or len(ref) == len(TAG_PREFIX):
        raise NoRelease("the run is not for a tag")
    tag = ref.removeprefix(TAG_PREFIX)
    values, version = release_configuration(root, schemas)
    if not any(plan.pattern_expression(pattern).fullmatch(tag) for pattern in values["tags"]):
        raise NoRelease(f"the tag `{tag}` matches none of the configured patterns {quoted(values['tags'])}")
    problems = value_problems(values)
    if problems:
        raise ReleaseError(f"`{plan.CONFIGURATION}` cannot be used:\n" + "\n".join(f"- {line}" for line in problems))
    commit = verified_commit(root, tag, values["allowed-signers"], default_branch)
    if "notes" in values:
        tracked_notes(root, commit, values["notes"])
    return {
        "run": True,
        "reason": f"the tag `{tag}` matches {quoted([pattern for pattern in values['tags'] if plan.pattern_expression(pattern).fullmatch(tag)][:1])} "
                  "and carries a valid signature of an allowed key",
        "tag": tag,
        "commit": commit,
        "title": values.get("title", "{tag}").replace("{tag}", tag).replace("{version}", tag.removeprefix("v")),
        "config": values,
        "script": values.get("script", ""),
        "notes": values.get("notes", ""),
        "runner": values.get("runner-label", DEFAULT_RUNNER),
        "setups": [version.setups[setup] for setup in values.get("setups", [])],
        "files": "\n".join(values["files"]),
    }


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


def verified_commit(root: Path, tag: str, signers: str, default_branch: str) -> str:
    """The commit that the tag names, once its signature is one of a key in the allowed-signers file of the default
    branch; a ReleaseError says why it is not."""
    ref = f"{TAG_PREFIX}{tag}"
    kind = plan.git(root, "cat-file", "-t", ref)
    if kind.returncode != 0:
        raise ReleaseError(f"the checkout holds no tag `{tag}`.")
    if kind.stdout.strip() != "tag":
        raise ReleaseError(f"the tag `{tag}` is not an annotated tag, so it carries no signature.")
    branch = f"refs/remotes/origin/{default_branch}"
    if plan.git(root, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}").returncode != 0:
        raise ReleaseError(f"the checkout holds no default branch `origin/{default_branch}`, whose allowed-signers file decides.")
    shown = plan.git(root, "show", f"{branch}:{signers}")
    if shown.returncode != 0:
        raise ReleaseError(f"the default branch `{default_branch}` holds no file `{signers}` with the keys that may sign a release tag.")
    with tempfile.TemporaryDirectory() as directory:
        allowed = Path(directory) / "allowed_signers"
        allowed.write_text(shown.stdout, encoding="utf-8", newline="\n")
        verified = plan.git(root, "-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={allowed.as_posix()}", "-c", "gpg.ssh.revocationFile=", "verify-tag", ref)
    if verified.returncode != 0:
        detail = " ".join(line.strip() for line in (verified.stderr or "").splitlines() if line.strip())
        raise ReleaseError(f"the tag `{tag}` carries no valid signature of a key in `{signers}` on the default branch `{default_branch}`."
                           + (f" Git says: {detail}" if detail else ""))
    commit = plan.git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if commit.returncode != 0:
        raise ReleaseError(f"the tag `{tag}` names no commit.")
    return commit.stdout.strip()


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


def run_plan(options: argparse.Namespace) -> int:
    title = "## Release"
    try:
        decision = make_plan(Path(options.root), Path(options.schemas), options.ref, options.default_branch)
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


def tag_commit(repository_url: str, tag: str, token: str | None) -> str | None:
    """The commit that the tag names on GitHub at this moment; None when the repository holds no such tag."""
    what = f"reading the tag `{tag}`"
    try:
        status, document = request("GET", f"{repository_url}/git/ref/tags/{urllib.parse.quote(tag, safe='/')}", token)
    except ReleaseError as error:
        raise ReleaseError(f"{what} failed: {error}.") from error
    if status == 404:
        return None
    for _ in range(TAG_DEPTH):
        target = document.get("object") if isinstance(document, dict) else None
        if not isinstance(target, dict) or not isinstance(target.get("sha"), str) or target.get("type") not in ("commit", "tag"):
            break
        if target["type"] == "commit":
            return target["sha"]
        document = step(what, "GET", f"{repository_url}/git/tags/{target['sha']}", token)
    raise ReleaseError(f"{what} failed: GitHub's answer names no commit.")


def check_tag(repository_url: str, tag: str, commit: str, token: str | None) -> None:
    """Fail unless the tag still names the commit whose signature the plan verified."""
    held = tag_commit(repository_url, tag, token)
    verified = f"the commit `{commit}` whose signature was verified"
    again = "A run for the tag as it is now verifies its signature again."
    if held is None:
        raise ReleaseError(f"the tag `{tag}` no longer exists; it named {verified}. {again}")
    if held != commit:
        raise ReleaseError(f"the tag `{tag}` moved after its signature was verified: it names the commit `{held}`, not {verified}. {again}")


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


def delete_draft(draft_url: str, token: str | None) -> str:
    """Delete the draft of a release that must not be published, with its uploaded files; the sentence that says what became of it."""
    try:
        status, _ = request("DELETE", draft_url, token)
    except ReleaseError as error:
        return f"The draft of the release could not be deleted ({error}); it is not published, so remove it before this run publishes again."
    if status == 404:
        return "The draft of the release no longer exists."
    return "The draft of the release was deleted."


def publish(part: dict, files: Path, repository: str, api_url: str, token: str | None) -> tuple[str, list[str]]:
    """Publish the release of the plan's tag with its files; the address of the release and the names of its files."""
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
    else:
        body = f"Published by the release part of the shared workflows from the signed tag `{tag}` at commit `{part['commit']}`."
    check_tag(repository_url, tag, part["commit"], token)
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
        check_tag(repository_url, tag, part["commit"], token)
    except ReleaseError as error:
        raise ReleaseError(f"{error} {delete_draft(f'{releases}/{draft_id}', token)}") from error
    try:
        published = step(f"publishing the release of the tag `{tag}`", "PATCH", f"{releases}/{draft_id}", token, json.dumps({"draft": False}).encode("utf-8"))
    except ReleaseError as error:
        raise ReleaseError(f"{error} {kept}") from error
    address = published.get("html_url") if isinstance(published, dict) else None
    return str(address or draft.get("html_url") or ""), [held.name for held in collected.values()]


def run_publish(options: argparse.Namespace) -> int:
    try:
        document = json.loads(Path(options.plan).read_text(encoding="utf-8"))
        part = document["parts"][PART]
        if part.get("run") is not True or not all(isinstance(part.get(key), str) and part[key] for key in ("tag", "commit", "files")):
            raise ValueError("the plan does not publish")
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"::error::The plan {options.plan} cannot be used: {error}", file=sys.stderr)
        return 2
    title = "## Release"
    try:
        address, names = publish(part, Path(options.files), options.repository, options.api_url, os.environ.get("GITHUB_TOKEN"))
    except ReleaseError as error:
        print(f"The release failed: {error}")
        append(options.summary, f"{title}\n\n**Failed.** Nothing is published: {error}\n")
        return 1
    print(f"Published the release of the tag {part['tag']}: {address}")
    append(options.summary, "\n".join([title, "", f"Published the release of the tag `{part['tag']}`: {address}", "", *[f"- `{name}`" for name in names]]) + "\n")
    return 0


# --- the command line -----------------------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    main_parser = argparse.ArgumentParser(description="Decide whether a run of the release workflow publishes a release, and publish it.")
    commands = main_parser.add_subparsers(dest="command", required=True)
    planning = commands.add_parser("plan", help="decide from the configuration file, the tag and its signature whether the run publishes")
    planning.add_argument("--root", default=".", help="the repository's checkout, with its full history and its tags")
    planning.add_argument("--schemas", default=str(plan.SCHEMAS), help="the schema folder of the configuration file")
    planning.add_argument("--ref", required=True, help="the ref of the run, such as refs/tags/v1.0.0")
    planning.add_argument("--default-branch", default="main", help="the repository's default branch, which holds the allowed-signers file")
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
    return {"plan": run_plan, "publish": run_publish}[options.command](options)


if __name__ == "__main__":
    sys.exit(main())
