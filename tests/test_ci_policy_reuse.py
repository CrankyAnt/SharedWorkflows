"""Policy checks over the push lookup and the cache steps that store and reuse passed results."""

from __future__ import annotations

import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, passed_list_steps, read_workflow

WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
PREFIX = "suite-results-${{ github.event.pull_request.number }}-run-"
LISTED = "${{ !cancelled() && github.event_name == 'pull_request' && (steps.check.outcome == 'success' || steps.check.outputs.additions != '') }}"
KEPT = "${{ !cancelled() && github.event_name == 'pull_request' && steps.check.outputs.additions != '' }}"
DISPATCH_CHECK = "Check that the dispatched commit is on the default branch"
DISPATCH_IF = "github.event_name == 'repository_dispatch' && github.event.client_payload.sha"
UPLOADED = "${{ !cancelled() && github.event_name == 'pull_request' && (steps.check.outcome == 'success' || steps.keep.outcome == 'success') }}"


def steps_of(workflow: dict, job: str) -> list[dict]:
    return workflow["jobs"][job]["steps"]


class SelectionStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, workflow = read_workflow(WORKFLOWS / "suite-selection.yml")
        self.steps = steps_of(workflow, "select")

    def test_the_dispatch_check_follows_the_checkout_and_the_push_lookup_starts_a_dispatch_run_from_its_payload(self) -> None:
        self.assertEqual("$/actions/suite-selection", self.steps[0]["uses"])
        self.assertTrue(self.steps[1]["uses"].startswith("actions/checkout@"))
        check = self.steps[2]
        self.assertEqual(DISPATCH_CHECK, check["name"])
        self.assertEqual(DISPATCH_IF, check["if"])
        self.assertEqual({"RESOURCE_PATH": "${{ steps.resources.outputs.path }}", "PAYLOAD": "${{ github.event.client_payload.sha }}",
                          "BRANCH": "${{ github.event.repository.default_branch }}"}, check["env"])
        self.assertIn('suite_selection.py" check-dispatch --root . --sha "$PAYLOAD" --branch "$BRANCH"', check["run"])
        self.assertTrue(all("if" not in step or "always()" not in step["if"] and "cancelled()" not in step["if"] for step in self.steps),
                        "a failed dispatch check skips every later step")
        lookup = self.steps[3]
        self.assertIn('suite_selection.py" lookup --root . --sha "$SHA"', lookup["run"])
        self.assertEqual("github.event_name == 'push' || github.event_name == 'repository_dispatch'", lookup["if"])
        self.assertEqual("${{ github.event_name == 'repository_dispatch' && github.event.client_payload.sha || github.sha }}", lookup["env"]["SHA"])
        self.assertEqual("${{ github.run_attempt }}", lookup["env"]["RUN_ATTEMPT"])
        self.assertIn('--lookup "$RUNNER_TEMP/push-lookup.json"', self.steps[-1]["run"])

    def test_select_makes_one_cache_lookup_per_run_in_the_pull_request_s_scope_and_none_on_a_re_run(self) -> None:
        self.assertEqual(1, len(re.findall(r"uses: actions/cache", self.text)))
        restore = next(step for step in self.steps if str(step.get("uses", "")).startswith("actions/cache/"))
        self.assertTrue(restore["uses"].startswith("actions/cache/restore@"), "select only reads the store")
        self.assertEqual("github.event_name == 'pull_request' && github.run_attempt == 1", restore["if"])
        self.assertEqual(PREFIX, restore["with"]["restore-keys"])
        self.assertTrue(restore["with"]["key"].startswith(PREFIX))
        self.assertEqual("${{ runner.temp }}/suite-results/record.json", restore["with"]["path"])
        script = self.steps[-1]["run"]
        self.assertIn('--results "$RUNNER_TEMP/suite-results/record.json" --run-attempt "$RUN_ATTEMPT"', script)

    def test_the_select_outputs_carry_the_units_the_reused_results_and_the_identical_tree_to_the_verdict(self) -> None:
        for output in ("units", "reused", "identical"):
            with self.subTest(output=output):
                self.assertIn(f"      {output}: ${{{{ steps.select.outputs.{output} }}}}\n", self.text)
                self.assertRegex(self.text, rf"(?m)^      {output}:\n        description: .*\n        value: \$\{{\{{ jobs\.select\.outputs\.{output} \}}\}}$")


class StoreStepTests(unittest.TestCase):
    def test_only_the_verdict_and_the_hygiene_job_store_and_only_a_pull_request_run_that_was_not_cancelled(self) -> None:
        for path in sorted(WORKFLOWS.glob("*.yml")):
            if path.name not in ("suite-verdict.yml", "repository-hygiene.yml"):
                with self.subTest(workflow=path.name):
                    self.assertNotIn("actions/cache/save", path.read_text(encoding="utf-8"))
        text, workflow = read_workflow(WORKFLOWS / "suite-verdict.yml")
        steps = steps_of(workflow, "validation")
        self.assertEqual(1, text.count("uses: actions/cache"))
        download, store, save = steps[1], steps[2], steps[3]
        self.assertTrue(download["uses"].startswith("actions/download-artifact@"))
        self.assertEqual("suite-results-*", download["with"]["pattern"])
        self.assertIn('suite_selection.py" store --needs', store["run"])
        for step in (download, store):
            self.assertEqual("${{ !cancelled() && github.event_name == 'pull_request' }}", step["if"])
        self.assertTrue(save["uses"].startswith("actions/cache/save@"))
        self.assertEqual("${{ !cancelled() && github.event_name == 'pull_request' && steps.store.outputs.stored == 'true' }}", save["if"])
        self.assertEqual(PREFIX + "${{ github.run_id }}-${{ github.run_attempt }}", save["with"]["key"])
        self.assertNotIn("if", steps[-1], "the verdict itself reports on every run")


class ValidateJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.workflow = read_workflow(WORKFLOWS / "validate.yml")

    def test_each_job_that_runs_units_uploads_its_passed_entry_files_after_its_check_without_those_with_additions(self) -> None:
        for job, name, keep in (
                ("linux", "suite-results-shard-${{ matrix.shard.shard }}",
                 'python3 "$RECORDING/recording.py" keep --additions "$ADDITIONS" "$RUNNER_TEMP/suite-results/passed.json"'),
                ("windows", "suite-results-windows",
                 'python "$env:RECORDING\\recording.py" keep --additions "$env:ADDITIONS" "$env:RUNNER_TEMP\\suite-results\\passed.json"\n'
                 "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }")):
            with self.subTest(job=job):
                steps = steps_of(self.workflow, job)
                check = next(index for index, step in enumerate(steps) if step.get("id") == "check")
                kept = next(index for index, step in enumerate(steps) if step.get("id") == "keep")
                upload = next(index for index, step in enumerate(steps) if str(step.get("uses", "")).startswith("actions/upload-artifact@"))
                self.assertLess(check, kept)
                self.assertEqual(kept + 1, upload)
                self.assertEqual(KEPT, steps[kept]["if"])
                self.assertEqual({"RECORDING": "${{ steps.recording.outputs.path }}", "ADDITIONS": "${{ steps.check.outputs.additions }}"},
                                 steps[kept]["env"])
                self.assertEqual(keep, steps[kept]["run"])
                self.assertEqual(UPLOADED, steps[upload]["if"])
                self.assertEqual(name, steps[upload]["with"]["name"])
                self.assertEqual("${{ runner.temp }}/suite-results/passed.json", steps[upload]["with"]["path"])
        linux = steps_of(self.workflow, "linux")
        listing = next(step for step in linux if step.get("name") == "List the entry files that passed")
        self.assertEqual(LISTED, listing["if"])
        self.assertIn('"$RUNNER_TEMP/entry-logs/$index.status"', listing["run"])
        self.assertEqual(linux.index(listing) + 1, next(index for index, step in enumerate(linux) if step.get("id") == "keep"))

    def test_a_check_that_passed_uploads_every_pass_one_with_additions_the_kept_passes_and_a_recording_error_none(self) -> None:
        listed = {"linux": ["List the entry files that passed"], "windows": []}
        for job in ("linux", "windows"):
            steps = steps_of(self.workflow, job)
            upload = "Upload the entry files that passed"
            for case, (outcome, additions, keep), expected in (
                    ("the check passed", ("success", "", "success"), [upload]),
                    ("additions", ("failure", "additions_json", "success"), ["keep", upload]),
                    ("additions whose keep failed", ("failure", "additions_json", "failure"), ["keep"]),
                    ("a recording error", ("failure", "", "success"), []),
                    ("no check", ("skipped", "", "success"), [])):
                with self.subTest(job=job, case=case):
                    ran = passed_list_steps(steps, outcome, additions, keep)
                    self.assertEqual((listed[job] if expected else []) + expected, ran)

    def test_the_runner_labels_and_setup_versions_of_the_key_are_those_the_jobs_use(self) -> None:
        inputs = self.workflow["jobs"]["select"]["with"]
        labels = dict(item.split("=", 1) for item in inputs["runner-labels"].split())
        self.assertEqual({"linux": self.workflow["jobs"]["linux"]["runs-on"], "windows": self.workflow["jobs"]["windows"]["runs-on"]}, labels)
        versions = dict(item.split("=", 1) for item in inputs["setup-versions"].split())
        for job in ("linux", "windows"):
            with self.subTest(job=job):
                setup = next(step for step in steps_of(self.workflow, job) if str(step.get("uses", "")).startswith("actions/setup-python@"))
                self.assertEqual(versions["python"], setup["with"]["python-version"])


if __name__ == "__main__":
    unittest.main()
