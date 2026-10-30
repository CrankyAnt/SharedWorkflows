from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

RESOURCES = REPOSITORY_ROOT / "actions" / "repository-hygiene"

sys.path.insert(0, str(RESOURCES))
import pins as pins_module  # noqa: E402


class PinsFileTests(unittest.TestCase):
    def test_pins_file_validates_and_every_tool_has_a_linux_entry(self) -> None:
        pins = pins_module.load_pins()
        self.assertEqual({"actionlint", "gitleaks", "shellcheck", "zizmor"}, set(pins["tools"]))
        for name, tool in pins["tools"].items():
            self.assertIn("linux-x64", tool["platforms"], name)
        self.assertEqual(pins["node"]["version"], json.loads((RESOURCES / "pins.json").read_text(encoding="utf-8"))["node"]["version"])

    def test_an_entry_without_version_asset_or_sha256_fails(self) -> None:
        pins = pins_module.load_pins()
        for mutate, message in [
            (lambda copy_: copy_["tools"]["zizmor"].pop("version"), "exactly repository, version, checksums and platforms"),
            (lambda copy_: copy_["tools"]["zizmor"].update(version=""), "lacks a version"),
            (lambda copy_: copy_["tools"]["zizmor"]["platforms"]["linux-x64"].pop("asset"), "exactly asset, binary and sha256"),
            (lambda copy_: copy_["tools"]["zizmor"]["platforms"]["linux-x64"].update(asset=""), "lacks a asset"),
            (lambda copy_: copy_["tools"]["zizmor"]["platforms"]["linux-x64"].update(sha256="abc"), "invalid sha256"),
            (lambda copy_: copy_["node"].update(version="24"), "invalid version"),
        ]:
            with self.subTest(message=message):
                mutated = copy.deepcopy(pins)
                mutate(mutated)
                with self.assertRaisesRegex(pins_module.PinsError, message):
                    pins_module.validate_pins(mutated)


if __name__ == "__main__":
    unittest.main()
