"""The hygiene decision on a push through the push lookup, and the reuse of passed checks on an ``edited`` run."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests import temporary_support
from tests.hygiene_fixtures import fixture_repository, git
from tests.hygiene_process import hygiene

CHECKS = ("actionlint", "zizmor", "packages", "markdown", "reuse")


class HygieneFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = temporary_support.Directory()
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

    def test_a_push_whose_pull_request_was_behind_runs_only_the_checks_both_sides_select_when_landing_base_passed(self) -> None:
        tested_base = self.base
        landing = self.commit({".github/workflows/ci.yml": "x: 1\n", "web/package.json": "{}\n"})
        head = self.commit({".github/workflows/other.yml": "y: 1\n", "docs/notes.md": "n\n"})
        decisions, stdout, _ = self.decide("--lookup", self.lookup(case="behind", head=head, landing_base=landing, tested_base=tested_base,
                                                                   pull_request=7, landing_base_passed=True))
        self.assertEqual({"actionlint": "true", "zizmor": "true", "packages": "false", "markdown": "false", "reuse": "false"},
                         {name: decisions[name] for name in CHECKS})
        self.assertEqual("true", decisions["workflows"])
        self.assertIn("markdown=false: only the pull request's side selects it", stdout)
        self.assertIn("packages=false: only main's side selects it", stdout)

    def test_a_push_whose_pull_request_was_behind_an_unpassed_landing_base_runs_every_check_the_newer_base_selects(self) -> None:
        tested_base = self.base
        landing = self.commit({".github/workflows/ci.yml": "x: 1\n", "web/package.json": "{}\n"})
        head = self.commit({".github/workflows/other.yml": "y: 1\n", "docs/notes.md": "n\n"})
        for extra in ({"landing_base_passed": False}, {}):
            with self.subTest(extra=extra):
                decisions, stdout, _ = self.decide("--lookup", self.lookup(case="behind", head=head, landing_base=landing, tested_base=tested_base,
                                                                           pull_request=7, **extra))
                self.assertEqual({"actionlint": "true", "zizmor": "true", "packages": "true", "markdown": "false", "reuse": "false"},
                                 {name: decisions[name] for name in CHECKS})
                self.assertEqual("true", decisions["workflows"])
                self.assertIn("markdown=false: only the pull request's side selects it", stdout)
                self.assertIn("packages=true: the newer base has not passed", stdout)

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

    def test_a_missing_or_unreadable_lookup_selects_every_check_instead_of_none(self) -> None:
        corrupt = self.root / "corrupt-lookup.json"
        corrupt.write_text("{not json\n", encoding="utf-8")
        incomplete = self.root / "incomplete-lookup.json"
        incomplete.write_text(json.dumps({"case": "unknown"}), encoding="utf-8")
        for path in (str(self.root / "missing-lookup.json"), str(corrupt), str(incomplete)):
            with self.subTest(lookup=path):
                decisions, stdout, summary = self.decide("--lookup", path)
                self.assertEqual({"true"}, {decisions[name] for name in CHECKS if name != "reuse"}, stdout)
                self.assertEqual("true", decisions["workflows"])
                self.assertEqual("false", decisions["reuse"], "the fixture has no REUSE.toml")
                self.assertIn("could not be read", summary)

    def test_a_behind_lookup_with_an_unreadable_side_selects_every_check(self) -> None:
        head = self.commit({"docs/notes.md": "n\n"})
        decisions, _, _ = self.decide("--lookup", self.lookup(case="behind", head=head, landing_base=self.base, tested_base="0" * 40))
        self.assertEqual({"true"}, {decisions[name] for name in CHECKS if name != "reuse"})
        self.assertEqual("true", decisions["workflows"])


class HygieneFallbackTests(HygieneFixture):
    """Every hygiene fallback to running more emits a counted notice by reason, and ``changes --fallbacks`` writes the
    counts that ``plan.py plan --hygiene-fallbacks`` adds to the plan's fallbacks."""

    def fallbacks(self, *arguments: str) -> tuple[dict[str, int], str]:
        path = self.root / "hygiene-fallbacks.json"
        _, stdout, _ = self.decide("--fallbacks", str(path), *arguments)
        return json.loads(path.read_text(encoding="utf-8")), stdout

    def test_an_unreadable_push_lookup_counts_the_hygiene_and_the_gitleaks_fallback(self) -> None:
        counts, stdout = self.fallbacks("--lookup", str(self.root / "missing-lookup.json"))
        self.assertEqual({"gitleaks-unreadable-push-lookup": 1, "hygiene-unreadable-push-lookup": 1}, counts)
        self.assertIn("::notice title=ci-fallback::hygiene-unreadable-push-lookup: every hygiene check runs: the push lookup could not be read", stdout)

    def test_unknown_changed_files_count_the_hygiene_and_the_gitleaks_fallback(self) -> None:
        head = self.commit({"docs/notes.md": "n\n"})
        counts, stdout = self.fallbacks("--base", "0" * 40, "--head", head)
        self.assertEqual({"gitleaks-range-unknown": 1, "hygiene-changed-files-unknown": 1}, counts)
        self.assertIn("::notice title=ci-fallback::hygiene-changed-files-unknown: every hygiene check runs: the changed files could not be determined", stdout)

    def test_a_readable_range_counts_no_fallback(self) -> None:
        head = self.commit({"docs/notes.md": "n\n"})
        for arguments in (("--base", self.base, "--head", head), ("--lookup", self.lookup(case="own", head=head, landing_base=self.base))):
            with self.subTest(arguments=arguments[0]):
                counts, stdout = self.fallbacks(*arguments)
                self.assertEqual({}, counts)
                self.assertNotIn("title=ci-fallback", stdout)


class EditedReuseTests(HygieneFixture):
    def record(self, tree: str, checks: dict[str, int], reuse_version: str | None = None) -> str:
        path = self.root / "record.json"
        payload: dict[str, object] = {"tree": tree, "checks": checks}
        if reuse_version is not None:
            payload["reuse_version"] = reuse_version
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def pins(self, reuse_version: str) -> str:
        path = self.root / "pins.json"
        path.write_text(json.dumps({"python": {"reuse": {"version": reuse_version}}}), encoding="utf-8")
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
                         "--outcome", "markdown=skipped", "--outcome", "reuse=success", "--reused", json.dumps({"packages": 111}),
                         "--pins", self.pins("6.3.0"), "--output", str(output))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual({"tree": git(self.repo, "rev-parse", f"{head}^{{tree}}"), "checks": {"actionlint": 444, "packages": 111, "reuse": 444},
                          "reuse_version": "6.3.0"},
                         json.loads(output.read_text(encoding="utf-8")))

    def test_an_edited_run_does_not_reuse_a_reuse_pass_from_another_pinned_reuse_version(self) -> None:
        base = self.commit({"REUSE.toml": "version = 1\n", ".github/workflows/ci.yml": "x: 1\n"})
        head = self.commit({".github/workflows/ci.yml": "x: 2\n", "src/new.py": "print(2)\n"})
        tree = git(self.repo, "rev-parse", "HEAD^{tree}")
        pins = self.pins("6.3.0")
        for recorded_version in ("6.2.0", None):
            with self.subTest(recorded_version=recorded_version):
                rec = self.record(tree, {"actionlint": 111, "reuse": 111}, reuse_version=recorded_version)
                decisions, _, _ = self.decide("--base", base, "--head", head, "--results", rec, "--pins", pins)
                self.assertEqual("false", decisions["actionlint"])
                self.assertEqual("true", decisions["reuse"])
                self.assertEqual({"actionlint": 111}, json.loads(decisions["reused"]))
        same = self.record(tree, {"actionlint": 111, "reuse": 111}, reuse_version="6.3.0")
        decisions, _, _ = self.decide("--base", base, "--head", head, "--results", same, "--pins", pins)
        self.assertEqual("false", decisions["reuse"])
        self.assertEqual({"actionlint": 111, "reuse": 111}, json.loads(decisions["reused"]))


class HygieneSharedDigestTests(HygieneFixture):
    """Hygiene check selection through shared CI digests: compared with the previous default-branch record on pull
    requests, with the tested digests on identical and behind pushes (plus the previous default-branch record on behind
    for checks that the newer base selects), zizmor-policy only without a consumer zizmor config, missing-on-either-side
    counts as moved, and unreadable digests fail closed with counted fallbacks."""

    DIGESTS = {
        "hygiene:actionlint": "a" * 64,
        "hygiene:zizmor": "b" * 64,
        "hygiene:zizmor-policy": "c" * 64,
        "hygiene:packages": "d" * 64,
        "hygiene:markdown": "e" * 64,
        "hygiene:reuse": "f" * 64,
    }

    def write_shared(self, digests: dict[str, str] | None, previous: dict[str, str] | None) -> str:
        path = self.root / "shared-lookup.json"
        path.write_text(json.dumps({"digests": digests, "previous": previous}), encoding="utf-8")
        return str(path)

    def test_a_moved_hygiene_digest_on_a_pull_request_runs_only_its_check(self) -> None:
        head = self.commit({"src/app.py": "print(2)\n"})
        for key, expected_check in (
            ("hygiene:actionlint", "actionlint"),
            ("hygiene:zizmor", "zizmor"),
            ("hygiene:packages", "packages"),
            ("hygiene:markdown", "markdown"),
        ):
            with self.subTest(moved=key):
                moved = {**self.DIGESTS, key: "1" * 64}
                decisions, stdout, _ = self.decide("--base", self.base, "--head", head, "--shared-lookup", self.write_shared(moved, self.DIGESTS))
                expected = {name: ("true" if name == expected_check else "false") for name in CHECKS}
                self.assertEqual(expected, {name: decisions[name] for name in CHECKS}, stdout)
                self.assertEqual("true" if expected_check in ("actionlint", "zizmor") else "false", decisions["workflows"])
                self.assertIn(f"`{key}` digest moved", stdout)

        # hygiene:reuse stays false without REUSE.toml and runs with REUSE.toml.
        moved_reuse = {**self.DIGESTS, "hygiene:reuse": "1" * 64}
        decisions, _, _ = self.decide("--base", self.base, "--head", head, "--shared-lookup", self.write_shared(moved_reuse, self.DIGESTS))
        self.assertEqual("false", decisions["reuse"])
        base_licensed = self.commit({"REUSE.toml": "version = 1\n"})
        head_licensed = self.commit({"src/app.py": "print(3)\n"})
        decisions, stdout, _ = self.decide("--base", base_licensed, "--head", head_licensed, "--shared-lookup", self.write_shared(moved_reuse, self.DIGESTS))
        self.assertEqual("true", decisions["reuse"])
        self.assertIn("`hygiene:reuse` digest moved", stdout)

    def test_zizmor_policy_digest_applies_only_when_the_repository_has_no_own_zizmor_config(self) -> None:
        head = self.commit({"src/app.py": "print(2)\n"})
        moved_policy = {**self.DIGESTS, "hygiene:zizmor-policy": "1" * 64}
        decisions, stdout, _ = self.decide("--base", self.base, "--head", head, "--shared-lookup", self.write_shared(moved_policy, self.DIGESTS))
        self.assertEqual("true", decisions["zizmor"], stdout)
        self.assertEqual("true", decisions["workflows"])
        self.assertEqual("false", decisions["actionlint"])

        base_with_cfg = self.commit({".github/zizmor.yml": "rules: {}\n"})
        head_with_cfg = self.commit({"src/app.py": "print(3)\n"})
        decisions, stdout, _ = self.decide("--base", base_with_cfg, "--head", head_with_cfg, "--shared-lookup", self.write_shared(moved_policy, self.DIGESTS))
        self.assertEqual("false", decisions["zizmor"], stdout)
        self.assertEqual("false", decisions["workflows"])

    def test_identical_and_behind_pushes_compare_hygiene_digests_with_tested_digests(self) -> None:
        moved_actionlint = {**self.DIGESTS, "hygiene:actionlint": "1" * 64}
        head = self.commit({".github/workflows/ci.yml": "x: 1\n", "docs/notes.md": "n\n"})

        # identical: current digests equal tested digests and differ from the previous default-branch record -> runs no check.
        decisions, _, _ = self.decide(
            "--lookup", self.lookup(case="identical", head=head, landing_base=self.base, pull_request=7, tested_digests=moved_actionlint),
            "--shared-lookup", self.write_shared(moved_actionlint, self.DIGESTS),
        )
        self.assertEqual({name: "false" for name in CHECKS}, {name: decisions[name] for name in CHECKS})
        self.assertEqual("false", decisions["workflows"])

        # identical: current digests differ from tested digests (e.g. a v1 rollback back to the previous default-branch
        # record) -> runs only the moved check (actionlint and workflows).
        decisions, stdout, _ = self.decide(
            "--lookup", self.lookup(case="identical", head=head, landing_base=self.base, pull_request=7, tested_digests=moved_actionlint),
            "--shared-lookup", self.write_shared(self.DIGESTS, self.DIGESTS),
        )
        self.assertEqual({"actionlint": "true", "zizmor": "false", "packages": "false", "markdown": "false", "reuse": "false"},
                         {name: decisions[name] for name in CHECKS}, stdout)
        self.assertEqual("true", decisions["workflows"])

        # identical: unreadable tested_digests -> runs every applicable check and counts hygiene-unreadable-tested-digests.
        fallbacks_file = self.root / "hygiene-fallbacks.json"
        decisions, stdout, _ = self.decide(
            "--lookup", self.lookup(case="identical", head=head, landing_base=self.base, pull_request=7, tested_digests=None),
            "--shared-lookup", self.write_shared(self.DIGESTS, self.DIGESTS),
            "--fallbacks", str(fallbacks_file),
        )
        self.assertEqual({"true"}, {decisions[name] for name in CHECKS if name != "reuse"})
        self.assertEqual("true", decisions["workflows"])
        self.assertEqual({"hygiene-unreadable-tested-digests": 1}, json.loads(fallbacks_file.read_text(encoding="utf-8")))
        self.assertIn("::notice title=ci-fallback::hygiene-unreadable-tested-digests:", stdout)

        # behind: when neither side touches a check, current digests equal to tested digests skip it even if the
        # previous default-branch record differs, while current digests differing from tested digests run the moved check.
        tested_base = head
        landing = self.commit({"src/a.py": "1\n"})
        merged = self.commit({"src/b.py": "2\n"})
        decisions, _, _ = self.decide(
            "--lookup", self.lookup(case="behind", head=merged, landing_base=landing, tested_base=tested_base,
                                    pull_request=7, landing_base_passed=True, tested_digests=moved_actionlint),
            "--shared-lookup", self.write_shared(moved_actionlint, self.DIGESTS),
        )
        self.assertEqual({name: "false" for name in CHECKS}, {name: decisions[name] for name in CHECKS})
        decisions, _, _ = self.decide(
            "--lookup", self.lookup(case="behind", head=merged, landing_base=landing, tested_base=tested_base,
                                    pull_request=7, landing_base_passed=True, tested_digests=moved_actionlint),
            "--shared-lookup", self.write_shared(self.DIGESTS, self.DIGESTS),
        )
        self.assertEqual({"actionlint": "true", "zizmor": "false", "packages": "false", "markdown": "false", "reuse": "false"},
                         {name: decisions[name] for name in CHECKS})

    def test_behind_push_compares_newer_base_selected_hygiene_checks_with_previous_default_branch_digests(self) -> None:
        # Landing base passed under older digests (previous default-branch record); v1 moved before the PR ran, so the
        # merge lands behind with current digests equal to tested digests but differing from the previous record.
        tested_base = self.commit({"REUSE.toml": "version = 1\n", "docs/guide.md": "v1\n", "pyproject.toml": "[project]\nname = 'app'\n"})
        # Default branch's side adds a file (selects reuse) and edits Markdown (selects markdown).
        landing = self.commit({"src/from_main.py": "1\n", "docs/guide.md": "v2\n"})
        # Pull request's side edits an existing package manifest (selects only packages, not reuse or markdown).
        merged = self.commit({"pyproject.toml": "[project]\nname = 'app'\nversion = '1.0'\n"})

        moved_reuse_and_packages = {**self.DIGESTS, "hygiene:reuse": "1" * 64, "hygiene:packages": "2" * 64}
        decisions, stdout, _ = self.decide(
            "--lookup", self.lookup(case="behind", head=merged, landing_base=landing, tested_base=tested_base,
                                    pull_request=9, landing_base_passed=True, tested_digests=moved_reuse_and_packages),
            "--shared-lookup", self.write_shared(moved_reuse_and_packages, self.DIGESTS),
        )
        # reuse: selected by main's side and its digest differs from the previous default-branch record -> must run.
        # markdown: selected by main's side, but its digest equals both tested and previous -> stays skipped.
        # packages: selected only by the PR's side and its digest equals tested -> stays skipped even though previous differs.
        self.assertEqual({"actionlint": "false", "zizmor": "false", "packages": "false", "markdown": "false", "reuse": "true"},
                         {name: decisions[name] for name in CHECKS}, stdout)
        self.assertEqual("false", decisions["workflows"])
        self.assertIn("`hygiene:reuse` digest moved", stdout)

        # Unreadable previous default-branch digests on behind: only checks selected by the newer base fail closed and
        # count hygiene-unreadable-shared-digests; checks selected only by the PR or neither side stay skipped.
        fallbacks_file = self.root / "behind-fallbacks.json"
        decisions, stdout, _ = self.decide(
            "--lookup", self.lookup(case="behind", head=merged, landing_base=landing, tested_base=tested_base,
                                    pull_request=9, landing_base_passed=True, tested_digests=moved_reuse_and_packages),
            "--shared-lookup", self.write_shared(moved_reuse_and_packages, None),
            "--fallbacks", str(fallbacks_file),
        )
        self.assertEqual({"actionlint": "false", "zizmor": "false", "packages": "false", "markdown": "true", "reuse": "true"},
                         {name: decisions[name] for name in CHECKS}, stdout)
        self.assertEqual({"hygiene-unreadable-shared-digests": 1}, json.loads(fallbacks_file.read_text(encoding="utf-8")))
        self.assertIn("::notice title=ci-fallback::hygiene-unreadable-shared-digests:", stdout)

        # Unreadable previous digests when the newer base selects no hygiene check: nothing runs and no fallback counts.
        clean_base = self.commit({"src/keep.py": "1\n"})
        clean_landing = self.commit({"src/keep.py": "2\n"})
        clean_merged = self.commit({"pyproject.toml": "[project]\nname = 'app2'\n"})
        no_fb_file = self.root / "behind-no-fallbacks.json"
        decisions, stdout, _ = self.decide(
            "--lookup", self.lookup(case="behind", head=clean_merged, landing_base=clean_landing, tested_base=clean_base,
                                    pull_request=10, landing_base_passed=True, tested_digests=moved_reuse_and_packages),
            "--shared-lookup", self.write_shared(moved_reuse_and_packages, None),
            "--fallbacks", str(no_fb_file),
        )
        self.assertEqual({name: "false" for name in CHECKS}, {name: decisions[name] for name in CHECKS}, stdout)
        self.assertEqual({}, json.loads(no_fb_file.read_text(encoding="utf-8")))

    def test_missing_digest_on_either_side_and_unreadable_shared_lookup_fail_closed(self) -> None:
        head = self.commit({"src/app.py": "print(2)\n"})
        without_links = {k: v for k, v in self.DIGESTS.items() if k != "hygiene:markdown"}
        for label, current, previous in (("missing from current", without_links, self.DIGESTS), ("missing from previous", self.DIGESTS, without_links)):
            with self.subTest(case=label):
                decisions, _, _ = self.decide("--base", self.base, "--head", head, "--shared-lookup", self.write_shared(current, previous))
                self.assertEqual("true", decisions["markdown"])
                self.assertEqual("false", decisions["packages"])

        fallbacks_file = self.root / "hygiene-fallbacks.json"
        decisions, stdout, _ = self.decide("--base", self.base, "--head", head, "--shared-lookup", self.write_shared(self.DIGESTS, None),
                                           "--fallbacks", str(fallbacks_file))
        self.assertEqual({"true"}, {decisions[name] for name in CHECKS if name != "reuse"})
        self.assertEqual({"hygiene-unreadable-shared-digests": 1}, json.loads(fallbacks_file.read_text(encoding="utf-8")))
        self.assertIn("::notice title=ci-fallback::hygiene-unreadable-shared-digests:", stdout)


if __name__ == "__main__":
    unittest.main()
