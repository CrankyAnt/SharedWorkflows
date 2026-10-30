"""The lookup report of the recording step, which a ``.pth`` file in the site directory of a set-up interpreter imports
into every Python process that runs ``site``; ``recording.py provide --python`` installs both.

While ``SHARED_LOOKUPS`` names a directory, the report appends to ``<process id>.tsv`` there, as the lookups happen,
every directory whose listing the import system makes and every module name it looks up in a directory, found or absent.
Without the variable it does nothing. Each line holds tab-separated fields, with backslashes, tabs and line breaks
escaped as in the ETW consumer's event file:

    suffixes <suffix> ...                          the file suffixes of the process's path finder, in the order it tries them
    list <thread id> <directory>                   a listing of the directory by the import system
    lookup <directory> <name> found <file>         a module or package found in the directory, and the file it loads
    lookup <directory> <name> namespace            a directory of the name without an ``__init__`` file, a namespace portion
    lookup <directory> <name> absent               no module of the name in the directory

Directories and files are real paths. The report imports no module that the interpreter has not loaded before ``site``.
"""

import _imp
import _thread
import os

import _frozen_importlib_external as external

VARIABLE = "SHARED_LOOKUPS"


def _escape(text):
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


class _Report:
    def __init__(self, directory):
        self.directory = directory
        self.pid = None
        self.handle = None
        self.lock = _thread.allocate_lock()
        self.real = {}

    def real_path(self, path):
        found = self.real.get(path)
        if found is None:
            found = self.real[path] = os.path.realpath(path)
        return found

    def write(self, *fields):
        if not self.lock.acquire(False):
            return  # a lookup that the report's own writing makes
        try:
            if self.pid != os.getpid():  # the first line of this process, or of a child forked from it
                self.pid = os.getpid()
                self.handle = open(os.path.join(self.directory, f"{self.pid}.tsv"), "a", encoding="utf-8", buffering=1)
                suffixes = [*_imp.extension_suffixes(), *external.SOURCE_SUFFIXES, *external.BYTECODE_SUFFIXES]
                self.handle.write("\t".join(["suffixes", *map(_escape, suffixes)]) + "\n")
            self.handle.write("\t".join(_escape(str(field)) for field in fields) + "\n")
        except (OSError, ValueError):
            self.handle = None
        finally:
            self.lock.release()


def _install(report):
    finder = external.FileFinder
    find_spec, fill_cache = finder.find_spec, finder._fill_cache

    def reported_find_spec(self, fullname, target=None):
        spec = find_spec(self, fullname, target)
        try:
            directory, name = report.real_path(self.path), fullname.rpartition(".")[2]
            if spec is None:
                report.write("lookup", directory, name, "absent")
            elif spec.loader is None:
                report.write("lookup", directory, name, "namespace")
            else:
                origin = spec.origin or ""
                report.write("lookup", directory, name, "found", os.path.join(report.real_path(os.path.dirname(origin)), os.path.basename(origin)))
        except Exception:  # noqa: BLE001 - the report never changes what an import does
            pass
        return spec

    def reported_fill_cache(self):
        try:
            report.write("list", _thread.get_native_id(), report.real_path(self.path or os.getcwd()))
        except Exception:  # noqa: BLE001
            pass
        return fill_cache(self)

    finder.find_spec = reported_find_spec
    finder._fill_cache = reported_fill_cache


if os.environ.get(VARIABLE):
    _install(_Report(os.environ[VARIABLE]))
