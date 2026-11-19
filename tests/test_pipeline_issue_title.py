"""Issue-title normalisation on fixture events, a fixture category file and fixture component folders: the title that
the program gives an opened issue, and the one request it makes to a stand-in for the GitHub API."""

from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from tests import temporary_support


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPOSITORY_ROOT / "actions" / "issue-title" / "issue_title.py"
REPOSITORY = "example-owner/example-repository"
# An invented category file: three component categories, each with a folder of components that a marker file names, and
# two generic forms. It also holds keys that the normalisation does not read, as a repository's file may.
CATEGORIES = {
    "product_categories": [
        {"key": "widget", "name": "Widget", "source_directory": "widgets", "marker": "WIDGET.md", "title_prefix": "[widget]",
         "dynamic_title_prefix": "[widget: {component}]", "component_label": "Widget", "required_metadata": ["name"],
         "labels": ["area:widget"], "form_file": "01_widget.yml"},
        {"key": "gadget", "name": "Gadget", "source_directory": "gadgets", "marker": "GADGET.md", "title_prefix": "[gadget]",
         "dynamic_title_prefix": "[gadget: {component}]", "component_label": "Gadget",
         "required_metadata": ["name", "description", "entrypoint"], "labels": ["area:gadget"]},
        {"key": "routine", "name": "Routine", "source_directory": "routines", "marker": "ROUTINE.md", "title_prefix": "[routine]",
         "dynamic_title_prefix": "[routine: {component}]", "component_label": "Routine", "required_metadata": ["name", "description"],
         "labels": ["area:routine"]},
    ],
    "generic_forms": [
        {"key": "suggestion", "name": "Suggestion", "title_prefix": "[suggestion]", "labels": []},
        {"key": "question", "name": "Question", "title_prefix": "[question]", "labels": ["type:question"]},
    ],
    "blank_issues_enabled": False,
}
HEADINGS = {"area:widget": "Widget", "area:gadget": "Gadget"}


def write_marker(path: Path, **values: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(["---", *[f"{key}: {value}" for key, value in values.items()], "---", ""]), encoding="utf-8")


def create_fixture(root: Path, categories: dict | None = None) -> Path:
    """A fixture repository with the components beta and Alpha, link-checker and nightly; the path of its category file."""
    write_marker(root / "widgets" / "beta" / "WIDGET.md", name="beta")
    write_marker(root / "widgets" / "Alpha" / "WIDGET.md", name="Alpha")
    write_marker(root / "gadgets" / "link-checker" / "GADGET.md", name="link-checker", description="Check links.", entrypoint="check-links.py")
    (root / "gadgets" / "link-checker" / "check-links.py").write_text("# fixture entrypoint\n", encoding="utf-8")
    write_marker(root / "routines" / "nightly" / "ROUTINE.md", name="nightly", description="Run every night.")
    path = root / "intake" / "categories.json"
    path.parent.mkdir()
    path.write_text(json.dumps(CATEGORIES if categories is None else categories), encoding="utf-8")
    return path


def opened_event(title: str, *, component: str | None = None, labels: list[str] | None = None, heading: str | None = None) -> dict:
    """The event of an opened issue; with a component, its body holds the component section of an issue form."""
    body = "### Description\nUseful evidence"
    if component is not None:
        heading = heading or next((HEADINGS[label] for label in labels or [] if label in HEADINGS), "Component")
        body = f"### {heading}\n{component}\n\n{body}"
    return {"action": "opened", "issue": {"number": 12, "title": title, "body": body, "labels": [{"name": label} for label in labels or []]},
            "repository": {"full_name": REPOSITORY}}


class IssueTitleFixture(unittest.TestCase):
    def setUp(self) -> None:
        temporary = temporary_support.Directory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.categories = create_fixture(self.root)

    def run_program(self, event: dict, *arguments: str, environment: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        event_path = self.root / "event.json"
        event_path.write_text(json.dumps(event), encoding="utf-8")
        environment = dict(os.environ if environment is None else environment)
        environment["PYTHONPYCACHEPREFIX"] = temporary_support.bytecode_cache()
        return subprocess.run([sys.executable, str(SCRIPT), "--event", str(event_path), "--root", str(self.root), "--categories", str(self.categories), *arguments],
                              cwd=self.root, env=environment, capture_output=True, text=True, encoding="utf-8", check=False)

    def title(self, text: str, **event: object) -> str:
        result = self.run_program(opened_event(text, **event))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result.stdout.strip()


class NormalisedTitleTests(IssueTitleFixture):
    def test_a_component_form_gets_the_prefix_of_its_canonical_component_or_the_unsure_prefix(self) -> None:
        self.assertEqual("[widget: beta] Stops early", self.title("Stops early", component="beta", labels=["area:widget"]))
        self.assertEqual("[gadget: link-checker] Source moved", self.title("Source moved", component="link-checker", labels=["area:gadget"]))
        self.assertEqual("[gadget: unsure] Hard to classify", self.title("Hard to classify", component="Not sure", labels=["area:gadget"]))
        self.assertEqual("[widget: beta] Keep  spaces!?  Exactly", self.title("Keep  spaces!?  Exactly", component="beta", labels=["area:widget"]))
        self.assertEqual("[widget: Alpha] Other case", self.title("Other case", component="alpha", labels=["area:widget"]))
        self.assertEqual("[widget: beta] Heading identifies category", self.title("Heading identifies category", component="beta", heading="Widget"))

    def test_a_component_that_the_repository_does_not_hold_gets_the_unsure_prefix_with_a_warning(self) -> None:
        result = self.run_program(opened_event("Stale choice", component="removed-widget", labels=["area:widget"]))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("[widget: unsure] Stale choice", result.stdout.strip())
        self.assertIn("invalid or stale component", result.stderr)

    def test_a_generic_prefix_is_canonical_and_a_normalised_title_stays(self) -> None:
        self.assertEqual("[suggestion] Improve docs", self.title("[Suggestion] Improve docs"))
        self.assertEqual("[question] Where is this?", self.title("Where is this?", labels=["type:question"]))
        self.assertEqual("unchanged", self.title("[widget: beta] Stops early", component="beta", labels=["area:widget"]))
        self.assertEqual("unchanged", self.title("[gadget: unsure] Hard to classify", component="Not sure", labels=["area:gadget"]))
        self.assertEqual("unchanged", self.title("No category at all"))

    def test_an_event_other_than_an_opened_issue_changes_nothing(self) -> None:
        edited = opened_event("No change", component="beta", labels=["area:widget"])
        edited["action"] = "edited"
        result = self.run_program(edited)
        self.assertEqual((0, "not opened"), (result.returncode, result.stdout.strip()))

    def test_a_valid_prefix_in_the_title_is_kept_when_the_body_has_no_component_section(self) -> None:
        result = self.run_program(opened_event("[widget: beta] Stops early", labels=["area:widget"]))
        self.assertEqual((0, "unchanged", ""), (result.returncode, result.stdout.strip(), result.stderr.strip()))
        self.assertEqual("unchanged", self.title("[gadget: link-checker] Source moved", labels=["area:gadget"]))

    def test_a_stale_prefix_in_the_title_without_a_component_section_becomes_the_unsure_prefix(self) -> None:
        stale = self.run_program(opened_event("[widget: removed-widget] Stale choice", labels=["area:widget"]))
        self.assertEqual("[widget: unsure] Stale choice", stale.stdout.strip())
        self.assertIn("invalid or stale component", stale.stderr)
        other_category = self.run_program(opened_event("[gadget: link-checker] Wrong category", labels=["area:widget"]))
        self.assertEqual("[widget: unsure] Wrong category", other_category.stdout.strip())
        self.assertIn("invalid or stale component", other_category.stderr)

    def test_only_the_leading_stale_prefix_is_replaced(self) -> None:
        self.assertEqual("[widget: unsure] Stale choice [widget: removed-widget] later",
                         self.title("Stale choice [widget: removed-widget] later", labels=["area:widget"]))
        self.assertEqual("[widget: unsure] Stale [widget: also-removed] choice",
                         self.title("[widget: removed-widget] Stale [widget: also-removed] choice", labels=["area:widget"]))
        self.assertEqual("[widget: unsure] [widget:removed-widget] No space", self.title("[widget:removed-widget] No space", labels=["area:widget"]))

    def test_the_component_section_decides_over_a_prefix_in_the_title(self) -> None:
        stale = self.run_program(opened_event("[widget: removed-widget] Stale title", component="beta", labels=["area:widget"]))
        self.assertEqual((0, "[widget: beta] Stale title", ""), (stale.returncode, stale.stdout.strip(), stale.stderr.strip()))
        self.assertEqual("[widget: beta] Section decides", self.title("[widget: Alpha] Section decides", component="beta", labels=["area:widget"]))
        self.assertEqual("[widget: unsure] Reporter chose not sure", self.title("[widget: beta] Reporter chose not sure", component="Not sure", labels=["area:widget"]))

    def test_a_title_that_is_only_a_prefix_stays_that_prefix(self) -> None:
        self.assertEqual("[question]", self.title("[Question]"))


class CategoryFileTests(IssueTitleFixture):
    def failure(self, event: dict) -> str:
        result = self.run_program(event)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result.stderr

    def test_a_missing_category_file_fails_with_its_path(self) -> None:
        self.categories.unlink()
        self.assertIn("categories.json", self.failure(opened_event("Stops early")))

    def test_a_category_file_without_a_required_key_fails_and_names_the_key(self) -> None:
        broken = json.loads(json.dumps(CATEGORIES))
        del broken["product_categories"][0]["marker"]
        self.categories.write_text(json.dumps(broken), encoding="utf-8")
        self.assertIn("product_categories[1] is missing: marker", self.failure(opened_event("Stops early")))

    def test_a_dynamic_prefix_without_the_component_placeholder_fails(self) -> None:
        broken = json.loads(json.dumps(CATEGORIES))
        broken["product_categories"][0]["dynamic_title_prefix"] = "[widget]"
        self.categories.write_text(json.dumps(broken), encoding="utf-8")
        self.assertIn("dynamic_title_prefix must contain {component}", self.failure(opened_event("Stops early")))

    def test_a_marker_without_its_required_metadata_fails_and_names_the_marker(self) -> None:
        write_marker(self.root / "gadgets" / "broken" / "GADGET.md", name="broken")
        message = self.failure(opened_event("Stops early"))
        self.assertIn("GADGET.md", message)
        self.assertIn("missing marker metadata: description, entrypoint", message)

    def test_a_marker_whose_name_differs_from_its_folder_fails(self) -> None:
        write_marker(self.root / "widgets" / "gamma" / "WIDGET.md", name="delta")
        self.assertIn("does not match directory 'gamma'", self.failure(opened_event("Stops early")))

    def test_a_marker_path_that_leaves_its_component_folder_fails(self) -> None:
        broken = json.loads(json.dumps(CATEGORIES))
        broken["product_categories"][0]["marker"] = "../WIDGET.md"
        self.categories.write_text(json.dumps(broken), encoding="utf-8")
        self.assertIn("outside product directory", self.failure(opened_event("Stops early")))

    def test_a_folder_without_a_marker_is_no_component(self) -> None:
        (self.root / "widgets" / "notes").mkdir()
        self.assertEqual("[widget: unsure] Stops early", self.run_program(opened_event("Stops early", component="notes", labels=["area:widget"])).stdout.strip())


class ApplyTests(IssueTitleFixture):
    def setUp(self) -> None:
        super().setUp()
        self.requests: list[dict[str, object]] = []
        self.status = 200
        tests = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_PATCH(self) -> None:  # noqa: N802 - the handler contract of http.server
                length = int(self.headers.get("Content-Length", "0"))
                tests.requests.append({"path": self.path, "authorization": self.headers.get("Authorization"), "body": json.loads(self.rfile.read(length))})
                self.send_response(tests.status)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *arguments: object) -> None:
                return

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)

    def apply(self, event: dict, token: str | None = "token-for-the-test") -> subprocess.CompletedProcess[str]:
        environment = {name: value for name, value in os.environ.items() if name != "GITHUB_TOKEN"}
        if token is not None:
            environment["GITHUB_TOKEN"] = token
        environment["GITHUB_API_URL"] = f"http://127.0.0.1:{self.server.server_port}"
        return self.run_program(event, "--apply", environment=environment)

    def test_apply_makes_one_request_that_changes_only_the_title_of_the_opened_issue(self) -> None:
        result = self.apply(opened_event("Stops early", component="beta", labels=["area:widget"]))
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertEqual([{"path": f"/repos/{REPOSITORY}/issues/12", "authorization": "Bearer token-for-the-test", "body": {"title": "[widget: beta] Stops early"}}],
                         self.requests)

    def test_apply_makes_no_request_for_a_title_that_stays(self) -> None:
        result = self.apply(opened_event("[widget: beta] Stops early", component="beta", labels=["area:widget"]))
        self.assertEqual((0, "unchanged"), (result.returncode, result.stdout.strip()))
        self.assertEqual([], self.requests)

    def test_apply_without_a_token_fails_and_makes_no_request(self) -> None:
        result = self.apply(opened_event("Stops early", component="beta", labels=["area:widget"]), token=None)
        self.assertEqual(1, result.returncode)
        self.assertIn("GITHUB_TOKEN is not set", result.stdout)
        self.assertEqual([], self.requests)

    def test_apply_fails_when_github_refuses_the_request(self) -> None:
        self.status = 403
        result = self.apply(opened_event("Stops early", component="beta", labels=["area:widget"]))
        self.assertEqual(1, result.returncode)
        self.assertIn("failed to update issue title: 403", result.stdout)


if __name__ == "__main__":
    unittest.main()
