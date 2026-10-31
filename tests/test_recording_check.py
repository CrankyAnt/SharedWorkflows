"""The recording step's start, exec and check on fixture repositories, against the stand-in strace and the stored strace
recordings of the fixtures' commands."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.recording_fixtures import STRACE_RECORDINGS, RecordingFixture, StoredFixture, load_recording, requires_linux, suite
from tests.strace_stand_in import BASE, TMPDIR, detokenize

READER = """\
    from pathlib import Path
    print(Path("lib/data.txt").read_text())
    """

TWO_READER = "from pathlib import Path\nprint(Path('lib/data.txt').read_text(), Path('lib/other.txt').read_text())\n"
FILE_READER = "print(open('tests/{0}.py').read())\n"
PROBER = "import os\nprint(os.access('Directory.Build.props', os.F_OK))\n"
SETUP = ("python3", "-c", "print(open('requirements.txt').read())")
# A stand-in dotnet. Given a project file, it reads it and NuGet.Config, opens an absent Directory.Build.props and probes
# app/global.json through the lstat system call alone, as .NET's existence reads do on Linux; otherwise it reads nothing.
DOTNET = """\
import ctypes, sys
if sys.argv[2:3] == ["app/App.csproj"]:
    print(open(sys.argv[2]).read(), open("NuGet.Config").read())
    try:
        open("app/Directory.Build.props")
    except OSError:
        pass
    ctypes.CDLL(None, use_errno=True).syscall(6, b"app/global.json", ctypes.create_string_buffer(256))
"""
BUILDER = f"""\
    import subprocess, sys, tempfile
    from pathlib import Path
    program = Path(tempfile.mkdtemp()) / "dotnet"
    program.write_text("#!" + sys.executable + "\\n" + {DOTNET!r})
    program.chmod(0o755)
    subprocess.run([str(program), "build", "app/App.csproj"], check=True)
    subprocess.run([str(program), "--version"], check=True, cwd="tools")
    subprocess.run([str(program), "build", "src/App"], check=True)
    """

READING = StoredFixture("check-reader", {"tests/a.py": READER, "lib/data.txt": "data\n"}, {"a": ("python3", "tests/a.py")})
READING_TWO = StoredFixture("check-two-reader", {"tests/a.py": TWO_READER, "lib/data.txt": "data\n", "lib/other.txt": "other\n", "lib/gone.txt": ""},
                            {"a": ("python3", "tests/a.py")})
SETUPS = StoredFixture("check-setups", {"requirements.txt": "dependency\n", **{f"tests/{name}.py": FILE_READER.format(name) for name in ("a", "b", "c1", "c2")}},
                       {"deps": SETUP, **{name: ("python3", f"tests/{name}.py") for name in ("a", "b", "c1", "c2")}})
PROBING = StoredFixture("check-prober", {"tests/a.py": PROBER}, {"a": ("python3", "tests/a.py")})
TOOLCHAIN = StoredFixture("check-toolchain", {"tests/build.py": BUILDER, "app/App.csproj": "<Project />\n", "NuGet.Config": "<configuration />\n",
                                              "tools/readme.txt": "", "src/App/Program.cs": ""},
                          {"build": ("python3", "tests/build.py")})
ENVIRONMENT_READER = ("import json, os, shutil\n"
                      "print(json.dumps({**{name: shutil.which(name) for name in ('alpha', 'beta')},"
                      " **{name: os.environ.get(name) for name in ('ALPHA_TOOLS', 'BETA_TOOLS', 'FIXTURE_PROFILE', 'UNPROVIDED')}}))\n")


def setup_location(fixture: RecordingFixture, name: str) -> Path:
    """A setup's own location with one tool of the setup's name."""
    location = fixture.base / "setups" / name
    location.mkdir(parents=True)
    tool = location / name
    tool.write_text("#!/bin/sh\n", encoding="utf-8")
    tool.chmod(0o755)
    return location


# The fixtures whose commands python3 -m tests.record_strace_fixture records.
STORED = (READING, READING_TWO, SETUPS, PROBING, TOOLCHAIN)
# The names that the .NET probe rules add in each directory of a project file, of a working directory and of a
# directory argument.
DOTNET_PROBES = ["Directory.Build.props", "Directory.Build.rsp", "Directory.Build.targets", "Directory.Packages.props", "NuGet.Config",
                 "NuGet.config", "global.json", "nuget.config"]
WORKING_PROBES = ["NuGet.Config", "NuGet.config", "global.json", "nuget.config"]
ARGUMENT_PROBES = [*DOTNET_PROBES, "Directory.Solution.props", "Directory.Solution.targets"]
# The existence reads of the three stand-in dotnet runs: the project file app/App.csproj from the checkout root, whose
# NuGet.Config it reads; no project in tools/; the directory argument src/App.
DOTNET_READS = [{"path": path, "kind": "any"} for path in sorted([
    *(name for name in ARGUMENT_PROBES if name != "NuGet.Config"), *(f"app/{name}" for name in DOTNET_PROBES),
    *(f"tools/{name}" for name in WORKING_PROBES), *(f"{directory}{name}" for directory in ("src/", "src/App/") for name in ARGUMENT_PROBES)])]


@requires_linux
class RecordingCheckTests(unittest.TestCase):
    def test_exec_runs_the_entry_under_strace_with_the_mid_trace_set_without_root(self) -> None:
        fixture = READING.fixture(self.addCleanup, [suite("a", ["tests/a.py"], paths=["tests/a.py", "lib/data.txt"])])
        self.assertEqual(0, fixture.start().returncode)
        ran = fixture.exec_entry("tests/a.py")
        self.assertEqual(0, ran.returncode, ran.stderr)
        self.assertIn("data", ran.stdout)
        self.assertNotEqual(0, os.geteuid())
        [unit] = [json.loads(path.read_text(encoding="utf-8")) for path in (fixture.output / "units").glob("*.json")]
        recorder = unit["recorder"]
        self.assertEqual(["strace", "-f", "--seccomp-bpf"], recorder[:3])
        traced = set(recorder[recorder.index("-e") + 1].removeprefix("trace=").replace("?", "").split(","))
        self.assertEqual({"open", "openat", "openat2", "execve", "execveat", "getdents", "getdents64", "access", "faccessat",
                          "faccessat2", "newfstatat", "statx", "chdir", "fchdir", "clone", "clone3", "fork", "vfork"}, traced)
        trace = str(fixture.output / "traces" / f"{unit['unit']}.strace")
        self.assertEqual([[*recorder[1:recorder.index("-e")], "-o", trace, *recorder[recorder.index("-e"):], "--", "python3", "tests/a.py"]],
                         fixture.stand_in_calls())

    def test_setup_runs_under_strace_and_returns_its_exit_code(self) -> None:
        fixture = RecordingFixture(self.addCleanup, {"lib/data.txt": "data\n"}, [suite("a", ["lib/data.txt"], paths=[])])
        fixture.start()
        ran = fixture.exec_setup("deps", "python3", "-c", "import sys; sys.exit(3)")
        self.assertEqual(3, ran.returncode)
        [call] = fixture.stand_in_calls()
        self.assertEqual(["--", "python3", "-c", "import sys; sys.exit(3)"], call[call.index("--"):])

    def test_check_passes_when_every_read_lies_inside_the_stored_lists(self) -> None:
        # The entry file's imports look up modules in its directory tests/.
        fixture = READING.fixture(self.addCleanup, [suite("a", ["tests/a.py"], paths=["tests/*.py", "lib/**"], lookup_directories=["tests"])])
        checked = fixture.record(["tests/a.py"])
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        self.assertEqual([], fixture.additions()["suites"])
        self.assertIn("lies inside the stored lists", fixture.summary.read_text(encoding="utf-8"))

    def test_check_fails_names_the_exact_additions_and_apply_makes_the_same_recording_pass(self) -> None:
        fixture = READING_TWO.fixture(self.addCleanup, [suite("a", ["tests/a.py"], paths=["tests/a.py", "lib/data.txt", "lib/gone.txt"])])
        checked = fixture.record(["tests/a.py"])
        self.assertEqual(1, checked.returncode, checked.stdout + checked.stderr)
        self.assertEqual({"kind": "additions", "suite_list": "suites.json", "suites": [
            {"suite": "a", "create": False, "recorded_entries": ["tests/a.py"],
             "reads": {"paths": ["lib/other.txt"], "existence_reads": [], "directory_memberships": [], "lookup_directories": ["tests"],
                       "module_lookups": {}}}]}, fixture.additions())
        self.assertIn("`lib/other.txt`", checked.stdout)
        summary = fixture.summary.read_text(encoding="utf-8")
        self.assertIn("| `a` | path `lib/other.txt` |", summary)
        self.assertIn('section "Additions" of the suite-selection consumer kit', summary, "a consumer's route")
        self.assertNotIn("actions/recording", summary, "no path into the tree of the shared parts")
        applied = fixture.apply()
        self.assertEqual(0, applied.returncode, applied.stderr)
        self.assertEqual(["lib/data.txt", "lib/gone.txt", "lib/other.txt", "tests/a.py"], fixture.suite("a")["paths"])
        self.assertEqual(["tests"], fixture.suite("a")["lookup_directories"])
        again = fixture.check()
        self.assertEqual(0, again.returncode, again.stdout + again.stderr)

    def test_a_pull_request_run_keeps_a_stored_read_that_no_run_makes(self) -> None:
        fixture = READING.fixture(self.addCleanup, [suite("a", ["tests/a.py"], paths=["tests/a.py", "lib/gone.txt"], directory_memberships=["lib"],
                                                          existence_reads=[{"path": "old", "kind": "any"}])],
                                  {"lib/gone.txt": "", "lib/new.txt": ""})
        checked = fixture.record(["tests/a.py"])
        self.assertEqual(1, checked.returncode)
        self.assertEqual(0, fixture.apply().returncode)
        stored = fixture.suite("a")
        self.assertEqual(["lib/data.txt", "lib/gone.txt", "tests/a.py"], stored["paths"])
        self.assertEqual(["lib"], stored["directory_memberships"])
        self.assertEqual([{"path": "old", "kind": "any"}], stored["existence_reads"])

    def test_check_assigns_a_setups_reads_to_the_suites_and_entry_files_in_the_shard_that_state_it(self) -> None:
        fixture = SETUPS.fixture(self.addCleanup, [
            suite("whole-with-setup", ["tests/a.py"], paths=["tests/a.py"], setups=["deps"]),
            suite("whole-without-setup", ["tests/b.py"], paths=["tests/b.py"]),
            suite("per-entry", ["tests/c1.py", "tests/c2.py"], entry_selection=True, paths=[],
                  entry_paths={"tests/c1.py": ["tests/c1.py"], "tests/c2.py": ["tests/c2.py"]}, entry_setups={"tests/c1.py": ["deps"]}),
        ])
        fixture.start()
        self.assertEqual(0, fixture.exec_setup("deps", *SETUP).returncode)
        for entry in ("tests/a.py", "tests/b.py", "tests/c1.py", "tests/c2.py"):
            self.assertEqual(0, fixture.exec_entry(entry).returncode)
        checked = fixture.check()
        self.assertEqual(1, checked.returncode, checked.stdout + checked.stderr)
        changes = {item["suite"]: item for item in fixture.additions()["suites"]}
        self.assertEqual(["whole-with-setup", "per-entry"], [name for name in ("whole-with-setup", "whole-without-setup", "per-entry") if name in changes])
        self.assertEqual(["requirements.txt"], changes["whole-with-setup"]["reads"]["paths"])
        self.assertEqual({"tests/c1.py"}, set(changes["per-entry"]["entries"]))
        self.assertEqual(["requirements.txt"], changes["per-entry"]["entries"]["tests/c1.py"]["paths"])

    def test_an_extra_existence_read_covers_a_recorded_probe_of_an_absent_file(self) -> None:
        for extra, code in (([], 1), ([{"path": "Directory.Build.props", "kind": "any"}], 0)):
            with self.subTest(extra=extra):
                fixture = PROBING.fixture(self.addCleanup, [suite("a", ["tests/a.py"], paths=["tests/a.py"], extra_existence_reads=extra)])
                checked = fixture.record(["tests/a.py"])
                self.assertEqual(code, checked.returncode, checked.stdout + checked.stderr)
                if code:
                    self.assertIn("existence `Directory.Build.props`", checked.stdout)

    def test_a_pull_request_run_adds_the_dotnet_probe_rules_that_the_mid_trace_set_misses(self) -> None:
        fixture = TOOLCHAIN.fixture(self.addCleanup, [suite("a", ["tests/build.py"], paths=["tests/build.py", "app/App.csproj", "NuGet.Config"])])
        checked = fixture.record(["tests/build.py"])
        self.assertEqual(1, checked.returncode, checked.stdout + checked.stderr)
        traces = "".join(path.read_text(encoding="utf-8") for path in (fixture.output / "traces").glob("*.strace"))
        self.assertIn('"app/App.csproj"', traces)
        self.assertNotIn("app/global.json", traces)
        [change] = fixture.additions()["suites"]
        self.assertEqual([], change["reads"]["paths"])
        self.assertEqual(DOTNET_READS, change["reads"]["existence_reads"])
        self.assertEqual(0, fixture.apply().returncode)
        self.assertEqual(0, fixture.check().returncode)

    def test_a_full_recording_holds_the_dotnet_probe_rules_once_beside_the_probes_it_sees(self) -> None:
        fixture = TOOLCHAIN.fixture(self.addCleanup, [suite("a", ["tests/build.py"], paths=[])])
        checked = fixture.record(["tests/build.py"], full=True)
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        traces = "".join(path.read_text(encoding="utf-8") for path in (fixture.output / "traces").glob("*.strace"))
        self.assertIn('lstat("app/global.json"', traces)
        reads = fixture.reads_of("tests/build.py")
        self.assertEqual(["NuGet.Config", "app/App.csproj", "tests/build.py"], reads["paths"])
        # Beside them the recording holds the interpreter's stat of its script directory.
        self.assertEqual(sorted([*DOTNET_READS, {"path": "tests", "kind": "directory"}], key=lambda read: read["path"]), reads["existence_reads"])

    def test_check_requires_a_stored_list_for_a_suite_without_one(self) -> None:
        fixture = READING.fixture(self.addCleanup, [suite("a", ["tests/a.py"])])
        checked = fixture.record(["tests/a.py"])
        self.assertEqual(1, checked.returncode)
        self.assertTrue(fixture.additions()["suites"][0]["create"])
        self.assertEqual(0, fixture.apply().returncode)
        self.assertIn("lib/data.txt", fixture.suite("a")["paths"])
        self.assertEqual(0, fixture.check().returncode)

    def test_an_entry_file_sees_the_job_s_environment_before_its_setups_and_only_what_its_stated_setups_provide(self) -> None:
        fixture = RecordingFixture(self.addCleanup, {"tests/a.py": ENVIRONMENT_READER, "tests/b.py": ENVIRONMENT_READER}, [
            suite("states-alpha", ["tests/a.py"], paths=[], setups=["alpha"]),
            suite("states-none", ["tests/b.py"], paths=[]),
        ])
        alpha, beta = (setup_location(fixture, name) for name in ("alpha", "beta"))
        with mock.patch.dict(os.environ, {"FIXTURE_PROFILE": "job"}):
            self.assertEqual(0, fixture.start().returncode)
        for name, location in (("alpha", alpha), ("beta", beta)):
            provided = fixture.provide(name, "--path", str(location), "--variable", f"{name.upper()}_TOOLS={location}")
            self.assertEqual(0, provided.returncode, provided.stderr)
        self.assertEqual(0, fixture.provide("alpha", "--variable", f"FIXTURE_PROFILE={alpha}").returncode)
        # After start the setups change the job's environment, as GITHUB_PATH and GITHUB_ENV do: they put their tool
        # directories and a strace of their own on PATH and set variables, provided or not.
        seen = {}
        for entry in ("tests/a.py", "tests/b.py"):
            ran = self.run_in_changed_job(fixture, [beta, alpha], entry)
            self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
            seen[entry] = json.loads(ran.stdout.strip().splitlines()[-1])
        self.assertEqual({"alpha": str(alpha / "alpha"), "beta": None, "ALPHA_TOOLS": str(alpha), "BETA_TOOLS": None,
                          "FIXTURE_PROFILE": str(alpha), "UNPROVIDED": None}, seen["tests/a.py"])
        self.assertEqual({"alpha": None, "beta": None, "ALPHA_TOOLS": None, "BETA_TOOLS": None, "FIXTURE_PROFILE": "job",
                          "UNPROVIDED": None}, seen["tests/b.py"])

    def test_start_takes_the_job_s_environment_from_the_snapshot_that_base_wrote_before_the_setups(self) -> None:
        fixture = RecordingFixture(self.addCleanup, {"tests/b.py": ENVIRONMENT_READER}, [suite("states-none", ["tests/b.py"], paths=[])])
        snapshot = fixture.base / "environment.json"
        with mock.patch.dict(os.environ, {"FIXTURE_PROFILE": "job"}):
            self.assertEqual(0, fixture.run("base", "--file", str(snapshot)).returncode)
        with mock.patch.dict(os.environ, {"FIXTURE_PROFILE": "set up", "UNPROVIDED": "leak"}):
            started = fixture.run("start", "--output", str(fixture.output), "--environment", str(snapshot))
        self.assertEqual(0, started.returncode, started.stderr)
        ran = self.run_in_changed_job(fixture, [], "tests/b.py")
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertEqual({"alpha": None, "beta": None, "ALPHA_TOOLS": None, "BETA_TOOLS": None, "FIXTURE_PROFILE": "job", "UNPROVIDED": None},
                         json.loads(ran.stdout.strip().splitlines()[-1]))

    def test_start_refuses_to_record_on_another_interpreter_than_the_snapshot_s_own(self) -> None:
        fixture = RecordingFixture(self.addCleanup, {"tests/b.py": ""}, [suite("states-none", ["tests/b.py"], paths=[])])
        snapshot = fixture.base / "environment.json"
        other = setup_location(fixture, "python3")
        self.assertEqual(0, fixture.run_with_path(os.pathsep.join([str(other), os.environ.get("PATH", "")]), "base", "--file", str(snapshot)).returncode)
        started = fixture.run("start", "--output", str(fixture.output), "--environment", str(snapshot))
        self.assertEqual(2, started.returncode, started.stdout + started.stderr)
        self.assertIn("python3", started.stderr)

    @staticmethod
    def run_in_changed_job(fixture: RecordingFixture, directories: list[Path], entry: str):
        """Run ``entry`` under exec in a job whose setups put ``directories`` and a failing strace first on PATH and set
        variables."""
        failing = fixture.base / "failing-strace"
        if not failing.is_dir():
            failing.mkdir()
            (failing / "strace").write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
            (failing / "strace").chmod(0o755)
        job_path = os.pathsep.join([*map(str, directories), str(failing), str(fixture.stand_in / "bin"), os.environ.get("PATH", "")])
        changed = {"BETA_TOOLS": str(fixture.base / "setups" / "beta"), "FIXTURE_PROFILE": "set up", "UNPROVIDED": "leak"}
        with mock.patch.dict(os.environ, changed):
            return fixture.run_with_path(job_path, "exec", "--output", str(fixture.output), "--entry", entry, "--suites", "suites.json",
                                         "--", "python3", entry)

    def test_exec_refuses_an_entry_file_that_states_a_setup_the_job_did_not_install(self) -> None:
        fixture = RecordingFixture(self.addCleanup, {"tests/a.py": ENVIRONMENT_READER}, [suite("a", ["tests/a.py"], paths=[], setups=["alpha"])])
        self.assertEqual(0, fixture.start().returncode)
        ran = fixture.exec_entry("tests/a.py")
        self.assertEqual(2, ran.returncode, ran.stdout + ran.stderr)
        self.assertIn("the setup alpha that tests/a.py states was not installed in this job", ran.stderr)

    def test_a_setup_that_exec_ran_counts_as_installed_and_provides_nothing_by_itself(self) -> None:
        fixture = SETUPS.fixture(self.addCleanup, [suite("a", ["tests/a.py"], paths=["tests/a.py"], setups=["deps"])])
        self.assertEqual(0, fixture.start().returncode)
        self.assertEqual(0, fixture.exec_setup("deps", *SETUP).returncode)
        ran = fixture.exec_entry("tests/a.py")
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)

    def test_provide_refuses_a_tool_directory_that_does_not_exist(self) -> None:
        fixture = RecordingFixture(self.addCleanup, {"tests/a.py": ""}, [suite("a", ["tests/a.py"], paths=[])])
        self.assertEqual(0, fixture.start().returncode)
        provided = fixture.provide("alpha", "--path", str(fixture.base / "missing"))
        self.assertEqual(2, provided.returncode)
        self.assertIn("missing", provided.stderr)

    def test_start_fails_with_a_clear_message_when_strace_is_missing(self) -> None:
        fixture = RecordingFixture(self.addCleanup, {"lib/data.txt": ""}, [suite("a", ["lib/data.txt"], paths=[])])
        started = fixture.run_with_path("/nonexistent", "start", "--output", str(fixture.output))
        self.assertEqual(2, started.returncode)
        self.assertIn("strace is not on PATH", started.stderr)


# The existence reads that the .NET probe rules give the stand-in dotnet's project file app/App.csproj when it resolves
# against the recording's start directory instead of the fixture.
START_DIRECTORY_PROBES = [*(name for name in ARGUMENT_PROBES if name.startswith("Directory.") and "Solution" not in name),
                          *(f"app/{name}" for name in DOTNET_PROBES)]


class VforkChildWorkingDirectoryTests(unittest.TestCase):
    """The stored trace of the stand-in dotnet starts of TOOLCHAIN, parsed as an outer recording parses it: the
    recording starts in the checkout, and the fixture lies outside it."""

    CHECKOUT, BASE = "/checkout", "/elsewhere/recording-fixture"

    def lines(self) -> list[str]:
        trace = STRACE_RECORDINGS / "commands" / TOOLCHAIN.name / "build.strace"
        return detokenize(trace.read_text(encoding="utf-8"), {TMPDIR: self.BASE + "/tmp", BASE: self.BASE}).splitlines(keepends=True)

    def parsed(self, lines: list[str]) -> list:
        recording = load_recording()
        trace = Path(self.enterContext(tempfile.TemporaryDirectory())) / "build.strace"
        trace.write_text("".join(lines), encoding="utf-8")
        return recording.parse_trace(trace, (self.CHECKOUT, self.BASE), self.CHECKOUT)

    def child_first(self, lines: list[str]) -> list[str]:
        """``lines`` with each vfork child's return of its execve written before its parent's clone return."""
        lines = list(lines)
        for index in range(len(lines) - 1):
            if "clone3 resumed>" in lines[index] or "vfork resumed>" in lines[index]:
                if "execve resumed>" in lines[index + 1]:
                    lines[index], lines[index + 1] = lines[index + 1], lines[index]
        return lines

    def test_a_vfork_child_gets_its_parents_working_directory_whichever_return_strace_writes_first(self) -> None:
        recording = load_recording()
        lines = self.lines()
        self.assertNotEqual(lines, self.child_first(lines))
        parent_first, swapped = self.parsed(lines), self.parsed(self.child_first(lines))
        self.assertEqual(parent_first, swapped)
        starts = [(event.arguments[1:], event.cwd) for event in swapped if event.kind == "exec" and event.executable == "dotnet"]
        self.assertEqual([(("build", "app/App.csproj"), f"{self.BASE}/repo"), (("--version",), f"{self.BASE}/repo/tools"),
                          (("build", "src/App"), f"{self.BASE}/repo")], starts)
        # A checkout without tracked files: the probe rules place a project file by its argument alone.
        project = recording.Checkout(Path(self.CHECKOUT), frozenset(), frozenset({""}), recording.suite_selection.IgnoreRules({}))
        recorded = recording.Start("python3", self.CHECKOUT, (f"{self.CHECKOUT}/tests/build.py",))
        for events in (parent_first, swapped):
            unit = recording.UnitRecording(events, f"{self.BASE}/tmp", project, self.CHECKOUT)
            derived = recording.derived_existence_reads(unit, recorded)
            self.assertEqual([], [path for path in START_DIRECTORY_PROBES if path in derived])

    def test_a_vfork_childs_opens_and_stats_before_its_parents_return_resolve_in_the_parents_working_directory(self) -> None:
        # The stand-in dotnet's first start, with an open and a stat by relative name that the child makes before its
        # execve, written before the parent's clone return.
        lines = self.lines()
        start = next(index for index, line in enumerate(lines) if "clone3(" in line)
        parent, child = lines[start].split(" ", 1)[0], lines[start + 1].split(" ", 1)[0]
        own = [f"{child} open(\"NuGet.Config\", O_RDONLY|O_CLOEXEC) = 3</{self.BASE}/repo/NuGet.Config>\n",
               f"{child} stat(\"app/global.json\", 0x7ffd0) = -1 ENOENT (No such file or directory)\n"]
        excerpt = [*lines[:start + 1], *own, *lines[start + 1:start + 4]]
        self.assertTrue(excerpt[-2].startswith(f"{parent} ") and "clone3 resumed" in excerpt[-2])
        for order in (excerpt, self.child_first(excerpt)):
            events = [(event.kind, event.path, event.cwd) for event in self.parsed(order) if event.pid == child]
            self.assertEqual([("read", f"{self.BASE}/repo/NuGet.Config", f"{self.BASE}/repo"),
                              ("absent", f"{self.BASE}/repo/app/global.json", f"{self.BASE}/repo"),
                              ("exec", f"{self.BASE}/tmp/tmp2qtajhls/dotnet", f"{self.BASE}/repo")], events)


class CheckSummaryRouteTests(unittest.TestCase):
    """The route that the job summary of a failed check names: the consumer kit's in a consumer, the maintenance
    documentation's in the repository of the shared parts."""

    LINES = [("`a`", "path `lib/other.txt`")]
    CONSUMER = frozenset({"src/app.py", ".github/suites.json"})

    def summary(self, tracked: frozenset[str], run_id: str | None, event: str | None) -> str:
        recording = load_recording()
        return recording.check_summary(self.LINES, 1, recording.additions_route("additions.json", tracked, run_id, event))

    def test_a_consumer_pull_request_run_is_sent_to_the_kit_s_workflow_with_its_run_id_and_no_path_into_the_shared_tree(self) -> None:
        summary = self.summary(self.CONSUMER, "123456789", "pull_request")
        self.assertIn("Start the workflow `Apply recorded additions` with the run id `123456789`", summary)
        self.assertIn('section "Additions" of the suite-selection consumer kit', summary)
        self.assertIn("| `a` | path `lib/other.txt` |", summary)
        for text in ("actions/", "docs/maintaining.md", "CrankyAnt/SharedWorkflows", "recording.py"):
            self.assertNotIn(text, summary)

    def test_a_consumer_run_of_no_pull_request_is_not_sent_to_a_workflow_that_refuses_it(self) -> None:
        for event in ("push", "repository_dispatch"):
            with self.subTest(event=event):
                summary = self.summary(self.CONSUMER, "55", event)
                self.assertNotIn("Start the workflow", summary)
                self.assertIn(f"This {event} run belongs to no pull request", summary)
                self.assertIn('section "Additions" of the suite-selection consumer kit', summary)
                self.assertNotIn("actions/", summary)

    def test_a_consumer_check_outside_a_run_names_the_pull_request_s_run_without_a_number(self) -> None:
        self.assertIn("with the run id of the pull request's validation run", self.summary(self.CONSUMER, None, None))

    def test_the_repository_of_the_shared_parts_is_sent_to_validation_in_its_maintenance_documentation(self) -> None:
        for event in ("pull_request", "push"):
            with self.subTest(event=event):
                summary = self.summary(frozenset({"actions/recording/recording.py", "docs/maintaining.md"}), "1", event)
                self.assertIn('as "Validation" in `docs/maintaining.md` states', summary)
                self.assertIn("`additions.json`", summary)
                self.assertNotIn("consumer kit", summary)

    def test_every_read_inside_the_lists_names_no_route(self) -> None:
        summary = load_recording().check_summary([], 2, "any route")
        self.assertEqual("## Recording check\n\nEvery recorded read of 2 entry files lies inside the stored lists.\n", summary)


# A suite selected per entry file, a pooled suite with additions and a pooled suite without, each entry file with its own
# stored path; lib/other.txt lies outside every stored list.
KEEP_SUITES = {"always": [], "suites": [
    {"name": "per-entry", "entries": ["tests/a.py", "tests/b.py", "tests/c.py"], "command": ["python3"], "setups": [], "entry_selection": True,
     "paths": ["tests/a.py", "tests/b.py", "tests/c.py"],
     "entry_paths": {entry: [entry] for entry in ("tests/a.py", "tests/b.py", "tests/c.py")}},
    {"name": "pooled", "entries": ["tests/p1.py", "tests/p2.py"], "command": ["python3"], "setups": [], "paths": ["tests/p1.py", "tests/p2.py"]},
    {"name": "clean", "entries": ["tests/q.py"], "command": ["python3"], "setups": [], "paths": ["tests/q.py"]},
    {"name": "new-per-entry", "entries": ["tests/n1.py", "tests/n2.py"], "command": ["python3"], "setups": [], "entry_selection": True},
    {"name": "new-pooled", "entries": ["tests/m.py"], "command": ["python3"], "setups": []},
]}
KEEP_TRACKED = frozenset({"tests/a.py", "tests/b.py", "tests/c.py", "tests/p1.py", "tests/p2.py", "tests/q.py", "tests/n1.py", "tests/n2.py",
                          "tests/m.py", "lib/other.txt"})


class KeepPassedTests(unittest.TestCase):
    """``keep`` leaves the entry files that a job's additions name out of its passed list, so that a job whose check
    reports additions still stores the passes of its other entry files."""

    def setUp(self) -> None:
        self.recording = load_recording()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def additions(self, reads: dict[str, set[str]]) -> Path:
        """The additions document that check writes for a job that recorded each entry file of ``reads`` with those
        content reads."""
        selection = self.recording.suite_selection
        suite_list = selection.resolve_entries(selection.parse_suite_list(json.dumps(KEEP_SUITES)), set(KEEP_TRACKED))
        recorded = []
        for entry, paths in reads.items():
            unit = self.recording.Unit({"kind": "entry", "name": entry}, self.recording.Reads(paths=set(paths)))
            recorded.append((self.recording.suite_of(suite_list, unit.record), unit, unit.reads))
        document = self.recording.additions_document("suites.json", suite_list, recorded, KEEP_TRACKED)
        path = self.directory / "additions.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def passed(self, pairs: list[tuple[str, str]], *, bom: bool = False) -> Path:
        path = self.directory / "passed.json"
        path.write_text(("\ufeff" if bom else "") + json.dumps([{"suite": suite, "entry": entry} for suite, entry in pairs]), encoding="utf-8")
        return path

    def keep(self, additions: Path, passed: Path) -> int:
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            return self.recording.main(["keep", "--additions", str(additions), str(passed)])

    def kept(self, passed: Path) -> list[tuple[str, str]]:
        return [(record["suite"], record["entry"]) for record in json.loads(passed.read_text(encoding="utf-8"))]

    def test_an_entry_file_with_additions_leaves_the_list_and_every_other_passed_entry_file_of_the_job_stays(self) -> None:
        additions = self.additions({"tests/a.py": {"tests/a.py"}, "tests/b.py": {"tests/b.py", "lib/other.txt"},
                                    "tests/c.py": {"tests/c.py"}, "tests/q.py": {"tests/q.py"}})
        passed = self.passed([("per-entry", "tests/a.py"), ("per-entry", "tests/b.py"), ("clean", "tests/q.py")])
        self.assertEqual(0, self.keep(additions, passed))
        self.assertEqual([("per-entry", "tests/a.py"), ("clean", "tests/q.py")], self.kept(passed))

    def test_pooled_additions_leave_out_every_recorded_entry_file_of_their_suite_and_no_entry_file_of_another(self) -> None:
        additions = self.additions({"tests/p1.py": {"tests/p1.py"}, "tests/p2.py": {"tests/p2.py", "lib/other.txt"},
                                    "tests/a.py": {"tests/a.py"}, "tests/q.py": {"tests/q.py"}})
        document = json.loads(additions.read_text(encoding="utf-8"))
        self.assertEqual([("pooled", ["tests/p1.py", "tests/p2.py"])],
                         [(item["suite"], item["recorded_entries"]) for item in document["suites"]], "check pools the additions")
        passed = self.passed([("pooled", "tests/p1.py"), ("pooled", "tests/p2.py"), ("per-entry", "tests/a.py"), ("clean", "tests/q.py")])
        self.assertEqual(0, self.keep(additions, passed))
        self.assertEqual([("per-entry", "tests/a.py"), ("clean", "tests/q.py")], self.kept(passed))

    def test_a_suite_without_a_stored_path_list_leaves_every_recorded_entry_file_out_whatever_it_read(self) -> None:
        additions = self.additions({"tests/n1.py": {"tests/n1.py"}, "tests/n2.py": {"tests/n2.py"}, "tests/m.py": {"tests/m.py"},
                                    "tests/q.py": {"tests/q.py"}})
        document = json.loads(additions.read_text(encoding="utf-8"))
        self.assertEqual([("new-per-entry", True, True), ("new-pooled", True, False)],
                         [(item["suite"], item["create"], "entries" in item) for item in document["suites"]])
        passed = self.passed([("new-per-entry", "tests/n1.py"), ("new-per-entry", "tests/n2.py"), ("new-pooled", "tests/m.py"), ("clean", "tests/q.py")])
        self.assertEqual(0, self.keep(additions, passed))
        self.assertEqual([("clean", "tests/q.py")], self.kept(passed))

    def test_a_passed_list_that_windows_powershell_wrote_with_a_byte_order_mark_is_read(self) -> None:
        additions = self.additions({"tests/a.py": {"tests/a.py", "lib/other.txt"}, "tests/q.py": {"tests/q.py"}})
        passed = self.passed([("per-entry", "tests/a.py"), ("clean", "tests/q.py")], bom=True)
        self.assertEqual(0, self.keep(additions, passed))
        self.assertEqual([("clean", "tests/q.py")], self.kept(passed))

    def test_a_document_that_is_no_additions_document_fails_with_a_recording_error_and_leaves_the_list_as_it_was(self) -> None:
        passed = self.passed([("clean", "tests/q.py")])
        before = passed.read_bytes()
        for name, text in (("full", json.dumps({"kind": "full-recording", "suite_list": "suites.json", "entries": []})),
                           ("broken", "{"), ("no suites", json.dumps({"kind": "additions"})),
                           ("bad item", json.dumps({"kind": "additions", "suites": [{"suite": "clean"}]}))):
            with self.subTest(document=name):
                additions = self.directory / "other.json"
                additions.write_text(text, encoding="utf-8")
                self.assertEqual(2, self.keep(additions, passed))
                self.assertEqual(before, passed.read_bytes())
        with self.subTest(document="missing"):
            self.assertEqual(2, self.keep(self.directory / "missing.json", passed))
            self.assertEqual(before, passed.read_bytes())

    def test_a_passed_list_that_is_no_list_of_suites_and_entry_files_fails_with_a_recording_error(self) -> None:
        additions = self.additions({"tests/a.py": {"tests/a.py", "lib/other.txt"}})
        for text in ("{", json.dumps({"suite": "clean"}), json.dumps([{"suite": "clean"}])):
            with self.subTest(passed=text):
                passed = self.directory / "passed.json"
                passed.write_text(text, encoding="utf-8")
                self.assertEqual(2, self.keep(additions, passed))
                self.assertEqual(text, passed.read_text(encoding="utf-8"))
        self.assertEqual(2, self.keep(additions, self.directory / "absent.json"))


if __name__ == "__main__":
    unittest.main()
