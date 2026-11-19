"""The recording step's reads of a ``git ls-files`` that prints only names, and ``select`` over them: a fixture matrix of
pathspecs and changes, in which each change that alters the output of a listing, as Git itself prints it, selects the
entry file that the listing's recorded reads belong to."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.recording_fixtures import GIT_IDENTITY, load_recording

ENTRY = "tests/entry.py"
VARIABLES = ("GIT_LITERAL_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_ICASE_PATHSPECS")

# The tracked files of the fixture repository before each change.
TREE = ("mypy.ini", "README.md", "Makefile", "src/app.py", "src/lib/util.py", "src/lib/deep/core.py", "tests/test_a.py",
        "tests/unit/test_b.py", "docs/guide.md", "lib/other.txt", ".gitattributes")
# The text of a tracked file is its name, except for these.
TEXTS = {".gitattributes": "src/** text\n"}

# Each change: the paths it adds and the paths it removes; a rename removes one and adds the other.
CHANGES = {
    "add a case variant of a root file": (("MYPY.INI",), ()),
    "add a root file beside it": ((".mypy.ini",), ()),
    "remove a root file": ((), ("mypy.ini",)),
    "rename a root file into a directory": (("config/mypy.ini",), ("mypy.ini",)),
    "add a file in a directory": (("src/new.py",), ()),
    "add a file two levels down": (("src/lib/deep/new.py",), ()),
    "remove a file two levels down": ((), ("src/lib/deep/core.py",)),
    "rename a nested file": (("src/lib/utils.py",), ("src/lib/util.py",)),
    "rename a file to another case": (("src/App.py",), ("src/app.py",)),
    "add a file in a new subdirectory": (("src/lib2/x.txt",), ()),
    "rename a directory": (("source/app.py",), ("src/app.py",)),
    "add a file whose name the directory name begins": (("srcx.py",), ()),
    "add a directory of another case": (("Src/app.py",), ()),
    "add a test file": (("tests/test_c.py",), ()),
    "add a test file two levels down": (("tests/unit/deep/test_d.py",), ()),
    "add a source file elsewhere": (("docs/x.py",), ()),
    "add a root configuration": (("pyproject.toml",), ()),
    "add an absent literal path": (("missing",), ()),
    "add a file beneath an absent literal path": (("missing/inner.txt",), ()),
    "add a file in a top-level directory of a nested name": (("lib/util.py",), ()),
    "remove a file in a top-level directory": ((), ("lib/other.txt",)),
    "add a file with a bracket in its name": (("src/[x].py",), ()),
    "add a file whose name holds a star": (("*.py",), ()),
    "add a file beneath a directory named as a wildcard pathspec": (("tests/test_*.py/new.txt",), ()),
}

# Each listing: Git's own options, the pathspecs after ``--``, the pathspec variables, the working directory relative to
# the fixture root, and whether its reads are narrower than ``**``.
LISTINGS = {
    "a literal root file": ((), ("mypy.ini",), (), "", True),
    "a literal directory": ((), ("src",), (), "", True),
    "a literal directory with a slash": ((), ("src/",), (), "", True),
    "a nested literal directory": ((), ("src/lib",), (), "", True),
    "a nested literal file": ((), ("src/lib/util.py",), (), "", True),
    "literal names, one absent": ((), ("mypy.ini", ".mypy.ini", "pyproject.toml"), (), "", True),
    "an absent literal path": ((), ("missing",), (), "", True),
    "a star": ((), ("*.py",), (), "", True),
    "a star after a directory": ((), ("tests/test_*.py",), (), "", True),
    "a question mark": ((), ("src/?pp.py",), (), "", True),
    "a glob over every depth": ((), (":(glob)**/*.py",), (), "", True),
    "a glob of direct children": ((), (":(glob)src/*",), (), "", True),
    "a glob of a whole directory": ((), (":(glob)tests/**",), (), "", True),
    "a glob without a wildcard": ((), (":(glob)src/lib",), (), "", True),
    "a literal directory with a long exclude": ((), ("src", ":(exclude)src/lib"), (), "", True),
    "a literal directory with a short exclude": ((), ("src", ":!src/app.py"), (), "", True),
    "icase magic": ((), (":(icase)mypy.ini",), (), "", False),
    "short top magic": ((), (":/src",), (), "", False),
    "literal magic": ((), (":(literal)src",), (), "", False),
    "top magic": ((), (":(top)src",), (), "", False),
    "attr magic": ((), (":(attr:text)src",), (), "", False),
    "glob and icase magic": ((), (":(glob,icase)**/*.py",), (), "", False),
    "a bracket class": ((), ("src/[al]*",), (), "", False),
    "only an exclude": ((), (":(exclude)src",), (), "", False),
    "the icase variable": ((), ("mypy.ini",), ("GIT_ICASE_PATHSPECS",), "", False),
    "the literal variable": ((), ("*.py",), ("GIT_LITERAL_PATHSPECS",), "", False),
    "the noglob variable": ((), ("*.py",), ("GIT_NOGLOB_PATHSPECS",), "", False),
    "the glob variable": ((), ("*.py",), ("GIT_GLOB_PATHSPECS",), "", False),
    "Git's -C": (("-C", "src"), ("lib",), (), "", False),
    "Git's icase option": (("--icase-pathspecs",), ("mypy.ini",), (), "", False),
    "a working directory below the root": ((), ("lib",), (), "src", False),
}


recording = load_recording()
suite_selection = recording.suite_selection


class Matrix:
    """The fixture repository, whose index each change rewrites from the commit of ``TREE``."""

    def __init__(self, base: Path) -> None:
        self.root = base / "repo"
        for name in TREE:
            (self.root / name).parent.mkdir(parents=True, exist_ok=True)
            (self.root / name).write_text(TEXTS.get(name, name), encoding="utf-8")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "fixture")
        self.blob = self.git("hash-object", "-w", "--stdin", input="added").strip()
        # A tracked directory whose name is a wildcard pathspec, which only the index holds: no Windows checkout can.
        self.git("update-index", "--add", "--cacheinfo", f"100644,{self.blob},tests/test_*.py/old.txt")
        self.git("commit", "-q", "-m", "wildcard directory")

    def git(self, *arguments: str, options: tuple[str, ...] = (), cwd: Path | None = None, variables: tuple[str, ...] = (),
            input: str | None = None) -> str:
        environment = {key: value for key, value in os.environ.items() if key not in VARIABLES}
        environment.update({name: "1" for name in variables})
        return subprocess.run(["git", *GIT_IDENTITY, "-c", "core.protectNTFS=false", *options, *arguments], cwd=cwd or self.root, input=input, capture_output=True,
                              text=True, encoding="utf-8", env=environment, check=True).stdout

    def change(self, added: tuple[str, ...], removed: tuple[str, ...]) -> None:
        self.git("read-tree", "HEAD")
        for path in removed:
            self.git("update-index", "--force-remove", "--", path)
        for path in added:
            self.git("update-index", "--add", "--cacheinfo", f"100644,{self.blob},{path}")

    def tracked(self) -> frozenset[str]:
        return frozenset(filter(None, self.git("ls-files", "-z").split("\0")))

    def arguments(self, listing: str) -> list[str]:
        options, pathspecs, _, _, _ = LISTINGS[listing]
        return [*(str(self.root / value) if previous == "-C" else value for previous, value in zip(("",) + options, options)),
                "ls-files", "-z", "--", *pathspecs]

    def listed(self, listing: str) -> str:
        _, _, variables, directory, _ = LISTINGS[listing]
        arguments = self.arguments(listing)
        command = arguments.index("ls-files")
        return self.git(*arguments[command:], options=tuple(arguments[:command]), cwd=self.root / directory, variables=variables)

    def reads(self, listing: str):
        """The recorded reads of the listing, from the arguments, working directory and environment of its exec."""
        _, _, variables, directory, _ = LISTINGS[listing]
        cwd = str(self.root / directory) if directory else str(self.root)
        return reads_of(["git", *self.arguments(listing)], cwd, str(self.root), tuple(f"{name}=1" for name in variables))


def reads_of(argv: list[str], cwd: str, root: str, environment: tuple[str, ...] | None = ()):
    event = recording.Event("1", "git", "exec", "/usr/bin/git", cwd, tuple(argv), environment)
    command, options, arguments = recording.git_command(event)
    assert command == "ls-files" and recording.prints_only_names(arguments), argv
    return recording.listing_reads(event, options, arguments, [root])


def suite_list(base: Path, reads) -> object:
    """A suite list whose one entry file has the recorded reads as its stored lists."""
    item = {"name": "listing", "entries": [ENTRY], "command": ["python3"], "entry_selection": True, "paths": [ENTRY],
            "entry_paths": {ENTRY: [ENTRY]}}
    if reads.memberships:
        item["entry_directory_memberships"] = {ENTRY: sorted(reads.memberships)}
    if reads.existence:
        item["entry_existence_reads"] = {ENTRY: [{"path": path, "kind": kind} for path, kind in sorted(reads.existence.items())]}
    path = base / "suites.json"
    path.write_text(json.dumps({"always": [], "suites": [item]}), encoding="utf-8")
    return suite_selection.load_suite_list(path)


def selected(suites, changed: list[str], before: frozenset[str], after: frozenset[str]) -> bool:
    (decision,) = suite_selection.select(suites, changed, previous_tracked=before, current_tracked=after).decisions
    return ENTRY in decision.entries and decision.run


class ListingReadsTests(unittest.TestCase):
    ROOT = "/checkout"

    def reads(self, *pathspecs: str, options: tuple[str, ...] = (), cwd: str = ROOT, environment: tuple[str, ...] | None = ()):
        return reads_of(["git", *options, "ls-files", "-z", "--", *pathspecs] if pathspecs else ["git", *options, "ls-files", "-z"],
                        cwd, self.ROOT, environment)

    def test_a_listing_without_a_pathspec_is_a_directory_membership_of_every_path(self) -> None:
        reads = self.reads()
        self.assertEqual(({"**"}, {}), (reads.memberships, reads.existence))
        self.assertEqual({"**"}, self.reads(options=("-C", "/elsewhere")).memberships)

    def test_a_literal_path_is_an_existence_read_of_any_kind_and_a_membership_beneath_it(self) -> None:
        reads = self.reads("mypy.ini", "src/lib/")
        self.assertEqual({"mypy.ini": "any", "src/lib": "any"}, reads.existence)
        self.assertEqual({"mypy.ini/**", "src/lib/**"}, reads.memberships)

    def test_each_star_and_question_mark_is_a_double_star(self) -> None:
        self.assertEqual({"tests/test_**.py", "tests/test_**.py/**", "src/**pp.py", "src/**pp.py/**"}, self.reads("tests/test_*.py", "src/?pp.py").memberships)

    def test_a_glob_pathspec_is_its_own_pattern(self) -> None:
        reads = self.reads(":(glob)**/requirements*.txt", ":(glob)src/*")
        self.assertEqual(({"**/requirements*.txt", "**/requirements*.txt/**", "src/*", "src/*/**"}, {}), (reads.memberships, reads.existence))

    def test_an_exclude_is_ignored(self) -> None:
        self.assertEqual({"src/**"}, self.reads("src", ":(exclude)src/lib", ":!src/app.py", ":^docs", ":(exclude,glob)x/*").memberships)

    def test_every_other_form_is_a_directory_membership_of_every_path(self) -> None:
        forms = {
            "other magic": self.reads(":(icase)mypy.ini"),
            "short top magic": self.reads(":/src"),
            "a bracket class": self.reads("src/[al]*"),
            "only an exclude": self.reads(":(exclude)src"),
            "a path out of the root": self.reads("../src"),
            "a working directory below the root": self.reads("lib", cwd=self.ROOT + "/src"),
            "-C": self.reads("lib", options=("-C", self.ROOT + "/src")),
            "Git's pathspec options": self.reads("mypy.ini", options=("--icase-pathspecs",)),
            "a pathspec file": reads_of(["git", "ls-files", "-z", "--pathspec-from-file", "list.txt"], self.ROOT, self.ROOT),
            "a pathspec variable": self.reads("mypy.ini", environment=("GIT_LITERAL_PATHSPECS=0",)),
            "an unknown environment": self.reads("mypy.ini", environment=None),
        }
        for form, reads in forms.items():
            with self.subTest(form=form):
                self.assertEqual(({"**"}, {}), (reads.memberships, reads.existence))


class FixtureMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(tempfile.mkdtemp(prefix="pathspec-matrix-"))
        cls.addClassCleanup(shutil.rmtree, cls.base, ignore_errors=True)
        cls.matrix = Matrix(cls.base)
        cls.before = cls.matrix.tracked()
        cls.listed_before = {listing: cls.matrix.listed(listing) for listing in LISTINGS}
        cls.outcomes: dict[tuple[str, str], tuple[bool, bool]] = {}
        for change, (added, removed) in CHANGES.items():
            cls.matrix.change(added, removed)
            after = cls.matrix.tracked()
            changed = sorted(set(added) | set(removed))
            for listing in LISTINGS:
                altered = cls.matrix.listed(listing) != cls.listed_before[listing]
                suites = suite_list(cls.base, cls.matrix.reads(listing))
                cls.outcomes[listing, change] = altered, selected(suites, changed, cls.before, after)
        cls.matrix.change((), ())

    def test_every_change_that_alters_a_listing_selects_its_entry_file(self) -> None:
        missed = [key for key, (altered, chosen) in self.outcomes.items() if altered and not chosen]
        self.assertEqual([], missed)

    def test_each_narrow_listing_is_altered_by_some_change_and_skips_some_other(self) -> None:
        for listing, (_, _, _, _, narrow) in LISTINGS.items():
            outcomes = [self.outcomes[listing, change] for change in CHANGES]
            with self.subTest(listing=listing):
                self.assertTrue(any(altered for altered, _ in outcomes))
                self.assertEqual(narrow, not all(chosen for _, chosen in outcomes))

    def test_the_reads_hold_no_every_path_membership_exactly_for_the_narrow_listings(self) -> None:
        for listing, (_, _, _, _, narrow) in LISTINGS.items():
            with self.subTest(listing=listing):
                self.assertEqual(not narrow, "**" in self.matrix.reads(listing).memberships)


if __name__ == "__main__":
    unittest.main()
