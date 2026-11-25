"""Policy checks over the push lookup and the cache steps that store and reuse passed results."""

from __future__ import annotations

import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, condition_holds, passed_list_steps, read_workflow

WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
PREFIX = "suite-results-${{ github.event.pull_request.number }}-run-"
DEFAULT_BRANCH_ONLY = (
    "(github.event_name == 'repository_dispatch'"
    " || (github.event_name == 'push' && github.ref_type == 'branch'"
    " && github.ref_name == github.event.repository.default_branch))"
)
LISTED = (
    "${{ !cancelled() && ((github.event_name == 'pull_request' && (steps.check.outcome == 'success' || steps.check.outputs.additions != ''))"
    f" || ({DEFAULT_BRANCH_ONLY} && steps.check.outcome == 'success')) }}}}"
)
KEPT = "${{ !cancelled() && github.event_name == 'pull_request' && steps.check.outputs.additions != '' }}"
DISPATCH_CHECK = "Check that the dispatched commit is on the default branch"
DISPATCH_IF = "github.event_name == 'repository_dispatch' && github.event.client_payload.sha"
UPLOADED = (
    "${{ !cancelled() && ((github.event_name == 'pull_request' && (steps.check.outcome == 'success' || steps.keep.outcome == 'success'))"
    f" || ({DEFAULT_BRANCH_ONLY} && steps.check.outcome == 'success')) }}}}"
)
DEFAULT_RECORD_SAVE_IF = (
    "${{ !cancelled() && steps.verdict.outcome == 'success'"
    " && steps.store-default.outputs.stored == 'true'"
    f" && {DEFAULT_BRANCH_ONLY} }}}}"
)
DEFAULT_RECORD_SAVE_WITH = {
    "path": "${{ runner.temp }}/default-branch-record/record.json",
    "key": "default-branch-record-${{ github.run_id }}-${{ github.run_attempt }}",
}
MARKER_SAVE_IF = (
    "${{ !cancelled() && steps.install-gitleaks.outcome == 'success'"
    " && steps.gitleaks.outputs.recorded-marker == 'true'"
    f" && {DEFAULT_BRANCH_ONLY} }}}}"
)
MARKER_SAVE_WITH = {
    "path": "${{ runner.temp }}/gitleaks-marker/marker.json",
    "key": "gitleaks-marker-${{ github.run_id }}-${{ github.run_attempt }}",
}
REUSE_MARKER_SAVE_IF = (
    "${{ !cancelled() && steps.install-reuse.outcome == 'success'"
    " && steps.reuse.outputs.recorded-marker == 'true'"
    f" && {DEFAULT_BRANCH_ONLY} }}}}"
)
REUSE_MARKER_SAVE_WITH = {
    "path": "${{ runner.temp }}/reuse-marker/marker.json",
    "key": "reuse-marker-${{ github.run_id }}-${{ github.run_attempt }}",
}


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
        restore_default = self.steps[3]
        self.assertEqual("Restore the default-branch record", restore_default["name"])
        self.assertTrue(restore_default["uses"].startswith("actions/cache/restore@"))
        lookup = self.steps[4]
        self.assertIn('suite_selection.py" lookup --root . --sha "$SHA"', lookup["run"])
        self.assertIn('--default-record "$RUNNER_TEMP/default-branch-record/record.json"', lookup["run"])
        self.assertEqual("github.event_name == 'push' || github.event_name == 'repository_dispatch'", lookup["if"])
        self.assertEqual("${{ github.event_name == 'repository_dispatch' && github.event.client_payload.sha || github.sha }}", lookup["env"]["SHA"])
        self.assertEqual("${{ github.run_attempt }}", lookup["env"]["RUN_ATTEMPT"])
        select = next(step for step in self.steps if step.get("id") == "select")
        self.assertIn('--lookup "$RUNNER_TEMP/push-lookup.json"', select["run"])

    def test_select_makes_two_read_only_cache_lookups_for_default_branch_and_pull_request_records_and_writes_tested_state(self) -> None:
        self.assertEqual(2, len(re.findall(r"uses: actions/cache", self.text)))
        restores = [step for step in self.steps if str(step.get("uses", "")).startswith("actions/cache/")]
        self.assertEqual(2, len(restores))
        self.assertTrue(all(step["uses"].startswith("actions/cache/restore@") for step in restores), "select only reads the stores")
        default_restore, pr_restore = restores
        self.assertEqual("default-branch-record-", default_restore["with"]["restore-keys"])
        self.assertEqual("${{ runner.temp }}/default-branch-record/record.json", default_restore["with"]["path"])
        self.assertEqual("github.event_name == 'pull_request' && github.run_attempt == 1", pr_restore["if"])
        self.assertEqual(PREFIX, pr_restore["with"]["restore-keys"])
        self.assertTrue(pr_restore["with"]["key"].startswith(PREFIX))
        self.assertEqual("${{ runner.temp }}/suite-results/record.json", pr_restore["with"]["path"])
        select = next(step for step in self.steps if step.get("id") == "select")
        script = select["run"]
        self.assertIn('--results "$RUNNER_TEMP/suite-results/record.json" --default-record "$RUNNER_TEMP/default-branch-record/record.json" --run-attempt "$RUN_ATTEMPT"', script)
        write_state = next(step for step in self.steps if step.get("name") == "Record the tested state of the pull request")
        upload_state = next(step for step in self.steps if step.get("name") == "Upload the tested state of the pull request")
        self.assertEqual("github.event_name == 'pull_request' && github.event.action != 'edited'", write_state["if"])
        self.assertEqual("github.event_name == 'pull_request' && github.event.action != 'edited'", upload_state["if"])
        self.assertTrue(upload_state["uses"].startswith("actions/upload-artifact@"))
        self.assertEqual("tested-state", upload_state["with"]["name"])

    def test_the_select_outputs_carry_the_units_the_reused_results_and_the_identical_tree_to_the_verdict(self) -> None:
        for output in ("units", "reused", "identical"):
            with self.subTest(output=output):
                self.assertIn(f"      {output}: ${{{{ steps.select.outputs.{output} }}}}\n", self.text)
                self.assertRegex(self.text, rf"(?m)^      {output}:\n        description: .*\n        value: \$\{{\{{ jobs\.select\.outputs\.{output} \}}\}}$")


class StoreStepTests(unittest.TestCase):
    def test_only_the_verdict_the_pipeline_and_hygiene_store_results_on_pull_requests_and_the_gitleaks_marker_on_the_default_branch(self) -> None:
        for path in sorted(WORKFLOWS.glob("*.yml")):
            if path.name not in ("suite-verdict.yml", "repository-hygiene.yml", "pipeline.yml"):
                with self.subTest(workflow=path.name):
                    self.assertNotIn("actions/cache/save", path.read_text(encoding="utf-8"))
        text, workflow = read_workflow(WORKFLOWS / "suite-verdict.yml")
        steps = steps_of(workflow, "validation")
        self.assertEqual(1, text.count("uses: actions/cache"))
        download = next(step for step in steps if step.get("name") == "Download the entry files that passed in the run")
        store = next(step for step in steps if step.get("id") == "store")
        save = next(step for step in steps if str(step.get("uses", "")).startswith("actions/cache/save@"))
        checkout = next(step for step in steps if str(step.get("uses", "")).startswith("actions/checkout@"))
        self.assertTrue(download["uses"].startswith("actions/download-artifact@"))
        self.assertEqual("suite-results-*", download["with"]["pattern"])
        self.assertIn('suite_selection.py" store --needs', store["run"])
        self.assertLess(steps.index(store), steps.index(save))
        for step in steps[1:steps.index(store) + 1]:
            if step is not checkout:
                self.assertEqual("${{ !cancelled() && github.event_name == 'pull_request' }}", step["if"])
        self.assertEqual("${{ !cancelled() && github.event_name == 'pull_request' && steps.additions.outputs.found == 'true' }}", checkout["if"])
        self.assertTrue(save["uses"].startswith("actions/cache/save@"))
        self.assertEqual("${{ !cancelled() && github.event_name == 'pull_request' && steps.store.outputs.stored == 'true' }}", save["if"])
        self.assertEqual(PREFIX + "${{ github.run_id }}-${{ github.run_attempt }}", save["with"]["key"])
        self.assertNotIn("if", steps[-1], "the verdict itself reports on every run")
        pipeline_text, pipeline = read_workflow(WORKFLOWS / "pipeline.yml")
        self.assertEqual(4, pipeline_text.count("uses: actions/cache/save"))
        pipeline_saves = {
            job: [step for step in body.get("steps", ()) if str(step.get("uses", "")).startswith("actions/cache/save@")]
            for job, body in pipeline["jobs"].items()
            if any(str(step.get("uses", "")).startswith("actions/cache/save@") for step in body.get("steps", ()))
        }
        self.assertEqual({"repository-hygiene", "suite-selection-results", "verdict"}, set(pipeline_saves))
        self.assertEqual(1, len(pipeline_saves["suite-selection-results"]))
        self.assertEqual(2, len(pipeline_saves["repository-hygiene"]))
        self.assertEqual(1, len(pipeline_saves["verdict"]))
        verdict_steps = steps_of(pipeline, "verdict")
        verdict_decision = next(step for step in verdict_steps if step.get("id") == "verdict")
        store_default = next(step for step in verdict_steps if step.get("id") == "store-default")
        default_save = pipeline_saves["verdict"][0]
        self.assertLess(verdict_steps.index(verdict_decision), verdict_steps.index(store_default))
        self.assertLess(verdict_steps.index(store_default), verdict_steps.index(default_save))
        self.assertEqual("Store the default-branch record in the default branch's cache", default_save["name"])
        self.assertEqual(DEFAULT_RECORD_SAVE_IF, default_save["if"])
        self.assertEqual(DEFAULT_RECORD_SAVE_WITH, default_save["with"])
        for event, ref_type, ref_name, verdict_outcome, stored, expected in (
            ("push", "branch", "main", "success", "true", True),
            ("repository_dispatch", "branch", "main", "success", "true", True),
            ("pull_request", "branch", "main", "success", "true", False),
            ("push", "branch", "feat-branch", "success", "true", False),
            ("push", "tag", "main", "success", "true", False),
            ("push", "branch", "main", "failure", "true", False),
            ("push", "branch", "main", "success", "", False),
        ):
            with self.subTest(event=event, ref_type=ref_type, ref_name=ref_name, verdict_outcome=verdict_outcome, stored=stored):
                self.assertEqual(expected, condition_holds(default_save["if"], {
                    "github.event_name": event, "github.ref_type": ref_type, "github.ref_name": ref_name,
                    "github.event.repository.default_branch": "main",
                    "steps.verdict.outcome": verdict_outcome, "steps.store-default.outputs.stored": stored,
                }))
        results = pipeline["jobs"]["suite-selection-results"]
        self.assertEqual(
            "${{ !cancelled() && github.event_name == 'pull_request' && needs.plan.result == 'success' && (needs.plan.outputs.units != '' || needs.plan.outputs.reused != '') }}",
            results["if"],
        )
        self.assertEqual(save["with"], results["steps"][-1]["with"], "the pipeline stores under the key that the selection restores")
        self.assertEqual(results["steps"][-1], pipeline_saves["suite-selection-results"][0])
        hygiene_text, hygiene = read_workflow(WORKFLOWS / "repository-hygiene.yml")
        self.assertEqual(3, hygiene_text.count("uses: actions/cache/save"))
        hygiene_saves = {
            job: [step for step in body.get("steps", ()) if str(step.get("uses", "")).startswith("actions/cache/save@")]
            for job, body in hygiene["jobs"].items()
            if any(str(step.get("uses", "")).startswith("actions/cache/save@") for step in body.get("steps", ()))
        }
        self.assertEqual({"hygiene"}, set(hygiene_saves))
        self.assertEqual(3, len(hygiene_saves["hygiene"]))
        for label, wf_steps, reuse_marker_save, marker_save in (
            ("pipeline", steps_of(pipeline, "repository-hygiene"), pipeline_saves["repository-hygiene"][0], pipeline_saves["repository-hygiene"][1]),
            ("repository-hygiene", steps_of(hygiene, "hygiene"), hygiene_saves["hygiene"][0], hygiene_saves["hygiene"][1]),
        ):
            with self.subTest(workflow=label):
                reuse = next(step for step in wf_steps if step.get("id") == "reuse")
                self.assertLess(wf_steps.index(reuse), wf_steps.index(reuse_marker_save))
                self.assertEqual("Store the default-branch REUSE version marker", reuse_marker_save["name"])
                self.assertEqual(REUSE_MARKER_SAVE_IF, reuse_marker_save["if"])
                self.assertEqual(REUSE_MARKER_SAVE_WITH, reuse_marker_save["with"])
                gitleaks = next(step for step in wf_steps if step.get("id") == "gitleaks")
                self.assertLess(wf_steps.index(gitleaks), wf_steps.index(marker_save))
                self.assertEqual("Store the default-branch Gitleaks scan marker", marker_save["name"])
                self.assertEqual(MARKER_SAVE_IF, marker_save["if"])
                self.assertEqual(MARKER_SAVE_WITH, marker_save["with"])
        hygiene_pr_save = hygiene_saves["hygiene"][2]
        self.assertEqual("Store the passed checks in the pull request's cache", hygiene_pr_save["name"])
        self.assertEqual("${{ !cancelled() && github.event_name == 'pull_request' }}", hygiene_pr_save["if"])
        self.assertEqual({
            "path": "${{ runner.temp }}/hygiene-results/record.json",
            "key": "hygiene-results-${{ github.event.pull_request.number }}-run-${{ github.run_id }}-${{ github.run_attempt }}",
        }, hygiene_pr_save["with"])

    def test_store_keys_the_passed_units_under_the_run_s_additions_in_a_checkout_of_the_tested_tree(self) -> None:
        _, verdict = read_workflow(WORKFLOWS / "suite-verdict.yml")
        _, pipeline = read_workflow(WORKFLOWS / "pipeline.yml")
        for label, steps in (("suite-verdict", steps_of(verdict, "validation")), ("pipeline", steps_of(pipeline, "suite-selection-results"))):
            with self.subTest(workflow=label):
                names = [step.get("name") or step.get("uses", "").split("@")[0] for step in steps]
                found = names.index("Find the additions of the run")
                additions = steps[found - 1]
                self.assertTrue(additions["uses"].startswith("actions/download-artifact@"))
                self.assertEqual({"pattern": "recording-additions-*", "path": "${{ runner.temp }}/suite-results/additions"}, additions["with"],
                                 "the artifacts that the additions workflow applies")
                self.assertEqual("additions", steps[found]["id"])
                self.assertIn('-name additions.json', steps[found]["run"])
                checkout = steps[found + 1]
                self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
                self.assertIn("steps.additions.outputs.found == 'true'", checkout["if"])
                self.assertEqual("false", str(checkout["with"]["persist-credentials"]).lower(), "the tested tree of the run, without a token")
                self.assertIn(checkout["with"].get("ref", ""), ("", "${{ github.event_name == 'repository_dispatch' && github.event.client_payload.sha || '' }}"),
                              "the commit that select checked out")
                store = steps[found + 2]
                self.assertEqual("store", store["id"])
                self.assertEqual("${{ steps.additions.outputs.found }}", store["env"]["ADDITIONS"])
                self.assertIn('if [ "$ADDITIONS" = true ]; then set -- --additions "$RUNNER_TEMP/suite-results/additions" --root .; else set --; fi',
                              store["run"])
                self.assertIn('--run-id "$RUN_ID" "$@" --output', store["run"])

    def test_the_store_steps_decide_no_result_the_verdict_reads(self) -> None:
        # A job that reports additions fails on its check; storing its passes changes neither that job nor the verdict.
        for path, job in (("suite-verdict.yml", "validation"), ("pipeline.yml", "suite-selection-results")):
            _, workflow = read_workflow(WORKFLOWS / path)
            for step in steps_of(workflow, job):
                with self.subTest(workflow=path, step=step.get("name") or step.get("uses")):
                    self.assertNotIn("continue-on-error", step)
                    self.assertNotIn("steps.store.outputs", str(step.get("env", {})), "no verdict reads what store wrote")


def outside_the_full_recording(workflow: dict) -> dict:
    """The suite jobs as a pull-request or push run reads them: without the condition that keeps a step out of the full
    recording."""
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if "if" in step:
                step["if"] = str(step["if"]).replace("!inputs.full && ", "")
    return workflow


class SuiteJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, workflow = read_workflow(WORKFLOWS / "suite-jobs.yml")
        self.workflow = outside_the_full_recording(workflow)

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
            for case, (event, ref_name, outcome, additions), expected in (
                    ("default-branch push passed", ("push", "main", "success", ""), [upload]),
                    ("default-branch dispatch passed", ("repository_dispatch", "main", "success", ""), [upload]),
                    ("default-branch push with additions", ("push", "main", "failure", "additions_json"), []),
                    ("non-default branch push passed", ("push", "topic", "success", ""), [])):
                with self.subTest(job=job, case=case):
                    ran = passed_list_steps(steps, outcome, additions, event_name=event, ref_name=ref_name)
                    self.assertEqual((listed[job] if expected else []) + expected, ran)


if __name__ == "__main__":
    unittest.main()
