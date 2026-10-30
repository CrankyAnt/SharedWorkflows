"""Propose newer releases for every pin in pins.json after a cooldown of seven days by default.

    python3 update_pins.py [--pins pins.json] [--apply] [--cooldown-days N] [--output GITHUB_OUTPUT] [--summary FILE] [--pr-body FILE]

For each tool the newest stable GitHub release that is at least the cooldown old and newer than the pin is
proposed; for Node.js the newest release of the pinned major line that is at least the cooldown old. A proposed
tool release refreshes every platform entry: each asset is downloaded and its sha256 taken from the upstream
checksums file and checked against the download, or computed from the download when the upstream publishes no
checksums file. A checksum mismatch fails the run and proposes nothing. An unreachable upstream leaves that pin
unchanged and is listed in the summary. Without ``--apply`` nothing is written.

For each Python tool the newest stable PyPI release under the same rule is proposed. pip resolves the tool's
requirement at that version, and the build requirements of its sdist, in a temporary virtual environment; every
resolved package is locked with the PyPI sha256 of each of its distributions that installs on linux-x64 or
windows-x64 from CPython 3.12 on. A resolved package younger than the cooldown leaves the pin unchanged.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pins import PINS_PATH, PinsError, asset_url, dump_pins, load_pins, render, version_key  # noqa: E402

COOLDOWN_DAYS = 7
RELEASES_URL = "https://api.github.com/repos/{repository}/releases?per_page=50"
NODE_INDEX_URL = "https://nodejs.org/dist/index.json"
PYPI_URL = "https://pypi.org/pypi/{name}/json"
PYPI_VERSION_URL = "https://pypi.org/pypi/{name}/{version}/json"
RELEASE_TAG = re.compile(r"^v?([0-9]+\.[0-9]+\.[0-9]+)$")
WHEEL = re.compile(r"^[^-]+-[^-]+(?:-[0-9][^-]*)?-(?P<python>[^-]+)-(?P<abi>[^-]+)-(?P<platform>[^-]+)\.whl$")
LOCKED_PLATFORMS = re.compile(r"^(?:any|win_amd64|manylinux[0-9_]*_x86_64)$")
OLDEST_PYTHON = (3, 12)
Fetcher = Callable[[str], bytes]
Resolver = Callable[[list[str], datetime], dict[str, str]]


@dataclass(frozen=True)
class Proposal:
    tool: str
    old: str
    new: str
    sha256_source: str
    platforms: dict[str, dict[str, str]] = field(default_factory=dict)
    packages: dict[str, dict] = field(default_factory=dict)
    build: list[str] = field(default_factory=list)


@dataclass
class Plan:
    proposals: list[Proposal] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    unreachable: list[str] = field(default_factory=list)
    current: list[str] = field(default_factory=list)
    pins: dict = field(default_factory=dict)

    @property
    def changed(self) -> bool:
        return bool(self.proposals)

    @property
    def failed(self) -> bool:
        return bool(self.failures)


class Unreachable(RuntimeError):
    """The upstream could not be read; the pin stays as it is."""


class ChecksumMismatch(RuntimeError):
    """A downloaded asset differs from the upstream checksums file."""


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "SharedWorkflows-update-pins"})
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310 - fixed upstream hosts
        return response.read()


def _read(fetcher: Fetcher, url: str) -> bytes:
    try:
        return fetcher(url)
    except (OSError, urllib.error.URLError, TimeoutError) as error:
        raise Unreachable(f"{url}: {error}") from error


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def eligible_release(releases: object, pinned: str, now: datetime, cooldown_days: int = COOLDOWN_DAYS) -> str | None:
    """The newest stable release version that is newer than ``pinned`` and at least the cooldown old."""
    if not isinstance(releases, list):
        raise Unreachable("the release list is not a JSON array")
    cutoff = now - timedelta(days=cooldown_days)
    candidates: list[str] = []
    for release in releases:
        if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
            continue
        match = RELEASE_TAG.match(str(release.get("tag_name", "")))
        published = _parse_time(release.get("published_at"))
        if not match or published is None or published > cutoff:
            continue
        version = match.group(1)
        if version_key(version) > version_key(pinned):
            candidates.append(version)
    return max(candidates, key=version_key) if candidates else None


def eligible_node(index: object, pinned: str, now: datetime, cooldown_days: int = COOLDOWN_DAYS) -> str | None:
    """The newest Node.js release of the pinned major line that is at least the cooldown old."""
    if not isinstance(index, list):
        raise Unreachable("the Node.js index is not a JSON array")
    major = version_key(pinned)[0]
    cutoff = (now - timedelta(days=cooldown_days)).date()
    candidates: list[str] = []
    for entry in index:
        if not isinstance(entry, dict):
            continue
        match = RELEASE_TAG.match(str(entry.get("version", "")))
        if not match:
            continue
        version = match.group(1)
        try:
            released = datetime.strptime(str(entry.get("date", "")), "%Y-%m-%d").date()
        except ValueError:
            continue
        if version_key(version)[0] == major and version_key(version) > version_key(pinned) and released <= cutoff:
            candidates.append(version)
    return max(candidates, key=version_key) if candidates else None


def parse_checksums(text: str) -> dict[str, str]:
    """``<sha256>  <name>`` or ``<sha256> *<name>`` lines, keyed by asset name."""
    checksums: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            checksums[parts[-1].lstrip("*")] = parts[0].lower()
    return checksums


def refresh_tool(name: str, tool: dict, new_version: str, fetcher: Fetcher) -> Proposal:
    """Download every platform asset of ``new_version`` and establish its sha256."""
    checksums: dict[str, str] | None = None
    source = "computed from the download; the upstream publishes no checksums file"
    if tool["checksums"]:
        checksums_name = render(tool["checksums"], new_version)
        checksums = parse_checksums(_read(fetcher, asset_url(tool["repository"], new_version, checksums_name)).decode("utf-8", "replace"))
        source = f"upstream checksums file `{checksums_name}`"
    platforms: dict[str, dict[str, str]] = {}
    for key, entry in tool["platforms"].items():
        asset = render(entry["asset"], new_version)
        digest = hashlib.sha256(_read(fetcher, asset_url(tool["repository"], new_version, asset))).hexdigest()
        if checksums is not None:
            expected = checksums.get(asset)
            if expected is None:
                raise ChecksumMismatch(f"{name} {new_version}: the upstream checksums file lists no entry for {asset}")
            if expected != digest:
                raise ChecksumMismatch(f"{name} {new_version}: {asset} downloaded with sha256 {digest}, the upstream checksums file says {expected}")
        platforms[key] = {"asset": entry["asset"], "binary": entry["binary"], "sha256": digest}
    return Proposal(name, tool["version"], new_version, source, platforms)


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pip_resolve(requirements: list[str], cutoff: datetime) -> dict[str, str]:
    """The packages, by normalized name, that pip in a fresh virtual environment of this interpreter would install."""
    with tempfile.TemporaryDirectory() as temporary:
        environment = Path(temporary) / "venv"
        try:
            subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True, capture_output=True, text=True)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            command = [str(python), "-m", "pip", "install", "--dry-run", "--ignore-installed", "--quiet", "--disable-pip-version-check",
                       "--report", str(Path(temporary) / "report.json"), *requirements]
            help_text = subprocess.run([str(python), "-m", "pip", "install", "--help"], check=True, capture_output=True, text=True).stdout
            if "--uploaded-prior-to" in help_text:
                command += ["--uploaded-prior-to", cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")]
            subprocess.run(command, check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as error:
            detail = error.stderr.strip() if isinstance(error, subprocess.CalledProcessError) and error.stderr else str(error)
            raise Unreachable(f"pip could not resolve {' '.join(requirements)}: {detail}") from error
        report = json.loads((Path(temporary) / "report.json").read_text(encoding="utf-8"))
    return {normalized(item["metadata"]["name"]): item["metadata"]["version"] for item in report["install"]}


def locked_distribution(filename: str) -> bool:
    """An sdist, or a wheel that installs on linux-x64 or windows-x64 from CPython 3.12 on; a compressed platform tag set
    needs one such platform."""
    if filename.endswith((".tar.gz", ".zip")):
        return True
    wheel = WHEEL.match(filename)
    if wheel is None or not any(LOCKED_PLATFORMS.match(platform) for platform in wheel["platform"].split(".")):
        return False
    for tag in wheel["python"].split("."):
        cpython = re.fullmatch(r"cp3([0-9]+)", tag)
        if tag in ("py3", "py2.py3") or (cpython and ((3, int(cpython[1])) >= OLDEST_PYTHON or wheel["abi"] == "abi3")):
            return True
    return False


def eligible_pypi_release(project: object, pinned: str, now: datetime, cooldown_days: int = COOLDOWN_DAYS) -> str | None:
    """The newest stable PyPI release that is newer than ``pinned``, at least the cooldown old and not yanked."""
    if not isinstance(project, dict) or not isinstance(project.get("releases"), dict):
        raise Unreachable("the PyPI project has no release list")
    cutoff = now - timedelta(days=cooldown_days)
    candidates: list[str] = []
    for version, files in project["releases"].items():
        if not RELEASE_TAG.match(version) or version.startswith("v") or not isinstance(files, list) or not files:
            continue
        uploaded = [_parse_time(item.get("upload_time_iso_8601")) for item in files if isinstance(item, dict)]
        if any(item.get("yanked") for item in files if isinstance(item, dict)) or None in uploaded or max(uploaded) > cutoff:
            continue
        if version_key(version) > version_key(pinned):
            candidates.append(version)
    return max(candidates, key=version_key) if candidates else None


def build_requirements(archive: bytes) -> list[str]:
    """``build-system.requires`` of the sdist's top-level pyproject.toml."""
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as bundle:
            member = next(info for info in bundle.getmembers() if info.name.count("/") == 1 and info.name.endswith("/pyproject.toml"))
            extracted = bundle.extractfile(member)
            if extracted is None:
                raise KeyError(member.name)
            requires = tomllib.loads(extracted.read().decode("utf-8"))["build-system"]["requires"]
    except (tarfile.TarError, StopIteration, KeyError, tomllib.TOMLDecodeError, UnicodeDecodeError) as error:
        raise ChecksumMismatch(f"the sdist states no build requirements: {error!r}") from error
    return [str(item) for item in requires]


def refresh_python_tool(name: str, tool: dict, new_version: str, fetcher: Fetcher, resolver: Resolver, now: datetime, cooldown_days: int) -> Proposal:
    """Resolve the closure at ``new_version`` and lock each package with its PyPI sha256 values."""
    cutoff = now - timedelta(days=cooldown_days)
    where = f"{name} {new_version}"
    resolved = resolver([f"{tool['requirement']}=={new_version}"], cutoff)
    if resolved.get(name) != new_version:
        raise ChecksumMismatch(f"{where}: pip resolved {name} {resolved.get(name)}")
    files = json.loads(_read(fetcher, PYPI_VERSION_URL.format(name=name, version=new_version)))["urls"]
    archive = next((item for item in files if item["filename"].endswith(".tar.gz")), None)
    if archive is None:
        raise ChecksumMismatch(f"{where}: PyPI holds no sdist")
    content = _read(fetcher, archive["url"])
    if hashlib.sha256(content).hexdigest() != archive["digests"]["sha256"]:
        raise ChecksumMismatch(f"{where}: {archive['filename']} differs from its PyPI sha256")
    build = resolver(build_requirements(content), cutoff)
    packages: dict[str, dict] = {}
    for package, version in sorted({**resolved, **build}.items()):
        listing = json.loads(_read(fetcher, PYPI_VERSION_URL.format(name=package, version=version)))["urls"]
        if any((_parse_time(item.get("upload_time_iso_8601")) or datetime.max.replace(tzinfo=timezone.utc)) > cutoff for item in listing):
            raise Unreachable(f"{package} {version} is younger than {cooldown_days} days")
        digests = sorted(item["digests"]["sha256"] for item in listing if locked_distribution(item["filename"]))
        if not digests:
            raise ChecksumMismatch(f"{where}: {package} {version} has no distribution for the supported platforms")
        packages[package] = {"version": version, "sha256": digests}
    return Proposal(name, tool["version"], new_version, "PyPI sha256 of every supported distribution of the closure that pip resolved",
                    packages=packages, build=sorted(build))


def plan(pins: dict, fetcher: Fetcher = fetch, now: datetime | None = None, cooldown_days: int = COOLDOWN_DAYS, resolver: Resolver = pip_resolve) -> Plan:
    """Decide what the pins file would become; nothing is written."""
    now = now or datetime.now(timezone.utc)
    result = Plan(pins=json.loads(json.dumps(pins)))
    for name, tool in pins["tools"].items():
        try:
            releases = json.loads(_read(fetcher, RELEASES_URL.format(repository=tool["repository"])))
            new_version = eligible_release(releases, tool["version"], now, cooldown_days)
            if new_version is None:
                result.current.append(f"{name} {tool['version']}")
                continue
            proposal = refresh_tool(name, tool, new_version, fetcher)
        except Unreachable as error:
            result.unreachable.append(f"{name}: {error}")
            continue
        except ChecksumMismatch as error:
            result.failures.append(str(error))
            continue
        except ValueError as error:
            result.unreachable.append(f"{name}: the release list could not be read: {error}")
            continue
        result.proposals.append(proposal)
        result.pins["tools"][name]["version"] = new_version
        result.pins["tools"][name]["platforms"] = proposal.platforms
    for name, tool in pins.get("python", {}).items():
        try:
            new_version = eligible_pypi_release(json.loads(_read(fetcher, PYPI_URL.format(name=name))), tool["version"], now, cooldown_days)
            if new_version is None:
                result.current.append(f"{name} {tool['version']}")
                continue
            proposal = refresh_python_tool(name, tool, new_version, fetcher, resolver, now, cooldown_days)
        except Unreachable as error:
            result.unreachable.append(f"{name}: {error}")
            continue
        except ChecksumMismatch as error:
            result.failures.append(str(error))
            continue
        except (ValueError, KeyError, TypeError) as error:
            result.unreachable.append(f"{name}: the PyPI data could not be read: {error!r}")
            continue
        result.proposals.append(proposal)
        result.pins["python"][name].update(version=new_version, build=proposal.build, packages=proposal.packages)
    pinned_node = pins["node"]["version"]
    try:
        new_node = eligible_node(json.loads(_read(fetcher, NODE_INDEX_URL)), pinned_node, now, cooldown_days)
    except Unreachable as error:
        result.unreachable.append(f"node: {error}")
    except ValueError as error:
        result.unreachable.append(f"node: the release index could not be read: {error}")
    else:
        if new_node is None:
            result.current.append(f"node {pinned_node}")
        else:
            result.proposals.append(Proposal("node", pinned_node, new_node, "not downloaded here; actions/setup-node verifies the Node.js download it installs"))
            result.pins["node"]["version"] = new_node
    return result


def render_summary(result: Plan, cooldown_days: int = COOLDOWN_DAYS) -> str:
    lines = ["## CI tool pins", ""]
    if result.proposals:
        lines += ["| Pin | Current | Proposed | sha256 source |", "| --- | --- | --- | --- |"]
        lines += [f"| {proposal.tool} | {proposal.old} | {proposal.new} | {proposal.sha256_source} |" for proposal in result.proposals]
    else:
        lines.append(f"No pin has a newer release that is at least {cooldown_days} days old.")
    if result.current:
        lines += ["", "Current: " + ", ".join(result.current) + "."]
    if result.unreachable:
        lines += ["", "Unreachable upstreams, pins left unchanged:", ""] + [f"- {item}" for item in result.unreachable]
    if result.failures:
        lines += ["", "Checksum failures, nothing proposed:", ""] + [f"- {item}" for item in result.failures]
    return "\n".join(lines) + "\n"


def render_pr_body(result: Plan, pins_path: str) -> str:
    lines = [f"Updates the exact tool pins in `{pins_path}`; every platform entry of each changed tool is refreshed.", ""]
    lines += ["| Pin | Old | New | sha256 source |", "| --- | --- | --- | --- |"]
    lines += [f"| {proposal.tool} | {proposal.old} | {proposal.new} | {proposal.sha256_source} |" for proposal in result.proposals]
    if result.unreachable:
        lines += ["", "Left unchanged because the upstream was unreachable in this run:", ""] + [f"- {item}" for item in result.unreachable]
    lines += ["", "Merging this pull request as the repository owner moves `v1` to the merged commit when the shared surface otherwise matches the current `v1`; see `docs/maintaining.md`."]
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    """The command line of this script; the part's tests parse the workflow's invocations with it."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pins", type=Path, default=PINS_PATH)
    parser.add_argument("--apply", action="store_true", help="write the proposed pins file")
    parser.add_argument("--cooldown-days", type=int, default=COOLDOWN_DAYS, help="minimum release age in days (default: %(default)s)")
    parser.add_argument("--output", type=Path, help="append changed= and failed= lines for GitHub Actions")
    parser.add_argument("--summary", type=Path, help="append the Markdown summary to this file")
    parser.add_argument("--pr-body", type=Path, help="write the pull request body to this file")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.cooldown_days < 0:
        parser.error("--cooldown-days must be 0 or more")
    try:
        pins = load_pins(arguments.pins)
    except PinsError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2
    result = plan(pins, cooldown_days=arguments.cooldown_days)
    summary = render_summary(result, arguments.cooldown_days)
    print(summary)
    if arguments.summary:
        with arguments.summary.open("a", encoding="utf-8") as handle:
            handle.write(summary)
    if arguments.output:
        with arguments.output.open("a", encoding="utf-8") as handle:
            handle.write(f"changed={str(result.changed and not result.failed).lower()}\nfailed={str(result.failed).lower()}\n")
    if result.failed:
        for failure in result.failures:
            print(f"::error::{failure}", file=sys.stderr)
        return 1
    if arguments.apply and result.changed:
        arguments.pins.write_text(dump_pins(result.pins), encoding="utf-8", newline="\n")
        print(f"wrote {arguments.pins}")
    if arguments.pr_body and result.changed:
        arguments.pr_body.write_text(render_pr_body(result, Path(os.path.relpath(arguments.pins.resolve(), Path.cwd())).as_posix()), encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
