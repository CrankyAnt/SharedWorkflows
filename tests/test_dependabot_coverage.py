"""The Dependabot coverage check of the shared hygiene workflow, over fixture trees and configurations."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RESOURCES = REPOSITORY_ROOT / "actions" / "repository-hygiene"
SCRIPT = RESOURCES / "dependabot_coverage.py"
GIT_IDENTITY = ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false"]

SPEC = importlib.util.spec_from_file_location("dependabot_coverage", SCRIPT)
assert SPEC and SPEC.loader
coverage = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = coverage  # dataclasses resolve annotations through sys.modules
SPEC.loader.exec_module(coverage)


def git(repo: Path, *arguments: str) -> None:
    subprocess.run(["git", *GIT_IDENTITY, "-C", str(repo), *arguments], capture_output=True, check=True)


def repository(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    git(root, "add", "-A")
    return root


def config(*entries: str) -> str:
    return "version: 2\nupdates:\n" + "".join(entries)


def entry(ecosystem: str, directory: str | None = None, directories: list[str] | None = None) -> str:
    text = f"  - package-ecosystem: {ecosystem}\n"
    if directory is not None:
        text += f"    directory: {directory}\n"
    if directories is not None:
        text += "    directories:\n" + "".join(f"      - {item}\n" for item in directories)
    return text + "    schedule:\n      interval: weekly\n"


class YamlSubsetTests(unittest.TestCase):
    def test_reads_the_shapes_dependabot_configurations_use(self) -> None:
        document = coverage.parse_yaml(
            "# leading comment\nversion: 2\nregistries:\n  npm-github:\n    type: npm-registry\n    url: https://npm.pkg.github.com\n"
            "updates:\n  - package-ecosystem: \"npm\"   # trailing comment\n    directory: /tools/x\n    schedule: {interval: weekly, day: 'monday'}\n"
            "    labels: [\"deps\", 'npm']\n    groups:\n      all:\n        patterns:\n          - \"*\"\n    ignore:\n      - dependency-name: lodash\n        versions: [\">=1\"]\n"
            "  - package-ecosystem: pip\n    directories:\n      - \"/a\"\n      - /b/*\n    cooldown:\n      default-days: 7\n    open-pull-requests-limit: 0\n"
            "  - package-ecosystem: nuget\n    directories: [\n      \"/src/**\",\n      \"/other\"\n    ]\n    allow:\n    - dependency-type: direct\n    enabled: true\n"
        )
        self.assertEqual(2, document["version"])
        self.assertEqual({"npm-github": {"type": "npm-registry", "url": "https://npm.pkg.github.com"}}, document["registries"])
        npm, pip, nuget = document["updates"]
        self.assertEqual({"interval": "weekly", "day": "monday"}, npm["schedule"])
        self.assertEqual(["deps", "npm"], npm["labels"])
        self.assertEqual({"all": {"patterns": ["*"]}}, npm["groups"])
        self.assertEqual([{"dependency-name": "lodash", "versions": [">=1"]}], npm["ignore"])
        self.assertEqual(["/a", "/b/*"], pip["directories"])
        self.assertEqual({"default-days": 7}, pip["cooldown"])
        self.assertEqual(0, pip["open-pull-requests-limit"])
        self.assertEqual(["/src/**", "/other"], nuget["directories"])
        self.assertEqual([{"dependency-type": "direct"}], nuget["allow"])
        self.assertIs(True, nuget["enabled"])
        self.assertIsNone(coverage.parse_yaml("# only a comment\n"))

    def test_reports_unsupported_constructs_instead_of_guessing(self) -> None:
        for text, message in [
            ("a: |\n  block\n", "block scalars"),
            ("a: &anchor b\nc: *anchor\n", "anchors"),
            ("a: [1, 2\n", "unterminated flow collection"),
            ("a:\n\t- b\n", "tabs"),
            ("? complex\n: key\n", "complex mapping keys"),
            ("a: 'open\n", "unterminated"),
        ]:
            with self.subTest(text=text):
                with self.assertRaisesRegex(coverage.YamlError, message):
                    coverage.parse_yaml(text)


class CoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ecosystems = coverage.load_ecosystems()

    def check(self, repo: Path, excludes: list[str] | None = None) -> list[str]:
        return [f"{item.path} {item.ecosystem}" for item in coverage.check(repo, self.ecosystems, excludes)]

    def test_an_uncovered_package_file_names_the_file_and_the_ecosystem(self) -> None:
        repo = repository(self.root / "uncovered", {
            "package.json": "{}\n",
            "service/requirements-dev.txt": "requests\n",
            "src/App/App.csproj": "<Project />\n",
            "go.mod": "module fixture\n",
            "crate/Cargo.toml": "[package]\n",
            "src/App/global.json": "{}\n",
            ".github/dependabot.yml": config(entry("npm", "/"), entry("github-actions", "/")),
        })
        self.assertEqual(["crate/Cargo.toml cargo", "go.mod gomod", "service/requirements-dev.txt pip", "src/App/App.csproj nuget",
                          "src/App/global.json dotnet-sdk"], self.check(repo))
        uncovered = coverage.check(repo, self.ecosystems)[0]
        self.assertEqual("/crate", uncovered.directory)
        self.assertIn("ecosystem cargo", uncovered.message())
        self.assertIn("directory /crate", uncovered.message())

    def test_a_directory_entry_or_a_matching_directories_pattern_covers_the_file(self) -> None:
        repo = repository(self.root / "covered", {
            "tools/checks/package.json": "{}\n",
            "services/api/pyproject.toml": "[project]\n",
            "services/web/requirements.txt": "flask\n",
            "src/Deep/Nested/Lib.fsproj": "<Project />\n",
            "Directory.Packages.props": "<Project />\n",
            "src/Deep/global.json": "{}\n",
            ".github/dependabot.yml": config(
                entry("dotnet-sdk", directory="/src/Deep"),
                entry("npm", directory="/tools/checks/"),
                entry("pip", directories=["/services/*"]),
                entry("nuget", directories=['"/src/**"', "/"]),
            ),
        })
        self.assertEqual([], self.check(repo))
        wrong_ecosystem = repository(self.root / "wrong", {"package.json": "{}\n", ".github/dependabot.yml": config(entry("pip", "/"))})
        self.assertEqual(["package.json npm"], self.check(wrong_ecosystem))
        wrong_directory = repository(self.root / "wrong-dir", {"a/package.json": "{}\n", ".github/dependabot.yml": config(entry("npm", "/"), entry("npm", directories=["/b/*"]))})
        self.assertEqual(["a/package.json npm"], self.check(wrong_directory))

    def test_without_configuration_the_check_passes_only_when_no_package_file_exists(self) -> None:
        empty = repository(self.root / "empty", {"README.md": "x\n", "src/main.py": "print()\n"})
        self.assertEqual([], self.check(empty))
        unmanaged = repository(self.root / "unmanaged", {"requirements.txt": "x\n"})
        self.assertEqual(["requirements.txt pip"], self.check(unmanaged))
        yaml_extension = repository(self.root / "yaml", {"requirements.txt": "x\n", ".github/dependabot.yaml": config(entry("pip", "/"))})
        self.assertEqual([], self.check(yaml_extension))

    def test_excluded_paths_are_not_checked(self) -> None:
        repo = repository(self.root / "excluded", {"fixtures/case/package.json": "{}\n", "fixtures/other.txt": "x\n", "package.json": "{}\n"})
        self.assertEqual(["fixtures/case/package.json npm", "package.json npm"], self.check(repo))
        self.assertEqual(["package.json npm"], self.check(repo, ["fixtures/"]))
        self.assertEqual(["package.json npm"], self.check(repo, ["fixtures/case/package.json"]))
        self.assertEqual(["fixtures/case/package.json npm", "package.json npm"], self.check(repo, ["fixtures/cas"]), "a prefix matches whole segments only")

    def test_untracked_package_files_are_not_checked(self) -> None:
        repo = repository(self.root / "untracked", {"README.md": "x\n"})
        (repo / "scratch-package.json").write_text("{}\n", encoding="utf-8")
        (repo / "package.json").write_text("{}\n", encoding="utf-8")
        self.assertEqual([], self.check(repo), "package.json is not tracked")

    def test_the_cli_reports_each_file_as_an_error_and_exits_non_zero(self) -> None:
        repo = repository(self.root / "cli", {"package.json": "{}\n", "lib/go.mod": "module x\n"})
        exclude_file = self.root / "exclude.txt"
        exclude_file.write_text("lib\n\n", encoding="utf-8")
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(repo)], capture_output=True, text=True, check=False)
        self.assertEqual(1, result.returncode)
        self.assertIn("::error file=package.json::package.json has no Dependabot entry for ecosystem npm that covers directory /", result.stdout)
        self.assertIn("::error file=lib/go.mod::", result.stdout)
        self.assertIn("2 tracked package file(s) without Dependabot coverage.", result.stderr)
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(repo), "--exclude-from", str(exclude_file), "--exclude", "package.json"], capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(repo), "--exclude-from", "-"], input="lib\npackage.json\n", capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("Every tracked package file has a covering Dependabot entry.", result.stdout)

    def test_unsupported_yaml_fails_the_check_with_the_reason(self) -> None:
        repo = repository(self.root / "bad-yaml", {".github/dependabot.yml": "version: 2\nupdates: &u\n  - package-ecosystem: npm\n"})
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(repo)], capture_output=True, text=True, check=False)
        self.assertEqual(2, result.returncode)
        self.assertIn("::error::.github/dependabot.yml: line 2: block scalars, anchors, aliases and tags are not supported", result.stderr)

    def test_the_ecosystem_mapping_is_the_documented_one(self) -> None:
        mapping = json.loads((RESOURCES / "package-ecosystems.json").read_text(encoding="utf-8"))
        self.assertEqual({
            "pip": ["requirements*.txt", "pyproject.toml"],
            "npm": ["package.json"],
            "nuget": ["*.csproj", "*.fsproj", "*.vbproj", "packages.config", "Directory.Packages.props"],
            "gomod": ["go.mod"],
            "cargo": ["Cargo.toml"],
            "dotnet-sdk": ["global.json"],
        }, mapping)

if __name__ == "__main__":
    unittest.main()
