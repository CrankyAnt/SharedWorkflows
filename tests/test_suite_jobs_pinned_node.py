"""Proof only: this entry file states the pinned tool ``node`` in a Linux suite and in a Windows suite, and runs the
Node.js that its job installed at ``node.version`` of the pins file."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

PINS = Path(__file__).resolve().parents[1] / "actions" / "repository-hygiene" / "pins.json"
ON_A_HOSTED_RUNNER = os.environ.get("GITHUB_ACTIONS") == "true" and bool(os.environ.get("RUNNER_TOOL_CACHE"))


@unittest.skipUnless(ON_A_HOSTED_RUNNER, "only a suite job on a hosted runner gives the pinned tools")
class PinnedNodeTests(unittest.TestCase):
    def test_the_job_gives_the_node_of_the_pins_file_first_on_the_path(self) -> None:
        pinned = json.loads(PINS.read_text(encoding="utf-8"))["node"]["version"]
        found = shutil.which("node")
        self.assertIsNotNone(found, "the entry file states the pinned tool node, so its job installs it")
        print(f"PINNED NODE {pinned} FOUND {found}")
        installed = Path(os.environ["RUNNER_TOOL_CACHE"]) / "node" / pinned
        self.assertTrue(Path(str(found)).resolve().is_relative_to(installed.resolve()), f"{found} is not under {installed}")
        version = subprocess.run([str(found), "--version"], capture_output=True, text=True, check=True).stdout.strip()
        print(f"NODE VERSION {version}")
        self.assertEqual(f"v{pinned}", version)


if __name__ == "__main__":
    unittest.main()
