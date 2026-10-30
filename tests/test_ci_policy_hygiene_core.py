"""Text contracts of the shared repository-hygiene workflow."""

from __future__ import annotations

import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, action_pin_pattern, read_workflow


WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "repository-hygiene.yml"


def step_names(workflow: str) -> list[str]:
    """The name of every step, or its ``uses:`` reference when it has none, in order."""
    return [named or uses for named, uses in re.findall(r"(?m)^      - (?:name: (.+)|uses: (\S+).*)$", workflow)]


class HygieneCoreTests(unittest.TestCase):
    def test_the_hygiene_workflow_holds_the_contract(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_call:", workflow)
        for name in ("gitleaks-config", "gitleaks-baseline", "zizmor-persona", "coverage-exclude", "link-exclude"):
            self.assertRegex(workflow, rf"(?m)^      {name}:\n        description: .*\n        type: string\n        required: false\n        default: \"\"$")
        self.assertIn("permissions: {}", workflow)
        self.assertEqual(1, workflow.count("    permissions:\n"))
        _, document = read_workflow(WORKFLOW)
        self.assertEqual("ubuntu-26.04", document["jobs"]["hygiene"]["runs-on"])
        self.assertEqual({"actions": "read", "contents": "read", "pull-requests": "read"}, document["jobs"]["hygiene"]["permissions"])
        self.assertNotIn("write", workflow)
        self.assertNotIn("secrets", workflow)
        self.assertEqual(1, workflow.count("${{ github.token }}"), "only the push lookup reads the API")
        self.assertIn("uses: $/actions/repository-hygiene", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn('install_tools.py" --pins "$RESOURCE_PATH/pins.json" --platform linux-x64', workflow)
        self.assertIn('--persona "$ZIZMOR_PERSONA"', workflow)
        self.assertIn('--config "$GITLEAKS_CONFIG" --baseline "$GITLEAKS_BASELINE"', workflow)
        self.assertIn('printf \'%s\\n\' "$COVERAGE_EXCLUDE" | PYTHONPYCACHEPREFIX="$RUNNER_TEMP/repo-pycache" python3 "$RESOURCE_PATH/hygiene.py" coverage --exclude-from -', workflow)
        self.assertIn('printf \'%s\\n\' "$LINK_EXCLUDE" | PYTHONPYCACHEPREFIX="$RUNNER_TEMP/repo-pycache" python3 "$RESOURCE_PATH/hygiene.py" links --exclude-from - --summary "$GITHUB_STEP_SUMMARY"', workflow)

    def test_pull_request_runs_are_cancelled_for_a_newer_head_and_push_and_dispatch_runs_never(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("    concurrency:\n"
                      "      group: hygiene-${{ github.workflow }}-${{ github.event_name == 'pull_request' && github.event.pull_request.number || github.run_id }}\n"
                      "      cancel-in-progress: ${{ github.event_name == 'pull_request' && github.event.action != 'edited' }}\n", workflow)

    def test_gitleaks_scans_every_reachable_commit_of_the_full_history_on_every_event(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("        with:\n          ref: ${{ github.event_name == 'repository_dispatch' && github.event.client_payload.sha || '' }}\n          fetch-depth: 0\n", workflow)
        self.assertRegex(workflow, r"(?m)^      - name: Gitleaks over all reachable history\n        if: \$\{\{ !cancelled\(\) && steps\.install\.outcome == 'success' \}\}\n(?:        env:\n(?:          .*\n)+)?        run: .*hygiene\.py\" gitleaks ")
        self.assertIn('tools="--tool gitleaks"', workflow)
        hygiene = (REPOSITORY_ROOT / "actions" / "repository-hygiene" / "hygiene.py").read_text(encoding="utf-8")
        self.assertIn('"git", "--log-opts=--all"', hygiene)

    def test_on_a_push_the_push_lookup_runs_first_after_the_checkout_and_the_decision_reads_it(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        names = step_names(workflow)
        checkout = next(index for index, name in enumerate(names) if name.startswith("actions/checkout@"))
        self.assertEqual("Look up the tested tree of the pushed commit's pull request", names[checkout + 1])
        self.assertTrue(all(name.startswith("Load ") for name in names[:checkout]), "only resource loads precede the checkout")
        self.assertIn("        if: github.event_name == 'push' || github.event_name == 'repository_dispatch'\n", workflow)
        self.assertIn("uses: $/actions/suite-selection", workflow)
        selection = (REPOSITORY_ROOT / ".github" / "workflows" / "suite-selection.yml").read_text(encoding="utf-8")
        lookup = next(line for line in workflow.splitlines() if 'suite_selection.py" lookup' in line)
        self.assertIn(lookup.replace("$SELECTION_PATH/", "$RESOURCE_PATH/"), selection.splitlines(), "the hygiene job runs the selection's own lookup command")
        self.assertIn("SHA: ${{ github.event_name == 'repository_dispatch' && github.event.client_payload.sha || github.sha }}", workflow)
        self.assertIn('push|repository_dispatch) set -- --lookup "$RUNNER_TEMP/push-lookup.json" ;;', workflow)

    def test_an_edited_run_reuses_the_passed_checks_of_its_tested_tree_and_every_pull_request_run_stores_them(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertRegex(workflow, r"(?m)^      - name: Restore the passed checks of the pull request\n        if: github\.event_name == 'pull_request' && github\.event\.action == 'edited' && github\.run_attempt == 1\n        uses: actions/cache/restore@[0-9a-f]{40} # v\d+(?:\.\d+)*\n")
        self.assertIn("          restore-keys: hygiene-results-${{ github.event.pull_request.number }}-run-\n", workflow)
        self.assertIn("REUSE_RESULTS: ${{ github.event_name == 'pull_request' && github.event.action == 'edited' && github.run_attempt == 1 }}", workflow)
        self.assertIn('if [ "$REUSE_RESULTS" = "true" ]; then set -- "$@" --results "$RUNNER_TEMP/hygiene-results/record.json"; fi', workflow)
        for check, step in (("actionlint", "actionlint"), ("zizmor", "zizmor"), ("packages", "coverage"), ("markdown", "links"), ("reuse", "reuse")):
            self.assertRegex(workflow, rf"(?m)^          {check.upper()}: \$\{{\{{ steps\.{step}\.outcome \}}\}}$")
            self.assertIn(f'--outcome "{check}=${check.upper()}"', workflow)
        names = step_names(workflow)
        self.assertEqual(["Record the passed checks for an edit of the pull request", "Store the passed checks in the pull request's cache"], names[-2:])
        self.assertEqual(2, workflow.count("        if: ${{ !cancelled() && github.event_name == 'pull_request' }}\n"), "a cancelled run stores nothing")
        self.assertIn("          key: hygiene-results-${{ github.event.pull_request.number }}-run-${{ github.run_id }}-${{ github.run_attempt }}\n", workflow)

    def test_an_edited_run_runs_gitleaks_and_the_convention_checks_again(self) -> None:
        _, document = read_workflow(WORKFLOW)
        steps = document["jobs"]["hygiene"]["steps"]
        self.assertEqual(["Restore the passed checks of the pull request"], [step["name"] for step in steps if "edited" in str(step.get("if", ""))])
        # Each condition reads only the event name, the author decision and earlier step outcomes, so an edited pull request runs the step.
        for name, condition in (
            ("Decide whether the PR author skips the convention checks", "github.event_name == 'pull_request'"),
            ("Load the convention checks from this workflow revision", "steps.author-decision.outputs.skip == 'false'"),
            ("actions/setup-node", "steps.author-decision.outputs.skip == 'false'"),
            ("Check the pull request branch", "${{ !cancelled() && steps.author-decision.outputs.skip == 'false' }}"),
            ("Install the locked commitlint toolchain", "${{ !cancelled() && steps.author-decision.outputs.skip == 'false' }}"),
            ("Check every pull request commit", "${{ !cancelled() && steps.commitlint.outcome == 'success' }}"),
            ("Check the pull request title", "${{ !cancelled() && steps.commitlint.outcome == 'success' }}"),
            ("Install the pinned tools and verify their checksums", "${{ !cancelled() }}"),
            ("Gitleaks over all reachable history", "${{ !cancelled() && steps.install.outcome == 'success' }}"),
        ):
            with self.subTest(step=name):
                step = next((step for step in steps if step.get("name", str(step.get("uses", "")).split("@")[0]) == name), None)
                self.assertIsNotNone(step, f"the hygiene job has no step {name}")
                self.assertEqual(condition, step.get("if"))

    def test_on_a_pull_request_the_job_runs_the_convention_checks_on_the_pinned_node(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("      - name: Decide whether the PR author skips the convention checks\n        id: author-decision\n        if: github.event_name == 'pull_request'\n", workflow)
        self.assertIn("PR_AUTHOR: ${{ github.event.pull_request.user.login }}", workflow)
        self.assertNotIn("github.actor", workflow)
        self.assertIn("uses: $/actions/publication-checks", workflow)
        self.assertRegex(workflow, action_pin_pattern("actions/setup-node"))
        self.assertIn("node-version: ${{ steps.resources.outputs.node-version }}", workflow)
        self.assertNotIn("include-node-version", workflow, "the resource action reads the pinned Node.js version")
        self.assertNotRegex(workflow, r"node-version: \"\d")
        self.assertIn("npm ci --ignore-scripts", workflow)
        self.assertIn("--userconfig /dev/null", workflow)
        self.assertIn('run: node "$CONVENTIONS_PATH/check-branch.cjs" "$HEAD_BRANCH"', workflow)
        self.assertIn('run: node "$CONVENTIONS_PATH/check-commits.cjs" "$BASE_SHA" "$HEAD_SHA"', workflow)
        self.assertIn('node "$COMMITLINT_CLI" --config "$COMMITLINT_CONFIG" --cwd "$GITHUB_WORKSPACE" --verbose', workflow)
        self.assertNotIn('run: node "$CONVENTIONS_PATH/verify-spec.cjs"', workflow, "the branch command verifies the specification itself")
        self.assertLess(workflow.index("Check the pull request branch"), workflow.index("Install the locked commitlint toolchain"))
        self.assertIn("      - name: Check the pull request branch\n        if: ${{ !cancelled() && steps.author-decision.outputs.skip == 'false' }}\n", workflow)
        for step_name in ("Check every pull request commit", "Check the pull request title"):
            self.assertIn(f"      - name: {step_name}\n        if: ${{{{ !cancelled() && steps.commitlint.outcome == 'success' }}}}\n", workflow)

    def test_the_hygiene_workflow_runs_each_check_only_when_the_change_can_affect_it(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertRegex(workflow, r'(?m)^        id: changes\n(?:        env:\n(?:          .*\n)+)?        run: \|\n(?:          .*\n)*?          .*hygiene\.py" changes "\$@" .* --github-output "\$GITHUB_OUTPUT" --summary "\$GITHUB_STEP_SUMMARY"$')
        self.assertIn('*) set -- --base "$BASE" --head "$HEAD" --compare "$COMPARE" ;;', workflow)
        for selector in ("step:resources", "key:outputs.path.value", "key:runs.using"):
            self.assertIn(f'--definition "actions/repository-hygiene/action.yml#{selector}"', workflow)
        for flag, step, field in (("packages", "coverage", "coverage-exclude"), ("markdown", "links", "link-exclude")):
            self.assertIn(f'--{flag}-definition ".github/workflows/repository-hygiene.yml#step:{step}"', workflow)
            for definition in ("repository-hygiene.yml", "repository-hygiene-caller.yml"):
                self.assertIn(f'--{flag}-definition ".github/workflows/{definition}#{field}"', workflow)
        self.assertIn("BASE: ${{ github.event_name == 'pull_request' && github.event.pull_request.base.sha || github.event.before }}", workflow)
        self.assertIn("HEAD: ${{ github.event_name == 'pull_request' && github.event.pull_request.head.sha || github.sha }}", workflow)
        self.assertIn("COMPARE: ${{ github.event_name == 'pull_request' && 'merge-base' || 'direct' }}", workflow)
        self.assertIn('if [ "$ACTIONLINT" = "true" ]; then tools="$tools --tool actionlint --tool shellcheck"; fi', workflow)
        self.assertIn('if [ "$ZIZMOR" = "true" ]; then tools="$tools --tool zizmor"; fi', workflow)
        self.assertIn('if [ "$REUSE" = "true" ]; then tools="$tools --tool reuse"; fi', workflow)
        self.assertNotIn("setup-python", workflow, "the pinned reuse installs into a virtual environment of the job's own python3")
        self.assertIn("id: install\n        if: ${{ !cancelled() }}", workflow)
        for check in ("Dependabot coverage", "Relative links"):
            self.assertLess(workflow.index(f"- name: {check}"), workflow.index("- name: Install the pinned tools"))
        for check in ("actionlint", "zizmor", "reuse"):
            self.assertRegex(workflow, rf"(?m)^        id: {check}\n        if: \$\{{\{{ !cancelled\(\) && steps\.install\.outcome == 'success' && steps\.changes\.outputs\.{check} == 'true' \}}\}}\n(?:        env:\n(?:          .*\n)+)?        run: .*hygiene\.py\" {check} ")
        self.assertRegex(workflow, r"(?m)^        if: \$\{\{ !cancelled\(\) && steps\.changes\.outputs\.packages == 'true' \}\}\n(?:        env:\n(?:          .*\n)+)?        run: .*hygiene\.py\" coverage ")
        self.assertRegex(workflow, r"(?m)^        if: \$\{\{ !cancelled\(\) && steps\.changes\.outputs\.markdown == 'true' \}\}\n(?:        env:\n(?:          .*\n)+)?        run: .*hygiene\.py\" links ")
        self.assertNotIn("paths:", workflow)
        self.assertNotIn("paths-ignore:", workflow)
        self.assertEqual(9, workflow.count('PYTHONPYCACHEPREFIX="$RUNNER_TEMP/repo-pycache" python3 "$RESOURCE_PATH/'))


if __name__ == "__main__":
    unittest.main()
