from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

RESOURCES = REPOSITORY_ROOT / "actions" / "repository-hygiene"

sys.path.insert(0, str(RESOURCES))
import pins as pins_module  # noqa: E402


class PinsFileTests(unittest.TestCase):
    def test_schema_requires_what_the_loader_enforces(self) -> None:
        schema = json.loads((RESOURCES / "pins.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(["node", "tools"], schema["required"])
        self.assertEqual(["repository", "version", "checksums", "platforms"], schema["$defs"]["tool"]["required"])
        self.assertEqual(["asset", "binary", "sha256"], schema["$defs"]["platform"]["required"])
        self.assertEqual(pins_module.VERSION.pattern, schema["$defs"]["version"]["pattern"])
        self.assertEqual(pins_module.SHA256.pattern, schema["$defs"]["sha256"]["pattern"])
        self.assertEqual("./pins.schema.json", json.loads((RESOURCES / "pins.json").read_text(encoding="utf-8"))["$schema"])
        self.assertEqual(["requirement", "version", "build", "packages"], schema["$defs"]["python-tool"]["required"])
        self.assertEqual(["version", "sha256"], schema["$defs"]["python-package"]["required"])
        self.assertEqual(pins_module.PACKAGE_NAME.pattern, schema["properties"]["python"]["propertyNames"]["pattern"])
        self.assertEqual(pins_module.PACKAGE_VERSION.pattern, schema["$defs"]["python-package"]["properties"]["version"]["pattern"])

    def test_reuse_is_pinned_with_its_whole_hash_locked_closure(self) -> None:
        reuse = pins_module.load_pins()["python"]["reuse"]
        self.assertEqual("reuse[charset-normalizer]", reuse["requirement"])
        self.assertEqual(reuse["version"], reuse["packages"]["reuse"]["version"])
        self.assertIn("charset-normalizer", reuse["packages"])
        self.assertEqual(["poetry-core"], reuse["build"])
        for name, package in reuse["packages"].items():
            with self.subTest(package=name):
                self.assertTrue(package["sha256"], "every package carries at least one sha256")

    def test_a_python_tool_must_lock_itself_and_its_build_packages(self) -> None:
        def pins(tool: dict) -> dict:
            return {"node": {"version": "24.0.0"}, "tools": {"a": {"repository": "o/a", "version": "1.0.0", "checksums": None,
                    "platforms": {"linux-x64": {"asset": "a", "binary": "a", "sha256": "0" * 64}}}}, "python": {"alpha": tool}}
        package = {"version": "1.0", "sha256": ["1" * 64]}
        valid = {"requirement": "alpha[extra]", "version": "1.0.0", "build": ["backend"], "packages": {"alpha": {"version": "1.0.0", "sha256": ["0" * 64]}, "backend": package}}
        self.assertEqual(valid, pins_module.validate_pins(pins(valid))["python"]["alpha"])
        for broken, message in (
            ({**valid, "requirement": "beta"}, "requirement must name alpha"),
            ({**valid, "build": ["missing"]}, "build package missing is not locked"),
            ({**valid, "packages": {"backend": package}}, "does not lock alpha itself"),
            ({**valid, "packages": {**valid["packages"], "alpha": {"version": "2.0.0", "sha256": ["0" * 64]}}}, "locks alpha at 2.0.0"),
            ({**valid, "packages": {**valid["packages"], "backend": {"version": "1.0", "sha256": []}}}, "backend lacks a sha256"),
            ({**valid, "packages": {**valid["packages"], "Backend": package}}, "invalid package name"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(pins_module.PinsError, message):
                pins_module.validate_pins(pins(broken))


if __name__ == "__main__":
    unittest.main()
