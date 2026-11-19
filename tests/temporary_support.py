"""The temporary files of the tests. Each test process creates them in a process folder of its own, under one folder
per repository in the temporary directory, and removes that folder when it ends; a test process that starts removes the
process folders that ended test processes of its platform left behind. ``tests/README.md`` describes the layout."""

from __future__ import annotations

import atexit
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

REPOSITORY = "SharedWorkflows"
PLATFORM = "windows" if sys.platform == "win32" else "linux" if sys.platform.startswith("linux") else sys.platform
FOLDER_NAME = re.compile(r"(?P<platform>[a-z0-9]+)-(?P<pid>[0-9]+)-(?P<started>[0-9]+)")
REMOVAL_RETRY_SECONDS = 5.0
REMOVAL_RETRY_PAUSE = 0.05
# Windows errors of a removal that another process's open handle or a pending delete causes, which end on their own:
# ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION and ERROR_DIR_NOT_EMPTY.
TRANSIENT_REMOVAL_ERRORS = frozenset({5, 32, 33, 145})
# When set, each fixture build appends its kind and wall-clock time as one JSON line to the file it names.
MEASURE = "SHARED_TESTS_FIXTURE_TIMES"


class RemovalError(OSError):
    """A temporary path that could not be removed; the message names the path and the error."""


def process_started(pid: int) -> int | None:
    """The start time of the running process ``pid``, in the system's own unit, or None when no process runs under it.
    Raises PermissionError when the process cannot be queried. Built on ``process_started`` in
    ``actions/recording/recording.py`` and kept apart, so that a change to the recording step selects no test that only
    uses this module; unlike that function, it tells a process that cannot be queried from one that does not run. No
    check sends a signal to the process."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetProcessTimes.argtypes = (wintypes.HANDLE, *(ctypes.POINTER(wintypes.FILETIME),) * 4)
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: no process runs under this id
                return None
            raise PermissionError(error, f"cannot query process {pid}", None, error)
        try:
            code, times = wintypes.DWORD(), [wintypes.FILETIME() for _ in range(4)]
            # An ended process whose handle another process still holds can be opened; its exit code is no longer 259.
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
                return None
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(time) for time in times)):
                return None
            return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
        finally:
            kernel.CloseHandle(handle)
    try:
        status = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, ProcessLookupError):
        return None
    fields = status[status.rindex(")") + 2:].split()  # the command name in parentheses may hold spaces
    return None if fields[0] in ("Z", "X") else int(fields[19])


def repository_folder() -> Path:
    """The repository's folder in the temporary directory, derived anew from ``tempfile.gettempdir()`` on each call."""
    return Path(tempfile.gettempdir()) / "repo-tests" / REPOSITORY


def folder_name(pid: int, started: int) -> str:
    """The name of the process folder of the process ``pid`` that started at ``started``."""
    return f"{PLATFORM}-{pid}-{started}"


_OWNER = os.getpid()
_STARTED: int | None = None
_FOLDERS: dict[Path, Path] = {}


def process_folder() -> Path:
    """This test process's folder in the repository's folder. Its first use in a repository's folder creates it with
    one ``mkdir`` and removes the process folders of ended test processes there."""
    global _STARTED
    if os.getpid() != _OWNER:
        raise RuntimeError("a forked process has no process folder of its own")
    root = repository_folder()
    folder = _FOLDERS.get(root)
    if folder is not None:
        return folder
    if _STARTED is None:
        started = process_started(_OWNER)
        assert started is not None, "this process runs"
        _STARTED = started
    root.mkdir(parents=True, exist_ok=True)
    folder = root / folder_name(_OWNER, _STARTED)
    folder.mkdir()
    if not _FOLDERS:
        atexit.register(remove_process_folders)
    _FOLDERS[root] = folder
    sweep(root)
    return folder


def sweep(root: Path) -> list[Path]:
    """Remove each process folder of this platform in ``root`` whose process id runs no process with its start time, and
    return the removed ones. A folder of a live process, of another platform, with a name that is no process folder's
    or of a process that cannot be queried stays; a removal that fails is reported on standard error."""
    removed = []
    own = {folder.name for folder in _FOLDERS.values()}
    try:
        entries = sorted(root.iterdir())
    except OSError as error:
        print(f"temporary_support: cannot list {root}: {error}", file=sys.stderr)
        return removed
    for entry in entries:
        match = FOLDER_NAME.fullmatch(entry.name)
        if match is None or match["platform"] != PLATFORM or entry.name in own or not entry.is_dir() or entry.is_symlink():
            continue
        try:
            running = process_started(int(match["pid"]))
        except OSError:
            continue
        if running == int(match["started"]):
            continue
        try:
            remove_tree(entry)
        except RemovalError as error:
            print(f"temporary_support: {error}", file=sys.stderr)
            continue
        removed.append(entry)
    return removed


def remove_process_folders() -> None:
    """Remove this test process's folders when it ends; when one cannot be removed, report it on standard error and end
    the process with exit code 1."""
    if os.getpid() != _OWNER:
        return
    failed = False
    for folder in list(_FOLDERS.values()):
        try:
            remove_tree(folder)
        except RemovalError as error:
            print(f"temporary_support: {error}", file=sys.stderr)
            failed = True
    _FOLDERS.clear()
    if failed:
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)


def remove_tree(path: str | os.PathLike[str]) -> None:
    """Remove the file or directory tree ``path``. A file or directory that cannot be removed loses its read-only
    attribute, and on Linux its parent directory becomes writable, before another try; on Windows a removal that another
    process's handle blocks is retried for up to ``REMOVAL_RETRY_SECONDS``. A path that vanished meanwhile counts as removed.
    Raises RemovalError, naming ``path`` and the error, when the removal still fails."""
    target = Path(path)
    deadline = time.monotonic() + REMOVAL_RETRY_SECONDS
    recovered: set[str] = set()
    try:
        if target.is_symlink() or not target.is_dir():
            _retry(os.unlink, str(target), deadline)
        else:
            shutil.rmtree(target, onexc=lambda function, name, error: _recover(function, name, error, deadline, recovered))
    except FileNotFoundError:
        return
    except OSError as error:
        raise RemovalError(f"cannot remove {target}: {error}") from error


def _recover(function: Callable, name: str, error: BaseException, deadline: float, recovered: set[str]) -> None:
    """The ``onexc`` handler of ``remove_tree``: removes ``name`` after its failed step, or raises the failure. A directory
    that could not be listed or opened gets one more try, recorded in ``recovered``; a second failure raises."""
    if isinstance(error, FileNotFoundError):
        return
    if not isinstance(error, OSError):
        raise error
    if function in (os.unlink, os.remove, os.rmdir):
        _retry(function, name, deadline)
        return
    # A directory that could not be listed or opened: make it accessible and remove it with its contents.
    if not isinstance(error, PermissionError) or name in recovered:
        raise error
    recovered.add(name)
    _make_writable(name)
    shutil.rmtree(name, onexc=lambda inner, path, failure: _recover(inner, path, failure, deadline, recovered))


def _retry(function: Callable, name: str, deadline: float) -> None:
    while True:
        _make_writable(name)
        try:
            function(name)
            return
        except FileNotFoundError:
            return
        except OSError as error:
            if sys.platform == "win32" and getattr(error, "winerror", None) in TRANSIENT_REMOVAL_ERRORS and time.monotonic() < deadline:
                time.sleep(REMOVAL_RETRY_PAUSE)
                continue
            raise


def _make_writable(name: str) -> None:
    """Clear the read-only attribute of ``name``; on Linux also make it and its parent directory accessible."""
    try:
        mode = os.lstat(name).st_mode
        if stat.S_ISLNK(mode):
            return
        wanted = mode | stat.S_IWUSR | stat.S_IRUSR | (stat.S_IXUSR if stat.S_ISDIR(mode) else 0)
        if wanted != mode:
            os.chmod(name, stat.S_IMODE(wanted))
        if sys.platform != "win32":
            parent = os.path.dirname(name) or "."
            parent_mode = os.stat(parent).st_mode
            if parent_mode & (stat.S_IWUSR | stat.S_IXUSR) != stat.S_IWUSR | stat.S_IXUSR:
                os.chmod(parent, stat.S_IMODE(parent_mode | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRUSR))
    except OSError:
        pass


def make_directory(cleanup: Callable | None = None, *, prefix: str = "tmp") -> Path:
    """A new empty directory in this test process's folder. ``cleanup``, a case's ``addCleanup`` or a class's
    ``addClassCleanup``, registers its removal; a removal that fails then becomes an error of that case or class."""
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=process_folder()))
    if cleanup is not None:
        cleanup(remove_tree, path)
    return path


def make_file(cleanup: Callable | None = None, *, prefix: str = "tmp", suffix: str = "") -> Path:
    """A new empty file in this test process's folder, registered for removal like ``make_directory``."""
    descriptor, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=process_folder())
    os.close(descriptor)
    if cleanup is not None:
        cleanup(remove_tree, name)
    return Path(name)


class Directory:
    """A new empty directory in this test process's folder, removed by ``cleanup`` or at the end of a ``with`` block,
    which yields its path as a string. A removal that fails raises RemovalError naming the path."""

    def __init__(self, *, prefix: str = "tmp") -> None:
        self.path = make_directory(prefix=prefix)
        self.name = str(self.path)
        self._removed = False

    def cleanup(self) -> None:
        if not self._removed:
            self._removed = True
            remove_tree(self.path)

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, *exception: object) -> None:
        self.cleanup()


def bytecode_cache() -> str:
    """The bytecode cache that a test gives a child process: ``PYTHONPYCACHEPREFIX`` when set, otherwise a folder in this
    test process's folder."""
    return os.environ.get("PYTHONPYCACHEPREFIX") or str(process_folder() / "pycache")


@contextmanager
def measured(kind: str) -> Iterator[None]:
    """Time one build of the fixture kind ``kind`` when ``MEASURE`` names a file, and append the result to it."""
    target = os.environ.get(MEASURE)
    if not target:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        line = {"kind": kind, "seconds": round(time.perf_counter() - started, 6), "pid": os.getpid(), "argv": sys.argv}
        with open(target, "a", encoding="utf-8") as file:
            file.write(json.dumps(line) + "\n")
