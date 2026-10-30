"""The hygiene decision on a push through the push lookup, and the reuse of passed checks on an ``edited`` run."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.hygiene_fixtures import fixture_repository, git
from tests.hygiene_process import hygiene

CHECKS = ("actionlint", "zizmor", "packages", "markdown", "reuse")


class HygieneFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = fixture_repository(self.root / "repo", {"README.md": "start\n", "src/app.py": "print(1)\n"})
        self.base = git(self.repo, "rev-parse", "HEAD")

    def commit(self, files: dict[str, str]) -> str:
        for relative, content in files.items():
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "feat: change")
        return git(self.repo, "rev-parse", "HEAD")

    def decide(self, *arguments: str) -> tuple[dict[str, str], str, str]:
        output, summary = self.root / "output.txt", self.root / "summary.md"
        output.write_text("", encoding="utf-8")
        summary.write_text("", encoding="utf-8")
        result = hygiene("changes", self.repo, "--github-output", str(output), "--summary", str(summary), *arguments)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        decisions = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
        return decisions, result.stdout, summary.read_text(encoding="utf-8")

    def lookup(self, **fields) -> str:
        path = self.root / "push-lookup.json"
        path.write_text(json.dumps({"reason": "fixture", **fields}), encoding="utf-8")
        return str(path)


class PushLookupTests(HygieneFixture):
    def test_a_push_whose_tree_equals_the_passed_tested_tree_runs_no_check_but_gitleaks(self) -> None:
        head = self.commit({".github/workflows/ci.yml": "x: 1\n", "docs/notes.md": "n\n", "web/package.json": "{}\n"})
        decisions, _, summary = self.decide("--lookup", self.lookup(case="identical", head=head, landing_base=self.base, pull_request=7))
        self.assertEqual({name: "false" for name in CHECKS}, {name: decisions[name] for name in CHECKS})
        self.assertEqual("false", decisions["workflows"])
        self.assertIn("the tree equals the tested tree that passed on pull request #7", summary)
        self.assertIn("| Gitleaks | yes | every event |", summary)

    def test_a_push_whose_pull_request_was_behind_runs_only_the_checks_both_sides_select(self) -> None:
        tested_base = self.base
        landing = self.commit({".github/workflows/ci.yml": "x: 1\n", "web/package.json": "{}\n"})
        head = self.commit({".github/workflows/other.yml": "y: 1\n", "docs/notes.md": "n\n"})
        decisions, stdout, _ = self.decide("--lookup", self.lookup(case="behind", head=head, landing_base=landing, tested_base=tested_base,
                                                                   pull_request=7))
        self.assertEqual({"actionlint": "true", "zizmor": "true", "packages": "false", "markdown": "false", "reuse": "false"},
                         {name: decisions[name] for name in CHECKS})
        self.assertEqual("true", decisions["workflows"])
        self.assertIn("markdown=false: only the pull request's side selects it", stdout)
        self.assertIn("packages=false: only main's side selects it", stdout)

    def test_a_push_without_a_passed_tested_tree_decides_over_its_landing_base_and_without_one_runs_every_check(self) -> None:
        head = self.commit({"docs/notes.md": "n\n"})
        for case in ("own", "pull-request"):
            with self.subTest(case=case):
                decisions, _, _ = self.decide("--lookup", self.lookup(case=case, head=head, landing_base=self.base))
                self.assertEqual({"actionlint": "false", "zizmor": "false", "packages": "false", "markdown": "true", "reuse": "false"},
                                 {name: decisions[name] for name in CHECKS})
        decisions, _, _ = self.decide("--lookup", self.lookup(case="own", head=head, landing_base=None))
        self.assertEqual({"true"}, {decisions[name] for name in CHECKS if name != "reuse"}, "without a landing base every check runs")

    def test_a_lookup_together_with_a_base_is_refused(self) -> None:
        head = self.commit({"docs/notes.md": "n\n"})
        result = hygiene("changes", self.repo, "--lookup", self.lookup(case="own", head=head, landing_base=self.base), "--base", self.base, "--head", head)
        self.assertEqual(2, result.returncode, result.stdout + result.stderr)


class EditedReuseTests(HygieneFixture):
    def record(self, tree: str, checks: dict[str, int]) -> str:
        path = self.root / "record.json"
        path.write_text(json.dumps({"tree": tree, "checks": checks}), encoding="utf-8")
        return str(path)

    def test_an_edited_run_reuses_each_selected_check_that_passed_on_the_same_tested_tree(self) -> None:
        head = self.commit({".github/workflows/ci.yml": "x: 1\n", "docs/notes.md": "n\n"})
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        decisions, stdout, summary = self.decide("--base", self.base, "--head", head, "--results", self.record(tree, {"actionlint": 111, "markdown": 222, "packages": 333}))
        self.assertEqual({"actionlint": "false", "zizmor": "true", "packages": "false", "markdown": "false", "reuse": "false"},
                         {name: decisions[name] for name in CHECKS})
        self.assertEqual({"actionlint": 111, "markdown": 222}, json.loads(decisions["reused"]), "a check the change does not select is not reused")
        self.assertIn("::notice title=actionlint::passed in run 111, whose tested tree this run shares", stdout)
        self.assertIn("| actionlint | passed in run 111 |", summary)
        self.assertIn("| Relative links | passed in run 222 |", summary)
        self.assertIn("| Gitleaks | yes | every event |", summary)

    def test_an_edited_run_never_takes_gitleaks_or_a_convention_check_from_the_record(self) -> None:
        head = self.commit({".github/workflows/ci.yml": "x: 1\n"})
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        record = self.record(tree, {"actionlint": 111, "gitleaks": 111, "branch": 111, "commits": 111, "title": 111})
        decisions, stdout, summary = self.decide("--base", self.base, "--head", head, "--results", record)
        self.assertEqual({"actionlint": 111}, json.loads(decisions["reused"]))
        self.assertEqual(1, stdout.count("::notice title="), stdout)
        self.assertIn("| Gitleaks | yes | every event |", summary)
        self.assertEqual(1, summary.count("| passed in run 111 |"), summary)

    def test_an_edited_run_on_another_tree_or_without_a_record_reuses_nothing(self) -> None:
        head = self.commit({".github/workflows/ci.yml": "x: 1\n"})
        other = git(self.repo, "rev-parse", f"{self.base}^{{tree}}")
        for record in (self.record(other, {"actionlint": 111}), str(self.root / "missing.json")):
            with self.subTest(record=record):
                decisions, _, _ = self.decide("--base", self.base, "--head", head, "--results", record)
                self.assertEqual("true", decisions["actionlint"])
                self.assertEqual({}, json.loads(decisions["reused"]))

    def test_record_keeps_the_checks_that_passed_or_were_reused_with_the_run_they_passed_in(self) -> None:
        head = self.commit({"docs/notes.md": "n\n"})
        output = self.root / "stored.json"
        result = hygiene("record", self.repo, "--run-id", "444", "--outcome", "actionlint=success", "--outcome", "zizmor=failure",
                         "--outcome", "markdown=skipped", "--outcome", "reuse=success", "--reused", json.dumps({"packages": 111}), "--output", str(output))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual({"tree": git(self.repo, "rev-parse", f"{head}^{{tree}}"), "checks": {"actionlint": 444, "packages": 111, "reuse": 444}},
                         json.loads(output.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
