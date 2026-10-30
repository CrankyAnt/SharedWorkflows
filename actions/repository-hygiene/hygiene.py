"""Run one repository-hygiene check with the pinned tools; the reusable workflow calls one subcommand per step.

    python3 hygiene.py changes    [--root DIR] --base SHA --head SHA [--compare merge-base|direct] [--definition GLOB ...] [--packages-definition PATH#INPUT ...] [--markdown-definition PATH#INPUT ...] [--github-output FILE] [--summary FILE]
    python3 hygiene.py changes    [--root DIR] --lookup FILE [definitions as above] [--github-output FILE] [--summary FILE]
    python3 hygiene.py changes    [--root DIR] --base SHA --head SHA --results FILE [definitions as above] [--github-output FILE] [--summary FILE]
    python3 hygiene.py record     [--root DIR] --run-id ID [--outcome CHECK=OUTCOME ...] [--reused JSON] --output FILE
    python3 hygiene.py actionlint [--tools DIR] [--root DIR]
    python3 hygiene.py zizmor     [--tools DIR] [--root DIR] [--persona NAME] [--policy FILE]
    python3 hygiene.py gitleaks   [--tools DIR] [--root DIR] [--config PATH] [--baseline PATH]
    python3 hygiene.py reuse      [--tools DIR] [--root DIR]
    python3 hygiene.py coverage   [--root DIR] [--ecosystems FILE] [--exclude PATH ...] [--exclude-from FILE|-]
    python3 hygiene.py links      [--root DIR] [--exclude GLOB ...] [--exclude-from FILE|-] [--summary FILE]

``changes`` writes ``actionlint``, ``zizmor``, ``packages``, ``markdown`` and ``reuse`` as ``true`` or ``false``. The
compatible ``workflows`` output is the union of the two linters. Each output says whether changed files can affect that
check. A change that cannot be read, or one to a shared ``--definition`` glob or selector, sets all outputs to
``true``; ``reuse`` stays ``false`` in a repository without a ``REUSE.toml``. A definition can select an input
(``PATH#INPUT``), step (``PATH#step:ID``), or exact mapping value (``PATH#key:outputs.path.value``). Check-specific definitions select only their check. ``--lookup``
takes the push lookup of ``suite_selection.py lookup`` in place of ``--base`` and ``--head``. ``--results`` takes the
record that ``record`` wrote in an earlier run of the pull request; a check it holds for the same tested tree is
reused and named in the ``reused`` output with the run it passed in. ``--tools`` names the directory
that install_tools.py filled; without it the tools come from PATH. An empty ``--persona``, ``--config`` or
``--baseline`` leaves the tool on its own default. The exit code is the tool's.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

RESOURCES = Path(__file__).resolve().parent
sys.path.insert(0, str(RESOURCES))
import dependabot_coverage  # noqa: E402
import link_check  # noqa: E402

ZIZMOR_POLICY = RESOURCES / "zizmor-policy.yml"
ACTIONLINT_IGNORE = r'specifying action "\$/[^"]*" in invalid format because ref is missing'
ACTIONLINT_WORKFLOW_IGNORE = r'reusable workflow call "\$/\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml" at "uses" is not following the format'
ACTIONLINT_LABEL_IGNORE = r'label "ubuntu-26\.04" is unknown'
CONSUMER_ZIZMOR_CONFIGS = (".github/zizmor.yml", ".github/zizmor.yaml", "zizmor.yml", "zizmor.yaml")


class ToolMissing(RuntimeError):
    """A required executable is not where the tools directory or PATH should hold it."""


def executable(name: str, tools: Path | None) -> Path:
    if tools is not None:
        for candidate in (tools / name, tools / f"{name}.exe"):
            if candidate.is_file():
                return candidate
        raise ToolMissing(f"{name} is missing from {tools}")
    found = shutil.which(name)
    if not found:
        raise ToolMissing(f"{name} is not on PATH")
    return Path(found)


def run(command: list[str], root: Path) -> int:
    print("+ " + " ".join(command), flush=True)
    return subprocess.run(command, cwd=root, check=False).returncode


def actionlint(root: Path, tools: Path | None) -> int:
    """actionlint over the repository's workflows with an explicit ShellCheck; a missing ShellCheck is a failure."""
    shellcheck = executable("shellcheck", tools)
    probe = subprocess.run([str(shellcheck), "--version"], capture_output=True, check=False)
    if probe.returncode != 0:
        raise ToolMissing(f"{shellcheck} does not run (exit {probe.returncode}); actionlint would silently skip ShellCheck")
    linter = executable("actionlint", tools)
    return run([str(linter), "-shellcheck", str(shellcheck), "-ignore", ACTIONLINT_IGNORE,
                "-ignore", ACTIONLINT_WORKFLOW_IGNORE, "-ignore", ACTIONLINT_LABEL_IGNORE], root)


def zizmor(root: Path, tools: Path | None, persona: str, policy: Path) -> int:
    """zizmor offline over workflows and action definitions, with the repository's own configuration when it has one."""
    config = next((root / relative for relative in CONSUMER_ZIZMOR_CONFIGS if (root / relative).is_file()), None)
    if config is None:
        print(f"no zizmor configuration in the repository; using the shared policy {policy}")
        config = policy
    else:
        print(f"using the repository's zizmor configuration {config.relative_to(root).as_posix()}")
    command = [str(executable("zizmor", tools)), "--offline", "--no-progress", "--collect=workflows", "--collect=actions", "--config", str(config)]
    if persona:
        command += ["--persona", persona]
    return run(command + ["."], root)


def gitleaks(root: Path, tools: Path | None, config: str, baseline: str) -> int:
    """Gitleaks over every commit reachable from any ref."""
    command = [str(executable("gitleaks", tools)), "git", "--log-opts=--all", "--redact", "--no-banner", "--no-color"]
    if config:
        command += ["--config", config]
    if baseline:
        command += ["--baseline-path", baseline]
    return run(command + ["."], root)


def reuse(root: Path, tools: Path | None) -> int:
    """``reuse lint`` over the repository; its report names every file without a copyright and licence assignment."""
    return run([str(executable("reuse", tools)), "--root", ".", "lint"], root)


# The files whose change can change what actionlint and zizmor report: workflows, action definitions and the zizmor
# configuration. zizmor collects action definitions from the whole tree, so the pattern is not limited to .github/.
ACTIONLINT_CONFIGS = (".github/actionlint.yml", ".github/actionlint.yaml")
WORKFLOW_PATTERNS = ("*.github/workflows/*", "*action.yml", "*action.yaml", *CONSUMER_ZIZMOR_CONFIGS, *ACTIONLINT_CONFIGS)
ALL_ZERO = re.compile(r"^0+$")


def changed_files(root: Path, base: str, head: str, compare: str) -> list[str] | None:
    """The files that differ between the commits, as the suite selection reads them, or None when they cannot be read:
    an all-zero or absent previous head, or commits the checkout does not hold. ``compare`` is ``merge-base`` for a
    pull request and ``direct`` for a push. A deleted or renamed file counts under each of its paths."""
    if not base or ALL_ZERO.match(base) or not head:
        return None
    start = base
    if compare == "merge-base":
        merge_base = subprocess.run(["git", "merge-base", base, head], cwd=root, capture_output=True, text=True, check=False)
        if merge_base.returncode != 0:
            return None
        start = merge_base.stdout.strip()
    listing = subprocess.run(["git", "diff", "--name-only", "--no-renames", "-z", start, head], cwd=root, capture_output=True, text=True, check=False)
    if listing.returncode != 0:
        return None
    return sorted({item for item in listing.stdout.split("\0") if item})


def added_or_removed_files(root: Path, start: str, head: str) -> set[str]:
    """The files that the change added or removed; a renamed file counts as both, under each of its paths."""
    listing = subprocess.run(["git", "diff", "--name-only", "--no-renames", "--diff-filter=AD", "-z", start, head], cwd=root, capture_output=True, text=True, check=True)
    return {item for item in listing.stdout.split("\0") if item}


def matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(PurePosixPath(path).name, pattern) for pattern in patterns)


def git_file(root: Path, revision: str, path: str) -> list[str] | None:
    """The file at ``revision``, split into lines, or None when that revision lacks it."""
    result = subprocess.run(["git", "show", f"{revision}:{path}"], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    return result.stdout.splitlines() if result.returncode == 0 else None


def key_block(lines: list[str], key: str) -> set[int]:
    """Line numbers in YAML mappings named ``key``. The workflow inputs use ordinary indentation-only mappings."""
    match_key = re.compile(rf"^(?P<indent>[ \t]*){re.escape(key)}:\s*.*$")
    numbers: set[int] = set()
    for index, line in enumerate(lines):
        match = match_key.match(line)
        if match is None:
            continue
        indent = len(match.group("indent").expandtabs(8))
        numbers.add(index + 1)
        for nested, candidate in enumerate(lines[index + 1:], start=index + 2):
            stripped = candidate.strip()
            if stripped and len(candidate) - len(candidate.lstrip(" \t")) <= indent:
                break
            numbers.add(nested)
    return numbers


def parse_definition(value: str, option: str) -> tuple[str, str]:
    """A precise input, step ID, or dotted mapping path inside a repository file."""
    path, separator, key = value.partition("#")
    if not separator or not path or not key:
        raise ValueError(f"{option} must be PATH#INPUT, got {value!r}")
    if ":" in key and not re.fullmatch(r"(?:step:[A-Za-z_][A-Za-z0-9_-]*|key:[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)", key):
        raise ValueError(f"{option} has an invalid selector in {value!r}; expected INPUT, step:ID or key:DOTTED.PATH")
    return path, key


def block_end(lines: list[str], start: int, indent: int) -> int:
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if line.strip() and not line.lstrip().startswith("#") and len(line) - len(line.lstrip()) <= indent:
            return index
    return len(lines)


def selector_block(lines: list[str], selector: str) -> set[int]:
    """Locate an ordinary indentation-based YAML mapping or a step by its ID, without importing a YAML package.

    Step IDs are unique within the selected file. Flow mappings and ambiguous IDs are rejected when selected.
    Legacy input selectors retain their original behavior across caller and workflow input mappings.
    """
    if selector.startswith("step:"):
        wanted = selector.removeprefix("step:")
        found: list[set[int]] = []
        for start, line in enumerate(lines):
            match = re.match(r"^( *)-\s+", line)
            if match is None:
                continue
            indent = len(match.group(1))
            end = block_end(lines, start, indent)
            for index in range(start, end):
                candidate = lines[index][indent + 2:]
                identifier = re.fullmatch(r"id:\s*['\"]?([A-Za-z_][A-Za-z0-9_-]*)['\"]?\s*(?:#.*)?", candidate)
                if identifier and identifier.group(1) == wanted:
                    found.append(set(range(start + 1, end + 1)))
                    break
        if len(found) > 1:
            raise ValueError(f"step ID {wanted!r} is ambiguous")
        return found[0] if found else set()
    if selector.startswith("key:"):
        wanted = selector.removeprefix("key:").split(".")
        start, end, parent_indent = 0, len(lines), -1
        for key in wanted:
            matches = []
            child_indent = None
            for index in range(start, end):
                line = lines[index]
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                indent = len(line) - len(line.lstrip())
                if indent <= parent_indent:
                    break
                if child_indent is None:
                    child_indent = indent
                if indent == child_indent and re.match(rf" *['\"]?{re.escape(key)}['\"]?:", line):
                    matches.append(index)
            if not matches:
                return set()
            if len(matches) > 1:
                raise ValueError(f"mapping path {selector!r} is ambiguous")
            selected = matches[0]
            parent_indent = len(lines[selected]) - len(lines[selected].lstrip())
            end = min(end, block_end(lines, selected, parent_indent))
            start = selected + 1
        return set(range(selected + 1, end + 1))
    return key_block(lines, selector)


def without_comment(line: str, keep_directives: bool = False) -> str:
    """Strip a comment outside quoted text, preserving linter controls when comparing linter inputs."""
    quote = None
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
        elif character == "\\" and quote != "'":
            escaped = True
        elif quote:
            if character == quote:
                quote = None
        elif character in "'\"":
            quote = character
        elif character == "#" and (index == 0 or line[index - 1].isspace()):
            if keep_directives and re.match(r"#\s*(?:shellcheck\b|zizmor\s*:|actionlint\b)", line[index:], re.IGNORECASE):
                break
            return line[:index].rstrip()
    return line.rstrip()


def definition_content(lines: list[str] | None, block: set[int], selector: str) -> list[str]:
    """Compare executable data, ignoring blank lines, comments, and a step's display name."""
    if lines is None:
        return []
    selected = [without_comment(lines[number - 1]) for number in sorted(block)]
    step_indent = min((len(line) - len(line.lstrip()) for line in selected if line.strip() and not line.lstrip().startswith("#")), default=0)
    result = []
    for line in selected:
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if selector.startswith("step:"):
            field = line[step_indent + 2:]
            if re.match(r"(?:name|id):", field):
                continue
        result.append(line[step_indent:].rstrip())
    return result


def delegated_workflow_refs(lines: list[str] | None, path: str) -> set[str]:
    """A same-named caller delegates its steps to the referenced reusable workflow."""
    if lines is None or not path.startswith(".github/workflows/"):
        return set()
    refs = set()
    for line in lines:
        match = re.fullmatch(r"uses:\s*['\"]?([^\s'\"]+)['\"]?", without_comment(line).strip())
        if match and "@" in match[1] and match[1].split("@", 1)[0].endswith("/" + path):
            refs.add(match[1])
    return refs


def definition_inputs_changed(root: Path, base: str, head: str, definitions: list[str], option: str) -> set[str]:
    """Definitions whose selected data changed between revisions; malformed precise selectors fail explicitly."""
    changed: set[str] = set()
    for definition in definitions:
        path, key = parse_definition(definition, option)
        old_lines, new_lines = git_file(root, base, path), git_file(root, head, path)
        if old_lines == new_lines:
            continue
        try:
            old_block = selector_block(old_lines, key) if old_lines is not None else set()
            new_block = selector_block(new_lines, key) if new_lines is not None else set()
            if ":" in key and not old_block and not new_block:
                local_steps = any(re.match(r"\s+['\"]?steps['\"]?\s*:", without_comment(line))
                                  for lines in (old_lines, new_lines) for line in lines or [])
                if key.startswith("step:") and not local_steps:
                    old_refs = delegated_workflow_refs(old_lines, path)
                    new_refs = delegated_workflow_refs(new_lines, path)
                    if old_refs or new_refs:
                        if old_refs != new_refs:
                            changed.add(path)
                        continue
                raise ValueError(f"selector {key!r} was not found in either revision of {path}")
        except ValueError as error:
            raise ValueError(f"{option}: {error}") from error
        if definition_content(old_lines, old_block, key) != definition_content(new_lines, new_block, key):
            changed.add(path)
    return changed


def comment_only_paths(root: Path, base: str, head: str, changed: list[str]) -> set[str]:
    """Workflow files with the same data, ignoring ordinary comments but retaining linter directives."""
    comment_only: set[str] = set()
    for path in changed:
        if not matches_any(path, WORKFLOW_PATTERNS):
            continue
        before, after = git_file(root, base, path), git_file(root, head, path)
        if before is None or after is None:
            continue
        old = [code for line in before if (code := without_comment(line, keep_directives=True)).strip()]
        new = [code for line in after if (code := without_comment(line, keep_directives=True)).strip()]
        if old == new:
            comment_only.add(path)
    return comment_only


CHECKS = ("workflows", "packages", "markdown", "actionlint", "zizmor", "reuse")
# The scripts and data of this folder that each check runs on: a change to one of them can change what that check
# reports, and nothing else. hygiene.py drives every check; install_tools.py and the pins install actionlint, zizmor,
# ShellCheck and reuse; the policy and the ecosystems file are the inputs of zizmor and the coverage check. In a
# repository that holds this folder (this repository itself), a changed file of a check's list runs that check; a caller
# that loads the folder from the shared workflow's revision has none of these files in its tree.
CHECK_SCRIPTS = {
    "workflows": ("hygiene.py", "install_tools.py", "pins.py", "pins.json", "zizmor-policy.yml"),
    "packages": ("hygiene.py", "dependabot_coverage.py", "package-ecosystems.json"),
    "markdown": ("hygiene.py", "link_check.py"),
    "actionlint": ("hygiene.py", "install_tools.py", "pins.py", "pins.json"),
    "zizmor": ("hygiene.py", "install_tools.py", "pins.py", "pins.json", "zizmor-policy.yml"),
    "reuse": ("hygiene.py", "install_tools.py", "pins.py", "pins.json"),
}
REUSE_CONFIG = "REUSE.toml"


HYGIENE_FOLDER = "actions/repository-hygiene"  # where the repository that owns this script keeps it


def check_script_patterns(root: Path | None) -> dict[str, tuple[str, ...]]:
    """The repository paths of each check's scripts: under this folder when it lies inside ``root``, under the owning
    repository's folder when ``root`` holds one (a copy of this script deciding for that checkout), empty otherwise."""
    if root is None:
        return {name: () for name in CHECKS}
    try:
        folder = RESOURCES.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        folder = HYGIENE_FOLDER if (root / HYGIENE_FOLDER).is_dir() else None
    if folder is None:
        return {name: () for name in CHECKS}
    patterns = {name: tuple(f"{folder}/{script}" for script in scripts) for name, scripts in CHECK_SCRIPTS.items()}
    if any((root / path).is_file() for path in CONSUMER_ZIZMOR_CONFIGS):
        patterns["zizmor"] = tuple(path for path in patterns["zizmor"] if not path.endswith("/zizmor-policy.yml"))
    return patterns


def local_action_definitions(root: Path | None) -> set[str]:
    """The local action metadata actionlint can load from workflow uses declarations."""
    if root is None:
        return set()
    paths: set[str] = set()
    folder = root / ".github/workflows"
    for workflow in folder.glob("*"):
        if workflow.suffix not in (".yml", ".yaml") or not workflow.is_file():
            continue
        for match in re.finditer(r"(?m)^\s*(?:-\s*)?uses:\s*['\"]?\./([^\s'\"#]+)", workflow.read_text(encoding="utf-8")):
            action = match.group(1).rstrip("/")
            paths.update((f"{action}/action.yml", f"{action}/action.yaml"))
    return paths


def decide_changes(changed: list[str] | None, definition: list[str], packages_definition: set[str], markdown_definition: set[str], ecosystems: dict[str, list[str]], root: Path | None = None, ignored: set[str] | None = None, added_or_removed: set[str] | None = None) -> dict[str, tuple[bool, str]]:
    """Which checks the change can affect: ``workflows`` (actionlint, zizmor), ``packages`` (Dependabot coverage) and
    ``markdown`` (the link check), each with its reason. A change that cannot be read, or one to the CI definition,
    runs every check; a change to a script of one check (``CHECK_SCRIPTS``) runs that check. A caller definition can
    change ``link-exclude``, so it runs only the link check. The link check also runs for a changed Markdown file and
    for a file that the change removed, since a link to it dies without any Markdown changing; ``root`` is where
    removal and the scripts' location are read, when given. REUSE runs only in a repository with a ``REUSE.toml`` at
    ``root``: for a file that ``added_or_removed`` holds (a rename is both) and for a change to ``REUSE.toml`` or
    ``LICENSES/``."""
    decisions = _decide_changes(changed, definition, packages_definition, markdown_definition, ecosystems, root, ignored, added_or_removed or set())
    if root is None or not (root / REUSE_CONFIG).is_file():
        decisions["reuse"] = (False, f"the repository has no {REUSE_CONFIG}")
    return decisions


def _decide_changes(changed: list[str] | None, definition: list[str], packages_definition: set[str], markdown_definition: set[str], ecosystems: dict[str, list[str]], root: Path | None, ignored: set[str] | None, added_or_removed: set[str]) -> dict[str, tuple[bool, str]]:
    if changed is None:
        reason = "the changed files could not be determined, so every check runs"
        return {name: (True, reason) for name in CHECKS}
    definition_hit = next((path for path in changed if matches_any(path, tuple(definition))), None)
    if definition_hit is not None:
        reason = f"`{definition_hit}` belongs to the CI definition, so every check runs"
        return {name: (True, reason) for name in CHECKS}
    ignored = ignored or set()
    relevant = [path for path in changed if path not in ignored]
    scripts = check_script_patterns(root)
    manifest_patterns = tuple(pattern for patterns in ecosystems.values() for pattern in patterns) + dependabot_coverage.CONFIG_PATHS
    removed = [] if root is None else [path for path in changed if not (root / path).exists()]
    local_actions = local_action_definitions(root)
    # Per check: what the change touched, in the order of the reasons: its own scripts first, then its inputs.
    inputs: dict[str, list[tuple[list[str], str]]] = {
        "workflows": [([path for path in relevant if path in scripts["workflows"]], "is a script or an input of actionlint and zizmor"),
                      ([path for path in relevant if matches_any(path, WORKFLOW_PATTERNS)], "is a workflow, an action definition or the zizmor configuration")],
        "packages": [([path for path in relevant if path in scripts["packages"]], "is a script or an input of the Dependabot coverage check"),
                     ([path for path in sorted(packages_definition) if path in changed], "is a check definition that can change coverage exclusions or execution"),
                     ([path for path in relevant if matches_any(path, manifest_patterns)], "is a package file or the Dependabot configuration")],
        "markdown": [([path for path in relevant if path in scripts["markdown"]], "is a script of the link check"),
                     ([path for path in sorted(markdown_definition) if path in changed], "is a check definition that can change link exclusions or execution"),
                     ([path for path in relevant if path.lower().endswith(".md")], "is a Markdown file"),
                     (removed, "was removed, which a link may point to")],
        "actionlint": [([path for path in relevant if path in scripts["actionlint"]], "is a script or an input of actionlint"),
                       ([path for path in relevant if matches_any(path, ("*.github/workflows/*", *ACTIONLINT_CONFIGS)) or path in local_actions], "is a workflow, referenced local action or actionlint configuration")],
        "zizmor": [([path for path in relevant if path in scripts["zizmor"]], "is a script or an input of zizmor"),
                   ([path for path in relevant if matches_any(path, ("*.github/workflows/*", "*action.yml", "*action.yaml", *CONSUMER_ZIZMOR_CONFIGS))], "is a workflow, an action definition or the zizmor configuration")],
        "reuse": [([path for path in changed if path in scripts["reuse"]], "is a script or an input of REUSE"),
                  ([path for path in changed if path == REUSE_CONFIG or path.startswith("LICENSES/")], "is the REUSE assignment or a licence text"),
                  ([path for path in changed if path in added_or_removed], "was added, renamed or removed")],
    }
    none = {
        "workflows": "no changed file is a workflow, an action definition or the zizmor configuration",
        "packages": "no changed file is a package file or the Dependabot configuration",
        "markdown": "no changed file is a Markdown file and none was removed",
        "actionlint": "no changed file is an input of actionlint",
        "zizmor": "no changed file is an input of zizmor",
        "reuse": "no file was added, renamed or removed, and neither REUSE.toml nor LICENSES/ changed",
    }
    decisions = {}
    for name in CHECKS:
        hit = next(((paths[0], reason) for paths, reason in inputs[name] if paths), None)
        decisions[name] = (True, f"`{hit[0]}` {hit[1]}") if hit else (False, none[name])
    if not any(decisions[name][0] for name in ("actionlint", "zizmor")):
        decisions["workflows"] = (False, none["workflows"])
    return decisions


def decisions_between(root: Path, base: str, head: str, compare: str, definition: list[str], packages_definition: list[str], markdown_definition: list[str], ecosystems: Path) -> tuple[list[str] | None, dict[str, tuple[bool, str]]]:
    """The changed files between ``base`` and ``head`` and the decision of every check over them."""
    changed = changed_files(root, base, head, compare)
    if changed is None:
        return None, decide_changes(None, definition, set(), set(), dependabot_coverage.load_ecosystems(ecosystems), root)
    if compare == "merge-base":
        base = subprocess.run(["git", "merge-base", base, head], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
    precise = [value for value in definition if "#" in value]
    global_definitions = [value for value in definition if "#" not in value]
    global_definitions.extend(definition_inputs_changed(root, base, head, precise, "--definition"))
    return changed, decide_changes(changed, global_definitions, definition_inputs_changed(root, base, head, packages_definition, "--packages-definition"), definition_inputs_changed(root, base, head, markdown_definition, "--markdown-definition"), dependabot_coverage.load_ecosystems(ecosystems), root, comment_only_paths(root, base, head, changed), added_or_removed_files(root, base, head))


LOOKUP_CASES = ("identical", "behind", "pull-request", "own")
# The checks whose result depends only on the tested tree; an edited run reuses their passed results.
RECORDED_CHECKS = ("actionlint", "zizmor", "packages", "markdown", "reuse")


def read_lookup(path: Path) -> dict:
    """The push lookup that ``suite_selection.py lookup`` wrote."""
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"the push lookup {path} cannot be read: {error}") from error
    if not isinstance(found, dict) or found.get("case") not in LOOKUP_CASES or not isinstance(found.get("head"), str) or not found["head"]:
        raise ValueError(f"the push lookup {path} must name a case and the pushed commit")
    if found["case"] == "behind" and not (isinstance(found.get("tested_base"), str) and isinstance(found.get("landing_base"), str)):
        raise ValueError(f"the push lookup {path} of a behind pull request must name the tested base and the landing base")
    return found


def both_sides(pull_request: dict[str, tuple[bool, str]], main: dict[str, tuple[bool, str]]) -> dict[str, tuple[bool, str]]:
    """A check runs on a behind push only when the pull request's side and main's side both select it."""
    decisions = {}
    for name in CHECKS:
        (ours, reason), (theirs, _) = pull_request[name], main[name]
        if ours and theirs:
            decisions[name] = (True, f"both sides select it: {reason}")
        elif ours:
            decisions[name] = (False, "only the pull request's side selects it")
        elif theirs:
            decisions[name] = (False, "only main's side selects it")
        else:
            decisions[name] = (False, reason)
    return decisions


def tested_tree(root: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()


def read_results(path: Path, tree: str) -> dict[str, int]:
    """The passed checks of the record when it holds the same tested tree; nothing otherwise."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(record, dict) or record.get("tree") != tree or not isinstance(record.get("checks"), dict):
        return {}
    return {name: run for name, run in record["checks"].items() if name in RECORDED_CHECKS and isinstance(run, int)}


def changes(root: Path, base: str, head: str, compare: str, definition: list[str], packages_definition: list[str], markdown_definition: list[str], ecosystems: Path, output: Path | None, summary: Path | None, lookup: Path | None = None, results: Path | None = None) -> int:
    """Write individual check decisions and the compatible ``workflows`` union for the workflow's gates. With
    ``lookup``, a push decides by its case; with ``results``, a passed check of the same tested tree is reused."""
    inputs = (definition, packages_definition, markdown_definition, ecosystems)
    heading = None
    if lookup is None:
        changed, decisions = decisions_between(root, base, head, compare, *inputs)
    else:
        found = read_lookup(lookup)
        heading = f"Push lookup: `{found['case']}`, {found.get('reason', '')}."
        landing = found.get("landing_base") or ""
        if found["case"] == "identical":
            reason = f"the tree equals the tested tree that passed on pull request #{found.get('pull_request')}"
            changed, decisions = [], {name: (False, reason) for name in CHECKS}
        elif found["case"] == "behind":
            changed, ours = decisions_between(root, landing, found["head"], "direct", *inputs)
            decisions = both_sides(ours, decisions_between(root, found["tested_base"], landing, "direct", *inputs)[1])
        else:
            changed, decisions = decisions_between(root, landing, found["head"], "direct", *inputs)
    reused: dict[str, int] = {}
    if results is not None:
        passed = read_results(results, tested_tree(root))
        for name in RECORDED_CHECKS:
            if decisions[name][0] and name in passed:
                reused[name] = passed[name]
                decisions[name] = (False, f"passed in run {passed[name]}, whose tested tree this run shares")
                print(f"::notice title={name}::{decisions[name][1]}")
    if not any(decisions[name][0] for name in ("actionlint", "zizmor")):
        decisions["workflows"] = (False, decisions["workflows"][1] if not decisions["workflows"][0] else "no check of actionlint and zizmor runs")
    lines = ["## Hygiene checks for this change", ""]
    if heading is not None:
        lines += [heading, ""]
    lines.append("The changed files could not be determined." if changed is None else f"{len(changed)} changed file{'' if len(changed) == 1 else 's'}.")
    lines += ["", "| Checks | Run | Reason |", "| --- | --- | --- |"]
    for name, label in (("workflows", "actionlint, zizmor"), ("actionlint", "actionlint"), ("zizmor", "zizmor"), ("packages", "Dependabot coverage"), ("markdown", "Relative links"), ("reuse", "REUSE")):
        decided, reason = decisions[name]
        print(f"{name}={'true' if decided else 'false'}: {reason}")
        lines.append(f"| {label} | {f'passed in run {reused[name]}' if name in reused else 'yes' if decided else 'no'} | {reason} |")
    lines.append("| Gitleaks | yes | every event |")
    if output is not None:
        with output.open("a", encoding="utf-8") as handle:
            handle.write("".join(f"{name}={'true' if decided else 'false'}\n" for name, (decided, _) in decisions.items()))
            if results is not None:
                handle.write(f"reused={json.dumps(reused, separators=(',', ':'))}\n")
    if summary is not None:
        with summary.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return 0


def record(root: Path, run_id: int, outcomes: list[str], reused: str, output: Path) -> int:
    """Write the record of a pull-request run: the tested tree and each check that passed in it or was reused, with
    the run it passed in."""
    checks: dict[str, int] = {}
    previous = json.loads(reused) if reused else {}
    for item in outcomes:
        name, _, outcome = item.partition("=")
        if name not in RECORDED_CHECKS:
            raise ValueError(f"--outcome names {name!r}, which is none of {', '.join(RECORDED_CHECKS)}")
        if outcome == "success":
            checks[name] = run_id
    for name, run in previous.items():
        if name in RECORDED_CHECKS and isinstance(run, int):
            checks.setdefault(name, run)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"tree": tested_tree(root), "checks": dict(sorted(checks.items()))}) + "\n", encoding="utf-8")
    print(f"recorded {', '.join(f'{name} (run {run})' for name, run in sorted(checks.items())) or 'no passed check'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """The command line of this script; the part's tests parse the workflow's invocations with it."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subcommands = parser.add_subparsers(dest="check", required=True)
    for name in ("actionlint", "zizmor", "gitleaks", "reuse"):
        subcommand = subcommands.add_parser(name)
        subcommand.add_argument("--tools", type=Path)
        subcommand.add_argument("--root", type=Path, default=Path("."))
    subcommands.choices["zizmor"].add_argument("--persona", default="")
    subcommands.choices["zizmor"].add_argument("--policy", type=Path, default=ZIZMOR_POLICY)
    subcommands.choices["gitleaks"].add_argument("--config", default="")
    subcommands.choices["gitleaks"].add_argument("--baseline", default="")
    coverage = subcommands.add_parser("coverage")
    coverage.add_argument("--root", type=Path, default=Path("."))
    coverage.add_argument("--ecosystems", type=Path, default=dependabot_coverage.ECOSYSTEMS_PATH)
    coverage.add_argument("--exclude", action="append", default=[])
    coverage.add_argument("--exclude-from")
    linking = subcommands.add_parser("links")
    linking.add_argument("--root", type=Path, default=Path("."))
    linking.add_argument("--exclude", action="append", default=[])
    linking.add_argument("--exclude-from")
    linking.add_argument("--summary", type=Path)
    deciding = subcommands.add_parser("changes")
    deciding.add_argument("--root", type=Path, default=Path("."))
    deciding.add_argument("--base", default="", help="the previous head of a push, or the base commit of a pull request")
    deciding.add_argument("--head", default="", help="the pushed commit, or the head commit of a pull request")
    deciding.add_argument("--compare", choices=("merge-base", "direct"), default="direct")
    deciding.add_argument("--definition", action="append", default=[], help="a glob or PATH#SELECTOR shared by every check (repeatable)")
    deciding.add_argument("--packages-definition", action="append", default=[], help="PATH#SELECTOR that can change coverage; INPUT, step:ID or key:DOTTED.PATH (repeatable)")
    deciding.add_argument("--markdown-definition", action="append", default=[], help="PATH#SELECTOR that can change links; INPUT, step:ID or key:DOTTED.PATH (repeatable)")
    deciding.add_argument("--ecosystems", type=Path, default=dependabot_coverage.ECOSYSTEMS_PATH)
    deciding.add_argument("--lookup", type=Path, help="the push lookup of a push or dispatch run, in place of --base and --head")
    deciding.add_argument("--results", type=Path, help="the record of an earlier run of the pull request, whose passed checks of the same tested tree are reused")
    deciding.add_argument("--github-output", type=Path)
    deciding.add_argument("--summary", type=Path)
    recording = subcommands.add_parser("record")
    recording.add_argument("--root", type=Path, default=Path("."))
    recording.add_argument("--run-id", type=int, required=True)
    recording.add_argument("--outcome", action="append", default=[], help="CHECK=OUTCOME of a check step of this run (repeatable)")
    recording.add_argument("--reused", default="", help="the reused output of changes")
    recording.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.check == "changes":
        if arguments.lookup is not None and (arguments.base or arguments.head):
            parser.error("changes takes --lookup without --base and --head")
        try:
            return changes(arguments.root.resolve(), arguments.base, arguments.head, arguments.compare, arguments.definition, arguments.packages_definition, arguments.markdown_definition,
                           arguments.ecosystems, arguments.github_output, arguments.summary, arguments.lookup, arguments.results)
        except ValueError as error:
            print(f"::error::{error}", file=sys.stderr)
            return 2
    if arguments.check == "record":
        try:
            return record(arguments.root.resolve(), arguments.run_id, arguments.outcome, arguments.reused, arguments.output)
        except (ValueError, json.JSONDecodeError) as error:
            print(f"::error::{error}", file=sys.stderr)
            return 2
    if arguments.check == "coverage":
        passthrough = ["--root", str(arguments.root), "--ecosystems", str(arguments.ecosystems)]
        for exclude in arguments.exclude:
            passthrough += ["--exclude", exclude]
        if arguments.exclude_from:
            passthrough += ["--exclude-from", arguments.exclude_from]
        return dependabot_coverage.main(passthrough)
    if arguments.check == "links":
        return link_check.check(arguments.root.resolve(), link_check.read_exclusions(arguments.exclude, arguments.exclude_from), arguments.summary)
    root = arguments.root.resolve()
    tools = arguments.tools.resolve() if arguments.tools else None
    try:
        if arguments.check == "actionlint":
            return actionlint(root, tools)
        if arguments.check == "zizmor":
            return zizmor(root, tools, arguments.persona, arguments.policy)
        if arguments.check == "reuse":
            return reuse(root, tools)
        return gitleaks(root, tools, arguments.config, arguments.baseline)
    except ToolMissing as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
