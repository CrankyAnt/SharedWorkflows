"""Text contracts of the convention resources, the publication caller template and the specification updater."""

from __future__ import annotations

import json
import unittest

from tests.ci_policy_support import REPOSITORY_ROOT


WORKFLOWS = REPOSITORY_ROOT / ".github" / "workflows"
TEMPLATE_DEPENDABOT = REPOSITORY_ROOT / "actions" / "dependabot-auto-merge" / "consumer-kit" / "dependabot.yml"
ZIZMOR_POLICY = REPOSITORY_ROOT / ".github" / "zizmor.yml"
DEPENDABOT = REPOSITORY_ROOT / ".github" / "dependabot.yml"
PACKAGE = REPOSITORY_ROOT / "actions" / "publication-checks" / "package.json"
LOCKFILE = REPOSITORY_ROOT / "actions" / "publication-checks" / "package-lock.json"


class PublicationWorkflowTests(unittest.TestCase):
    def test_the_convention_resources_and_the_updater_preserve_the_contract(self) -> None:
        self.assertIn("package-ecosystem: github-actions", TEMPLATE_DEPENDABOT.read_text(encoding="utf-8"))
        policy = ZIZMOR_POLICY.read_text(encoding="utf-8")
        self.assertIn('"*": hash-pin', policy)
        self.assertNotIn("publication-checks.yml", policy, "the publication-checks workflow is gone")
        dependabot = DEPENDABOT.read_text(encoding="utf-8")
        self.assertIn("package-ecosystem: github-actions", dependabot)
        self.assertIn("directory: /actions/publication-checks", dependabot)
        self.assertIn("uses: $/actions/publication-checks\n", (WORKFLOWS / "repository-hygiene.yml").read_text(encoding="utf-8"),
                      "the npm entry covers the package that the hygiene job installs")
        self.assertTrue(PACKAGE.is_file())
        updater = (WORKFLOWS / "update-publication-spec.yml").read_text(encoding="utf-8")
        self.assertRegex(updater, r"(?m)^  plan:\n    runs-on: ubuntu-slim\n    permissions:\n      contents: read$")
        self.assertRegex(updater, r"(?m)^  propose:\n    needs: plan\n    if: .*\n    runs-on: ubuntu-slim\n    permissions:\n      contents: read$")
        self.assertIn("uses: $/actions/repository-hygiene", updater)
        self.assertIn("node-version: ${{ steps.pins.outputs.node-version }}", updater)
        self.assertIn("install_tools.py --pins actions/repository-hygiene/pins.json --platform linux-x64 --tool gitleaks", updater)
        self.assertIn('--log-opts="HEAD --not --remotes=origin"', updater)
        self.assertLess(updater.index("fetch --prune origin"), updater.index("--log-opts="))
        self.assertIn("git -c credential.helper= -c 'credential.helper=!gh auth git-credential' ls-remote --exit-code --heads origin \"refs/heads/$branch\"", updater)
        self.assertIn("git -c credential.helper= -c 'credential.helper=!gh auth git-credential' fetch --no-tags origin \"refs/heads/$branch:refs/remotes/origin/$branch\"", updater)
        self.assertIn('--force-with-lease="refs/heads/$branch:$remote_tip"', updater)
        package = json.loads(PACKAGE.read_text(encoding="utf-8"))
        lockfile = json.loads(LOCKFILE.read_text(encoding="utf-8"))
        self.assertNotIn("scripts", package)
        for name, version in package["dependencies"].items():
            self.assertEqual(version, lockfile["packages"][f"node_modules/{name}"]["version"])
        for name, metadata in lockfile["packages"].items():
            if name:
                self.assertTrue(metadata["resolved"].startswith("https://registry.npmjs.org/"))
                self.assertTrue(metadata["integrity"].startswith("sha512-"))
