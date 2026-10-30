from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

RESOURCES = REPOSITORY_ROOT / "actions" / "repository-hygiene"

sys.path.insert(0, str(RESOURCES))
import install_tools  # noqa: E402


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.releases = self.root / "releases"
        self.installer = install_tools

    def _release(self, repository: str, version: str, asset: str, members: dict[str, bytes]) -> str:
        directory = self.releases / repository / "releases" / "download" / f"v{version}"
        directory.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        if asset.endswith(".zip"):
            with zipfile.ZipFile(buffer, "w") as bundle:
                for name, content in members.items():
                    bundle.writestr(name, content)
        else:
            with tarfile.open(fileobj=buffer, mode="w:xz" if asset.endswith(".xz") else "w:gz") as bundle:
                for name, content in members.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    info.mode = 0o755
                    bundle.addfile(info, io.BytesIO(content))
        (directory / asset).write_bytes(buffer.getvalue())
        return hashlib.sha256(buffer.getvalue()).hexdigest()

    def _pins(self, sha_a: str, sha_b: str) -> dict:
        return {
            "node": {"version": "24.22.0"},
            "tools": {
                "alpha": {"repository": "fixture/alpha", "version": "1.2.3", "checksums": "alpha_{version}_checksums.txt",
                          "platforms": {"test-x64": {"asset": "alpha_{version}_test.tar.gz", "binary": "alpha", "sha256": sha_a}}},
                "beta": {"repository": "fixture/beta", "version": "2.0.0", "checksums": None,
                         "platforms": {"test-x64": {"asset": "beta-v{version}.tar.xz", "binary": "beta-v{version}/beta", "sha256": sha_b}}},
            },
        }

    def test_matching_checksums_install_every_tool_as_an_executable(self) -> None:
        sha_a = self._release("fixture/alpha", "1.2.3", "alpha_1.2.3_test.tar.gz", {"alpha": b"#!/bin/sh\necho alpha\n", "README": b"x"})
        sha_b = self._release("fixture/beta", "2.0.0", "beta-v2.0.0.tar.xz", {"beta-v2.0.0/beta": b"#!/bin/sh\necho beta\n"})
        destination = self.root / "tools"
        installed = self.installer.install(self._pins(sha_a, sha_b), "test-x64", destination, base_url=self.releases.as_uri())
        self.assertEqual([destination / "alpha", destination / "beta"], installed)
        self.assertEqual(b"#!/bin/sh\necho beta\n", (destination / "beta").read_bytes())
        if os.name != "nt":
            self.assertTrue(os.access(destination / "beta", os.X_OK))

    def test_a_checksum_mismatch_installs_nothing(self) -> None:
        sha_a = self._release("fixture/alpha", "1.2.3", "alpha_1.2.3_test.tar.gz", {"alpha": b"alpha"})
        self._release("fixture/beta", "2.0.0", "beta-v2.0.0.tar.xz", {"beta-v2.0.0/beta": b"beta"})
        destination = self.root / "tools"
        pins_path = self.root / "pins.json"
        pins_path.write_text(json.dumps(self._pins(sha_a, "0" * 64)), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(RESOURCES / "install_tools.py"), "--pins", str(pins_path), "--platform", "test-x64", "--destination", str(destination), "--release-base-url", self.releases.as_uri()],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("beta 2.0.0: beta-v2.0.0.tar.xz has sha256", result.stderr)
        self.assertIn("nothing was installed", result.stderr)
        self.assertFalse(destination.exists())

    def test_a_missing_platform_entry_or_tool_is_an_error(self) -> None:
        sha_a = self._release("fixture/alpha", "1.2.3", "alpha_1.2.3_test.tar.gz", {"alpha": b"alpha"})
        pins = self._pins(sha_a, "0" * 64)
        with self.assertRaisesRegex(self.installer.InstallError, "no other-x64 entry for alpha"):
            self.installer.install(pins, "other-x64", self.root / "tools", ["alpha"], base_url=self.releases.as_uri())
        with self.assertRaisesRegex(self.installer.InstallError, "no tool gamma"):
            self.installer.install(pins, "test-x64", self.root / "tools", ["gamma"], base_url=self.releases.as_uri())

    def test_only_selected_tools_are_installed(self) -> None:
        sha_a = self._release("fixture/alpha", "1.2.3", "alpha_1.2.3_test.tar.gz", {"alpha": b"alpha"})
        destination = self.root / "tools"
        installed = self.installer.install(self._pins(sha_a, "0" * 64), "test-x64", destination, ["alpha"], base_url=self.releases.as_uri())
        self.assertEqual([destination / "alpha"], installed)


def wheel(directory: Path, name: str, version: str, module: str, entry_point: bool) -> str:
    """A pure-Python wheel of ``module``, with a console script named ``name`` when ``entry_point`` is set."""
    info = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": module,
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    if entry_point:
        files[f"{info}/entry_points.txt"] = f"[console_scripts]\n{name} = {name}:main\n"
    files[f"{info}/RECORD"] = "".join(f"{path},,\n" for path in files) + f"{info}/RECORD,,\n"
    path = directory / f"{name}-{version}-py3-none-any.whl"
    directory.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as bundle:
        for member, content in files.items():
            bundle.writestr(member, content)
    return hashlib.sha256(path.read_bytes()).hexdigest()


class PythonToolInstallerTests(unittest.TestCase):
    """A Python tool installs from its hash lock into a virtual environment of the running interpreter."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.index = self.root / "index"
        self.tool_sha = wheel(self.index, "alpha", "1.0.0", "def main():\n    import backend\n    print('alpha ran with', backend.NAME)\n", True)
        self.backend_sha = wheel(self.index, "backend", "2.0.0", "NAME = 'backend'\n", False)

    def pins(self, tool_sha: str) -> dict:
        return {"node": {"version": "24.22.0"}, "tools": {}, "python": {"alpha": {
            "requirement": "alpha", "version": "1.0.0", "build": ["backend"],
            "packages": {"alpha": {"version": "1.0.0", "sha256": [tool_sha]}, "backend": {"version": "2.0.0", "sha256": ["0" * 64, self.backend_sha]}}}}}

    def test_a_locked_python_tool_runs_from_the_tools_directory(self) -> None:
        destination = self.root / "tools"
        installed = install_tools.install(self.pins(self.tool_sha), "test-x64", destination, ["alpha"], python_find_links=self.index)
        executable = destination / ("alpha.exe" if os.name == "nt" else "alpha")
        self.assertEqual([executable], installed)
        result = subprocess.run([str(executable)], capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("alpha ran with backend", result.stdout.strip())

    def test_a_sha256_outside_the_lock_installs_nothing(self) -> None:
        destination = self.root / "tools"
        with self.assertRaisesRegex(install_tools.InstallError, "alpha 1.0.0: pip refused the locked packages"):
            install_tools.install(self.pins("1" * 64), "test-x64", destination, ["alpha"], python_find_links=self.index)
        self.assertEqual([], [path.name for path in destination.iterdir()] if destination.exists() else [])


if __name__ == "__main__":
    unittest.main()
