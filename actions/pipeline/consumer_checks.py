#!/usr/bin/env python3
"""Check what a consumer holds beside its configuration file; the pipeline's hygiene job runs one command per step.

    python3 consumer_checks.py callers    [--kit DIR] [--today DATE] [--surface FILE] [common options]
    python3 consumer_checks.py register   [common options]
    python3 consumer_checks.py dependabot [--kit DIR] [common options]

    common options: [--root DIR] [--schemas DIR] [--event NAME] [--author LOGIN] [--repository OWNER/NAME]
                    [--api-url URL] [--summary FILE]

``callers`` compares the fixed callers ``ci.yml``, ``automation.yml`` and ``release.yml`` under ``.github/workflows/``
with their templates in the consumer kit, byte for byte. A caller that equals the previous version of its template,
``previous/<caller>`` in the kit, passes for one month after the promotion that changed the template, whose date the
kit's record ``fixed-callers.json`` holds. The CI guide ``docs/agents/ci.md`` equals its template in the kit,
at that path there, byte for byte up to and including the heading ``## Repository choices`` that closes the template;
below it a consumer records its own choices. Its previous version, ``previous/docs/agents/ci.md``, passes for the
same month. A kit without that template compares none. Every other workflow file under
``.github/workflows/`` must be registered under ``local-workflows`` in the configuration file. A caller or a CI guide
that differs is reported with the consumer's open pull request on the sync's branch, which the record names.
In the proof repository, which the record of the shared surface names with the candidate tag, a caller equals its template
except that it calls the shared workflow at the candidate tag instead of ``v1``; no other repository may call at
that tag.

``register`` asks GitHub for the issue that each entry of ``local-workflows`` names: a temporary entry names an issue
of the repository, a permanent entry a closed one. A pull request is no issue. A temporary entry whose request was
closed as not planned is a finding: the request was declined, so the entry becomes permanent, with
``declined-request`` in place of ``request`` and ``waits-for``. A temporary entry whose ``waits-for``
names a part of the current schema version is a finding too, the switch-over check: the part exists, so the consumer
switches it on, removes the workflow with its entry and closes the request. An entry that waits for a setting of an
existing part, ``<part>.<setting>``, is that finding once the part's configuration knows the setting. The free job,
``parts.custom``, is checked the same way when it names a ``request`` and what it ``waits-for``: its request is an
issue of the repository, a request that was closed as not planned leaves the free job without the two settings, and
the switch-over check tells the consumer to move the work to the part that exists now. One of the two settings
without the other is a finding.

``dependabot`` compares ``.github/dependabot.yml`` with the file generated from ``dependabot`` in the configuration
file: one ``updates`` entry per ecosystem and directory, in the file's order. Without ecosystems the repository holds
no Dependabot configuration. The summary holds the expected content.

A finding fails a run of a pull request whose author is not Dependabot. On any other run, a Dependabot pull request or
a push, a finding is a notice and the command passes. In the repository that the templates call, the repository of the
shared parts, every command passes without a check: it holds the templates, not the callers. The token for GitHub is
``GITHUB_TOKEN``. The exit code is 0 when the command passed, 1 when a finding failed it and 2 when the kit or its
record cannot be read.
"""

from __future__ import annotations

import argparse
import calendar
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

PART_FOLDER = Path(__file__).resolve().parent
sys.path.insert(0, str(PART_FOLDER))
import plan  # noqa: E402

KIT = PART_FOLDER / "consumer-kit"
KIT_PATH = "actions/pipeline/consumer-kit"  # the kit's path in the repository of the shared parts
PROOF_KIT_PATH = "actions/pipeline/proof-kit"
SURFACE = PART_FOLDER.parent / "repository-hygiene" / "shared-surface.json"
RECORD = "fixed-callers.json"
CALLERS = ("ci.yml", "automation.yml", "release.yml")
WORKFLOWS = ".github/workflows"
CI_GUIDE = "docs/agents/ci.md"  # the CI guide: its template is this path in the kit, when the kit holds it
CHOICES = b"## Repository choices\n"  # the heading that closes the template of the CI guide
DEPENDABOT_FILE = ".github/dependabot.yml"
DEPENDABOT_OTHER_NAME = ".github/dependabot.yaml"
DEPENDABOT_AUTHOR = "dependabot[bot]"
API_TIMEOUT = 20
PIPELINE_CALL = re.compile(r"(?m)^\s*uses:\s*([^/\s]+/[^/\s]+)/\.github/workflows/pipeline\.yml@")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class KitError(Exception):
    """The consumer kit or its record cannot be read; the message says why."""


class ApiError(Exception):
    """GitHub did not answer a request; the message says how."""


# --- the consumer kit -----------------------------------------------------------------------------------------------


def read_bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def shared_repository(kit: Path) -> str:
    """The repository of the shared parts: the one whose pipeline the template of ``ci.yml`` calls."""
    try:
        match = PIPELINE_CALL.search((kit / "ci.yml").read_text(encoding="utf-8"))
    except OSError as error:
        raise KitError(f"{kit / 'ci.yml'}: {error}") from error
    if match is None:
        raise KitError(f"{kit / 'ci.yml'} does not call the pipeline")
    return match.group(1)


def read_record(kit: Path) -> tuple[dict[str, datetime.date], str]:
    """The record of the kit: per fixed file with a previous version, a caller by its name and the CI guide by
    its path, the date of the promotion that changed its template, and the branch of the sync's pull request."""
    path = kit / RECORD
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise KitError(f"{path}: {error}") from error
    previous = record.get("previous") if isinstance(record, dict) else None
    branch = record.get("sync-branch") if isinstance(record, dict) else None
    if not isinstance(previous, dict) or not isinstance(branch, str) or not branch:
        raise KitError(f"{path}: expected the keys previous and sync-branch")
    promoted = {}
    for name, date in previous.items():
        if name not in (*CALLERS, CI_GUIDE) or not isinstance(date, str) or not DATE.match(date):
            raise KitError(f"{path}: previous names {name} with {date!r}; expected a fixed caller or {CI_GUIDE} with the date of its promotion as YYYY-MM-DD")
        if not (kit / "previous" / name).is_file():
            raise KitError(f"{path}: previous names {name}, but the kit holds no previous/{name}")
        try:
            promoted[name] = datetime.date.fromisoformat(date)
        except ValueError as error:
            raise KitError(f"{path}: previous names {name} with {date!r}: {error}") from error
    return promoted, branch


def candidate_tag(surface: Path, repository: str) -> str | None:
    """The candidate tag when ``repository`` is the proof repository that the record of the shared surface names; None in any other repository."""
    try:
        record = json.loads(surface.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise KitError(f"{surface}: {error}") from error
    proof, tag = (record.get(key) if isinstance(record, dict) else None for key in ("proof_repository", "candidate_tag"))
    if not isinstance(proof, str) or not proof or not isinstance(tag, str) or not tag:
        raise KitError(f"{surface}: expected the keys proof_repository and candidate_tag")
    return tag if repository == proof else None


def at_candidate(template: bytes | None, shared: str, tag: str | None) -> bytes | None:
    """The template as the proof repository holds it: its call of a shared workflow names the candidate tag instead of v1."""
    if template is None or tag is None:
        return template
    call = re.compile(rb"(?m)^(\s*uses:\s*" + re.escape(shared.encode()) + rb"/\.github/workflows/[^@\s]+@)v1([ \t]*\r?)$")
    return call.sub(lambda match: match.group(1) + tag.encode() + match.group(2), template)


def fixed_part(document: bytes) -> bytes:
    """A CI guide up to and including the first heading ``## Repository choices``, below which a consumer
    records its own choices; the whole document when it has no such heading."""
    if document.startswith(CHOICES):
        return CHOICES
    index = document.find(b"\n" + CHOICES)
    return document if index < 0 else document[:index + 1 + len(CHOICES)]


def ci_guide_for(template: bytes, held: bytes | None) -> bytes:
    """The CI guide that a consumer holds after it follows the template: the template, and below its closing
    heading ``## Repository choices`` what the consumer recorded below that heading."""
    if held is None or not template.endswith(CHOICES) or fixed_part(held) == held:
        return template
    return template + held[len(fixed_part(held)):]


def a_month_after(date: datetime.date) -> datetime.date:
    """The same day of the next month, or that month's last day when it is shorter."""
    year, month = (date.year + 1, 1) if date.month == 12 else (date.year, date.month + 1)
    return datetime.date(year, month, min(date.day, calendar.monthrange(year, month)[1]))


# --- GitHub ---------------------------------------------------------------------------------------------------------


class Api:
    """The GitHub API of one repository, read with the token of the run."""

    def __init__(self, base: str, repository: str, token: str | None) -> None:
        self.base, self.repository, self.token = base.rstrip("/"), repository, token

    def get(self, path: str) -> tuple[int, object]:
        """The status and the document of ``repos/<repository>/<path>``; a 404 is an answer, any other failure an ApiError."""
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(f"{self.base}/repos/{self.repository}/{path}", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:  # noqa: S310 - the API host of the run
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return 404, None
            raise ApiError(f"HTTP {error.code}") from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise ApiError(str(getattr(error, "reason", error))) from error


def sync_pull_request_note(api: Api, branch: str, shared: str) -> str:
    """Where a consumer finds the current fixed callers: its open pull request on the sync's branch, or the templates."""
    owner = api.repository.split("/", 1)[0]
    query = urllib.parse.urlencode({"state": "open", "head": f"{owner}:{branch}", "per_page": 1})
    try:
        _, pulls = api.get(f"pulls?{query}")
    except ApiError as error:
        return f"The sync pull request could not be looked up: {error}. The templates are at {templates_url(shared)}."
    if isinstance(pulls, list) and pulls and isinstance(pulls[0], dict) and pulls[0].get("html_url"):
        return f"The open sync pull request brings the fixed callers up to date: {pulls[0]['html_url']}"
    return f"No sync pull request is open on the branch `{branch}`. The templates are at {templates_url(shared)}."


def templates_url(shared: str) -> str:
    return f"https://github.com/{shared}/tree/v1/{KIT_PATH}"


def catalogue_url(shared: str) -> str:
    return f"https://github.com/{shared}/blob/v1/catalogue.json"


# --- the configuration file -----------------------------------------------------------------------------------------


def read_configuration(root: Path, schemas: Path) -> tuple[dict | None, list[str]]:
    """The configuration file when it matches its schema; otherwise None and each reason."""
    path = root / plan.CONFIGURATION
    if not path.is_file():
        return None, ["the file does not exist"]
    try:
        configuration, _ = plan.checked_configuration(path.read_text(encoding="utf-8"), plan.schema_versions(schemas))
    except (plan.PlanError, plan.SchemaError) as error:
        return None, str(error).splitlines()
    return configuration, []


def settings_of(schema: object) -> set[str]:
    """Every setting that a part's configuration knows: its own properties and those of the items of its lists."""
    if not isinstance(schema, dict):
        return set()
    properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    return set(properties).union(*(settings_of(inner) for inner in properties.values()), settings_of(schema.get("items")))


def existing_parts(schemas: Path) -> dict[str, set[str]]:
    """The shared parts that exist, each with the settings it knows: the parts of the current schema version of the
    pipeline's own revision. A part that always runs knows the settings of its configuration, and none without one."""
    versions = plan.schema_versions(schemas)
    current = versions[max(versions)]
    configurations = current.schema["properties"]["parts"]["properties"]
    return {part: settings_of(configurations.get(part)) for part in current.parts}


# --- the report -----------------------------------------------------------------------------------------------------


class Finding:
    """One finding of a check: its text, and the file that an annotation points to."""

    def __init__(self, text: str, file: str | None = None) -> None:
        self.text, self.file = text, file


def annotation(kind: str, finding: Finding) -> str:
    text = finding.text.replace("`", "").replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::{kind} file={finding.file}::{text}" if finding.file else f"::{kind}::{text}"


def report(options: argparse.Namespace, title: str, findings: list[Finding], passed: list[str], notes: list[Finding] = (), details: list[str] = ()) -> int:
    """Print the findings and write the summary; fail only a pull request whose author is not Dependabot. ``notes`` are
    notices on every run, ``details`` further summary lines below the findings."""
    enforced = options.event == "pull_request" and options.author != DEPENDABOT_AUTHOR
    lines = [f"## {title}", ""]
    for note in notes:
        print(annotation("notice", note))
    if not findings:
        lines += passed
    elif enforced:
        lines += ["**Failed.**", ""]
    else:
        reason = "This pull request is Dependabot's" if options.event == "pull_request" else "This run is no pull request"
        lines += [f"**Notice.** {reason}, so these findings do not fail it:", ""]
    for finding in findings:
        print(annotation("error" if enforced else "notice", finding))
    lines += [f"- {finding.text}" for finding in findings]
    if notes:
        lines += ["", *[f"- {note.text}" for note in notes]]
    if findings and details:
        lines += ["", *details]
    for line in passed if not findings else []:
        print(line)
    plan.append(options.summary, "\n".join(lines) + "\n")
    return 1 if findings and enforced else 0


def holds_the_shared_parts(options: argparse.Namespace, kit: Path, title: str) -> bool:
    """Whether the run is in the repository that the templates call; when it is, say that the check does not apply."""
    if options.repository != shared_repository(kit):
        return False
    line = "This repository holds the shared parts and the templates of the fixed callers, so it is no consumer: nothing to check."
    print(line)
    plan.append(options.summary, f"## {title}\n\n{line}\n")
    return True


# --- callers --------------------------------------------------------------------------------------------------------

CALLERS_TITLE = "Fixed callers and local workflows"


def run_callers(options: argparse.Namespace) -> int:
    kit, root = Path(options.kit), Path(options.root)
    if holds_the_shared_parts(options, kit, CALLERS_TITLE):
        return 0
    promoted, branch = read_record(kit)
    shared = shared_repository(kit)
    tag = candidate_tag(Path(options.surface), options.repository)
    today = options.today or datetime.datetime.now(datetime.timezone.utc).date()
    findings, notes, differs = [], [], False
    guide = read_bytes(kit / CI_GUIDE)
    for name in (*CALLERS, *([CI_GUIDE] if guide is not None else [])):
        path = CI_GUIDE if name == CI_GUIDE else f"{WORKFLOWS}/{name}"
        template, held = read_bytes(kit / name), read_bytes(root / path)
        if template is None:
            raise KitError(f"{kit} holds no template {name}")
        # Of the CI guide, only the part up to and including the heading of the repository's choices is fixed.
        compared = fixed_part(held) if name == CI_GUIDE and held is not None else held
        # In the proof repository a caller calls its shared workflow at the candidate tag; the CI guide names no tag.
        own_tag = None if name == CI_GUIDE else tag
        if compared == at_candidate(template, shared, own_tag):
            continue
        if name in promoted and compared == at_candidate(read_bytes(kit / "previous" / name), shared, own_tag):
            until = a_month_after(promoted[name])
            if today <= until:
                notes.append(Finding(f"`{path}` equals the previous version of its template, which passes until {until.isoformat()}; "
                                     "the sync pull request brings the current one.", path))
                continue
            findings.append(Finding(f"`{path}` equals the previous version of its template, which passed until {until.isoformat()}.", path))
        elif held is None:
            findings.append(Finding(f"`{path}` does not exist; every consumer holds this {'CI guide' if name == CI_GUIDE else 'fixed caller'}.", path))
        elif name != CI_GUIDE and tag:
            findings.append(Finding(f"`{path}` differs from its template at the candidate tag `{tag}`; in the proof repository a fixed caller equals its template "
                                    "except for that tag.", path))
        elif name != CI_GUIDE:
            findings.append(Finding(f"`{path}` differs from its template; a fixed caller equals its template byte for byte.", path))
        elif template.endswith(CHOICES):
            findings.append(Finding(f"`{path}` differs from its template up to and including the heading `{CHOICES.decode().strip()}`; "
                                    "the CI guide equals its template byte for byte there, and the repository's own choices go below that heading.", path))
        else:
            findings.append(Finding(f"`{path}` differs from its template; the CI guide equals its template byte for byte.", path))
        differs = True
    if differs and tag:
        findings.append(Finding(f"The proof kit holds its callers: https://github.com/{shared}/tree/v1/{PROOF_KIT_PATH}"))
    elif differs:
        findings.append(Finding(sync_pull_request_note(Api(options.api_url, options.repository, os.environ.get("GITHUB_TOKEN")), branch, shared)))
    configuration, reasons = read_configuration(root, Path(options.schemas))
    folder = root / WORKFLOWS
    local = sorted(f"{WORKFLOWS}/{item.name}" for item in (folder.iterdir() if folder.is_dir() else [])
                   if item.is_file() and item.suffix in (".yml", ".yaml") and item.name not in CALLERS)
    registered = []
    if configuration is None:
        findings.append(Finding(f"`{plan.CONFIGURATION}` cannot be used, so the local workflow register is unknown: {'; '.join(reasons)}.", plan.CONFIGURATION))
    else:
        register = plan.registered_workflows(configuration)
        registered = [path for path in local if path in register]
        unregistered = [path for path in local if path not in register]
        findings += [Finding(f"`{path}` is neither a fixed caller nor registered under `local-workflows` in `{plan.CONFIGURATION}`.", path) for path in unregistered]
        if unregistered:
            findings.append(Finding(f"The decision rule in the catalogue says where its work belongs: {catalogue_url(shared)}"))
    fixed = "Every fixed caller equals its template" if guide is None else "Every fixed caller and the CI guide equal their templates"
    if tag:
        fixed += f", except that a caller calls the shared workflow at the candidate tag `{tag}`"
    passed = [f"{fixed}, and every other workflow file is registered.", *[f"- `{path}` is registered." for path in registered]]
    return report(options, CALLERS_TITLE, findings, passed, notes)


# --- register -------------------------------------------------------------------------------------------------------

REGISTER_TITLE = "Local workflow register"
FREE_JOB = "custom"  # the part whose script may carry work that waits for a shared part


def unusable_configuration(reasons: list[str]) -> Finding:
    return Finding(f"`{plan.CONFIGURATION}` cannot be used: {'; '.join(reasons)}.", plan.CONFIGURATION)


@dataclass(frozen=True)
class Waiting:
    """What names a request of the consumer: an entry of the register, or the free job with its request.

    ``waits_for`` is empty for a permanent entry, which waits for nothing. ``removal`` says what the consumer removes
    once the part exists, and ``declined`` what it does about a request that was closed as not planned; a permanent
    entry needs neither.
    """

    subject: str
    request: int
    permanent: bool = False
    waits_for: str = ""
    removal: str = ""
    declined: str = ""


def issue_problem(api: Api, waiting: Waiting) -> str | None:
    """Why the issue is not one that the entry or the free job may name; None when it is."""
    try:
        status, issue = api.get(f"issues/{waiting.request}")
    except ApiError as error:
        return f"could not be read: {error}"
    if status == 404 or not isinstance(issue, dict):
        return "is no issue of this repository"
    if "pull_request" in issue:
        return "is a pull request and no issue"
    if waiting.permanent and issue.get("state") != "closed":
        return "is still open; a permanent entry names a closed request that was declined"
    if not waiting.permanent and issue.get("state") == "closed" and issue.get("state_reason") == "not_planned":
        return f"was closed as not planned: the request was declined, so {waiting.declined}"
    return None


def register_entry(entry: dict) -> Waiting:
    """An entry of the local workflow register, temporary or permanent."""
    subject = f"The entry of `{entry['workflow']}`"
    if "declined-request" in entry:
        return Waiting(subject, entry["declined-request"], permanent=True)
    return Waiting(subject, entry["request"], waits_for=entry["waits-for"], removal="remove the workflow and its entry",
                   declined=f"make the entry permanent, with `declined-request: {entry['request']}` in place of `request` and `waits-for`")


def free_job_request(request: int, waits_for: str) -> Waiting:
    """The free job that names its request and what it waits for."""
    return Waiting(f"The free job, `parts.{FREE_JOB}`,", request, waits_for=waits_for,
                   removal=f"remove the work from the free job with `request` and `waits-for` of `parts.{FREE_JOB}`",
                   declined=f"remove `request` and `waits-for` from `parts.{FREE_JOB}`; the work stays in the free job")


def run_register(options: argparse.Namespace) -> int:
    if holds_the_shared_parts(options, Path(options.kit), REGISTER_TITLE):
        return 0
    configuration, reasons = read_configuration(Path(options.root), Path(options.schemas))
    if configuration is None:
        return report(options, REGISTER_TITLE, [unusable_configuration(reasons)], [])
    entries = configuration.get("local-workflows") or []
    free_job = (configuration.get("parts") or {}).get(FREE_JOB) or {}
    stated = [key for key in ("request", "waits-for") if key in free_job]
    if not entries and not stated:
        return report(options, REGISTER_TITLE, [], ["The local workflow register is empty."])
    findings, switches = [], False
    if len(stated) == 1:
        missing = "waits-for" if stated == ["request"] else "request"
        findings.append(Finding(f"The free job, `parts.{FREE_JOB}`, states `{stated[0]}` without `{missing}`: a request for shared CI names both, or neither.", plan.CONFIGURATION))
    # What waits for a part: each entry of the register, and the free job when it names its request.
    waiting = [register_entry(entry) for entry in entries]
    if len(stated) == 2:
        waiting.append(free_job_request(free_job["request"], free_job["waits-for"]))
    api = Api(options.api_url, options.repository, os.environ.get("GITHUB_TOKEN"))
    existing = existing_parts(Path(options.schemas))
    for item in waiting:
        part, _, setting = item.waits_for.partition(".")
        if part in existing and (not setting or setting in existing[part]):
            switches = True
            what = (f"the setting `{setting}` of the part `{part}`, which exists now: state it under `parts.{part}` in" if setting else
                    f"the part `{part}`, which exists now: switch the part on under `parts` in")
            findings.append(Finding(f"{item.subject} waits for {what} `{plan.CONFIGURATION}`, {item.removal}, and close the request #{item.request}.", plan.CONFIGURATION))
        problem = issue_problem(api, item)
        if problem:
            findings.append(Finding(f"{item.subject} names the {'declined ' if item.permanent else ''}request #{item.request}, which {problem}.", plan.CONFIGURATION))
    if switches:
        findings.append(Finding(f"The catalogue describes the part: {catalogue_url(shared_repository(Path(options.kit)))}"))
    passed = "Every entry of the local workflow register names its issue, and no part that an entry waits for exists yet."
    if stated:
        passed = (f"{passed} The free job names its request, and what it waits for does not exist yet." if entries else
                  "The local workflow register is empty. The free job names its request, and what it waits for does not exist yet.")
    return report(options, REGISTER_TITLE, findings, [passed])


# --- dependabot -----------------------------------------------------------------------------------------------------

DEPENDABOT_TITLE = "Generated Dependabot configuration"
UPDATE_ENTRY = """  - package-ecosystem: {ecosystem}
    directory: {directory}
    schedule:
      interval: weekly
    cooldown:
      default-days: 7
    groups:
      minor-and-patch:
        patterns: ["*"]
        update-types: [minor, patch]
"""


def dependabot_text(ecosystems: dict[str, list[str]], shared: str) -> str:
    """The Dependabot configuration of a consumer: one entry per ecosystem and directory, in the order of the
    configuration file. The entry of GitHub Actions leaves the shared workflows alone, which a consumer calls at v1."""
    text = "version: 2\nupdates:\n"
    for ecosystem, directories in ecosystems.items():
        for directory in directories:
            text += UPDATE_ENTRY.format(ecosystem=ecosystem, directory=directory)
            if ecosystem == "github-actions":
                text += f"    ignore:\n      - dependency-name: {shared}\n"
    return text


def run_dependabot(options: argparse.Namespace) -> int:
    kit, root = Path(options.kit), Path(options.root)
    if holds_the_shared_parts(options, kit, DEPENDABOT_TITLE):
        return 0
    configuration, reasons = read_configuration(root, Path(options.schemas))
    if configuration is None:
        return report(options, DEPENDABOT_TITLE, [unusable_configuration(reasons)], [])
    ecosystems = configuration.get("dependabot") or {}
    held, other = read_bytes(root / DEPENDABOT_FILE), read_bytes(root / DEPENDABOT_OTHER_NAME)
    if not ecosystems:
        findings = [Finding(f"`{name}` exists, but `{plan.CONFIGURATION}` states no Dependabot ecosystems under `dependabot`: state them there or remove the file.", name)
                    for name, content in ((DEPENDABOT_FILE, held), (DEPENDABOT_OTHER_NAME, other)) if content is not None]
        return report(options, DEPENDABOT_TITLE, findings, [f"`{plan.CONFIGURATION}` states no Dependabot ecosystems, and the repository holds no Dependabot configuration."])
    expected = dependabot_text(ecosystems, shared_repository(kit))
    findings = []
    if held is None:
        findings.append(Finding(f"`{DEPENDABOT_FILE}` does not exist; it is generated from `dependabot` in `{plan.CONFIGURATION}`.", DEPENDABOT_FILE))
    elif held != expected.encode("utf-8"):
        findings.append(Finding(f"`{DEPENDABOT_FILE}` differs from the file generated from `dependabot` in `{plan.CONFIGURATION}`.", DEPENDABOT_FILE))
    if other is not None:
        findings.append(Finding(f"`{DEPENDABOT_OTHER_NAME}` exists; the generated file is `{DEPENDABOT_FILE}`.", DEPENDABOT_OTHER_NAME))
    details = [f"The expected content of `{DEPENDABOT_FILE}`:", "", "```yaml", *expected.splitlines(), "```"]
    return report(options, DEPENDABOT_TITLE, findings, [f"`{DEPENDABOT_FILE}` equals the file generated from `dependabot` in `{plan.CONFIGURATION}`."], details=details)


# --- the command line -----------------------------------------------------------------------------------------------


def parser() -> argparse.ArgumentParser:
    main_parser = argparse.ArgumentParser(description="Check a consumer's fixed callers, local workflow register and generated Dependabot configuration.")
    commands = main_parser.add_subparsers(dest="command", required=True)
    for name, text in (("callers", "compare the fixed callers with their templates and the other workflow files with the register"),
                       ("register", "ask GitHub for the issue that each entry of the register names"),
                       ("dependabot", "compare the Dependabot configuration with the one generated from the configuration file")):
        command = commands.add_parser(name, help=text)
        command.add_argument("--root", default=".", help="the repository's checkout")
        command.add_argument("--schemas", default=str(plan.SCHEMAS), help="the schema folder of the configuration file")
        command.add_argument("--event", default="", help="the name of the event of the run")
        command.add_argument("--author", default="", help="the login of the pull request's author, empty on another event")
        command.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""), help="the repository of the run, as OWNER/NAME")
        command.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"), help="the address of the GitHub API")
        command.add_argument("--summary", help="the file that receives the Markdown summary")
        command.add_argument("--kit", default=str(KIT), help="the consumer kit with the templates and their record")
    commands.choices["callers"].add_argument("--today", type=datetime.date.fromisoformat, help="the date of the run as YYYY-MM-DD; the current date in UTC without it")
    commands.choices["callers"].add_argument("--surface", default=str(SURFACE), help="the record of the shared surface, which names the proof repository and the candidate tag")
    return main_parser


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    try:
        return {"callers": run_callers, "register": run_register, "dependabot": run_dependabot}[options.command](options)
    except KitError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
