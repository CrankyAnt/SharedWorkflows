"""The proof repository kit: the content of the proof repository, whose fixed callers equal the templates except that
they call the shared workflows at the candidate tag, with a small Python and a small .NET project for the pipeline."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PIPELINE = REPOSITORY_ROOT / "actions" / "pipeline"
KIT = PIPELINE / "proof-kit"
TEMPLATES = PIPELINE / "consumer-kit"
SURFACE = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "shared-surface.json"
CALLERS = ("ci.yml", "automation.yml", "release.yml")
WORKFLOWS = ".github/workflows"
sys.path.insert(0, str(PIPELINE))
import consumer_checks  # noqa: E402
import plan  # noqa: E402


def kit_files() -> list[str]:
    return sorted(path.relative_to(KIT).as_posix() for path in KIT.rglob("*") if path.is_file())


def as_proof_caller(template: str, tag: str) -> str:
    """The one difference between a template and the proof repository's caller: the tag in the line that calls the shared workflow."""
    lines = template.splitlines(keepends=True)
    calls = [index for index, line in enumerate(lines) if line.lstrip().startswith("uses: CrankyAnt/SharedWorkflows/.github/workflows/")]
    assert len(calls) == 1 and lines[calls[0]].endswith("@v1\n"), "a template calls one shared workflow, at v1"
    lines[calls[0]] = lines[calls[0]].removesuffix("@v1\n") + f"@{tag}\n"
    return "".join(lines)


class ProofKitCallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.surface = json.loads(SURFACE.read_text(encoding="utf-8"))
        self.tag = self.surface["candidate_tag"]

    def test_each_caller_equals_its_template_except_for_the_candidate_tag(self) -> None:
        for name in CALLERS:
            with self.subTest(caller=name):
                template = (TEMPLATES / name).read_bytes().decode("utf-8")
                held = (KIT / WORKFLOWS / name).read_bytes().decode("utf-8")
                self.assertEqual(as_proof_caller(template, self.tag), held)
                self.assertNotEqual(template, held)
                self.assertEqual(1, held.count(f"@{self.tag}"))
                self.assertNotIn("@v1\n", held)

    def test_a_caller_with_any_other_difference_is_no_caller_of_the_kit(self) -> None:
        template = (TEMPLATES / "ci.yml").read_text(encoding="utf-8")
        expected = as_proof_caller(template, self.tag)
        for name, changed in (("at v1", template), ("another tag", as_proof_caller(template, "v2")), ("a further line", expected + "# a note\n"),
                              ("another permission", expected.replace("contents: read", "contents: write"))):
            with self.subTest(name=name):
                self.assertNotEqual(expected, changed)
        self.assertEqual(expected, template.replace("pipeline.yml@v1", f"pipeline.yml@{self.tag}"))

    def test_the_kit_holds_the_three_fixed_callers_and_no_other_workflow(self) -> None:
        self.assertEqual(sorted(f"{WORKFLOWS}/{name}" for name in CALLERS), [path for path in kit_files() if path.startswith(f"{WORKFLOWS}/")])
        self.assertEqual([], [path for path in kit_files() if path.endswith((".yml", ".yaml")) and "/workflows/" in path and not path.startswith(f"{WORKFLOWS}/")])


class ProofKitProjectTests(unittest.TestCase):
    def setUp(self) -> None:
        text = (KIT / plan.CONFIGURATION).read_text(encoding="utf-8")
        self.configuration, _ = plan.checked_configuration(text, plan.schema_versions(plan.SCHEMAS))

    def test_the_configuration_file_switches_the_python_parts_and_the_dotnet_part_on_for_the_kit_s_projects(self) -> None:
        parts = self.configuration["parts"]
        self.assertEqual({"python-verify", "python-test", "dotnet-test"}, set(parts))
        for name in ("python-verify", "python-test"):
            with self.subTest(part=name):
                self.assertEqual(["python"], [project["working-directory"] for project in parts[name]["projects"]])
        self.assertEqual(["dotnet/Greeting.Tests.csproj"], [project["project"] for project in parts["dotnet-test"]["projects"]])
        for path in ("python/pyproject.toml", "python/greeting.py", "python/tests/test_greeting.py", "dotnet/Greeting.Tests.csproj", "dotnet/Greeting.cs", "dotnet/GreetingTests.cs"):
            with self.subTest(path=path):
                self.assertIn(path, kit_files())

    def test_every_part_runs_on_every_candidate(self) -> None:
        for name, part in self.configuration["parts"].items():
            with self.subTest(part=name):
                self.assertNotIn("paths", part, "a part with paths would be skipped by a commit that changes nothing of the kit")

    def test_the_dependabot_configuration_is_the_one_generated_from_the_configuration_file(self) -> None:
        self.assertEqual({"pip": ["/python"], "nuget": ["/dotnet"]}, self.configuration["dependabot"])
        expected = consumer_checks.dependabot_text(self.configuration["dependabot"], "CrankyAnt/SharedWorkflows")
        self.assertEqual(expected.encode("utf-8"), (KIT / ".github" / "dependabot.yml").read_bytes())

    def test_the_kit_carries_its_licence_and_only_the_files_of_the_proof_repository(self) -> None:
        self.assertEqual((REPOSITORY_ROOT / "LICENSES" / "MIT.txt").read_bytes(), (KIT / "LICENSES" / "MIT.txt").read_bytes())
        self.assertIn('SPDX-License-Identifier = "MIT"', (KIT / "REUSE.toml").read_text(encoding="utf-8"))
        self.assertEqual([".github/ci-config.yml", ".github/dependabot.yml", *sorted(f"{WORKFLOWS}/{name}" for name in CALLERS), "LICENSES/MIT.txt", "REUSE.toml",
                          "docs/agents/ci.md", "dotnet/Greeting.Tests.csproj", "dotnet/Greeting.cs", "dotnet/GreetingTests.cs",
                          "python/greeting.py", "python/pyproject.toml", "python/tests/test_greeting.py"], kit_files())

    def test_the_pointer_document_equals_its_template(self) -> None:
        self.assertEqual((TEMPLATES / "docs" / "agents" / "ci.md").read_bytes(), (KIT / "docs" / "agents" / "ci.md").read_bytes())

    def test_the_dependabot_entries_of_this_repository_cover_the_kit_s_projects(self) -> None:
        dependabot = (REPOSITORY_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        for ecosystem, directory in (("pip", "/actions/pipeline/proof-kit/python"), ("nuget", "/actions/pipeline/proof-kit/dotnet")):
            with self.subTest(ecosystem=ecosystem):
                self.assertIn(f"  - package-ecosystem: {ecosystem}\n    directory: {directory}\n", dependabot)


class ProofKitChecksTests(unittest.TestCase):
    """The consumer checks of the pipeline's hygiene job on the kit as the proof repository holds it."""

    def check(self, command: str, repository: str) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            environment = {name: value for name, value in os.environ.items() if name != "GITHUB_TOKEN"}
            with mock.patch.dict(os.environ, environment, clear=True), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = consumer_checks.main([command, "--root", str(KIT), "--event", "pull_request", "--author", "example-author", "--repository", repository,
                                               "--api-url", "http://127.0.0.1:9", "--summary", str(summary)])
            return status, summary.read_text(encoding="utf-8") if summary.exists() else ""

    def test_the_kit_passes_the_caller_check_and_the_dependabot_check_as_the_proof_repository(self) -> None:
        proof = json.loads(SURFACE.read_text(encoding="utf-8"))["proof_repository"]
        status, summary = self.check("callers", proof)
        self.assertEqual(0, status, summary)
        self.assertIn("except that a caller calls the shared workflow at the candidate tag `v1-candidate`", summary)
        status, summary = self.check("dependabot", proof)
        self.assertEqual(0, status, summary)

    def test_the_kit_s_callers_fail_in_any_other_repository(self) -> None:
        status, summary = self.check("callers", "CrankyAnt/AnotherRepository")
        self.assertEqual(1, status, summary)
        self.assertIn("differs from its template; a fixed caller equals its template byte for byte", summary)


if __name__ == "__main__":
    unittest.main()
