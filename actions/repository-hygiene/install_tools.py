"""Download the pinned tools for one platform, verify each asset's sha256 and place the executables in one directory.

    python3 install_tools.py --pins pins.json [--platform linux-x64] --destination DIR [--tool NAME ...]

Without ``--platform`` the platform is that of the machine: its system and its processor, such as ``windows-x64``.
A sha256 mismatch stops the run before anything is extracted, so a tool that does not match its pin never runs.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pins import DEFAULT_RELEASE_BASE_URL, PinsError, asset_url, load_pins, render  # noqa: E402


class InstallError(RuntimeError):
    """A tool could not be installed from its pinned asset."""


SYSTEMS = {"Linux": "linux", "Windows": "windows", "Darwin": "macos"}
PROCESSORS = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}


def machine_platform(system: str | None = None, processor: str | None = None) -> str:
    """The platform key of this machine in the pins file: its system and its processor, such as ``linux-x64``. A system
    or a processor that the pins file has no name for keeps its own name, so the error for a missing entry names it."""
    system, processor = system or platform.system(), processor or platform.machine()
    return f"{SYSTEMS.get(system, system.lower())}-{PROCESSORS.get(processor.lower(), processor.lower())}"


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310 - the URL comes from the pins file
        return response.read()


def extract_binary(archive: bytes, asset: str, member: str) -> bytes:
    """Return the bytes of ``member`` inside the tar or zip ``archive``."""
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            try:
                return bundle.read(member)
            except KeyError as error:
                raise InstallError(f"{asset} holds no member {member}") from error
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as bundle:
            try:
                info = bundle.getmember(member)
            except KeyError as error:
                raise InstallError(f"{asset} holds no member {member}") from error
            extracted = bundle.extractfile(info)
            if extracted is None:
                raise InstallError(f"{asset} member {member} is not a file")
            return extracted.read()
    except tarfile.TarError as error:
        raise InstallError(f"{asset} is not a readable archive: {error}") from error


def tool_pin_digest(name: str, pins: dict, platform: str) -> str:
    """Stable sha256 digest of one tool's pin entry for ``platform`` (and Python minor version for Python tools)."""
    python_tools = pins.get("python", {})
    if name in python_tools:
        payload = {"python": f"{sys.version_info.major}.{sys.version_info.minor}", "tool": python_tools[name]}
    else:
        tool = pins["tools"].get(name)
        if tool is None:
            raise InstallError(f"the pins file holds no tool {name}")
        entry = tool["platforms"].get(platform)
        if entry is None:
            raise InstallError(f"the pins file holds no {platform} entry for {name} {tool['version']}")
        payload = {"version": tool["version"], "platform": platform, "entry": entry}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def cache_key(pins: dict, platform: str, tools: list[str] | None = None) -> str:
    """Deterministic cache key for the selected tools on ``platform``, keyed by their exact pin entries."""
    python_tools = pins.get("python", {})
    selected = sorted(set(tools)) if tools else sorted(pins["tools"]) + sorted(python_tools)
    digests = {name: tool_pin_digest(name, pins, platform) for name in selected}
    combined = hashlib.sha256(json.dumps(digests, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return f"tools-{platform}-{combined[:16]}"


def _pin_marker(destination: Path, name: str) -> Path:
    return destination / f".pin-{name}.sha256"


def _marker_matches(destination: Path, name: str, digest: str) -> bool:
    marker = _pin_marker(destination, name)
    try:
        return marker.is_file() and marker.read_text(encoding="utf-8").strip() == digest
    except OSError:
        return False


def install_python_tool(name: str, tool: dict, destination: Path, find_links: Path | None = None, pin_digest: str | None = None) -> Path:
    """Install the tool's hash-locked packages into ``destination/<name>-venv`` with this interpreter, build packages
    first, and copy its console script into ``destination``. pip refuses any file whose sha256 the lock lacks."""
    environment = destination / f"{name}-venv"
    if pin_digest is not None and environment.is_dir() and _marker_matches(destination, name, pin_digest):
        existing = next((candidate for candidate in (destination / f"{name}.exe", destination / name) if candidate.is_file()), None)
        if existing is not None:
            print(f"cached {name} {tool['version']} -> {existing}")
            return existing
    if environment.exists():
        shutil.rmtree(environment)
    scripts = environment / ("Scripts" if os.name == "nt" else "bin")
    python = scripts / ("python.exe" if os.name == "nt" else "python")
    index = ["--no-index", "--find-links", str(find_links)] if find_links else []
    pip = [str(python), "-m", "pip", "install", "--require-hashes", "--no-deps", "--disable-pip-version-check", "--quiet", *index]
    build = tool["build"]
    groups = [(names, options) for names, options in (([item for item in build], []), ([item for item in sorted(tool["packages"]) if item not in build], ["--no-build-isolation"])) if names]
    try:
        subprocess.run([sys.executable, "-m", "venv", str(environment)], check=True, capture_output=True, text=True)
        for number, (names, options) in enumerate(groups):
            requirements = environment / f"locked-{number}.txt"
            requirements.write_text("".join(f"{item}=={tool['packages'][item]['version']} " + " ".join(f"--hash=sha256:{digest}" for digest in tool["packages"][item]["sha256"]) + "\n"
                                            for item in names), encoding="utf-8")
            subprocess.run([*pip, *options, "-r", str(requirements)], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        shutil.rmtree(environment, ignore_errors=True)
        raise InstallError(f"{name} {tool['version']}: pip refused the locked packages; nothing was installed: {(error.stderr or error.stdout).strip()}") from error
    script = next((candidate for candidate in (scripts / f"{name}.exe", scripts / name) if candidate.is_file()), None)
    if script is None:
        raise InstallError(f"{name} {tool['version']} installed no console script {name}")
    target = destination / script.name
    shutil.copy2(script, target)
    if pin_digest is not None:
        _pin_marker(destination, name).write_text(pin_digest + "\n", encoding="utf-8")
    print(f"installed {name} {tool['version']} with {len(tool['packages'])} locked packages -> {target}")
    return target


def install(pins: dict, platform: str, destination: Path, tools: list[str] | None = None, base_url: str = DEFAULT_RELEASE_BASE_URL, fetch=download, python_find_links: Path | None = None) -> list[Path]:
    """Install the selected tools and return the executable paths; verify every sha256 before extracting anything.
    A Python tool installs after the release tools, from its hash lock."""
    python_tools = pins.get("python", {})
    selected = tools or sorted(pins["tools"]) + sorted(python_tools)
    staged: list[tuple[str, dict, bytes | None, str]] = []
    for name in selected:
        if name in python_tools:
            continue
        pin_digest = tool_pin_digest(name, pins, platform)
        tool = pins["tools"][name]
        entry = tool["platforms"][platform]
        member = render(entry["binary"], tool["version"])
        target = destination / (name + ".exe" if member.endswith(".exe") else name)
        if target.is_file() and _marker_matches(destination, name, pin_digest):
            staged.append((name, tool, None, pin_digest))
            continue
        asset = render(entry["asset"], tool["version"])
        url = asset_url(tool["repository"], tool["version"], asset, base_url)
        try:
            archive = fetch(url)
        except OSError as error:
            raise InstallError(f"{name} {tool['version']}: {asset} could not be downloaded from {url}: {error}") from error
        digest = hashlib.sha256(archive).hexdigest()
        if digest != entry["sha256"]:
            raise InstallError(f"{name} {tool['version']}: {asset} has sha256 {digest}, the pins file expects {entry['sha256']}; nothing was installed")
        print(f"verified {name} {tool['version']} {asset} sha256 {digest}")
        staged.append((name, tool, archive, pin_digest))
    destination.mkdir(parents=True, exist_ok=True)
    installed: list[Path] = []
    for name, tool, archive, pin_digest in staged:
        entry = tool["platforms"][platform]
        member = render(entry["binary"], tool["version"])
        target = destination / (name + ".exe" if member.endswith(".exe") else name)
        if archive is None:
            print(f"cached {name} {tool['version']} -> {target}")
            installed.append(target)
            continue
        asset = render(entry["asset"], tool["version"])
        target.write_bytes(extract_binary(archive, asset, member))
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _pin_marker(destination, name).write_text(pin_digest + "\n", encoding="utf-8")
        print(f"installed {name} {tool['version']} -> {target}")
        installed.append(target)
    for name in selected:
        if name in python_tools:
            pin_digest = tool_pin_digest(name, pins, platform)
            installed.append(install_python_tool(name, python_tools[name], destination, python_find_links, pin_digest))
    return installed


def build_parser() -> argparse.ArgumentParser:
    """The command line of this script; the part's tests parse the workflow's invocations with it."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pins", type=Path, required=True)
    parser.add_argument("--platform", default=None, help="a platform key of the pins file, for example linux-x64 (default: this machine's)")
    parser.add_argument("--destination", type=Path, default=None)
    parser.add_argument("--tool", action="append", dest="tools", help="install only this tool; repeatable")
    parser.add_argument("--print-cache-key", action="store_true", help="print the cache key for the selected tools and exit without installing")
    parser.add_argument("--github-output", type=Path, default=None, help="append key=<cache key> to this file when --print-cache-key is set")
    parser.add_argument("--release-base-url", default=os.environ.get("HYGIENE_RELEASE_BASE_URL", DEFAULT_RELEASE_BASE_URL), help=argparse.SUPPRESS)
    parser.add_argument("--python-find-links", type=Path, default=os.environ.get("HYGIENE_PYTHON_FIND_LINKS") or None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if not arguments.print_cache_key and arguments.destination is None:
        parser.error("--destination is required unless --print-cache-key is set")
    try:
        pins = load_pins(arguments.pins)
        target_platform = arguments.platform or machine_platform()
        if arguments.print_cache_key:
            key = cache_key(pins, target_platform, arguments.tools)
            print(key)
            if arguments.github_output is not None:
                with arguments.github_output.open("a", encoding="utf-8") as handle:
                    handle.write(f"key={key}\n")
            return 0
        install(pins, target_platform, arguments.destination, arguments.tools, arguments.release_base_url,
                python_find_links=arguments.python_find_links)
    except (PinsError, InstallError) as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
