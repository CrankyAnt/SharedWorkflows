"""Find dead relative links in tracked Markdown: missing files or folders and missing heading anchors.

    python3 link_check.py [--root DIR] [--exclude PATTERN ...] [--exclude-from FILE|-] [--summary FILE]

Links with a scheme (``https:``, ``mailto:``) and protocol-relative links are not checked. Fenced and indented code
blocks and inline code spans hold no links. An excluded pattern, a glob relative to the repository root, takes its
Markdown out of the check as a source; a link into an excluded file is still followed to the file. Each dead link is one line,
``<file>:<line>: ...``, also written as a GitHub annotation, and the exit code is 1 while any remains.
"""

from __future__ import annotations

import argparse
import bisect
import fnmatch
import html
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
SETEXT = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
THEMATIC_BREAK = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
HTML_ANCHOR = re.compile(r"<a\s+(?:id|name)=[\"']([^\"']+)[\"']")
CODE_SPAN = re.compile(r"(?<!`)(`+)(?!`)([\s\S]*?)(?<!`)\1(?!`)")
BLOCKQUOTE = re.compile(r"^ {0,3}>[ \t]?")
LIST_ITEM = re.compile(r"^ {0,3}(?:[-+*]|[0-9]{1,9}[.)])[ \t]+")
REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[([^\]]+)\]:[ \t]*(<[^>]+>|\S+)")
REFERENCE_START = re.compile(r"^ {0,3}\[([^\]\n]+)\]:[ \t]*(?:\n[ \t]*)?", re.MULTILINE)
LINK_START = re.compile(r"!?\[(?:[^\[\]]|\[[^\[\]]*\])*\]\(")
LINK_END = re.compile(r'''[ \t\n]*(?:(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\((?:\\.|[^)\\])*\))[ \t\n]*)?\)''')
HTML_CODE = re.compile(r"^ {0,3}<(pre|script|style|textarea)(?:\s|>|$)", re.IGNORECASE)
REFERENCE_LINK = re.compile(r"!?\[([^\[\]]+)\](?:\[([^\[\]]*)\])?")
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
ANCHOR_NOISE = re.compile(r"[^\w\- ]", re.UNICODE)


def tracked_markdown(root: Path) -> list[str]:
    """The tracked Markdown files of the repository at ``root``, as repository paths."""
    listing = subprocess.run(["git", "ls-files", "-z", "--", ":(icase)*.md"], cwd=root, capture_output=True, text=True, encoding="utf-8", check=True)
    return [item for item in listing.stdout.split("\0") if item]


def escaped(text: str, position: int) -> bool:
    """An odd run of preceding backslashes escapes a Markdown punctuation character."""
    start = position
    while start > 0 and text[start - 1] == "\\":
        start -= 1
    return (position - start) % 2 == 1


def blank_code_spans(text: str) -> str:
    """Blank exact-length backtick pairs, retaining newlines for source locations."""
    result = list(text)
    position = 0
    while match := CODE_SPAN.search(text, position):
        if escaped(text, match.start()):
            position = match.start() + len(match.group(1))
            continue
        for index in range(match.start(), match.end()):
            if text[index] != "\n":
                result[index] = " "
        position = match.end()
    return "".join(result)


def prose_lines(text: str) -> list[tuple[int, str, str]]:
    """Remove code blocks and container prefixes, preserving source lines and blanking code spans."""
    lines: list[tuple[int, str, str]] = []
    pending: list[tuple[int, str]] = []
    fence: str | None = None
    paragraph = False
    quote_depth = 0
    list_indents: list[int] = []
    html_closing: str | None = None
    source_lines = text.splitlines()

    def flush() -> None:
        joined = "\n".join(line for _, line in pending)
        masked = blank_code_spans(joined).split("\n")
        lines.extend((number, raw, masked[index]) for index, (number, raw) in enumerate(pending))
        pending.clear()

    for number, line in enumerate(source_lines, start=1):
        depth = 0
        while (fence is None or depth < quote_depth) and (quote := BLOCKQUOTE.match(line)):
            depth += 1
            line = line[quote.end():]
        if depth != quote_depth:
            flush()
            list_indents = []
            paragraph = False
            fence = None
            quote_depth = depth
        line = line.expandtabs(4)
        indent = len(line) - len(line.lstrip(" "))
        while list_indents and line.strip() and indent < list_indents[-1]:
            list_indents.pop()
            fence = None
        base_indent = list_indents[-1] if list_indents else 0
        line = line[base_indent:]
        if fence is None and (item := LIST_ITEM.match(line)) and not THEMATIC_BREAK.match(line):
            flush()
            list_indents.append(base_indent + item.end())
            line = line[item.end():]
            paragraph = False
        if fence is None and (html_closing is not None or HTML_CODE.match(line) or "<!--" in line):
            if html_closing is None:
                # Look through this paragraph so a multiline code span can quote an HTML opener; a line of spaces and
                # tabs with a line after it ends the paragraph, and the lines past it are not read.
                head = [raw for _, raw in pending] + [line]
                last = len(head) - 1 + len(source_lines) - number
                probe = head[:1]
                for index in range(1, last + 1):
                    candidate = head[index] if index < len(head) else source_lines[number + index - len(head)]
                    if index < last and not candidate.strip(" \t"):
                        break
                    probe.append(candidate)
                visible = blank_code_spans("\n".join(probe)).split("\n")[len(pending)]
                code = HTML_CODE.match(visible)
                if code:
                    html_closing = f"</{code.group(1).lower()}>"
                elif "<!--" in visible:
                    html_closing = "-->"
            if html_closing is not None:
                pending.append((number, line))
                if html_closing in line.lower():
                    html_closing = None
                    flush()
                paragraph = False
                continue
        opening = FENCE.match(line)
        if fence is not None:
            if (opening and opening.group(1)[0] == fence[0] and len(opening.group(1)) >= len(fence)
                    and not opening.group(2).strip()):
                fence = None
            continue
        if opening and not (opening.group(1)[0] == "`" and "`" in opening.group(2)):
            flush()
            fence = opening.group(1)
            paragraph = False
            continue
        if not paragraph and line.startswith("    "):
            flush()
            continue
        definition = REFERENCE_DEFINITION.match(line) and not paragraph
        if not line.strip() or HEADING.match(line) or definition:
            flush()
        pending.append((number, line))
        paragraph = bool(line.strip()) and not (HEADING.match(line) or SETEXT.match(line)
                                               or THEMATIC_BREAK.match(line) or definition)
        if not paragraph:
            flush()
    flush()
    return without_html_regions(lines)


def without_html_regions(lines: list[tuple[int, str, str]]) -> list[tuple[int, str, str]]:
    """Hide HTML comments and raw HTML code blocks without hiding tags inside inline code."""
    result: list[tuple[int, str, str]] = []
    closing: str | None = None
    block = False
    for number, raw, masked in lines:
        position = 0
        while position < len(raw):
            if closing is None:
                code = HTML_CODE.match(masked) if position == 0 else None
                start = code.start() if code else masked.find("<!--", position)
                if start < 0:
                    break
                block = code is not None or not masked[:start].strip()
                closing = f"</{code.group(1).lower()}>" if code else "-->"
            else:
                start = position
            end = raw.lower().find(closing, start)
            stop = len(raw) if end < 0 or block else end + len(closing)
            raw = raw[:start] + " " * (stop - start) + raw[stop:]
            masked = masked[:start] + " " * (stop - start) + masked[stop:]
            if end >= 0:
                closing = None
            position = stop
        result.append((number, raw, masked))
    return result


def github_anchor(heading: str) -> str:
    """GitHub's anchor for a heading text: lowercase, punctuation dropped, spaces as hyphens."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # a link keeps its text
    text = re.sub(r"</?[A-Za-z][^>]*>", "", text)
    text = re.sub(r"(?<!\w)_{1,2}(.+?)_{1,2}(?!\w)", r"\1", text)
    text = html.unescape(text)
    text = text.replace("`", "").replace("*", "")
    text = ANCHOR_NOISE.sub("", text.strip().lower())
    return text.replace(" ", "-")


def anchors(text: str) -> set[str]:
    """The anchors a Markdown file offers: one per heading in GitHub's form, repeated headings numbered, plus HTML anchors."""
    found: set[str] = set()
    custom: set[str] = set()
    counts: dict[str, int] = {}
    paragraph: list[str] = []
    previous = 0
    for number, line, _ in prose_lines(text):
        if number != previous + 1:
            paragraph = []
        previous = number
        heading = HEADING.match(line)
        title = heading.group(2) if heading else " ".join(paragraph) if paragraph and SETEXT.match(line) else None
        if title is not None:
            base = github_anchor(title)
            seen = counts.get(base, 0)
            candidate = base if seen == 0 else f"{base}-{seen}"
            while candidate in found:
                seen += 1
                candidate = f"{base}-{seen}"
            counts[base] = seen + 1
            found.add(candidate)
        if (heading or SETEXT.match(line) or THEMATIC_BREAK.match(line) or REFERENCE_DEFINITION.match(line)
                or not line.strip()):
            paragraph = []
        else:
            paragraph.append(line.strip())
        for anchor in HTML_ANCHOR.findall(line):
            custom.add(anchor.lower())  # compared with the lowercased fragment, like the heading anchors
    return found | custom


def destination(text: str, position: int) -> tuple[str, int] | None:
    """Read an angle-delimited destination or a destination with balanced parentheses."""
    start = position
    if position < len(text) and text[position] == "<":
        position += 1
        while position < len(text):
            if text[position] == ">" and not escaped(text, position):
                return text[start:position + 1], position + 1
            if text[position] in "\n<":
                return None
            position += 1
        return None
    depth = 0
    while position < len(text):
        character = text[position]
        if not escaped(text, position):
            if character.isspace():
                break
            if character == "(":
                depth += 1
            elif character == ")":
                if depth == 0:
                    break
                depth -= 1
        position += 1
    return (text[start:position], position) if depth == 0 else None


def links(text: str) -> list[tuple[int, str]]:
    """Inline links, images and used references, with the line of each usage."""
    found: list[tuple[int, str]] = []
    lines = prose_lines(text)
    definitions: dict[str, str] = {}
    raw_lines = [""] * len(text.splitlines())
    masked_lines = raw_lines.copy()
    for number, raw, masked in lines:
        raw_lines[number - 1] = raw
        masked_lines[number - 1] = masked
    raw_text = "\n".join(raw_lines)
    masked_text = "\n".join(masked_lines)
    pending: list[tuple[int, int]] = []

    def blank(start: int, end: int) -> None:
        """Queue a range for blanking; the queued ranges are ordered and disjoint."""
        pending.append((start, end))

    def rebuild() -> None:
        """Blank the queued ranges in one rebuild of the text, retaining newlines."""
        nonlocal masked_text
        pieces: list[str] = []
        previous = 0
        for start, end in pending:
            pieces += [masked_text[previous:start], re.sub(r"[^\n]", " ", masked_text[start:end])]
            previous = end
        masked_text = "".join(pieces) + masked_text[previous:]
        pending.clear()

    def reach(position: int) -> None:
        """Rebuild before a read from ``position`` when a queued range ends past it. Reads look forward, and the backward
        look for escapes ends at a queued inline link's closing parenthesis, so a range ending before ``position`` cannot
        change a read."""
        if pending and position < pending[-1][1]:
            rebuild()

    def label(value: str) -> str:
        return " ".join(value.split()).casefold()

    for definition in REFERENCE_START.finditer(raw_text):
        reach(definition.start())
        if not masked_text[definition.start():definition.end()].strip():
            continue
        parsed = destination(raw_text, definition.end())
        if parsed is None or not parsed[0]:
            continue
        target, end = parsed
        line_end = raw_text.find("\n", end)
        if line_end < 0:
            line_end = len(raw_text)
        definitions.setdefault(label(definition.group(1)), target)
        blank(definition.start(), line_end)
    rebuild()
    for match in list(LINK_START.finditer(masked_text)):
        reach(match.start())
        if escaped(masked_text, match.start()):
            continue
        start = match.end()
        while start < len(masked_text) and masked_text[start] in " \t\n":
            start += 1
        parsed = destination(masked_text, start)
        if parsed is None:
            continue
        target, end = parsed
        closing = LINK_END.match(masked_text, end)
        if closing is None:
            continue
        if re.search(r"\n[ \t]*\n", masked_text[match.start():closing.end()]):
            continue
        found.append((match.start(), target))
        blank(match.start(), closing.end())
    rebuild()
    for match in REFERENCE_LINK.finditer(masked_text):
        if escaped(masked_text, match.start()):
            continue
        target = definitions.get(label(match.group(2) or match.group(1)))
        if target is not None:
            found.append((match.start(), target))
    newlines = [index for index, character in enumerate(masked_text) if character == "\n"]
    return [(bisect.bisect_left(newlines, start) + 1, target) for start, target in sorted(found)]


def excluded(relative: str, patterns: list[str]) -> bool:
    """Whether a repository path matches an exclusion: a glob anchored at the repository root (``**`` spans directories),
    or a directory pattern that ends in ``/`` and covers everything under it."""
    path = PurePosixPath(relative)
    for pattern in patterns:
        pattern = pattern.strip().replace("\\", "/").removeprefix("./")
        if not pattern:
            continue
        if pattern.endswith("/") and (relative + "/").startswith(pattern):
            return True
        if glob_match(PurePosixPath(pattern).parts, path.parts):
            return True
    return False


def glob_match(pattern: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    """Whether path segments match pattern segments as a whole, as ``PurePath.full_match`` of Python 3.13 matches them:
    ``**`` spans zero or more segments, and one or more as the last segment after another one; every other segment
    matches one segment with ``*``, ``?`` and ``[...]``, case-sensitively."""
    if pattern and all(segment == "**" for segment in pattern):
        return True
    return segments_match(pattern, parts)


def segments_match(pattern: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    if not pattern:
        return not parts
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        if not rest:
            return bool(parts)
        return any(segments_match(rest, parts[index:]) for index in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], head) and segments_match(rest, parts[1:])


def plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def annotation(file: str, line: str, message: str) -> str:
    """A GitHub error annotation; the characters the workflow-command syntax reserves are percent-encoded."""
    def property_value(text: str) -> str:
        return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A").replace(":", "%3A").replace(",", "%2C")
    data = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return f"::error file={property_value(file)},line={property_value(line)}::{data}"


def link_problems(root: Path, files: list[str]) -> list[str]:
    """Each relative link in ``files`` whose target file or heading does not exist, as ``<file>:<line>: ...``."""
    problems: list[str] = []
    anchor_cache: dict[Path, set[str]] = {}

    def anchors_of(path: Path) -> set[str]:
        if path not in anchor_cache:
            anchor_cache[path] = anchors(path.read_text(encoding="utf-8"))
        return anchor_cache[path]

    for relative in files:
        source = root / relative
        for number, raw in links(source.read_text(encoding="utf-8")):
            target = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
            if SCHEME.match(target) or target.startswith("//"):
                continue
            location, _, fragment = target.partition("#")
            location = unquote(location.partition("?")[0])
            if location:
                resolved = Path(os.path.normpath((root / location.lstrip("/")) if location.startswith("/") else (source.parent / location)))
                if not resolved.exists():
                    problems.append(f"{relative}:{number}: the link target {target} does not exist")
                    continue
            else:
                resolved = source
            if fragment and resolved.suffix.lower() == ".md" and resolved.is_file():
                if unquote(fragment).lower() not in anchors_of(resolved):
                    shown = resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else str(resolved)
                    problems.append(f"{relative}:{number}: the link target {target} names no heading of {shown}")
    return problems


def read_exclusions(patterns: list[str], exclude_from: str | None) -> list[str]:
    """The exclusion patterns of the command line and of the file (``-`` for standard input), one per line."""
    collected = list(patterns)
    if exclude_from:
        text = sys.stdin.read() if exclude_from == "-" else Path(exclude_from).read_text(encoding="utf-8")
        collected += [line.strip() for line in text.splitlines() if line.strip()]
    return collected


def check(root: Path, exclusions: list[str], summary: Path | None = None) -> int:
    """Run the check over the tracked Markdown of ``root`` and report; 0 when every link resolves."""
    files = tracked_markdown(root)
    checked = [relative for relative in files if not excluded(relative, exclusions)]
    problems = link_problems(root, checked)
    for problem in problems:
        location, _, message = problem.partition(": ")
        file, _, line = location.rpartition(":")
        print(annotation(file, line, message))
        print(problem)
    lines = ["## Relative links in tracked Markdown", "",
             f"{plural(len(checked), 'Markdown file')} checked" + (f", {len(files) - len(checked)} excluded" if len(files) != len(checked) else "") + ".", ""]
    if problems:
        lines += [f"{plural(len(problems), 'dead link')}:", ""] + [f"- `{problem}`" for problem in problems]
    else:
        lines.append("Every relative link has an existing target and, when specified, heading.")
    if summary is not None:
        with summary.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    print(f"{plural(len(checked), 'Markdown file')} checked, {plural(len(problems), 'dead link')}.")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--exclude", action="append", default=[], help="a glob of Markdown files not checked as sources; repeatable")
    parser.add_argument("--exclude-from", help="a file with one such glob per line, or - for standard input")
    parser.add_argument("--summary", type=Path, help="file to append the Markdown summary to, such as GITHUB_STEP_SUMMARY")
    arguments = parser.parse_args(argv)
    return check(arguments.root.resolve(), read_exclusions(arguments.exclude, arguments.exclude_from), arguments.summary)


if __name__ == "__main__":
    raise SystemExit(main())
