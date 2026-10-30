"""The weekly pin updater against mocked release, checksum and asset responses."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.workflow_commands import script_invocations

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RESOURCES = REPOSITORY_ROOT / "actions" / "repository-hygiene"
SCRIPT = RESOURCES / "update_pins.py"
SPEC = importlib.util.spec_from_file_location("update_pins", SCRIPT)
assert SPEC and SPEC.loader
update_pins = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = update_pins  # dataclasses resolve annotations through sys.modules
SPEC.loader.exec_module(update_pins)
sys.path.insert(0, str(RESOURCES))
import pins as pins_module  # noqa: E402

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)


def days_ago(days: int) -> str:
    return (NOW - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def pins_document() -> dict:
    return {
        "node": {"version": "24.22.0"},
        "tools": {
            "alpha": {"repository": "fixture/alpha", "version": "1.2.3", "checksums": "alpha_{version}_checksums.txt",
                      "platforms": {
                          "linux-x64": {"asset": "alpha_{version}_linux.tar.gz", "binary": "alpha", "sha256": "a" * 64},
                          "windows-x64": {"asset": "alpha_{version}_windows.zip", "binary": "alpha.exe", "sha256": "b" * 64},
                      }},
            "beta": {"repository": "fixture/beta", "version": "2.0.0", "checksums": None,
                     "platforms": {"linux-x64": {"asset": "beta-v{version}.tar.xz", "binary": "beta-v{version}/beta", "sha256": "c" * 64}}},
        },
    }


class Upstream:
    """Mocked upstreams: releases per repository, assets per URL, the Node.js index, and a record of every request."""

    def __init__(self) -> None:
        self.releases: dict[str, list[dict]] = {"fixture/alpha": [], "fixture/beta": []}
        self.assets: dict[str, bytes] = {}
        self.node_index: list[dict] = [{"version": "v24.22.0", "date": "2026-10-14"}]
        self.unreachable: set[str] = set()
        self.requests: list[str] = []

    def release(self, repository: str, version: str, age_days: int, **fields: object) -> None:
        self.releases[repository].append({"tag_name": f"v{version}", "draft": False, "prerelease": False, "published_at": days_ago(age_days), **fields})

    def asset(self, repository: str, version: str, name: str, content: bytes) -> str:
        self.assets[f"https://github.com/{repository}/releases/download/v{version}/{name}"] = content
        return hashlib.sha256(content).hexdigest()

    def checksums(self, repository: str, version: str, name: str, entries: dict[str, str]) -> None:
        text = "".join(f"{digest}  {asset}\n" for asset, digest in entries.items())
        self.assets[f"https://github.com/{repository}/releases/download/v{version}/{name}"] = text.encode("utf-8")

    def fetch(self, url: str) -> bytes:
        self.requests.append(url)
        if url in self.unreachable:
            raise OSError("connection refused")
        if url == update_pins.NODE_INDEX_URL:
            return json.dumps(self.node_index).encode("utf-8")
        for repository, releases in self.releases.items():
            if url == update_pins.RELEASES_URL.format(repository=repository):
                return json.dumps(releases).encode("utf-8")
        if url in self.assets:
            return self.assets[url]
        raise OSError(f"not found: {url}")

    def alpha_release(self, version: str, age_days: int, linux: bytes = b"linux", windows: bytes = b"windows", checksum_of_linux: str | None = None) -> None:
        self.release("fixture/alpha", version, age_days)
        linux_digest = self.asset("fixture/alpha", version, f"alpha_{version}_linux.tar.gz", linux)
        windows_digest = self.asset("fixture/alpha", version, f"alpha_{version}_windows.zip", windows)
        self.checksums("fixture/alpha", version, f"alpha_{version}_checksums.txt", {
            f"alpha_{version}_linux.tar.gz": checksum_of_linux or linux_digest,
            f"alpha_{version}_windows.zip": windows_digest,
        })


class PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = Upstream()

    def plan(self) -> update_pins.Plan:
        return update_pins.plan(pins_document(), fetcher=self.upstream.fetch, now=NOW)

    def test_nothing_newer_proposes_nothing(self) -> None:
        self.upstream.release("fixture/alpha", "1.2.3", 30)
        self.upstream.release("fixture/beta", "1.9.0", 30)
        result = self.plan()
        self.assertFalse(result.changed)
        self.assertFalse(result.failed)
        self.assertEqual(["alpha 1.2.3", "beta 2.0.0", "node 24.22.0"], result.current)
        self.assertEqual(pins_document(), result.pins)

    def test_a_newer_release_refreshes_every_platform_entry_from_the_upstream_checksums_file(self) -> None:
        self.upstream.alpha_release("1.3.0", 10, linux=b"new linux", windows=b"new windows")
        result = self.plan()
        self.assertTrue(result.changed)
        proposal = result.proposals[0]
        self.assertEqual(("alpha", "1.2.3", "1.3.0"), (proposal.tool, proposal.old, proposal.new))
        self.assertEqual("upstream checksums file `alpha_1.3.0_checksums.txt`", proposal.sha256_source)
        self.assertEqual(hashlib.sha256(b"new linux").hexdigest(), result.pins["tools"]["alpha"]["platforms"]["linux-x64"]["sha256"])
        self.assertEqual(hashlib.sha256(b"new windows").hexdigest(), result.pins["tools"]["alpha"]["platforms"]["windows-x64"]["sha256"])
        self.assertEqual("1.3.0", result.pins["tools"]["alpha"]["version"])
        self.assertEqual("alpha_{version}_linux.tar.gz", result.pins["tools"]["alpha"]["platforms"]["linux-x64"]["asset"], "asset names stay templates")
        self.assertEqual(pins_document()["tools"]["beta"], result.pins["tools"]["beta"])

    def test_the_newest_release_waits_until_it_is_seven_days_old(self) -> None:
        self.upstream.alpha_release("1.4.0", 6)
        self.upstream.alpha_release("1.3.0", 7)
        self.assertEqual("1.3.0", self.plan().proposals[0].new, "seven days old is old enough; six days is not")
        self.upstream = Upstream()
        self.upstream.alpha_release("1.4.0", 3)
        self.upstream.release("fixture/alpha", "1.2.3", 40)
        result = self.plan()
        self.assertFalse(result.changed, "the only old enough release is the pinned one")
        self.assertIn("alpha 1.2.3", result.current)

    def test_a_shorter_cooldown_proposes_a_younger_release(self) -> None:
        self.upstream.alpha_release("1.4.0", 2)
        self.assertFalse(self.plan().changed, "two days is younger than the default cooldown")
        result = update_pins.plan(pins_document(), fetcher=self.upstream.fetch, now=NOW, cooldown_days=2)
        self.assertEqual("1.4.0", result.proposals[0].new)
        result = update_pins.plan(pins_document(), fetcher=self.upstream.fetch, now=NOW, cooldown_days=0)
        self.assertEqual("1.4.0", result.proposals[0].new, "a cooldown of 0 days takes every published release")

    def test_drafts_prereleases_and_unparsable_tags_are_ignored(self) -> None:
        self.upstream.release("fixture/alpha", "1.5.0", 30, draft=True)
        self.upstream.release("fixture/alpha", "1.4.0", 30, prerelease=True)
        self.upstream.releases["fixture/alpha"].append({"tag_name": "v1.3.0-rc.1", "draft": False, "prerelease": False, "published_at": days_ago(30)})
        self.upstream.releases["fixture/alpha"].append({"tag_name": "nightly", "draft": False, "prerelease": False, "published_at": days_ago(30)})
        self.assertFalse(self.plan().changed)

    def test_a_checksum_mismatch_fails_the_run_and_proposes_nothing_for_that_tool(self) -> None:
        self.upstream.alpha_release("1.3.0", 10, checksum_of_linux="0" * 64)
        self.upstream.release("fixture/beta", "2.1.0", 10)
        self.upstream.asset("fixture/beta", "2.1.0", "beta-v2.1.0.tar.xz", b"beta")
        result = self.plan()
        self.assertTrue(result.failed)
        self.assertEqual(1, len(result.failures))
        self.assertIn("alpha 1.3.0: alpha_1.3.0_linux.tar.gz downloaded with sha256", result.failures[0])
        self.assertEqual(pins_document()["tools"]["alpha"], result.pins["tools"]["alpha"])
        self.assertEqual(["beta"], [proposal.tool for proposal in result.proposals], "the other tool is still planned")
        self.upstream = Upstream()
        self.upstream.release("fixture/alpha", "1.3.0", 10)
        self.upstream.asset("fixture/alpha", "1.3.0", "alpha_1.3.0_linux.tar.gz", b"x")
        self.upstream.asset("fixture/alpha", "1.3.0", "alpha_1.3.0_windows.zip", b"y")
        self.upstream.checksums("fixture/alpha", "1.3.0", "alpha_1.3.0_checksums.txt", {"alpha_1.3.0_linux.tar.gz": hashlib.sha256(b"x").hexdigest()})
        result = self.plan()
        self.assertIn("lists no entry for alpha_1.3.0_windows.zip", result.failures[0])

    def test_without_an_upstream_checksums_file_the_sha256_is_computed_and_said_so(self) -> None:
        self.upstream.release("fixture/beta", "2.1.0", 10)
        digest = self.upstream.asset("fixture/beta", "2.1.0", "beta-v2.1.0.tar.xz", b"beta 2.1.0")
        result = self.plan()
        proposal = result.proposals[0]
        self.assertEqual("computed from the download; the upstream publishes no checksums file", proposal.sha256_source)
        self.assertEqual(digest, result.pins["tools"]["beta"]["platforms"]["linux-x64"]["sha256"])
        self.assertEqual("beta-v{version}/beta", result.pins["tools"]["beta"]["platforms"]["linux-x64"]["binary"])

    def test_node_moves_within_its_major_line_only(self) -> None:
        self.upstream.node_index = [
            {"version": "v26.1.0", "date": "2026-09-01"},
            {"version": "v24.23.0", "date": "2026-10-20"},
            {"version": "v24.22.2", "date": "2026-10-13"},
            {"version": "v24.22.1", "date": "2026-10-14"},
            {"version": "v24.22.0", "date": "2026-10-01"},
            {"version": "v22.20.0", "date": "2026-09-01"},
        ]
        result = self.plan()
        node = [proposal for proposal in result.proposals if proposal.tool == "node"][0]
        self.assertEqual(("24.22.0", "24.22.2"), (node.old, node.new), "24.22.1 and 24.23.0 are younger than seven days; 26.x is another major line")
        self.assertEqual("24.22.2", result.pins["node"]["version"])
        self.upstream.node_index = [{"version": "v26.1.0", "date": "2026-09-01"}, {"version": "v24.22.0", "date": "2026-10-01"}]
        self.assertFalse(self.plan().changed)

    def test_an_unreachable_upstream_leaves_the_pin_unchanged_and_appears_in_the_summary(self) -> None:
        self.upstream.unreachable.add(update_pins.RELEASES_URL.format(repository="fixture/alpha"))
        self.upstream.unreachable.add(update_pins.NODE_INDEX_URL)
        self.upstream.release("fixture/beta", "2.1.0", 10)
        self.upstream.asset("fixture/beta", "2.1.0", "beta-v2.1.0.tar.xz", b"beta")
        result = self.plan()
        self.assertFalse(result.failed)
        self.assertEqual(["beta"], [proposal.tool for proposal in result.proposals])
        self.assertEqual(2, len(result.unreachable))
        self.assertIn("alpha:", result.unreachable[0])
        self.assertIn("node:", result.unreachable[1])
        self.assertEqual(pins_document()["tools"]["alpha"], result.pins["tools"]["alpha"])
        summary = update_pins.render_summary(result)
        self.assertIn("Unreachable upstreams, pins left unchanged:", summary)
        self.assertIn("| beta | 2.0.0 | 2.1.0 |", summary)
        self.upstream.unreachable.add("https://github.com/fixture/beta/releases/download/v2.1.0/beta-v2.1.0.tar.xz")
        result = self.plan()
        self.assertFalse(result.changed, "an unreachable asset leaves that tool unchanged too")

    def test_the_pull_request_body_names_old_and_new_versions_and_each_sha256_source(self) -> None:
        self.upstream.alpha_release("1.3.0", 10)
        self.upstream.release("fixture/beta", "2.1.0", 10)
        self.upstream.asset("fixture/beta", "2.1.0", "beta-v2.1.0.tar.xz", b"beta")
        self.upstream.node_index.append({"version": "v24.22.5", "date": "2026-10-01"})
        body = update_pins.render_pr_body(self.plan(), "actions/repository-hygiene/pins.json")
        self.assertIn("| alpha | 1.2.3 | 1.3.0 | upstream checksums file `alpha_1.3.0_checksums.txt` |", body)
        self.assertIn("| beta | 2.0.0 | 2.1.0 | computed from the download; the upstream publishes no checksums file |", body)
        self.assertIn("| node | 24.22.0 | 24.22.5 | not downloaded here; actions/setup-node verifies the Node.js download it installs |", body)
        self.assertIn("moves `v1`", body)


def sdist(build_requires: list[str]) -> bytes:
    """An sdist that holds only the pyproject.toml that states its build requirements."""
    import io
    import tarfile
    content = ("[build-system]\nrequires = [" + ", ".join(f'"{item}"' for item in build_requires) + "]\n").encode("utf-8")
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
        info = tarfile.TarInfo("gamma-1.1.0/pyproject.toml")
        info.size = len(content)
        bundle.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


def distribution(filename: str, age_days: int, digest: str) -> dict:
    return {"filename": filename, "url": f"https://files.example/{filename}", "upload_time_iso_8601": days_ago(age_days),
            "yanked": False, "digests": {"sha256": digest}}


class PythonToolPlanTests(unittest.TestCase):
    """A Python tool's newer PyPI release relocks its whole closure from pip's resolution and PyPI's sha256 values."""

    def setUp(self) -> None:
        self.upstream = Upstream()
        self.upstream.release("fixture/alpha", "1.2.3", 30)
        self.upstream.release("fixture/beta", "2.0.0", 30)
        self.projects: dict[str, dict] = {}
        self.resolutions: list[tuple[list[str], datetime]] = []
        self.resolved = {"gamma": "1.1.0", "delta": "3.0"}
        self.resolved_build = {"backend": "2.1.0"}

    def pins(self) -> dict:
        document = pins_document()
        document["python"] = {"gamma": {"requirement": "gamma[extra]", "version": "1.0.0", "build": ["backend"], "packages": {
            "gamma": {"version": "1.0.0", "sha256": ["d" * 64]}, "backend": {"version": "2.0.0", "sha256": ["e" * 64]}}}}
        return document

    def project(self, name: str, version: str, files: list[dict]) -> None:
        self.projects.setdefault(name, {"releases": {}})["releases"][version] = files
        self.upstream.assets[update_pins.PYPI_VERSION_URL.format(name=name, version=version)] = json.dumps({"urls": files}).encode("utf-8")
        self.upstream.assets[update_pins.PYPI_URL.format(name=name)] = json.dumps(self.projects[name]).encode("utf-8")

    def resolve(self, requirements: list[str], cutoff: datetime) -> dict[str, str]:
        self.resolutions.append((requirements, cutoff))
        return dict(self.resolved_build if requirements == ["backend>=2"] else self.resolved)

    def plan(self) -> update_pins.Plan:
        return update_pins.plan(self.pins(), fetcher=self.upstream.fetch, now=NOW, resolver=self.resolve)

    def gamma_release(self, age_days: int = 10, dependency_age_days: int = 10) -> None:
        archive = sdist(["backend>=2"])
        self.project("gamma", "1.1.0", [distribution("gamma-1.1.0.tar.gz", age_days, hashlib.sha256(archive).hexdigest()),
                                        distribution("gamma-1.1.0-cp310-cp310-manylinux_2_41_x86_64.whl", age_days, "0" * 64)])
        self.upstream.assets["https://files.example/gamma-1.1.0.tar.gz"] = archive
        self.project("delta", "3.0", [
            distribution("delta-3.0.tar.gz", dependency_age_days, "1" * 64),
            distribution("delta-3.0-cp312-cp312-manylinux_2_17_x86_64.manylinux2014_x86_64.whl", dependency_age_days, "2" * 64),
            distribution("delta-3.0-cp314-cp314-win_amd64.whl", dependency_age_days, "3" * 64),
            distribution("delta-3.0-cp38-abi3-manylinux_2_28_x86_64.whl", dependency_age_days, "4" * 64),
            distribution("delta-3.0-cp311-cp311-manylinux_2_17_x86_64.whl", dependency_age_days, "5" * 64),
            distribution("delta-3.0-cp312-cp312-musllinux_1_2_x86_64.whl", dependency_age_days, "6" * 64),
            distribution("delta-3.0-cp312-cp312-macosx_11_0_arm64.whl", dependency_age_days, "7" * 64),
            distribution("delta-3.0-pp310-pypy310_pp73-manylinux_2_17_x86_64.whl", dependency_age_days, "8" * 64),
            distribution("delta-3.0-py3-none-any.whl", dependency_age_days, "9" * 64),
            distribution("delta-3.0-py3-none-manylinux_2_12_x86_64.manylinux2010_x86_64.musllinux_1_1_x86_64.whl", dependency_age_days, "b" * 64),
        ])
        self.project("backend", "2.1.0", [distribution("backend-2.1.0-py3-none-any.whl", 10, "a" * 64)])

    def test_a_newer_release_locks_the_resolved_closure_with_every_supported_sha256(self) -> None:
        self.gamma_release()
        result = self.plan()
        self.assertFalse(result.failed, result.failures)
        self.assertEqual(["gamma"], [proposal.tool for proposal in result.proposals])
        cutoff = NOW - timedelta(days=7)
        self.assertEqual([(["gamma[extra]==1.1.0"], cutoff), (["backend>=2"], cutoff)], self.resolutions)
        gamma = result.pins["python"]["gamma"]
        self.assertEqual(("gamma[extra]", "1.1.0", ["backend"]), (gamma["requirement"], gamma["version"], gamma["build"]))
        self.assertEqual({"backend", "delta", "gamma"}, set(gamma["packages"]))
        self.assertEqual({"version": "3.0", "sha256": ["1" * 64, "2" * 64, "3" * 64, "4" * 64, "9" * 64, "b" * 64]}, gamma["packages"]["delta"],
                         "the sdist and the wheels for linux-x64 and windows-x64 from CPython 3.12 on, a compressed platform tag set "
                         "that names one of them included; not 3.11, musl, macOS or PyPy")
        self.assertEqual([hashlib.sha256(sdist(["backend>=2"])).hexdigest()], gamma["packages"]["gamma"]["sha256"])
        self.assertEqual({"version": "2.1.0", "sha256": ["a" * 64]}, gamma["packages"]["backend"])
        pins_module.validate_pins(result.pins)
        self.assertIn("| gamma | 1.0.0 | 1.1.0 | PyPI sha256 of every supported distribution of the closure that pip resolved |", update_pins.render_summary(result))

    def test_a_release_younger_than_the_cooldown_proposes_nothing(self) -> None:
        self.gamma_release(age_days=3)
        result = self.plan()
        self.assertFalse(result.changed)
        self.assertIn("gamma 1.0.0", result.current)
        self.assertEqual([], self.resolutions)

    def test_a_dependency_younger_than_the_cooldown_leaves_the_pin_unchanged(self) -> None:
        self.gamma_release(dependency_age_days=2)
        result = self.plan()
        self.assertFalse(result.changed)
        self.assertEqual(self.pins()["python"], result.pins["python"])
        self.assertTrue(any(item.startswith("gamma:") and "delta 3.0 is younger than 7 days" in item for item in result.unreachable), result.unreachable)

    def test_a_resolution_without_the_tool_at_its_new_version_fails(self) -> None:
        self.gamma_release()
        self.resolved = {"gamma": "1.0.9", "delta": "3.0"}
        result = self.plan()
        self.assertTrue(result.failed)
        self.assertIn("gamma 1.1.0: pip resolved gamma 1.0.9", result.failures[0])

    def test_an_unreachable_index_leaves_the_pin_unchanged(self) -> None:
        self.gamma_release()
        self.upstream.unreachable.add(update_pins.PYPI_URL.format(name="gamma"))
        result = self.plan()
        self.assertFalse(result.changed)
        self.assertTrue(any(item.startswith("gamma:") for item in result.unreachable), result.unreachable)


class WorkflowInvocationTests(unittest.TestCase):
    def test_the_updater_workflow_invokes_the_script_with_options_it_accepts(self) -> None:
        invocations = script_invocations(REPOSITORY_ROOT / ".github" / "workflows" / "update-ci-pins.yml", "update_pins.py")
        self.assertEqual(len(invocations), 2, "the plan step and the apply step")
        parsed = [update_pins.build_parser().parse_args(tokens) for tokens in invocations]
        self.assertEqual([arguments.apply for arguments in parsed], [False, True])
        self.assertTrue(all(arguments.cooldown_days == 7 and arguments.output and arguments.summary for arguments in parsed))


class CommandLineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.pins_path = self.root / "actions" / "repository-hygiene" / "pins.json"
        self.pins_path.parent.mkdir(parents=True)
        self.pins_path.write_text(json.dumps(pins_document(), indent=2) + "\n", encoding="utf-8")

    def run_cli(self, *arguments: str, fetch_script: str) -> subprocess.CompletedProcess[str]:
        """Run the script with ``update_pins.fetch`` replaced by a mocked upstream, in a child process."""
        driver = self.root / "driver.py"
        driver.write_text(
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('update_pins', {str(SCRIPT)!r})\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "sys.modules[spec.name] = module\n"
            "spec.loader.exec_module(module)\n"
            f"{fetch_script}\n"
            "module.fetch = fetch\n"
            "module.plan.__defaults__ = (fetch, None, module.COOLDOWN_DAYS, module.pip_resolve)\n"
            "sys.exit(module.main(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        return subprocess.run([sys.executable, str(driver), "--pins", str(self.pins_path), *arguments], capture_output=True, text=True, check=False, cwd=self.root)

    NEWER_BETA = (
        "import hashlib, json\n"
        "def fetch(url):\n"
        "    if url.endswith('fixture/beta/releases?per_page=50'):\n"
        "        return json.dumps([{'tag_name': 'v2.1.0', 'draft': False, 'prerelease': False, 'published_at': '2020-01-01T00:00:00Z'}]).encode()\n"
        "    if url.endswith('beta-v2.1.0.tar.xz'):\n"
        "        return b'beta'\n"
        "    if url.endswith('index.json'):\n"
        "        return b'[]'\n"
        "    return b'[]'\n"
    )

    def test_report_only_writes_outputs_and_leaves_the_pins_file_unchanged(self) -> None:
        before = self.pins_path.read_text(encoding="utf-8")
        output = self.root / "output.txt"
        summary = self.root / "summary.md"
        result = self.run_cli("--output", str(output), "--summary", str(summary), fetch_script=self.NEWER_BETA)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual("changed=true\nfailed=false\n", output.read_text(encoding="utf-8"))
        self.assertIn("| beta | 2.0.0 | 2.1.0 |", summary.read_text(encoding="utf-8"))
        self.assertEqual(before, self.pins_path.read_text(encoding="utf-8"))

    def test_apply_writes_the_pins_file_and_the_pull_request_body(self) -> None:
        body = self.root / "body.md"
        result = self.run_cli("--apply", "--pr-body", str(body), fetch_script=self.NEWER_BETA)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        written = json.loads(self.pins_path.read_text(encoding="utf-8"))
        self.assertEqual("2.1.0", written["tools"]["beta"]["version"])
        self.assertEqual(hashlib.sha256(b"beta").hexdigest(), written["tools"]["beta"]["platforms"]["linux-x64"]["sha256"])
        self.assertEqual(pins_document()["tools"]["alpha"], written["tools"]["alpha"])
        self.assertIn("| beta | 2.0.0 | 2.1.0 |", body.read_text(encoding="utf-8"))
        self.assertIn("`actions/repository-hygiene/pins.json`", body.read_text(encoding="utf-8"), "the body names the pins file by its repository path")
        self.assertTrue(self.pins_path.read_text(encoding="utf-8").endswith("}\n"))

    def test_the_cooldown_days_option_reaches_the_summary_and_rejects_a_negative_value(self) -> None:
        summary = self.root / "summary.md"
        nothing = "def fetch(url):\n    return b'[]'\n"
        result = self.run_cli("--cooldown-days", "3", "--summary", str(summary), fetch_script=nothing)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("at least 3 days old", summary.read_text(encoding="utf-8"))
        result = self.run_cli("--cooldown-days", "-1", fetch_script=nothing)
        self.assertEqual(2, result.returncode)
        self.assertIn("--cooldown-days must be 0 or more", result.stderr)

    def test_a_checksum_failure_exits_non_zero_and_writes_nothing(self) -> None:
        before = self.pins_path.read_text(encoding="utf-8")
        output = self.root / "output.txt"
        mismatch = (
            "import json\n"
            "def fetch(url):\n"
            "    if url.endswith('fixture/alpha/releases?per_page=50'):\n"
            "        return json.dumps([{'tag_name': 'v1.3.0', 'draft': False, 'prerelease': False, 'published_at': '2020-01-01T00:00:00Z'}]).encode()\n"
            "    if url.endswith('checksums.txt'):\n"
            "        return ('0' * 64 + '  alpha_1.3.0_linux.tar.gz\\n' + '1' * 64 + '  alpha_1.3.0_windows.zip\\n').encode()\n"
            "    if url.endswith('.tar.gz') or url.endswith('.zip'):\n"
            "        return b'asset'\n"
            "    return b'[]'\n"
        )
        result = self.run_cli("--apply", "--output", str(output), fetch_script=mismatch)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("::error::alpha 1.3.0: alpha_1.3.0_linux.tar.gz downloaded with sha256", result.stderr)
        self.assertEqual("changed=false\nfailed=true\n", output.read_text(encoding="utf-8"))
        self.assertEqual(before, self.pins_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
