"""Decide whether a Dependabot pull request merges automatically, and merge it once its required checks have passed.

    python3 auto_merge.py --repository OWNER/NAME [--summary FILE] pull-request --number N
    python3 auto_merge.py --repository OWNER/NAME [--summary FILE] workflow-run --head-branch BRANCH

The reusable workflow runs ``pull-request`` on a Dependabot ``pull_request`` event and ``workflow-run`` when a
workflow that produces a required check completes for a Dependabot pull request. Both read the pull request, its
commits and the active rules on the base branch with ``gh api`` and the run's token, and read the update metadata
from the pull request's first commit as ``dependabot/fetch-metadata`` v3.1.0 does: the pull request and that commit
must be authored by ``dependabot[bot]``, the commit's signature must be verified, and its message must hold the
``updated-dependencies`` block.

The decision is automatic only when every updated dependency has a patch or minor update type, every GitHub Actions
dependency belongs to the ``actions`` owner, every commit is authored by ``dependabot[bot]`` or is the updater App's
rebuild of this repository's ETW consumer, which changes only the consumer's build and the suite list, and an active
ruleset rule on the base branch requires at least one status check. A pull request that fails one of the first three rules
is manual, and the run succeeds. A pull request that meets them while the base branch has no such rule is blocked,
and the run fails naming the missing ruleset. Unverified metadata or a failed read is an error, and the run fails
with the reason.

``pull-request`` enables no auto-merge and disables one that the pull request still has. ``workflow-run`` decides
again for each open Dependabot pull request of the head branch and, on an automatic decision, requests a squash merge
bound to the head commit that the decision read. A merge refused because a required check is pending or failed,
because the head moved, or because the pull request is already merged ends the run successfully. After a merge it
made itself, it sends one ``repository_dispatch`` event of type ``dependabot-merged`` with the merged commit.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import quote

BOT = "dependabot[bot]"
# The updater App's rebuild of this repository's ETW consumer on a Dependabot branch, and the paths it may change.
UPDATER_APP = "crankyant-publication-checks[bot]"
UPDATER_REPOSITORY = "CrankyAnt/SharedWorkflows"
UPDATER_BUILD = "actions/recording/etw-consumer/build/"
UPDATER_SUITE_LIST = "tests/suites.json"
AUTOMATIC_UPDATE_TYPES = ("version-update:semver-patch", "version-update:semver-minor")
ACTIONS_ECOSYSTEM = "github_actions"
ACTIONS_OWNER = "actions"
MERGE_METHOD = "squash"
DISPATCH_EVENT = "dependabot-merged"


class Outcome(StrEnum):
    AUTOMATIC = "automatic"  # every rule holds; the run succeeds
    MANUAL = "manual"  # the pull request is left to the user; the run succeeds
    BLOCKED = "blocked"  # rules 1 to 3 hold but no ruleset requires a check; the run fails
    ERROR = "error"  # the metadata is unverified or a call failed; the run fails
    NO_PULL_REQUEST = "none"  # no open Dependabot pull request has the head branch; the run succeeds

    @property
    def exit_code(self) -> int:
        return 0 if self in (Outcome.AUTOMATIC, Outcome.MANUAL, Outcome.NO_PULL_REQUEST) else 1


@dataclass(frozen=True)
class Dependency:
    name: str
    ecosystem: str
    update_type: str


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reason: str
    dependencies: tuple[Dependency, ...] = ()

    def failed(self) -> Decision:
        """The same decision as an error, when a call after it failed."""
        return Decision(Outcome.ERROR, self.reason, self.dependencies)


# --- the update metadata, as dependabot/fetch-metadata v3.1.0 reads it ------------------------------------------


class MetadataError(ValueError):
    """The update metadata of the pull request is missing, unverified or not in Dependabot's form."""


UPDATE_FRAGMENT = re.compile(r"\b[Uu]pdate .* requirement from \S*? ?(?P<from>v?[0-9]\S*) to \S*? ?(?P<to>v?[0-9]\S*)")
BUMP_FRAGMENT = re.compile(r"^Bumps .* from (?P<from>v?[0-9][^ ]*) to (?P<to>v?[0-9][^ ]*)\.$", re.MULTILINE)
YAML_FRAGMENT = re.compile(r"^-{3}\n(?P<dependencies>[\S\s]*?)\n^\.{3}\n", re.MULTILINE)
METADATA_LINK = re.compile(r"^Updates `(?P<name>\S+)` (?:from (?P<from>\S+) )?to (?P<to>\S+)$", re.MULTILINE)
DEPENDABOT_PREFIX = "dependabot"  # the head branch starts with it; the character after it delimits the branch's segments
ENTRY_LINE = re.compile(r"(?P<start>- |  )(?P<key>[A-Za-z0-9_.-]+):(?: (?P<value>.*))?")
BLOCK_KEY = "updated-dependencies:"


def _shown(login: str) -> str:
    return f"`{login}`" if login else "an account GitHub cannot match"


def _scalar(value: str) -> str:
    """A plain or quoted YAML scalar on one line, without its quotes."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return value[1:-1]
    return value


def parse_block(block: str) -> list[dict[str, str]]:
    """The entries of the ``updated-dependencies`` block; any line outside a list of ``key: value`` entries is an error."""
    lines = block.split("\n")
    if lines[0].rstrip() != BLOCK_KEY:
        raise MetadataError(f"the metadata block starts with `{lines[0]}`, not with `{BLOCK_KEY}`.")
    entries: list[dict[str, str]] = []
    for number, line in enumerate(lines[1:], start=2):
        match = ENTRY_LINE.fullmatch(line.rstrip())
        if match is None or (match["start"] == "  " and not entries):
            raise MetadataError(f"line {number} of the metadata block, `{line}`, is not a `key: value` pair of a dependency entry.")
        if match["start"] == "- ":
            entries.append({})
        if match["key"] in entries[-1]:
            raise MetadataError(f"line {number} of the metadata block repeats the key `{match['key']}` in one entry.")
        entries[-1][match["key"]] = _scalar(match["value"] or "")
    if not entries:
        raise MetadataError("the metadata block lists no updated dependency.")
    for index, entry in enumerate(entries, start=1):
        if not entry.get("dependency-name"):
            raise MetadataError(f"entry {index} of the metadata block has no `dependency-name`.")
    return entries


def calculate_update_type(last_version: str, next_version: str) -> str:
    """The update type that fetch-metadata derives from two versions; empty when a version is missing or both agree."""
    if not last_version or not next_version or last_version == next_version:
        return ""
    last_parts = last_version.replace("v", "", 1).split(".")
    next_parts = next_version.replace("v", "", 1).split(".")
    if last_parts[0] != next_parts[0]:
        return "version-update:semver-major"
    if len(last_parts) < 2 or len(next_parts) < 2 or last_parts[1] != next_parts[1]:
        return "version-update:semver-minor"
    return "version-update:semver-patch"


def parse_update_metadata(message: str, branch: str, title: str) -> tuple[Dependency, ...]:
    """The updated dependencies of a Dependabot commit message on its head branch, with fetch-metadata's results."""
    block = YAML_FRAGMENT.search(message)
    if block is None:
        raise MetadataError("the first commit's message has no `updated-dependencies` block.")
    if not branch.startswith(DEPENDABOT_PREFIX):
        raise MetadataError(f"the head branch `{branch}` is not a Dependabot branch.")
    entries = parse_block(block["dependencies"])
    bump = BUMP_FRAGMENT.search(message)
    update = UPDATE_FRAGMENT.search(message.split("\n")[0])
    title_update = UPDATE_FRAGMENT.search(title) if not bump and not update and title else None
    fragment = bump or update or title_update
    previous, new = (fragment["from"], fragment["to"]) if fragment else ("", "")
    delimiter = branch[len(DEPENDABOT_PREFIX)] if len(branch) > len(DEPENDABOT_PREFIX) else ""
    chunks = branch.split(delimiter) if delimiter else [branch]
    ecosystem = chunks[1] if len(chunks) > 1 else ""
    links: dict[str, list[tuple[str, str]]] = {}
    for link in METADATA_LINK.finditer(message):
        links.setdefault(link["name"], []).append((link["from"] or "", link["to"]))
    seen: dict[str, int] = {}
    dependencies = []
    for index, entry in enumerate(entries):
        name = entry["dependency-name"]
        occurrence = seen.get(name, 0)
        seen[name] = occurrence + 1
        linked = links.get(name, [])
        linked_from, linked_to = linked[occurrence] if occurrence < len(linked) else ("", "")
        last_version = linked_from or (previous if index == 0 else "")
        next_version = entry.get("dependency-version") or linked_to or (new if index == 0 else "")
        update_type = entry.get("update-type") or calculate_update_type(last_version, next_version)
        dependencies.append(Dependency(name, ecosystem, update_type))
    return tuple(dependencies)


def read_metadata(pull_request: dict, commits: list[dict]) -> tuple[Dependency, ...]:
    """The verified update metadata of a pull request from its first commit; anything unverified is an error."""
    author = (pull_request.get("user") or {}).get("login") or ""
    if author != BOT:
        raise MetadataError(f"the pull request is authored by {_shown(author)}, not by `{BOT}`.")
    if not commits:
        raise MetadataError("the pull request has no commit.")
    first = commits[0]
    sha = str(first.get("sha") or "")[:7]
    login = (first.get("author") or {}).get("login") or ""
    if login != BOT:
        raise MetadataError(f"the first commit `{sha}` is authored by {_shown(login)}, not by `{BOT}`.")
    commit = first.get("commit") or {}
    if not (commit.get("verification") or {}).get("verified"):
        raise MetadataError(f"the signature of the first commit `{sha}` is not verified.")
    head = pull_request.get("head") or {}
    return parse_update_metadata(str(commit.get("message") or ""), str(head.get("ref") or ""), str(pull_request.get("title") or ""))


# --- the decision ------------------------------------------------------------------------------------------------


def dependency_problems(dependencies: tuple[Dependency, ...]) -> list[str]:
    """Why each dependency keeps the pull request manual, in metadata order; empty when all qualify."""
    problems = []
    for dependency in dependencies:
        if dependency.update_type not in AUTOMATIC_UPDATE_TYPES:
            shown = f"`{dependency.update_type}`" if dependency.update_type else "an empty update type"
            problems.append(f"`{dependency.name}` has {shown}, not a patch or minor update")
        if dependency.ecosystem == ACTIONS_ECOSYSTEM:
            owner, separator, _ = dependency.name.partition("/")
            if not separator or owner != ACTIONS_OWNER:
                problems.append(f"`{dependency.name}` is a GitHub Action whose owner is not `{ACTIONS_OWNER}`")
    return problems


def _author(entry: dict) -> str:
    return ((entry.get("author") or {}).get("login")) or ""


def updater_commits(commits: list[dict]) -> list[str]:
    """The commits by the updater App, whose changed files the decision needs."""
    return [str(entry.get("sha") or "") for entry in commits if _author(entry) == UPDATER_APP]


def commit_problems(commits: list[dict], commit_files: dict[str, list[str]] | None) -> list[str]:
    """Why each commit keeps the pull request manual; without ``commit_files`` the updater App counts as any other author."""
    problems = []
    for entry in commits:
        login = _author(entry)
        sha = str(entry.get("sha") or "")
        if login == UPDATER_APP and commit_files is not None:
            if sha not in commit_files:
                problems.append(f"the changed files of commit `{sha[:7]}` by `{UPDATER_APP}` were not read")
                continue
            outside = [path for path in commit_files[sha] if not path.startswith(UPDATER_BUILD) and path != UPDATER_SUITE_LIST]
            if outside:
                shown = ", ".join(f"`{path}`" for path in outside)
                problems.append(f"commit `{sha[:7]}` by `{UPDATER_APP}` changes {shown}, outside the ETW consumer's build and the suite list")
        elif login != BOT:
            problems.append(f"commit `{sha[:7]}` is authored by {_shown(login)}, not by `{BOT}`")
    return problems


def required_checks(rules: list[dict]) -> list[str]:
    """The status checks that active ruleset rules on the branch require."""
    contexts = []
    for rule in rules:
        if rule.get("type") == "required_status_checks":
            for check in (rule.get("parameters") or {}).get("required_status_checks") or []:
                contexts.append(str(check.get("context") or ""))
    return [context for context in contexts if context]


def decide(dependencies: tuple[Dependency, ...], commits: list[dict], rules: list[dict], commit_files: dict[str, list[str]] | None = None) -> Decision:
    """The decision; ``commit_files`` holds the changed files of each commit by the updater App in this repository, and is
    None in any other repository, where the App's commits keep the pull request manual."""
    problems = dependency_problems(dependencies) + commit_problems(commits, commit_files)
    if problems:
        return Decision(Outcome.MANUAL, "; ".join(problems) + ".", dependencies)
    if not required_checks(rules):
        return Decision(Outcome.BLOCKED, "no active ruleset rule on the base branch requires a status check, so a merge would not wait for any check.", dependencies)
    return Decision(Outcome.AUTOMATIC, f"every update is a patch or minor update, every GitHub Action belongs to `{ACTIONS_OWNER}`, every commit is by `{BOT}` "
                    f"or a rebuild of the ETW consumer by `{UPDATER_APP}`, and a ruleset requires status checks on the base branch.", dependencies)


# --- GitHub ------------------------------------------------------------------------------------------------------


class GitHubError(RuntimeError):
    """A read or mutation through the GitHub API did not succeed; ``status`` is the HTTP status when gh reported one."""

    def __init__(self, message: str, status: int | None = None, response: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.response = response


class GitHub:
    """The GitHub API through ``gh``, authenticated with the token in ``GH_TOKEN``."""

    def __init__(self, gh: str = "gh") -> None:
        self.gh = gh

    def _run(self, *arguments: str) -> str:
        result = subprocess.run([self.gh, *arguments], capture_output=True, text=True, check=False)
        if result.returncode != 0:
            status = re.search(r"\(HTTP (\d{3})\)", result.stderr)
            response = result.stdout.strip() or result.stderr.strip()
            shown = " ".join(part for part in (result.stderr.strip(), result.stdout.strip()) if part)
            raise GitHubError(f"`gh {' '.join(arguments)}` failed (exit {result.returncode}): {shown}", int(status[1]) if status else None, response)
        return result.stdout

    def _api(self, *arguments: str) -> object:
        try:
            return json.loads(self._run("api", *arguments))
        except json.JSONDecodeError as error:
            raise GitHubError(f"`gh api {' '.join(arguments)}` returned no JSON: {error}") from error

    def pull_request(self, repository: str, number: int) -> dict:
        return self._api(f"repos/{repository}/pulls/{number}")

    def open_pull_requests(self, repository: str, head: str) -> list[dict]:
        return self._api("--method", "GET", f"repos/{repository}/pulls", "-f", "state=open", "-f", f"head={head}")

    def commits(self, repository: str, number: int) -> list[dict]:
        pages = self._api("--paginate", "--slurp", f"repos/{repository}/pulls/{number}/commits")
        return [entry for page in pages for entry in page]

    def commit_files(self, repository: str, sha: str) -> list[str]:
        """The paths a commit changes; a rename names its old and its new path."""
        pages = self._api("--paginate", "--slurp", f"repos/{repository}/commits/{sha}")
        return [path for page in pages for entry in page.get("files") or [] for path in (entry.get("filename"), entry.get("previous_filename")) if path]

    def branch_rules(self, repository: str, branch: str) -> list[dict]:
        return self._api(f"repos/{repository}/rules/branches/{quote(branch, safe='')}")

    def disable_auto_merge(self, url: str) -> None:
        self._run("pr", "merge", "--disable-auto", url)

    def merge(self, repository: str, number: int, sha: str, method: str) -> dict:
        return self._api("--method", "PUT", f"repos/{repository}/pulls/{number}/merge", "-f", f"merge_method={method}", "-f", f"sha={sha}")

    def dispatch(self, repository: str, event_type: str, sha: str) -> None:
        self._run("api", "--method", "POST", f"repos/{repository}/dispatches", "-f", f"event_type={event_type}", "-f", f"client_payload[sha]={sha}")


@dataclass(frozen=True)
class Result:
    decision: Decision
    action: str

    @property
    def exit_code(self) -> int:
        return self.decision.outcome.exit_code

    @property
    def summary(self) -> str:
        rows = "".join(f"| `{dependency.name}` | {dependency.ecosystem} | {dependency.update_type} |\n" for dependency in self.decision.dependencies)
        table = f"| Dependency | Ecosystem | Update type |\n| --- | --- | --- |\n{rows}" if rows else "No updated dependency was reported.\n"
        return f"## Dependabot auto-merge\n\n{table}\n**Decision:** {self.decision.outcome}: {self.decision.reason}\n\n**Action:** {self.action}\n"


@dataclass(frozen=True)
class Reading:
    decision: Decision
    head: str  # the head commit the decision read; empty when the commits did not end at the pull request's head


def _read_and_decide(github: GitHub, repository: str, pull_request: dict) -> Reading:
    """Read the commits, the metadata and the rules of an already read pull request, and decide."""
    dependencies: tuple[Dependency, ...] = ()
    try:
        commits = github.commits(repository, int(pull_request["number"]))
        dependencies = read_metadata(pull_request, commits)
        commit_files = {sha: github.commit_files(repository, sha) for sha in updater_commits(commits)} if repository == UPDATER_REPOSITORY else None
        rules = github.branch_rules(repository, (pull_request.get("base") or {}).get("ref") or "")
    except (MetadataError, GitHubError) as error:
        return Reading(Decision(Outcome.ERROR, str(error), dependencies), "")
    head = (pull_request.get("head") or {}).get("sha") or ""
    return Reading(decide(dependencies, commits, rules, commit_files), head if commits[-1].get("sha") == head else "")


def _disable_if_enabled(github: GitHub, pull_request: dict, rest: str) -> str:
    if not pull_request.get("auto_merge"):
        return f"nothing to disable; {rest}"
    github.disable_auto_merge(pull_request["html_url"])
    return f"auto-merge disabled; {rest}"


def run_pull_request(github: GitHub, repository: str, number: int) -> Result:
    """On a pull request event: read the pull request, decide, and disable an auto-merge it still has."""
    try:
        pull_request = github.pull_request(repository, number)
    except GitHubError as error:
        return Result(Decision(Outcome.ERROR, f"the pull request could not be read: {error}"), "nothing; auto-merge could not be read, so it was not changed.")
    url = pull_request["html_url"]
    decision = _read_and_decide(github, repository, pull_request).decision
    try:
        if decision.outcome in (Outcome.MANUAL, Outcome.ERROR):
            return Result(decision, _disable_if_enabled(github, pull_request, "the pull request is left to the user."))
        if decision.outcome is Outcome.BLOCKED:
            if not pull_request.get("auto_merge"):
                return Result(decision, "nothing enabled and nothing merged; the user settles the ruleset first.")
            github.disable_auto_merge(url)
            return Result(decision, "auto-merge disabled; nothing merged; the user settles the ruleset first.")
        if not pull_request.get("auto_merge"):
            return Result(decision, "nothing enabled and nothing to disable.")
        github.disable_auto_merge(url)
        return Result(decision, "auto-merge disabled; nothing enabled.")
    except GitHubError as error:
        return Result(decision.failed(), f"auto-merge could not be disabled: {error}")


def _refusal(github: GitHub, repository: str, number: int, error: GitHubError) -> str | None:
    """Why GitHub refused the merge, when the refusal ends the run successfully; None for any other failure."""
    if error.status not in (405, 409):
        return None
    try:
        if github.pull_request(repository, number).get("merged"):
            return "the pull request is already merged."
    except GitHubError:
        return None
    if error.status == 409:
        return f"the head moved after the decision read it ({error.response})."
    if "status check" in error.response.lower():
        return f"a required check is pending or failed ({error.response})."
    return None


def _merge(github: GitHub, repository: str, pull_request: dict) -> Result:
    number = int(pull_request["number"])
    reading = _read_and_decide(github, repository, pull_request)
    decision = reading.decision
    if decision.outcome is not Outcome.AUTOMATIC:
        return Result(decision, "nothing merged and nothing dispatched.")
    if not reading.head:
        return Result(decision, "nothing merged and nothing dispatched; the head moved while the decision read the commits.")
    try:
        merged = github.merge(repository, number, reading.head, MERGE_METHOD)
    except GitHubError as error:
        refusal = _refusal(github, repository, number, error)
        if refusal is not None:
            return Result(decision, f"nothing merged and nothing dispatched; GitHub refused the merge: {refusal}")
        return Result(decision.failed(), f"the merge request failed: {error}")
    sha = str(merged.get("sha") or "")
    try:
        github.dispatch(repository, DISPATCH_EVENT, sha)
    except GitHubError as error:
        return Result(decision.failed(), f"merged `{reading.head[:7]}` with the {MERGE_METHOD} method as `{sha}`, but the `{DISPATCH_EVENT}` dispatch failed: {error}")
    return Result(decision, f"merged `{reading.head[:7]}` with the {MERGE_METHOD} method as `{sha}` and sent `{DISPATCH_EVENT}` with that commit.")


def run_workflow_run(github: GitHub, repository: str, head_branch: str) -> list[Result]:
    """When a required-check workflow completed: decide again for each open Dependabot pull request of the branch, and merge."""
    owner = repository.partition("/")[0]
    try:
        pulls = github.open_pull_requests(repository, f"{owner}:{head_branch}")
    except GitHubError as error:
        return [Result(Decision(Outcome.ERROR, f"the open pull requests could not be read: {error}"), "nothing merged and nothing dispatched.")]
    candidates = [pull for pull in pulls if (pull.get("user") or {}).get("login") == BOT]
    if not candidates:
        return [Result(Decision(Outcome.NO_PULL_REQUEST, f"no open pull request by `{BOT}` has the head branch `{head_branch}`."), "nothing merged and nothing dispatched.")]
    return [_merge(github, repository, pull) for pull in candidates]


def build_parser() -> argparse.ArgumentParser:
    """The command line of this script; the part's tests parse the workflow's invocations with it."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repository", required=True, help="OWNER/NAME of the repository the pull request belongs to")
    parser.add_argument("--summary", type=Path, help="file to append the run summary to, such as GITHUB_STEP_SUMMARY")
    events = parser.add_subparsers(dest="event", required=True)
    pull_request = events.add_parser("pull-request", help="decide on a pull request event and disable an auto-merge it still has")
    pull_request.add_argument("--number", required=True, type=int, help="the pull request number")
    workflow_run = events.add_parser("workflow-run", help="decide again when a required-check workflow completed, and merge")
    workflow_run.add_argument("--head-branch", required=True, help="the head branch of the completed run")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.event == "pull-request":
        results = [run_pull_request(GitHub(), arguments.repository, arguments.number)]
    else:
        results = run_workflow_run(GitHub(), arguments.repository, arguments.head_branch)
    summary = "\n".join(result.summary for result in results)
    print(summary)
    if arguments.summary:
        with arguments.summary.open("a", encoding="utf-8") as handle:
            handle.write(summary)
    return max(result.exit_code for result in results)


if __name__ == "__main__":
    raise SystemExit(main())
