"""Text contract of the Python, .NET and Node parts of the pipeline: each is a job with read rights only that runs once per
configured project, takes its values from the plan and runs a shared step of the pipeline's own revision, and none is a
reusable workflow of its own; the pipeline reaches one reusable workflow, the suite jobs."""

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
PYTHON_VERIFY, PYTHON_TEST, DOTNET_TEST, NODE_TEST = "python-verify", "python-test", "dotnet-test", "node-test"
# Each part with the shared steps its job reaches at the pipeline's own revision, in the job's order; node-test reaches
# its step twice, to check the project before Node is installed and to run its npm commands after.
SHARED_STEPS = {
    PYTHON_VERIFY: ["actions/python-tests", "actions/python-language-step"],
    PYTHON_TEST: ["actions/python-tests"],
    DOTNET_TEST: ["actions/dotnet-test"],
    NODE_TEST: ["actions/node-test", "actions/node-test"],
}
# The one setting that names a cache and is allowed: it switches off the cache that actions/setup-node restores by itself
# when package.json names packageManager.
NO_PACKAGE_MANAGER_CACHE = "package-manager-cache: false"
# The pipeline calls one reusable workflow, the suite jobs, and that one calls none: a caller, the pipeline and the suite
# jobs keep the pipeline two levels deep, as the spec states. The suite jobs are a workflow and not jobs of the pipeline
# because the full recording of the automation workflow runs the same jobs, so their steps stand once.
ALLOWED_REUSABLE_WORKFLOWS = 1
SUITE_JOBS = ".github/workflows/suite-jobs.yml"
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
                if part == NODE_TEST:
                    lines = [line.strip() for line in section.splitlines() if "cache" in line.lower()]
                    self.assertEqual([NO_PACKAGE_MANAGER_CACHE], lines, "the one cache setting of node-test switches setup-node's cache off")
                    section = "\n".join(line for line in section.splitlines() if line.strip() != NO_PACKAGE_MANAGER_CACHE)
                for forbidden in ("secrets.", "github.token", "GITHUB_TOKEN", "inputs.", "vars.", "actions/cache", "cache:", "cache-mode", "PIP_CACHE_DIR"):
                    self.assertNotIn(forbidden, section)
                checkouts = [step for step in self.steps(part) if str(step.get("uses", "")).startswith("actions/checkout@")]
                self.assertEqual(1, len(checkouts))
                self.assertEqual({"ref": "${{ github.event_name == 'repository_dispatch' && github.event.client_payload.sha || '' }}", "persist-credentials": "false"}, {key: str(item) for key, item in checkouts[0]["with"].items()},
                                 "without credentials, at the dispatched commit on a dependabot-merged run")

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
        self.assertEqual(f"{NODE_TEST} (${{{{ matrix.project['working-directory'] }}}}{label})", self.jobs[NODE_TEST]["name"])

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

    def test_node_test_checks_the_project_installs_the_node_of_its_nvmrc_and_runs_the_npm_commands_on_the_configured_runner(self) -> None:
        job = self.jobs[NODE_TEST]
        self.assertEqual(f"${{{{ {value('runner-label')} || 'ubuntu-26.04' }}}}", job["runs-on"])
        checkout, check, setup, step = self.steps(NODE_TEST)
        directory = f"${{{{ {value('working-directory')} }}}}"
        self.assertEqual("$/actions/node-test", check["uses"])
        self.assertEqual({"command": "check", "working-directory": directory}, check["with"], "a missing .nvmrc fails before setup-node reads it")
        self.assertRegex(setup["uses"], r"^actions/setup-node@[0-9a-f]{40}$")
        self.assertEqual({"node-version-file": f"{directory}/.nvmrc", "package-manager-cache": "false"}, {key: str(item).lower() if isinstance(item, bool) else str(item) for key, item in setup["with"].items()},
                         "the Node version of the project's .nvmrc, and no cache")
        self.assertEqual("$/actions/node-test", step["uses"])
        self.assertEqual({"command": "test", "working-directory": directory}, step["with"])
        self.assertNotIn("if", step, "a failed check or setup skips the npm commands")

    def test_the_setup_actions_carry_the_pins_of_this_repository_s_other_workflows(self) -> None:
        for action, source in (("actions/setup-python", "python-tests.yml"), ("actions/setup-dotnet", "suite-jobs.yml"), ("actions/setup-node", "suite-jobs.yml"),
                               ("actions/checkout", "python-tests.yml")):
            with self.subTest(action=action):
                pins = set(re.findall(rf"uses: ({re.escape(action)}@[0-9a-f]{{40}} # v[\d.]+)", self.text))
                self.assertEqual(1, len(pins))
                self.assertIn(pins.pop(), (WORKFLOWS / source).read_text(encoding="utf-8"))


CONFIGURATION = REPOSITORY_ROOT / ".github" / "ci-config.yml"
# The paths that every fixture part names besides its fixture and its part folders: the plan program and the schema, the
# pipeline and this repository's caller of it.
COMMON_PATHS = ["actions/pipeline/", ".github/workflows/pipeline.yml", ".github/workflows/ci.yml"]
# A part folder that a shared step's own run block reaches beside its own folder, such as the pins of repository-hygiene.
SIBLING_FOLDER = re.compile(r"\$RESOURCE_PATH/\.\./([\w.-]+)/")


def fixture_folders(part: str, settings: dict) -> list[str]:
    """The folder of each configured project of a part: its working directory, or the folder of its .NET project."""
    folders = []
    for project in settings.get("projects", []):
        folder = project["project"].rpartition("/")[0] if part == DOTNET_TEST else project.get("working-directory", "")
        if folder.strip("./"):
            folders.append(folder.rstrip("/") + "/")
    return folders


def loaded_part_folders(job: dict) -> list[str]:
    """The part folders that a job loads through `$/`, with each folder that the loaded step's run block reaches beside it."""
    folders = []
    for step in job["steps"]:
        uses = str(step.get("uses", ""))
        if uses.startswith("$/actions/"):
            folders.append(uses[2:].rstrip("/") + "/")
            text = (REPOSITORY_ROOT / uses[2:] / "action.yml").read_text(encoding="utf-8")
            folders += [f"actions/{sibling}/" for sibling in SIBLING_FOLDER.findall(text)]
    return list(dict.fromkeys(folders))


def missing_paths(part: str, settings: dict, job: dict) -> list[str]:
    """Each path that a change can reach the part's job through and that the part's `paths` do not cover, named with the part."""
    paths = [str(item) for item in settings.get("paths", [])]
    required = list(dict.fromkeys(fixture_folders(part, settings) + loaded_part_folders(job) + COMMON_PATHS))
    return [f"{part}: paths lack {path}" for path in required
            if not any(entry == path or (entry.endswith("/") and path.startswith(entry)) for entry in paths)]


class FixturePartPathsTests(unittest.TestCase):
    """This repository's configuration file runs the four parts on its fixtures only for a change that can reach them, so
    their `paths` must name every path through which a change reaches a part's job."""

    def setUp(self) -> None:
        self.parts = ci_definition().read_yaml(CONFIGURATION.read_text(encoding="utf-8"), str(CONFIGURATION))["parts"]
        self.jobs = read_workflow(PIPELINE)[1]["jobs"]

    def test_the_configuration_file_states_paths_for_each_fixture_part(self) -> None:
        for part in SHARED_STEPS:
            with self.subTest(part=part):
                self.assertTrue(self.parts[part].get("paths"), f"{part} states no paths")

    def test_the_paths_of_each_fixture_part_name_its_fixture_its_part_folders_the_plan_and_the_pipeline(self) -> None:
        for part in SHARED_STEPS:
            with self.subTest(part=part):
                self.assertEqual([], missing_paths(part, self.parts[part], self.jobs[part]))

    def test_a_missing_path_is_named_with_its_part(self) -> None:
        for part, path in ((PYTHON_VERIFY, "tests/fixtures/python_tests/"), (PYTHON_VERIFY, "actions/python-language-step/"),
                           (PYTHON_VERIFY, "actions/repository-hygiene/"), (PYTHON_TEST, "actions/python-tests/"), (DOTNET_TEST, "tests/fixtures/dotnet_test/"),
                           (DOTNET_TEST, "actions/dotnet-test/"), (DOTNET_TEST, "actions/pipeline/"), (PYTHON_TEST, ".github/workflows/pipeline.yml"),
                           (PYTHON_TEST, ".github/workflows/ci.yml"), (NODE_TEST, "tests/fixtures/node_test/"), (NODE_TEST, "actions/node-test/")):
            with self.subTest(part=part, path=path):
                settings = {**self.parts[part], "paths": [item for item in self.parts[part]["paths"] if item != path]}
                self.assertEqual([f"{part}: paths lack {path}"], missing_paths(part, settings, self.jobs[part]))

    def test_a_folder_that_holds_a_required_path_covers_it(self) -> None:
        settings = {**self.parts[PYTHON_TEST], "paths": ["tests/", "actions/", ".github/workflows/"]}
        self.assertEqual([], missing_paths(PYTHON_TEST, settings, self.jobs[PYTHON_TEST]))


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


class NodeTestStepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text, self.action = read_workflow(REPOSITORY_ROOT / "actions" / "node-test" / "action.yml")

    def test_the_step_runs_its_script_on_the_job_s_python_with_the_command_and_the_working_directory_of_its_inputs(self) -> None:
        self.assertEqual("composite", self.action["runs"]["using"])
        self.assertEqual(["command", "working-directory"], list(self.action["inputs"]))
        for name in ("command", "working-directory"):
            self.assertEqual("true", str(self.action["inputs"][name]["required"]))
        check, test, unknown = self.action["runs"]["steps"]
        for step, command in ((check, "check"), (test, "test")):
            with self.subTest(command=command):
                self.assertEqual(f"inputs.command == '{command}'", step["if"])
                self.assertEqual("bash", step["shell"])
                self.assertEqual({"RESOURCE_PATH": "${{ github.action_path }}", "WORKING_DIRECTORY": "${{ inputs.working-directory }}",
                                  "PYTHON": "${{ runner.os == 'Windows' && 'python' || 'python3' }}"}, step["env"])
                self.assertEqual(f'PYTHONPYCACHEPREFIX="$RUNNER_TEMP/repo-pycache" "$PYTHON" "$RESOURCE_PATH/node_test.py" {command} --working-directory "$WORKING_DIRECTORY"',
                                 step["run"])
        self.assertEqual("inputs.command != 'check' && inputs.command != 'test'", unknown["if"], "another command fails the step")
        self.assertIn("exit 2", unknown["run"])

    def test_the_step_installs_no_node_and_checks_out_nothing(self) -> None:
        for text in ("uses:", "setup-node", "node-version", "checkout"):
            self.assertNotIn(text, self.text)


class ReusableWorkflowCountTests(unittest.TestCase):
    def test_the_pipeline_reaches_no_more_reusable_workflows_than_the_spec_allows(self) -> None:
        reached = reached_reusable_workflows(tracked_workflows(REPOSITORY_ROOT), PIPELINE_PATH)
        self.assertLessEqual(len(reached), ALLOWED_REUSABLE_WORKFLOWS, f"the pipeline reaches {reached}; a shared part is a job with a shared step")
        self.assertEqual([SUITE_JOBS], reached, "the suite jobs, which call no workflow in turn")
        self.assertEqual([], reached_reusable_workflows(tracked_workflows(REPOSITORY_ROOT), SUITE_JOBS))

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
                self.assertEqual(sorted(set(shared)), sorted(entry["steps"]))
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
            for own in ("tests/", "python_tests", "dotnet_test", "node_test", "maintaining.md", "docs/agents"):
                with self.subTest(part=part, own=own):
                    self.assertNotIn(own, text)

    def test_the_python_test_page_excludes_a_repository_that_uses_the_suite_selection(self) -> None:
        self.assertIn("suite selection", self.entries[PYTHON_TEST]["applies"])


if __name__ == "__main__":
    unittest.main()
