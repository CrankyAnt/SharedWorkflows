"""Check that every tracked package file has a Dependabot entry for its ecosystem that covers its directory.

    python3 dependabot_coverage.py --root . [--ecosystems package-ecosystems.json] [--exclude PATH ...] [--exclude-from FILE|-]

The file patterns per Dependabot ecosystem live in ``package-ecosystems.json``. A file is covered when an
``updates`` entry for its ecosystem lists the file's directory under ``directory`` or matches it under
``directories``. Each uncovered file is reported with its path and the missing ecosystem, and the exit code is 1.
The Dependabot configuration is read with the YAML subset such files use; an unsupported construct is reported
as an error rather than guessed at.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ECOSYSTEMS_PATH = Path(__file__).resolve().parent / "package-ecosystems.json"
CONFIG_PATHS = (".github/dependabot.yml", ".github/dependabot.yaml")
INTEGER = re.compile(r"^-?(0|[1-9][0-9]*)$")
PLAIN_KEY = re.compile(r"""^([^\s'"\[\]{}#&*!|>%@`,][^:#]*?|'[^']*'|"[^"]*")\s*:(\s+|$)""")


class YamlError(ValueError):
    """The Dependabot configuration uses YAML this check does not read."""


class CoverageError(RuntimeError):
    """The check could not run."""


@dataclass(frozen=True)
class Uncovered:
    path: str
    ecosystem: str
    directory: str

    def message(self) -> str:
        return f"{self.path} has no Dependabot entry for ecosystem {self.ecosystem} that covers directory {self.directory}"


# --- a reader for the YAML subset that Dependabot configurations use ---------------------------------------------


def _strip_comment(line: str, number: int) -> str:
    quote: str | None = None
    for index, character in enumerate(line):
        if quote:
            if character == quote:
                quote = None
        elif character in "'\"":
            quote = character
        elif character == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index].rstrip()
    if quote:
        raise YamlError(f"line {number}: unterminated quoted string")
    return line.rstrip()


def _logical_lines(text: str) -> list[tuple[int, int, str]]:
    lines: list[tuple[int, int, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if raw.startswith("---") or raw.startswith("..."):
            if raw.strip() in ("---", "..."):
                continue
        content = _strip_comment(raw, number)
        if not content.strip():
            continue
        if content.lstrip(" ").startswith("\t"):
            raise YamlError(f"line {number}: tabs are not allowed in indentation")
        indent = len(content) - len(content.lstrip(" "))
        if content.lstrip().startswith("%"):
            continue
        lines.append((number, indent, content.strip()))
    return lines


def _scalar(text: str, number: int) -> object:
    text = text.strip()
    if text == "":
        return None
    if text[0] in "|>&*!":
        raise YamlError(f"line {number}: block scalars, anchors, aliases and tags are not supported")
    if text.startswith("'"):
        if not text.endswith("'") or len(text) < 2:
            raise YamlError(f"line {number}: unterminated single-quoted string")
        return text[1:-1].replace("''", "'")
    if text.startswith('"'):
        if not text.endswith('"') or len(text) < 2:
            raise YamlError(f"line {number}: unterminated double-quoted string")
        try:
            return json.loads(text)
        except ValueError as error:
            raise YamlError(f"line {number}: invalid double-quoted string") from error
    if text.startswith("[") or text.startswith("{"):
        return _flow(text, number)
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if text in ("null", "Null", "NULL", "~"):
        return None
    if INTEGER.fullmatch(text):
        return int(text)
    return text


def _split_flow(body: str, number: int) -> list[str]:
    items: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []
    for character in body:
        if quote:
            current.append(character)
            if character == quote:
                quote = None
        elif character in "'\"":
            quote = character
            current.append(character)
        elif character in "[{":
            depth += 1
            current.append(character)
        elif character in "]}":
            depth -= 1
            current.append(character)
        elif character == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    if depth != 0 or quote:
        raise YamlError(f"line {number}: unbalanced flow collection")
    return items


def _flow(text: str, number: int) -> object:
    opening, closing = text[0], text[-1]
    if (opening, closing) not in (("[", "]"), ("{", "}")):
        raise YamlError(f"line {number}: unbalanced flow collection")
    items = _split_flow(text[1:-1], number)
    if opening == "[":
        return [_scalar(item, number) for item in items]
    mapping: dict[str, object] = {}
    for item in items:
        match = PLAIN_KEY.match(item)
        if not match:
            raise YamlError(f"line {number}: flow mapping entry without a key: {item!r}")
        mapping[str(_scalar(match.group(1), number))] = _scalar(item[match.end():], number)
    return mapping


def _flow_spans_lines(text: str) -> bool:
    depth = 0
    quote: str | None = None
    for character in text:
        if quote:
            if character == quote:
                quote = None
        elif character in "'\"":
            quote = character
        elif character in "[{":
            depth += 1
        elif character in "]}":
            depth -= 1
    return depth > 0


def _collect_flow(lines: list[tuple[int, int, str]], index: int, text: str) -> tuple[str, int]:
    """Join the lines of a flow collection that continues past ``index``; return the text and the next index."""
    number = lines[index][0]
    index += 1
    while _flow_spans_lines(text):
        if index >= len(lines):
            raise YamlError(f"line {number}: unterminated flow collection")
        text += " " + lines[index][2]
        index += 1
    return text, index


def _value(lines: list[tuple[int, int, str]], index: int, text: str) -> tuple[object, int]:
    """The value written after a key or sequence dash on line ``index``; return it and the next index."""
    number = lines[index][0]
    if text.startswith("[") or text.startswith("{"):
        text, index = _collect_flow(lines, index, text)
        return _flow(text, number), index
    return _scalar(text, number), index + 1


def _parse_node(lines: list[tuple[int, int, str]], index: int, indent: int) -> tuple[object, int]:
    number, line_indent, content = lines[index]
    if line_indent != indent:
        raise YamlError(f"line {number}: unexpected indentation")
    if content == "-" or content.startswith("- "):
        return _parse_sequence(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _nested(lines: list[tuple[int, int, str]], index: int, indent: int, allow_same_indent_sequence: bool) -> tuple[object, int]:
    """The block value of an empty key or dash on the previous line, or None when nothing is nested."""
    if index < len(lines):
        next_indent, next_content = lines[index][1], lines[index][2]
        if next_indent > indent:
            return _parse_node(lines, index, next_indent)
        if allow_same_indent_sequence and next_indent == indent and (next_content == "-" or next_content.startswith("- ")):
            return _parse_sequence(lines, index, indent)
    return None, index


def _parse_sequence(lines: list[tuple[int, int, str]], index: int, indent: int) -> tuple[list[object], int]:
    items: list[object] = []
    while index < len(lines) and lines[index][1] == indent and (lines[index][2] == "-" or lines[index][2].startswith("- ")):
        number, _, content = lines[index]
        rest = content[1:].lstrip()
        if rest == "":
            value, index = _nested(lines, index + 1, indent, allow_same_indent_sequence=False)
            items.append(value)
            continue
        if rest.startswith("? "):
            raise YamlError(f"line {number}: complex mapping keys are not supported")
        if PLAIN_KEY.match(rest) and not rest.startswith("[") and not rest.startswith("{"):
            item_indent = indent + (len(content) - len(rest))
            lines[index] = (number, item_indent, rest)
            value, index = _parse_mapping(lines, index, item_indent)
            items.append(value)
            continue
        if rest == "-" or rest.startswith("- "):
            item_indent = indent + (len(content) - len(rest))
            lines[index] = (number, item_indent, rest)
            value, index = _parse_sequence(lines, index, item_indent)
            items.append(value)
            continue
        value, index = _value(lines, index, rest)
        items.append(value)
    return items, index


def _parse_mapping(lines: list[tuple[int, int, str]], index: int, indent: int) -> tuple[dict[str, object], int]:
    mapping: dict[str, object] = {}
    while index < len(lines) and lines[index][1] == indent:
        number, _, content = lines[index]
        if content == "-" or content.startswith("- "):
            break
        if content.startswith("? "):
            raise YamlError(f"line {number}: complex mapping keys are not supported")
        match = PLAIN_KEY.match(content)
        if not match:
            raise YamlError(f"line {number}: expected a mapping key")
        key = str(_scalar(match.group(1), number))
        rest = content[match.end():].strip()
        if rest == "":
            value, index = _nested(lines, index + 1, indent, allow_same_indent_sequence=True)
        else:
            value, index = _value(lines, index, rest)
        mapping[key] = value
    if index < len(lines) and lines[index][1] > indent:
        raise YamlError(f"line {lines[index][0]}: unexpected indentation")
    return mapping, index


def parse_yaml(text: str) -> object:
    """Read the YAML subset of a Dependabot configuration: block and flow mappings and sequences, plain and quoted scalars."""
    lines = _logical_lines(text)
    if not lines:
        return None
    value, index = _parse_node(lines, 0, lines[0][1])
    if index != len(lines):
        raise YamlError(f"line {lines[index][0]}: unexpected content")
    return value


# --- the coverage check -------------------------------------------------------------------------------------------


def load_ecosystems(path: Path = ECOSYSTEMS_PATH) -> dict[str, list[str]]:
    ecosystems = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(ecosystems, dict) or not all(isinstance(patterns, list) and patterns for patterns in ecosystems.values()):
        raise CoverageError(f"{path} must map each Dependabot ecosystem to a non-empty list of file patterns")
    return ecosystems


def tracked_files(root: Path) -> list[str]:
    result = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=False)
    if result.returncode != 0:
        raise CoverageError(f"git ls-files failed in {root}: {result.stderr.decode('utf-8', 'replace').strip()}")
    return [path for path in result.stdout.decode("utf-8", "surrogateescape").split("\0") if path]


def normalize_directory(directory: str) -> str:
    directory = "/" + directory.strip().strip("/")
    return directory


def directory_glob_matches(pattern: str, directory: str) -> bool:
    """Dependabot ``directories`` entries are globs: ``*`` within one segment, ``**`` across segments."""
    expression = ""
    index = 0
    pattern = normalize_directory(pattern)
    while index < len(pattern):
        if pattern.startswith("**", index):
            expression += ".*"
            index += 2
        elif pattern[index] == "*":
            expression += "[^/]*"
            index += 1
        elif pattern[index] == "?":
            expression += "[^/]"
            index += 1
        else:
            expression += re.escape(pattern[index])
            index += 1
    return re.fullmatch(expression, directory) is not None


def ecosystem_of(path: str, ecosystems: dict[str, list[str]]) -> str | None:
    name = PurePosixPath(path).name
    for ecosystem, patterns in ecosystems.items():
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns):
            return ecosystem
    return None


def is_excluded(path: str, excludes: list[str]) -> bool:
    for exclude in excludes:
        prefix = exclude.strip().strip("/")
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            return True
    return False


def read_configuration(root: Path) -> list[dict[str, object]] | None:
    """The ``updates`` entries of the Dependabot configuration, or None when the repository has none."""
    for relative in CONFIG_PATHS:
        path = root / relative
        if path.is_file():
            try:
                document = parse_yaml(path.read_text(encoding="utf-8"))
            except YamlError as error:
                raise CoverageError(f"{relative}: {error}") from error
            if not isinstance(document, dict):
                raise CoverageError(f"{relative}: the configuration must be a mapping")
            updates = document.get("updates")
            if updates is None:
                return []
            if not isinstance(updates, list) or not all(isinstance(entry, dict) for entry in updates):
                raise CoverageError(f"{relative}: updates must be a list of entries")
            return updates
    return None


def entry_covers(entry: dict[str, object], ecosystem: str, directory: str) -> bool:
    if entry.get("package-ecosystem") != ecosystem:
        return False
    listed = entry.get("directory")
    if isinstance(listed, str) and normalize_directory(listed) == directory:
        return True
    patterns = entry.get("directories")
    if isinstance(patterns, list):
        return any(isinstance(pattern, str) and directory_glob_matches(pattern, directory) for pattern in patterns)
    return False


def check(root: Path, ecosystems: dict[str, list[str]], excludes: list[str] | None = None) -> list[Uncovered]:
    """Every tracked package file without a covering Dependabot entry."""
    updates = read_configuration(root)
    uncovered: list[Uncovered] = []
    for path in tracked_files(root):
        if is_excluded(path, excludes or []):
            continue
        ecosystem = ecosystem_of(path, ecosystems)
        if ecosystem is None:
            continue
        parent = PurePosixPath(path).parent
        directory = "/" if str(parent) == "." else "/" + parent.as_posix()
        if updates is None or not any(entry_covers(entry, ecosystem, directory) for entry in updates):
            uncovered.append(Uncovered(path, ecosystem, directory))
    return uncovered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--ecosystems", type=Path, default=ECOSYSTEMS_PATH)
    parser.add_argument("--exclude", action="append", default=[], help="a path whose files are not checked; repeatable")
    parser.add_argument("--exclude-from", help="a file with one excluded path per line, or - for standard input")
    arguments = parser.parse_args(argv)
    excludes = list(arguments.exclude)
    if arguments.exclude_from:
        source = sys.stdin.read() if arguments.exclude_from == "-" else Path(arguments.exclude_from).read_text(encoding="utf-8")
        excludes.extend(line.strip() for line in source.splitlines() if line.strip())
    try:
        uncovered = check(arguments.root.resolve(), load_ecosystems(arguments.ecosystems), excludes)
    except CoverageError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2
    for item in uncovered:
        print(f"::error file={item.path}::{item.message()}")
    if uncovered:
        print(f"{len(uncovered)} tracked package file(s) without Dependabot coverage.", file=sys.stderr)
        return 1
    print("Every tracked package file has a covering Dependabot entry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
