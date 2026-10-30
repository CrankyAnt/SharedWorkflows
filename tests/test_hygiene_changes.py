from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests.hygiene_fixtures import fixture_repository, git
from tests.hygiene_process import hygiene


class ChangeDecisionTests(unittest.TestCase):
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

    def decide(self, base: str, head: str, *arguments: str, all_checks: bool = False) -> tuple[dict[str, str], str, str]:
        output, summary = self.root / "output.txt", self.root / "summary.md"
        output.write_text("", encoding="utf-8")
        summary.write_text("", encoding="utf-8")
        result = hygiene("changes", self.repo, "--base", base, "--head", head, "--compare", "direct", "--github-output", str(output),
                         "--summary", str(summary), *arguments)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        decisions = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
        if not all_checks:
            decisions = {name: decisions[name] for name in ("workflows", "packages", "markdown")}
        return decisions, result.stdout, summary.read_text(encoding="utf-8")

    def test_a_workflow_action_or_zizmor_change_runs_actionlint_and_zizmor(self) -> None:
        base = self.base
        for relative in (".github/workflows/ci.yml", "tooling/setup/action.yml", ".github/zizmor.yml"):
            with self.subTest(changed=relative):
                head = self.commit({relative: "x: 1\n"})
                decisions, stdout, summary = self.decide(base, head)
                base = head
                self.assertEqual(decisions, {"workflows": "true", "packages": "false", "markdown": "false"})
                self.assertIn(f"`{relative}` is a workflow, an action definition or the zizmor configuration", stdout)
                self.assertIn("| actionlint, zizmor | yes |", summary)
                self.assertIn("| Relative links | no |", summary)
                self.assertIn("| Gitleaks | yes | every event |", summary)

    def test_a_dependabot_merged_dispatch_decides_as_a_push_from_the_merge_s_first_parent(self) -> None:
        git(self.repo, "switch", "-q", "-c", "dependabot")
        self.commit({"web/package.json": "{}\n"})
        git(self.repo, "switch", "-q", "-")
        parent = self.commit({"docs/notes.md": "main moved on\n"})
        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge", "dependabot")
        merged = git(self.repo, "rev-parse", "HEAD")
        pushed = self.decide(parent, merged, all_checks=True)
        dispatched = self.decide(f"{merged}^1", merged, all_checks=True)
        self.assertEqual(pushed[0], dispatched[0])
        self.assertEqual({"workflows": "false", "packages": "true", "markdown": "false"}, {name: dispatched[0][name] for name in ("workflows", "packages", "markdown")})
        every_check, _, _ = self.decide("", merged, all_checks=True)
        self.assertEqual({"true"}, {value for name, value in every_check.items() if name != "reuse"}, "without a SHA the base is empty and every check runs")
        self.assertEqual("false", every_check["reuse"], "the fixture has no REUSE.toml")

    def test_a_package_file_or_dependabot_change_runs_the_coverage_check(self) -> None:
        base = self.base
        for relative in ("service/requirements.txt", "web/package.json", ".github/dependabot.yml"):
            with self.subTest(changed=relative):
                head = self.commit({relative: "x\n"})
                decisions, stdout, _ = self.decide(base, head)
                base = head
                self.assertEqual(decisions, {"workflows": "false", "packages": "true", "markdown": "false"})
                self.assertIn(f"`{relative}` is a package file or the Dependabot configuration", stdout)

    def test_a_markdown_change_or_a_removed_file_runs_the_link_check_and_a_code_change_does_not(self) -> None:
        head = self.commit({"docs/notes.md": "n\n"})
        decisions, stdout, summary = self.decide(self.base, head)
        self.assertEqual(decisions, {"workflows": "false", "packages": "false", "markdown": "true"})
        self.assertIn("`docs/notes.md` is a Markdown file", stdout)
        self.assertIn("| Relative links | yes | `docs/notes.md` is a Markdown file |", summary)
        (self.repo / "src/app.py").unlink()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "chore: remove a file")
        removed = git(self.repo, "rev-parse", "HEAD")
        decisions, stdout, _ = self.decide(head, removed)
        self.assertEqual(decisions["markdown"], "true", "a link may point to the removed file")
        self.assertIn("`src/app.py` was removed, which a link may point to", stdout)
        changed = self.commit({"src/app.py": "print(3)\n"})
        decisions, stdout, _ = self.decide(removed, changed)
        self.assertEqual(decisions, {"workflows": "false", "packages": "false", "markdown": "false"})
        self.assertIn("no changed file is a Markdown file and none was removed", stdout)

    def test_a_change_to_a_check_s_own_script_runs_that_check_and_the_action_file_runs_every_check(self) -> None:
        """In a repository that holds the hygiene folder, each check depends on its own scripts: the link checker runs
        the link check alone, the coverage module the coverage check alone, the installer and the zizmor policy the
        workflow checks alone, and hygiene.py, which drives them all, every check; the action file is the definition.
        The fixture repository holds the owning repository's folder (stand-ins for its files), so the script decides for a
        checkout that keeps it, as this repository's own hygiene run does."""
        folder = "actions/repository-hygiene"
        base = self.commit({f"{folder}/{name}": f"# {name}\n" for name in ("hygiene.py", "link_check.py", "dependabot_coverage.py", "zizmor-policy.yml", "install_tools.py", "pins.py", "pins.schema.json", "README.md", "action.yml")})
        for relative, expected, reason in (
            (f"{folder}/link_check.py", {"workflows": "false", "packages": "false", "markdown": "true"}, "is a script of the link check"),
            (f"{folder}/dependabot_coverage.py", {"workflows": "false", "packages": "true", "markdown": "false"}, "is a script or an input of the Dependabot coverage check"),
            (f"{folder}/zizmor-policy.yml", {"workflows": "true", "packages": "false", "markdown": "false"}, "is a script or an input of actionlint and zizmor"),
            (f"{folder}/install_tools.py", {"workflows": "true", "packages": "false", "markdown": "false"}, "is a script or an input of actionlint and zizmor"),
            (f"{folder}/pins.py", {"workflows": "true", "packages": "false", "markdown": "false"}, "is a script or an input of actionlint and zizmor"),
            (f"{folder}/pins.schema.json", {"workflows": "false", "packages": "false", "markdown": "false"}, "no changed file is a workflow, an action definition or the zizmor configuration"),
            (f"{folder}/hygiene.py", {"workflows": "true", "packages": "true", "markdown": "true"}, "is a script"),
            (f"{folder}/README.md", {"workflows": "false", "packages": "false", "markdown": "true"}, "is a Markdown file"),
            (f"{folder}/action.yml", {"workflows": "true", "packages": "true", "markdown": "true"}, "belongs to the CI definition, so every check runs"),
        ):
            with self.subTest(changed=relative):
                head = self.commit({relative: f"# {relative} changed\n"})
                decisions, stdout, _ = self.decide(base, head, "--definition", "actions/repository-hygiene/action.yml")
                base = head
                self.assertEqual(decisions, expected, stdout)
                self.assertIn(reason, stdout)

    def test_a_caller_link_exclude_definition_runs_only_the_link_check(self) -> None:
        caller = ".github/workflows/repository-hygiene-caller.yml"
        base = self.commit({caller: "name: caller\njobs:\n  hygiene:\n    with:\n      link-exclude: old-docs/**\n"})
        head = self.commit({caller: "name: caller\njobs:\n  hygiene:\n    with:\n      link-exclude: vendor-docs/**\n"})
        decisions, stdout, _ = self.decide(base, head, "--markdown-definition", f"{caller}#link-exclude")
        self.assertEqual(decisions, {"workflows": "true", "packages": "false", "markdown": "true"})
        self.assertIn(f"`{caller}` is a check definition that can change link exclusions or execution", stdout)

    def test_each_caller_input_selects_its_check_without_selecting_the_other_one(self) -> None:
        workflow = ".github/workflows/repository-hygiene.yml"
        initial = """name: Repository hygiene
on:
  workflow_call:
    inputs:
      zizmor-persona:
        default: regular
      coverage-exclude:
        default: old-fixture/**
      link-exclude:
        default: old-docs/**
jobs: {}
"""
        base = self.commit({workflow: initial})
        definitions = ("--packages-definition", f"{workflow}#coverage-exclude", "--markdown-definition", f"{workflow}#link-exclude")
        for before, after, expected, reason in (
            ("old-fixture/**", "new-fixture/**", {"workflows": "true", "packages": "true", "markdown": "false"}, "can change coverage exclusions"),
            ("old-docs/**", "new-docs/**", {"workflows": "true", "packages": "false", "markdown": "true"}, "can change link exclusions"),
            ("default: regular", "default: pedantic", {"workflows": "true", "packages": "false", "markdown": "false"}, "is a workflow, an action definition or the zizmor configuration"),
        ):
            with self.subTest(change=after):
                head = self.commit({workflow: initial.replace(before, after)})
                decisions, stdout, _ = self.decide(base, head, *definitions)
                self.assertEqual(decisions, expected, stdout)
                self.assertIn(reason, stdout)
                base, initial = head, initial.replace(before, after)

    def test_a_comment_only_workflow_change_selects_no_check(self) -> None:
        workflow = ".github/workflows/repository-hygiene.yml"
        base = self.commit({workflow: "name: hygiene\n# old explanation\njobs: {}\n"})
        head = self.commit({workflow: "name: hygiene\n# revised explanation\njobs: {}\n"})
        decisions, stdout, _ = self.decide(base, head)
        self.assertEqual(decisions, {"workflows": "false", "packages": "false", "markdown": "false"})
        self.assertIn("no changed file is a workflow", stdout)

    def test_a_comment_in_a_precise_definition_block_selects_no_check(self) -> None:
        workflow = ".github/workflows/repository-hygiene.yml"
        base = self.commit({workflow: "name: hygiene\njobs:\n  hygiene:\n    with:\n      link-exclude: |\n        # old explanation\n        vendor-docs/**\n"})
        head = self.commit({workflow: "name: hygiene\njobs:\n  hygiene:\n    with:\n      link-exclude: |\n        # revised explanation\n        vendor-docs/**\n"})
        decisions, _, _ = self.decide(base, head, "--markdown-definition", f"{workflow}#link-exclude")
        self.assertEqual(decisions, {"workflows": "false", "packages": "false", "markdown": "false"})

    def test_a_malformed_precise_definition_fails_with_its_option_name(self) -> None:
        head = self.commit({"docs/notes.md": "changed\n"})
        result = hygiene("changes", self.repo, "--base", self.base, "--head", head, "--packages-definition", ".github/workflows/repository-hygiene.yml")
        self.assertNotEqual(0, result.returncode)
        self.assertIn("--packages-definition must be PATH#INPUT", result.stderr)

    def test_step_definitions_select_only_the_check_whose_execution_changed(self) -> None:
        workflow = ".github/workflows/repository-hygiene.yml"
        initial = """name: hygiene
jobs:
  hygiene:
    steps:
      - name: Coverage
        id: coverage
        if: inputs.coverage
        env:
          COVERAGE_EXCLUDE: old-packages/**
        run: python hygiene.py coverage
      - name: Links
        id: links
        if: inputs.links
        env:
          LINK_EXCLUDE: old-docs/**
        run: python hygiene.py links
"""
        definitions = ("--packages-definition", f"{workflow}#step:coverage",
                       "--markdown-definition", f"{workflow}#step:links")
        base = self.commit({workflow: initial})
        for before, after, selected in (
            ("python hygiene.py coverage", "python hygiene.py coverage --exclude fixture", "packages"),
            ("if: inputs.coverage", "if: always()", "packages"),
            ("old-packages/**", "new-packages/**", "packages"),
            ("python hygiene.py links", "python hygiene.py links --exclude fixture", "markdown"),
            ("if: inputs.links", "if: always()", "markdown"),
            ("old-docs/**", "new-docs/**", "markdown"),
        ):
            with self.subTest(change=before):
                updated = initial.replace(before, after)
                head = self.commit({workflow: updated})
                decisions, stdout, _ = self.decide(base, head, *definitions)
                expected = {"workflows": "true", "packages": "false", "markdown": "false"}
                expected[selected] = "true"
                self.assertEqual(expected, decisions, stdout)
                base, initial = head, updated

    def test_a_same_named_reusable_caller_tracks_the_referenced_workflow(self) -> None:
        workflow = ".github/workflows/repository-hygiene.yml"
        initial = f"name: hygiene\njobs:\n  hygiene:\n    uses: owner/tooling/{workflow}@old\n    with:\n      coverage-exclude: vendor/**\n"
        definitions = ("--packages-definition", f"{workflow}#step:coverage",
                       "--packages-definition", f"{workflow}#coverage-exclude",
                       "--markdown-definition", f"{workflow}#step:links")
        base = self.commit({workflow: initial})
        for updated, expected in (
            (initial.replace("@old", "@new"), {"workflows": "true", "packages": "true", "markdown": "true"}),
            (initial + "# explanation\n", {"workflows": "false", "packages": "false", "markdown": "false"}),
            (initial.replace("vendor/**", "generated/**"), {"workflows": "true", "packages": "true", "markdown": "false"}),
        ):
            with self.subTest(updated=updated):
                head = self.commit({workflow: updated})
                decisions, _, _ = self.decide(base, head, *definitions)
                self.assertEqual(decisions, expected)

        mixed = initial + '  local:\n    runs-on: ubuntu-latest\n    "steps":\n      - run: echo local\n'
        mixed_base = self.commit({workflow: mixed})
        mixed_head = self.commit({workflow: mixed.replace("echo local", "echo changed")})
        result = hygiene("changes", self.repo, "--base", mixed_base, "--head", mixed_head,
                         "--packages-definition", f"{workflow}#step:typo")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("selector 'step:typo' was not found", result.stderr)

    def test_exact_action_output_definition_ignores_unused_output_and_selects_resource_path(self) -> None:
        action = "actions/repository-hygiene/action.yml"
        initial = """name: resources
outputs:
  path:
    value: ${{ steps.resources.outputs.path }}
  node-version:
    value: ${{ steps.resources.outputs.node-version }}
runs:
  using: composite
  steps: []
"""
        base = self.commit({action: initial})
        arguments = ("--definition", f"{action}#key:outputs.path.value")
        updated = initial.replace("outputs.node-version", "outputs.unused-node")
        head = self.commit({action: updated})
        decisions, stdout, _ = self.decide(base, head, *arguments)
        self.assertEqual({"workflows": "true", "packages": "false", "markdown": "false"}, decisions, stdout)
        changed_path = self.commit({action: updated.replace("outputs.path", "outputs.changed-path")})
        decisions, stdout, _ = self.decide(head, changed_path, *arguments)
        self.assertEqual({name: "true" for name in ("workflows", "packages", "markdown")}, decisions, stdout)

    def test_step_comments_are_cosmetic_but_linter_directives_are_inputs(self) -> None:
        workflow = ".github/workflows/repository-hygiene.yml"
        initial = "jobs:\n  hygiene:\n    steps:\n      - id: links\n        run: |\n          # old explanation\n          python hygiene.py links\n"
        base = self.commit({workflow: initial})
        head = self.commit({workflow: initial.replace("old explanation", "new explanation")})
        decisions, stdout, _ = self.decide(base, head, "--markdown-definition", f"{workflow}#step:links")
        self.assertEqual({name: "false" for name in ("workflows", "packages", "markdown")}, decisions, stdout)
        directive = self.commit({workflow: initial.replace("old explanation", "shellcheck disable=SC2086")})
        decisions, stdout, _ = self.decide(head, directive)
        self.assertEqual({"workflows": "true", "packages": "false", "markdown": "false"}, decisions, stdout)

    def test_inline_comments_do_not_select_a_step_but_directives_still_select_linters(self) -> None:
        workflow = ".github/workflows/hygiene.yml"
        initial = "jobs:\n  hygiene:\n    steps:\n      - id: links\n        run: python hygiene.py links # old explanation\n"
        base = self.commit({workflow: initial})
        head = self.commit({workflow: initial.replace("old explanation", "new explanation")})
        decisions, stdout, _ = self.decide(base, head, "--markdown-definition", f"{workflow}#step:links")
        self.assertEqual({name: "false" for name in ("workflows", "packages", "markdown")}, decisions, stdout)
        directive = self.commit({workflow: initial.replace("old explanation", "zizmor: ignore[template-injection]")})
        decisions, stdout, _ = self.decide(head, directive, "--markdown-definition", f"{workflow}#step:links")
        self.assertEqual({"workflows": "true", "packages": "false", "markdown": "false"}, decisions, stdout)

    def test_unknown_precise_selector_fails_without_enabling_unrelated_checks(self) -> None:
        workflow = ".github/workflows/repository-hygiene.yml"
        base = self.commit({workflow: "jobs:\n  hygiene:\n    steps:\n      - id: links\n        run: echo old\n"})
        head = self.commit({workflow: "jobs:\n  hygiene:\n    steps:\n      - id: links\n        run: echo new\n"})
        for selector in ("step:missing", "key:jobs.missing", "unknown:links"):
            with self.subTest(selector=selector):
                result = hygiene("changes", self.repo, "--base", base, "--head", head,
                                 "--markdown-definition", f"{workflow}#{selector}")
                self.assertNotEqual(0, result.returncode, result.stdout)
                self.assertIn("--markdown-definition", result.stderr)
                self.assertNotIn("packages=true", result.stdout)

    def test_linter_outputs_select_only_tools_that_read_the_changed_input(self) -> None:
        base = self.base
        for path, actionlint, zizmor in (
            (".github/zizmor.yml", "false", "true"),
            ("tools/action/action.yml", "false", "true"),
            (".github/actionlint.yaml", "true", "false"),
            (".github/workflows/check.yml", "true", "true"),
        ):
            with self.subTest(path=path):
                head = self.commit({path: "name: changed\n"})
                decisions, stdout, _ = self.decide(base, head, all_checks=True)
                self.assertEqual({"workflows": "true", "packages": "false", "markdown": "false",
                                  "actionlint": actionlint, "zizmor": zizmor, "reuse": "false"}, decisions, stdout)
                base = head

    def test_shared_zizmor_policy_selects_zizmor_only_when_the_caller_uses_it(self) -> None:
        path = "actions/repository-hygiene/zizmor-policy.yml"
        base = self.commit({path: "rules: {}\n"})
        head = self.commit({path: "rules: {unpinned-uses: {disable: true}}\n"})
        decisions, stdout, _ = self.decide(base, head, all_checks=True)
        self.assertEqual({"workflows": "true", "packages": "false", "markdown": "false",
                          "actionlint": "false", "zizmor": "true", "reuse": "false"}, decisions, stdout)
        own = self.commit({".github/zizmor.yml": "rules: {}\n"})
        changed = self.commit({path: "rules: {}\n"})
        decisions, stdout, _ = self.decide(own, changed, all_checks=True)
        self.assertEqual({name: "false" for name in ("workflows", "packages", "markdown", "actionlint", "zizmor", "reuse")}, decisions, stdout)

    def test_composite_resource_step_changes_select_all_but_node_step_changes_do_not(self) -> None:
        action = "actions/repository-hygiene/action.yml"
        initial = """outputs:
  path:
    value: ${{ steps.resources.outputs.path }}
  node-version:
    value: ${{ steps.node-version.outputs.value }}
runs:
  using: composite
  steps:
    - id: resources
      shell: bash
      env:
        RESOURCE_PATH: ${{ github.action_path }}
      run: printf 'path=%s\\n' "$RESOURCE_PATH" >> "$GITHUB_OUTPUT"
    - id: node-version
      shell: bash
      run: jq '.node.version' pins.json
"""
        base = self.commit({action: initial})
        arguments = ("--definition", f"{action}#key:outputs.path.value", "--definition", f"{action}#step:resources",
                     "--definition", f"{action}#key:runs.using")
        for before, after, all_selected in (
            ("jq '.node.version'", "jq -r '.node.version'", False),
            ("github.action_path", "github.workspace", True),
            ("printf 'path=%s", "printf 'path=other/%s", True),
            ("using: composite", "using: node24", True),
        ):
            with self.subTest(change=before):
                updated = initial.replace(before, after)
                head = self.commit({action: updated})
                decisions, stdout, _ = self.decide(base, head, *arguments)
                self.assertEqual({"workflows": "true", "packages": str(all_selected).lower(),
                                  "markdown": str(all_selected).lower()}, decisions, stdout)
                base, initial = head, updated

    def test_caller_rename_selects_links_without_local_shared_scripts(self) -> None:
        base = self.commit({"docs/target.txt": "target\n"})
        git(self.repo, "mv", "docs/target.txt", "docs/renamed.txt")
        git(self.repo, "commit", "-q", "-m", "chore: rename target")
        head = git(self.repo, "rev-parse", "HEAD")
        decisions, stdout, _ = self.decide(base, head, "--definition", "actions/repository-hygiene/action.yml#step:resources")
        self.assertEqual({"workflows": "false", "packages": "false", "markdown": "true"}, decisions, stdout)

    def test_actionlint_selects_the_metadata_of_a_referenced_local_action(self) -> None:
        base = self.commit({".github/workflows/check.yml": "jobs:\n  check:\n    steps:\n      - uses: ./tools/action\n",
                            "tools/action/action.yml": "name: before\n"})
        head = self.commit({"tools/action/action.yml": "name: after\n"})
        decisions, stdout, _ = self.decide(base, head, all_checks=True)
        self.assertEqual("true", decisions["actionlint"], stdout)
        self.assertEqual("true", decisions["zizmor"], stdout)

    def test_unknown_changed_files_select_all_individual_checks(self) -> None:
        decisions, stdout, _ = self.decide("0" * 40, self.base, all_checks=True)
        self.assertEqual({**{name: "true" for name in ("workflows", "packages", "markdown", "actionlint", "zizmor")}, "reuse": "false"}, decisions, stdout)

    def test_precise_definitions_use_the_pull_request_merge_base(self) -> None:
        workflow = ".github/workflows/hygiene.yml"
        initial = "jobs:\n  hygiene:\n    steps:\n      - id: coverage\n        run: coverage old\n      - id: links\n        run: links old\n"
        shared = self.commit({workflow: initial})
        git(self.repo, "switch", "-q", "-c", "upstream")
        upstream = self.commit({workflow: initial.replace("links old", "links upstream")})
        git(self.repo, "switch", "-q", "-c", "feature", shared)
        head = self.commit({workflow: initial.replace("coverage old", "coverage feature")})
        decisions, stdout, _ = self.decide(upstream, head, "--compare", "merge-base",
                                          "--packages-definition", f"{workflow}#step:coverage",
                                          "--markdown-definition", f"{workflow}#step:links")
        self.assertEqual({"workflows": "true", "packages": "true", "markdown": "false"}, decisions, stdout)

    def test_an_unrelated_change_runs_neither_and_a_deleted_file_counts_under_its_path(self) -> None:
        head = self.commit({"src/app.py": "print(2)\n", "docs/notes.txt": "n\n"})
        decisions, stdout, summary = self.decide(self.base, head)
        self.assertEqual(decisions, {"workflows": "false", "packages": "false", "markdown": "false"})
        self.assertIn("2 changed files.", summary)
        (self.repo / "src/app.py").unlink()
        (self.repo / "web").mkdir()
        (self.repo / "web/package.json").write_text("{}\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "chore: add a manifest")
        with_manifest = git(self.repo, "rev-parse", "HEAD")
        (self.repo / "web/package.json").unlink()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "chore: remove the manifest")
        removed = git(self.repo, "rev-parse", "HEAD")
        decisions, _, _ = self.decide(with_manifest, removed)
        self.assertEqual(decisions["packages"], "true", "a removed package file still changes the coverage")

    def test_an_unreadable_range_or_a_definition_change_runs_every_check(self) -> None:
        decisions, stdout, _ = self.decide("0" * 40, self.base)
        self.assertEqual(decisions, {"workflows": "true", "packages": "true", "markdown": "true"})
        self.assertIn("the changed files could not be determined", stdout)
        head = self.commit({"ci/hygiene/pins.json": "{}\n"})
        decisions, stdout, _ = self.decide(self.base, head, "--definition", "ci/hygiene/*")
        self.assertEqual(decisions, {"workflows": "true", "packages": "true", "markdown": "true"})
        self.assertIn("`ci/hygiene/pins.json` belongs to the CI definition, so every check runs", stdout)
        decisions, _, _ = self.decide(self.base, head)
        self.assertEqual(decisions, {"workflows": "false", "packages": "false", "markdown": "false"})


class ReuseSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def repository(self, name: str, files: dict[str, str]) -> str:
        self.repo = fixture_repository(self.root / name, files)
        return git(self.repo, "rev-parse", "HEAD")

    def commit(self, files: dict[str, str | None]) -> str:
        for relative, content in files.items():
            path = self.repo / relative
            if content is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "feat: change")
        return git(self.repo, "rev-parse", "HEAD")

    def reuse(self, base: str, head: str, *arguments: str) -> tuple[str, str, str]:
        output, summary = self.root / "output.txt", self.root / "summary.md"
        output.write_text("", encoding="utf-8")
        summary.write_text("", encoding="utf-8")
        result = hygiene("changes", self.repo, "--base", base, "--head", head, "--compare", "direct", "--github-output", str(output),
                         "--summary", str(summary), *arguments)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        decisions = dict(line.split("=", 1) for line in output.read_text(encoding="utf-8").splitlines())
        return decisions["reuse"], result.stdout, summary.read_text(encoding="utf-8")

    def licensed(self) -> str:
        return self.repository("licensed", {"REUSE.toml": "version = 1\n", "LICENSES/MIT.txt": "MIT\n", "src/app.py": "print(1)\n"})

    def test_an_added_renamed_or_removed_file_selects_reuse(self) -> None:
        base = self.licensed()
        added = self.commit({"src/new.py": "print(2)\n"})
        decision, stdout, summary = self.reuse(base, added)
        self.assertEqual("true", decision, stdout)
        self.assertIn("`src/new.py` was added, renamed or removed", stdout)
        self.assertIn("| REUSE | yes |", summary)
        git(self.repo, "mv", "src/new.py", "src/moved.py")
        git(self.repo, "commit", "-q", "-m", "chore: rename")
        renamed = git(self.repo, "rev-parse", "HEAD")
        self.assertEqual("true", self.reuse(added, renamed)[0])
        removed = self.commit({"src/moved.py": None})
        self.assertEqual("true", self.reuse(renamed, removed)[0])

    def test_a_change_to_reuse_toml_or_a_licence_text_selects_reuse(self) -> None:
        base = self.licensed()
        for relative, content in (("REUSE.toml", "version = 1\n# changed\n"), ("LICENSES/MIT.txt", "MIT licence\n")):
            with self.subTest(changed=relative):
                head = self.commit({relative: content})
                decision, stdout, _ = self.reuse(base, head)
                self.assertEqual("true", decision, stdout)
                self.assertIn(f"`{relative}` is the REUSE assignment or a licence text", stdout)
                base = head

    def test_a_content_change_to_an_existing_file_does_not_select_reuse(self) -> None:
        base = self.licensed()
        head = self.commit({"src/app.py": "print(3)\n"})
        decision, stdout, summary = self.reuse(base, head)
        self.assertEqual("false", decision, stdout)
        self.assertIn("no file was added, renamed or removed, and neither REUSE.toml nor LICENSES/ changed", stdout)
        self.assertIn("| REUSE | no |", summary)
        self.assertEqual("true", self.reuse("0" * 40, head)[0], "an unreadable range runs every check")

    def test_without_reuse_toml_no_change_selects_reuse(self) -> None:
        base = self.repository("unlicensed", {"src/app.py": "print(1)\n"})
        added = self.commit({"src/new.py": "print(2)\n", "LICENSES/MIT.txt": "MIT\n"})
        git(self.repo, "mv", "src/new.py", "src/moved.py")
        git(self.repo, "commit", "-q", "-m", "chore: rename")
        renamed = git(self.repo, "rev-parse", "HEAD")
        removed = self.commit({"src/moved.py": None})
        for start, end, arguments in ((base, added, ()), (added, renamed, ()), (renamed, removed, ()), ("0" * 40, removed, ()),
                                      (base, removed, ("--definition", "src/*"))):
            with self.subTest(start=start, end=end, arguments=arguments):
                decision, stdout, _ = self.reuse(start, end, *arguments)
                self.assertEqual("false", decision, stdout)
                self.assertIn("the repository has no REUSE.toml", stdout)


if __name__ == "__main__":
    unittest.main()
