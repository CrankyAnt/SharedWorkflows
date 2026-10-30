"""The committed build of the ETW consumer belongs to its committed source."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = REPOSITORY_ROOT / "actions" / "recording" / "etw_consumer_build.py"


@unittest.skipUnless(sys.platform == "win32", "the consumer's build is byte-reproducible on Windows only")
class CommittedBuildTest(unittest.TestCase):
    def test_a_rebuild_of_the_source_equals_the_committed_build(self) -> None:
        if shutil.which("dotnet") is None:
            if os.environ.get("GITHUB_ACTIONS") == "true":
                self.fail("dotnet is not on PATH: the job runs this entry file without its dotnet setup")
            self.skipTest("the pinned .NET SDK is not on PATH")
        result = subprocess.run([sys.executable, str(BUILD_SCRIPT), "check"], cwd=REPOSITORY_ROOT,
                                capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class PublishCommandTest(unittest.TestCase):
    def test_the_build_runs_without_build_servers_and_node_reuse(self) -> None:
        spec = importlib.util.spec_from_file_location("etw_consumer_build", BUILD_SCRIPT)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, work, True)
        with mock.patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            module.publish(work / "build", work / "artifacts")
        command = run.call_args.args[0]
        self.assertEqual(["dotnet", "publish"], command[:2])
        for flag in ("--disable-build-servers", "-nodeReuse:false", "-p:UseSharedCompilation=false"):
            self.assertIn(flag, command)


if __name__ == "__main__":
    unittest.main()
