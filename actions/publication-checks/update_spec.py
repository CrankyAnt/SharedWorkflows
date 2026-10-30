"""Refresh the vendored Conventional Branch specification after review."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable


LATEST_URL = "https://conventionalbranch.org/spec.json"
VERSIONED_URL = "https://conventionalbranch.org/v{version}/spec.json"
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*))*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$")
FIXTURE_BRANCHES = (
    "main", "master", "develop", "feature/add-check", "feat/add-check",
    "bugfix/repair", "fix/repair", "hotfix/repair", "release/v1.2.3",
    "chore/docs", "ai/add-check", "copilot/add-check", "cursor/add-check",
    "claude/add-check", "codex/add-check", "research/issue-1-loader-shape",
    "feat/has.dot", "release/v1.2.3-rc.1", "feature/", "feat/has--gap",
    "trunk/main", "Main",
)


class CandidateError(ValueError):
    """The upstream document cannot safely replace the vendored document."""


def _paths(root: Path) -> tuple[Path, Path, Path]:
    base = root / "actions" / "publication-checks"
    return base / "spec.json", base / "spec-metadata.json", root / "tests" / "fixtures" / "publication_checks" / "specification-cases.json"


def _load_json(raw: bytes, label: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CandidateError(f"{label} is not valid JSON") from error
    if not isinstance(value, dict):
        raise CandidateError(f"{label} must contain a JSON object")
    return value


def _validate_candidate(raw: bytes) -> tuple[dict[str, object], str, str]:
    candidate = _load_json(raw, "candidate specification")
    version = candidate.get("version")
    grammar = candidate.get("grammar")
    if candidate.get("spec") != "Conventional Branch":
        raise CandidateError("candidate specification has an unknown identity")
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        raise CandidateError("candidate specification has an invalid semantic version")
    if not isinstance(grammar, dict) or not isinstance(grammar.get("regex"), str) or not grammar["regex"]:
        raise CandidateError("candidate specification has no regex")
    return candidate, version, grammar["regex"]


def _compile_and_match(regex: str, branches: tuple[str, ...] = FIXTURE_BRANCHES) -> list[bool]:
    source = "const [regex, branches] = JSON.parse(process.argv[1]); const expression = new RegExp(regex); process.stdout.write(JSON.stringify(branches.map((branch) => expression.test(branch))));"
    try:
        result = subprocess.run(["node", "-e", source, json.dumps([regex, list(branches)])], capture_output=True, text=True, check=False)
    except OSError as error:
        raise CandidateError("Node.js is unavailable for regex validation") from error
    if result.returncode:
        raise CandidateError(f"candidate regex does not compile in Node.js: {result.stderr.strip()}")
    try:
        matches = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise CandidateError("Node.js returned invalid regex results") from error
    if not isinstance(matches, list) or not all(isinstance(match, bool) for match in matches):
        raise CandidateError("Node.js returned invalid regex results")
    return matches


def _version_key(version: str) -> tuple[int, int, int, int, tuple[tuple[int, object], ...]]:
    core, separator, suffix = version.partition("+")[0].partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    prerelease = tuple((0, int(part)) if part.isdigit() else (1, part) for part in suffix.split(".")) if separator else ()
    return major, minor, patch, 1 if not separator else 0, prerelease


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read()


def update(root: Path, fetcher: Callable[[str], bytes] = _fetch, candidate_path: Path | None = None) -> dict[str, object]:
    specification_path, metadata_path, fixture_path = _paths(root)
    try:
        current = _load_json(specification_path.read_bytes(), "vendored specification")
        current_version = current.get("version")
        if not isinstance(current_version, str) or not SEMVER.fullmatch(current_version):
            raise CandidateError("vendored specification has an invalid semantic version")
        raw = candidate_path.read_bytes() if candidate_path else fetcher(LATEST_URL)
        candidate, version, regex = _validate_candidate(raw)
        matches = _compile_and_match(regex)
        if candidate_path is None:
            versioned = fetcher(VERSIONED_URL.format(version=version))
            if versioned != raw:
                raise CandidateError("latest specification differs from its versioned document")
    except (CandidateError, OSError, urllib.error.URLError, TimeoutError) as error:
        return {"changed": False, "message": f"Specification refresh skipped: {error}"}

    candidate_key = _version_key(version)
    current_key = _version_key(current_version)
    if candidate_key < current_key:
        return {"changed": False, "message": f"Specification refresh skipped: upstream {version} is older than vendored {current_version}."}
    if candidate_key == current_key:
        if raw != specification_path.read_bytes():
            return {"changed": False, "message": f"Specification refresh needs review: upstream {version} is not newer and has changed content."}
        return {"changed": False, "message": f"Specification refresh skipped: Conventional Branch {version} is current."}

    metadata = {"version": version, "sha256": hashlib.sha256(raw).hexdigest(), "source": VERSIONED_URL.format(version=version)}
    fixture = {"version": version, "official_regex": regex, "cases": [{"branch": branch, "matches": match} for branch, match in zip(FIXTURE_BRANCHES, matches, strict=True)]}
    metadata_json = json.dumps(metadata, indent=2) + "\n"
    fixture_json = json.dumps(fixture, indent=2) + "\n"
    specification_path.write_bytes(raw)
    metadata_path.write_text(metadata_json, encoding="utf-8")
    fixture_path.write_text(fixture_json, encoding="utf-8")
    return {"changed": True, "version": version, "sha256": metadata["sha256"], "message": f"Updated Conventional Branch specification to {version}."}


def build_parser() -> argparse.ArgumentParser:
    """The command line of this script; the part's tests parse the workflow's invocations with it."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--output", type=Path, help="Write GitHub Actions outputs to this file and the result message to stdout.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    result = update(arguments.root.resolve(), candidate_path=arguments.candidate)
    rendered = json.dumps(result, sort_keys=True)
    if arguments.output:
        lines = [f"changed={str(result['changed']).lower()}"]
        for key in ("version", "sha256"):
            if key in result:
                lines.append(f"{key}={result[key]}")
        with arguments.output.open("a", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")
        print(result["message"])
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
