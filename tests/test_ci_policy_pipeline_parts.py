"""Text contract of the Python and .NET parts of the pipeline: each is a job with read rights only that runs once per
configured project, takes its values from the plan and runs a shared step of the pipeline's own revision, and none is a
reusable workflow of its own."""

from __future__ import annotations

import json
import re
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT, ci_definition, read_workflow, tracked_workflows

WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
PIPELINE_PATH = ".github/workflows/pipeline.yml"
PIPELINE = REPOSITORY_ROOT / PIPELINE_PATH
SURFACE = REPOSITORY_ROOT / "actions" / "repository-hygiene" / "shared-surface.json"
CATALOGUE = REPOSITORY_ROOT / "catalogue.json"
FRAGMENTS = REPOSITORY_ROOT / "actions" / "pipeline" / "schema" / "v1" / "parts"
PYTHON_VERIFY, PYTHON_TEST, DOTNET_TEST = "python-verify", "python-test", "dotnet-test"
# Each part with the shared steps its job reaches at the pipeline's own revision, in the job's order.
SHARED_STEPS = {
    PYTHON_VERIFY: ["actions/python-tests", "actions/python-language-step"],
    PYTHON_TEST: ["actions/python-tests"],
    DOTNET_TEST: ["actions/dotnet-test"],
}
# The pipeline calls no reusable workflow: a caller and the pipeline are the only two levels, as the spec states.
ALLOWED_REUSABLE_WORKFLOWS = 0
LOCAL_WORKFLOW = re.compile(r"^(?:\$|\.)/(\.github/workflows/[^/@]+\.ya?ml)$")


def value(setting: str) -> str:
    """The expression by which a job reads one value of its project, an entry of the matrix that the plan gives."""
    return f"matrix.project['{setting}']"


def projects(part: str) -> str:
    """The expression by which a job reads the projects of its part from the plan."""
    return f"${{{{ fromJSON(needs.plan.outputs.plan).parts['{part}'].config['projects'] }}}}"


def job_uses(text: str, source: str) -> list[str]:
    """The ``uses:`` value of each job of a workflow that calls a reusable workflow."""
    document = ci_definition().read_yaml(text, source)
    jobs = document.get("jobs") if isinstance(document, dict) else None
    return [str(job["uses"]) for job in jobs.values() if isinstance(job, dict) and job.get("uses")] if isinstance(jobs, dict) else []


def reached_reusable_workflows(workflows: dict[str, str], start: str) -> list[str]:
    """The reusable workflows that the jobs of ``start`` call, and those that these call in turn, each once: a workflow of
    this repository by its path, any other by its reference. ``workflows`` maps the path of each workflow to its text."""
    reached: list[str] = []
    pending = [start]
    while pending:
        path = pending.pop(0)
        for reference in job_uses(workflows.get(path, ""), path):
            local = LOCAL_WORKFLOW.match(reference)
            name = local.group(1) if local else reference
            if name not in reached and name != start:
                reached.append(name)
                if local:
                    pending.append(name)
    return reached


class PartJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.pipeline = read_workflow(PIPELINE)
        self.jobs = self.pipeline["jobs"]

    def section(self, part: str) -> str:
        """The text of one job of the pipeline."""
        start = self.text.index(f"\n  {part}:\n")
        following = re.search(r"\n  [a-z][a-z0-9-]*:\n", self.text[start + 1:])
        return self.text[start:start + 1 + following.start()] if following else self.text[start:]

    def steps(self, part: str) -> list[dict]:
        return self.jobs[part]["steps"]

    def test_each_part_has_read_rights_only_takes_no_secret_and_keeps_no_checkout_credentials(self) -> None:
        for part in SHARED_STEPS:
            with self.subTest(part=part):
                self.assertEqual({"contents": "read"}, self.jobs[part]["permissions"])
                section = self.section(part)
                for forbidden in ("secrets.", "github.token", "GITHUB_TOKEN", "inputs.", "vars.", "actions/cache", "cache:", "cache-mode", "PIP_CACHE_DIR"):
                    self.assertNotIn(forbidden, section)
                checkouts = [step for step in self.steps(part) if str(step.get("uses", "")).startswith("actions/checkout@")]
                self.assertEqual(1, len(checkouts))
                self.assertEqual({"persist-credentials": "false"}, {key: str(item) for key, item in checkouts[0]["with"].items()})

    def test_each_part_reaches_its_shared_steps_at_the_pipeline_s_own_revision(self) -> None:
        for part, shared in SHARED_STEPS.items():
            with self.subTest(part=part):
                uses = [str(step["uses"]) for step in self.steps(part) if "uses" in step]
                self.assertEqual([f"$/{step}" for step in shared], [item for item in uses if not item.startswith("actions/")])
                self.assertNotIn("@v1", self.section(part))
                self.assertNotIn("./", self.section(part))

    def test_each_part_runs_one_job_per_project_of_its_own_configuration_and_a_failing_project_stops_no_other(self) -> None:
        for part in SHARED_STEPS:
            with self.subTest(part=part):
                self.assertEqual({"fail-fast": "false", "matrix": {"project": projects(part)}},
                                 {**self.jobs[part]["strategy"], "fail-fast": str(self.jobs[part]["strategy"]["fail-fast"])})
                section = self.section(part)
                self.assertEqual(1, section.count(".config["), "a job reads the plan's configuration only for its projects")
                self.assertEqual(1, section.count(f".parts['{part}'].config['projects']"), "a job reads no other part's values")
                self.assertNotRegex(section, r"run:.*\$\{\{", "no value is written into a command line")

    def test_each_project_s_job_carries_a_name_that_tells_the_projects_apart(self) -> None:
        label = "${{ matrix.project['runner-label'] && format(', {0}', matrix.project['runner-label']) || '' }}"
        directory = "${{ matrix.project['working-directory'] || '.' }}, ${{ matrix.project['python-version'] }}"
        self.assertEqual(f"{PYTHON_VERIFY} ({directory})", self.jobs[PYTHON_VERIFY]["name"])
        self.assertEqual(f"{PYTHON_TEST} ({directory}{label})", self.jobs[PYTHON_TEST]["name"])
        self.assertEqual(f"{DOTNET_TEST} (${{{{ matrix.project['project'] }}}}{label})", self.jobs[DOTNET_TEST]["name"])

    def test_the_jobs_of_two_projects_of_one_part_do_not_share_a_concurrency_group(self) -> None:
        for part in SHARED_STEPS:
            with self.subTest(part=part):
                self.assertEqual(f"${{{{ fromJSON(needs.plan.outputs.concurrency || '{{}}')['{part}'].group }}}}-${{{{ strategy.job-index }}}}",
                                 self.jobs[part]["concurrency"]["group"])

    def test_python_verify_installs_the_configured_python_and_dependencies_and_runs_the_language_step_from_the_working_directory(self) -> None:
        job = self.jobs[PYTHON_VERIFY]
        self.assertEqual("ubuntu-26.04", job["runs-on"])
        resources, checkout, setup, install, language = self.steps(PYTHON_VERIFY)
        self.assertEqual("resources", resources["id"])
        self.assertRegex(setup["uses"], r"^actions/setup-python@[0-9a-f]{40}$")
        self.assertEqual({"python-version": f"${{{{ {value('python-version')} }}}}"}, setup["with"])
        self.assertEqual(f"${{{{ {value('working-directory')} || '.' }}}}", install["env"]["WORKING_DIRECTORY"])
        self.assertEqual(f"${{{{ {value('install')} }}}}", install["env"]["INSTALL"])
        self.assertIn('python3 "$RESOURCE_PATH/python_tests.py" install --root "$WORKING_DIRECTORY" --install="$INSTALL"', install["run"])
        self.assertEqual({"root": f"${{{{ {value('working-directory')} || '.' }}}}",
                          "format-check": f"${{{{ {value('format-check')} == true }}}}"}, language["with"])
        self.assertNotIn("if", language, "a failed installation skips the checks")

    def test_python_test_installs_the_configured_python_and_dependencies_and_runs_the_tests_on_the_configured_runner(self) -> None:
        job = self.jobs[PYTHON_TEST]
        self.assertEqual(f"${{{{ {value('runner-label')} || 'ubuntu-26.04' }}}}", job["runs-on"])
        self.assertEqual({"run": {"shell": "bash"}}, job["defaults"], "Windows runs the same commands")
        resources, checkout, setup, install, tests = self.steps(PYTHON_TEST)
        self.assertEqual({"python-version": f"${{{{ {value('python-version')} }}}}"}, setup["with"])
        for step in (install, tests):
            self.assertEqual("${{ runner.os == 'Windows' && 'python' || 'python3' }}", step["env"]["PYTHON"])
            self.assertEqual(f"${{{{ {value('working-directory')} || '.' }}}}", step["env"]["WORKING_DIRECTORY"])
            self.assertIn('PYTHONPYCACHEPREFIX="$RUNNER_TEMP/repo-pycache" "$PYTHON" "$RESOURCE_PATH/python_tests.py" ', step["run"])
        self.assertEqual(f"${{{{ {value('install')} }}}}", install["env"]["INSTALL"])
        self.assertIn('install --root "$WORKING_DIRECTORY" --install="$INSTALL"', install["run"])
        self.assertIn('test --root "$WORKING_DIRECTORY"', tests["run"])
        self.assertNotIn("if", tests, "a failed installation skips the tests")
        self.assertNotIn("python-language-step", self.section(PYTHON_TEST), "the language checks are the part python-verify")

    def test_dotnet_test_installs_the_configured_sdk_and_builds_and_tests_the_configured_project_on_the_configured_runner(self) -> None:
        job = self.jobs[DOTNET_TEST]
        self.assertEqual(f"${{{{ {value('runner-label')} || 'ubuntu-26.04' }}}}", job["runs-on"])
        checkout, setup, step = self.steps(DOTNET_TEST)
        self.assertRegex(setup["uses"], r"^actions/setup-dotnet@[0-9a-f]{40}$")
        self.assertEqual({"dotnet-version": f"${{{{ {value('sdk-version')} }}}}"}, setup["with"])
        self.assertEqual("$/actions/dotnet-test", step["uses"])
        self.assertEqual({"project": f"${{{{ {value('project')} }}}}"}, step["with"])

    def test_the_setup_actions_carry_the_pins_of_this_repository_s_other_workflows(self) -> None:
        for action, source in (("actions/setup-python", "python-tests.yml"), ("actions/setup-dotnet", "validate.yml"), ("actions/checkout", "python-tests.yml")):
            with self.subTest(action=action):
                pins = set(re.findall(rf"uses: ({re.escape(action)}@[0-9a-f]{{40}} # v[\d.]+)", self.text))
                self.assertEqual(1, len(pins))
                self.assertIn(pins.pop(), (WORKFLOWS / source).read_text(encoding="utf-8"))


class DotnetTestStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.action = read_workflow(REPOSITORY_ROOT / "actions" / "dotnet-test" / "action.yml")

    def test_the_step_runs_its_script_on_the_job_s_python_with_the_project_of_its_one_input(self) -> None:
        self.assertEqual("composite", self.action["runs"]["using"])
        self.assertEqual(["project"], list(self.action["inputs"]))
        self.assertEqual("true", str(self.action["inputs"]["project"]["required"]))
        (run,) = self.action["runs"]["steps"]
        self.assertEqual("bash", run["shell"])
        self.assertEqual({"RESOURCE_PATH": "${{ github.action_path }}", "PROJECT": "${{ inputs.project }}",
                          "PYTHON": "${{ runner.os == 'Windows' && 'python' || 'python3' }}"}, run["env"])
        self.assertEqual('PYTHONPYCACHEPREFIX="$RUNNER_TEMP/repo-pycache" "$PYTHON" "$RESOURCE_PATH/dotnet_test.py" --project "$PROJECT"', run["run"])

    def test_the_step_installs_no_sdk_and_checks_out_nothing(self) -> None:
        for text in ("uses:", "setup-dotnet", "dotnet-version", "checkout"):
            self.assertNotIn(text, self.text)


class ReusableWorkflowCountTests(unittest.TestCase):
    def test_the_pipeline_reaches_no_more_reusable_workflows_than_the_spec_allows(self) -> None:
        reached = reached_reusable_workflows(tracked_workflows(REPOSITORY_ROOT), PIPELINE_PATH)
        self.assertLessEqual(len(reached), ALLOWED_REUSABLE_WORKFLOWS, f"the pipeline reaches {reached}; a shared part is a job with a shared step")

    def test_no_part_of_the_pipeline_is_a_reusable_workflow_of_its_own(self) -> None:
        workflows = tracked_workflows(REPOSITORY_ROOT)
        jobs = read_workflow(PIPELINE)[1]["jobs"]
        for part in SHARED_STEPS:
            with self.subTest(part=part):
                self.assertTrue((FRAGMENTS / f"{part}.json").is_file())
                self.assertNotIn(f".github/workflows/{part}.yml", workflows)
                self.assertNotIn("uses", jobs[part])
                self.assertIn("steps", jobs[part])

    def test_a_workflow_that_a_job_calls_is_counted_whether_it_is_local_or_of_another_repository(self) -> None:
        caller = "on: workflow_call\njobs:\n  plan:\n    runs-on: ubuntu-26.04\n    steps:\n      - uses: $/actions/pipeline\n"
        for reference, counted in (("$/.github/workflows/python-test.yml", ".github/workflows/python-test.yml"),
                                   ("./.github/workflows/python-test.yml", ".github/workflows/python-test.yml"),
                                   ("octo-org/shared/.github/workflows/test.yml@v1", "octo-org/shared/.github/workflows/test.yml@v1")):
            with self.subTest(reference=reference):
                workflows = {PIPELINE_PATH: caller + f"  python-test:\n    needs: plan\n    uses: {reference}\n"}
                self.assertEqual([counted], reached_reusable_workflows(workflows, PIPELINE_PATH))
        self.assertEqual([], reached_reusable_workflows({PIPELINE_PATH: caller}, PIPELINE_PATH), "a step that uses an action is no reusable workflow")

    def test_a_workflow_that_a_called_workflow_calls_in_turn_is_counted_once(self) -> None:
        workflows = {
            PIPELINE_PATH: "on: workflow_call\njobs:\n  one:\n    uses: $/.github/workflows/one.yml\n  two:\n    uses: $/.github/workflows/two.yml\n",
            ".github/workflows/one.yml": "on: workflow_call\njobs:\n  inner:\n    uses: $/.github/workflows/two.yml\n",
            ".github/workflows/two.yml": "on: workflow_call\njobs:\n  inner:\n    uses: $/.github/workflows/three.yml\n  back:\n    uses: $/.github/workflows/pipeline.yml\n",
            ".github/workflows/three.yml": "on: workflow_call\njobs:\n  unit:\n    runs-on: ubuntu-26.04\n",
        }
        self.assertEqual([".github/workflows/one.yml", ".github/workflows/two.yml", ".github/workflows/three.yml"],
                         reached_reusable_workflows(workflows, PIPELINE_PATH))


class CataloguePageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = {entry["name"]: entry for entry in json.loads(CATALOGUE.read_text(encoding="utf-8"))["parts"]}
        self.surface = json.loads(SURFACE.read_text(encoding="utf-8"))["paths"]

    def test_each_part_has_a_catalogue_page_that_names_its_shared_steps_and_its_schema_fragment(self) -> None:
        for part, shared in SHARED_STEPS.items():
            with self.subTest(part=part):
                entry = self.entries[part]
                self.assertEqual(sorted(shared), sorted(entry["steps"]))
                self.assertEqual([], entry["workflows"], "a part of the pipeline is no shared workflow")
                self.assertEqual([], entry["required_checks"], "the pipeline's verdict is the one required check")
                self.assertIsNone(entry["consumer_kit"])
                self.assertIn(f"parts.{part}", entry["needs"])
                self.assertIn(f"actions/pipeline/schema/v1/parts/{part}.json", entry["needs"])
                self.assertTrue((FRAGMENTS / f"{part}.json").is_file())

    def test_each_shared_step_of_the_parts_is_on_the_shared_surface(self) -> None:
        for step in sorted({step for shared in SHARED_STEPS.values() for step in shared}):
            with self.subTest(step=step):
                self.assertIn(step, self.surface)
                self.assertTrue((REPOSITORY_ROOT / step / "action.yml").is_file())

    def test_a_page_names_nothing_that_exists_only_in_this_repository(self) -> None:
        for part in SHARED_STEPS:
            text = json.dumps(self.entries[part])
            for own in ("tests/", "python_tests", "dotnet_test", "maintaining.md", "docs/agents"):
                with self.subTest(part=part, own=own):
                    self.assertNotIn(own, text)

    def test_the_python_test_page_excludes_a_repository_that_uses_the_suite_selection(self) -> None:
        self.assertIn("suite selection", self.entries[PYTHON_TEST]["applies"])


if __name__ == "__main__":
    unittest.main()
