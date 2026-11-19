"""The temporary files of the tests: the process folder of a test process, its removal when the process ends, the
removal of the folders that ended test processes left behind, and the removal function with its read-only handling,
bounded retry and reports."""

from __future__ import annotations

import errno
import io
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import temporary_support
from tests.temporary_support import PLATFORM, RemovalError, folder_name, process_started, remove_tree

ROOT = Path(__file__).resolve().parents[1]
OTHER_PLATFORM = "linux" if PLATFORM == "windows" else "windows"


def ended_process() -> int:
    """The process id of a process that has ended."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid


def failing_unlink(name: str):
    """An ``os.unlink`` that fails for each file named ``name`` and removes any other."""
    real = os.unlink

    def unlink(path, *arguments, **keywords):
        if os.path.basename(os.fspath(path)) == name:
            raise OSError(errno.EIO, "input/output error (injected)", os.fspath(path))
        return real(path, *arguments, **keywords)
    return unlink


def unreadable(real, name: str):
    """``real``, ``os.open`` or ``os.scandir``, failing with PermissionError for each directory named ``name``, as a
    directory whose permissions no removal can change."""

    def function(path, *arguments, **keywords):
        if isinstance(path, (str, os.PathLike)) and os.path.basename(os.fspath(path)) == name:
            raise PermissionError(errno.EACCES, "permission denied (injected)", os.fspath(path))
        return real(path, *arguments, **keywords)
    return function


def git(directory: Path, *arguments: str) -> None:
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
                    "-C", str(directory), *arguments], check=True, capture_output=True)


def tree(path: Path) -> list[str]:
    return sorted(str(item.relative_to(path)) for item in path.rglob("*"))


class ProcessFolderTests(unittest.TestCase):
    def test_the_process_folder_lies_in_the_repository_folder_named_by_platform_process_id_and_start_time(self) -> None:
        folder = temporary_support.process_folder()
        self.assertEqual(Path(tempfile.gettempdir()) / "repo-tests" / "SharedWorkflows", folder.parent)
        self.assertEqual(f"{PLATFORM}-{os.getpid()}-{process_started(os.getpid())}", folder.name)
        self.assertTrue(folder.is_dir())

    def test_a_directory_and_a_file_lie_in_the_process_folder_and_go_with_their_cleanup(self) -> None:
        cleanups: list = []
        directory = temporary_support.make_directory(lambda *call: cleanups.append(call), prefix="case-")
        file = temporary_support.make_file(lambda *call: cleanups.append(call), suffix=".txt")
        self.assertEqual(temporary_support.process_folder(), directory.parent)
        self.assertEqual(temporary_support.process_folder(), file.parent)
        self.assertTrue(directory.name.startswith("case-") and file.name.endswith(".txt"))
        for function, *arguments in cleanups:
            function(*arguments)
        self.assertFalse(directory.exists() or file.exists())

    def test_a_with_block_yields_the_directory_and_removes_it(self) -> None:
        with temporary_support.Directory(prefix="block-") as name:
            (Path(name) / "file").write_text("text", encoding="utf-8")
            self.assertEqual(temporary_support.process_folder(), Path(name).parent)
        self.assertFalse(Path(name).exists())

    def test_the_folder_follows_a_change_of_the_temporary_directory(self) -> None:
        other = temporary_support.make_directory(self.addCleanup)
        with mock.patch.object(tempfile, "tempdir", str(other)):
            folder = temporary_support.process_folder()
            self.assertEqual(other / "repo-tests" / "SharedWorkflows", folder.parent)
            self.addCleanup(temporary_support._FOLDERS.pop, folder.parent, None)
        self.assertNotEqual(folder, temporary_support.process_folder())

    def test_a_child_gets_the_bytecode_cache_of_the_environment_or_one_in_the_process_folder(self) -> None:
        with mock.patch.dict(os.environ, {"PYTHONPYCACHEPREFIX": "/set/elsewhere"}):
            self.assertEqual("/set/elsewhere", temporary_support.bytecode_cache())
        with mock.patch.dict(os.environ):
            os.environ.pop("PYTHONPYCACHEPREFIX", None)
            self.assertEqual(temporary_support.process_folder(), Path(temporary_support.bytecode_cache()).parent)


class SweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = temporary_support.make_directory(self.addCleanup, prefix="sweep-")

    def folder(self, name: str) -> Path:
        folder = self.root / name
        (folder / "inside").mkdir(parents=True)
        (folder / "inside" / "file").write_text("left behind", encoding="utf-8")
        return folder

    def test_folders_of_a_process_id_that_runs_no_process_or_another_process_go(self) -> None:
        own = process_started(os.getpid())
        assert own is not None
        ended = self.folder(folder_name(ended_process(), 1))
        reused = self.folder(folder_name(os.getpid(), own + 1))
        self.assertEqual(sorted([ended, reused]), sorted(temporary_support.sweep(self.root)))
        self.assertEqual([], tree(self.root))

    def test_folders_of_a_live_process_another_platform_an_unreadable_name_or_an_unqueried_process_stay(self) -> None:
        live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(live.wait)
        self.addCleanup(live.terminate)
        started = process_started(live.pid)
        assert started is not None
        stay = [self.folder(folder_name(live.pid, started)), self.folder(f"{OTHER_PLATFORM}-{ended_process()}-1"),
                self.folder(f"{PLATFORM}-process-1"), self.folder("unnamed")]
        self.assertEqual([], temporary_support.sweep(self.root))
        unqueried = self.folder(folder_name(ended_process(), 1))
        stay.append(unqueried)
        before = tree(self.root)
        with mock.patch.object(temporary_support, "process_started", side_effect=PermissionError(errno.EACCES, "access denied")):
            self.assertEqual([], temporary_support.sweep(self.root))
        self.assertEqual(before, tree(self.root))
        self.assertTrue(all(folder.is_dir() for folder in stay))

    def test_a_folder_that_cannot_be_removed_is_reported_on_standard_error_and_the_sweep_goes_on(self) -> None:
        stuck = self.folder(folder_name(ended_process(), 1))
        (stuck / "stuck").write_text("held", encoding="utf-8")
        other = self.folder(folder_name(ended_process(), 2))
        error = io.StringIO()
        with mock.patch("os.unlink", failing_unlink("stuck")), mock.patch("sys.stderr", error):
            removed = temporary_support.sweep(self.root)
        self.assertEqual([other], removed)
        self.assertIn(str(stuck), error.getvalue())
        self.assertIn("injected", error.getvalue())


class RemoveTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = temporary_support.make_directory(self.addCleanup, prefix="remove-")

    def test_read_only_files_and_directories_go(self) -> None:
        target = self.directory / "tree"
        (target / "nested").mkdir(parents=True)
        for path in (target / "file", target / "nested" / "file"):
            path.write_text("read-only", encoding="utf-8")
            path.chmod(stat.S_IREAD)
        (target / "nested").chmod(stat.S_IREAD | stat.S_IEXEC)
        remove_tree(target)
        self.assertFalse(target.exists())

    def test_a_git_repository_with_read_only_object_files_goes(self) -> None:
        target = self.directory / "repository"
        target.mkdir()
        (target / "file").write_text("content\n", encoding="utf-8")
        git(target, "init", "-q")
        git(target, "add", "-A")
        git(target, "commit", "-q", "-m", "fixture")
        objects = [path for path in (target / ".git" / "objects").rglob("*") if path.is_file()]
        self.assertTrue(objects)
        if sys.platform == "win32":
            self.assertTrue(all(not os.access(path, os.W_OK) for path in objects), "Git keeps its object files read-only")
        remove_tree(target)
        self.assertFalse(target.exists())

    def test_a_path_that_vanished_counts_as_removed(self) -> None:
        remove_tree(self.directory / "never-there")
        file = self.directory / "file"
        file.write_text("text", encoding="utf-8")
        with mock.patch("os.unlink", side_effect=FileNotFoundError(errno.ENOENT, "gone")):
            remove_tree(file)

    def test_a_removal_step_that_raises_names_the_path(self) -> None:
        target = self.directory / "tree"
        target.mkdir()
        (target / "stuck").write_text("text", encoding="utf-8")
        with mock.patch("os.unlink", failing_unlink("stuck")), self.assertRaises(RemovalError) as raised:
            remove_tree(target)
        self.assertIn(str(target), str(raised.exception))
        self.assertIn("injected", str(raised.exception))

    def test_a_directory_that_stays_unreadable_fails_with_its_path_after_one_more_try(self) -> None:
        target = self.directory / "tree"
        (target / "closed").mkdir(parents=True)
        (target / "closed" / "file").write_text("text", encoding="utf-8")
        with (mock.patch("os.open", unreadable(os.open, "closed")), mock.patch("os.scandir", unreadable(os.scandir, "closed")),
              mock.patch.object(temporary_support, "_make_writable"), self.assertRaises(RemovalError) as raised):
            remove_tree(target)
        self.assertIn(str(target), str(raised.exception))
        self.assertIn("closed", str(raised.exception))

    @unittest.skipUnless(sys.platform == "win32", "only Windows keeps a file open against its removal")
    def test_a_file_held_open_past_the_retry_bound_fails_with_its_path(self) -> None:
        target = self.directory / "held"
        target.mkdir()
        handle = open(target / "file", "w", encoding="utf-8")
        self.addCleanup(handle.close)
        started = time.monotonic()
        with mock.patch.object(temporary_support, "REMOVAL_RETRY_SECONDS", 0.5), self.assertRaises(RemovalError) as raised:
            remove_tree(target)
        self.assertGreaterEqual(time.monotonic() - started, 0.5)
        self.assertIn(str(target), str(raised.exception))

    @unittest.skipUnless(sys.platform == "win32", "only Windows keeps a file open against its removal")
    def test_a_file_held_open_within_the_retry_bound_goes(self) -> None:
        target = self.directory / "held"
        target.mkdir()
        handle = open(target / "file", "w", encoding="utf-8")
        threading.Timer(0.3, handle.close).start()
        remove_tree(target)
        self.assertFalse(target.exists())

    @unittest.skipIf(sys.platform == "win32", "Windows has no directory write permission")
    def test_a_file_in_a_directory_without_write_permission_goes(self) -> None:
        target = self.directory / "locked"
        (target / "inner").mkdir(parents=True)
        (target / "inner" / "file").write_text("text", encoding="utf-8")
        (target / "inner").chmod(stat.S_IREAD | stat.S_IEXEC)
        (target / "closed").mkdir()
        (target / "closed" / "file").write_text("text", encoding="utf-8")
        (target / "closed").chmod(0)
        remove_tree(target)
        self.assertFalse(target.exists())


class CleanupReportTests(unittest.TestCase):
    def run_inner(self, case: type[unittest.TestCase]) -> unittest.TestResult:
        result = unittest.TestResult()
        with mock.patch("os.unlink", failing_unlink("stuck")):
            unittest.defaultTestLoader.loadTestsFromTestCase(case).run(result)
        return result

    def test_a_failed_removal_in_a_case_cleanup_is_an_error_of_the_case_that_names_the_path(self) -> None:
        created: list[Path] = []

        class Inner(unittest.TestCase):
            def test_inner(self) -> None:
                directory = temporary_support.make_directory(self.addCleanup)
                (directory / "stuck").write_text("text", encoding="utf-8")
                created.append(directory)

        result = self.run_inner(Inner)
        self.addCleanup(remove_tree, created[0])
        self.assertEqual(1, len(result.errors))
        self.assertIn("test_inner", str(result.errors[0][0]))
        self.assertIn(str(created[0]), result.errors[0][1])

    def test_a_failed_removal_in_a_class_cleanup_is_an_error_of_the_class_that_names_the_path(self) -> None:
        created: list[Path] = []

        class Inner(unittest.TestCase):
            @classmethod
            def setUpClass(cls) -> None:
                directory = temporary_support.make_directory(cls.addClassCleanup)
                (directory / "stuck").write_text("text", encoding="utf-8")
                created.append(directory)

            def test_inner(self) -> None:
                pass

        result = self.run_inner(Inner)
        self.addCleanup(remove_tree, created[0])
        self.assertEqual(1, len(result.errors))
        self.assertIn("Inner", str(result.errors[0][0]))
        self.assertIn(str(created[0]), result.errors[0][1])

    def test_a_failed_removal_at_the_end_of_a_with_block_names_the_path(self) -> None:
        with mock.patch("os.unlink", failing_unlink("stuck")), self.assertRaises(RemovalError) as raised:
            with temporary_support.Directory() as name:
                (Path(name) / "stuck").write_text("text", encoding="utf-8")
        self.addCleanup(remove_tree, name)
        self.assertIn(name, str(raised.exception))


CHILD = """\
import os
import sys
import unittest

sys.path.insert(0, {root!r})
from tests import temporary_support
{prelude}

class Child(unittest.TestCase):
    def test_passes(self):
        directory = temporary_support.make_directory(self.addCleanup)
        (directory / "file").write_text("text", encoding="utf-8")
        temporary_support.make_file()
{failing}

if __name__ == "__main__":
    unittest.main(verbosity=2)
"""

FAILING_CASE = """\
    def test_fails(self):
        with temporary_support.Directory() as name:
            self.fail("this case fails")
"""


class ProcessEndTests(unittest.TestCase):
    """Test processes of their own, each with a fresh temporary directory."""

    def setUp(self) -> None:
        self.temporary = temporary_support.make_directory(self.addCleanup, prefix="child-temp-")
        self.root = self.temporary / "repo-tests" / "SharedWorkflows"
        self.scripts = temporary_support.make_directory(self.addCleanup, prefix="child-script-")

    def run_child(self, *, prelude: str = "", failing: bool = False) -> subprocess.CompletedProcess[str]:
        script = self.scripts / "child.py"
        script.write_text(CHILD.format(root=str(ROOT), prelude=textwrap.dedent(prelude), failing=FAILING_CASE if failing else ""),
                          encoding="utf-8")
        temporary = str(self.temporary)
        environment = {**os.environ, "TMPDIR": temporary, "TEMP": temporary, "TMP": temporary,
                       "PYTHONPYCACHEPREFIX": temporary_support.bytecode_cache()}
        return subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=environment, check=False, timeout=120)

    def test_a_process_that_passes_or_fails_leaves_only_the_empty_repository_folder(self) -> None:
        for failing in (False, True):
            with self.subTest(failing=failing):
                child = self.run_child(failing=failing)
                self.assertEqual(1 if failing else 0, child.returncode, child.stderr)
                self.assertEqual(["repo-tests"], [path.name for path in self.temporary.iterdir()])
                self.assertEqual([], list(self.root.iterdir()))

    def test_a_process_folder_that_cannot_be_removed_at_the_end_is_reported_and_the_exit_code_is_not_zero(self) -> None:
        child = self.run_child(prelude="""
            import errno
            real = os.unlink
            def unlink(path, *arguments, **keywords):
                if os.path.basename(os.fspath(path)) == "stuck":
                    raise OSError(errno.EIO, "input/output error (injected)", os.fspath(path))
                return real(path, *arguments, **keywords)
            os.unlink = unlink
            (temporary_support.process_folder() / "stuck").write_text("text", encoding="utf-8")
        """)
        self.assertIn("OK", child.stderr)
        self.assertNotEqual(0, child.returncode)
        folder = next(self.root.iterdir())
        self.addCleanup(remove_tree, folder)
        self.assertIn(f"cannot remove {folder}", child.stderr)
        self.assertIn("injected", child.stderr)

    def test_a_process_removes_the_folder_of_an_ended_process_when_it_starts(self) -> None:
        dead = self.root / folder_name(ended_process(), 1)
        (dead / "fixture" / ".git").mkdir(parents=True)
        (dead / "fixture" / ".git" / "object").write_text("object", encoding="utf-8")
        (dead / "fixture" / ".git" / "object").chmod(stat.S_IREAD)
        child = self.run_child()
        self.assertEqual(0, child.returncode, child.stderr)
        self.assertFalse(dead.exists())
        self.assertEqual([], list(self.root.iterdir()))

    def test_a_folder_of_an_ended_process_that_cannot_be_removed_is_reported_and_the_cases_run(self) -> None:
        dead = self.root / folder_name(ended_process(), 1)
        dead.mkdir(parents=True)
        (dead / "stuck").write_text("text", encoding="utf-8")
        child = self.run_child(prelude="""
            import errno
            real = os.unlink
            def unlink(path, *arguments, **keywords):
                if os.path.basename(os.fspath(path)) == "stuck":
                    raise OSError(errno.EIO, "input/output error (injected)", os.fspath(path))
                return real(path, *arguments, **keywords)
            os.unlink = unlink
        """)
        self.assertEqual(0, child.returncode, child.stderr)
        self.assertIn(f"cannot remove {dead}", child.stderr)
        self.assertIn("Ran 1 test", child.stderr)
        self.assertEqual([dead.name], [path.name for path in self.root.iterdir()])

    @unittest.skipUnless(sys.platform == "win32", "only Windows keeps a file open against its removal")
    def test_a_folder_of_an_ended_process_held_open_is_reported_and_the_cases_run(self) -> None:
        dead = self.root / folder_name(ended_process(), 1)
        dead.mkdir(parents=True)
        handle = open(dead / "held", "w", encoding="utf-8")
        try:
            child = self.run_child(prelude="temporary_support.REMOVAL_RETRY_SECONDS = 0.5")
        finally:
            handle.close()
        self.assertEqual(0, child.returncode, child.stderr)
        self.assertIn(f"cannot remove {dead}", child.stderr)
        self.assertIn("Ran 1 test", child.stderr)


if __name__ == "__main__":
    unittest.main()
