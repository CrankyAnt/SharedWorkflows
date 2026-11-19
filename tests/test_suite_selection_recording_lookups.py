"""The lookup report of the recording step on real Python processes: ``provide --python`` installs it into the site
directory of a set-up interpreter, and every process that runs ``site`` reports its module lookups."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.recording_fixtures import RECORDING, python_with_lookup_report

ENTRY = """\
    import sys
    sys.path.append("lib")
    import helper
    try:
        import absent_module
    except ImportError:
        pass
    print(helper.VALUE)
    """


class LookupReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(tempfile.mkdtemp(prefix="lookups-"))
        cls.addClassCleanup(shutil.rmtree, cls.base, ignore_errors=True)
        output = cls.base / "recording"
        (output / "setups").mkdir(parents=True)
        (output / "recording.json").write_text(json.dumps({"mode": "pull-request", "trace_set": "mid"}), encoding="utf-8")
        cls.python = python_with_lookup_report(cls.base / "python", lambda *options: subprocess.run(
            [sys.executable, str(RECORDING), "provide", "--output", str(output), "--setup", "python", *options],
            capture_output=True, text=True, check=False))
        cls.root = cls.base / "repo"
        for relative, text in {"tests/entry.py": ENTRY, "lib/helper.py": "VALUE = 1\n"}.items():
            (cls.root / relative).parent.mkdir(parents=True, exist_ok=True)
            (cls.root / relative).write_text(textwrap.dedent(text), encoding="utf-8")

    def run_python(self, *arguments: str) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
        """Run the set-up interpreter in the fixture with a report directory of its own, and the report's lines."""
        reports = Path(tempfile.mkdtemp(dir=self.base))
        environment = dict(os.environ, SHARED_LOOKUPS=str(reports), PYTHONPYCACHEPREFIX=str(self.base / "pycache"))
        ran = subprocess.run([str(self.python), *arguments], cwd=self.root, capture_output=True, text=True, env=environment, check=False)
        lines = [line.split("\t") for report in sorted(reports.glob("*.tsv")) for line in report.read_text(encoding="utf-8").splitlines()]
        return ran, lines

    def real(self, relative: str) -> str:
        return os.path.realpath(self.root / relative).replace("\\", "\\\\")

    def test_provide_installs_the_report_through_an_import_line_in_the_site_directory(self) -> None:
        site = subprocess.run([str(self.python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                              capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(["import shared_lookup_report"], Path(site, "shared-lookups.pth").read_text(encoding="utf-8").splitlines())
        paths = [subprocess.run([str(self.python), "-c", "import json, sys; print(json.dumps(sys.path))"], capture_output=True, text=True,
                                env=environment, check=True).stdout for environment in (dict(os.environ), dict(os.environ, SHARED_LOOKUPS=str(self.base)))]
        self.assertEqual(paths[0], paths[1], "the report adds no directory to the module search path")
        self.assertNotIn(str(RECORDING.parent), json.loads(paths[0]))

    def test_the_report_names_every_listing_and_every_lookup_of_the_import_system_found_or_absent(self) -> None:
        ran, lines = self.run_python("tests/entry.py")
        self.assertEqual((0, "1\n", ""), (ran.returncode, ran.stdout, ran.stderr))
        self.assertEqual("suffixes", lines[0][0])
        self.assertIn(".py", lines[0])
        self.assertIn(["list", lines[[line[0] for line in lines].index("list")][1], self.real("tests")], lines)
        self.assertTrue([line for line in lines if line[0] == "list" and line[2] == self.real("lib")])
        self.assertIn(["lookup", self.real("tests"), "helper", "absent"], lines)
        self.assertIn(["lookup", self.real("lib"), "helper", "found", self.real("lib/helper.py")], lines)
        for directory in ("tests", "lib"):
            self.assertIn(["lookup", self.real(directory), "absent_module", "absent"], lines)
        self.assertIn(["lookup", self.real("tests"), "json", "absent"], self.run_python("-c", "import sys; sys.path.insert(0, 'tests'); import json")[1])

    def test_a_directory_without_an_init_file_is_a_namespace_portion(self) -> None:
        (self.root / "lib" / "portion").mkdir(exist_ok=True)
        self.assertIn(["lookup", self.real("lib"), "portion", "namespace"], self.run_python("-c", "import sys; sys.path.insert(0, 'lib'); import portion")[1])

    def test_isolated_and_environment_ignoring_processes_report_and_one_without_site_does_not(self) -> None:
        code = "import sys; sys.path.insert(0, 'lib'); import helper"
        for option in ("-I", "-E"):
            with self.subTest(option=option):
                ran, lines = self.run_python(option, "-c", code)
                self.assertEqual((0, ""), (ran.returncode, ran.stderr))
                self.assertIn(["lookup", self.real("lib"), "helper", "found", self.real("lib/helper.py")], lines)
        ran, lines = self.run_python("-S", "-c", code)
        self.assertEqual((0, [], ""), (ran.returncode, lines, ran.stderr))

    def test_without_the_variable_the_report_writes_nothing(self) -> None:
        environment = {key: value for key, value in os.environ.items() if key != "SHARED_LOOKUPS"}
        ran = subprocess.run([str(self.python), "tests/entry.py"], cwd=self.root, capture_output=True, text=True, check=False,
                             env=dict(environment, PYTHONPYCACHEPREFIX=str(self.base / "pycache")))
        self.assertEqual((0, "1\n", ""), (ran.returncode, ran.stdout, ran.stderr))
        self.assertEqual([], [path for path in self.root.rglob("*.tsv")])


if __name__ == "__main__":
    unittest.main()
