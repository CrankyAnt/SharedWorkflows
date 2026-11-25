"""The Dependabot coverage check of the shared hygiene workflow, over fixture trees and configurations."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path
from tests import temporary_support


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

    def test_reads_escaped_double_quotes_before_comments_and_in_flow_collections(self) -> None:
        document = coverage.parse_yaml(
            'name: "a \\" # not a comment" # real comment\n'
            'list: ["x \\", z", "ok"] # trailing\n'
            'escaped_backslash: "tail\\\\" # real comment\n'
        )
        self.assertEqual('a " # not a comment', document["name"])
        self.assertEqual(['x ", z', "ok"], document["list"])
        self.assertEqual("tail\\", document["escaped_backslash"])

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
        self.temporary = temporary_support.Directory()
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

    def test_double_star_directory_glob_matches_zero_or_more_segments(self) -> None:
        repo = repository(self.root / "zero-segments", {
            "tests/package.json": "{}\n",
            "a/b/tests/package.json": "{}\n",
            "src/tests/requirements.txt": "flask\n",
            "src/a/b/tests/requirements.txt": "flask\n",
            "other/package.json": "{}\n",
            ".github/dependabot.yml": config(
                entry("npm", directories=["/**/tests"]),
                entry("pip", directories=["/src/**/tests"]),
            ),
        })
        self.assertEqual(["other/package.json npm"], self.check(repo))

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


SLN_PREAMBLE = "﻿\nMicrosoft Visual Studio Solution File, Format Version 12.00\n# Visual Studio Version 17\n"
CSHARP = "{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}"
SOLUTION_FOLDER = "{2150E333-8FDC-42A3-9474-1A3956D46DE8}"


def sln(*projects: tuple[str, str, str]) -> str:
    """A .sln with one ``Project`` line per (type GUID, name, path)."""
    lines = "".join(f'Project("{kind}") = "{name}", "{path}", "{{0000000{index}-0000-0000-0000-000000000000}}"\nEndProject\n'
                    for index, (kind, name, path) in enumerate(projects))
    return SLN_PREAMBLE + lines + "Global\nEndGlobal\n"


def slnx(body: str) -> str:
    return f"<Solution>\n{body}</Solution>\n"


class SolutionCoverageTests(unittest.TestCase):
    """A project file that a solution directly in a covered nuget directory lists counts as covered (issue #161)."""

    def setUp(self) -> None:
        self.temporary = temporary_support.Directory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ecosystems = coverage.load_ecosystems()

    def check(self, name: str, files: dict[str, str], excludes: list[str] | None = None) -> list[str]:
        repo = repository(self.root / name, files)
        return [item.path for item in coverage.check(repo, self.ecosystems, excludes)]

    def run_cli(self, name: str, files: dict[str, str]) -> subprocess.CompletedProcess[str]:
        repo = repository(self.root / name, files)
        return subprocess.run([sys.executable, str(SCRIPT), "--root", str(repo)], capture_output=True, text=True, check=False)

    def test_a_slnx_in_a_covered_directory_covers_the_projects_it_lists_in_other_directories(self) -> None:
        files = {
            "App.slnx": slnx('  <Project Path="src/App/App.csproj" />\n  <Project Path="tests/App.Tests/App.Tests.fsproj" />\n'),
            "Directory.Packages.props": "<Project />\n",
            "src/App/App.csproj": "<Project />\n",
            "tests/App.Tests/App.Tests.fsproj": "<Project />\n",
            "tools/Unlisted/Unlisted.vbproj": "<Project />\n",
            ".github/dependabot.yml": config(entry("nuget", "/")),
        }
        self.assertEqual(["tools/Unlisted/Unlisted.vbproj"], self.check("slnx", files))
        result = self.run_cli("slnx-cli", {path: text for path, text in files.items() if not path.startswith("tools/")})
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_a_sln_lists_paths_with_backslashes_and_another_case(self) -> None:
        files = {
            "build/App.sln": sln((CSHARP, "App", "..\\SRC\\app\\App.CSPROJ"), (SOLUTION_FOLDER, "src", "src"), (CSHARP, "Lib", "Lib\\Lib.csproj")),
            "src/App/App.csproj": "<Project />\n",
            "build/Lib/Lib.csproj": "<Project />\n",
            ".github/dependabot.yml": config(entry("nuget", "/build")),
        }
        self.assertEqual([], self.check("sln", files))

    def test_a_project_inside_a_slnx_folder_element_is_covered(self) -> None:
        files = {
            "App.slnx": slnx('  <Folder Name="/src/">\n    <Folder Name="/src/nested/">\n      <Project Path="src\\Deep\\Deep.csproj" />\n'
                             '    </Folder>\n    <File Path="README.md" />\n  </Folder>\n'),
            "src/Deep/Deep.csproj": "<Project />\n",
            ".github/dependabot.yml": config(entry("nuget", "/")),
        }
        self.assertEqual([], self.check("folder", files))

    def test_a_solution_in_a_directory_covered_only_by_a_directories_glob_covers_its_projects(self) -> None:
        files = {
            "apps/Web/Web.slnx": slnx('  <Project Path="../../libs/Core/Core.csproj" />\n'),
            "apps/Web/Web.csproj": "<Project />\n",
            "libs/Core/Core.csproj": "<Project />\n",
            ".github/dependabot.yml": config(entry("nuget", directories=["/apps/*"])),
        }
        self.assertEqual([], self.check("glob", files))

    def test_a_listed_path_outside_the_repository_or_without_a_tracked_file_covers_nothing_and_fails_nothing(self) -> None:
        files = {
            "App.slnx": slnx('  <Project Path="../Outside/Outside.csproj" />\n  <Project Path="src/Missing/Missing.csproj" />\n'
                             '  <Project Path="/abs/Abs.csproj" />\n  <Project Path="src/Build/build.proj" />\n  <Project Path="src/Other/Other.esproj" />\n'),
            "src/Build/build.proj": "<Project />\n",
            "src/Other/Other.esproj": "<Project />\n",
            "src/Other/Other.csproj": "<Project />\n",
            "src/Untracked/Untracked.csproj": "<Project />\n",
            ".github/dependabot.yml": config(entry("nuget", "/")),
        }
        repo = repository(self.root / "outside", files)
        git(repo, "rm", "-q", "--cached", "src/Untracked/Untracked.csproj")
        (repo / "src" / "Missing").mkdir(parents=True)
        uncovered = coverage.check(repo, self.ecosystems)
        self.assertEqual(["src/Other/Other.csproj"], [item.path for item in uncovered])
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(repo)], capture_output=True, text=True, check=False)
        self.assertEqual(1, result.returncode)
        self.assertIn("::error file=src/Other/Other.csproj::src/Other/Other.csproj has no Dependabot entry for ecosystem nuget that covers directory /src/Other", result.stdout)
        self.assertEqual(1, result.stdout.count("::error"), result.stdout)

    def test_a_solution_in_a_subdirectory_or_an_uncovered_directory_covers_nothing(self) -> None:
        files = {
            "src/App.slnx": slnx('  <Project Path="App/App.csproj" />\n'),
            "other/Other.sln": sln((CSHARP, "Lib", "..\\lib\\Lib.csproj")),
            "src/App/App.csproj": "<Project />\n",
            "lib/Lib.csproj": "<Project />\n",
            ".github/dependabot.yml": config(entry("nuget", "/")),
        }
        result = self.run_cli("nested", files)
        self.assertEqual(1, result.returncode)
        self.assertIn("::error file=lib/Lib.csproj::lib/Lib.csproj has no Dependabot entry for ecosystem nuget that covers directory /lib", result.stdout)
        self.assertIn("::error file=src/App/App.csproj::src/App/App.csproj has no Dependabot entry for ecosystem nuget that covers directory /src/App", result.stdout)
        self.assertEqual(2, result.stdout.count("::error"), result.stdout)

    def test_a_project_reached_only_through_a_project_reference_is_not_covered(self) -> None:
        files = {
            "App.slnx": slnx('  <Project Path="src/App/App.csproj" />\n'),
            "src/App/App.csproj": '<Project>\n  <ItemGroup>\n    <ProjectReference Include="..\\Lib\\Lib.csproj" />\n  </ItemGroup>\n</Project>\n',
            "src/Lib/Lib.csproj": "<Project />\n",
            ".github/dependabot.yml": config(entry("nuget", "/")),
        }
        self.assertEqual(["src/Lib/Lib.csproj"], self.check("reference", files))

    def test_a_solution_in_an_uncovered_directory_fails_nothing_of_its_own_even_when_unreadable(self) -> None:
        files = {
            "elsewhere/Broken.slnx": "<Solution>\n",
            "elsewhere/Broken.sln": "not a solution\n",
            "package.json": "{}\n",
            ".github/dependabot.yml": config(entry("npm", "/"), entry("nuget", "/src")),
        }
        result = self.run_cli("uncovered-solution", files)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_an_unreadable_solution_in_a_covered_directory_exits_2_and_names_the_file(self) -> None:
        for name, content in [
            ("Broken.slnx", "<Solution>\n  <Project Path=\"a.csproj\">\n</Solution>\n"),
            ("Wrong.slnx", "<Project />\n"),
            ("NoPath.slnx", slnx("  <Project />\n")),
            ("Header.sln", "Project(\"x\") = \"a\", \"a.csproj\", \"{0}\"\n"),
            ("Line.sln", SLN_PREAMBLE + "Project(\"{X}\") = \"a\"\nEndProject\n"),
        ]:
            with self.subTest(solution=name):
                result = self.run_cli(f"broken-{name}", {name: content, "App.csproj": "<Project />\n", ".github/dependabot.yml": config(entry("nuget", "/"))})
                self.assertEqual(2, result.returncode, result.stdout + result.stderr)
                self.assertIn(f"::error::{name}: ", result.stderr)
        repo = repository(self.root / "broken-bytes", {"App.csproj": "<Project />\n", ".github/dependabot.yml": config(entry("nuget", "/"))})
        (repo / "Bytes.sln").write_bytes(b"\xff\x00 not text")
        git(repo, "add", "-A")
        result = subprocess.run([sys.executable, str(SCRIPT), "--root", str(repo)], capture_output=True, text=True, check=False)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)
        self.assertIn("::error::Bytes.sln: cannot be read as a solution", result.stderr)

    def test_a_utf16_sln_and_a_slnx_with_a_namespace_are_read(self) -> None:
        repo = repository(self.root / "encodings", {
            "src/App/App.csproj": "<Project />\n",
            "src/Lib/Lib.csproj": "<Project />\n",
            "Tools.slnx": '<Solution xmlns="urn:example">\n  <Folder Name="/src/">\n    <Project Path="src/Lib/Lib.csproj" />\n  </Folder>\n</Solution>\n',
            ".github/dependabot.yml": config(entry("nuget", "/")),
        })
        (repo / "App.sln").write_bytes(sln((CSHARP, "App", "src\\App\\App.csproj")).lstrip("﻿").encode("utf-16"))
        git(repo, "add", "-A")
        self.assertEqual([], coverage.check(repo, self.ecosystems))

    def test_package_files_other_than_project_files_keep_the_directory_rule(self) -> None:
        files = {
            "App.slnx": slnx('  <Project Path="src/App/App.csproj" />\n  <Project Path="src/App/packages.config" />\n'),
            "src/App/App.csproj": "<Project />\n",
            "src/App/packages.config": "<packages />\n",
            "src/App/Directory.Packages.props": "<Project />\n",
            ".github/dependabot.yml": config(entry("nuget", "/")),
        }
        self.assertEqual(["src/App/Directory.Packages.props", "src/App/packages.config"], self.check("props", files))

    def test_an_excluded_solution_is_not_read(self) -> None:
        files = {
            "fixtures/Broken.slnx": "<Solution>\n",
            "fixtures/App.slnx": slnx('  <Project Path="../src/App.csproj" />\n'),
            "src/App.csproj": "<Project />\n",
            ".github/dependabot.yml": config(entry("nuget", directories=["/fixtures"])),
        }
        self.assertEqual(["src/App.csproj"], self.check("excluded", files, ["fixtures"]))


if __name__ == "__main__":
    unittest.main()
