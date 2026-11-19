"""The classification rules of the recording step's check, on stored strace recordings of fixture entry files and on the
neutral event files of a Windows ETW recording of the same fixture, each with the lookup reports of their Python
processes."""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import unittest
from pathlib import Path, PureWindowsPath

from tests.recording_fixtures import (ETW_RECORDINGS, ROOT, STRACE_RECORDINGS, RecordingFixture, copy_recording, copy_strace_recording,
                                      requires_posix_paths, suite)

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

    def test_a_stat_of_a_path_that_exists_in_the_checkout_is_a_content_read(self) -> None:
        self.assertIn("data/stat.txt", self.reads("tests/stats.py")["paths"])

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
        reads = self.reads("tests/lookups.py")
        self.assertIn("lib/helper.py", reads["paths"])
        existence = self.existence("tests/lookups.py")
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

    def test_only_the_tracked_gitignore_files_of_the_checked_commit_decide_matched_case_sensitively(self) -> None:
        # listed/ matches the directory that the recording lists, absent.txt/ no directory, and Missing-Tool and
        # ABSENT-IN-COPY.TXT nothing under case-sensitive matching. The local settings, an untracked .gitignore file and an
        # uncommitted edit of the tracked one match the rest.
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
        local = {"absent.txt": "any", "missing-tool": "any", "lib/absent-in-copy.txt": "any"}

        checked = fixture.check()
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        reads = classified_lists(fixture)
        existence = {read["path"]: read["kind"] for entry in reads.values() for read in entry["existence_reads"]}
        self.assertLessEqual(local.items(), existence.items())
        self.assertNotIn("listed", reads["tests/imports.py"]["directory_memberships"])
        self.assertNotIn("cache.txt", reads["tests/ignored.py"]["paths"])

        # A pull-request run with empty lists reports the reads that only the local settings match as additions.
        settings = fixture.output / "recording.json"
        settings.write_text(settings.read_text(encoding="utf-8").replace('"full"', '"pull-request"'), encoding="utf-8")
        checked = fixture.check()
        self.assertEqual(1, checked.returncode, checked.stdout + checked.stderr)
        added = fixture.additions()["suites"][0]["entries"]
        additions = {read["path"]: read["kind"] for entry in added.values() for read in entry["existence_reads"]}
        self.assertLessEqual(local.items(), additions.items())
        self.assertNotIn("cache.txt", added["tests/ignored.py"]["paths"])

    def test_an_absent_root_name_that_a_program_other_than_powershell_probes_is_an_existence_read(self) -> None:
        # The entry file starts git and then probes git.ps1 itself: no name drops because a started program shares its stem.
        existence = self.existence("tests/commands.py")
        self.assertEqual("any", existence["git.ps1"])
        self.assertEqual("any", existence["missing-tool"])

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
        self.assertEqual(["**"], reads["directory_memberships"])
        self.assertEqual(["tests/git_names.py"], reads["paths"])

    def test_git_cat_file_of_full_object_ids_reads_the_tracked_paths_that_hold_each_object(self) -> None:
        # A blob that two paths hold, a tree with a ^{tree} suffix, and a blob that no tracked path holds; the Python
        # process's reads of the Git directory's configuration and HEAD stay dropped.
        self.assertEqual(["held/**", "held/also-one.txt", "held/one.txt", "tests/git_objects.py"], self.reads("tests/git_objects.py")["paths"])

    def test_git_cat_file_of_the_checked_commits_id_reads_every_tracked_path(self) -> None:
        self.assertEqual(["**", "tests/git_commit.py"], self.reads("tests/git_commit.py")["paths"])

    def test_git_cat_file_of_the_checked_commits_tree_id_reads_every_tracked_path(self) -> None:
        self.assertEqual(["**", "tests/git_root_tree.py"], self.reads("tests/git_root_tree.py")["paths"])

    def test_git_cat_file_of_names_that_are_not_only_full_object_ids_reads_every_tracked_path(self) -> None:
        # Names on standard input, a full id beside a revision expression, and a full id with the suffix ^{}.
        for entry in ("tests/git_batch.py", "tests/git_mixed.py", "tests/git_peeled.py"):
            self.assertEqual(["**", entry], self.reads(entry)["paths"], entry)

    def test_a_git_process_that_reads_the_configuration_reads_every_tracked_path_through_discovery(self) -> None:
        self.assertEqual(["**", "tests/git_config.py"], self.reads("tests/git_config.py")["paths"])

    def linked(self) -> RecordingFixture:
        """The linked-worktree fixture, holding a checked recording of LINKED_ENTRIES."""
        raise NotImplementedError

    def test_a_git_process_in_a_checkout_that_is_a_linked_worktree_reads_its_git_and_common_directory(self) -> None:
        fixture = self.linked()
        self.assertEqual(["**", "tests/linked_revisions.py"], fixture.reads_of("tests/linked_revisions.py")["paths"])
        self.assertEqual(["**"], fixture.reads_of("tests/linked_names.py")["directory_memberships"])


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
        extensions = [path for path in self.existence("tests/lookups.py") if path.startswith("lib/helper.")]
        self.assertIn("lib/helper.so", extensions)
        self.assertIn("lib/helper.abi3.so", extensions)

    def test_a_command_search_through_the_working_directory_on_path_is_an_existence_read(self) -> None:
        # The entry file puts its working directory first on PATH: Python's search probes sh there, and sh probes git.
        existence = self.existence("tests/commands.py")
        self.assertEqual("any", existence["sh"])
        self.assertEqual("any", existence["git"])

    def test_an_absent_path_that_a_process_tries_to_run_is_an_existence_read(self) -> None:
        self.assertEqual("any", self.existence("tests/commands.py")["run.exe"])

    def test_no_linux_read_drops_for_a_colon(self) -> None:
        self.assertEqual("any", self.existence("tests/commands.py")["notes:draft.txt"])

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
        for entry in ("tests/git_pathspec_variable.py", "tests/git_pathspec_subdirectory.py", "tests/git_pathspec_magic.py"):
            with self.subTest(entry=entry):
                self.assertEqual(["**"], self.reads(entry)["directory_memberships"])

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

    def test_a_directory_on_the_checking_machines_disk_is_no_directory_that_the_recording_shows(self) -> None:
        # Without the opens of data\, the recording shows data only through an enumeration of its literal name, which
        # names no directory, so data/ does not match it although data is a directory where check runs.
        files = {path: text for path, text in WINDOWS_FILES.items() if path != "data/stat.txt"}
        fixture = self.recorded({**files, ".gitignore": DATA[".gitignore"] + "data/\n"})
        (fixture.root / "data").mkdir()
        events = fixture.output / "events.tsv"
        events.write_bytes(b"".join(line for line in events.read_bytes().splitlines(keepends=True) if b"\\repo\\data\\" not in line))
        checked = fixture.check()
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        existence = {read["path"]: read["kind"] for read in fixture.reads_of("tests/stats.py")["existence_reads"]}
        self.assertEqual("directory", existence.get("data"), existence)

    def second(self) -> dict:
        other = classification_fixture(self.addCleanup, WINDOWS_FILES)
        copy_recording(ETW_RECORDINGS / "classification" / "second", other.output)
        checked = other.check()
        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        return classified_lists(other)

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


if __name__ == "__main__":
    unittest.main()
