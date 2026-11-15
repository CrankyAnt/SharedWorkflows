#!/usr/bin/env python3
"""Keep the fixed files of every consumer equal to their templates, and compare each consumer's settings.

    python3 sync.py sync    [--own-repository OWNER/NAME] [common options]
    python3 sync.py sync    --local --repository OWNER/NAME [--repository ...] [common options]
    python3 sync.py adopt   --repository OWNER/NAME [--adoption FILE] [common options]
    python3 sync.py failure --own-repository OWNER/NAME --run-url URL [--api-url URL] [--summary FILE]

    common options: [--api-url URL] [--kit DIR] [--schemas DIR] [--ruleset FILE] [--merge-settings FILE]
                    [--summary FILE] [--now TIME]

``sync`` compares, for every repository it reaches, the fixed callers and the CI guide with their templates in
the consumer kit, ``.github/dependabot.yml`` with the file generated from the configuration file, the default-branch
ruleset with the ruleset template and the merge settings with ``merge-settings.json``. For file differences it opens one
pull request on the sync's branch, which the kit's record names, requests auto-merge and stops: it waits for nothing.
A pull request that is already open keeps its number; its branch moves when the files it should hold changed. Of the
CI guide only the part up to and including the heading ``## Repository choices``, which closes its template, is
compared and replaced: what a repository recorded below that heading stays.

Without ``--local`` the sync runs with the installation token of the sync App in ``GH_TOKEN`` and reaches the
repositories of that installation, which it asks GitHub for. A settings difference is reported, never changed. With
``--own-repository`` it opens, with the run's ``GITHUB_TOKEN``, one issue in that repository for each sync pull request
that is still open six hours after it was opened, each open issue with the label ``shared-ci`` in a repository it
reaches, and each repository whose settings differ. An issue is found again by a marker in its body: a pull request and
a request get one issue ever, a settings difference gets none while an issue with the same differences is open.

With ``--local`` the sync runs with the owner's own token in ``GH_TOKEN`` on the repositories that ``--repository``
names, and corrects each settings difference as well.

``adopt`` makes the adoption pull request of one repository in the local mode: the fixed callers, the CI guide,
a configuration file derived from the repository's workflows and its Dependabot configuration, and the generated
Dependabot configuration. A workflow whose every job calls a shared workflow that ``adoption.json`` maps to a part of
the pipeline leaves the repository, and its part is switched on with the inputs that are settings of the part. The
command reports everything it could not derive, in its output and in the pull request's body, and requests no
auto-merge.

``failure`` opens the issue for a failed run, unless one is open.

A repository without a configuration file has not adopted the pipeline: ``sync`` says so and leaves it alone. The
repository of the shared parts, which the template of ``ci.yml`` calls, is never synced; an installation of the App on
it fails the run before any write. The exit code is 0 when the command did its work, 1 when a repository could not be
synced or the command refused, and 2 when the kit, a template or the token is missing.
"""

from __future__ import annotations

import argparse
import base64
import copy
import datetime
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

PART_FOLDER = Path(__file__).resolve().parent
PIPELINE = PART_FOLDER.parent / "pipeline"
sys.path.insert(0, str(PIPELINE))
import consumer_checks  # noqa: E402
import plan  # noqa: E402
from dependabot_coverage import YamlError, parse_yaml  # noqa: E402  (plan puts the hygiene folder on the module path)

KIT = PIPELINE / "consumer-kit"
RULESET = PART_FOLDER.parent / "repository-ruleset" / "ruleset-main-template.json"
MERGE_SETTINGS = PART_FOLDER / "merge-settings.json"
ADOPTION = PART_FOLDER / "adoption.json"
CI_GUIDE, WORKFLOWS = consumer_checks.CI_GUIDE, consumer_checks.WORKFLOWS
DEPENDABOT_FILE, DEPENDABOT_OTHER_NAME = consumer_checks.DEPENDABOT_FILE, consumer_checks.DEPENDABOT_OTHER_NAME
REQUIRED_CHECK = "ci / verdict"
REQUEST_LABEL = "shared-ci"
ADOPTION_BRANCH = "chore/adopt-shared-pipeline"
SYNC_TITLE = "chore: sync the fixed files with their templates"
ADOPTION_TITLE = "chore: adopt the shared pipeline"
UNMERGED_AFTER = datetime.timedelta(hours=6)
MARKER = "<!-- sync-finding: {key} -->"
MARKER_PATTERN = re.compile(r"<!-- sync-finding: (\S+) -->")
API_TIMEOUT = 30
PAGE = 100
GLOB = re.compile(r"[*?\[]")
EXPRESSION = "${{"
PLAIN_SCALAR = re.compile(r"^[A-Za-z_./][A-Za-z0-9_./-]*$")
YAML_WORDS = {"true", "false", "null", "yes", "no", "on", "off"}


class SetupError(Exception):
    """The kit, a template or the token cannot be used; the message says why."""


class SyncError(Exception):
    """A repository could not be synced or a command refuses; the message says why."""


# --- GitHub ---------------------------------------------------------------------------------------------------------


class Api:
    """The GitHub API, asked with one token. A 404 is an answer; any other failure is a SyncError."""

    def __init__(self, base: str, token: str) -> None:
        self.base, self.token = base.rstrip("/"), token

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "Authorization": f"Bearer {self.token}"}
        data = None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode("utf-8"), "application/json"
        request = urllib.request.Request(f"{self.base}/{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:  # noqa: S310 - the API host of the run
                text = response.read().decode("utf-8")
                return response.status, json.loads(text) if text else None
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return 404, None
            raise SyncError(f"{method} {path}: HTTP {error.code}") from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise SyncError(f"{method} {path}: {getattr(error, 'reason', error)}") from error

    def get(self, path: str) -> object:
        """The document of a path, or None when it does not exist."""
        return self.request("GET", path)[1]

    def send(self, method: str, path: str, body: dict) -> dict:
        status, document = self.request(method, path, body)
        if status == 404 or not isinstance(document, dict):
            raise SyncError(f"{method} {path}: GitHub does not know this address, or the token may not use it")
        return document

    def pages(self, path: str, key: str | None = None) -> list[dict]:
        """Every item of a listing, read page by page; ``key`` names the list in a document that is a mapping."""
        items: list[dict] = []
        page = 1
        while True:
            document = self.get(f"{path}{'&' if '?' in path else '?'}per_page={PAGE}&page={page}")
            found = document.get(key) if key and isinstance(document, dict) else document
            if not isinstance(found, list):
                raise SyncError(f"GET {path}: expected a list")
            items += [item for item in found if isinstance(item, dict)]
            if len(found) < PAGE:
                return items
            page += 1

    def file(self, repository: str, path: str, ref: str) -> bytes | None:
        """The content of a file on a branch, or None when the branch holds no such file."""
        document = self.get(f"repos/{repository}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref, safe='')}")
        if not isinstance(document, dict) or document.get("type") != "file":
            return None
        return base64.b64decode(document.get("content") or "")

    def folder(self, repository: str, path: str, ref: str) -> list[str]:
        """The paths of the files directly in a folder on a branch."""
        document = self.get(f"repos/{repository}/contents/{urllib.parse.quote(path)}?ref={urllib.parse.quote(ref, safe='')}")
        return sorted(item["path"] for item in document if isinstance(item, dict) and item.get("type") == "file") if isinstance(document, list) else []


# --- the templates --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Templates:
    """What a consumer is compared with: the fixed files by their path in a consumer, the sync's branch, the repository
    of the shared parts, the schema versions, the expected ruleset and the expected merge settings."""

    fixed: dict[str, bytes]
    branch: str
    shared: str
    versions: dict
    ruleset: dict
    merge_settings: dict


def read_json(path: Path, what: str) -> dict:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SetupError(f"{what} {path}: {error}") from error
    if not isinstance(document, dict):
        raise SetupError(f"{what} {path}: expected a mapping")
    return document


def expected_ruleset(path: Path) -> dict:
    """The ruleset template with the verdict of the pipeline as its one required check."""
    ruleset = copy.deepcopy(read_json(path, "the ruleset template"))
    checks = [rule for rule in ruleset.get("rules", []) if isinstance(rule, dict) and rule.get("type") == "required_status_checks"]
    if len(checks) != 1:
        raise SetupError(f"the ruleset template {path}: expected one rule required_status_checks")
    parameters = checks[0].setdefault("parameters", {})
    kept = [check for check in parameters.get("required_status_checks", []) if check.get("context") == REQUIRED_CHECK]
    if len(kept) != 1:
        raise SetupError(f"the ruleset template {path} does not require the check {REQUIRED_CHECK}")
    parameters["required_status_checks"] = kept
    return ruleset


def read_templates(options: argparse.Namespace) -> Templates:
    kit = Path(options.kit)
    try:
        _, branch = consumer_checks.read_record(kit)
        shared = consumer_checks.shared_repository(kit)
        versions = plan.schema_versions(Path(options.schemas))
    except (consumer_checks.KitError, plan.SchemaError) as error:
        raise SetupError(str(error)) from error
    fixed = {}
    for name in consumer_checks.CALLERS:
        content = consumer_checks.read_bytes(kit / name)
        if content is None:
            raise SetupError(f"{kit} holds no template {name}")
        fixed[f"{WORKFLOWS}/{name}"] = content
    guide = consumer_checks.read_bytes(kit / CI_GUIDE)
    if guide is not None:
        fixed[CI_GUIDE] = guide
    return Templates(fixed, branch, shared, versions, expected_ruleset(Path(options.ruleset)), read_json(Path(options.merge_settings), "the merge settings"))


# --- the report -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """What a run reports as an issue in its own repository. ``ever`` means one issue ever for this key; otherwise none
    while an issue with the key is open."""

    key: str
    title: str
    body: str
    ever: bool


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    failed: bool = False

    def section(self, repository: str, lines: list[str]) -> None:
        if lines:
            self.lines += [f"### `{repository}`", "", *[f"- {line}" for line in lines], ""]


def publish(options: argparse.Namespace, title: str, lines: list[str]) -> None:
    text = "\n".join([f"## {title}", "", *lines]).rstrip("\n") + "\n"
    print(text, end="")
    plan.append(options.summary, text)


def code(value: object) -> str:
    return f"`{json.dumps(value, sort_keys=True)}`"


# --- pull requests --------------------------------------------------------------------------------------------------


def branch_tip(api: Api, repository: str, branch: str) -> str | None:
    document = api.get(f"repos/{repository}/git/ref/heads/{urllib.parse.quote(branch)}")
    return document["object"]["sha"] if isinstance(document, dict) else None


def tree_of(api: Api, repository: str, commit: str) -> str:
    document = api.get(f"repos/{repository}/git/commits/{commit}")
    if not isinstance(document, dict):
        raise SyncError(f"the commit {commit} could not be read")
    return document["tree"]["sha"]


def open_pull_request(api: Api, repository: str, branch: str) -> dict | None:
    owner = repository.split("/", 1)[0]
    query = urllib.parse.urlencode({"state": "open", "head": f"{owner}:{branch}"})
    pulls = api.get(f"repos/{repository}/pulls?{query}")
    return pulls[0] if isinstance(pulls, list) and pulls and isinstance(pulls[0], dict) else None


def propose(api: Api, repository: str, default: str, branch: str, changes: dict[str, bytes | None], title: str, body: str) -> dict:
    """Put the changed files, None for a file that leaves, as one commit on the default branch's tip onto ``branch`` and
    open its pull request, or keep the one that is open. The branch stays when it already holds these files."""
    tip = branch_tip(api, repository, default)
    if tip is None:
        raise SyncError(f"the branch {default} could not be read")
    entries = [{"path": path, "mode": "100644", "type": "blob", **({"sha": None} if content is None else {"content": content.decode("utf-8")})}
               for path, content in sorted(changes.items())]
    tree = api.send("POST", f"repos/{repository}/git/trees", {"base_tree": tree_of(api, repository, tip), "tree": entries})["sha"]
    held = branch_tip(api, repository, branch)
    if held is None or tree_of(api, repository, held) != tree:
        commit = api.send("POST", f"repos/{repository}/git/commits", {"message": title, "tree": tree, "parents": [tip]})["sha"]
        if held is None:
            api.send("POST", f"repos/{repository}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})
        else:
            api.send("PATCH", f"repos/{repository}/git/refs/heads/{urllib.parse.quote(branch)}", {"sha": commit, "force": True})
    pull = open_pull_request(api, repository, branch)
    if pull is None:
        pull = api.send("POST", f"repos/{repository}/pulls", {"title": title, "head": branch, "base": default, "body": body})
    return pull


AUTO_MERGE = "mutation($pullRequest: ID!, $method: PullRequestMergeMethod!) { enablePullRequestAutoMerge(input: {pullRequestId: $pullRequest, mergeMethod: $method}) { pullRequest { number } } }"


def request_auto_merge(api: Api, pull: dict) -> str | None:
    """Request auto-merge with a squash; the reason when GitHub refuses."""
    answer = api.send("POST", "graphql", {"query": AUTO_MERGE, "variables": {"pullRequest": pull["node_id"], "method": "SQUASH"}})
    errors = answer.get("errors")
    if errors:
        return "; ".join(str(error.get("message", error)) if isinstance(error, dict) else str(error) for error in errors)
    return None


# --- the files of a consumer ----------------------------------------------------------------------------------------


def file_changes(api: Api, repository: str, default: str, templates: Templates, configuration_text: bytes, notes: list[str]) -> dict[str, bytes | None]:
    """The files of the default branch that differ from what the templates and the configuration file give."""
    changes: dict[str, bytes | None] = {}
    for path, content in templates.fixed.items():
        held = api.file(repository, path, default)
        if path == CI_GUIDE:
            # Only the part up to and including the heading of the repository's choices is fixed; what follows it stays.
            if held is None or consumer_checks.fixed_part(held) != content:
                changes[path] = consumer_checks.ci_guide_for(content, held)
        elif held != content:
            changes[path] = content
    held, other = api.file(repository, DEPENDABOT_FILE, default), api.file(repository, DEPENDABOT_OTHER_NAME, default)
    try:
        configuration, _ = plan.checked_configuration(configuration_text.decode("utf-8"), templates.versions)
    except (plan.PlanError, UnicodeDecodeError) as error:
        notes.append(f"`{plan.CONFIGURATION}` cannot be used, so `{DEPENDABOT_FILE}` was not compared: {'; '.join(str(error).splitlines())}.")
        return changes
    ecosystems = configuration.get("dependabot") or {}
    if not ecosystems:
        notes += [f"`{name}` exists, but `{plan.CONFIGURATION}` states no Dependabot ecosystems; the repository states them or removes the file."
                  for name, content in ((DEPENDABOT_FILE, held), (DEPENDABOT_OTHER_NAME, other)) if content is not None]
        return changes
    expected = consumer_checks.dependabot_text(ecosystems, templates.shared).encode("utf-8")
    if held != expected:
        changes[DEPENDABOT_FILE] = expected
    if other is not None:
        changes[DEPENDABOT_OTHER_NAME] = None
    return changes


def sync_body(changes: dict[str, bytes | None], shared: str) -> str:
    lines = [f"The sync of `{shared}` brings these files in line with their templates at `v1` and with the configuration file:", ""]
    lines += [f"- `{path}`{' (removed)' if content is None else ''}" for path, content in sorted(changes.items())]
    lines += ["", "The fixed callers and the CI guide equal their templates byte for byte in every repository; "
              f"`{DEPENDABOT_FILE}` is generated from `dependabot` in `{plan.CONFIGURATION}`. This pull request merges itself once its required checks pass."]
    return "\n".join(lines) + "\n"


# --- settings -------------------------------------------------------------------------------------------------------


def holds(expected: object, actual: object) -> bool:
    """Whether ``actual`` holds everything ``expected`` states: a mapping each key, a list the same items in any order."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and holds(value, actual[key]) for key, value in expected.items())
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        left = list(actual)
        for item in expected:
            match = next((index for index, candidate in enumerate(left) if holds(item, candidate)), None)
            if match is None:
                return False
            del left[match]
        return True
    return expected == actual


def merge_differences(held: dict, expected: dict) -> dict[str, str]:
    """Per merge setting that differs, what GitHub answered beside what the shared parts rely on."""
    differences = {}
    for name, value in expected.items():
        if name not in held:
            differences[name] = f"`{name}` could not be read; expected {code(value)}"
        elif held[name] != value:
            differences[name] = f"`{name}` is {code(held[name])}, expected {code(value)}"
    return differences


def find_ruleset(api: Api, repository: str, expected: dict) -> dict | None:
    listed = api.get(f"repos/{repository}/rulesets")
    for item in listed if isinstance(listed, list) else []:
        if isinstance(item, dict) and item.get("name") == expected["name"] and item.get("target") == expected.get("target"):
            ruleset = api.get(f"repos/{repository}/rulesets/{item['id']}")
            return ruleset if isinstance(ruleset, dict) else None
    return None


def ruleset_differences(held: dict | None, expected: dict) -> list[str]:
    name = expected["name"]
    if held is None:
        return [f"the ruleset `{name}` does not exist"]
    differences = []
    for key, value in expected.items():
        if key == "bypass_actors" and key not in held:
            continue  # GitHub shows the bypass list only to a token that administers the repository
        if not holds(value, held.get(key)):
            differences.append(f"the ruleset `{name}`: `{key}` is {code(held.get(key))}, expected {code(value)}")
    return differences


def settings(api: Api, repository: str, held: dict, templates: Templates, correct: bool) -> tuple[list[str], list[str]]:
    """The settings differences of a repository and, in the local mode, what was corrected."""
    merge = merge_differences(held, templates.merge_settings)
    ruleset = find_ruleset(api, repository, templates.ruleset)
    rules = ruleset_differences(ruleset, templates.ruleset)
    differences = [*merge.values(), *rules]
    if not correct or not differences:
        return differences, []
    if merge:
        api.send("PATCH", f"repos/{repository}", {name: templates.merge_settings[name] for name in merge})
    if rules and ruleset is None:
        api.send("POST", f"repos/{repository}/rulesets", templates.ruleset)
    elif rules:
        api.send("PUT", f"repos/{repository}/rulesets/{ruleset['id']}", templates.ruleset)
    return [], differences


# --- one repository -------------------------------------------------------------------------------------------------


def moment(text: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def sync_repository(api: Api, repository: str, templates: Templates, options: argparse.Namespace, report: Report) -> None:
    held = api.get(f"repos/{repository}")
    if not isinstance(held, dict):
        raise SyncError("the repository could not be read")
    if held.get("archived"):
        return
    default = held.get("default_branch") or "main"
    configuration = api.file(repository, plan.CONFIGURATION, default)
    if configuration is None:
        report.section(repository, [f"`{repository}` holds no `{plan.CONFIGURATION}`: it has not adopted the pipeline. `sync.py adopt` makes its adoption pull request."])
        return
    lines: list[str] = []
    differences, corrected = settings(api, repository, held, templates, correct=options.local)
    lines += [f"Corrected: {difference}." for difference in corrected]
    lines += [f"Settings difference: {difference}." for difference in differences]
    if differences:
        digest = hashlib.sha256("\n".join(differences).encode("utf-8")).hexdigest()[:12]
        body = "\n".join([f"The settings of `{repository}` differ from what the shared parts rely on:", "", *[f"- {difference}" for difference in differences], "",
                          "The sync changes no setting with the App's token. Its local mode corrects them: `sync.py sync --local --repository <repository>`."])
        report.findings.append(Finding(f"settings:{repository}:{digest}", f"Settings differ: {repository}", body, ever=False))
    if options.own_repository:
        for issue in api.pages(f"repos/{repository}/issues?state=open&labels={REQUEST_LABEL}"):
            if "pull_request" not in issue:
                reference = f"{repository}#{issue['number']}"
                body = f"An open request for shared CI: [{reference}]({issue.get('html_url', '')}), \"{issue.get('title', '')}\"."
                report.findings.append(Finding(f"request:{reference}", f"Request for shared CI: {reference}", body, ever=True))
    changes = file_changes(api, repository, default, templates, configuration, lines)
    if changes:
        existing = open_pull_request(api, repository, templates.branch)
        if existing is not None and options.now - moment(existing["created_at"]) >= UNMERGED_AFTER:
            reference = f"{repository}#{existing['number']}"
            body = f"The sync pull request [{reference}]({existing.get('html_url', '')}) was opened at {existing['created_at']} and is still open."
            report.findings.append(Finding(f"pull-request:{reference}", f"Sync pull request not merged after six hours: {reference}", body, ever=True))
        pull = propose(api, repository, default, templates.branch, changes, SYNC_TITLE, sync_body(changes, templates.shared))
        names = ", ".join(f"`{path}`" for path in sorted(changes))
        lines.append(f"{'The open' if existing is not None else 'A new'} sync pull request brings {names} in line: {pull.get('html_url', '')}")
        if not pull.get("auto_merge"):
            refusal = request_auto_merge(api, pull)
            if refusal:
                lines.append(f"For that pull request auto-merge could not be enabled: {refusal}")
    report.section(repository, lines)


# --- issues in the run's own repository -----------------------------------------------------------------------------


def report_findings(api: Api, repository: str, findings: list[Finding]) -> list[str]:
    """Open one issue per finding that has none yet; the lines of the issues that were opened."""
    if not findings:
        return []
    ever: set[str] = set()
    still_open: set[str] = set()
    for issue in api.pages(f"repos/{repository}/issues?state=all"):
        keys = MARKER_PATTERN.findall(issue.get("body") or "")
        ever.update(keys)
        if issue.get("state") == "open":
            still_open.update(keys)
    lines = []
    for finding in findings:
        if finding.key in (ever if finding.ever else still_open):
            continue
        issue = api.send("POST", f"repos/{repository}/issues", {"title": finding.title, "body": f"{finding.body}\n\n{MARKER.format(key=finding.key)}\n"})
        ever.add(finding.key)
        still_open.add(finding.key)
        lines.append(f"Opened the issue #{issue.get('number')}: {finding.title}")
    return lines


def token(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SetupError(f"the environment holds no token in {name}")
    return value


# --- sync -----------------------------------------------------------------------------------------------------------


def run_sync(options: argparse.Namespace) -> int:
    templates = read_templates(options)
    api = Api(options.api_url, token("GH_TOKEN"))
    if options.local:
        if not options.repository:
            raise SetupError("the local mode names its repositories with --repository")
        repositories = list(options.repository)
    else:
        if options.repository:
            raise SetupError("without --local the repositories come from the App's installation; --repository belongs to the local mode")
        repositories = [item["full_name"] for item in api.pages("installation/repositories", "repositories") if not item.get("archived")]
    if templates.shared in repositories:
        where = "--repository names" if options.local else "the App is installed on"
        raise SyncError(f"{where} the repository of the shared parts, which holds the templates and is never synced")
    report = Report()
    for repository in repositories:
        if repository == options.own_repository:
            continue
        try:
            sync_repository(api, repository, templates, options, report)
        except SyncError as error:
            report.failed = True
            report.section(repository, [f"`{repository}` could not be synced: {error}"])
    lines = report.lines
    if options.own_repository:
        opened = report_findings(Api(options.api_url, token("GITHUB_TOKEN")), options.own_repository, report.findings)
        lines += [*[f"- {line}" for line in opened], ""] if opened else []
    publish(options, "Sync", lines or ["Every repository that the sync reached matches the templates."])
    return 1 if report.failed else 0


def run_failure(options: argparse.Namespace) -> int:
    body = f"The run {options.run_url} of the sync failed. Its log says which repository could not be synced and why."
    opened = report_findings(Api(options.api_url, token("GITHUB_TOKEN")), options.own_repository, [Finding("failure", "The sync failed", body, ever=False)])
    publish(options, "Sync failure", [f"- {line}" for line in opened] or ["An issue for a failed run is already open."])
    return 0


# --- adoption -------------------------------------------------------------------------------------------------------


def yaml_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and PLAIN_SCALAR.match(value) and value.lower() not in YAML_WORDS:
        return value
    return json.dumps(value)


def yaml_lines(value: dict, indent: int = 0) -> list[str]:
    """A mapping as block YAML; a list is a flow list of scalars and an empty mapping is ``{}``."""
    lines = []
    for key, item in value.items():
        if isinstance(item, dict) and item:
            lines += [f"{' ' * indent}{key}:", *yaml_lines(item, indent + 2)]
        elif isinstance(item, dict):
            lines.append(f"{' ' * indent}{key}: {{}}")
        elif isinstance(item, list):
            lines.append(f"{' ' * indent}{key}: [{', '.join(yaml_scalar(entry) for entry in item)}]")
        else:
            lines.append(f"{' ' * indent}{key}: {yaml_scalar(item)}")
    return lines


def is_plain(value: object) -> bool:
    """Whether a value of an input can be a setting: a scalar or a list of scalars, without an expression."""
    items = value if isinstance(value, list) else [value]
    return all(isinstance(item, (str, int, bool)) and not (isinstance(item, str) and EXPRESSION in item) for item in items)


def derive_parts(workflows: dict[str, str], templates: Templates, table: dict[str, str], notes: list[str]) -> tuple[dict[str, dict], list[str]]:
    """The parts that the repository's workflows switch on, and the workflows that leave because parts replace them."""
    version = templates.versions[max(templates.versions)]
    settings_of = version.schema["properties"]["parts"]["properties"]
    call = re.compile(rf"^{re.escape(templates.shared)}/\.github/workflows/([^/@]+)@")
    parts: dict[str, dict] = {}
    replaced = []
    for path, text in sorted(workflows.items()):
        fixed = path in templates.fixed
        stays = (f"`{path}` is replaced by the fixed caller of that name and its work is not carried over" if fixed
                 else f"`{path}` stays, and hygiene fails on it until `local-workflows` registers it or a part or the free job takes its work")
        try:
            document = parse_yaml(text)
        except YamlError as error:
            notes.append(f"{stays}: it could not be read ({error}).")
            continue
        jobs = document.get("jobs") if isinstance(document, dict) else None
        jobs = {name: job for name, job in jobs.items() if isinstance(job, dict)} if isinstance(jobs, dict) else {}
        called = {name: (call.match(str(job.get("uses", ""))) or [None, None])[1] for name, job in jobs.items()}
        own = sorted(name for name, workflow in called.items() if workflow is None)
        unknown = sorted({workflow for workflow in called.values() if workflow and table.get(workflow) not in version.parts})
        if not jobs or own or unknown:
            reasons = ([f"its job{'s' if len(own) > 1 else ''} {', '.join(f'`{name}`' for name in own)} call{'' if len(own) > 1 else 's'} no shared workflow"] if own else [])
            reasons += [f"no part of the pipeline replaces `{workflow}` yet" for workflow in unknown]
            notes.append(f"{stays}: {'; '.join(reasons) or 'it holds no job'}.")
            continue
        for name, job in jobs.items():
            part = table[called[name]]
            inputs = job.get("with") if isinstance(job.get("with"), dict) else {}
            if part in version.always:
                notes += [f"`{path}`: the input `{key}` is not carried over; the part `{part}` always runs and has no settings." for key in inputs]
                continue
            if part in parts:
                notes.append(f"`{path}`: the job `{name}` is a second call of the part `{part}`, which the configuration file states once; its inputs are not carried over.")
                continue
            known = settings_of[part].get("properties", {})
            parts[part] = {key: value for key, value in inputs.items() if key in known and is_plain(value)}
            notes += [f"`{path}`: the input `{key}` is not carried over; " + (f"it is no setting of the part `{part}`." if key not in known else "its value is no plain value.")
                      for key, value in inputs.items() if key not in parts[part]]
        if not fixed:
            replaced.append(path)
    return parts, replaced


def derive_dependabot(text: str | None, name: str, expected_entry: dict, notes: list[str]) -> dict[str, list[str]]:
    """The Dependabot ecosystems with their directories that an existing Dependabot configuration states."""
    if text is None:
        return {}
    try:
        document = parse_yaml(text)
    except YamlError as error:
        notes.append(f"`{name}` could not be read ({error}), so no Dependabot ecosystem is derived from it.")
        return {}
    document = document if isinstance(document, dict) else {}
    notes += [f"`{name}`: the key `{key}` is not carried over; the generated file holds no such key." for key in document if key not in ("version", "updates")]
    ecosystems: dict[str, list[str]] = {}
    for entry in document.get("updates") if isinstance(document.get("updates"), list) else []:
        ecosystem = entry.get("package-ecosystem") if isinstance(entry, dict) else None
        if not isinstance(ecosystem, str):
            notes.append(f"`{name}`: an entry without `package-ecosystem` is not carried over.")
            continue
        directories = entry.get("directories") if isinstance(entry.get("directories"), list) else [entry.get("directory")]
        for directory in directories:
            if not isinstance(directory, str) or not directory.startswith("/") or GLOB.search(directory):
                notes.append(f"`{name}`: the directory {code(directory)} of `{ecosystem}` is not carried over; the configuration file names each directory as a path that starts with `/`.")
            elif directory not in ecosystems.setdefault(ecosystem, []):
                ecosystems[ecosystem].append(directory)
        lost = sorted(key for key, value in entry.items() if key not in ("package-ecosystem", "directory", "directories") and expected_entry.get(key) != value
                      and not (ecosystem == "github-actions" and key == "ignore" and is_shared_ignore(value)))
        notes += [f"`{name}`: `{key}` of the entry of `{ecosystem}` is not carried over; every generated entry holds the same schedule, cooldown and group."
                  for key in lost]
    return {ecosystem: directories for ecosystem, directories in ecosystems.items() if directories}


def is_shared_ignore(value: object) -> bool:
    """Whether an ``ignore`` list holds one entry that only names a dependency, as the generated entry of GitHub Actions does."""
    return isinstance(value, list) and len(value) == 1 and isinstance(value[0], dict) and set(value[0]) == {"dependency-name"}


def run_adopt(options: argparse.Namespace) -> int:
    templates = read_templates(options)
    table = read_json(Path(options.adoption), "the adoption table").get("shared-workflows")
    if not isinstance(table, dict):
        raise SetupError(f"the adoption table {options.adoption}: expected the key shared-workflows")
    if len(options.repository or []) != 1:
        raise SetupError("adopt names one repository with --repository")
    repository = options.repository[0]
    if repository == templates.shared:
        raise SyncError("--repository names the repository of the shared parts, which holds the templates and is no consumer")
    api = Api(options.api_url, token("GH_TOKEN"))
    held = api.get(f"repos/{repository}")
    if not isinstance(held, dict):
        raise SyncError(f"`{repository}` could not be read")
    default = held.get("default_branch") or "main"
    if api.file(repository, plan.CONFIGURATION, default) is not None:
        raise SyncError(f"`{repository}` holds `{plan.CONFIGURATION}` already: it adopted the pipeline, and `sync.py sync` keeps its fixed files")
    notes: list[str] = []
    workflows = {path: (api.file(repository, path, default) or b"").decode("utf-8", "replace")
                 for path in api.folder(repository, WORKFLOWS, default) if path.endswith((".yml", ".yaml"))}
    parts, replaced = derive_parts(workflows, templates, table, notes)
    files = {name: api.file(repository, name, default) for name in (DEPENDABOT_FILE, DEPENDABOT_OTHER_NAME)}
    version = templates.versions[max(templates.versions)]
    expected_entry = parse_yaml(consumer_checks.dependabot_text({"x": ["/"]}, templates.shared))["updates"][0]
    ecosystems: dict[str, list[str]] = {}
    for name, content in files.items():
        for ecosystem, directories in derive_dependabot(None if content is None else content.decode("utf-8", "replace"), name, expected_entry, notes).items():
            ecosystems.setdefault(ecosystem, [])
            ecosystems[ecosystem] += [directory for directory in directories if directory not in ecosystems[ecosystem]]
    document = {"version": version.version, **({"parts": parts} if parts else {}), **({"dependabot": ecosystems} if ecosystems else {})}
    text = "\n".join(yaml_lines(document)) + "\n"
    try:
        read_back, _ = plan.checked_configuration(text, templates.versions)
    except plan.PlanError as error:
        raise SyncError(f"the derived configuration file does not match its schema: {'; '.join(str(error).splitlines())}") from error
    if read_back != document:
        raise SyncError("the derived configuration file does not read back as it was derived")
    changes: dict[str, bytes | None] = {**templates.fixed, plan.CONFIGURATION: text.encode("utf-8"), **{path: None for path in replaced}}
    if CI_GUIDE in templates.fixed:
        changes[CI_GUIDE] = consumer_checks.ci_guide_for(templates.fixed[CI_GUIDE], api.file(repository, CI_GUIDE, default))
    if ecosystems:
        changes[DEPENDABOT_FILE] = consumer_checks.dependabot_text(ecosystems, templates.shared).encode("utf-8")
        changes.update({DEPENDABOT_OTHER_NAME: None} if files[DEPENDABOT_OTHER_NAME] is not None else {})
    else:
        for name, content in files.items():
            if content is not None:
                changes[name] = None
                notes.append(f"`{name}` leaves the repository: no Dependabot ecosystem could be derived from it, and without ecosystems a repository holds no Dependabot configuration.")
    derived = [f"`{plan.CONFIGURATION}` switches on: {', '.join(f'`{part}`' for part in parts) or 'no part'}; repository hygiene always runs.",
               f"Dependabot ecosystems: {', '.join(f'`{ecosystem}` in {code(directories)}' for ecosystem, directories in ecosystems.items()) or 'none'}.",
               *[f"`{path}` leaves the repository: its part replaces it." for path in replaced]]
    missing = notes or ["Nothing: every workflow and every Dependabot entry is carried over."]
    body = "\n".join([f"The repository adopts the shared pipeline of `{templates.shared}`: the fixed callers"
                      f"{', the CI guide' if CI_GUIDE in templates.fixed else ''} and the configuration file `{plan.CONFIGURATION}`.", "",
                      "## Derived", "", *[f"- {line}" for line in derived], "", "## Not derived", "", *[f"- {line}" for line in missing], "",
                      f"After the merge, the default-branch ruleset requires `{REQUIRED_CHECK}`; the sync's local mode sets it."]) + "\n"
    pull = propose(api, repository, default, ADOPTION_BRANCH, changes, ADOPTION_TITLE, body)
    publish(options, "Adoption", [f"The adoption pull request of `{repository}`: {pull.get('html_url', '')}", "", "### Derived", "", *[f"- {line}" for line in derived], "",
                                  "### Not derived", "", *[f"- {line}" for line in missing]])
    return 0


# --- the command line -----------------------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    main_parser = argparse.ArgumentParser(description="Keep the fixed files of every consumer equal to their templates, and compare each consumer's settings.")
    commands = main_parser.add_subparsers(dest="command", required=True)
    for name, text in (("sync", "compare every repository with the templates, open pull requests and report or correct settings"),
                       ("adopt", "make the adoption pull request of one repository, in the local mode"),
                       ("failure", "open the issue for a failed run")):
        command = commands.add_parser(name, help=text)
        command.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"), help="the address of the GitHub API")
        command.add_argument("--summary", help="the file that receives the Markdown summary")
        command.add_argument("--own-repository", default="", help="the repository of the run, as OWNER/NAME: it is not synced and receives the issues")
        if name == "failure":
            command.add_argument("--run-url", required=True, help="the address of the failed run")
            continue
        command.add_argument("--kit", default=str(KIT), help="the consumer kit with the templates and their record")
        command.add_argument("--schemas", default=str(plan.SCHEMAS), help="the schema folder of the configuration file")
        command.add_argument("--ruleset", default=str(RULESET), help="the ruleset template")
        command.add_argument("--merge-settings", default=str(MERGE_SETTINGS), help="the merge settings that the shared parts rely on")
        command.add_argument("--adoption", default=str(ADOPTION), help="the table from an earlier shared workflow to the part that replaces it")
        command.add_argument("--repository", action="append", help="a repository of the local mode, as OWNER/NAME")
        command.add_argument("--now", type=moment, default=datetime.datetime.now(datetime.timezone.utc), help="the time of the run; the current time without it")
    commands.choices["sync"].add_argument("--local", action="store_true", help="run with the owner's own token on the named repositories and correct settings")
    return main_parser


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    if options.command != "sync":
        options.local = True
    try:
        return {"sync": run_sync, "adopt": run_adopt, "failure": run_failure}[options.command](options)
    except SetupError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2
    except SyncError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
