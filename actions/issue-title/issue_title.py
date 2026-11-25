#!/usr/bin/env python3
"""Give an opened issue the title prefix of its category.

    python3 issue_title.py --event FILE --root DIR --categories FILE [--apply]

``--event`` is the payload of an ``issues`` event, ``--root`` the checkout of the repository and ``--categories`` the
repository's category file. The program prints the normalised title, or why the title stays: ``not opened`` for another
action than ``opened``, ``unchanged`` for a title that already has its prefix or belongs to no category. With ``--apply``
it changes the title of the issue with one ``PATCH /repos/{repository}/issues/{number}``, with the token in
``GITHUB_TOKEN`` and the API address in ``GITHUB_API_URL``. It exits 1 when the category file or a marker cannot be used,
or when the change fails.

The category file is a JSON object with two lists, of which it reads these keys and ignores any other:

- ``product_categories``: the component categories. Each holds ``key``, ``source_directory`` (the folder whose direct
  subfolders are the components), ``marker`` (the file in a component's folder that makes it a component),
  ``title_prefix``, ``dynamic_title_prefix`` (with ``{component}``), ``component_label`` (the heading of the component
  field in the issue form), ``required_metadata`` (the keys its marker must hold) and ``labels``.
- ``generic_forms``: the categories without components. Each holds ``key``, ``title_prefix`` and ``labels``.

A marker starts with a block of ``key: value`` lines between two ``---`` lines. Its ``name``, when present, equals the
folder's name; an ``entrypoint`` that the category requires names a file inside the folder.

The category of an issue is the one category whose label the issue carries, otherwise the component category whose
``component_label`` heads a filled section of the body, otherwise the category that alone holds the component under the
heading ``Component``. A component category gives the prefix of the component that the section names, in the spelling
of its folder; ``Not sure``, an unknown component and a missing one give the ``unsure`` prefix. Without a component
section, a prefix in the title that names a current component stays. A generic category gives its ``title_prefix``. An
issue without a category keeps its title, with a known prefix in its canonical spelling.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

GITHUB_API_URL = "https://api.github.com"
USER_AGENT = "shared-workflows-issue-title"
UNSURE = "unsure"
NOT_SURE = "not sure"
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 0.5
MAX_RETRY_AFTER = 60.0


class CategoryError(ValueError):
    """The category file or a marker cannot be used; the message says why."""


@dataclass(frozen=True)
class Category:
    key: str
    title_prefix: str
    labels: tuple[str, ...]
    dynamic_title_prefix: str | None = None
    source_directory: str = ""
    marker: str = ""
    component_label: str = ""
    required_metadata: tuple[str, ...] = ()

    @property
    def has_components(self) -> bool:
        return self.dynamic_title_prefix is not None


# --- the category file ----------------------------------------------------------------------------------------------


def _strings(item: dict, keys: tuple[str, ...], location: str) -> None:
    missing = sorted(set(keys) - item.keys())
    if missing:
        raise CategoryError(f"{location} is missing: {', '.join(missing)}")
    for key in keys:
        if not isinstance(item[key], str) or not item[key].strip():
            raise CategoryError(f"{location}.{key} must be a non-empty string")


def _string_list(item: dict, key: str, location: str) -> tuple[str, ...]:
    values = item.get(key)
    if not isinstance(values, list) or not all(isinstance(value, str) and value.strip() for value in values):
        raise CategoryError(f"{location}.{key} must be a list of non-empty strings")
    return tuple(value.strip() for value in values)


def load_categories_text(text: str, location: str) -> tuple[Category, ...]:
    """The categories parsed from JSON text, the component categories first."""
    try:
        raw = json.loads(text)
    except ValueError as error:
        raise CategoryError(f"the category file {location} is no JSON: {error}") from error
    if not isinstance(raw, dict):
        raise CategoryError(f"{location} must contain a JSON object")
    categories: list[Category] = []
    for name in ("product_categories", "generic_forms"):
        if not isinstance(raw.get(name, []), list):
            raise CategoryError(f"{name} must be a list")
    for index, item in enumerate(raw.get("product_categories", []), start=1):
        item_location = f"product_categories[{index}]"
        if not isinstance(item, dict):
            raise CategoryError(f"{item_location} must be an object")
        _strings(item, ("key", "source_directory", "marker", "title_prefix", "dynamic_title_prefix", "component_label"), item_location)
        if "{component}" not in item["dynamic_title_prefix"]:
            raise CategoryError(f"{item_location}.dynamic_title_prefix must contain {{component}}")
        categories.append(Category(key=item["key"].strip(), title_prefix=item["title_prefix"].strip(), labels=_string_list(item, "labels", item_location),
                                   dynamic_title_prefix=item["dynamic_title_prefix"].strip(), source_directory=item["source_directory"].strip(),
                                   marker=item["marker"].strip(), component_label=item["component_label"].strip(),
                                   required_metadata=_string_list(item, "required_metadata", item_location)))
    for index, item in enumerate(raw.get("generic_forms", []), start=1):
        item_location = f"generic_forms[{index}]"
        if not isinstance(item, dict):
            raise CategoryError(f"{item_location} must be an object")
        _strings(item, ("key", "title_prefix"), item_location)
        categories.append(Category(key=item["key"].strip(), title_prefix=item["title_prefix"].strip(), labels=_string_list(item, "labels", item_location)))
    if not categories:
        raise CategoryError(f"{location} holds no category")
    keys = [category.key for category in categories]
    if len(keys) != len(set(keys)):
        raise CategoryError("the keys of the categories must be unique")
    return tuple(categories)


def load_categories(path: Path) -> tuple[Category, ...]:
    """The categories of a category file, the component categories first."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise CategoryError(f"the category file {path} cannot be read: {error}") from error
    return load_categories_text(text, str(path))


# --- the components of the repository -------------------------------------------------------------------------------


def _marker_metadata_text(text: str, location: str, required: tuple[str, ...]) -> dict[str, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise CategoryError(f"{location}: marker metadata must start with ---")
    closing = next((index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"), None)
    if closing is None:
        raise CategoryError(f"{location}: marker metadata has no closing ---")
    metadata: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise CategoryError(f"{location}: malformed metadata line {line!r}")
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip().strip("\"'")
    missing = [key for key in required if not metadata.get(key)]
    if missing:
        raise CategoryError(f"{location}: missing marker metadata: {', '.join(missing)}")
    return metadata


def _marker_metadata(marker: Path, required: tuple[str, ...]) -> dict[str, str]:
    return _marker_metadata_text(marker.read_text(encoding="utf-8"), str(marker), required)


def _inside(folder: Path, value: str, field: str) -> Path:
    """The path that a value names inside a component's folder; a value that leaves the folder fails."""
    windows = PureWindowsPath(value)
    target = folder / value
    portable = folder / value.replace("\\", "/")
    if windows.drive or windows.root or not portable.resolve().is_relative_to(folder.resolve()) or not target.resolve().is_relative_to(folder.resolve()):
        raise CategoryError(f"{folder}: {field} {value!r} outside product directory")
    return target


def components(root: Path, categories: tuple[Category, ...]) -> dict[str, list[str]]:
    """Per component category the names of its components: the folders under its source directory that hold its marker."""
    found: dict[str, list[str]] = {}
    for category in categories:
        if not category.has_components:
            continue
        names: list[str] = []
        base = root / category.source_directory
        folders = sorted((path for path in base.iterdir() if path.is_dir()), key=lambda path: path.name.casefold()) if base.is_dir() else []
        for folder in folders:
            marker = _inside(folder, category.marker, "marker")
            if not marker.is_file():
                continue
            metadata = _marker_metadata(marker, category.required_metadata)
            name = metadata.get("name", folder.name)
            if name != folder.name:
                raise CategoryError(f"{marker}: canonical name {name!r} does not match directory {folder.name!r}")
            if "entrypoint" in category.required_metadata and not _inside(folder, metadata["entrypoint"], "entrypoint").is_file():
                raise CategoryError(f"{marker}: missing entrypoint {metadata['entrypoint']!r}")
            if any(other.casefold() == name.casefold() for other in names):
                raise CategoryError(f"{marker}: duplicate canonical component name {name!r}")
            names.append(name)
        found[category.key] = sorted(names, key=str.casefold)
    return found


# --- the body of the issue ------------------------------------------------------------------------------------------


def _section_key(label: str) -> str:
    return label.strip().lower().replace(" ", "_")


def sections(body: str) -> dict[str, str]:
    """The text under each heading of level two or deeper, by the heading in lower case with ``_`` for a space."""
    found: dict[str, str] = {}
    current, lines = None, []
    for raw in body.splitlines():
        heading = re.match(r"^#{2,}\s*(.+?)\s*$", raw)
        if heading:
            if current is not None:
                found[current] = "\n".join(lines).strip()
            current, lines = _section_key(heading.group(1)), []
        elif current is not None:
            lines.append(raw)
    if current is not None:
        found[current] = "\n".join(lines).strip()
    return found


def _first_line(value: str) -> str:
    """The first line with text, without the box of a checklist item."""
    for line in value.splitlines():
        text = line.strip()
        if text:
            return text.split("]", 1)[1].strip() if text.startswith("- [") and "]" in text else text
    return ""


def category_from_body(found: dict[str, str], categories: tuple[Category, ...], inventory: dict[str, list[str]]) -> tuple[str | None, str | None]:
    """The category and the component that the body names: a filled section under a category's own heading, or the one
    category that holds the component under the heading ``Component``."""
    for category in categories:
        if category.has_components:
            value = _first_line(found.get(_section_key(category.component_label), ""))
            if value:
                return category.key, value
    component = _first_line(found.get("component", ""))
    if not component:
        return None, None
    for same in (lambda item: item == component, lambda item: item.casefold() == component.casefold()):
        matches = [(key, item) for key, items in inventory.items() for item in items if same(item)]
        if len(matches) == 1:
            return matches[0]
    return None, None


def category_from_labels(issue: dict, categories: tuple[Category, ...]) -> str | None:
    """The one category that a label of the issue names."""
    names = set()
    for label in issue.get("labels", []) or []:
        name = label if isinstance(label, str) else label.get("name") if isinstance(label, dict) else None
        if isinstance(name, str):
            names.add(name.casefold())
    matches = [category.key for category in categories if any(label.casefold() in names for label in category.labels)]
    return matches[0] if len(matches) == 1 else None


def canonical_component(raw: str, names: list[str]) -> tuple[str, bool]:
    """The component in the spelling of its folder, or ``unsure``; and whether the value named no current component."""
    if raw.casefold() == NOT_SURE:
        return UNSURE, False
    matches = [name for name in names if name.casefold() == raw.casefold()] if raw else []
    return (matches[0], False) if len(matches) == 1 else (UNSURE, True)


# --- the title ------------------------------------------------------------------------------------------------------


def _known_prefixes(categories: tuple[Category, ...], inventory: dict[str, list[str]]) -> list[str]:
    prefixes: list[str] = []
    for category in categories:
        prefixes.append(category.title_prefix)
        if category.dynamic_title_prefix:
            prefixes += [category.dynamic_title_prefix.format(component=name) for name in [*inventory.get(category.key, []), UNSURE]]
    return list(dict.fromkeys(prefixes))


def _split_known_prefix(title: str, prefixes: list[str]) -> tuple[str, str | None]:
    """The title without its known prefix, and that prefix in its canonical spelling."""
    stripped = title.strip()
    for prefix in sorted(set(prefixes), key=len, reverse=True):
        if stripped.lower() == prefix.lower():
            return "", prefix
        if stripped.lower().startswith(prefix.lower() + " "):
            return stripped[len(prefix) + 1:].strip(), prefix
    return stripped, None


def _component_of_prefix(title: str, category: Category, names: list[str]) -> str | None:
    """The current component that the title's own prefix names."""
    stripped = title.strip().casefold()
    for name in names:
        prefix = (category.dynamic_title_prefix or "").format(component=name).casefold()
        if stripped == prefix or stripped.startswith(prefix + " "):
            return name
    return None


def _without_stale_prefix(title: str, category: Category) -> str:
    """The title without one leading prefix of the category's dynamic shape, whatever component it names."""
    before, _, after = (category.dynamic_title_prefix or "").partition("{component}")
    match = re.match(rf"^{re.escape(before)}[^\]]+{re.escape(after)}(?:\s+|$)", title, re.IGNORECASE)
    return title if match is None else title[match.end():].strip()


def _normalise(event: dict, categories: tuple[Category, ...], inventory: dict[str, list[str]]) -> tuple[str | None, str]:
    if event.get("action") != "opened":
        return None, "not opened"
    issue = event.get("issue") if isinstance(event.get("issue"), dict) else {}
    original = issue.get("title", "")
    if not isinstance(original, str):
        return None, "missing title"
    found = sections(issue.get("body") or "")
    body_key, body_component = category_from_body(found, categories, inventory)
    key = category_from_labels(issue, categories) or body_key
    category = next((item for item in categories if item.key == key), None)
    component, invalid = body_component, False
    if category and category.has_components:
        names = inventory.get(category.key, [])
        if _section_key(category.component_label) in found or "component" in found:
            raw = _first_line(found.get(_section_key(category.component_label), "") or found.get("component", "")) or body_component or ""
            component, invalid = canonical_component(raw, names)
        else:
            component = _component_of_prefix(original, category, names)
            if component is None:
                component, invalid = canonical_component("", names)
    base, existing = _split_known_prefix(original, _known_prefixes(categories, inventory))
    if existing is None and category is not None and category.has_components:
        base = _without_stale_prefix(base, category)
    if category is None:
        if existing is None:
            return None, "unchanged"
        prefix = existing
    elif category.dynamic_title_prefix is None:
        prefix = category.title_prefix
    else:
        prefix = category.dynamic_title_prefix.format(component=component or UNSURE)
    if base == "":
        return prefix, "fallback" if invalid else "cleared"
    title = f"{prefix} {base}"
    if title == original.strip():
        return None, "unchanged"
    return title, "fallback" if invalid else "updated"


def normalised_title(event: dict, root: Path, categories_path: Path) -> tuple[str | None, str]:
    """The new title of the event's issue with ``updated``, ``cleared`` or ``fallback``, or None with the reason."""
    if event.get("action") != "opened":
        return None, "not opened"
    issue = event.get("issue") if isinstance(event.get("issue"), dict) else {}
    if not isinstance(issue.get("title", ""), str):
        return None, "missing title"
    categories = load_categories(categories_path)
    inventory = components(root, categories)
    return _normalise(event, categories, inventory)


# --- the GitHub API -------------------------------------------------------------------------------------------------


def _retry_delay(error: Exception, attempt: int) -> float | None:
    if isinstance(error, urllib.error.HTTPError):
        if error.code != 429 and error.code < 500:
            return None
        header = error.headers.get("Retry-After") if error.headers else None
        if header is not None:
            try:
                seconds = float(header)
                if 0 <= seconds <= MAX_RETRY_AFTER:
                    return seconds
            except ValueError:
                pass
        return RETRY_DELAY_SECONDS * attempt
    if isinstance(error, (urllib.error.URLError, TimeoutError, OSError)):
        return RETRY_DELAY_SECONDS * attempt
    return None


def _request_json(url: str, *, token: str | None = None, method: str = "GET", body: dict | None = None) -> object:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT, "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - the API host of the run
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as error:
            delay = _retry_delay(error, attempt) if attempt < RETRY_ATTEMPTS else None
            if delay is None:
                raise
            time.sleep(delay)
    raise AssertionError("unreachable")


def _api_url() -> str:
    return os.environ.get("GITHUB_API_URL", GITHUB_API_URL).rstrip("/")


def _contents_url(repository: str, relative_path: str, ref: str) -> str:
    quoted = "/".join(urllib.parse.quote(segment, safe="") for segment in relative_path.strip("/").split("/") if segment)
    return f"{_api_url()}/repos/{repository}/contents/{quoted}?ref={urllib.parse.quote(ref, safe='')}"


def _read_api_file(repository: str, relative_path: str, ref: str, token: str | None) -> str | None:
    try:
        payload = _request_json(_contents_url(repository, relative_path, ref), token=token)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise
    if not isinstance(payload, dict) or payload.get("type") != "file" or payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
        raise CategoryError(f"{relative_path}: unexpected GitHub contents response")
    return base64.b64decode(payload["content"]).decode("utf-8")


def _relative_inside(folder: str, value: str, field: str) -> str:
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value.replace("\\", "/"))
    if windows.drive or windows.root or posix.is_absolute() or ".." in posix.parts:
        raise CategoryError(f"{folder}: {field} {value!r} outside product directory")
    return f"{folder}/{posix.as_posix()}"


def components_from_api(repository: str, ref: str, token: str | None, categories: tuple[Category, ...]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for category in categories:
        if not category.has_components:
            continue
        names: list[str] = []
        try:
            entries = _request_json(_contents_url(repository, category.source_directory, ref), token=token)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                found[category.key] = []
                continue
            raise
        folders = sorted((entry["name"] for entry in entries if isinstance(entry, dict) and entry.get("type") == "dir" and isinstance(entry.get("name"), str)),
                         key=str.casefold) if isinstance(entries, list) else []
        for folder in folders:
            folder_path = f"{category.source_directory.rstrip('/')}/{folder}"
            marker_path = _relative_inside(folder_path, category.marker, "marker")
            marker_text = _read_api_file(repository, marker_path, ref, token)
            if marker_text is None:
                continue
            metadata = _marker_metadata_text(marker_text, marker_path, category.required_metadata)
            name = metadata.get("name", folder)
            if name != folder:
                raise CategoryError(f"{marker_path}: canonical name {name!r} does not match directory {folder!r}")
            if "entrypoint" in category.required_metadata:
                entry_path = _relative_inside(folder_path, metadata["entrypoint"], "entrypoint")
                if _read_api_file(repository, entry_path, ref, token) is None:
                    raise CategoryError(f"{marker_path}: missing entrypoint {metadata['entrypoint']!r}")
            if any(other.casefold() == name.casefold() for other in names):
                raise CategoryError(f"{marker_path}: duplicate canonical component name {name!r}")
            names.append(name)
        found[category.key] = sorted(names, key=str.casefold)
    return found


def read_issue_title(token: str, repository: str, number: int) -> str:
    payload = _request_json(f"{_api_url()}/repos/{repository}/issues/{number}", token=token)
    if not isinstance(payload, dict) or not isinstance(payload.get("title"), str):
        raise ValueError(f"issue #{number} response has no title")
    return payload["title"]


def update_title(token: str, repository: str, number: int, title: str) -> None:
    _request_json(f"{_api_url()}/repos/{repository}/issues/{number}", token=token, method="PATCH", body={"title": title})


def parser() -> argparse.ArgumentParser:
    main_parser = argparse.ArgumentParser(description="Give an opened issue the title prefix of its category.")
    main_parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_PATH", ""), help="the payload of the issues event")
    main_parser.add_argument("--root", default=".", help="the repository's checkout, where the components are")
    main_parser.add_argument("--ref", default="", help="read the category file from GitHub at this ref instead of a local checkout")
    main_parser.add_argument("--categories", required=True, help="the repository's category file")
    main_parser.add_argument("--apply", action="store_true", help="change the title of the issue through the GitHub API")
    return main_parser


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    try:
        event = json.loads(Path(options.event).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"the event payload {options.event} cannot be read: {error}", file=sys.stderr)
        return 1
    if not isinstance(event, dict):
        print(f"the event payload {options.event} is no JSON object", file=sys.stderr)
        return 1
    repository = (event.get("repository") or {}).get("full_name")
    token = os.environ.get("GITHUB_TOKEN")
    try:
        if options.ref:
            if event.get("action") != "opened":
                title, state = None, "not opened"
            elif not repository:
                print("missing repository in event payload", file=sys.stderr)
                return 1
            else:
                text = _read_api_file(repository, options.categories, options.ref, token)
                if text is None:
                    raise CategoryError(f"the category file {options.categories} was not found at {options.ref}")
                categories = load_categories_text(text, options.categories)
                inventory = components_from_api(repository, options.ref, token, categories)
                title, state = _normalise(event, categories, inventory)
        else:
            title, state = normalised_title(event, Path(options.root), Path(options.categories))
    except (CategoryError, OSError, urllib.error.URLError) as error:
        print(error, file=sys.stderr)
        return 1
    if title is None:
        print(state)
        return 0
    print(title)
    if state == "fallback":
        print("warning: invalid or stale component; applied the category unsure prefix", file=sys.stderr)
    if not options.apply:
        return 0
    if not token:
        print("apply requested but GITHUB_TOKEN is not set")
        return 1
    number = (event.get("issue") or {}).get("number")
    if not number or not repository:
        print("missing issue number or repository in event payload")
        return 1
    original = (event.get("issue") or {}).get("title", "")
    try:
        current = read_issue_title(token, repository, number)
        if current != original:
            print("unchanged: title changed since event")
            return 0
        update_title(token, repository, number, title)
    except urllib.error.HTTPError as error:
        print(f"failed to update issue title: {error.code} {error.read().decode('utf-8', errors='replace')}")
        return 1
    except (urllib.error.URLError, ValueError, OSError) as error:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        print(f"failed to update issue title: {reason}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
