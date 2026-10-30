"""The recording step on Windows: exec runs each entry file as its own process with an empty bytecode cache, a live ETW
recording of the classification fixture follows the same classification rules as a stored strace recording, and one
session attributes the reads of two entry files that run at the same time to the entry file whose process made them."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tests.recording_fixtures import RecordingFixture, entry_command, requires_etw, suite
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
    talk to the consumer."""
    for part in ("units", "logs", "setups"):
        (fixture.output / part).mkdir(parents=True)
    (fixture.output / "recording.json").write_text(json.dumps({"mode": "pull-request", "trace_set": "etw", "events": "events.tsv"}),
                                                   encoding="utf-8")
    (fixture.output / "environment.json").write_text(json.dumps(dict(os.environ)), encoding="utf-8")


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


if __name__ == "__main__":
    unittest.main()
