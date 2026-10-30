"""The recording step's check on the neutral event files of a Windows ETW recording: attribution by process id, the
additions of a pull-request run, and the report of the stop and parse time."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests.recording_fixtures import ETW_RECORDINGS, ROOT, RecordingFixture, copy_recording, suite
from tests.test_recording_classification import ENTRIES, WINDOWS_FILES, classification_fixture

RECORDED = ETW_RECORDINGS / "classification" / "first"
NODE_RECORDED = ETW_RECORDINGS / "node"
SELECTION = ROOT / "actions" / "suite-selection" / "suite_selection.py"

# A Python entry file that runs a script under node, which looks up a module beside it, an absent one there and an
# absent package, and reads a data file; node has no lookup report.
NODE_FILES = {
    "tests/node_run.py": """\
        import subprocess
        subprocess.run(["node", "tests/js/main.js"], check=True)
        """,
    "tests/js/main.js": """\
        const fs = require("fs");
        const helper = require("./lib/helper");
        for (const name of ["./lib/optional", "absent-package"]) {
          try { require(name); } catch (error) { if (error.code !== "MODULE_NOT_FOUND") throw error; }
        }
        console.log(helper.value, fs.readFileSync("data/input.txt", "utf8").trim());
        """,
    "tests/js/lib/helper.js": "module.exports = { value: 1 };\n", "tests/js/lib/unused.js": "", "data/input.txt": "input\n",
}
NODE_ENTRIES = ["tests/node_run.py"]


def node_fixture(cleanup) -> RecordingFixture:
    """The fixture repository of a Node process without a lookup report, with one suite selected per entry file and
    empty lists."""
    return RecordingFixture(cleanup, NODE_FILES, [suite("node", NODE_ENTRIES, entry_selection=True, paths=[], setups=["python"],
                                                        entry_paths={entry: [] for entry in NODE_ENTRIES})])


def recorded_fixture(cleanup) -> RecordingFixture:
    fixture = classification_fixture(cleanup, WINDOWS_FILES)
    copy_recording(RECORDED, fixture.output)
    return fixture


def entries(fixture: RecordingFixture) -> dict[str, dict]:
    return {item["entry"]: item["reads"] for item in fixture.full_result()["entries"]}


def units(output: Path) -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted((output / "units").glob("*.json"))]


def rewrite_events(output: Path, keep) -> None:
    events = output / "events.tsv"
    lines = events.read_text(encoding="utf-8").splitlines(keepends=True)
    events.write_text("".join(line for line in lines if keep(line.rstrip("\n").split("\t", 3))), encoding="utf-8", newline="\n")


def rewrite_first_clone(output: Path, command) -> str:
    """Give the first start of a ``git clone`` the command line that ``command`` makes of its own, or none for None, and
    return its process id."""
    events = output / "events.tsv"
    lines = events.read_text(encoding="utf-8").split("\n")
    index = next(index for index, line in enumerate(lines) if "\tstart\t" in line and " clone " in line)
    parts = lines[index].split("\t")
    changed = command(parts[4])
    lines[index] = "\t".join(parts[:4] + ([] if changed is None else [changed]))
    events.write_text("\n".join(lines), encoding="utf-8", newline="\n")
    return parts[0]


class RecordedEventFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = recorded_fixture(cls.addClassCleanup)
        cls.checked = cls.fixture.check()
        assert cls.checked.returncode == 0, cls.checked.stdout + cls.checked.stderr
        cls.reads = entries(cls.fixture)

    def test_every_entry_file_ran_as_its_own_process_with_an_empty_bytecode_cache(self) -> None:
        records = [record for record in units(self.fixture.output) if record["kind"] == "entry"]
        self.assertEqual(sorted(ENTRIES), sorted(record["name"] for record in records))
        self.assertEqual(len(records), len({record["pid"] for record in records}))
        self.assertEqual(len(records), len({record["cache"] for record in records}))
        # Each entry file probed its helper's bytecode in its own cache, because no earlier entry had compiled it; the
        # interpreter is a child of the virtual environment's launcher, the entry file's own process.
        events = (self.fixture.output / "events.tsv").read_text(encoding="utf-8")
        for record in records:
            if record["name"] in ("tests/imports.py", "tests/imports_only.py"):
                cache = record["cache"][2:].lower()
                self.assertTrue([line for line in events.splitlines() if "\tabsent\t" in line and cache in line.lower()
                                 and line.lower().endswith(".pyc")], record["name"])

    def test_reads_are_attributed_to_entry_files_by_process_id(self) -> None:
        # tests/copy.py and tests/worktree.py copy the fixture, and git reads every file of each copy, tests/no_copies.py
        # copies lib/, and tests/lookups.py imports scripts/tool.py; the other entry files read their own.
        others = {entry: reads for entry, reads in self.reads.items()
                  if entry not in ("tests/copy.py", "tests/worktree.py", "tests/no_copies.py", "tests/lookups.py")}
        owners = {path: [entry for entry, reads in others.items() if path in reads["paths"]]
                  for path in ("data/stat.txt", "scripts/tool.py", "kept.log", "lib/copied.txt")}
        self.assertEqual({"data/stat.txt": ["tests/stats.py"], "scripts/tool.py": ["tests/sources.py"], "kept.log": ["tests/ignored.py"],
                          "lib/copied.txt": []}, owners)
        self.assertIn("lib/copied.txt", self.reads["tests/copy.py"]["paths"])
        for entry, reads in others.items():
            self.assertEqual([entry], [other for other in ENTRIES if other in reads["paths"]])

    def test_a_child_process_belongs_to_the_entry_file_whose_root_process_started_it(self) -> None:
        # The listing of lib/ in tests/imports.py comes from its child process; tests/no_copies.py lists lib/ to copy it, and
        # tests/lookups.py lists it with its own code beside its import from it.
        self.assertIn("lib", self.reads["tests/imports.py"]["directory_memberships"])
        self.assertEqual(["tests/imports.py", "tests/lookups.py", "tests/no_copies.py"],
                         sorted(entry for entry, reads in self.reads.items() if "lib" in reads["directory_memberships"]))

    def test_a_root_process_without_a_start_event_forms_its_own_group(self) -> None:
        fixture = recorded_fixture(self.addCleanup)
        # A root process whose id another process had earlier joins that process's unit without its start event, so only
        # the roots whose id starts no other process lose theirs.
        started = [line.split("\t", 3)[:2] for line in (fixture.output / "events.tsv").read_text(encoding="utf-8").splitlines()
                   if line.split("\t", 3)[2:3] == ["start"]]
        roots = {(str(record["pid"]), str(record["launcher"])) for record in units(fixture.output)
                 if [pid for pid, _ in started].count(str(record["pid"])) == 1}
        before = (fixture.output / "events.tsv").read_text(encoding="utf-8").count("\n")
        rewrite_events(fixture.output, lambda parts: not (parts[2] == "start" and (parts[0], parts[1]) in roots))
        self.assertLess((fixture.output / "events.tsv").read_text(encoding="utf-8").count("\n"), before)
        checked = fixture.check()
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        self.assertEqual(self.reads, entries(fixture))

    def test_check_reports_how_long_the_stop_and_the_parse_took(self) -> None:
        self.assertRegex(self.checked.stdout, r"Stopped the ETW session in \d+\.\d\d s \(the consumer drained it in \d+ ms: \d+ events, "
                                              r"\d+ lines, none lost\); parsed and classified the event file in \d+\.\d\d s\.")
        self.assertIn("parsed and classified the event file", self.fixture.summary.read_text(encoding="utf-8"))

    def test_a_pull_request_run_names_the_additions_of_a_recorded_event_file(self) -> None:
        fixture = recorded_fixture(self.addCleanup)
        settings = json.loads((fixture.output / "recording.json").read_text(encoding="utf-8"))
        (fixture.output / "recording.json").write_text(json.dumps(dict(settings, mode="pull-request")), encoding="utf-8")
        stored = {entry: reads["paths"] for entry, reads in self.reads.items()}
        stored["tests/stats.py"] = [path for path in stored["tests/stats.py"] if path != "data/stat.txt"]
        fixture.write_suites([suite("all", ENTRIES, entry_selection=True, paths=[], entry_paths=stored)])
        checked = fixture.check()
        self.assertEqual(1, checked.returncode, checked.stdout + checked.stderr)
        [change] = fixture.additions()["suites"]
        self.assertEqual(["data/stat.txt"], change["entries"]["tests/stats.py"]["paths"])
        self.assertIn("`all`: `tests/stats.py` | path `data/stat.txt` |", fixture.summary.read_text(encoding="utf-8"))

    def test_a_git_start_without_its_command_line_fails_the_check_and_names_the_process(self) -> None:
        fixture = recorded_fixture(self.addCleanup)
        pid = rewrite_first_clone(fixture.output, lambda command: None)
        checked = fixture.check()
        self.assertEqual(2, checked.returncode, checked.stdout + checked.stderr)
        self.assertIn(f"the recording holds no command line of process {pid} (", checked.stderr)

    def test_a_clone_with_a_relative_path_in_an_unknown_working_directory_fails_the_check_and_names_it(self) -> None:
        # The started git process has no working directory in the event file; only an entry file's root process has.
        fixture = recorded_fixture(self.addCleanup)
        pid = rewrite_first_clone(fixture.output, lambda command: command.rsplit(" ", 1)[0] + " copy")
        checked = fixture.check()
        self.assertEqual(2, checked.returncode, checked.stdout + checked.stderr)
        self.assertIn(f"process {pid} ran `git clone -q ", checked.stderr)
        self.assertIn(" copy` with a relative source or destination in a working directory that the recording does not know",
                      checked.stderr)

    def test_a_session_that_lost_events_fails_the_check(self) -> None:
        fixture = recorded_fixture(self.addCleanup)
        log = fixture.output / "logs" / "etw.log"
        log.write_text(log.read_text(encoding="utf-8").replace(" lost=0", " lost=3"), encoding="utf-8")
        checked = fixture.check()
        self.assertEqual(2, checked.returncode)
        self.assertIn("lost 3 events", checked.stderr)


class AttributionTests(unittest.TestCase):
    """Attribution on a written neutral event file, where a process id returns for another process."""

    def test_a_reused_process_id_belongs_to_the_process_of_its_latest_start(self) -> None:
        fixture = RecordingFixture(self.addCleanup, {"tests/a.py": "", "tests/b.py": "", "lib/a.txt": "", "lib/b.txt": ""},
                                   [suite("a", ["tests/a.py", "tests/b.py"], entry_selection=True, paths=[],
                                          entry_paths={"tests/a.py": [], "tests/b.py": []})])
        copy_recording(RECORDED, fixture.output)
        shutil.rmtree(fixture.output / "lookups")  # the written event file's processes have no lookup reports
        settings = json.loads((fixture.output / "recording.json").read_text(encoding="utf-8"))
        root = settings["roots"][0]
        device, drive = next((device, drive) for device, drive in settings["devices"].items() if drive == root[:2])
        prefix = device + root[2:]
        record = json.loads(next((fixture.output / "units").glob("*.json")).read_text(encoding="utf-8"))
        for path in (fixture.output / "units").glob("*.json"):
            path.unlink()
        for unit, entry, pid, launcher in (("1-a", "tests/a.py", 100, 10), ("2-b", "tests/b.py", 200, 20)):
            (fixture.output / "units" / f"{unit}.json").write_text(json.dumps(dict(record, unit=unit, name=entry, pid=pid, launcher=launcher,
                                                                                  command=["python.exe", entry], cwd=root)), encoding="utf-8")
        lines = [("100", "10", "start", "C:\\Python\\python.exe"), ("300", "100", "start", "C:\\Python\\python.exe"),
                 ("300", "100", "open", prefix + "\\lib\\a.txt"),
                 ("200", "20", "start", "C:\\Python\\python.exe"), ("300", "200", "start", "C:\\Python\\python.exe"),
                 ("300", "200", "open", prefix + "\\lib\\b.txt")]
        (fixture.output / "events.tsv").write_text("".join("\t".join(line) + "\n" for line in lines), encoding="utf-8", newline="\n")
        checked = fixture.check()
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        self.assertEqual({"tests/a.py": ["lib/a.txt"], "tests/b.py": ["lib/b.txt"]},
                         {item["entry"]: item["reads"]["paths"] for item in fixture.full_result()["entries"]})


DOTNET = "C:\\Program Files\\dotnet\\dotnet.exe"
# The names that the .NET probe rules add in each directory of a project, and in each directory of a working directory.
PROJECT_PROBES = ["Directory.Build.props", "Directory.Build.rsp", "Directory.Build.targets", "Directory.Packages.props", "NuGet.Config",
                  "NuGet.config", "global.json", "nuget.config"]
WORKING_PROBES = ["NuGet.Config", "NuGet.config", "global.json", "nuget.config"]


class WrittenRecording:
    """A fixture repository with a written neutral event file in place of a recorded one, in the settings of a recorded
    Windows recording; ``prefix`` is the recorded checkout as a device path, ``root`` as a drive path."""

    def __init__(self, cleanup, files: dict[str, str], suites: list[dict], *, mode: str = "full") -> None:
        self.fixture = RecordingFixture(cleanup, files, suites)
        output = self.fixture.output
        copy_recording(RECORDED, output)
        shutil.rmtree(output / "lookups")  # the written event file's processes have no lookup reports
        settings = json.loads((output / "recording.json").read_text(encoding="utf-8"))
        (output / "recording.json").write_text(json.dumps(dict(settings, mode=mode)), encoding="utf-8")
        self.root = settings["roots"][0]
        self.prefix = next(device for device, drive in settings["devices"].items() if drive == self.root[:2]) + self.root[2:]
        self.record = json.loads(next((output / "units").glob("*.json")).read_text(encoding="utf-8"))
        for path in (output / "units").glob("*.json"):
            path.unlink()

    def unit(self, unit: str, entry: str, pid: int, launcher: int, command: list[str], cwd: str | None = None, kind: str = "entry") -> None:
        """A unit record of an entry file, or with ``kind`` ``setup`` of the setup that ``entry`` names."""
        (self.fixture.output / "units" / f"{unit}.json").write_text(json.dumps(dict(
            self.record, unit=unit, kind=kind, name=entry, pid=pid, launcher=launcher, command=command, cwd=cwd or self.root)), encoding="utf-8")

    def events(self, lines: list[tuple[str, ...]]) -> None:
        (self.fixture.output / "events.tsv").write_text("".join("\t".join(line) + "\n" for line in lines), encoding="utf-8", newline="\n")


class ToolchainProbeTests(unittest.TestCase):
    """The probe rules of the .NET SDK on a written neutral event file, whose Kernel-File session sees no attribute
    query, so that the configuration probes of a dotnet process are missing from it."""

    def full_reads(self, recording: WrittenRecording) -> dict[str, dict]:
        checked = recording.fixture.check()
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        return {item["entry"]: item["reads"] for item in recording.fixture.full_result()["entries"]}

    def test_a_dotnet_process_adds_an_existence_read_of_every_name_its_rules_probe_unless_the_recording_holds_it(self) -> None:
        recording = WrittenRecording(self.addCleanup, {"tests/build.py": "", "tests/plain.py": "", "app/App.csproj": "<Project />\n",
                                                       "App.slnx": "<Solution />\n", "NuGet.Config": "<configuration />\n"},
                                     [suite("a", ["tests/build.py", "tests/plain.py"], entry_selection=True, paths=[],
                                            entry_paths={"tests/build.py": [], "tests/plain.py": []})])
        recording.unit("1-build", "tests/build.py", 100, 10, ["python.exe", "tests/build.py"])
        recording.unit("2-plain", "tests/plain.py", 200, 20, ["python.exe", "tests/plain.py"])
        prefix = recording.prefix
        recording.events([("100", "10", "start", "C:\\Python\\python.exe"), ("300", "100", "start", DOTNET),
                          ("300", "100", "open", prefix + "\\App.slnx"), ("300", "100", "open", prefix + "\\app\\App.csproj"),
                          ("300", "100", "open", prefix + "\\NuGet.Config"), ("300", "100", "absent", prefix + "\\app\\Directory.Build.props"),
                          ("200", "20", "start", "C:\\Python\\python.exe"), ("200", "20", "open", prefix + "\\app\\App.csproj")])
        reads = self.full_reads(recording)
        self.assertEqual(["App.slnx", "NuGet.Config", "app/App.csproj"], reads["tests/build.py"]["paths"])
        solution = ["Directory.Solution.props", "Directory.Solution.targets", "after.App.slnx.targets", "before.App.slnx.targets"]
        expected = sorted([*(name for name in PROJECT_PROBES if name != "NuGet.Config"), *solution, *(f"app/{name}" for name in PROJECT_PROBES)])
        self.assertEqual(expected, [read["path"] for read in reads["tests/build.py"]["existence_reads"]])
        self.assertEqual({"any"}, {read["kind"] for read in reads["tests/build.py"]["existence_reads"]})
        # A project file that no dotnet process reads anchors no rule.
        self.assertEqual(["app/App.csproj"], reads["tests/plain.py"]["paths"])
        self.assertEqual([], reads["tests/plain.py"]["existence_reads"])

    def test_a_directory_on_the_dotnet_command_line_anchors_the_rules_of_its_project_and_solution(self) -> None:
        # The recorded command is dotnet itself, with the directory of the project as its argument and no project file read.
        recording = WrittenRecording(self.addCleanup, {"src/App/Program.cs": "", "tests/build.py": ""},
                                     [suite("a", ["tests/build.py"], paths=[])])
        recording.unit("1-build", "tests/build.py", 100, 10, [DOTNET, "build", "--nologo", "SRC\\app"])
        recording.events([("100", "10", "start", DOTNET)])
        reads = self.full_reads(recording)
        names = ["Directory.Build.props", "Directory.Build.rsp", "Directory.Build.targets", "Directory.Packages.props", "Directory.Solution.props",
                 "Directory.Solution.targets", "NuGet.Config", "NuGet.config", "global.json", "nuget.config"]
        expected = sorted(f"{directory}{name}" for directory in ("", "src/", "src/App/") for name in names)
        self.assertEqual(expected, [read["path"] for read in reads["tests/build.py"]["existence_reads"]])

    def test_a_new_nuget_config_above_the_project_selects_the_suite_after_check_and_apply_of_a_dotnet_build(self) -> None:
        recording = WrittenRecording(self.addCleanup, {"tools/consumer/App.csproj": "<Project />\n", "tests/build.py": ""},
                                     [suite("build", ["tests/build.py"], paths=["tests/build.py", "tools/consumer/App.csproj"])], mode="pull-request")
        recording.unit("1-build", "tests/build.py", 100, 10, ["python.exe", "tests/build.py"])
        recording.events([("100", "10", "start", "C:\\Python\\python.exe"), ("300", "100", "start", DOTNET),
                          ("300", "100", "open", recording.prefix + "\\tools\\consumer\\App.csproj")])
        fixture = recording.fixture
        self.assertEqual(1, fixture.check().returncode)
        self.assertEqual(0, fixture.apply().returncode)
        fixture.git("commit", "-q", "-am", "lists")
        base = fixture.git("rev-parse", "HEAD").strip()
        (fixture.root / "tools" / "NuGet.Config").write_text("<configuration />\n", encoding="utf-8")
        fixture.git("add", "tools/NuGet.Config")
        fixture.git("commit", "-q", "-m", "configuration")
        head = fixture.git("rev-parse", "HEAD").strip()
        select = [sys.executable, str(SELECTION), "select", "--root", ".", "--suites", "suites.json", "--base", base, "--head", head]
        selected = subprocess.run(select, cwd=fixture.root, capture_output=True, text=True, encoding="utf-8", check=False)
        self.assertEqual(0, selected.returncode, selected.stderr)
        self.assertIn("build: run (`tools/NuGet.Config` matches `any existence `tools/NuGet.Config``)", selected.stdout)
        # The three MSBuild names that the suite list stated by hand before the rules leave it out.
        fixture.write_suites([suite("build", ["tests/build.py"], paths=["tests/build.py", "tools/consumer/App.csproj"], extra_existence_reads=[
            {"path": f"{directory}{name}", "kind": "any"} for directory in ("", "tools/", "tools/consumer/")
            for name in ("Directory.Build.props", "Directory.Build.targets", "Directory.Packages.props")])])
        selected = subprocess.run(select, cwd=fixture.root, capture_output=True, text=True, encoding="utf-8", check=False)
        self.assertEqual(0, selected.returncode, selected.stderr)
        self.assertIn("build: skip", selected.stdout)

    def test_a_started_dotnet_gets_the_rules_of_its_absolute_arguments_and_of_the_directories_it_read_in(self) -> None:
        # ETW gives a started process no working directory: its relative argument places nothing, and the directories it
        # read in stand in for its working directory beside the recorded command's.
        recording = WrittenRecording(self.addCleanup, {"src/App/Program.cs": "", "docs/notes.txt": "", "lib/Other/a.txt": "", "tests/build.py": ""},
                                     [suite("a", ["tests/build.py"], paths=[])])
        recording.unit("1-build", "tests/build.py", 100, 10, ["python.exe", "tests/build.py"])
        command = f'"{DOTNET}" build {recording.root.upper()}\\SRC\\APP lib\\Other'.replace("\\", "\\\\")
        recording.events([("100", "10", "start", "C:\\Python\\python.exe"), ("300", "100", "start", DOTNET, command),
                          ("300", "100", "open", recording.prefix + "\\docs\\notes.txt")])
        reads = self.full_reads(recording)
        names = ["Directory.Build.props", "Directory.Build.rsp", "Directory.Build.targets", "Directory.Packages.props", "Directory.Solution.props",
                 "Directory.Solution.targets", "NuGet.Config", "NuGet.config", "global.json", "nuget.config"]
        expected = sorted([*(f"{directory}{name}" for directory in ("", "src/", "src/App/") for name in names),
                           *(f"docs/{name}" for name in WORKING_PROBES)])
        self.assertEqual(expected, [read["path"] for read in reads["tests/build.py"]["existence_reads"]])

    def test_the_working_directory_rules_start_from_a_checkout_subdirectory_spelled_in_another_case(self) -> None:
        recording = WrittenRecording(self.addCleanup, {"app/readme.txt": "", "tests/build.py": ""}, [suite("a", ["tests/build.py"], paths=[])])
        recording.unit("1-build", "tests/build.py", 100, 10, ["python.exe", "..\\tests\\build.py"], cwd=recording.root.upper() + "\\APP")
        recording.events([("100", "10", "start", "C:\\Python\\python.exe"), ("300", "100", "start", DOTNET)])
        reads = self.full_reads(recording)
        self.assertEqual(sorted([*WORKING_PROBES, *(f"app/{name}" for name in WORKING_PROBES)]),
                         [read["path"] for read in reads["tests/build.py"]["existence_reads"]])


WRITTEN_NODE_FILES = {"tests/run.py": "", "tests/plain.py": "", "package.json": "{}\n", "tests/js/main.js": "", "tests/js/lib/helper.js": "",
                      "tests/js/lib/unused.js": "", "data/input.txt": "", "docs/notes.txt": "", ".gitignore": "node_modules/\n",
                      "node_modules/pkg/index.js": ""}


class RecordedNodeTests(unittest.TestCase):
    def test_a_recorded_node_process_without_a_lookup_report_passes_the_check_and_its_directories_are_memberships(self) -> None:
        fixture = node_fixture(self.addCleanup)
        copy_recording(NODE_RECORDED, fixture.output)
        checked = fixture.check()
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        reads = fixture.reads_of("tests/node_run.py")
        # The node process opened its script, the module it found and the data file; the recording sees none of its absent
        # lookups of ./lib/optional and absent-package.
        self.assertEqual(["data", "tests/js", "tests/js/lib"], reads["directory_memberships"])
        self.assertEqual(["data/input.txt", "tests/js/lib/helper.js", "tests/js/main.js", "tests/node_run.py"], reads["paths"])


class UnreportedNodeTests(unittest.TestCase):
    """A Node process without a lookup report on a written neutral event file: the Kernel-File session sees none of its
    module lookups, so each checkout directory in which it opened a file is a directory membership."""

    def recording(self, program: str) -> WrittenRecording:
        recording = WrittenRecording(self.addCleanup, WRITTEN_NODE_FILES, [suite("a", ["tests/run.py", "tests/plain.py"], entry_selection=True,
                                                                                 paths=[], entry_paths={"tests/run.py": [], "tests/plain.py": []})])
        recording.unit("1-run", "tests/run.py", 100, 10, ["python.exe", "tests/run.py"])
        recording.unit("2-plain", "tests/plain.py", 200, 20, ["python.exe", "tests/plain.py"])
        prefix = recording.prefix
        node = f"C:\\Program Files\\nodejs\\{program}"
        recording.events([("100", "10", "start", "C:\\Python\\python.exe"), ("300", "100", "start", node),
                          ("300", "100", "open", prefix + "\\package.json"), ("300", "100", "opendir", prefix + "\\tests"),
                          ("300", "100", "open", prefix + "\\tests\\js\\main.js"), ("300", "100", "open", prefix + "\\tests\\js\\lib\\helper.js"),
                          ("300", "100", "absent", prefix + "\\tests\\js\\lib\\missing.js"), ("300", "100", "open", prefix + "\\data\\input.txt"),
                          ("300", "100", "absent", prefix + "\\docs\\absent\\index.js"), ("300", "100", "open", prefix + "\\node_modules\\pkg\\index.js"),
                          ("100", "10", "open", prefix + "\\docs\\notes.txt"),
                          ("200", "20", "start", "C:\\Python\\python.exe"), ("200", "20", "open", prefix + "\\tests\\js\\main.js")])
        return recording

    def test_each_checkout_directory_in_which_a_node_process_opened_a_file_is_a_directory_membership(self) -> None:
        for program in ("node.exe", "tsc.exe"):
            with self.subTest(program=program):
                recording = self.recording(program)
                checked = recording.fixture.check()
                self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
                reads = {item["entry"]: item["reads"] for item in recording.fixture.full_result()["entries"]}
                # Neither the directory it opened as a directory, the directory of an absent probe, an ignored directory,
                # nor a directory in which only another process opened a file is one.
                self.assertEqual([".", "data", "tests/js", "tests/js/lib"], reads["tests/run.py"]["directory_memberships"])
                self.assertEqual(["data/input.txt", "docs/notes.txt", "package.json", "tests/js/lib/helper.js", "tests/js/main.js"],
                                 reads["tests/run.py"]["paths"])
                self.assertEqual([], reads["tests/plain.py"]["directory_memberships"])

    def test_a_node_process_of_a_setup_gives_the_entry_files_that_state_the_setup_its_directory_memberships(self) -> None:
        recording = WrittenRecording(self.addCleanup, {"tests/run.py": "", "tests/plain.py": "", "tools/setup/install.js": "", "tools/setup/data.json": ""},
                                     [suite("a", ["tests/run.py", "tests/plain.py"], entry_selection=True, paths=[], entry_setups={"tests/run.py": ["node"]},
                                            entry_paths={"tests/run.py": [], "tests/plain.py": []})])
        recording.unit("0-node", "node", 50, 5, ["C:\\Program Files\\nodejs\\node.exe", "tools\\setup\\install.js"], kind="setup")
        recording.unit("1-run", "tests/run.py", 100, 10, ["python.exe", "tests/run.py"])
        recording.unit("2-plain", "tests/plain.py", 200, 20, ["python.exe", "tests/plain.py"])
        prefix = recording.prefix
        recording.events([("50", "5", "start", "C:\\Program Files\\nodejs\\node.exe"), ("50", "5", "open", prefix + "\\tools\\setup\\install.js"),
                          ("100", "10", "start", "C:\\Python\\python.exe"), ("200", "20", "start", "C:\\Python\\python.exe")])
        checked = recording.fixture.check()
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        reads = {item["entry"]: item["reads"] for item in recording.fixture.full_result()["entries"]}
        self.assertEqual(["tools/setup"], reads["tests/run.py"]["directory_memberships"])
        self.assertEqual(["tools/setup/install.js"], reads["tests/run.py"]["paths"])
        self.assertEqual([], reads["tests/plain.py"]["directory_memberships"])

    def test_a_file_added_to_or_removed_from_such_a_directory_selects_the_entry_file_after_check_and_apply(self) -> None:
        recording = self.recording("node.exe")
        fixture = recording.fixture
        settings = json.loads((fixture.output / "recording.json").read_text(encoding="utf-8"))
        (fixture.output / "recording.json").write_text(json.dumps(dict(settings, mode="pull-request")), encoding="utf-8")
        self.assertEqual(1, fixture.check().returncode)
        self.assertEqual(0, fixture.apply().returncode)
        fixture.git("commit", "-q", "-am", "lists")
        base = fixture.git("rev-parse", "HEAD").strip()

        def selected(change) -> str:
            fixture.git("checkout", "-q", "--detach", base)
            change()
            fixture.git("add", "-A")
            fixture.git("commit", "-q", "-m", "change")
            head = fixture.git("rev-parse", "HEAD").strip()
            select = [sys.executable, str(SELECTION), "select", "--root", ".", "--suites", "suites.json", "--base", base, "--head", head]
            result = subprocess.run(select, cwd=fixture.root, capture_output=True, text=True, encoding="utf-8", check=False)
            self.assertEqual(0, result.returncode, result.stderr)
            return result.stdout

        added = selected(lambda: (fixture.root / "tests" / "js" / "lib" / "other.js").write_text("", encoding="utf-8"))
        self.assertIn("a: run (`tests/js/lib/other.js` matches `directory membership `tests/js/lib`` of `tests/run.py`; entries: `tests/run.py`)",
                      added)
        removed = selected(lambda: (fixture.root / "tests" / "js" / "lib" / "unused.js").unlink())
        self.assertIn("a: run (`tests/js/lib/unused.js` matches `directory membership `tests/js/lib`` of `tests/run.py`; entries: `tests/run.py`)",
                      removed)
        # A directory in which the process opened nothing is the stated limit: a new file there selects nothing.
        unseen = selected(lambda: (fixture.root / "tests" / "other.js").write_text("", encoding="utf-8"))
        self.assertIn("a: skip", unseen)


if __name__ == "__main__":
    unittest.main()
