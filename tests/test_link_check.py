"""The link check of the repository hygiene: ``link_check.py`` and ``hygiene.py links`` on fixture Markdown.

The seams are ``link_problems`` on fixture files, the exclusion rule, and the command line as the hygiene workflow
invokes it. The repository's own tracked Markdown is checked by its own hygiene run, not here.
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
RESOURCES = REPOSITORY_ROOT / "actions" / "repository-hygiene"


def load_module(name: str):
    spec = importlib.util.spec_from_file_location(name, RESOURCES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


link_check = load_module("link_check")


class FixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.write("docs/guide.md", """\
            # Guide

            ## Install steps

            Text with a [link back](../README.md#overview).

            ## The `ci.md` template

            ### Install steps

            ## Promote v1 by hand (manual)
            """)
        self.write("docs/notes.txt", "notes\n")
        self.write("docs/read me.txt", "notes\n")

    def write(self, relative: str, text: str) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(text), encoding="utf-8")

    def problems(self, *files: str) -> list[str]:
        return link_check.link_problems(self.root, list(files))

    def test_a_link_to_a_missing_file_fails_naming_the_file_the_line_and_the_target(self) -> None:
        self.write("README.md", "# Overview\n\nSee the [guide](docs/guide.md) and the [plan](docs/plan.md).\n")
        self.assertEqual(["README.md:3: the link target docs/plan.md does not exist"], self.problems("README.md", "docs/guide.md"))

    def test_a_link_to_a_missing_heading_fails_naming_the_file_the_line_and_the_target(self) -> None:
        self.write("README.md", "# Overview\n\n[steps](docs/guide.md#install-steps)\n\n[nowhere](docs/guide.md#nowhere)\n\n[here](#missing-here)\n")
        self.assertEqual(["README.md:5: the link target docs/guide.md#nowhere names no heading of docs/guide.md",
                          "README.md:7: the link target #missing-here names no heading of README.md"],
                         self.problems("README.md", "docs/guide.md"))

    def test_existing_folders_pass_and_missing_folders_fail(self) -> None:
        self.write("README.md", "[folder](docs/)\n[missing](absent/)\n")
        self.assertEqual(["README.md:2: the link target absent/ does not exist"], self.problems("README.md"))

    def test_heading_anchors_remove_formatting_and_avoid_all_prior_collisions(self) -> None:
        self.write("docs/headings.md", "# _Formatted_ <em>heading</em>\n# Same\n# Same-1\n# Same\n")
        self.write("README.md", "[format](docs/headings.md#formatted-heading)\n[third](docs/headings.md#same-2)\n")
        self.assertEqual([], self.problems("README.md"))

    def test_resolving_links_pass_in_every_form_the_check_reads(self) -> None:
        self.write("README.md", """\
            # Overview

            [steps](docs/guide.md#install-steps), [code in the heading](docs/guide.md#the-cimd-template),
            [a repeated heading](docs/guide.md#install-steps-1), [punctuation](docs/guide.md#promote-v1-by-hand-manual),
            [root-relative](/docs/guide.md), [a folder](docs/), [a text file](docs/notes.txt),
            [an encoded space](docs/read%20me.txt), [a title](docs/guide.md "Guide") and ![an image](docs/notes.txt) resolve; so does a
            [reference][guide] and a link to [this file](#overview).

            [guide]: docs/guide.md#install-steps

            External links are not checked: [site](https://example.com/missing.md), [mail](mailto:a@example.invalid).

            ```markdown
            A fence holds [a broken link](nothing.md) that the check leaves alone.
            ```

            Inline code too: `[broken](nothing.md)` and ``[also](nowhere.md)``.
            """)
        self.assertEqual([], self.problems("README.md", "docs/guide.md"))

    def test_angle_bracket_destinations_with_spaces_pass_inline_and_by_reference(self) -> None:
        self.write("README.md", """\
            [inline](<docs/read me.txt>)
            [reference][spaced]

            [spaced]: <docs/read me.txt>
            """)
        self.assertEqual([], self.problems("README.md"))

    def test_a_heading_inside_a_fence_is_no_heading(self) -> None:
        self.write("docs/fenced.md", "# Fenced\n\n```text\n## Not a heading\n```\n")
        self.write("README.md", "[no](docs/fenced.md#not-a-heading)\n")
        self.assertEqual(["README.md:1: the link target docs/fenced.md#not-a-heading names no heading of docs/fenced.md"],
                         self.problems("README.md", "docs/fenced.md"))

    def test_fences_require_the_same_character_sufficient_length_and_no_closing_info(self) -> None:
        for marker in ("`", "~"):
            with self.subTest(marker=marker):
                self.write("README.md", marker * 4 + "\n" + marker * 3 + "\n[short](short.md)\n"
                           + marker * 4 + " trailing\n[info](info.md)\n" + marker * 5 + "\n[real](real.md)\n")
                self.assertEqual(["README.md:7: the link target real.md does not exist"], self.problems("README.md"))

    def test_indented_code_is_ignored_but_indented_paragraph_continuation_is_checked(self) -> None:
        self.write("README.md", "    [code](code.md)\n\n\t[tab](tab.md)\n\nParagraph\n    [real](real.md)\n")
        self.assertEqual(["README.md:6: the link target real.md does not exist"], self.problems("README.md"))

    def test_reference_links_use_first_definition_and_report_each_usage_line(self) -> None:
        self.write("README.md", "[unused]: unused.md\n[Used Label]: missing.md\n[used label]: docs/guide.md\n"
                   "\n[full][USED   LABEL]\n[Used Label][]\n[Used Label]\n![Used Label]\n"
                   "`[Used Label]`\n[inline](docs/guide.md)\n[inline]: unused.md\n")
        self.assertEqual([f"README.md:{line}: the link target missing.md does not exist" for line in (5, 6, 7, 8)],
                         self.problems("README.md"))

    def test_setext_headings_have_anchors_but_thematic_breaks_do_not(self) -> None:
        self.write("docs/setext.md", "Title\n=====\n\nInstall\nsteps\n-----\n\n---\n\n    Not a heading\n    ====\n")
        self.write("README.md", "[title](docs/setext.md#title)\n[steps](docs/setext.md#install-steps)\n"
                   "[code](docs/setext.md#not-a-heading)\n")
        self.assertEqual(["README.md:3: the link target docs/setext.md#not-a-heading names no heading of docs/setext.md"],
                         self.problems("README.md"))

    def test_url_queries_are_not_part_of_the_path_or_fragment(self) -> None:
        self.write("README.md", "# Overview\n[query](docs/guide.md?plain=1#install-steps)\n"
                   "[self](?plain=1#overview)\n[dead](docs/gone.md?raw=1)\n"
                   "[anchor](docs/guide.md?plain=1#gone)\n")
        self.assertEqual(["README.md:4: the link target docs/gone.md?raw=1 does not exist",
                          "README.md:5: the link target docs/guide.md?plain=1#gone names no heading of docs/guide.md"],
                         self.problems("README.md"))

    def test_container_code_is_ignored_and_list_continuation_links_are_checked(self) -> None:
        for source, usage_line in (("> ```\n> [code](gone.md)\n> ```\n[real](real.md)\n", 4),
                                   ("- item\n\n  ```\n  [code](gone.md)\n  ```\n\n  [real](real.md)\n", 7),
                                   ("> - item\n>\n>       [code](gone.md)\n>\n>   [real](real.md)\n", 5),
                                   ("```\n> [code](gone.md)\n```\n[real](real.md)\n", 4),
                                   ("> ```\n> > [code](gone.md)\n> ```\n[real](real.md)\n", 4)):
            with self.subTest(source=source):
                self.write("README.md", source)
                self.assertEqual([f"README.md:{usage_line}: the link target real.md does not exist"],
                                 self.problems("README.md"))

    def test_multiline_code_and_escaped_openers_hold_no_links(self) -> None:
        self.write("README.md", "``code\n[code](gone.md)\n` still code\n``\n\\[literal](gone.md)\n[real](real.md)\n")
        self.assertEqual(["README.md:6: the link target real.md does not exist"], self.problems("README.md"))

    def test_unclosed_inline_code_does_not_hide_links_or_cross_a_paragraph(self) -> None:
        self.write("README.md", "` unmatched\n\n[real](real.md) `\n")
        self.assertEqual(["README.md:3: the link target real.md does not exist"], self.problems("README.md"))

    def test_escaped_backtick_does_not_open_code(self) -> None:
        self.write("README.md", "\\` [real](real.md) `\n")
        self.assertEqual(["README.md:1: the link target real.md does not exist"], self.problems("README.md"))

    def test_multiline_destinations_and_balanced_parentheses_are_checked(self) -> None:
        self.write("README.md", "[inline](\nmissing.md)\n[ref]:\n  reference.md\n\n[ref]\n"
                   "[parentheses](file(a)(b).md)\n[nested](file(a(b)).md)\n")
        self.assertEqual(["README.md:1: the link target missing.md does not exist",
                          "README.md:6: the link target reference.md does not exist",
                          "README.md:7: the link target file(a)(b).md does not exist",
                          "README.md:8: the link target file(a(b)).md does not exist"], self.problems("README.md"))

    def test_html_comments_and_raw_code_blocks_hold_no_links_or_headings(self) -> None:
        self.write("README.md", "Text <!-- [comment](missing.md) --> [real](real.md)\n"
                   "<!--\n# Hidden heading\n[comment](missing.md)\n-->\n"
                   "<pre>\n[code](missing.md)\n</pre>\n"
                   "`<!--` [real](real.md)\n[hidden](#hidden-heading)\n")
        self.assertEqual(["README.md:1: the link target real.md does not exist",
                          "README.md:9: the link target real.md does not exist",
                          "README.md:10: the link target #hidden-heading names no heading of README.md"], self.problems("README.md"))

    def test_custom_anchors_do_not_change_heading_collision_numbering(self) -> None:
        self.write("README.md", '<a name="same"></a>\n\n# Same\n\n[extra](#same-1)\n')
        self.assertEqual(["README.md:5: the link target #same-1 names no heading of README.md"], self.problems("README.md"))

    def test_raw_html_code_markers_cannot_swallow_following_links(self) -> None:
        for opening, closing in (("<pre>", "</pre>"), ("<!--", "-->")):
            with self.subTest(opening=opening):
                self.write("README.md", f"{opening}\n```\n{closing}\n[real](real.md)\n")
                self.assertEqual(["README.md:4: the link target real.md does not exist"], self.problems("README.md"))

    def test_reference_definitions_inside_multiline_code_do_not_create_links(self) -> None:
        self.write("README.md", "`code\n[ref]: missing.md\n`\n\n[ref]\n")
        self.assertEqual([], self.problems("README.md"))

    def test_an_exclusion_takes_a_file_out_as_a_source_by_glob_or_directory_and_keeps_it_as_a_target(self) -> None:
        for pattern in ("vendor/**", "vendor/", "vendor/*.md", "**/upstream.md", "vendor/upstream.md", "./vendor/upstream.md"):
            with self.subTest(pattern=pattern):
                self.assertTrue(link_check.excluded("vendor/upstream.md", [pattern]))
        for pattern in ("docs/", "vendor/other.md", "*.txt", "", "upstream.md", "*.md"):
            with self.subTest(pattern=pattern):
                self.assertFalse(link_check.excluded("vendor/upstream.md", [pattern]), "a glob is anchored at the repository root")
        self.assertTrue(link_check.excluded("README.md", ["*.md"]))
        self.assertTrue(link_check.excluded(".github/PULL_REQUEST_TEMPLATE.md", [".github/"]), "a dot directory keeps its dot")
        self.assertTrue(link_check.excluded("vendor/upstream.md", ["", "  vendor/  "]), "patterns are stripped and blanks ignored")

    def test_an_html_anchor_is_matched_without_regard_to_case_like_a_heading_anchor(self) -> None:
        self.write("docs/anchors.md", '# Anchors\n\n<a id="Setup-Notes"></a>\n')
        self.write("README.md", "[a](docs/anchors.md#Setup-Notes) [b](docs/anchors.md#setup-notes) [c](docs/anchors.md#other)\n")
        self.assertEqual(["README.md:1: the link target docs/anchors.md#other names no heading of docs/anchors.md"], self.problems("README.md", "docs/anchors.md"))

    def split(self, line: int, target: str) -> str:
        return f"README.md:{line}: the link to {target} is split between ] and ( and renders as plain text"

    def test_a_link_split_across_a_line_break_fails_naming_the_file_and_the_line_of_the_bracket(self) -> None:
        # GitHub renders each of these as text: a link text and a parenthesis apart, whether the target exists or not.
        for source, line in (("See [the guide]\n(missing.md) here.\n", 1), ("See [the guide]\n(docs/guide.md) here.\n", 1),
                             ("Intro\nSee [the\nguide]\n(docs/guide.md \"Guide\") here.\n", 2),
                             ("See [the guide]  \n  (docs/guide.md) here.\n", 1), ("See [the guide]\t\n\t(docs/guide.md)\n", 1),
                             # A lazy continuation line: the parenthesis continues the paragraph of the block quote.
                             ("> See [the guide]\n(docs/guide.md) here.\n", 1)):
            with self.subTest(source=source):
                self.write("README.md", source)
                target = "missing.md" if "missing" in source else "docs/guide.md"
                self.assertEqual([self.split(line, target)], self.problems("README.md"))

    def test_a_link_with_spaces_or_tabs_between_the_bracket_and_the_parenthesis_fails_the_same_way(self) -> None:
        for source in ("See [the guide] (docs/guide.md) here.\n", "See [the guide]\t(docs/guide.md) here.\n",
                       "See [the guide] \t (docs/guide.md) here.\n"):
            with self.subTest(source=source):
                self.write("README.md", "# Overview\n\n" + source)
                self.assertEqual([self.split(3, "docs/guide.md")], self.problems("README.md"))

    def test_a_split_link_with_a_scheme_and_a_split_image_fail_the_same_way(self) -> None:
        for source, target in (("[site] (https://example.com/)", "https://example.com/"), ("[site]\n(https://example.com/)", "https://example.com/"),
                               ("[mail] (mailto:a@example.invalid)", "mailto:a@example.invalid"),
                               ("![an image] (docs/notes.txt)", "docs/notes.txt"), ("![an image]\n(docs/notes.txt)", "docs/notes.txt")):
            with self.subTest(source=source):
                self.write("README.md", f"Text\n{source} here.\n")
                self.assertEqual([self.split(2, target)], self.problems("README.md"))

    def test_a_split_whose_bracket_text_matches_a_reference_definition_is_a_reference_link_and_passes(self) -> None:
        # A shortcut, collapsed or full reference link, followed by text in parentheses.
        self.write("README.md", "[ref]: docs/guide.md\n\nAnd [ref]\n(note) and [Ref] (note). And ![ref] (note).\n"
                   "And [ref][] (note), [text][ref] (note) and [text][ref]\n(note).\n")
        self.assertEqual([], self.problems("README.md"))
        self.write("README.md", "[ref]: docs/guide.md\n\nAnd [text][missing] (note).\n")
        self.assertEqual([self.split(3, "note")], self.problems("README.md"))

    def test_a_split_in_code_or_an_html_comment_or_behind_an_escaped_bracket_passes(self) -> None:
        self.write("README.md", "```text\n[fenced] (gone.md)\n[fenced]\n(gone.md)\n```\n\n    [indented] (gone.md)\n\n"
                   "Inline `[code] (gone.md)` and ``[code]\n(gone.md)`` text.\n\n<!-- [comment] (gone.md) -->\n\n"
                   "<!--\n[comment]\n(gone.md)\n-->\n\nAn escaped \\[literal] (gone.md) and \\[literal]\n(gone.md).\n")
        self.assertEqual([], self.problems("README.md"))

    def test_a_blank_line_between_the_bracket_and_the_parenthesis_passes(self) -> None:
        for source in ("See [the guide]\n\n(missing.md) here.\n", "See [the guide]  \n \t \n(missing.md) here.\n"):
            with self.subTest(source=source):
                self.write("README.md", source)
                self.assertEqual([], self.problems("README.md"))

    def test_a_parenthesis_that_starts_another_block_and_a_task_list_marker_are_no_split_link(self) -> None:
        # GitHub renders the bracket text and the parenthesis in separate blocks here, and a task list marker as a checkbox.
        for source in ("See [the guide]\n- (missing.md) item\n", "See [the guide]\n> (missing.md) quote\n",
                       "# Head [the guide]\n(missing.md) text\n", "- [ ] (optional) task\n- [x] (done) task\n  * [X] (done) task\n"):
            with self.subTest(source=source):
                self.write("README.md", source)
                self.assertEqual([], self.problems("README.md"))
        self.write("README.md", "Text [ ] (missing.md) and\n- item [x] (missing.md)\n")
        self.assertEqual([self.split(1, "missing.md"), self.split(2, "missing.md")], self.problems("README.md"))

    def test_an_annotation_encodes_the_characters_the_workflow_command_syntax_reserves(self) -> None:
        self.assertEqual("::error file=docs/a%2Cb.md,line=3::the link target x%25y does not exist%0Amore",
                         link_check.annotation("docs/a,b.md", "3", "the link target x%y does not exist\nmore"))


class CommandLineTests(unittest.TestCase):
    """``hygiene.py links`` as the workflow invokes it, on a fixture Git repository."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "repo"
        self.root.mkdir()
        (self.root / "README.md").write_text("# Start\n\n[guide](docs/guide.md#use) and [gone](docs/gone.md)\n", encoding="utf-8")
        (self.root / "docs").mkdir()
        (self.root / "docs" / "guide.md").write_text("# Guide\n\n## Use\n", encoding="utf-8")
        (self.root / "vendor").mkdir()
        (self.root / "vendor" / "upstream.md").write_text("[broken](nowhere.md)\n", encoding="utf-8")
        (self.root / "untracked.md").write_text("[broken](nowhere.md)\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "README.md", "docs/guide.md", "vendor/upstream.md"], cwd=self.root, check=True)

    def links(self, *arguments: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(RESOURCES / "hygiene.py"), "links", "--root", str(self.root), *arguments],
                              input=stdin, capture_output=True, text=True, encoding="utf-8", check=False,
                              env={**os.environ, "PYTHONPYCACHEPREFIX": os.environ.get("PYTHONPYCACHEPREFIX", str(Path(tempfile.gettempdir()) / "repo-pycache"))})

    def test_dead_links_in_tracked_markdown_fail_with_annotations_and_a_summary_and_untracked_files_are_not_read(self) -> None:
        summary = Path(self.temporary.name) / "summary.md"
        result = self.links("--summary", str(summary))
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("::error file=README.md,line=3::the link target docs/gone.md does not exist", result.stdout)
        self.assertIn("::error file=vendor/upstream.md,line=1::the link target nowhere.md does not exist", result.stdout)
        self.assertNotIn("untracked.md", result.stdout)
        self.assertIn("3 Markdown files checked, 2 dead links.", result.stdout)
        text = summary.read_text(encoding="utf-8")
        self.assertIn("## Relative links in tracked Markdown", text)
        self.assertIn("- `README.md:3: the link target docs/gone.md does not exist`", text)

    def test_exclusions_from_standard_input_and_options_take_their_sources_out_and_a_clean_repository_passes(self) -> None:
        result = self.links("--exclude-from", "-", stdin="vendor/\n\n")
        self.assertEqual(1, result.returncode)
        self.assertNotIn("vendor/upstream.md", result.stdout)
        self.assertIn("2 Markdown files checked, 1 dead link.", result.stdout)
        (self.root / "README.md").write_text("# Start\n\n[guide](docs/guide.md#use)\n", encoding="utf-8")
        result = self.links("--exclude", "vendor/**")
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("2 Markdown files checked, 0 dead links.", result.stdout)

    def test_a_split_link_fails_with_an_annotation_and_counts_in_the_summary_unless_its_file_is_excluded(self) -> None:
        (self.root / "vendor" / "split.md").write_text("[split]\n(https://example.com/)\n", encoding="utf-8")
        (self.root / "README.md").write_text("# Start\n\n[guide] (docs/guide.md#use) and [gone](docs/gone.md)\n", encoding="utf-8")
        subprocess.run(["git", "add", "vendor/split.md"], cwd=self.root, check=True)
        summary = Path(self.temporary.name) / "summary.md"
        result = self.links("--summary", str(summary))
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn("::error file=README.md,line=3::the link to docs/guide.md#use is split between ] and ( and renders as plain text",
                      result.stdout)
        self.assertIn("vendor/split.md:1: the link to https://example.com/ is split between ] and ( and renders as plain text", result.stdout)
        self.assertIn("4 Markdown files checked, 2 dead links, 2 split links.", result.stdout)
        text = summary.read_text(encoding="utf-8")
        self.assertIn("2 dead links, 2 split links:", text)
        self.assertIn("- `README.md:3: the link to docs/guide.md#use is split between ] and ( and renders as plain text`", text)
        for readme, counts in (("[guide] (docs/guide.md#use) and [gone](docs/gone.md)", "1 dead link, 1 split link"),
                               ("[guide] (docs/guide.md#use)", "0 dead links, 1 split link")):
            with self.subTest(readme=readme):
                (self.root / "README.md").write_text(f"# Start\n\n{readme}\n", encoding="utf-8")
                output = io.StringIO()
                with redirect_stdout(output):
                    code = link_check.main(["--root", str(self.root), "--exclude", "vendor/"])
                self.assertEqual(1, code, "a split link alone fails the check")
                self.assertNotIn("vendor/", output.getvalue())
                self.assertIn(f"2 Markdown files checked, {counts}.", output.getvalue())

    def test_the_module_runs_in_process_with_the_same_result(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = link_check.main(["--root", str(self.root), "--exclude", "vendor/"])
        self.assertEqual(1, code)
        self.assertIn("README.md:3: the link target docs/gone.md does not exist", output.getvalue())

    def test_markdown_extension_is_case_insensitive(self) -> None:
        (self.root / "UPPER.MD").write_text("[broken](missing.md)\n", encoding="utf-8")
        subprocess.run(["git", "add", "UPPER.MD"], cwd=self.root, check=True)
        result = self.links()
        self.assertIn("UPPER.MD:1: the link target missing.md does not exist", result.stdout)


class LargeFileTests(unittest.TestCase):
    """``hygiene.py links`` on one generated file with 25,000 relative links."""

    BLOCK = textwrap.dedent("""\
        ## Section {i}

        See [guide](docs/guide.md#use), [self](#section-{i}), ![image](docs/guide.md) and [ref][r{i}].
        An escaped \\[literal](gone.md) and code `[code](gone.md)` hold no link; [dead {i}](missing-{i}.md) does.
        [multi](
        docs/guide.md) and [anchor](docs/guide.md#absent-{i}) and <!-- [hidden](gone.md) --> [nested [x]](docs/)
        [title](docs/guide.md "Guide") [angle](<docs/guide.md>)

        ```text
        [fenced](gone.md)
        ```

        [r{i}]: docs/guide.md#use

        """)
    BLOCKS = 2_500  # ten links in each block of fourteen lines

    def test_a_file_with_25000_links_reports_every_dead_link_within_a_linear_time_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "docs").mkdir()
            (root / "docs" / "guide.md").write_text("# Guide\n\n## Use\n", encoding="utf-8", newline="\n")
            (root / "large.md").write_text("".join(self.BLOCK.format(i=i) for i in range(self.BLOCKS)),
                                           encoding="utf-8", newline="\n")
            subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
            subprocess.run(["git", "add", "large.md", "docs/guide.md"], cwd=root, check=True)
            started = time.perf_counter()
            result = subprocess.run([sys.executable, str(RESOURCES / "hygiene.py"), "links", "--root", str(root)],
                                    capture_output=True, text=True, encoding="utf-8", check=False,
                                    env={**os.environ, "PYTHONPYCACHEPREFIX": os.environ.get("PYTHONPYCACHEPREFIX", str(Path(tempfile.gettempdir()) / "repo-pycache"))})
            elapsed = time.perf_counter() - started
        expected = []
        for i in range(self.BLOCKS):
            expected += [f"large.md:{14 * i + 4}: the link target missing-{i}.md does not exist",
                         f"large.md:{14 * i + 6}: the link target docs/guide.md#absent-{i} names no heading of docs/guide.md"]
        self.assertEqual(1, result.returncode, result.stderr)
        self.assertEqual(expected, [line for line in result.stdout.splitlines() if line.startswith("large.md:")])
        self.assertIn("2 Markdown files checked, 5000 dead links.", result.stdout)
        self.assertLess(elapsed, 15, "the check takes more than linear time in the file's size")


if __name__ == "__main__":
    unittest.main()
