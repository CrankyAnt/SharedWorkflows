from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.hygiene_fixtures import REPOSITORY_ROOT, fixture_repository, git, tools_available, tools_directory
from tests.hygiene_process import hygiene

FAKE_TOKEN = "ghp_" + "".join(chr(ord("A") + (index * 7) % 26) + str(index % 10) for index in range(18))

HOME_PATH = "C:" + "\\" + "Users" + "\\" + "somebody" + "\\" + "notes.txt"


@unittest.skipUnless(tools_available("gitleaks"), "the pinned tools are neither in HYGIENE_TOOLS nor on PATH")
class CheckRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_gitleaks_scans_all_reachable_history_and_uses_the_config_and_baseline_inputs(self) -> None:
        repo = fixture_repository(self.root / "leaks", {"README.md": "clean\n"})
        git(repo, "switch", "-q", "-c", "side")
        (repo / "notes.txt").write_text(f"token={FAKE_TOKEN}\n", encoding="utf-8")
        git(repo, "add", "notes.txt")
        git(repo, "commit", "-q", "-m", "chore: side branch")
        git(repo, "switch", "-q", "main")
        self.assertFalse((repo / "notes.txt").exists())
        result = hygiene("gitleaks", repo)
        self.assertNotEqual(0, result.returncode, "the side branch secret is reachable history")
        self.assertIn("--log-opts=--all", result.stdout)
        baseline = self.root / "baseline.json"
        directory = tools_directory()
        gitleaks = next(path for path in ([directory / "gitleaks", directory / "gitleaks.exe"] if directory else [Path(shutil.which("gitleaks") or "")]) if path.is_file())
        subprocess.run([str(gitleaks), "git", "--log-opts=--all", "--no-banner", "--report-format", "json", "--report-path", str(baseline), "."], cwd=repo, capture_output=True, check=False)
        self.assertTrue(baseline.is_file())
        result = hygiene("gitleaks", repo, "--baseline", str(baseline))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"--baseline-path {baseline}", result.stdout)
        shutil.copy2(REPOSITORY_ROOT / ".gitleaks.toml", repo / "rules.toml")
        (repo / "paths.txt").write_text(f"{HOME_PATH}\n", encoding="utf-8")
        git(repo, "add", "paths.txt")
        git(repo, "commit", "-q", "-m", "chore: personal path")
        result = hygiene("gitleaks", repo, "--baseline", str(baseline))
        self.assertEqual(0, result.returncode, "the default rules do not report personal home paths")
        result = hygiene("gitleaks", repo, "--config", "rules.toml", "--baseline", str(baseline))
        self.assertNotEqual(0, result.returncode, "the repository's rule reports the personal home path")
        self.assertIn("--config rules.toml", result.stdout)
        # With the repository's own baseline, a personal home path in a committed configuration file is the one finding,
        # reported under the repository's rule and redacted.
        shutil.copy2(REPOSITORY_ROOT / ".gitleaks-baseline.json", repo / "baseline.json")
        configuration = repo / "configs" / "codex" / "config.toml"
        configuration.parent.mkdir(parents=True)
        configuration.write_text(f'workspace = "{HOME_PATH}"\n', encoding="utf-8")
        git(repo, "add", "configs")
        git(repo, "commit", "-q", "-m", "chore: configuration with a personal path")
        report = self.root / "policy-findings.json"
        scan = subprocess.run([str(gitleaks), "git", "--config", "rules.toml", "--baseline-path", "baseline.json", "--log-opts=HEAD~1..HEAD", "--redact",
                               "--no-banner", "--no-color", "--report-format", "json", "--report-path", str(report), "."],
                              cwd=repo, capture_output=True, text=True, check=False)
        self.assertEqual(1, scan.returncode, scan.stdout + scan.stderr)
        self.assertEqual([item["RuleID"] for item in json.loads(report.read_text(encoding="utf-8"))], ["personal-user-home-path"])
        self.assertNotIn("somebody", scan.stdout + scan.stderr, "the report is redacted")


if __name__ == "__main__":
    unittest.main()
