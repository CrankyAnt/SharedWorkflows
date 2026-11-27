"""The classification rules of the recording step's check, on stored strace recordings of fixture entry files and on the
neutral event files of a Windows ETW recording of the same fixture, each with the lookup reports of their Python
processes."""

from __future__ import annotations

import hashlib
import json
import posixpath
import shutil
import sys
import unittest
from pathlib import Path, PureWindowsPath

from tests import temporary_support
from tests.recording_fixtures import (ETW_RECORDINGS, ROOT, STRACE_RECORDINGS, RecordingFixture, copy_recording, copy_strace_recording,
                                      load_recording, requires_posix_paths, suite)

sys.path.insert(0, str(ROOT / "actions" / "suite-selection"))

import suite_selection  # noqa: E402

STATS = """\
    import os
    os.stat("data/stat.txt")
    os.path.exists("absent.txt")
    os.path.isdir("data")
    """

IMPORTS_AND_LISTINGS = """\
    import os, subprocess, sys
    sys.path.insert(0, "lib")
    import helper
    print(sorted(os.listdir("listed")))
    subprocess.run(["ls", "lib"], check=True)
    """

SOURCES = """\
    import os
    from pathlib import Path
    for name in sorted(os.listdir("scripts")):
        print(Path("scripts", name).read_text())
    """

IMPORTS_ONLY = """\
    import sys
    sys.path.insert(0, "lib")
    import helper
    print(helper.VALUE)
    """

IGNORED = """\
    import os
    from pathlib import Path
    print(Path("cache.txt").read_text(), Path("kept.log").read_text())
    os.path.exists("build/out.bin")
    """

COMMANDS = """\
    import os, subprocess
    path = os.getcwd() + ":" + os.environ["PATH"]
    subprocess.run(["sh", "-c", "git --version"], check=True, env=dict(os.environ, PATH=path))
    os.path.exists("git.ps1")
    os.path.exists("missing-tool")
    subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", "Get-Content child-tool.ps1 -ErrorAction Ignore; exit 0"],
                   check=True, cwd="data")
    os.path.exists("notes:draft.txt")
    try:
        subprocess.run(["./run.exe"])
    except OSError:
        pass
    """

# PowerShell probes <name>.ps1 in its working directory as an entry file's own process, in the checkout root, and as a
# process that tests/commands.py starts in data/. Neither recording sees a probe of its command search, so the probe is an
# open of the absent script.
POWERSHELL_SEARCH = """\
Get-Content tool.ps1 -ErrorAction Ignore
Test-Path data/helper.ps1 | Out-Null
exit 0
"""

COPY = """\
    import os, subprocess, tempfile
    from pathlib import Path
    work = Path(tempfile.mkdtemp())
    subprocess.run(["git", "clone", "-q", os.getcwd(), str(work / "copy")], check=True)
    print((work / "copy" / "lib" / "copied.txt").read_text())
    scratch = work / "scratch"
    scratch.mkdir()
    (scratch / "note.txt").write_text("note")
    print((scratch / "note.txt").read_text())
    os.path.exists(work / "copy" / "lib" / "absent-in-copy.txt")
    subprocess.run(["git", "clone", "-q", os.path.relpath(os.getcwd(), work), "relative"], cwd=work, check=True)
    print((work / "relative" / "lib" / "relative.txt").read_text())
    """

NESTED = """\
    import os, subprocess, tempfile
    from pathlib import Path
    copy = Path(tempfile.mkdtemp()) / "copy"
    subprocess.run(["git", "clone", "-q", os.getcwd(), str(copy)], check=True)
    print((copy / "lib" / "copied.txt").read_text())
    nested = copy / "upstream"
    subprocess.run(["git", "init", "-q", str(nested)], check=True)
    subprocess.run(["git", "-C", str(nested), "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "commit", "-q",
                    "--allow-empty", "-m", "nested"], check=True)
    head = subprocess.run(["git", "-C", str(nested), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "-C", str(nested), "rev-parse", "--verify", head], check=True, capture_output=True)
    os.path.exists(nested / "absent-in-nested.txt")
    """

WORKTREE = """\
    import os, subprocess, tempfile
    from pathlib import Path
    tree = Path(tempfile.mkdtemp()) / "tree"
    subprocess.run(["git", "-C", os.getcwd(), "worktree", "add", "-q", "--detach", str(tree)], check=True)
    print((tree / "lib" / "worktree.txt").read_text())
    os.path.exists(tree / "lib" / "absent-in-worktree.txt")
    """

# A directory filled by copying files and a fixture repository of its own, each holding names that the checkout tracks.
NO_COPIES = """\
    import os, shutil, subprocess, tempfile
    from pathlib import Path
    work = Path(tempfile.mkdtemp())
    files = work / "files"
    shutil.copytree("lib", files / "lib")
    print((files / "lib" / "copied.txt").read_text())
    os.path.exists(files / "lib" / "absent-in-file-copy.txt")
    other = work / "other"
    (other / "data").mkdir(parents=True)
    (other / "data" / "stat.txt").write_text("fixture")
    identity = ["-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    subprocess.run(["git", "-C", str(other), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(other), *identity, "commit", "-q", "-m", "other"], check=True)
    subprocess.run(["git", "-C", str(other), "show", "-s", "HEAD:data/stat.txt"], check=True, capture_output=True)
    os.path.exists(other / "data" / "absent-in-fixture.txt")
    """

REVISION = """\
    import subprocess
    subprocess.run(["git", "log", "-1", "--format=%H", "HEAD"], check=True)
    """

# The script's directory tests/ comes first on the module search path and lib/ last. The entry file lists lib/ itself as
# well; an isolated child looks up modules in data/, and a child without site imports from scripts/.
LOOKUPS = """\
    import os, subprocess, sys
    sys.path.append("lib")
    import helper
    try:
        import absent_module
    except ImportError:
        pass
    print(sorted(os.listdir("lib")))
    subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path.insert(0, 'data'); import json\\ntry: import absent_isolated\\nexcept ImportError: pass"],
                   check=True)
    subprocess.run([sys.executable, "-S", "-c", "import sys; sys.path.insert(0, 'scripts'); import tool"], check=True)
    """

# Git processes in the checkout: ones that read its index, objects or refs, git ls-files printing only names, and git
# cat-file of full object ids that the checked commit holds or does not hold.
GIT_REVISIONS = """\
    import subprocess
    subprocess.run(["git", "rev-parse", "HEAD"], check=True)
    subprocess.run(["git", "ls-files", "-s"], check=True, capture_output=True)
    subprocess.run(["git", "cat-file", "-p", "HEAD:lib/helper.py"], check=True)
    """

GIT_NAMES = """\
    import os, subprocess
    subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True)
    subprocess.run(["git", "-C", os.getcwd(), "ls-files", "--cached", "--full-name", "-z", "--", "lib"], check=True, capture_output=True)
    """

# git ls-files with pathspecs: literal paths, a star and a question mark, a glob with an exclude beside it; and listings
# whose pathspecs only ** covers: a pathspec variable in the environment of the git process, a working directory below
# the checkout root, and other magic.
GIT_PATHSPECS = """\
    import subprocess
    subprocess.run(["git", "ls-files", "-z", "--", "lib/helper.py", "listed"], check=True, capture_output=True)
    subprocess.run(["git", "ls-files", "-z", "scripts/*.py", "da?a"], check=True, capture_output=True)
    subprocess.run(["git", "ls-files", "-z", "--", ":(glob)held/*.txt", ":(exclude)held/two.txt"], check=True, capture_output=True)
    """

GIT_PATHSPEC_VARIABLE = """\
    import os, subprocess
    subprocess.run(["git", "ls-files", "-z", "--", "lib"], check=True, capture_output=True, env=dict(os.environ, GIT_ICASE_PATHSPECS="1"))
    """

GIT_PATHSPEC_SUBDIRECTORY = """\
    import subprocess
    subprocess.run(["git", "ls-files", "-z", "--", "helper.py"], check=True, capture_output=True, cwd="lib")
    """

GIT_PATHSPEC_MAGIC = """\
    import subprocess
    subprocess.run(["git", "ls-files", "-z", "--", ":(icase)lib"], check=True, capture_output=True)
    """

HELD_FILES = {"held/also-one.txt": "one", "held/one.txt": "one", "held/two.txt": "two"}


def object_id(kind: str, body: bytes) -> str:
    return hashlib.sha1(f"{kind} {len(body)}\0".encode() + body).hexdigest()


ONE_BLOB_ID = object_id("blob", b"one")
HELD_TREE_ID = object_id("tree", b"".join(f"100644 {path.split('/')[1]}\0".encode() + bytes.fromhex(object_id("blob", text.encode()))
                                          for path, text in sorted(HELD_FILES.items())))
UNHELD_BLOB_ID = object_id("blob", b"held by no tracked path")

# The Python process's own reads of the Git directory, its configuration among them, are no reads.
GIT_OBJECTS = f"""\
    import subprocess
    from pathlib import Path
    print(Path(".git/config").read_text(), Path(".git/HEAD").read_text())
    subprocess.run(["git", "cat-file", "blob", "{ONE_BLOB_ID}"], check=True)
    subprocess.run(["git", "cat-file", "-t", "{HELD_TREE_ID}^{{tree}}"], check=True)
    subprocess.run(["git", "cat-file", "-e", "{UNHELD_BLOB_ID}"])
    """

# The checked commit's id, read from the Git directory without a git process; classification_fixture fixes its dates.
CHECKED_COMMIT = """\
    import subprocess, zlib
    from pathlib import Path
    head = Path(".git/HEAD").read_text().strip()
    commit = Path(".git", head[len("ref: "):]).read_text().strip() if head.startswith("ref: ") else head
"""

GIT_COMMIT = CHECKED_COMMIT + """\
    subprocess.run(["git", "cat-file", "-p", commit], check=True)
    subprocess.run(["git", "cat-file", "-t", commit + "^{commit}"], check=True)
    """

# The checked commit's tree, read from its loose object.
GIT_ROOT_TREE = CHECKED_COMMIT + """\
    tree = zlib.decompress(Path(".git", "objects", commit[:2], commit[2:]).read_bytes()).split(b"\\0", 1)[1].split()[1].decode()
    subprocess.run(["git", "cat-file", "-t", tree], check=True)
    """

# Object names that are not only full object ids: names on standard input, a full id beside a revision expression, and
# a full id with the peeling suffix ^{}.
GIT_BATCH = f"""\
    import subprocess
    subprocess.run(["git", "cat-file", "--batch"], input="{ONE_BLOB_ID}\\n", text=True, check=True, capture_output=True)
    """

GIT_MIXED = f"""\
    import subprocess
    subprocess.run(["git", "cat-file", "-e", "{ONE_BLOB_ID}", "HEAD:lib/helper.py"], capture_output=True)
    """

GIT_PEELED = f"""\
    import subprocess
    subprocess.run(["git", "cat-file", "-t", "{ONE_BLOB_ID}^{{}}"], check=True)
    """

# Git reads HEAD and probes objects and refs when it finds the Git directory, before it reads the configuration.
GIT_CONFIG = """\
    import subprocess
    subprocess.run(["git", "config", "--get", "core.bare"], check=True)
    """

# The entry files of a fixture whose checkout is a linked worktree.
LINKED_FILES = {"tests/linked_revisions.py": GIT_REVISIONS, "tests/linked_names.py": GIT_NAMES, "lib/helper.py": "VALUE = 1\n"}
LINKED_ENTRIES = ["tests/linked_revisions.py", "tests/linked_names.py"]

# On Windows the Kernel-File session sees no attribute query, so the Windows entry files make each existence read
# through an operation that it sees: a literal FindFirstFileW for a present path and an open for an absent one.
WINDOWS_PROBES = """\
    import ctypes

    def find(path):
        kernel = ctypes.windll.kernel32
        kernel.FindFirstFileW.restype = ctypes.c_void_p
        handle = kernel.FindFirstFileW(path, ctypes.create_string_buffer(1024))
        if handle != ctypes.c_void_p(-1).value:
            kernel.FindClose(ctypes.c_void_p(handle))

    def probe(path):
        try:
            open(path).close()
        except OSError:
            pass
"""

WINDOWS_STATS = WINDOWS_PROBES + """\
    find("data\\\\stat.txt")
    probe("absent.txt")
    find("data")
    """

WINDOWS_IMPORTS_AND_LISTINGS = """\
    import os, subprocess, sys
    sys.path.insert(0, "lib")
    import helper
    print(sorted(os.listdir("listed")))
    subprocess.run([sys.executable, "-I", "-c", "import os; print(os.listdir('lib'))"], check=True)
    """

WINDOWS_IGNORED = WINDOWS_PROBES + """\
    from pathlib import Path
    print(Path("cache.txt").read_text(), Path("kept.log").read_text())
    probe("build/out.bin")
    """

WINDOWS_COMMANDS = WINDOWS_PROBES + """\
    import subprocess
    subprocess.run(["git", "--version"], check=True)
    probe("git.ps1")
    probe("missing-tool")
    subprocess.run(["pwsh", "-NoProfile", "-NonInteractive", "-Command", "Get-Content child-tool.ps1 -ErrorAction Ignore; exit 0"],
                   check=True, cwd="data")
    print(open("listed\\\\one.txt::$DATA").read())
    probe("data\\\\stat.txt:Zone.Identifier")
    probe("listed\\\\one.txt:extra:$DATA")
    probe("missing.txt::$DATA")
    try:
        subprocess.run(["run.exe"])
    except OSError:
        pass
    """

# A local clone on Windows runs with a working directory outside the checkout, where the MSYS2 shell of Git's transport
# helper looks up its command text as a name.
WINDOWS_COPY = WINDOWS_PROBES + """\
    import os, subprocess, tempfile
    from pathlib import Path
    work = Path(tempfile.mkdtemp())
    subprocess.run(["git", "clone", "-q", os.getcwd(), str(work / "copy")], cwd=work, check=True)
    print((work / "copy" / "lib" / "copied.txt").read_text())
    scratch = work / "scratch"
    scratch.mkdir()
    (scratch / "note.txt").write_text("note")
    print((scratch / "note.txt").read_text())
    probe(work / "copy" / "lib" / "absent-in-copy.txt")
    """

WINDOWS_WORKTREE = WINDOWS_PROBES + WORKTREE.replace("os.path.exists(", "probe(")

WINDOWS_NO_COPIES = WINDOWS_PROBES + NO_COPIES.replace("os.path.exists(", "probe(")

DATA = {
    "scripts/tool.py": "print(1)\n", "data/stat.txt": "never opened\n", "lib/helper.py": "VALUE = 1\n", "listed/one.txt": "",
    "lib/copied.txt": "copied\n", "lib/worktree.txt": "worktree\n", "lib/relative.txt": "relative\n",
    ".gitignore": "cache.txt\n*.log\nbuild/\n", "cache.txt": "ignored\n", "kept.log": "tracked\n", **HELD_FILES,
}
FILES = {
    "tests/stats.py": STATS, "tests/imports.py": IMPORTS_AND_LISTINGS, "tests/sources.py": SOURCES, "tests/imports_only.py": IMPORTS_ONLY,
    "tests/ignored.py": IGNORED, "tests/commands.py": COMMANDS, "tests/copy.py": COPY, "tests/search.ps1": POWERSHELL_SEARCH,
    "tests/worktree.py": WORKTREE, "tests/no_copies.py": NO_COPIES, "tests/lookups.py": LOOKUPS, "tests/git_revisions.py": GIT_REVISIONS,
    "tests/git_names.py": GIT_NAMES, "tests/git_objects.py": GIT_OBJECTS, "tests/git_commit.py": GIT_COMMIT,
    "tests/git_root_tree.py": GIT_ROOT_TREE, "tests/git_batch.py": GIT_BATCH, "tests/git_mixed.py": GIT_MIXED, "tests/git_peeled.py": GIT_PEELED,
    "tests/git_config.py": GIT_CONFIG, **DATA,
}
WINDOWS_FILES = {
    **FILES, "tests/stats.py": WINDOWS_STATS, "tests/imports.py": WINDOWS_IMPORTS_AND_LISTINGS, "tests/ignored.py": WINDOWS_IGNORED,
    "tests/commands.py": WINDOWS_COMMANDS, "tests/copy.py": WINDOWS_COPY, "tests/worktree.py": WINDOWS_WORKTREE,
    "tests/no_copies.py": WINDOWS_NO_COPIES,
}
ENTRIES = ["tests/stats.py", "tests/imports.py", "tests/sources.py", "tests/imports_only.py", "tests/ignored.py", "tests/commands.py",
           "tests/copy.py", "tests/search.ps1", "tests/worktree.py", "tests/no_copies.py", "tests/lookups.py", "tests/git_revisions.py", "tests/git_names.py",
           "tests/git_objects.py", "tests/git_commit.py", "tests/git_root_tree.py", "tests/git_batch.py", "tests/git_mixed.py",
           "tests/git_peeled.py", "tests/git_config.py"]
# A fixed date gives the fixture's commit the same id in every recording of the same files.
COMMIT_DATE = "2026-01-01T00:00:00+0000"
# The recorded ETW event files hold no recording of the nested-repository entry file, and the Kernel-File session sees no
# attribute query such as Git's probe of a revision name, so only the strace recordings hold these two.
# Only the strace recordings hold the entry files of git ls-files with pathspecs.
STRACE_FILES = {**FILES, "tests/nested.py": NESTED, "tests/revision.py": REVISION, "tests/git_pathspecs.py": GIT_PATHSPECS,
                "tests/git_pathspec_variable.py": GIT_PATHSPEC_VARIABLE, "tests/git_pathspec_subdirectory.py": GIT_PATHSPEC_SUBDIRECTORY,
                "tests/git_pathspec_magic.py": GIT_PATHSPEC_MAGIC}
STRACE_ENTRIES = [*ENTRIES, "tests/nested.py", "tests/revision.py", "tests/git_pathspecs.py", "tests/git_pathspec_variable.py",
                  "tests/git_pathspec_subdirectory.py", "tests/git_pathspec_magic.py"]


def classification_fixture(cleanup, files: dict[str, str], entries: list[str] = ENTRIES, *, stand_in: bool = True) -> RecordingFixture:
    """The fixture repository of the classification rules, with one suite selected per entry file and empty lists."""
    fixture = RecordingFixture(cleanup, files, [suite("all", entries, entry_selection=True, paths=[], setups=["python"],
                                                      entry_paths={entry: [] for entry in entries})], commit=False, stand_in=stand_in)
    fixture.git("add", "-f", "kept.log")
    fixture.git("commit", "-q", "-m", "fixture", environment={"GIT_AUTHOR_DATE": COMMIT_DATE, "GIT_COMMITTER_DATE": COMMIT_DATE})
    return fixture


def linked_worktree_fixture(cleanup, *, stand_in: bool = True) -> RecordingFixture:
    """A fixture whose checkout is ``linked``, a linked worktree of the fixture repository beside it, with one suite
    selected per entry file of LINKED_ENTRIES and empty lists."""
    fixture = RecordingFixture(cleanup, LINKED_FILES, [suite("linked", LINKED_ENTRIES, entry_selection=True, paths=[], setups=["python"],
                                                             entry_paths={entry: [] for entry in LINKED_ENTRIES})], stand_in=stand_in)
    fixture.git("worktree", "add", "-q", "--detach", str(fixture.base / "linked"))
    fixture.root = fixture.base / "linked"
    return fixture


def classified_lists(fixture: RecordingFixture) -> dict:
    """The classified reads of each entry file in the full-recording result of ``fixture``, by entry file."""
    return {item["entry"]: item["reads"] for item in fixture.full_result()["entries"]}


class ClassificationRules:
    """The rules, checked on the full-recording result of ``fixture``; ``second`` gives the result of a second recording."""

    fixture: RecordingFixture

    def second(self) -> dict:
        raise NotImplementedError

    def files(self) -> dict[str, str]:
        raise NotImplementedError

    def recorded(self, files: dict[str, str]) -> RecordingFixture:
        """A new classification fixture of ``files``, holding the first recording, unchecked."""
        raise NotImplementedError

    def reads(self, entry: str) -> dict:
        return self.fixture.reads_of(entry)

    def existence(self, entry: str) -> dict[str, str]:
        return {read["path"]: read["kind"] for read in self.reads(entry)["existence_reads"]}

    def holds_absent(self, relative: str) -> bool:
        """Whether the recording holds a failed probe of the checkout path ``relative``."""
        raise NotImplementedError

    def assert_probe(self, reads: dict, relative: str) -> None:
        """The failed probe of ``relative`` is an existence read of kind ``any`` in ``reads``, or the recording holds it
        and the membership of its directory in ``reads`` covers it, so that the stored lists leave it out."""
        existence = {read["path"]: read["kind"] for read in reads["existence_reads"]}
        if relative in existence:
            self.assertEqual("any", existence[relative], relative)
            return
        self.assertIn(posixpath.dirname(relative) or ".", reads["directory_memberships"], relative)
        self.assertTrue(self.holds_absent(relative), relative)

    def test_a_python_process_s_stat_of_a_file_that_it_never_opens_is_an_existence_read(self) -> None:
        # On Windows the stat is the literal enumeration of FindFirstFileW.
        self.assertNotIn("data/stat.txt", self.reads("tests/stats.py")["paths"])
        self.assertEqual("any", self.existence("tests/stats.py")["data/stat.txt"])

    def test_a_failed_probe_and_a_stat_of_a_directory_are_existence_reads(self) -> None:
        existence = self.existence("tests/stats.py")
        self.assertEqual("any", existence["absent.txt"])
        self.assertEqual("directory", existence["data"])
        self.assertNotIn("absent.txt", self.reads("tests/stats.py")["paths"])
        self.assertNotIn("data", self.reads("tests/stats.py")["paths"])

    def test_a_listing_of_python_sources_that_the_code_reads_without_importing_is_a_directory_membership(self) -> None:
        self.assertIn("scripts", self.reads("tests/sources.py")["directory_memberships"])
        self.assertIn("scripts/tool.py", self.reads("tests/sources.py")["paths"])

    def recording_directory(self) -> Path:
        """The stored recording that ``fixture`` holds."""
        raise NotImplementedError

    def test_a_listing_that_the_lookup_report_attributes_to_the_import_system_is_no_directory_membership(self) -> None:
        reads = self.reads("tests/imports_only.py")
        self.assertIn("lib/helper.py", reads["paths"])
        self.assertEqual([], reads["directory_memberships"])
        self.assertEqual(["lib"], reads["lookup_directories"])

    def test_any_other_listing_is_a_directory_membership_decided_per_reading_process(self) -> None:
        # The Python process lists lib/ to import from it; another process lists it as well.
        self.assertEqual(["lib", "listed"], self.reads("tests/imports.py")["directory_memberships"])

    def test_a_process_s_own_listing_of_a_directory_beside_its_imports_and_a_process_without_a_report_are_memberships(self) -> None:
        # tests/lookups.py lists lib/ after importing from it; its child started with -S runs no site, so no report, and
        # every directory it lists is a membership, on Windows the checkout root as well.
        memberships = self.reads("tests/lookups.py")["directory_memberships"]
        self.assertLessEqual({"lib", "scripts"}, set(memberships))
        self.assertFalse({"tests", "data"} & set(memberships), memberships)
        self.assertIn("scripts/tool.py", self.reads("tests/lookups.py")["paths"])

    def test_an_absent_lookup_in_a_checkout_directory_is_a_module_lookup_outside_the_standard_library_names(self) -> None:
        # helper and absent_module are looked up in tests/ first, absent_module in lib/ too, and the isolated child's
        # absent_isolated in data/; json, subprocess and the other standard-library names stay out.
        reads = self.reads("tests/lookups.py")
        self.assertEqual({"data": ["absent_isolated"], "lib": ["absent_module"], "tests": ["absent_module", "helper"]}, reads["module_lookups"])
        self.assertEqual(["data", "lib", "tests"], reads["lookup_directories"])

    def test_a_found_module_is_a_content_read_with_existence_reads_of_the_names_that_would_precede_it(self) -> None:
        # The names of lib/helper with each extension-module suffix of the interpreter that recorded it precede lib/helper.py.
        # tests/imports_only.py imports it without listing lib/ itself.
        reads = self.reads("tests/imports_only.py")
        self.assertIn("lib/helper.py", reads["paths"])
        existence = self.existence("tests/imports_only.py")
        self.assertEqual("directory", existence["lib/helper"])
        extensions = [path for path, kind in existence.items() if path.startswith("lib/helper.") and kind == "any"]
        self.assertTrue(extensions, existence)
        self.assertFalse([path for path in extensions if path.endswith((".py", ".pyw", ".pyc"))], extensions)

    def test_the_stored_recordings_carry_the_lookup_report_beside_the_recording(self) -> None:
        recording = self.recording_directory()
        for path in (recording / "units").iterdir():
            record = json.loads(path.read_text(encoding="utf-8"))
            if PureWindowsPath(record["command"][0]).stem.lower() != "pwsh":
                self.assertTrue(list((recording / "lookups" / record["lookups"]).glob("*.tsv")), record["name"])

    def test_every_listing_of_a_process_without_a_lookup_report_is_a_directory_membership(self) -> None:
        # Without the lookup reports, the imports from lib/ and from the script's directory tests/ list them as the code's
        # own listing of listed/ does.
        fixture = self.recorded(self.files())
        shutil.rmtree(fixture.output / "lookups")
        checked = fixture.check()
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        self.assertEqual(["lib"], fixture.reads_of("tests/imports_only.py")["directory_memberships"])
        self.assertEqual(["lib", "listed", "tests"], fixture.reads_of("tests/imports.py")["directory_memberships"])
        self.assertEqual([], fixture.reads_of("tests/imports_only.py")["lookup_directories"])

    def test_a_read_of_an_ignored_untracked_path_is_no_read(self) -> None:
        reads = self.reads("tests/ignored.py")
        self.assertIn("kept.log", reads["paths"])
        self.assertNotIn("cache.txt", reads["paths"])
        self.assertNotIn("build/out.bin", self.existence("tests/ignored.py"))

    case_insensitive_ignore: bool = False

    def test_only_the_tracked_gitignore_files_of_the_checked_commit_decide(self) -> None:
        # listed/ matches the directory that the recording lists, absent.txt/ no directory, and Missing-Tool and
        # ABSENT-IN-COPY.TXT match only under Windows case-insensitive matching. The local settings, an untracked
        # .gitignore file and an uncommitted edit of the tracked one never take part.
        files = {path: text for path, text in self.files().items() if path != "listed/one.txt"}
        fixture = self.recorded({**files, ".gitignore": DATA[".gitignore"] + "listed/\nabsent.txt/\nMissing-Tool\nABSENT-IN-COPY.TXT\n"})
        fixture.git("config", "core.ignoreCase", "true")
        (fixture.root / ".git" / "info").mkdir(exist_ok=True)
        (fixture.root / ".git" / "info" / "exclude").write_text("absent.txt\n", encoding="utf-8")
        excludes = fixture.base / "global-ignore"
        excludes.write_text("missing-tool\n", encoding="utf-8")
        fixture.git("config", "core.excludesFile", str(excludes))
        (fixture.root / "lib" / ".gitignore").write_text("absent-in-copy.txt\n", encoding="utf-8")
        with (fixture.root / ".gitignore").open("a", encoding="utf-8") as handle:
            handle.write("absent.txt\n")
        kept = {"tests/stats.py": "absent.txt"}
        folded = {"tests/commands.py": "missing-tool", "tests/copy.py": "lib/absent-in-copy.txt"}
        expected_probes = kept if self.case_insensitive_ignore else {**kept, **folded}

        checked = fixture.check()
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        reads = classified_lists(fixture)
        for entry, path in expected_probes.items():
            self.assert_probe(reads[entry], path)
        if self.case_insensitive_ignore:
            for entry, path in folded.items():
                self.assertNotIn(path, {read["path"] for read in reads[entry]["existence_reads"]})
        self.assertNotIn("listed", reads["tests/imports.py"]["directory_memberships"])
        self.assertNotIn("cache.txt", reads["tests/ignored.py"]["paths"])

        # A pull-request run with empty lists reports the reads that only the local settings match as additions.
        settings = fixture.output / "recording.json"
        settings.write_text(settings.read_text(encoding="utf-8").replace('"full"', '"pull-request"'), encoding="utf-8")
        checked = fixture.check()
        self.assertEqual(1, checked.returncode, checked.stdout + checked.stderr)
        added = fixture.additions()["suites"][0]["entries"]
        for entry, path in expected_probes.items():
            self.assert_probe(added[entry], path)
        self.assertNotIn("cache.txt", added["tests/ignored.py"]["paths"])

    def test_an_absent_root_name_that_a_program_other_than_powershell_probes_is_an_existence_read(self) -> None:
        # The entry file starts git and then probes git.ps1 itself: no name drops because a started program shares its stem.
        for path in ("git.ps1", "missing-tool"):
            self.assert_probe(self.reads("tests/commands.py"), path)

    def test_a_path_in_a_clone_of_the_checkout_maps_back_by_its_relative_path(self) -> None:
        reads = self.reads("tests/copy.py")
        self.assertIn("lib/copied.txt", reads["paths"])
        self.assertEqual("any", self.existence("tests/copy.py")["lib/absent-in-copy.txt"])
        self.assertFalse([path for path in reads["paths"] if path.endswith("note.txt")])

    def test_a_path_in_a_worktree_of_the_checkout_maps_back_by_its_relative_path(self) -> None:
        self.assertIn("lib/worktree.txt", self.reads("tests/worktree.py")["paths"])
        self.assertEqual("any", self.existence("tests/worktree.py")["lib/absent-in-worktree.txt"])

    def test_a_directory_filled_by_copying_files_and_a_fixture_repository_are_no_repository_copies(self) -> None:
        reads = self.reads("tests/no_copies.py")
        self.assertIn("lib/copied.txt", reads["paths"])  # the copier's own read in the checkout
        existence = self.existence("tests/no_copies.py")
        self.assertNotIn("lib/absent-in-file-copy.txt", existence)
        self.assertNotIn("data/absent-in-fixture.txt", existence)
        self.assertFalse([name for name in [*reads["paths"], *existence] if ":" in name], existence)

    def test_no_path_with_a_git_component_is_a_read(self) -> None:
        # tests/copy.py clones the checkout, so git reads the checkout's Git directory.
        names = []
        for item in self.fixture.full_result()["entries"]:
            reads = item["reads"]
            names += reads["paths"] + [read["path"] for read in reads["existence_reads"]] + reads["directory_memberships"]
        self.assertFalse([name for name in names if ".git" in name.lower().split("/")])

    def test_two_recordings_of_the_same_fixture_tree_give_identical_classified_lists(self) -> None:
        first = classified_lists(self.fixture)
        self.assertEqual(first, self.second())

    def test_a_git_process_that_reads_the_index_objects_or_refs_of_the_checkout_or_a_copy_reads_every_tracked_path(self) -> None:
        # git rev-parse, git ls-files -s and git cat-file of a revision expression in the checkout, git clone of the
        # checkout, and git worktree add, whose new worktree's Git directory lies in the checkout's.
        for entry in ("tests/git_revisions.py", "tests/copy.py", "tests/worktree.py"):
            self.assertIn("**", self.reads(entry)["paths"], entry)

    def test_a_git_process_in_a_fixture_repository_reads_no_tracked_path(self) -> None:
        self.assertNotIn("**", self.reads("tests/no_copies.py")["paths"])

    def test_git_ls_files_printing_only_names_is_a_directory_membership_of_every_path(self) -> None:
        reads = self.reads("tests/git_names.py")
        self.assertEqual(["**", "lib/**"], reads["directory_memberships"])
        self.assertEqual(["tests/git_names.py"], reads["paths"])

    def test_git_cat_file_of_full_object_ids_reads_the_tracked_paths_that_hold_each_object(self) -> None:
        # A blob that two paths hold, a tree with a ^{tree} suffix, an unheld blob id, and a Python read of .git/HEAD:
        # the held ids record their tracked paths, the unheld id adds nothing, and the non-git .git/HEAD read adds **.
        self.assertEqual(["**", "held/**", "held/also-one.txt", "held/one.txt", "tests/git_objects.py"], self.reads("tests/git_objects.py")["paths"])

    def test_git_cat_file_of_the_checked_commits_id_reads_every_tracked_path(self) -> None:
        self.assertEqual(["**", "tests/git_commit.py"], self.reads("tests/git_commit.py")["paths"])

    def test_git_cat_file_of_the_checked_commits_tree_id_reads_every_tracked_path(self) -> None:
        self.assertEqual(["**", "tests/git_root_tree.py"], self.reads("tests/git_root_tree.py")["paths"])

    def test_git_cat_file_of_names_that_are_not_only_full_object_ids_reads_every_tracked_path(self) -> None:
        # Names on standard input and a full id with the suffix ^{} still read **; a full id beside HEAD:<path>
        # attributes each operand to its paths.
        for entry in ("tests/git_batch.py", "tests/git_peeled.py"):
            self.assertEqual(["**", entry], self.reads(entry)["paths"], entry)
        self.assertEqual(["held/also-one.txt", "held/one.txt", "lib/helper.py", "tests/git_mixed.py"],
                         self.reads("tests/git_mixed.py")["paths"])

    def test_a_git_process_that_reads_the_configuration_reads_every_tracked_path_through_discovery(self) -> None:
        self.assertEqual(["**", "tests/git_config.py"], self.reads("tests/git_config.py")["paths"])

    def linked(self) -> RecordingFixture:
        """The linked-worktree fixture, holding a checked recording of LINKED_ENTRIES."""
        raise NotImplementedError

    def test_a_git_process_in_a_checkout_that_is_a_linked_worktree_reads_its_git_and_common_directory(self) -> None:
        fixture = self.linked()
        self.assertEqual(["**", "lib/helper.py", "tests/linked_revisions.py"], fixture.reads_of("tests/linked_revisions.py")["paths"])
        self.assertEqual(["**", "lib/**"], fixture.reads_of("tests/linked_names.py")["directory_memberships"])


@requires_posix_paths
class StoredStraceClassificationTests(ClassificationRules, unittest.TestCase):
    """The rules on two stored strace recordings of the fixture, which python3 -m tests.record_strace_fixture records."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = classification_fixture(cls.addClassCleanup, STRACE_FILES, STRACE_ENTRIES)
        copy_strace_recording(STRACE_RECORDINGS / "classification" / "first", cls.fixture)
        checked = cls.fixture.check()
        assert checked.returncode == 0, checked.stdout + checked.stderr

    def files(self) -> dict[str, str]:
        return STRACE_FILES

    def recorded(self, files: dict[str, str]) -> RecordingFixture:
        fixture = classification_fixture(self.addCleanup, files, STRACE_ENTRIES)
        copy_strace_recording(STRACE_RECORDINGS / "classification" / "first", fixture)
        return fixture

    def linked(self) -> RecordingFixture:
        fixture = linked_worktree_fixture(self.addCleanup)
        copy_strace_recording(STRACE_RECORDINGS / "linked", fixture)
        checked = fixture.check()
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        return fixture

    def second(self) -> dict:
        second_recording = classification_fixture(self.addCleanup, STRACE_FILES, STRACE_ENTRIES)
        copy_strace_recording(STRACE_RECORDINGS / "classification" / "second", second_recording)
        checked = second_recording.check()
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        return classified_lists(second_recording)

    def recording_directory(self) -> Path:
        return STRACE_RECORDINGS / "classification" / "first"

    def test_the_linux_extension_module_names_precede_a_found_source_module(self) -> None:
        extensions = [path for path in self.existence("tests/imports_only.py") if path.startswith("lib/helper.")]
        self.assertIn("lib/helper.so", extensions)
        self.assertIn("lib/helper.abi3.so", extensions)

    def test_a_command_search_through_the_working_directory_on_path_is_an_existence_read(self) -> None:
        # The entry file puts its working directory first on PATH: Python's search probes sh there, and sh probes git.
        for path in ("sh", "git"):
            self.assert_probe(self.reads("tests/commands.py"), path)

    def test_an_absent_path_that_a_process_tries_to_run_is_an_existence_read(self) -> None:
        self.assert_probe(self.reads("tests/commands.py"), "run.exe")

    def test_no_linux_read_drops_for_a_colon(self) -> None:
        self.assert_probe(self.reads("tests/commands.py"), "notes:draft.txt")

    def holds_absent(self, relative: str) -> bool:
        recording = load_recording()
        path = f"{self.fixture.root}/{relative}"
        return any(event.kind in ("absent", "exec-absent") and event.path == path for trace in (self.fixture.output / "traces").iterdir()
                   for event in recording.parse_trace(trace, (str(self.fixture.base),), str(self.fixture.root)))

    def holds_call(self, relative: str) -> bool:
        """Whether the stored recording holds a call on the checkout path ``relative``."""
        traces = STRACE_RECORDINGS / "classification" / "first" / "traces"
        return any(f'"${{BASE}}/repo/{relative}"' in path.read_text(encoding="utf-8") for path in traces.iterdir())

    def test_powershells_probe_of_a_script_name_in_its_working_directory_is_no_repository_path(self) -> None:
        # tests/search.ps1 runs as the entry file's own PowerShell process in the checkout root.
        self.assertTrue(self.holds_call("tool.ps1"))
        self.assertNotIn("tool.ps1", self.existence("tests/search.ps1"))

    def test_a_powershell_probe_outside_its_working_directory_is_an_existence_read(self) -> None:
        self.assertEqual("any", self.existence("tests/search.ps1")["data/helper.ps1"])

    def test_a_started_powershell_process_drops_its_probe_in_its_own_working_directory(self) -> None:
        # strace follows the working directory of every process: tests/commands.py starts PowerShell in data/.
        self.assertTrue(self.holds_call("data/child-tool.ps1"))
        self.assertNotIn("data/child-tool.ps1", self.existence("tests/commands.py"))

    def test_a_path_in_the_git_directory_of_a_nested_repository_is_no_repository_path(self) -> None:
        reads = self.reads("tests/nested.py")
        names = reads["paths"] + list(self.existence("tests/nested.py")) + reads["directory_memberships"]
        self.assertFalse([name for name in names if ".git" in name.split("/")], names)
        self.assertEqual("any", self.existence("tests/nested.py")["upstream/absent-in-nested.txt"])

    def test_a_failed_probe_of_a_revision_name_in_the_checkout_is_an_existence_read(self) -> None:
        self.assertEqual("any", self.existence("tests/revision.py")["HEAD"])

    def test_a_clone_with_relative_paths_in_a_known_working_directory_maps_back(self) -> None:
        self.assertIn("lib/relative.txt", self.reads("tests/copy.py")["paths"])

    def test_git_ls_files_with_pathspecs_records_what_they_can_list(self) -> None:
        reads = self.reads("tests/git_pathspecs.py")
        self.assertEqual(["da**a", "da**a/**", "held/*.txt", "held/*.txt/**", "lib/helper.py/**", "listed/**", "scripts/**.py", "scripts/**.py/**"], reads["directory_memberships"])
        self.assertEqual("any", self.existence("tests/git_pathspecs.py")["lib/helper.py"])
        self.assertEqual("any", self.existence("tests/git_pathspecs.py")["listed"])
        self.assertEqual(["tests/git_pathspecs.py"], reads["paths"])

    def test_git_ls_files_with_a_pathspec_variable_a_working_directory_below_the_root_or_other_magic_is_every_path(self) -> None:
        for entry in ("tests/git_pathspec_variable.py", "tests/git_pathspec_magic.py"):
            with self.subTest(entry=entry):
                self.assertEqual(["**"], self.reads(entry)["directory_memberships"])
        sub = self.reads("tests/git_pathspec_subdirectory.py")
        self.assertEqual(["lib/helper.py/**"], sub["directory_memberships"])
        self.assertEqual("any", self.existence("tests/git_pathspec_subdirectory.py")["lib/helper.py"])

    def test_select_runs_a_pathspec_listing_on_a_change_that_its_pathspecs_can_list_and_on_no_other(self) -> None:
        fixture = self.recorded(STRACE_FILES)
        self.assertEqual(0, fixture.check().returncode)
        applied = fixture.apply(fixture.output / "full-recording.json")
        self.assertEqual(0, applied.returncode, applied.stdout + applied.stderr)
        suites = suite_selection.load_suite_list(fixture.root / "suites.json")
        before = frozenset(filter(None, fixture.git("ls-files", "-z").split("\0")))
        changes = {("listed/two.txt",): True, ("lib/helper.py",): True, ("scripts/deep/tool.py",): True, ("dama",): True,
                   ("held/new.txt",): True, ("lib/other.py",): False, ("held/deep/new.txt",): False, ("docs/new.md",): False}
        for changed, runs in changes.items():
            after = before ^ set(changed)
            with self.subTest(changed=changed):
                (decision,) = suite_selection.select(suites, list(changed), previous_tracked=before, current_tracked=after).decisions
                self.assertEqual(runs, "tests/git_pathspecs.py" in decision.entries)


class RecordedEventFileClassificationTests(ClassificationRules, unittest.TestCase):
    """The same rules on the neutral event files of two ETW recordings of the Windows fixture on a hosted runner."""

    case_insensitive_ignore = True

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = classification_fixture(cls.addClassCleanup, WINDOWS_FILES)
        copy_recording(ETW_RECORDINGS / "classification" / "first", cls.fixture.output)
        checked = cls.fixture.check()
        assert checked.returncode == 0, checked.stdout + checked.stderr

    def files(self) -> dict[str, str]:
        return WINDOWS_FILES

    def recorded(self, files: dict[str, str]) -> RecordingFixture:
        fixture = classification_fixture(self.addCleanup, files)
        copy_recording(ETW_RECORDINGS / "classification" / "first", fixture.output)
        return fixture

    def recording_directory(self) -> Path:
        return ETW_RECORDINGS / "classification" / "first"

    def test_a_windows_listing_counts_once_per_open_of_the_directory(self) -> None:
        # The consumer writes the first enumeration of each open, so the import system's listing of lib/ and the code's
        # own listing in tests/lookups.py are two lines of the same process.
        record = next(json.loads(path.read_text(encoding="utf-8")) for path in (self.recording_directory() / "units").iterdir()
                      if json.loads(path.read_text(encoding="utf-8"))["name"] == "tests/lookups.py")
        events = (self.recording_directory() / "events.tsv").read_text(encoding="utf-8").splitlines()
        listings = [line for line in events if line.split("\t")[2:3] == ["enum"] and line.lower().endswith("\\repo\\lib\\*")]
        self.assertGreaterEqual(len({line.split("\t")[0] for line in listings}), 1)
        reports = list((self.recording_directory() / "lookups" / record["lookups"]).glob("*.tsv"))
        pid = next(report.stem for report in reports if "\tabsent_module\t" in report.read_text(encoding="utf-8"))
        self.assertEqual(2, len([line for line in listings if line.split("\t")[0] == pid]), listings)

    def linked(self) -> RecordingFixture:
        fixture = linked_worktree_fixture(self.addCleanup)
        copy_recording(ETW_RECORDINGS / "linked", fixture.output)
        checked = fixture.check()
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        return fixture

    def test_an_untracked_ignored_directory_hit_by_stat_matches_a_directory_only_gitignore_rule(self) -> None:
        # Without the opens of data\, the recording hits data only through a literal-name enumeration (stat); because
        # data is an untracked directory in the checkout, data/ in .gitignore matches and drops it.
        files = {path: text for path, text in WINDOWS_FILES.items() if path != "data/stat.txt"}
        fixture = self.recorded({**files, ".gitignore": DATA[".gitignore"] + "data/\n"})
        (fixture.root / "data").mkdir()
        events = fixture.output / "events.tsv"
        events.write_bytes(b"".join(line for line in events.read_bytes().splitlines(keepends=True) if b"\\repo\\data\\" not in line))
        checked = fixture.check()
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        existence = {read["path"]: read["kind"] for read in fixture.reads_of("tests/stats.py")["existence_reads"]}
        self.assertNotIn("data", existence)

    def second(self) -> dict:
        other = classification_fixture(self.addCleanup, WINDOWS_FILES)
        copy_recording(ETW_RECORDINGS / "classification" / "second", other.output)
        checked = other.check()
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        return classified_lists(other)

    def holds_absent(self, relative: str) -> bool:
        events = (ETW_RECORDINGS / "classification" / "first" / "events.tsv").read_text(encoding="utf-8").lower().splitlines()
        name = "\\repo\\" + relative.replace("/", "\\").lower()
        return any(line.split("\t")[2:3] == ["absent"] and line.endswith(name) for line in events)

    def holds_call(self, name: str) -> bool:
        """Whether the recorded event file holds a line on the checkout path ``name``, in its Windows spelling."""
        events = (ETW_RECORDINGS / "classification" / "first" / "events.tsv").read_text(encoding="utf-8").lower()
        return f"\\repo\\{name}\t".lower() in events.replace("\n", "\t\n")

    def test_a_path_ending_in_the_main_data_stream_is_a_read_of_the_file_itself(self) -> None:
        self.assertTrue(self.holds_call("listed\\one.txt::$DATA"))
        self.assertTrue(self.holds_call("missing.txt::$DATA"))
        self.assertIn("listed/one.txt", self.reads("tests/commands.py")["paths"])
        self.assertEqual("any", self.existence("tests/commands.py")["missing.txt"])

    def test_a_path_that_names_another_stream_is_no_repository_path(self) -> None:
        # tests/commands.py probes two named streams, and PowerShell probes its script's Zone.Identifier.
        for name in ("data\\stat.txt:Zone.Identifier", "listed\\one.txt:extra:$DATA", "tests\\search.ps1:Zone.Identifier"):
            self.assertTrue(self.holds_call(name), name)
        for entry in ("tests/commands.py", "tests/search.ps1"):
            reads = self.reads(entry)
            names = reads["paths"] + list(self.existence(entry)) + reads["directory_memberships"]
            self.assertFalse([name for name in names if ":" in name], names)
        self.assertNotIn("data/stat.txt", self.reads("tests/commands.py")["paths"])

    def test_powershells_absent_script_in_its_working_directory_is_no_repository_path_and_a_started_ones_stays(self) -> None:
        # Depending on the runner's PowerShell, it checks an absent script through an attribute query, which the
        # Kernel-File session does not see, or through an open of it. An open by the entry file's own process in its
        # working directory, the checkout root, is command search; the PowerShell process that tests/commands.py starts
        # in data/ has no working directory that the recording knows, so its open stays an existence read.
        self.assertNotIn("tool.ps1", self.existence("tests/search.ps1"))
        self.assertEqual("any" if self.holds_call("data\\child-tool.ps1") else None, self.existence("tests/commands.py").get("data/child-tool.ps1"))


# A pytest-shaped run of tests/test_a.py beside independent test files, as the classification sees it: the collecting
# process lists tests/, stats each sibling twice, probes for a virtual environment below it and opens only its entry file.
SIBLING_FILES = {"tests/test_a.py": "", "tests/test_b.py": "", "tests/test_c.py": "", "lib/helper.py": "VALUE = 1\n", "data/keep.txt": ""}
SIBLINGS = ("tests/test_b.py", "tests/test_c.py")
PYTHON, OTHER = "10", "20"


def sibling_events(root: str, pid: str = PYTHON, executable: str = "python3") -> list:
    """The events of the pytest-shaped run, made by the process ``pid``."""
    recording = load_recording()
    events = [recording.Event(pid, executable, "list", root + "/tests"), recording.Event(pid, executable, "read", root + "/tests/test_a.py")]
    for sibling in SIBLINGS:
        events += [recording.Event(pid, executable, "stat", f"{root}/{sibling}")] * 2
        events += [recording.Event(pid, executable, "absent", f"{root}/{sibling}/{name}") for name in ("pyvenv.cfg", "conda-meta/history")]
    return events


class StatOnlyClassificationTests(unittest.TestCase):
    """The stat-only rule and the existence reads that a unit's directory memberships cover, on written events and lookup
    reports of a fixture checkout."""

    ROOT = "/checkout"

    @classmethod
    def setUpClass(cls) -> None:
        cls.recording = load_recording()
        fixture = RecordingFixture(cls.addClassCleanup, SIBLING_FILES, [suite("a", ["tests/test_a.py"], paths=[])], stand_in=False)
        cls.project = cls.recording.checkout(fixture.root)

    def reads(self, events: list, *, reported: tuple[str, ...] = (PYTHON,), found: tuple[str, ...] = ()) -> dict:
        """The stored form of the reads of ``events``, whose processes ``reported`` have a lookup report naming each
        checkout file of ``found`` as a found module."""
        recording = self.recording
        lookups = recording.Lookups(frozenset(reported), {}, tuple(
            recording.FoundModule(posixpath.dirname(f"{self.ROOT}/{file}"), posixpath.basename(file).split(".")[0], f"{self.ROOT}/{file}", (".py",))
            for file in found))
        unit = recording.UnitRecording(events, None, self.project, self.ROOT, lookups)
        return recording.unit_reads(unit, recording.Start("python3", self.ROOT, ())).document()

    def event(self, kind: str, relative: str, pid: str = PYTHON, executable: str = "python3"):
        return self.recording.Event(pid, executable, kind, f"{self.ROOT}/{relative}".rstrip("/"))

    def test_a_pytest_shaped_run_reads_its_own_entry_file_and_no_sibling(self) -> None:
        reads = self.reads(sibling_events(self.ROOT))
        self.assertEqual(["tests/test_a.py"], reads["paths"])
        self.assertEqual([], reads["existence_reads"])
        self.assertEqual(["tests"], reads["directory_memberships"])

    def test_without_the_membership_of_their_directory_the_stats_and_the_probes_below_a_tracked_file_stay(self) -> None:
        events = [event for event in sibling_events(self.ROOT) if event.kind != "list"]
        reads = self.reads(events)
        self.assertEqual(["tests/test_a.py"], reads["paths"])
        expected = [*SIBLINGS, *(f"{sibling}/{name}" for sibling in SIBLINGS for name in ("conda-meta/history", "pyvenv.cfg"))]
        self.assertEqual(sorted(expected), [read["path"] for read in reads["existence_reads"]])
        self.assertEqual({"any"}, {read["kind"] for read in reads["existence_reads"]})

    def test_a_python_process_that_opens_a_file_it_stats_reads_its_content(self) -> None:
        reads = self.reads([self.event("stat", "lib/helper.py"), self.event("read", "lib/helper.py")])
        self.assertEqual(["lib/helper.py"], reads["paths"])
        self.assertEqual([], reads["existence_reads"])

    def test_a_module_loaded_from_cached_bytecode_that_the_report_names_as_found_is_a_content_read(self) -> None:
        # The path finder stats the source to check the cached bytecode and never opens it.
        reads = self.reads([self.event("stat", "lib/helper.py")], found=("lib/helper.py",))
        self.assertEqual(["lib/helper.py"], reads["paths"])
        self.assertNotIn("lib/helper.py", [read["path"] for read in reads["existence_reads"]])

    def test_a_stat_of_a_process_without_a_lookup_report_is_a_content_read(self) -> None:
        self.assertEqual(["lib/helper.py"], self.reads([self.event("stat", "lib/helper.py", OTHER, "dotnet")])["paths"])
        self.assertEqual(["tests/test_a.py", *SIBLINGS], self.reads(sibling_events(self.ROOT, OTHER, "dotnet"))["paths"])

    def test_a_stat_under_a_process_id_that_runs_more_than_one_program_is_a_content_read(self) -> None:
        # A shell that runs Python in its own process after a stat, a Python process that runs dotnet in its place, and,
        # on Windows, a process id that a later process reuses: the lookup report cannot tell which program made the stat.
        start = self.recording.Event
        for programs in (("sh", "python3"), ("python3", "dotnet")):
            with self.subTest(programs=programs):
                before = [start(PYTHON, programs[0], "exec", f"/usr/bin/{programs[0]}"), self.event("stat", "lib/helper.py", executable=programs[0])]
                after = [start(PYTHON, programs[1], "exec", f"/usr/bin/{programs[1]}"), self.event("stat", "data/keep.txt", executable=programs[1])]
                self.assertEqual(["data/keep.txt", "lib/helper.py"], self.reads(before + after)["paths"])

    def test_a_stat_after_the_one_start_of_a_python_process_with_a_report_is_an_existence_read(self) -> None:
        reads = self.reads([self.recording.Event(PYTHON, "python3", "exec", "/usr/bin/python3"), self.event("stat", "lib/helper.py")])
        self.assertEqual(([], [{"path": "lib/helper.py", "kind": "any"}]), (reads["paths"], reads["existence_reads"]))

    def test_a_python_process_s_stat_of_an_absent_path_and_of_a_directory_stay_existence_reads(self) -> None:
        reads = self.reads([self.event("absent", "lib/missing.py"), self.event("statdir", "data")])
        self.assertEqual([{"path": "data", "kind": "directory"}, {"path": "lib/missing.py", "kind": "any"}], reads["existence_reads"])
        self.assertEqual([], reads["paths"])

    def test_a_held_membership_covers_no_existence_read_of_a_directory_and_none_below_an_untracked_name(self) -> None:
        reads = self.reads([self.event("list", ""), self.event("statdir", "data"), self.event("absent", "absent.txt/pyvenv.cfg"),
                            self.event("absent", "lib/missing.py")])
        self.assertEqual([{"path": "absent.txt/pyvenv.cfg", "kind": "any"}, {"path": "data", "kind": "directory"},
                          {"path": "lib/missing.py", "kind": "any"}], reads["existence_reads"])
        self.assertEqual(["."], reads["directory_memberships"])

    def test_a_worker_threads_import_listing_is_credited_to_that_thread_and_hides_no_main_thread_listing(self) -> None:
        # Process 1000's worker thread 1002 imports from lib/; the main thread 1000 lists lib/ itself.
        recording = self.recording
        directory = Path(self.enterContext(temporary_support.Directory()))
        (directory / "1000.tsv").write_text(f"suffixes\t.py\nlist\t1002\t{self.ROOT}/lib\n", encoding="utf-8")
        lookups = recording.read_lookups(directory, recording.normalize)
        events = [self.event("list", "lib", pid="1000"), self.event("list", "lib", pid="1002")]
        unit = recording.UnitRecording(events, None, self.project, self.ROOT, lookups)
        reads = recording.unit_reads(unit, recording.Start("python3", self.ROOT, ())).document()
        self.assertEqual(["lib"], reads["directory_memberships"])
        # When only the worker thread lists lib/ for its import, no directory membership is recorded.
        worker_only = recording.UnitRecording([self.event("list", "lib", pid="1002")], None, self.project, self.ROOT, lookups)
        self.assertEqual([], recording.unit_reads(worker_only, recording.Start("python3", self.ROOT, ())).document()["directory_memberships"])

    def test_an_open_through_a_symlink_records_both_the_link_and_its_target(self) -> None:
        recording = self.recording
        trace = Path(self.enterContext(temporary_support.Directory())) / "link.strace"
        trace.write_text(
            f'100 openat(AT_FDCWD<{self.ROOT}>, "link.txt", O_RDONLY|O_CLOEXEC) = 3<{self.ROOT}/data/keep.txt>\n'
            f'100 openat(AT_FDCWD<{self.ROOT}>, "data/keep.txt", O_RDONLY|O_CLOEXEC) = 4<{self.ROOT}/data/keep.txt>\n'
            f'100 openat(AT_FDCWD<{self.ROOT}>, "pipe_read", O_RDONLY) = 5<pipe:[12345]>\n',
            encoding="utf-8",
        )
        events = recording.parse_trace(trace, (self.ROOT,), self.ROOT)
        reads = [(event.kind, event.path) for event in events]
        self.assertEqual([
            ("read", f"{self.ROOT}/link.txt"),
            ("read", f"{self.ROOT}/data/keep.txt"),
            ("read", f"{self.ROOT}/data/keep.txt"),
            ("read", f"{self.ROOT}/pipe_read"),
        ], reads)

    def test_write_opens_are_recorded_and_rdwr_without_trunc_also_records_a_read(self) -> None:
        recording = self.recording
        trace = Path(self.enterContext(temporary_support.Directory())) / "writes.strace"
        trace.write_text(
            f'100 openat(AT_FDCWD<{self.ROOT}>, "out.txt", O_WRONLY|O_CREAT|O_TRUNC, 0666) = 3<{self.ROOT}/out.txt>\n'
            f'100 openat(AT_FDCWD<{self.ROOT}>, "data/keep.txt", O_RDWR|O_CREAT, 0666) = 4<{self.ROOT}/data/keep.txt>\n'
            f'100 openat(AT_FDCWD<{self.ROOT}>, "trunc.txt", O_RDWR|O_TRUNC) = 5<{self.ROOT}/trunc.txt>\n',
            encoding="utf-8",
        )
        events = recording.parse_trace(trace, (self.ROOT,), self.ROOT)
        self.assertEqual([
            ("write", f"{self.ROOT}/out.txt"),
            ("write", f"{self.ROOT}/data/keep.txt"),
            ("read", f"{self.ROOT}/data/keep.txt"),
            ("write", f"{self.ROOT}/trunc.txt"),
        ], [(event.kind, event.path) for event in events])

    def test_a_git_revision_and_path_argument_adds_no_existence_read_and_still_reads_every_tracked_path(self) -> None:
        recording = self.recording
        project = replace_git_dirs(self.project, (f"{self.ROOT}/.git",))
        rev_arg = "0123456789abcdef:docs/guides/runs/run-outcomes.md"
        events = [
            recording.Event(OTHER, "git", "exec", "/usr/bin/git", self.ROOT, ("git", "show", rev_arg)),
            recording.Event(OTHER, "git", "absent", f"{self.ROOT}/{rev_arg}", self.ROOT),
            recording.Event(OTHER, "git", "read", f"{self.ROOT}/.git/HEAD", self.ROOT),
            # A bare revision without a colon still stays an existence read, and a non-git probe with a colon stays too.
            recording.Event("21", "git", "exec", "/usr/bin/git", self.ROOT, ("git", "rev-parse", "HEAD")),
            recording.Event("21", "git", "absent", f"{self.ROOT}/HEAD", self.ROOT),
            recording.Event("21", "git", "read", f"{self.ROOT}/.git/HEAD", self.ROOT),
            recording.Event(PYTHON, "python3", "absent", f"{self.ROOT}/notes:draft.txt", self.ROOT),
        ]
        unit = recording.UnitRecording(events, None, project, self.ROOT)
        reads = recording.unit_reads(unit, recording.Start("python3", self.ROOT, ())).document()
        self.assertIn("**", reads["paths"])
        existence = {read["path"]: read["kind"] for read in reads["existence_reads"]}
        self.assertNotIn(rev_arg, existence)
        self.assertEqual("any", existence.get("HEAD"))
        self.assertEqual("any", existence.get("notes:draft.txt"))

    def test_derived_existence_reads_respect_gitignore_and_skip_dotnet_option_values(self) -> None:
        recording = self.recording
        fixture = RecordingFixture(self.addCleanup, {
            **SIBLING_FILES,
            ".gitignore": "*.so\n*.pyd\nNuGet.Config\n",
            "app/App.csproj": "<Project />\n",
            "out/keep.txt": "",
        }, [suite("a", ["tests/test_a.py"], paths=[])], stand_in=False)
        project = recording.checkout(fixture.root)
        # preceding_modules generates lib/helper.so and lib/helper.pyd; .gitignore drops both while keeping lib/helper.
        lookups = recording.Lookups(frozenset({PYTHON}), {}, (
            recording.FoundModule(f"{self.ROOT}/lib", "helper", f"{self.ROOT}/lib/helper.py", (".so", ".pyd", ".py")),
        ))
        events = [
            recording.Event(OTHER, "dotnet", "exec", "/usr/bin/dotnet", self.ROOT,
                            ("dotnet", "test", "--results-directory", "out", "--project", "app/App.csproj")),
            recording.Event(OTHER, "dotnet", "read", f"{self.ROOT}/app/App.csproj", self.ROOT),
        ]
        unit = recording.UnitRecording(events, None, project, self.ROOT, lookups)
        reads = recording.unit_reads(unit, recording.Start("python3", self.ROOT, ())).document()
        existence = {read["path"]: read["kind"] for read in reads["existence_reads"]}
        self.assertEqual("directory", existence.get("lib/helper"))
        self.assertNotIn("lib/helper.so", existence)
        self.assertNotIn("lib/helper.pyd", existence)
        self.assertNotIn("NuGet.Config", existence)
        self.assertNotIn("app/NuGet.Config", existence)
        self.assertIn("app/Directory.Build.props", existence)
        # out was the value of --results-directory, not an argument anchor.
        self.assertNotIn("out/Directory.Build.props", existence)
        self.assertNotIn("out/Directory.Solution.props", existence)

    def test_untracked_ignored_directory_and_windows_case_folding_in_gitignore(self) -> None:
        recording = self.recording
        fixture = RecordingFixture(self.addCleanup, {
            **SIBLING_FILES,
            ".gitignore": "ignored_dir/\nMixedCase.txt\n",
        }, [suite("a", ["tests/test_a.py"], paths=[])], stand_in=False)
        (fixture.root / "ignored_dir").mkdir()
        project = recording.checkout(fixture.root)
        events = [
            recording.Event(OTHER, "tool", "stat", f"{self.ROOT}/ignored_dir", self.ROOT),
            recording.Event(OTHER, "tool", "absent", f"{self.ROOT}/mixedcase.txt", self.ROOT),
        ]
        unit = recording.UnitRecording(events, None, project, self.ROOT)
        linux_existence = {read["path"]: read["kind"] for read in recording.unit_reads(unit, recording.Start("tool", self.ROOT, ()), fold=False).document()["existence_reads"]}
        windows_existence = {read["path"]: read["kind"] for read in recording.unit_reads(unit, recording.Start("tool", self.ROOT, ()), fold=True).document()["existence_reads"]}
        self.assertNotIn("ignored_dir", linux_existence)
        self.assertNotIn("ignored_dir", windows_existence)
        self.assertEqual("any", linux_existence.get("mixedcase.txt"))
        self.assertNotIn("mixedcase.txt", windows_existence)

    def test_non_git_readers_of_git_state_and_unheld_cat_file_ids_fall_back_to_every_path(self) -> None:
        recording = self.recording
        fixture = RecordingFixture(self.addCleanup, {**SIBLING_FILES, **HELD_FILES}, [suite("a", ["tests/test_a.py"], paths=[])], stand_in=False)
        project = replace_git_dirs(recording.checkout(fixture.root), (f"{self.ROOT}/.git",))
        # Non-git process reading .git/HEAD falls back to **; reading only .git/config does not.
        head_events = [recording.Event(PYTHON, "python3", "read", f"{self.ROOT}/.git/HEAD", self.ROOT)]
        config_events = [recording.Event(PYTHON, "python3", "read", f"{self.ROOT}/.git/config", self.ROOT)]
        self.assertIn("**", recording.unit_reads(recording.UnitRecording(head_events, None, project, self.ROOT), recording.Start("python3", self.ROOT, ())).paths)
        self.assertNotIn("**", recording.unit_reads(recording.UnitRecording(config_events, None, project, self.ROOT), recording.Start("python3", self.ROOT, ())).paths)
        # git cat-file of a held object id stays narrow; an unheld id falls back to **.
        held_only = [
            recording.Event(OTHER, "git", "exec", "/usr/bin/git", self.ROOT, ("git", "cat-file", "blob", ONE_BLOB_ID)),
            recording.Event(OTHER, "git", "read", f"{self.ROOT}/.git/objects/{ONE_BLOB_ID[:2]}/{ONE_BLOB_ID[2:]}", self.ROOT),
        ]
        unheld = [
            recording.Event(OTHER, "git", "exec", "/usr/bin/git", self.ROOT, ("git", "cat-file", "-e", UNHELD_BLOB_ID)),
            recording.Event(OTHER, "git", "read", f"{self.ROOT}/.git/objects/pack/pack.idx", self.ROOT),
        ]
        held_reads = recording.unit_reads(recording.UnitRecording(held_only, None, project, self.ROOT), recording.Start("python3", self.ROOT, ())).paths
        unheld_reads = recording.unit_reads(recording.UnitRecording(unheld, None, project, self.ROOT), recording.Start("python3", self.ROOT, ())).paths
        self.assertEqual({"held/also-one.txt", "held/one.txt"}, held_reads)
        self.assertEqual(set(), unheld_reads)

    def test_stored_covers_existence_soundness_and_linear_scaling(self) -> None:
        recording = self.recording
        # Parent membership covers kind="any", not kind="directory"; src/** covers src for kind="directory" and src/a for kind="any", not src for kind="any".
        with_root_membership = recording.Stored(patterns=(), memberships=(".",), existence=(), tracked=frozenset({"data/keep.txt"}))
        self.assertTrue(with_root_membership.covers_existence("top.txt", "any"))
        self.assertFalse(with_root_membership.covers_existence("data", "directory"))
        with_tree_pattern = recording.Stored(patterns=("src/**",), memberships=(), existence=(), tracked=frozenset({"data/keep.txt"}))
        self.assertTrue(with_tree_pattern.covers_existence("src", "directory"))
        self.assertTrue(with_tree_pattern.covers_existence("src/a", "any"))
        self.assertFalse(with_tree_pattern.covers_existence("src", "any"))
        # Large lists deduplicate and cover linearly without quadratic blowup.
        many = [f"pkg/file_{index}.py" for index in range(3000)]
        joined = recording.add_unique(many, many + ["pkg/new.py"])
        self.assertEqual(3001, len(joined))
        large_stored = recording.Stored(patterns=tuple(many), memberships=(), existence=(), tracked=frozenset(many))
        self.assertTrue(all(large_stored.covers_path(path) for path in many))
        self.assertTrue(large_stored.covers_existence("pkg", "directory"))
        self.assertFalse(large_stored.covers_existence("other", "directory"))


class GitAttributionTests(unittest.TestCase):
    """Attribution of Git reads to tracked paths or none across commands, global options, replace refs, copies and
    non-Git readers of Git state."""

    ROOT = "/checkout"

    @classmethod
    def setUpClass(cls) -> None:
        cls.recording = load_recording()
        files = {
            **SIBLING_FILES,
            **HELD_FILES,
            "src/app.py": "print('app')\n",
            "src/sub/mod.py": "VALUE = 2\n",
            "docs/guide.md": "# Guide\n",
        }
        cls.fixture = RecordingFixture(cls.addClassCleanup, files, [suite("a", ["tests/test_a.py"], paths=[])], stand_in=False)
        cls.head_commit = cls.fixture.git("rev-parse", "HEAD").strip()
        cls.head_tree = cls.fixture.git("rev-parse", "HEAD^{tree}").strip()
        cls.project = replace_git_dirs(cls.recording.checkout(cls.fixture.root), (f"{cls.ROOT}/.git",))
        cls.unheld_commit = "a" * 40
        cls.other_commit = "b" * 40

    def git_reads(self, argv: tuple[str, ...], *, cwd: str | None = None, environment: tuple[str, ...] | None = (),
                  fold: bool = False, project=None, copies: list[str] = (), git_state_path: str | None = None):
        recording = self.recording
        target_project = project or self.project
        effective_cwd = self.ROOT if cwd is None else cwd
        state_file = git_state_path or f"{self.ROOT}/.git/HEAD"
        events = [
            recording.Event(OTHER, "git", "exec", "/usr/bin/git", effective_cwd, argv, environment),
            recording.Event(OTHER, "git", "read", state_file, effective_cwd),
        ]
        return recording.git_directory_reads(events, list(copies), target_project, self.ROOT, fold=fold)

    def test_cat_file_of_unheld_full_object_id_adds_no_tracked_path(self) -> None:
        for argv in (
            ("git", "cat-file", "-p", UNHELD_BLOB_ID),
            ("git", "cat-file", "-e", UNHELD_BLOB_ID),
            ("git", "cat-file", "-t", UNHELD_BLOB_ID),
            ("git", "cat-file", "-s", UNHELD_BLOB_ID),
            ("git", "cat-file", "blob", UNHELD_BLOB_ID),
        ):
            with self.subTest(argv=argv):
                reads = self.git_reads(argv)
                self.assertEqual((set(), set(), {}), (reads.paths, reads.memberships, reads.existence))

    def test_cat_file_of_held_blob_and_tree_adds_their_tracked_paths(self) -> None:
        self.assertEqual({"held/also-one.txt", "held/one.txt"}, self.git_reads(("git", "cat-file", "-p", ONE_BLOB_ID)).paths)
        self.assertEqual({"held/**"}, self.git_reads(("git", "cat-file", "-p", HELD_TREE_ID)).paths)
        self.assertEqual({"**"}, self.git_reads(("git", "cat-file", "-p", self.head_commit)).paths)
        self.assertEqual({"**"}, self.git_reads(("git", "cat-file", "-p", self.head_tree)).paths)

    def test_cat_file_and_show_of_unheld_full_commit_path_add_no_tracked_path(self) -> None:
        for argv in (
            ("git", "cat-file", "-p", f"{self.unheld_commit}:src/app.py"),
            ("git", "show", f"{self.unheld_commit}:src/app.py"),
        ):
            with self.subTest(argv=argv):
                reads = self.git_reads(argv)
                self.assertEqual((set(), set(), {}), (reads.paths, reads.memberships, reads.existence))

    def test_show_and_cat_file_of_head_and_head_commit_path_add_file_or_directory_glob(self) -> None:
        for argv in (
            ("git", "show", "HEAD:src/app.py"),
            ("git", "cat-file", "-p", "HEAD:src/app.py"),
            ("git", "show", f"{self.head_commit}:src/app.py"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual({"src/app.py"}, self.git_reads(argv).paths)
        for argv in (
            ("git", "show", "HEAD:src"),
            ("git", "cat-file", "-p", "HEAD:src"),
            ("git", "show", f"{self.head_commit}:src/sub"),
        ):
            expected = {"src/sub/**"} if "src/sub" in argv[-1] else {"src/**"}
            with self.subTest(argv=argv):
                self.assertEqual(expected, self.git_reads(argv).paths)

    def test_ls_tree_with_prefix_adds_prefix_and_subtree_only(self) -> None:
        reads = self.git_reads(("git", "ls-tree", "HEAD", "--", "src/sub"))
        self.assertEqual({"src/sub", "src/sub/**"}, reads.paths)
        self.assertEqual(set(), reads.memberships)

    def test_archive_with_paths_adds_paths_and_ancestor_gitattributes_only(self) -> None:
        reads = self.git_reads(("git", "archive", "HEAD", "--", "src/sub/mod.py", "docs"))
        self.assertEqual({"src/sub/mod.py", "src/sub/.gitattributes", "src/.gitattributes", ".gitattributes",
                          "docs/**", "docs/.gitattributes"}, reads.paths)
        self.assertNotIn("**", reads.paths)

    def test_rev_parse_location_and_merge_base_of_full_ids_add_no_tracked_path(self) -> None:
        for argv in (
            ("git", "rev-parse", "--show-toplevel"),
            ("git", "rev-parse", "--git-dir"),
            ("git", "rev-parse", "--show-prefix"),
            ("git", "rev-parse", "--is-inside-work-tree"),
            ("git", "merge-base", self.unheld_commit, self.other_commit),
        ):
            with self.subTest(argv=argv):
                reads = self.git_reads(argv)
                self.assertEqual((set(), set(), {}), (reads.paths, reads.memberships, reads.existence))

    def test_ref_dependent_and_whole_tree_commands_add_every_path(self) -> None:
        for argv in (
            ("git", "rev-parse", "HEAD"),
            ("git", "rev-parse", "main"),
            ("git", "describe"),
            ("git", "for-each-ref"),
            ("git", "merge-base", "HEAD", "main"),
            ("git", "show", "HEAD"),
            ("git", "log", "-p"),
            ("git", "ls-tree", "-r", "HEAD"),
            ("git", "archive", "HEAD"),
            ("git", "diff", "HEAD~1", "HEAD"),
            ("git", "status"),
            ("git", "diff"),
            ("git", "add", "-A"),
            ("git", "commit", "-m", "msg"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual({"**"}, self.git_reads(argv).paths)

    def test_diff_and_add_with_pathspec_add_path_and_no_every_path(self) -> None:
        self.assertEqual({"src/app.py"}, self.git_reads(("git", "diff", "--", "src/app.py")).paths)
        self.assertEqual({"src/app.py"}, self.git_reads(("git", "add", "--", "src/app.py")).paths)
        self.assertEqual({"src/**"}, self.git_reads(("git", "diff", "--", "src")).paths)
        self.assertEqual({"src/app.py"}, self.git_reads(("git", "restore", "--", "src/app.py")).paths)
        self.assertEqual({"src/app.py"}, self.git_reads(("git", "checkout", "HEAD", "--", "src/app.py")).paths)
        self.assertEqual({"src/app.py"}, self.git_reads(("git", "grep", "-n", "app", "--", "src/app.py")).paths)
        # Unlisted or unsafe options on diff/grep fall back to **.
        for argv in (
            ("git", "diff", "--textconv", "--", "src/app.py"),
            ("git", "diff", "--recurse-submodules", "--", "src/app.py"),
            ("git", "diff", "--unknown-flag", "--", "src/app.py"),
            ("git", "grep", "--untracked", "app", "--", "src/app.py"),
            ("git", "grep", "--recurse-submodules", "app", "--", "src/app.py"),
            ("git", "grep", "--unknown-flag", "app", "--", "src/app.py"),
        ):
            with self.subTest(argv=argv):
                self.assertEqual({"**"}, self.git_reads(argv).paths)

    def test_index_lookup_adds_ancestor_gitattributes_and_absent_rev_path_covers_dir_and_file(self) -> None:
        self.assertEqual({"src/sub/mod.py", "src/sub/.gitattributes", "src/.gitattributes", ".gitattributes"},
                         self.git_reads(("git", "show", ":src/sub/mod.py")).paths)
        self.assertEqual({"old_dir", "old_dir/**"},
                         self.git_reads(("git", "show", "HEAD~1:old_dir")).paths)

    def test_windows_attached_drive_options_resolve_paths(self) -> None:
        converted = self.recording.windows_arguments(
            ["git", "-CD:\\checkout\\src", "--work-tree=D:\\checkout", "diff", "--", "sub/mod.py"]
        )
        self.assertEqual(["git", "-C/D:/checkout/src", "--work-tree=/D:/checkout", "diff", "--", "sub/mod.py"], converted)
        win_project = replace_git_dirs(self.project, ("/D:/checkout/.git",))
        events = [
            self.recording.Event(OTHER, "git", "exec", "/D:/Git/git.exe", None, tuple(converted), None),
            self.recording.Event(OTHER, "git", "read", "/D:/checkout/.git/HEAD", None),
        ]
        self.assertEqual({"src/sub/mod.py"},
                         self.recording.git_directory_reads(events, [], win_project, "/D:/checkout", fold=True).paths)

    def test_global_options_resolve_paths_and_unlisted_config_keeps_every_path(self) -> None:
        self.assertEqual({"src/sub/mod.py"},
                         self.git_reads(("git", "-C", f"{self.ROOT}/src", "diff", "--", "sub/mod.py")).paths)
        self.assertEqual({"src/app.py"},
                         self.git_reads(("git", "--no-pager", "show", "HEAD:src/app.py")).paths)
        self.assertEqual({"src/app.py"},
                         self.git_reads(("git", "-c", "core.quotepath=false", "show", "HEAD:src/app.py")).paths)
        self.assertEqual({"**"},
                         self.git_reads(("git", "-c", "core.attributesFile=/tmp/attrs", "show", "HEAD:src/app.py")).paths)

    def test_windows_unknown_environment_attributes_pathspecs_and_keeps_icase_at_every_path(self) -> None:
        reads = self.git_reads(("git", "diff", "--", "src/*.py"), environment=None, fold=True)
        self.assertEqual({"src/**.py", "src/**.py/**"}, reads.paths)
        self.assertEqual({"**"}, self.git_reads(("git", "--icase-pathspecs", "diff", "--", "src/app.py"), environment=None, fold=True).paths)
        self.assertEqual({"**"}, self.git_reads(("git", "diff", "--", ":(icase)src/app.py"), environment=None, fold=True).paths)

    def test_replace_refs_loose_and_packed_prevent_empty_attribution_unless_switched_off(self) -> None:
        recording = self.recording
        for storage in ("loose", "packed"):
            with self.subTest(storage=storage):
                fixture = RecordingFixture(self.addCleanup, {**SIBLING_FILES, **HELD_FILES}, [suite("a", ["tests/test_a.py"], paths=[])], stand_in=False)
                project = replace_git_dirs(recording.checkout(fixture.root), (f"{self.ROOT}/.git",))
                if storage == "loose":
                    replace_dir = fixture.root / ".git" / "refs" / "replace"
                    replace_dir.mkdir(parents=True, exist_ok=True)
                    (replace_dir / UNHELD_BLOB_ID).write_text(ONE_BLOB_ID + "\n", encoding="utf-8")
                    (replace_dir / self.unheld_commit).write_text(self.other_commit + "\n", encoding="utf-8")
                else:
                    packed = fixture.root / ".git" / "packed-refs"
                    packed.write_text(f"{ONE_BLOB_ID} refs/replace/{UNHELD_BLOB_ID}\n"
                                      f"{self.other_commit} refs/replace/{self.unheld_commit}\n", encoding="utf-8")
                # Replaced by held blob -> attributes to held blob's paths.
                self.assertEqual({"held/also-one.txt", "held/one.txt"},
                                 self.git_reads(("git", "cat-file", "-p", UNHELD_BLOB_ID), project=project).paths)
                # Replaced by unheld object -> falls back to **, never empty.
                self.assertEqual({"**"}, self.git_reads(("git", "cat-file", "-p", self.unheld_commit), project=project).paths)
                # Switching replace refs off restores unheld empty attribution.
                self.assertEqual(set(), self.git_reads(("git", "--no-replace-objects", "cat-file", "-p", UNHELD_BLOB_ID), project=project).paths)
                self.assertEqual(set(), self.git_reads(("git", "-c", "core.useReplaceRefs=false", "cat-file", "-p", UNHELD_BLOB_ID), project=project).paths)
                self.assertEqual(set(), self.git_reads(("git", "cat-file", "-p", UNHELD_BLOB_ID),
                                                       environment=("GIT_NO_REPLACE_OBJECTS=1",), project=project).paths)

    def test_reads_in_repository_copy_follow_same_rules_and_copy_only_object_adds_nothing(self) -> None:
        copy_root = "/tmp/work/copy"
        self.assertEqual({"src/app.py"},
                         self.git_reads(("git", "show", "HEAD:src/app.py"), cwd=copy_root, copies=[copy_root],
                                        git_state_path=f"{copy_root}/.git/HEAD").paths)
        copy_only_blob = object_id("blob", b"created only in copy")
        self.assertEqual(set(),
                         self.git_reads(("git", "cat-file", "-p", copy_only_blob), cwd=copy_root, copies=[copy_root],
                                        git_state_path=f"{copy_root}/.git/objects/{copy_only_blob[:2]}/{copy_only_blob[2:]}").paths)

    def test_non_git_listing_of_objects_or_refs_adds_nothing_while_opening_index_object_or_pack_adds_every_path(self) -> None:
        recording = self.recording
        list_only = [
            recording.Event(PYTHON, "python3", "diropen", f"{self.ROOT}/.git/objects", self.ROOT),
            recording.Event(PYTHON, "python3", "list", f"{self.ROOT}/.git/objects", self.ROOT),
            recording.Event(PYTHON, "python3", "statdir", f"{self.ROOT}/.git/objects/ab", self.ROOT),
            recording.Event(PYTHON, "python3", "list", f"{self.ROOT}/.git/refs", self.ROOT),
            recording.Event(PYTHON, "python3", "list", f"{self.ROOT}/.git/refs/heads", self.ROOT),
        ]
        reads = recording.git_directory_reads(list_only, [], self.project, self.ROOT)
        self.assertEqual((set(), set(), {}), (reads.paths, reads.memberships, reads.existence))
        for path in (f"{self.ROOT}/.git/index", f"{self.ROOT}/.git/objects/ab/cdef", f"{self.ROOT}/.git/objects/pack/pack-1.pack"):
            with self.subTest(path=path):
                opened = [recording.Event(PYTHON, "python3", "read", path, self.ROOT)]
                self.assertEqual({"**"}, recording.git_directory_reads(opened, [], self.project, self.ROOT).paths)

    def test_batch_helper_and_unrecorded_git_command_still_add_every_path(self) -> None:
        recording = self.recording
        self.assertEqual({"**"}, self.git_reads(("git", "cat-file", "--batch")).paths)
        helper_events = [
            recording.Event(OTHER, "git-lfs", "exec", "/usr/bin/git-lfs", self.ROOT, ("git-lfs", "pointer")),
            recording.Event(OTHER, "git-lfs", "read", f"{self.ROOT}/.git/HEAD", self.ROOT),
        ]
        self.assertEqual({"**"}, recording.git_directory_reads(helper_events, [], self.project, self.ROOT).paths)
        no_cmd_events = [
            recording.Event(OTHER, "git", "exec", "/usr/bin/git", self.ROOT, None),
            recording.Event(OTHER, "git", "read", f"{self.ROOT}/.git/HEAD", self.ROOT),
        ]
        self.assertEqual({"**"}, recording.git_directory_reads(no_cmd_events, [], self.project, self.ROOT).paths)


def replace_git_dirs(project, directories: tuple[str, ...]):
    from dataclasses import replace

    return replace(project, git_directories=directories)


if __name__ == "__main__":
    unittest.main()
