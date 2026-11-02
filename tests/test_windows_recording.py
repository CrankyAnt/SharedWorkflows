"""The recording step on Windows: exec runs each entry file as its own process with an empty bytecode cache, a live ETW
recording of the classification fixture follows the same classification rules as a stored strace recording, one session
attributes the reads of two entry files that run at the same time to the entry file whose process made them, the gate of
the live ETW tests names what denies a session, and the ETW consumer, the gate and the ETW access setup name one provider
set."""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from typing import NamedTuple
from unittest import mock

from tests import recording_fixtures
from tests.recording_fixtures import ROOT, RecordingFixture, entry_command, requires_etw, suite
from tests.test_recording_classification import (ENTRIES, LINKED_ENTRIES, WINDOWS_FILES, ClassificationRules, classification_fixture,
                                                 classified_lists, linked_worktree_fixture)

CACHE_REPORT = """\
    import importlib.util, os, sys
    sys.path.insert(0, "lib")
    cached = importlib.util.cache_from_source(os.path.abspath("lib/helper.py"))
    before = os.path.exists(cached)
    import helper
    print("pid", os.getpid(), "cache", sys.pycache_prefix, "compiled-before", before, "compiled-after", os.path.exists(cached))
    """

ENVIRONMENT_READER = """\
    import json, os, shutil
    print(json.dumps({"tool": shutil.which("fixture-tool"), "USERPROFILE": os.environ.get("USERPROFILE")}))
    """

# Each entry file reads its own files before and after both entry files have started, the later one through a child process.
CONCURRENT = """\
    import subprocess, sys, time
    from pathlib import Path
    name, ready = Path(__file__).stem, Path(sys.argv[1])
    print(Path(f"data/{name}.txt").read_text())
    (ready / name).write_text("")
    deadline = time.monotonic() + 60
    while len(list(ready.iterdir())) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    subprocess.run([sys.executable, "-c", f"print(open('data/{name}-child.txt').read())"], check=True)
    print(Path(f"data/{name}-after.txt").read_text())
    """


def run_entries(fixture: RecordingFixture, entries: list[str] = ENTRIES) -> None:
    """Start a full live recording in ``fixture`` and run each of ``entries`` under it, on an interpreter of the
    fixture's own with the lookup report, as the setup ``python`` that the entry files state provides it."""
    started = fixture.start(full=True)
    assert started.returncode == 0, started.stdout + started.stderr
    fixture.set_up_python()
    for entry in entries:
        ran = fixture.exec_entry(entry, *entry_command(entry, "python"))
        assert ran.returncode == 0, ran.stdout + ran.stderr


def windows_settings(fixture: RecordingFixture) -> None:
    """Write the settings and the job's environment as start writes them on Windows, without the session: exec does not
    talk to the consumer. The job's environment leaves out PYTHONDONTWRITEBYTECODE of the process that runs the tests, as
    the hosted job does not set it: the entry files then write bytecode, as compiled-after True of the bytecode-cache test
    expects."""
    for part in ("units", "logs", "setups"):
        (fixture.output / part).mkdir(parents=True)
    (fixture.output / "recording.json").write_text(json.dumps({"mode": "pull-request", "trace_set": "etw", "events": "events.tsv"}),
                                                   encoding="utf-8")
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONDONTWRITEBYTECODE"}
    (fixture.output / "environment.json").write_text(json.dumps(environment), encoding="utf-8")


@unittest.skipUnless(sys.platform == "win32", "the Windows recording runs on Windows")
class WindowsExecTests(unittest.TestCase):
    def test_exec_runs_each_entry_file_as_its_own_process_with_an_empty_bytecode_cache(self) -> None:
        # Both entry files import the same helper; the second finds no bytecode that the first compiled.
        fixture = RecordingFixture(self.addCleanup, {"tests/a.py": CACHE_REPORT, "tests/b.py": CACHE_REPORT, "lib/helper.py": ""},
                                   [suite("a", ["tests/a.py", "tests/b.py"], paths=[])])
        windows_settings(fixture)
        reports = []
        for entry in ("tests/a.py", "tests/b.py"):
            ran = fixture.exec_entry(entry, sys.executable, entry)
            self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
            reports.append(ran.stdout.split())
        records = {record["name"]: record for record in
                   (json.loads(path.read_text(encoding="utf-8")) for path in (fixture.output / "units").glob("*.json"))}
        for entry, report in zip(("tests/a.py", "tests/b.py"), reports):
            record = records[entry]
            self.assertEqual(["pid", str(record["pid"]), "cache", record["cache"], "compiled-before", "False", "compiled-after", "True"], report)
            self.assertEqual(["etw"], record["recorder"])
            self.assertEqual("events.tsv", record["trace"])
        self.assertNotEqual(records["tests/a.py"]["cache"], records["tests/b.py"]["cache"])
        self.assertNotEqual(records["tests/a.py"]["launcher"], records["tests/a.py"]["pid"])

    def test_an_entry_file_gets_the_tools_and_the_profile_of_its_stated_setups_alone(self) -> None:
        fixture = RecordingFixture(self.addCleanup, {f"tests/{name}.py": ENVIRONMENT_READER for name in ("tool", "profile", "none")}, [
            suite("tool", ["tests/tool.py"], paths=[], setups=["tool"]),
            suite("profile", ["tests/profile.py"], paths=[], setups=["delegate-shell-directory"]),
            suite("none", ["tests/none.py"], paths=[]),
        ])
        windows_settings(fixture)
        tools, profile = fixture.base / "setups" / "tool", fixture.base / "setups" / "delegate-shell-directory" / "profile"
        tools.mkdir(parents=True)
        (profile / "source" / "repos").mkdir(parents=True)
        (tools / "fixture-tool.cmd").write_text("@echo fixture tool ran\r\n", encoding="utf-8")
        for setup, option in (("tool", ["--path", str(tools)]), ("delegate-shell-directory", ["--variable", f"USERPROFILE={profile}"])):
            provided = fixture.provide(setup, *option)
            self.assertEqual(0, provided.returncode, provided.stderr)
        ran = fixture.exec_entry("tests/tool.py", "fixture-tool")
        self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
        self.assertIn("fixture tool ran", ran.stdout, "exec finds the command on the entry file's own PATH")
        # After the snapshot the setup puts its directory on the job's PATH in another case, as GITHUB_PATH would.
        job_path = os.pathsep.join([str(tools).upper(), os.environ["PATH"]])
        seen = {}
        with mock.patch.dict(os.environ, {"PATH": job_path}):
            for name in ("tool", "profile", "none"):
                ran = fixture.exec_entry(f"tests/{name}.py", sys.executable, f"tests/{name}.py")
                self.assertEqual(0, ran.returncode, ran.stdout + ran.stderr)
                seen[name] = json.loads(ran.stdout.strip().splitlines()[-1])
        self.assertEqual(os.path.normcase(str(tools / "fixture-tool.CMD")), os.path.normcase(seen["tool"]["tool"]))
        self.assertEqual({"tool": None, "USERPROFILE": str(profile)}, seen["profile"])
        self.assertEqual({"tool": None, "USERPROFILE": os.environ["USERPROFILE"]}, seen["none"])


@requires_etw
class LiveEtwClassificationTests(ClassificationRules, unittest.TestCase):
    """The classification rules on a live ETW recording of the Windows fixture."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = cls.record()

    @classmethod
    def record(cls) -> RecordingFixture:
        fixture = classification_fixture(cls.addClassCleanup, WINDOWS_FILES)
        run_entries(fixture)
        checked = fixture.check()
        assert checked.returncode == 0, checked.stdout + checked.stderr
        return fixture

    def files(self) -> dict[str, str]:
        return WINDOWS_FILES

    def recording_directory(self) -> Path:
        return self.fixture.output

    def recorded(self, files: dict[str, str]) -> RecordingFixture:
        fixture = classification_fixture(self.addCleanup, files)
        # The entry files list listed/ and open listed\one.txt, so it stays on disk, untracked, when files leaves it out.
        (fixture.root / "listed").mkdir(exist_ok=True)
        (fixture.root / "listed" / "one.txt").touch()
        run_entries(fixture)
        return fixture

    def linked(self) -> RecordingFixture:
        fixture = linked_worktree_fixture(self.addCleanup)
        run_entries(fixture, LINKED_ENTRIES)
        checked = fixture.check()
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        return fixture

    def second(self) -> dict:
        other = self.record()
        return classified_lists(other)


@requires_etw
class ConcurrentEtwAttributionTests(unittest.TestCase):
    def test_one_session_attributes_each_read_of_two_concurrent_entry_files_to_the_entry_file_whose_process_made_it(self) -> None:
        entries = ["tests/a.py", "tests/b.py"]
        files = {entry: CONCURRENT for entry in entries}
        files.update({f"data/{name}{part}.txt": f"{name}\n" for name in ("a", "b") for part in ("", "-child", "-after")})
        fixture = RecordingFixture(self.addCleanup, files, [suite("pair", entries, entry_selection=True, paths=[],
                                                                 entry_paths={entry: [] for entry in entries})])
        ready = fixture.base / "ready"
        ready.mkdir()
        started = fixture.start(full=True)
        self.assertEqual(0, started.returncode, started.stdout + started.stderr)
        processes = [subprocess.Popen(fixture.command("exec", "--output", str(fixture.output), "--entry", entry, "--suites", "suites.json", "--", sys.executable, entry,
                                                      str(ready)), cwd=fixture.root, env=fixture.environment(), stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace") for entry in entries]
        outputs = [process.communicate(timeout=120)[0] for process in processes]
        self.assertEqual([0, 0], [process.returncode for process in processes], outputs)
        checked = fixture.check()
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        for name, other in (("a", "b"), ("b", "a")):
            paths = fixture.reads_of(f"tests/{name}.py")["paths"]
            self.assertLessEqual({f"tests/{name}.py", f"data/{name}.txt", f"data/{name}-child.txt", f"data/{name}-after.txt"}, set(paths))
            self.assertEqual([], [path for path in paths if path.startswith(f"data/{other}") or path == f"tests/{other}.py"], paths)


PERFORMANCE_LOG_USERS_ROW = '"BUILTIN\\Performance Log Users","Alias","S-1-5-32-559","Mandatory group, Enabled by default, Enabled group"\n'
DENIED_AT_KERNEL_PROVIDER = """\
Unhandled exception. System.UnauthorizedAccessException: Access is denied. (0x80070005 (E_ACCESSDENIED))
   at Microsoft.Diagnostics.Tracing.Session.TraceEventSession.EnableKernelProvider(Keywords flags, Keywords stackCapture)
   at Program.Main(String[] args)
"""
DENIED_AT_PROVIDER = """\
Unhandled exception. System.UnauthorizedAccessException: Access is denied. (0x80070005 (E_ACCESSDENIED))
   at Microsoft.Diagnostics.Tracing.Session.TraceEventSession.EnableProvider(Guid providerGuid, TraceEventLevel providerLevel)
   at Program.Main(String[] args)
"""


class SessionStandIn:
    """Stands in for ``subprocess.run`` of the gate: the ETW consumer exits with ``consumer`` and ``output``, ``whoami``
    lists the Performance Log Users membership when ``member`` is true, and ``logman`` is denied the providers in
    ``denied``."""

    def __init__(self, consumer: int = 0, output: str = "", *, member: bool = True, denied: tuple[str, ...] = ()) -> None:
        self.consumer, self.output, self.member, self.denied = consumer, output, member, denied
        self.calls: list[list[str]] = []
        self.stop_existed: list[bool] = []

    def __call__(self, command: list[str], **options) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        program = Path(command[0]).name.lower()
        if program == "whoami.exe":
            return subprocess.CompletedProcess(command, 0, PERFORMANCE_LOG_USERS_ROW if self.member else "", "")
        if program == "logman.exe":
            if command[1] == "start" and any(provider in command for provider in self.denied):
                return subprocess.CompletedProcess(command, -2147024891, "Access is denied.\n", "")
            return subprocess.CompletedProcess(command, 0, "The command completed successfully.\n", "")
        self.stop_existed.append(Path(command[5]).is_file())
        return subprocess.CompletedProcess(command, self.consumer, "", self.output)

    def programs(self) -> list[str]:
        return [Path(command[0]).name.lower() for command in self.calls]


class EtwGateTests(unittest.TestCase):
    """The gate of the live ETW tests decides by a real session start, here replaced by a stand-in."""

    def denial(self, stand_in: SessionStandIn) -> str | None:
        with mock.patch.object(recording_fixtures.subprocess, "run", stand_in):
            return recording_fixtures.etw_session_denial("dotnet")

    def test_a_session_that_starts_and_stops_opens_the_gate(self) -> None:
        stand_in = SessionStandIn()
        self.assertIsNone(self.denial(stand_in))
        self.assertEqual(1, len(stand_in.calls))
        self.assertEqual(["dotnet", str(recording_fixtures.ETW_CONSUMER)], stand_in.calls[0][:2])
        self.assertEqual([True], stand_in.stop_existed, "the probe session stops as soon as it runs")

    def test_a_log_on_without_the_membership_names_the_membership_and_the_setup(self) -> None:
        reason = self.denial(SessionStandIn(1, DENIED_AT_KERNEL_PROVIDER, member=False))
        assert reason is not None
        self.assertIn("Performance Log Users", reason)
        self.assertIn("next log-on", reason)
        self.assertIn("tests/etw_access.ps1", reason)

    def test_a_denied_system_logger_session_names_the_system_trace_provider_alone(self) -> None:
        stand_in = SessionStandIn(1, DENIED_AT_KERNEL_PROVIDER)
        reason = self.denial(stand_in)
        assert reason is not None
        self.assertIn("SystemTraceProvider", reason)
        self.assertNotIn("Kernel-File", reason)
        self.assertNotIn("Kernel-Process", reason)
        self.assertIn("tests/etw_access.ps1", reason)

    def test_a_denied_provider_is_named_and_every_probe_session_is_stopped(self) -> None:
        stand_in = SessionStandIn(1, DENIED_AT_PROVIDER, denied=(recording_fixtures.KERNEL_FILE,))
        reason = self.denial(stand_in)
        assert reason is not None
        self.assertIn("Microsoft-Windows-Kernel-File", reason)
        self.assertNotIn("Kernel-Process", reason)
        self.assertNotIn("SystemTraceProvider", reason)
        self.assertIn("tests/etw_access.ps1", reason)
        starts = [command[2] for command in stand_in.calls if command[1:2] == ["start"]]
        stops = [command[2] for command in stand_in.calls if command[1:2] == ["stop"]]
        self.assertEqual(2, len(starts))
        self.assertEqual(starts, stops)

    def test_a_denied_provider_that_starts_on_its_own_names_neither_provider_alone_nor_the_system_trace_provider(self) -> None:
        reason = self.denial(SessionStandIn(1, DENIED_AT_PROVIDER))
        assert reason is not None
        self.assertIn("Microsoft-Windows-Kernel-Process or Microsoft-Windows-Kernel-File", reason)
        self.assertNotIn("SystemTraceProvider", reason)

    def test_a_probe_that_does_not_end_in_time_counts_as_denied(self) -> None:
        def run(command: list[str], **options) -> subprocess.CompletedProcess[str]:
            if Path(command[0]).name.lower() == "logman.exe" and command[1] == "start":
                raise subprocess.TimeoutExpired(command, options.get("timeout") or 0)
            return SessionStandIn(1, DENIED_AT_PROVIDER)(command, **options)

        with mock.patch.object(recording_fixtures.subprocess, "run", run):
            reason = recording_fixtures.etw_session_denial("dotnet")
        assert reason is not None
        self.assertIn("Microsoft-Windows-Kernel-Process; Microsoft-Windows-Kernel-File", reason)

    def test_a_consumer_that_fails_for_another_reason_is_named_with_its_first_line(self) -> None:
        stand_in = SessionStandIn(1, "You must install or update .NET to run this application.\n")
        reason = self.denial(stand_in)
        assert reason is not None
        self.assertIn("did not start", reason)
        self.assertIn("You must install or update .NET to run this application.", reason)
        self.assertIn("tests/etw_access.ps1", reason)
        self.assertEqual(["dotnet"], stand_in.programs())

    def test_every_test_of_a_gated_class_skips_with_the_reason(self) -> None:
        @recording_fixtures.etw_gate("a live ETW session was denied: SystemTraceProvider")
        class Gated(unittest.TestCase):
            def test_one(self) -> None:
                pass

            def test_two(self) -> None:
                pass

        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(Gated).run(result)
        self.assertEqual(2, result.testsRun)
        self.assertEqual(["a live ETW session was denied: SystemTraceProvider"] * 2, [reason for _, reason in result.skipped])

    def test_an_open_gate_runs_the_tests(self) -> None:
        @recording_fixtures.etw_gate(None)
        class Gated(unittest.TestCase):
            def test_one(self) -> None:
                pass

        result = unittest.TestResult()
        unittest.defaultTestLoader.loadTestsFromTestCase(Gated).run(result)
        self.assertEqual((1, []), (result.testsRun, result.skipped))

    def test_the_gate_does_not_ask_whether_the_user_is_an_administrator(self) -> None:
        self.assertNotIn("IsUserAnAdmin", Path(recording_fixtures.__file__).read_text(encoding="utf-8"))


PROGRAM = "actions/recording/etw-consumer/Program.cs"
FIXTURES = "tests/recording_fixtures.py"
ACCESS = "tests/etw_access.ps1"
SYSTEM_TRACE_PROVIDER = "9e814aad-3204-11d2-9a82-006008a86939"


class ConsumerProviders(NamedTuple):
    """What the consumer's source enables: the keywords of each provider it enables by GUID, the field that holds each
    such GUID, and whether it enables the kernel Process flag."""

    keywords: dict[str, int]
    fields: dict[str, str]
    kernel_process: bool


def provider_sources() -> dict[str, str]:
    """The text of the consumer's source, the gate's fixtures and the ETW access script, by path."""
    return {path: (ROOT / path).read_text(encoding="utf-8") for path in (PROGRAM, FIXTURES, ACCESS)}


def consumer_providers(program: str) -> ConsumerProviders:
    """The providers that the consumer's source ``program`` enables."""
    program = re.sub(r"//.*", "", program)
    fields = {name: guid.lower() for name, guid in re.findall(r'\bGuid\s+(\w+)\s*=\s*new\s+Guid\("([^"]+)"\)', program)}
    constants = {name: int(value, 16) for name, value in re.findall(r"\bconst\s+ulong\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+)", program)}
    keywords: dict[str, int] = {}
    for provider, mask in re.findall(r"\.EnableProvider\(\s*([^,]+?)\s*,[^,;]*,\s*([^,)]+?)\s*[,)]", program):
        literal = re.fullmatch(r'new\s+Guid\("([^"]+)"\)', provider)
        guid = literal.group(1).lower() if literal else fields[provider]
        keywords[guid] = constants[mask] if mask in constants else int(mask, 16)
    process = re.search(r"\.EnableKernelProvider\([^;]*\bKeywords\.Process\b", program) is not None
    return ConsumerProviders(keywords, {guid: name for name, guid in fields.items()}, process)


def gate_providers(fixtures: str) -> dict[str, tuple[str, int]]:
    """The name and keywords of each provider, by GUID, in ``ETW_PROVIDERS`` of the fixture source ``fixtures``."""
    assignments = {statement.targets[0].id: statement.value for statement in ast.parse(fixtures).body
                   if isinstance(statement, ast.Assign) and len(statement.targets) == 1 and isinstance(statement.targets[0], ast.Name)}

    def value(node: ast.expr) -> object:
        if isinstance(node, ast.Name):
            return value(assignments[node.id])
        if isinstance(node, ast.Tuple):
            return tuple(value(item) for item in node.elts)
        if isinstance(node, ast.Dict):
            return {value(key): value(item) for key, item in zip(node.keys, node.values) if key is not None}
        return ast.literal_eval(node)

    providers = value(assignments["ETW_PROVIDERS"])
    assert isinstance(providers, dict)
    return {guid.strip("{}").lower(): (name, int(keywords, 16)) for name, (guid, keywords) in providers.items()}


def setup_providers(access: str) -> dict[str, str]:
    """The name of each provider, by GUID, in ``$Providers`` of the ETW access script ``access``."""
    block = re.search(r"^\$Providers\s*=\s*\[ordered\]@\{\n(.*?)^\}", access, re.MULTILINE | re.DOTALL)
    assert block is not None, "$Providers"
    entries = re.sub(r"#.*", "", block.group(1))
    return {guid.lower(): name for guid, name in re.findall(r"'([0-9A-Fa-f-]+)'\s*=\s*@\{\s*Name\s*=\s*'([^']+)'", entries)}


def provider_set_differences(program: str, fixtures: str, access: str) -> list[str]:
    """Each difference between the providers that the consumer enables by GUID (``program``), that the gate probes
    (``fixtures``) and that the ETW access setup grants (``access``), naming the file that differs and the provider; the
    setup's SystemTraceProvider is compared with the consumer's kernel Process flag instead. The consumer's keywords are
    the ones the gate probes with."""
    consumer = consumer_providers(program)
    probed = gate_providers(fixtures)
    granted = setup_providers(access)
    sets = {PROGRAM: set(consumer.keywords), FIXTURES: set(probed), ACCESS: set(granted) - {SYSTEM_TRACE_PROVIDER}}
    verbs = {PROGRAM: "enables", FIXTURES: "probes", ACCESS: "grants"}
    differences = []
    for guid in sorted(set().union(*sets.values())):
        name = probed[guid][0] if guid in probed else granted.get(guid) or consumer.fields.get(guid, "the provider")
        present = [path for path, guids in sets.items() if guid in guids]
        absent = [path for path in sets if path not in present]
        if not absent:
            continue
        if len(absent) == 1:
            differences.append(f"{absent[0]} lacks {name} {{{guid}}}, which {', '.join(present)} name")
        else:
            differences.append(f"{present[0]} alone {verbs[present[0]]} {name} {{{guid}}}, which {', '.join(absent)} lack")
    if consumer.kernel_process and SYSTEM_TRACE_PROVIDER not in granted:
        differences.append(f"{ACCESS} lacks SystemTraceProvider {{{SYSTEM_TRACE_PROVIDER}}}, which the kernel Process flag of {PROGRAM} needs")
    for guid in sorted(set(consumer.keywords) & set(probed)):
        if consumer.keywords[guid] != probed[guid][1]:
            differences.append(f"{FIXTURES} probes {probed[guid][0]} {{{guid}}} with keywords 0x{probed[guid][1]:X}, which {PROGRAM} "
                               f"enables with 0x{consumer.keywords[guid]:X}")
    return differences


EXTRA_PROVIDER = "3d6fa8d1-fe05-11d0-9dda-00c04fd7ba7c"
SYSTEM_TRACE_ENTRY = f"    '{SYSTEM_TRACE_PROVIDER}' = @{{ Name = 'SystemTraceProvider'; Mask = 0x0FF7 }}\n"
ENABLE_KERNEL_FILE = "session.EnableProvider(KernelFile, TraceEventLevel.Verbose, FileKeywords);"
ENABLE_KERNEL_PROCESS = "session.EnableProvider(KernelProcess, TraceEventLevel.Verbose, ProcessKeywords);"


def setup_entry(name: str) -> str:
    """An entry of ``$Providers`` for the extra provider under ``name``, placed before SystemTraceProvider's."""
    return f"    '{EXTRA_PROVIDER}' = @{{ Name = '{name}'; Mask = 0x0081 }}\n" + SYSTEM_TRACE_ENTRY


def gate_entry(name: str) -> str:
    """The end of ``ETW_PROVIDERS`` with the extra provider under ``name``."""
    return f'(KERNEL_FILE, "0xF0"), "{name}": ("{{{EXTRA_PROVIDER}}}", "0x10")}}'


class EtwProviderSetTests(unittest.TestCase):
    """The providers that the ETW consumer enables, that the gate probes and that the ETW access setup grants are one
    set; each failure case changes a copy of a source."""

    def changed(self, texts: dict[str, str], source: str, old: str, new: str) -> dict[str, str]:
        """``texts`` with the one occurrence of ``old`` in ``source`` replaced by ``new``."""
        self.assertEqual(1, texts[source].count(old), f"{old!r} in {source}")
        return {**texts, source: texts[source].replace(old, new)}

    def differences(self, source: str | None = None, old: str = "", new: str = "") -> list[str]:
        """The differences of the three sources, with ``old`` replaced by ``new`` in ``source`` when given."""
        texts = provider_sources() if source is None else self.changed(provider_sources(), source, old, new)
        return provider_set_differences(texts[PROGRAM], texts[FIXTURES], texts[ACCESS])

    def assert_named(self, differences: list[str], source: str, provider: str) -> None:
        self.assertTrue(differences, "no difference")
        for difference in differences:
            self.assertTrue(difference.startswith(source), difference)
        self.assertTrue(any(provider in difference for difference in differences), differences)

    def test_the_consumer_the_gate_and_the_setup_name_one_provider_set(self) -> None:
        self.assertEqual([], self.differences())
        consumer = consumer_providers(provider_sources()[PROGRAM])
        self.assertEqual({recording_fixtures.KERNEL_FILE.strip("{}"): 0xF0, recording_fixtures.KERNEL_PROCESS.strip("{}"): 0x10}, consumer.keywords)
        self.assertTrue(consumer.kernel_process)

    def test_a_guid_that_differs_in_one_file_names_the_file_and_the_provider(self) -> None:
        for provider, (guid, _) in recording_fixtures.ETW_PROVIDERS.items():
            guid = guid.strip("{}")
            for source in (PROGRAM, FIXTURES, ACCESS):
                with self.subTest(provider=provider, source=source):
                    self.assert_named(self.differences(source, guid, guid[:-1] + "0"), source, provider)

    def test_a_provider_that_the_consumer_enables_and_the_gate_or_the_setup_lacks_fails(self) -> None:
        texts = self.changed(provider_sources(), PROGRAM, ENABLE_KERNEL_FILE,
                             f'{ENABLE_KERNEL_FILE}\n                session.EnableProvider(new Guid("{EXTRA_PROVIDER}"), TraceEventLevel.Verbose, '
                             "0x10, new TraceEventProviderOptions());")
        self.assertEqual(0x10, consumer_providers(texts[PROGRAM]).keywords[EXTRA_PROVIDER])
        cases = {(FIXTURES, ACCESS): texts, (FIXTURES,): self.changed(texts, ACCESS, SYSTEM_TRACE_ENTRY, setup_entry("Extra")),
                 (ACCESS,): self.changed(texts, FIXTURES, '(KERNEL_FILE, "0xF0")}', gate_entry("Extra"))}
        for lacking, case in cases.items():
            with self.subTest(lacking=lacking):
                differences = provider_set_differences(case[PROGRAM], case[FIXTURES], case[ACCESS])
                self.assertEqual(1, len(differences), differences)
                self.assertIn(EXTRA_PROVIDER, differences[0])
                for source in lacking:
                    self.assertIn(source, differences[0])

    def test_a_kernel_provider_that_the_gate_or_the_setup_names_and_the_consumer_does_not_enable_fails(self) -> None:
        with self.subTest("the gate"):
            self.assert_named(self.differences(FIXTURES, '(KERNEL_FILE, "0xF0")}', gate_entry("Microsoft-Windows-Kernel-Extra")),
                              FIXTURES, "Microsoft-Windows-Kernel-Extra")
        with self.subTest("the setup"):
            self.assert_named(self.differences(ACCESS, SYSTEM_TRACE_ENTRY, setup_entry("Microsoft-Windows-Kernel-Extra")),
                              ACCESS, "Microsoft-Windows-Kernel-Extra")
        for change, new in (("removed", ""), ("commented out", "// " + ENABLE_KERNEL_PROCESS)):
            with self.subTest("a provider the consumer stops enabling", change=change):
                self.assert_named(self.differences(PROGRAM, ENABLE_KERNEL_PROCESS, new), PROGRAM, "Microsoft-Windows-Kernel-Process")

    def test_a_setup_without_the_system_trace_provider_fails_while_the_consumer_enables_the_kernel_process_flag(self) -> None:
        for change, new in (("removed", ""), ("commented out", "    # " + SYSTEM_TRACE_ENTRY.lstrip())):
            with self.subTest(change):
                self.assert_named(self.differences(ACCESS, SYSTEM_TRACE_ENTRY, new), ACCESS, "SystemTraceProvider")

    def test_keywords_that_the_gate_probes_and_the_consumer_does_not_enable_fail(self) -> None:
        with self.subTest("the gate"):
            self.assert_named(self.differences(FIXTURES, '(KERNEL_FILE, "0xF0")', '(KERNEL_FILE, "0xF1")'), FIXTURES, "Microsoft-Windows-Kernel-File")
        with self.subTest("the consumer"):
            differences = self.differences(PROGRAM, "const ulong ProcessKeywords = 0x10;", "const ulong ProcessKeywords = 0x30;")
            self.assert_named(differences, FIXTURES, "Microsoft-Windows-Kernel-Process")
            self.assertIn(PROGRAM, differences[0])
            self.assertIn("0x30", differences[0])


ELEVATION_REQUESTS = re.compile(r"\brunas\b|-Verb\s+RunAs|ShellExecute|Start-Process\b.*-Verb", re.IGNORECASE)


class NoElevationTests(unittest.TestCase):
    def test_neither_the_etw_gate_and_its_tests_nor_the_recording_step_requests_elevation(self) -> None:
        sources = ["tests/recording_fixtures.py", "tests/record_etw_fixture.py", "tests/etw_access.ps1", "actions/recording/recording.py",
                   "actions/recording/action.yml", "actions/recording/etw-consumer/Program.cs"]
        found = [f"{source}:{number}" for source in sources
                 for number, line in enumerate((ROOT / source).read_text(encoding="utf-8").splitlines(), 1) if ELEVATION_REQUESTS.search(line)]
        # This file names the patterns themselves; its own code starts PowerShell and the consumer stand-in alone.
        own = Path(__file__).read_text(encoding="utf-8").replace(ELEVATION_REQUESTS.pattern, "")
        found += [f"tests/test_windows_recording.py:{number}" for number, line in enumerate(own.splitlines(), 1) if ELEVATION_REQUESTS.search(line)]
        self.assertEqual([], found)


@unittest.skipUnless(sys.platform == "win32", "the recording step's ETW consumer runs on Windows")
class EtwStartFailureTests(unittest.TestCase):
    def test_a_consumer_that_exits_before_its_session_runs_names_the_setup_beside_administrator_rights(self) -> None:
        fixture = RecordingFixture(self.addCleanup, {"lib/data.txt": ""}, [suite("a", ["lib/data.txt"], paths=[])])
        tools = fixture.base / "tools"
        tools.mkdir()
        (tools / "dotnet.cmd").write_text("@echo System.UnauthorizedAccessException: Access is denied.\r\n@exit /b 3\r\n", encoding="ascii")
        system = Path(os.environ["SystemRoot"]) / "System32"
        started = fixture.run_with_path(os.pathsep.join([str(tools), str(system)]), "start", "--output", str(fixture.output))
        self.assertEqual(2, started.returncode, started.stdout + started.stderr)
        self.assertIn("exited with 3 before its session ran", started.stderr)
        self.assertIn("administrator rights", started.stderr)
        self.assertIn("tests/etw_access.ps1", started.stderr)
        self.assertIn("Access is denied.", started.stderr)


SCRIPT = ROOT / "tests" / "etw_access.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


class EtwAccessScriptTests(unittest.TestCase):
    """The setup, restore and inspect actions change machine security settings and are verified by hand from an elevated
    PowerShell; these cases check what holds without elevation."""

    def test_the_script_names_no_path_or_account_of_this_machine(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(?<![A-Za-z])[A-Za-z]:\\", text), "an absolute path")
        for variable in ("USERNAME", "USER", "COMPUTERNAME", "USERDOMAIN", "USERPROFILE"):
            value = os.environ.get(variable)
            if value and len(value) > 2:
                self.assertNotIn(value.lower(), text.lower(), variable)
        self.assertNotIn("S-1-5-21-", text, "the SID of a particular account")

    @unittest.skipIf(POWERSHELL is None, "PowerShell is not on PATH")
    def test_the_script_parses(self) -> None:
        command = f"$errors = $null; [void][System.Management.Automation.Language.Parser]::ParseFile('{str(SCRIPT).replace("'", "''")}', [ref]$null, [ref]$errors); $errors | ForEach-Object {{ $_.ToString() }}"
        parsed = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, check=False)
        self.assertEqual((0, ""), (parsed.returncode, parsed.stdout.strip()), parsed.stderr)

    @unittest.skipUnless(sys.platform == "win32" and POWERSHELL is not None, "the script runs in PowerShell on Windows")
    def test_without_elevation_every_action_refuses_and_requests_no_elevation(self) -> None:
        import ctypes

        if ctypes.windll.shell32.IsUserAnAdmin():  # type: ignore[attr-defined]
            self.skipTest("the process is elevated; the actions would change this machine")
        for action in ("inspect", "setup", "restore"):
            with self.subTest(action=action):
                ran = subprocess.run([str(POWERSHELL), "-NoProfile", "-NonInteractive", "-File", str(SCRIPT), action], capture_output=True,
                                     text=True, check=False, timeout=120)
                self.assertNotEqual(0, ran.returncode, ran.stdout + ran.stderr)
                self.assertIn("elevated PowerShell", ran.stdout + ran.stderr)


if __name__ == "__main__":
    unittest.main()
