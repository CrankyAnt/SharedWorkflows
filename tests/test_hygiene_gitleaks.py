from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from tests import temporary_support
from tests.hygiene_fixtures import REPOSITORY_ROOT, fixture_repository, git, tools_available, tools_directory
from tests.hygiene_process import hygiene

FAKE_TOKEN = "ghp_" + "".join(chr(ord("A") + (index * 7) % 26) + str(index % 10) for index in range(18))

HOME_PATH = "C:" + "\\" + "Users" + "\\" + "somebody" + "\\" + "notes.txt"


@unittest.skipUnless(tools_available("gitleaks"), "the pinned tools are neither in HYGIENE_TOOLS nor on PATH")
class CheckRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = temporary_support.Directory()
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

    def _repo_with_history_secret(self, name: str) -> tuple[Path, str]:
        """A repository whose history already holds a secret before ``base``, so only a full scan finds it."""
        repo = fixture_repository(self.root / name, {"README.md": "clean\n"})
        (repo / "old.txt").write_text(f"token={FAKE_TOKEN}\n", encoding="utf-8")
        git(repo, "add", "old.txt")
        git(repo, "commit", "-q", "-m", "chore: historical secret")
        (repo / "old.txt").write_text("redacted\n", encoding="utf-8")
        git(repo, "add", "old.txt")
        git(repo, "commit", "-q", "-m", "chore: remove secret from tip")
        return repo, git(repo, "rev-parse", "HEAD")

    def _write_pins(self, path: Path, gitleaks_version: str = "8.30.1") -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"tools": {"gitleaks": {"version": gitleaks_version}}}) + "\n", encoding="utf-8")
        return path

    def test_gitleaks_scans_only_added_commits_for_pull_request_and_push(self) -> None:
        repo, base = self._repo_with_history_secret("range")
        git(repo, "switch", "-q", "-c", "feat/add-note")
        (repo / "notes.txt").write_text("clean note\n", encoding="utf-8")
        git(repo, "add", "notes.txt")
        git(repo, "commit", "-q", "-m", "feat: add clean note")
        head = git(repo, "rev-parse", "HEAD")

        result = hygiene("gitleaks", repo, "--base", base, "--head", head, "--compare", "merge-base")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"--log-opts={base}..{head}", result.stdout)
        self.assertNotIn("--log-opts=--all", result.stdout)

        # Adding a secret in the new commit fails the range scan.
        (repo / "leak.txt").write_text(f"token={FAKE_TOKEN}\n", encoding="utf-8")
        git(repo, "add", "leak.txt")
        git(repo, "commit", "-q", "-m", "feat: add secret")
        leaky_head = git(repo, "rev-parse", "HEAD")
        result = hygiene("gitleaks", repo, "--base", base, "--head", leaky_head, "--compare", "merge-base")
        self.assertNotEqual(0, result.returncode, "a secret in the added commits must fail")
        self.assertIn(f"--log-opts={base}..{leaky_head}", result.stdout)

        # Direct comparison (push) scans only base..head.
        result = hygiene("gitleaks", repo, "--base", base, "--head", head, "--compare", "direct")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"--log-opts={base}..{head}", result.stdout)

        # An empty range (base == head) passes without a full scan.
        result = hygiene("gitleaks", repo, "--base", head, "--head", head, "--compare", "direct")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("--log-opts=--all", result.stdout)

    def test_gitleaks_scans_whole_history_when_config_baseline_or_in_repo_pin_changes(self) -> None:
        repo, base = self._repo_with_history_secret("triggers")
        shutil.copy2(REPOSITORY_ROOT / ".gitleaks.toml", repo / ".gitleaks.toml")
        (repo / ".gitleaks-baseline.json").write_text("[]\n", encoding="utf-8")
        self._write_pins(repo / "actions" / "repository-hygiene" / "pins.json", "8.30.1")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "chore: seed gitleaks files")
        seeded = git(repo, "rev-parse", "HEAD")

        # 1. Config change -> full history scan (catches the historical secret).
        (repo / ".gitleaks.toml").write_text((repo / ".gitleaks.toml").read_text(encoding="utf-8") + "\n# tweak\n", encoding="utf-8")
        git(repo, "add", ".gitleaks.toml")
        git(repo, "commit", "-q", "-m", "chore: update config")
        config_head = git(repo, "rev-parse", "HEAD")
        result = hygiene("gitleaks", repo, "--config", ".gitleaks.toml", "--baseline", ".gitleaks-baseline.json",
                         "--base", seeded, "--head", config_head, "--compare", "direct")
        self.assertNotEqual(0, result.returncode, "config change must scan whole history and find the historical secret")
        self.assertIn("--log-opts=--all", result.stdout)

        # 2. Baseline change -> full history scan.
        git(repo, "checkout", "-q", seeded)
        git(repo, "switch", "-q", "-c", "baseline-change")
        (repo / ".gitleaks-baseline.json").write_text("[] \n", encoding="utf-8")
        git(repo, "add", ".gitleaks-baseline.json")
        git(repo, "commit", "-q", "-m", "chore: update baseline")
        baseline_head = git(repo, "rev-parse", "HEAD")
        result = hygiene("gitleaks", repo, "--config", ".gitleaks.toml", "--baseline", ".gitleaks-baseline.json",
                         "--base", seeded, "--head", baseline_head, "--compare", "direct")
        self.assertNotEqual(0, result.returncode, "baseline change must scan whole history")
        self.assertIn("--log-opts=--all", result.stdout)

        # 3. Pinned version change in actions/repository-hygiene/pins.json -> full history scan.
        git(repo, "checkout", "-q", seeded)
        git(repo, "switch", "-q", "-c", "pin-change")
        self._write_pins(repo / "actions" / "repository-hygiene" / "pins.json", "8.31.0")
        git(repo, "add", "actions/repository-hygiene/pins.json")
        git(repo, "commit", "-q", "-m", "chore: bump gitleaks pin")
        pin_head = git(repo, "rev-parse", "HEAD")
        result = hygiene("gitleaks", repo, "--config", ".gitleaks.toml", "--baseline", ".gitleaks-baseline.json",
                         "--base", seeded, "--head", pin_head, "--compare", "direct")
        self.assertNotEqual(0, result.returncode, "in-repo gitleaks pin change must scan whole history")
        self.assertIn("--log-opts=--all", result.stdout)

    def test_gitleaks_consumer_marker_triggers_full_scan_when_missing_unreadable_or_pin_moves(self) -> None:
        # Clean repository with no historical secret so a default-branch full scan can pass and write a marker.
        repo = fixture_repository(self.root / "consumer", {"README.md": "clean\n"})
        base = git(repo, "rev-parse", "HEAD")
        (repo / "app.txt").write_text("v1\n", encoding="utf-8")
        git(repo, "add", "app.txt")
        git(repo, "commit", "-q", "-m", "feat: clean commit")
        head = git(repo, "rev-parse", "HEAD")

        pins = self._write_pins(self.root / "shared-v1" / "pins.json", "8.30.1")
        marker = self.root / "cache" / "marker.json"
        output = self.root / "github-output.txt"

        # 1. Missing marker -> full scan; --record-marker writes it after a pass.
        result = hygiene("gitleaks", repo, "--base", base, "--head", head, "--compare", "direct",
                         "--pins", str(pins), "--marker", str(marker), "--record-marker", str(marker),
                         "--github-output", str(output))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("--log-opts=--all", result.stdout)
        self.assertTrue(marker.is_file(), "a passed full scan writes the default-branch marker")
        self.assertIn("recorded-marker=true", output.read_text(encoding="utf-8"))

        # 2. Matching marker -> incremental scan only; does not rewrite marker.
        output.write_text("", encoding="utf-8")
        result = hygiene("gitleaks", repo, "--base", base, "--head", head, "--compare", "direct",
                         "--pins", str(pins), "--marker", str(marker), "--record-marker", str(marker),
                         "--github-output", str(output))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"--log-opts={base}..{head}", result.stdout)
        self.assertNotIn("--log-opts=--all", result.stdout)
        self.assertIn("recorded-marker=false", output.read_text(encoding="utf-8"))

        # 3. Unreadable marker -> full scan.
        bad_marker = self.root / "cache" / "corrupt.json"
        bad_marker.write_text("{not-json", encoding="utf-8")
        result = hygiene("gitleaks", repo, "--base", base, "--head", head, "--compare", "direct",
                         "--pins", str(pins), "--marker", str(bad_marker))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("--log-opts=--all", result.stdout)

        # 4. Pinned Gitleaks version moves at v1 -> old marker no longer matches -> full scan.
        moved_pins = self._write_pins(self.root / "shared-v1-moved" / "pins.json", "8.31.0")
        result = hygiene("gitleaks", repo, "--base", base, "--head", head, "--compare", "direct",
                         "--pins", str(moved_pins), "--marker", str(marker))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("--log-opts=--all", result.stdout)

        # 5. Failed full scan must not write the marker.
        leaky_repo, leaky_base = self._repo_with_history_secret("consumer-leaky")
        (leaky_repo / "app.txt").write_text("clean\n", encoding="utf-8")
        git(leaky_repo, "add", "app.txt")
        git(leaky_repo, "commit", "-q", "-m", "feat: clean")
        leaky_head = git(leaky_repo, "rev-parse", "HEAD")
        failed_marker = self.root / "cache" / "never-written.json"
        result = hygiene("gitleaks", leaky_repo, "--base", leaky_base, "--head", leaky_head, "--compare", "direct",
                         "--pins", str(pins), "--marker", str(failed_marker), "--record-marker", str(failed_marker))
        self.assertNotEqual(0, result.returncode)
        self.assertFalse(failed_marker.exists(), "a failed scan must never write the marker")

    def test_gitleaks_identical_tree_push_still_scans_its_new_commits(self) -> None:
        repo, base = self._repo_with_history_secret("identical")
        # Create a commit that introduces a secret, then a commit that reverts it so the tip tree matches,
        # or a squash/new commit with the same tree carrying a secret in its message/diff.
        (repo / "secret.txt").write_text(f"token={FAKE_TOKEN}\n", encoding="utf-8")
        git(repo, "add", "secret.txt")
        git(repo, "commit", "-q", "-m", "feat: introduce secret")
        git(repo, "rm", "-q", "secret.txt")
        git(repo, "commit", "-q", "-m", "feat: remove secret")
        head = git(repo, "rev-parse", "HEAD")
        self.assertEqual(git(repo, "rev-parse", f"{base}^{{tree}}"), git(repo, "rev-parse", f"{head}^{{tree}}"),
                         "the tip tree equals base's tree while new commits carry a secret")
        lookup = self.root / "lookup.json"
        lookup.write_text(json.dumps({"case": "identical", "head": head, "landing_base": base, "pull_request": 7}), encoding="utf-8")

        result = hygiene("gitleaks", repo, "--lookup", str(lookup))
        self.assertNotEqual(0, result.returncode, "an identical tree must still scan its new commits")
        self.assertIn(f"--log-opts={base}..{head}", result.stdout)
        self.assertNotIn("--log-opts=--all", result.stdout)


if __name__ == "__main__":
    unittest.main()
