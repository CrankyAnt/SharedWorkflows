"""Load and validate the CI tool pins file beside this module."""

from __future__ import annotations

import json
import re
from pathlib import Path

PINS_PATH = Path(__file__).resolve().parent / "pins.json"
SCHEMA_PATH = Path(__file__).resolve().parent / "pins.schema.json"
DOWNLOAD_URL = "{base}/{repository}/releases/download/v{version}/{asset}"
DEFAULT_RELEASE_BASE_URL = "https://github.com"
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
TOOL_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
PLATFORM_KEY = re.compile(r"^[a-z0-9]+-[a-z0-9]+$")
PACKAGE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
PACKAGE_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc|\.post|\.dev)[0-9]+)*$")
REQUIREMENT = re.compile(r"^(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)(?:\[[a-z0-9-]+(?:,[a-z0-9-]+)*\])?$")


class PinsError(ValueError):
    """The pins file does not satisfy pins.schema.json."""


def render(template: str, version: str) -> str:
    """Replace ``{version}`` in an asset, binary or checksums name."""
    return template.replace("{version}", version)


def asset_url(repository: str, version: str, asset: str, base: str = DEFAULT_RELEASE_BASE_URL) -> str:
    return DOWNLOAD_URL.format(base=base.rstrip("/"), repository=repository, version=version, asset=asset)


def version_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise PinsError(message)


def _require_string(mapping: dict, key: str, where: str, pattern: re.Pattern[str] | None = None) -> str:
    value = mapping.get(key)
    _require(isinstance(value, str) and value != "", f"{where} lacks a {key}")
    if pattern is not None:
        _require(pattern.fullmatch(value) is not None, f"{where} has an invalid {key}: {value!r}")
    return value


def validate_pins(pins: object) -> dict:
    """Return ``pins`` when it satisfies the schema; raise ``PinsError`` naming the first violation."""
    _require(isinstance(pins, dict), "the pins file must contain a JSON object")
    _require(set(pins) <= {"$schema", "node", "tools", "python"}, f"unknown top-level keys: {sorted(set(pins) - {'$schema', 'node', 'tools', 'python'})}")
    node = pins.get("node")
    _require(isinstance(node, dict), "the pins file lacks a node object")
    _require(set(node) == {"version"}, "node holds only a version")
    _require_string(node, "version", "node", VERSION)
    tools = pins.get("tools")
    _require(isinstance(tools, dict) and tools, "the pins file lacks a tools object")
    for name, tool in tools.items():
        _require(TOOL_NAME.fullmatch(name) is not None, f"invalid tool name {name!r}")
        _require(isinstance(tool, dict), f"tool {name} must be an object")
        _require(set(tool) == {"repository", "version", "checksums", "platforms"}, f"tool {name} must hold exactly repository, version, checksums and platforms")
        _require_string(tool, "repository", f"tool {name}", REPOSITORY)
        _require_string(tool, "version", f"tool {name}", VERSION)
        checksums = tool["checksums"]
        _require(checksums is None or (isinstance(checksums, str) and checksums != ""), f"tool {name} has an invalid checksums name")
        platforms = tool["platforms"]
        _require(isinstance(platforms, dict) and platforms, f"tool {name} lacks platform entries")
        for key, platform in platforms.items():
            _require(PLATFORM_KEY.fullmatch(key) is not None, f"tool {name} has an invalid platform key {key!r}")
            where = f"tool {name} platform {key}"
            _require(isinstance(platform, dict), f"{where} must be an object")
            _require(set(platform) == {"asset", "binary", "sha256"}, f"{where} must hold exactly asset, binary and sha256")
            _require_string(platform, "asset", where)
            _require_string(platform, "binary", where)
            _require_string(platform, "sha256", where, SHA256)
    python = pins.get("python", {})
    _require(isinstance(python, dict), "python must be an object")
    for name, tool in python.items():
        _validate_python_tool(name, tool)
    return pins


def _validate_python_tool(name: str, tool: object) -> None:
    """A Python tool locks itself, its build packages and every package it runs on, each with exact sha256 values."""
    where = f"python tool {name}"
    _require(PACKAGE_NAME.fullmatch(name) is not None, f"invalid package name {name!r}")
    _require(isinstance(tool, dict) and set(tool) == {"requirement", "version", "build", "packages"}, f"{where} must hold exactly requirement, version, build and packages")
    requirement = REQUIREMENT.fullmatch(str(tool["requirement"]))
    _require(requirement is not None and requirement.group("name") == name, f"{where}: its requirement must name {name}")
    _require_string(tool, "version", where, VERSION)
    packages = tool["packages"]
    _require(isinstance(packages, dict) and packages, f"{where} lacks packages")
    for package_name, package in packages.items():
        _require(PACKAGE_NAME.fullmatch(package_name) is not None, f"{where} has an invalid package name {package_name!r}")
        _require(isinstance(package, dict) and set(package) == {"version", "sha256"}, f"{where} package {package_name} must hold exactly version and sha256")
        _require_string(package, "version", f"{where} package {package_name}", PACKAGE_VERSION)
        digests = package["sha256"]
        _require(isinstance(digests, list) and digests and all(isinstance(digest, str) and SHA256.fullmatch(digest) for digest in digests),
                 f"{where} package {package_name} lacks a sha256 list")
    _require(name in packages, f"{where} does not lock {name} itself")
    _require(packages[name]["version"] == tool["version"], f"{where} locks {name} at {packages[name]['version']}, not at {tool['version']}")
    build = tool["build"]
    _require(isinstance(build, list) and all(isinstance(item, str) for item in build), f"{where} has an invalid build list")
    for item in build:
        _require(item in packages, f"{where}: build package {item} is not locked")


def load_pins(path: Path = PINS_PATH) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PinsError(f"{path} could not be read as JSON: {error}") from error
    return validate_pins(raw)


def dump_pins(pins: dict) -> str:
    return json.dumps(pins, indent=2) + "\n"
