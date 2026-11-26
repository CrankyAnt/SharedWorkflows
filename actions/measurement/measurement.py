#!/usr/bin/env python3
"""Measure the shared CI goal for every consumer repository.

    python3 measurement.py measure [--own-repository OWNER/NAME] [common options]
    python3 measurement.py measure --local --repository OWNER/NAME [--repository ...] [common options]
    python3 measurement.py report --store FILE [--window-days DAYS] [--now TIME]
                                  [--repository OWNER/NAME ...] [--summary FILE] [--json-output FILE]
    python3 measurement.py failure --own-repository OWNER/NAME --run-url URL [--api-url URL] [--summary FILE]

    common options: [--api-url URL] [--workflow FILE] [--window-days DAYS] [--now TIME]
                    [--store-in FILE] [--store-out FILE] [--workers N] [--max-rps N]
                    [--time-budget SECONDS] [--request-budget N] [--ratelimit-reserve N]
                    [--summary FILE] [--json-output FILE]
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import datetime
import enum
import http.client
import io
import json
import math
import os
import re
import ssl
import statistics
import sys
import threading
import time
import urllib.parse
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

API_TIMEOUT = 30
PAGE = 100
CREATED_SEARCH_CAP = 1000
DEFAULT_WINDOW_DAYS = 28
DEFAULT_RETENTION_DAYS = 90
CI_PLAN_RETENTION_DAYS = 30
SETTLE_MINUTES = 30
LISTING_OVERLAP_DAYS = 1
FINDING_COOLDOWN_DAYS = 7
DEFAULT_WORKFLOW = "ci.yml"
DEFAULT_WORKERS = 6
DEFAULT_MAX_RPS = 10.0
DEFAULT_TIME_BUDGET_SECONDS = 480.0
DEFAULT_REQUEST_BUDGET = 4000
DEFAULT_RATELIMIT_RESERVE = 500
DEFAULT_MIN_SECONDARY_WAIT_SECONDS = 60.0
MAX_SECONDARY_WAIT_SECONDS = 120.0
MAX_REDIRECTS = 5

SCHEMA_VERSION = 1
JUDGE_VERSION = 1
MEASUREMENT_STORE_ARTIFACT = "measurement-records"
MEASUREMENT_STORE_FILENAME = "measurement-records.json"

THREE_MINUTE_SECONDS = 180.0
THREE_MINUTE_TARGET = 0.99
ONE_MINUTE_SECONDS = 60.0
PLAN_ARTIFACT_NAME = "ci-plan"
INPUTS_UNKNOWN_REASON = "inputs unknown"
TEST_PARTS = frozenset({"python-test", "dotnet-test", "node-test"})
# The part whose tests are its units: it runs for a language step alone too, so its test units decide.
SUITE_SELECTION_PART = "suite-selection"
VERDICT_CONCLUSIONS = frozenset({"success", "failure"})
MANUAL_EVENTS = frozenset({"workflow_dispatch"})
ENTRY_FILE_STEP = "Run the shard's entry files under the recording, two at a time"
PYTHON_LANGUAGE_STEP = "Run the Python language step outside the recording"
SHARD_JOB_PATTERN = re.compile(r"^shard (\d+)$")
FIXED_COST_QUANTILE = 0.95
SUITE_SELECTION_SCRIPT = Path(__file__).resolve().parent.parent / "suite-selection" / "suite_selection.py"
MARKER = "<!-- measurement-finding: {key} -->"
MARKER_PATTERN = re.compile(r"<!-- measurement-finding: (\S+) -->")

RECORD_STATES = frozenset({"pending", "unfinished", "verdict", "no-verdict", "excluded-cancelled"})
FROZEN_STATES = frozenset({"verdict", "no-verdict", "excluded-cancelled"})


def _suite_selection_constant(name: str) -> float:
    """Read a numeric constant from the shared suite-selection code of this checkout."""
    text = SUITE_SELECTION_SCRIPT.read_text(encoding="utf-8")
    match = re.search(rf"(?m)^{re.escape(name)}\s*=\s*([0-9]+(?:\.[0-9]+)?)\b", text)
    if match is None:
        raise SetupError(f"{name} not found in {SUITE_SELECTION_SCRIPT}")
    return float(match.group(1))


def assumed_fixed_cost_seconds() -> float:
    """Read ``FIXED_COST_SECONDS`` from the shared suite-selection code of this checkout."""
    return _suite_selection_constant("FIXED_COST_SECONDS")


def language_step_allowance_seconds() -> float:
    """Read ``LANGUAGE_STEP_ALLOWANCE_SECONDS`` from the shared suite-selection code of this checkout."""
    return _suite_selection_constant("LANGUAGE_STEP_ALLOWANCE_SECONDS")


class SetupError(Exception):
    """The command arguments or tokens cannot be used; the message says why."""


class MeasurementError(Exception):
    """A repository could not be measured or an API request failed; the message says why."""


class StoreInvalidError(Exception):
    """A measurement store file or artifact failed structural or schema validation."""


class BudgetStoppedError(Exception):
    """Fetching stopped cleanly because a time, request or rate-limit budget was reached."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RateLimitError(MeasurementError):
    """GitHub returned HTTP 403 or 429 for a primary or secondary rate limit."""

    def __init__(self, message: str, *, kind: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


def format_moment(value: datetime.datetime) -> str:
    return value.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def moment(text: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def token(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SetupError(f"the environment holds no token in {name}")
    return value


class RatePacer:
    """Global pace limiter across worker threads."""

    def __init__(self, max_rps: float) -> None:
        self.interval = (1.0 / max_rps) if max_rps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_time = 0.0

    def wait(self, deadline: float | None = None, stop_event: threading.Event | None = None) -> bool:
        """Reserve a paced slot. Return False if waiting would pass ``deadline`` or ``stop_event`` is set."""
        if self.interval <= 0.0:
            if stop_event is not None and stop_event.is_set():
                return False
            return deadline is None or time.monotonic() < deadline
        with self._lock:
            if stop_event is not None and stop_event.is_set():
                return False
            now = time.monotonic()
            slot = max(now, self._next_time)
            if deadline is not None and slot >= deadline:
                return False
            self._next_time = slot + self.interval
            delay = slot - now
        if delay > 0.0:
            if stop_event is not None:
                if stop_event.wait(delay):
                    return False
            else:
                time.sleep(delay)
        if stop_event is not None and stop_event.is_set():
            return False
        return deadline is None or time.monotonic() < deadline


class BudgetController:
    """Shared time, request and rate-limit budget across all fetch workers."""

    def __init__(
        self,
        *,
        deadline: float | None,
        request_budget: int | None,
        ratelimit_reserve: int,
        min_secondary_wait: float,
    ) -> None:
        self.deadline = deadline
        self.request_budget = request_budget
        self.ratelimit_reserve = ratelimit_reserve
        self.min_secondary_wait = min_secondary_wait
        self._lock = threading.Lock()
        self.stop_event = threading.Event()
        self.stop_reason: str | None = None
        self.rate_limit_failure: str | None = None
        self._secondary_waited = False
        self.secondary_wait_seconds: float = 0.0

    def check_before_request(self, request_count: int, ratelimit_remaining: int | None) -> None:
        with self._lock:
            if self.stop_reason is not None:
                raise BudgetStoppedError(self.stop_reason)
            if self.deadline is not None and time.monotonic() >= self.deadline:
                self.stop_reason = "time budget reached"
                self.stop_event.set()
                raise BudgetStoppedError(self.stop_reason)
            if self.request_budget is not None and request_count >= self.request_budget:
                self.stop_reason = f"request budget reached ({request_count}/{self.request_budget})"
                self.stop_event.set()
                raise BudgetStoppedError(self.stop_reason)
            if (
                ratelimit_remaining is not None
                and self.ratelimit_reserve > 0
                and ratelimit_remaining < self.ratelimit_reserve
            ):
                self.stop_reason = f"rate-limit reserve reached ({ratelimit_remaining} remaining < {self.ratelimit_reserve})"
                self.stop_event.set()
                raise BudgetStoppedError(self.stop_reason)

    def note_deadline_exceeded(self) -> None:
        with self._lock:
            if self.stop_reason is None:
                self.stop_reason = "time budget reached"
                self.stop_event.set()

    def handle_rate_limit(self, error: RateLimitError) -> None:
        wait_for = 0.0
        with self._lock:
            if self.stop_reason is None:
                self.stop_reason = str(error)
            if self.rate_limit_failure is None:
                self.rate_limit_failure = str(error)
            self.stop_event.set()
            if error.kind == "secondary" and not self._secondary_waited:
                self._secondary_waited = True
                raw_wait = max(error.retry_after or 0.0, self.min_secondary_wait)
                wait_for = min(raw_wait, MAX_SECONDARY_WAIT_SECONDS)
                self.secondary_wait_seconds = wait_for
        if wait_for > 0.0:
            time.sleep(wait_for)


class Api:
    """The GitHub REST API, asked with one token, reusing HTTP(S) connections per thread."""

    def __init__(
        self,
        base: str,
        token_value: str,
        *,
        pacer: RatePacer | None = None,
        budget: BudgetController | None = None,
    ) -> None:
        self.base = base.rstrip("/")
        self.token = token_value
        parsed = urllib.parse.urlsplit(self.base)
        self._scheme = parsed.scheme.lower()
        self._host = parsed.hostname or ""
        self._port = parsed.port
        self._base_path = parsed.path.rstrip("/")
        self._origin = parsed.netloc
        self._ssl_context = ssl.create_default_context() if self._scheme == "https" else None
        self._local = threading.local()
        self._connections_lock = threading.Lock()
        self._connections: list[http.client.HTTPConnection] = []
        self._metrics_lock = threading.Lock()
        self.request_count = 0
        self.ratelimit_remaining: int | None = None
        self.ratelimit_reset: int | None = None
        self.pacer = pacer
        self.budget = budget

    def close(self) -> None:
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for conn in connections:
            try:
                conn.close()
            except OSError:
                pass

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        return {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "SharedWorkflows-Measurement",
            "Connection": "keep-alive",
        }

    def _get_connection(self) -> http.client.HTTPConnection:
        conn: http.client.HTTPConnection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open_connection(self._scheme, self._host, self._port)
            self._local.conn = conn
            with self._connections_lock:
                self._connections.append(conn)
        return conn

    def _reset_connection(self) -> None:
        conn: http.client.HTTPConnection | None = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
            self._local.conn = None

    def _open_connection(self, scheme: str, host: str, port: int | None) -> http.client.HTTPConnection:
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=API_TIMEOUT, context=self._ssl_context)
        return http.client.HTTPConnection(host, port, timeout=API_TIMEOUT)

    def _record_ratelimit(self, headers: http.client.HTTPMessage) -> int | None:
        rem_raw = headers.get("X-RateLimit-Remaining")
        reset_raw = headers.get("X-RateLimit-Reset")
        remaining: int | None = None
        if rem_raw is not None:
            try:
                remaining = int(rem_raw.strip())
            except ValueError:
                remaining = None
        reset_val: int | None = None
        if reset_raw is not None:
            try:
                reset_val = int(reset_raw.strip())
            except ValueError:
                reset_val = None
        with self._metrics_lock:
            if remaining is not None:
                self.ratelimit_remaining = remaining
            if reset_val is not None:
                self.ratelimit_reset = reset_val
        return remaining

    def _before_request(self) -> None:
        if self.budget is not None:
            with self._metrics_lock:
                count = self.request_count
                rem = self.ratelimit_remaining
            self.budget.check_before_request(count, rem)
        if self.pacer is not None:
            deadline = self.budget.deadline if self.budget is not None else None
            stop_event = self.budget.stop_event if self.budget is not None else None
            if not self.pacer.wait(deadline=deadline, stop_event=stop_event):
                if self.budget is not None:
                    self.budget.note_deadline_exceeded()
                    raise BudgetStoppedError(self.budget.stop_reason or "time budget reached")
                raise BudgetStoppedError("time budget reached")
        with self._metrics_lock:
            self.request_count += 1

    def _check_rate_limit_error(
        self,
        method: str,
        path: str,
        status: int,
        headers: http.client.HTTPMessage,
        body_bytes: bytes,
        remaining: int | None,
    ) -> RateLimitError | None:
        if status not in (403, 429):
            return None
        retry_after_raw = headers.get("Retry-After")
        retry_after: float | None = None
        if retry_after_raw is not None:
            try:
                retry_after = float(retry_after_raw.strip())
            except ValueError:
                retry_after = None
        message_lower = ""
        try:
            doc = json.loads(body_bytes.decode("utf-8", errors="replace"))
            if isinstance(doc, dict) and isinstance(doc.get("message"), str):
                message_lower = doc["message"].lower()
        except ValueError:
            message_lower = body_bytes.decode("utf-8", errors="replace").lower()
        if remaining == 0:
            return RateLimitError(f"{method} {path}: HTTP {status} primary rate limit exceeded", kind="primary")
        if status == 429 or retry_after is not None or "secondary rate limit" in message_lower:
            return RateLimitError(
                f"{method} {path}: HTTP {status} secondary rate limit exceeded",
                kind="secondary",
                retry_after=retry_after,
            )
        if "rate limit" in message_lower:
            return RateLimitError(f"{method} {path}: HTTP {status} primary rate limit exceeded", kind="primary")
        return None

    def _raw_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
    ) -> tuple[int, http.client.HTTPMessage, bytes]:
        self._before_request()
        target = f"{self._base_path}/{path.lstrip('/')}"
        attempts = 2 if method == "GET" else 1
        last_error: Exception | None = None
        for attempt in range(attempts):
            conn = self._get_connection()
            try:
                conn.request(method, target, body=body, headers=headers)
                response = conn.getresponse()
                payload = response.read()
                resp_headers = response.headers
                status = response.status
                if response.will_close:
                    self._reset_connection()
                return status, resp_headers, payload
            except (http.client.RemoteDisconnected, http.client.BadStatusLine, ConnectionResetError, BrokenPipeError) as error:
                self._reset_connection()
                last_error = error
                if attempt + 1 < attempts:
                    continue
                raise MeasurementError(f"{method} {path}: {error}") from error
            except (http.client.HTTPException, OSError) as error:
                self._reset_connection()
                raise MeasurementError(f"{method} {path}: {error}") from error
        raise MeasurementError(f"{method} {path}: {last_error}")

    def request_json(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        headers = self._headers()
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, resp_headers, payload = self._raw_request(method, path, headers=headers, body=data)
        remaining = self._record_ratelimit(resp_headers)
        if status == 404:
            return 404, None
        rate_error = self._check_rate_limit_error(method, path, status, resp_headers, payload, remaining)
        if rate_error is not None:
            raise rate_error
        if status < 200 or status >= 300:
            raise MeasurementError(f"{method} {path}: HTTP {status}")
        try:
            text = payload.decode("utf-8")
            return status, json.loads(text) if text else None
        except (UnicodeDecodeError, ValueError) as error:
            raise MeasurementError(f"{method} {path}: {error}") from error

    def get(self, path: str) -> object:
        return self.request_json("GET", path)[1]

    def send(self, method: str, path: str, body: dict) -> dict:
        status, document = self.request_json(method, path, body)
        if status == 404 or not isinstance(document, dict):
            raise MeasurementError(f"{method} {path}: GitHub does not know this address, or the token may not use it")
        return document

    def pages(self, path: str, key: str | None = None, *, allow_missing: bool = False) -> list[dict] | None:
        items: list[dict] = []
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            status, document = self.request_json("GET", f"{path}{separator}per_page={PAGE}&page={page}")
            if status == 404:
                if allow_missing and page == 1:
                    return None
                raise MeasurementError(f"GET {path}: HTTP 404")
            found = document.get(key) if key and isinstance(document, dict) else document
            if not isinstance(found, list):
                raise MeasurementError(f"GET {path}: expected a list")
            items += [item for item in found if isinstance(item, dict)]
            if len(found) < PAGE:
                return items
            page += 1

    def get_bytes(self, path: str) -> bytes | None:
        """Download binary content from ``path`` (e.g. an artifact zip), returning None on 404/410."""
        headers = self._headers()
        status, resp_headers, payload = self._raw_request("GET", path, headers=headers)
        remaining = self._record_ratelimit(resp_headers)
        current_url = f"{self.base}/{path.lstrip('/')}"
        for _ in range(MAX_REDIRECTS):
            if status in (301, 302, 303, 307, 308):
                location = resp_headers.get("Location")
                if not location:
                    raise MeasurementError(f"GET {path}: HTTP {status} without Location")
                next_url = urllib.parse.urljoin(current_url, location)
                parsed = urllib.parse.urlsplit(next_url)
                redirect_headers = self._headers() if parsed.netloc == self._origin else {"User-Agent": "SharedWorkflows-Measurement"}
                conn = self._open_connection(parsed.scheme.lower(), parsed.hostname or "", parsed.port)
                try:
                    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
                    conn.request("GET", target or "/", headers=redirect_headers)
                    response = conn.getresponse()
                    status = response.status
                    resp_headers = response.headers
                    payload = response.read()
                except (http.client.HTTPException, OSError) as error:
                    raise MeasurementError(f"GET {path}: {error}") from error
                finally:
                    try:
                        conn.close()
                    except OSError:
                        pass
                current_url = next_url
                continue
            break
        if status in (404, 410):
            return None
        rate_error = self._check_rate_limit_error("GET", path, status, resp_headers, payload, remaining)
        if rate_error is not None:
            raise rate_error
        if status < 200 or status >= 300:
            raise MeasurementError(f"GET {path}: HTTP {status}")
        return payload


def _select_plan_artifact(candidates: list[dict], max_created_at: datetime.datetime) -> dict | None:
    qualified: list[tuple[datetime.datetime, dict]] = []
    for item in candidates:
        if item.get("name") != PLAN_ARTIFACT_NAME or item.get("expired"):
            continue
        if not isinstance(item.get("id"), int):
            continue
        created_raw = item.get("created_at")
        if not isinstance(created_raw, str) or not created_raw:
            continue
        try:
            created_at = moment(created_raw)
        except ValueError:
            continue
        if created_at <= max_created_at:
            qualified.append((created_at, item))
    if not qualified:
        return None
    qualified.sort(key=lambda pair: pair[0])
    return qualified[0][1]


def _download_plan_zip(api: Api, repository: str, artifact_id: int) -> dict | None:
    raw = api.get_bytes(f"repos/{repository}/actions/artifacts/{artifact_id}/zip")
    if not raw:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [name for name in archive.namelist() if name.endswith(".json")]
            if not names:
                return None
            chosen = "ci-plan.json" if "ci-plan.json" in names else names[0]
            document = json.loads(archive.read(chosen).decode("utf-8"))
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, ValueError):
        return None
    return document if isinstance(document, dict) else None


class RepoPlanArtifactIndex:
    """Lazy per-repository index of ``ci-plan`` artifacts fetched at repository scope when cheaper than per-run."""

    def __init__(self, api: Api, repository: str, planned_runs: int) -> None:
        self.api = api
        self.repository = repository
        self.planned_runs = planned_runs
        self._lock = threading.Lock()
        self._loaded = False
        self._use_per_run = planned_runs <= 1
        self._by_run: dict[int, list[dict]] = {}

    def _ensure_loaded(self) -> None:
        with self._lock:
            if self._loaded or self._use_per_run:
                return
            path = f"repos/{self.repository}/actions/artifacts?name={urllib.parse.quote(PLAN_ARTIFACT_NAME)}"
            status, doc = self.api.request_json("GET", f"{path}&per_page={PAGE}&page=1")
            if status == 404 or not isinstance(doc, dict):
                self._loaded = True
                return
            artifacts = doc.get("artifacts")
            if not isinstance(artifacts, list):
                self._use_per_run = True
                self._loaded = True
                return
            total_count = doc.get("total_count")
            total_pages = math.ceil(total_count / PAGE) if isinstance(total_count, int) and total_count > 0 else 1
            if total_pages > self.planned_runs:
                self._use_per_run = True
                self._loaded = True
                return
            all_items = [item for item in artifacts if isinstance(item, dict)]
            page = 2
            while len(artifacts) >= PAGE and page <= total_pages + 1:
                status, doc = self.api.request_json("GET", f"{path}&per_page={PAGE}&page={page}")
                if status == 404 or not isinstance(doc, dict):
                    break
                artifacts = doc.get("artifacts")
                if not isinstance(artifacts, list):
                    break
                all_items.extend(item for item in artifacts if isinstance(item, dict))
                page += 1
            by_run: dict[int, list[dict]] = {}
            missing_run_link = False
            for item in all_items:
                workflow_run = item.get("workflow_run")
                if isinstance(workflow_run, dict) and isinstance(workflow_run.get("id"), int):
                    by_run.setdefault(workflow_run["id"], []).append(item)
                else:
                    missing_run_link = True
            if missing_run_link and not by_run:
                self._use_per_run = True
            else:
                self._by_run = by_run
            self._loaded = True

    def read_plan(self, run_id: int, max_created_at: datetime.datetime) -> dict | None:
        self._ensure_loaded()
        if self._use_per_run:
            return read_plan_artifact(self.api, self.repository, run_id, max_created_at=max_created_at)
        candidates = self._by_run.get(run_id, [])
        artifact = _select_plan_artifact(candidates, max_created_at)
        if artifact is None:
            return None
        return _download_plan_zip(self.api, self.repository, int(artifact["id"]))


def read_plan_artifact(
    api: Api,
    repository: str,
    run_id: int,
    *,
    max_created_at: datetime.datetime | None = None,
) -> dict | None:
    """Read and parse the ``ci-plan`` artifact JSON of ``run_id``, or None when absent, expired or unreadable.
    When ``max_created_at`` is given, only an artifact created no later than that moment qualifies."""
    artifacts = api.pages(
        f"repos/{repository}/actions/runs/{run_id}/artifacts?name={urllib.parse.quote(PLAN_ARTIFACT_NAME)}",
        "artifacts",
        allow_missing=True,
    )
    if not artifacts:
        return None
    if max_created_at is not None:
        artifact = _select_plan_artifact(artifacts, max_created_at)
    else:
        candidates = [item for item in artifacts if item.get("name") == PLAN_ARTIFACT_NAME and not item.get("expired")]
        artifact = candidates[0] if candidates else None
    if artifact is None or not isinstance(artifact.get("id"), int):
        return None
    return _download_plan_zip(api, repository, artifact["id"])


def plan_selects_no_tests(plan: dict) -> bool:
    """Whether ``plan`` selects no test part and no test unit. The suite selection counts by its units: a run that
    plans its job only for a language step selects no test, and one whose units are unknown counts as selecting tests."""
    parts = plan.get("parts")
    if isinstance(parts, dict):
        for name in TEST_PARTS:
            part = parts.get(name)
            if isinstance(part, dict) and part.get("run") is True:
                return False
    units = plan.get("units")
    if isinstance(units, dict):
        if any(isinstance(unit, dict) and unit.get("run") is True for unit in units.values()):
            return False
    elif isinstance(parts, dict) and isinstance(parts.get(SUITE_SELECTION_PART), dict) and parts[SUITE_SELECTION_PART].get("run") is True:
        return False
    return True


def plan_unknown_input_parts(plan: dict) -> set[str]:
    """The parts that ``plan`` reports as running with unknown inputs."""
    found: set[str] = set()
    departures = plan.get("departures")
    if isinstance(departures, list):
        for entry in departures:
            if isinstance(entry, dict) and entry.get("reason") == INPUTS_UNKNOWN_REASON and isinstance(entry.get("part"), str):
                found.add(entry["part"])
    parts = plan.get("parts")
    if isinstance(parts, dict):
        for name, entry in parts.items():
            if isinstance(name, str) and isinstance(entry, dict) and entry.get("run") is True and entry.get("reason") == INPUTS_UNKNOWN_REASON:
                found.add(name)
    return found


def plan_fallbacks(plan: dict) -> dict[str, int]:
    """The fallback counts by reason recorded in ``plan``."""
    raw = plan.get("fallbacks")
    if not isinstance(raw, dict):
        return {}
    return {name: count for name, count in raw.items() if isinstance(name, str) and isinstance(count, int) and count > 0}


def short_job_name(job: dict) -> str:
    return str(job.get("name") or "").rsplit(" / ", 1)[-1]


def verdict_job(jobs: list[dict]) -> dict | None:
    """Return the completed verdict job of a run, or None when the run did not reach a verdict."""
    for job in jobs:
        if short_job_name(job) != "verdict":
            continue
        if job.get("status") == "completed" and job.get("conclusion") in VERDICT_CONCLUSIONS and job.get("completed_at"):
            return job
    return None


class FixedCostOutcome(enum.Enum):
    SAMPLE = "sample"
    NON_SHARD = "non-shard"
    UNMATCHED = "unmatched"
    SKIP = "skip"


@dataclass(frozen=True)
class FixedCostSample:
    fixed_cost: float
    outside_entry_step: float
    step_excess: float
    language_step_seconds: float
    plan_seconds: float
    queue_seconds: float
    shard_to_entry_seconds: float
    after_entry_seconds: float
    results_seconds: float
    verdict_seconds: float

    def to_dict(self) -> dict[str, float]:
        return {
            "fixed_cost": self.fixed_cost,
            "outside_entry_step": self.outside_entry_step,
            "step_excess": self.step_excess,
            "language_step_seconds": self.language_step_seconds,
            "plan_seconds": self.plan_seconds,
            "queue_seconds": self.queue_seconds,
            "shard_to_entry_seconds": self.shard_to_entry_seconds,
            "after_entry_seconds": self.after_entry_seconds,
            "results_seconds": self.results_seconds,
            "verdict_seconds": self.verdict_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict) -> FixedCostSample:
        return cls(
            fixed_cost=float(data["fixed_cost"]),
            outside_entry_step=float(data["outside_entry_step"]),
            step_excess=float(data["step_excess"]),
            language_step_seconds=float(data["language_step_seconds"]),
            plan_seconds=float(data["plan_seconds"]),
            queue_seconds=float(data["queue_seconds"]),
            shard_to_entry_seconds=float(data["shard_to_entry_seconds"]),
            after_entry_seconds=float(data["after_entry_seconds"]),
            results_seconds=float(data["results_seconds"]),
            verdict_seconds=float(data["verdict_seconds"]),
        )

    def phases_document(self) -> dict[str, float]:
        return {
            "plan": round(self.plan_seconds, 1),
            "queue": round(self.queue_seconds, 1),
            "shard_to_entry": round(self.shard_to_entry_seconds, 1),
            "after_entry": round(self.after_entry_seconds, 1),
            "results": round(self.results_seconds, 1),
            "verdict": round(self.verdict_seconds, 1),
        }


def _step_by_name(job: dict, name: str) -> dict | None:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return None
    for step in steps:
        if isinstance(step, dict) and step.get("name") == name:
            return step
    return None


def _successful_step_times(job: dict, name: str) -> tuple[datetime.datetime, datetime.datetime] | None:
    """Return ``(started_at, completed_at)`` for step ``name`` when it completed with conclusion ``success``."""
    step = _step_by_name(job, name)
    if (
        step is None
        or step.get("conclusion") != "success"
        or not isinstance(step.get("started_at"), str)
        or not isinstance(step.get("completed_at"), str)
    ):
        return None
    return moment(step["started_at"]), moment(step["completed_at"])


def _planned_shard_estimate(plan: dict | None, shard_number: int) -> float | None:
    if not isinstance(plan, dict):
        return None
    shards = plan.get("shards")
    if not isinstance(shards, list):
        return None
    for item in shards:
        if (
            isinstance(item, dict)
            and item.get("shard") == shard_number
            and not isinstance(item.get("shard"), bool)
            and isinstance(item.get("estimate"), (int, float))
            and not isinstance(item.get("estimate"), bool)
        ):
            return float(item["estimate"])
    return None


def fixed_cost_of_run(
    created_at: datetime.datetime,
    verdict: dict,
    jobs: list[dict],
    plan: dict | None,
) -> tuple[FixedCostOutcome, FixedCostSample | None]:
    """Classify a verdict run for fixed-cost measurement."""
    shard_jobs: list[tuple[int, datetime.datetime, dict]] = []
    competing: list[tuple[str, datetime.datetime]] = []
    plan_job: dict | None = None
    results_job: dict | None = None
    for job in jobs:
        if not isinstance(job, dict) or job.get("status") != "completed" or job.get("conclusion") == "skipped":
            continue
        completed_raw = job.get("completed_at")
        if not isinstance(completed_raw, str) or not completed_raw:
            continue
        short = short_job_name(job)
        if short == "verdict":
            continue
        if short == "plan":
            plan_job = job
            continue
        if short == "suite-selection-results":
            results_job = job
            continue
        match = SHARD_JOB_PATTERN.fullmatch(short)
        if match:
            shard_jobs.append((int(match.group(1)), moment(completed_raw), job))
        else:
            competing.append((short, moment(completed_raw)))

    if not shard_jobs:
        return (FixedCostOutcome.NON_SHARD, None) if competing else (FixedCostOutcome.SKIP, None)

    # An empty language-step-only shard skips the entry-file step; skip the run rather than counting it as unmatched.
    if all(
        (step := _step_by_name(job, ENTRY_FILE_STEP)) is not None and step.get("conclusion") == "skipped"
        for _, _, job in shard_jobs
    ):
        return FixedCostOutcome.SKIP, None

    shard_number, shard_end, critical_shard = max(shard_jobs, key=lambda item: item[1])
    results_end: datetime.datetime | None = None
    if results_job is not None:
        candidate = moment(str(results_job["completed_at"]))
        if candidate >= shard_end:
            results_end = candidate

    for short, other_end in competing:
        if short == "windows":
            if other_end > shard_end:
                return FixedCostOutcome.NON_SHARD, None
        elif other_end > (results_end or shard_end):
            return FixedCostOutcome.NON_SHARD, None

    entry_times = _successful_step_times(critical_shard, ENTRY_FILE_STEP)
    shard_started_raw = critical_shard.get("started_at")
    if entry_times is None or not isinstance(shard_started_raw, str) or not shard_started_raw:
        return FixedCostOutcome.UNMATCHED, None

    planned_estimate = _planned_shard_estimate(plan, shard_number)
    if planned_estimate is None or plan_job is None or not isinstance(plan_job.get("completed_at"), str):
        return FixedCostOutcome.UNMATCHED, None

    entry_start, entry_end = entry_times
    shard_start = moment(shard_started_raw)
    plan_end = moment(str(plan_job["completed_at"]))
    verdict_end = moment(str(verdict["completed_at"]))

    language_step_times = _successful_step_times(critical_shard, PYTHON_LANGUAGE_STEP)
    language_step_seconds = (
        max(0.0, (language_step_times[1] - language_step_times[0]).total_seconds())
        if language_step_times is not None
        else 0.0
    )

    run_duration = max(0.0, (verdict_end - created_at).total_seconds())
    entry_duration = max(0.0, (entry_end - entry_start).total_seconds())
    outside = max(0.0, run_duration - entry_duration - language_step_seconds)
    excess = entry_duration - planned_estimate
    return (
        FixedCostOutcome.SAMPLE,
        FixedCostSample(
            fixed_cost=outside + excess,
            outside_entry_step=outside,
            step_excess=excess,
            language_step_seconds=language_step_seconds,
            plan_seconds=max(0.0, (plan_end - created_at).total_seconds()),
            queue_seconds=max(0.0, (shard_start - plan_end).total_seconds()),
            shard_to_entry_seconds=max(0.0, (entry_start - shard_start).total_seconds()),
            after_entry_seconds=max(0.0, (shard_end - entry_end).total_seconds() - language_step_seconds),
            results_seconds=max(0.0, (results_end - shard_end).total_seconds()) if results_end is not None else 0.0,
            verdict_seconds=max(0.0, (verdict_end - (results_end or shard_end)).total_seconds()),
        ),
    )


# ---------------------------------------------------------------------------
# Incremental store: schema, migration, validation, pruning and persistence
# ---------------------------------------------------------------------------


def empty_store(now: datetime.datetime) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "judge_version": JUDGE_VERSION,
        "retention_days": DEFAULT_RETENTION_DAYS,
        "generated_at": format_moment(now),
        "repositories": {},
    }


def _empty_repo_entry() -> dict:
    return {
        "has_workflow": True,
        "listed_from": None,
        "listed_to": None,
        "records": {},
    }


def _validate_fixed_cost_sample(raw: object) -> dict[str, float]:
    if not isinstance(raw, dict):
        raise StoreInvalidError("fixed_cost_sample must be an object")
    required = (
        "fixed_cost",
        "outside_entry_step",
        "step_excess",
        "language_step_seconds",
        "plan_seconds",
        "queue_seconds",
        "shard_to_entry_seconds",
        "after_entry_seconds",
        "results_seconds",
        "verdict_seconds",
    )
    out: dict[str, float] = {}
    for key in required:
        val = raw.get(key)
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            raise StoreInvalidError(f"fixed_cost_sample.{key} must be numeric")
        out[key] = float(val)
    return out


def _normalize_record(key: str, raw: object, *, migrating: bool) -> dict:
    if not isinstance(raw, dict):
        raise StoreInvalidError(f"record {key} must be an object")
    run_id = raw.get("run_id")
    if not isinstance(run_id, int) or isinstance(run_id, bool):
        raise StoreInvalidError(f"record {key} lacks integer run_id")
    created_raw = raw.get("created_at")
    if not isinstance(created_raw, str) or not created_raw:
        raise StoreInvalidError(f"record {key} lacks created_at")
    try:
        moment(created_raw)
    except ValueError as error:
        raise StoreInvalidError(f"record {key} has invalid created_at") from error
    state = raw.get("state")
    if not isinstance(state, str) or state not in RECORD_STATES:
        if migrating:
            state = "pending"
        else:
            raise StoreInvalidError(f"record {key} has invalid state {state!r}")
    attempt = raw.get("run_attempt", 1)
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        if migrating:
            attempt = 1
        else:
            raise StoreInvalidError(f"record {key} has invalid run_attempt")
    status = raw.get("status")
    status_str = status if isinstance(status, str) else "unknown"
    conclusion = raw.get("conclusion")
    conclusion_val = conclusion if isinstance(conclusion, str) else None
    updated_at = raw.get("updated_at")
    updated_val = updated_at if isinstance(updated_at, str) else None
    judged_at = raw.get("judged_at")
    if judged_at is not None:
        if not isinstance(judged_at, str):
            raise StoreInvalidError(f"record {key} has invalid judged_at")
        try:
            moment(judged_at)
        except ValueError as error:
            raise StoreInvalidError(f"record {key} has invalid judged_at") from error
    judge_version = raw.get("judge_version")
    if judge_version is not None and (not isinstance(judge_version, int) or isinstance(judge_version, bool)):
        if migrating:
            judge_version = 0
        else:
            raise StoreInvalidError(f"record {key} has invalid judge_version")

    normalized: dict = {
        "run_id": run_id,
        "created_at": created_raw,
        "run_attempt": attempt,
        "status": status_str,
        "conclusion": conclusion_val,
        "updated_at": updated_val,
        "state": state,
        "verdict_completed_at": None,
        "duration_seconds": None,
        "plan_found": None,
        "selects_no_tests": None,
        "fallbacks": {},
        "unknown_input_parts": [],
        "fixed_cost_outcome": None,
        "fixed_cost_sample": None,
        "judged_at": judged_at,
        "judge_version": judge_version,
    }
    if state == "verdict":
        vc_raw = raw.get("verdict_completed_at")
        dur_raw = raw.get("duration_seconds")
        valid_verdict = (
            isinstance(vc_raw, str)
            and bool(vc_raw)
            and isinstance(dur_raw, (int, float))
            and not isinstance(dur_raw, bool)
        )
        if valid_verdict:
            try:
                moment(str(vc_raw))
            except ValueError:
                valid_verdict = False
        if not valid_verdict:
            if migrating:
                normalized["state"] = "pending"
                normalized["judge_version"] = None
                return normalized
            raise StoreInvalidError(f"verdict record {key} lacks valid verdict_completed_at/duration_seconds")
        normalized["verdict_completed_at"] = vc_raw
        normalized["duration_seconds"] = float(dur_raw)
        plan_found = raw.get("plan_found")
        if isinstance(plan_found, bool):
            normalized["plan_found"] = plan_found
        elif migrating:
            normalized["plan_found"] = raw.get("selects_no_tests") is not None
        else:
            raise StoreInvalidError(f"verdict record {key} lacks boolean plan_found")
        selects_no_tests = raw.get("selects_no_tests")
        if selects_no_tests is not None and not isinstance(selects_no_tests, bool):
            if migrating:
                selects_no_tests = None
            else:
                raise StoreInvalidError(f"verdict record {key} has invalid selects_no_tests")
        normalized["selects_no_tests"] = selects_no_tests
        fb = raw.get("fallbacks", {})
        if isinstance(fb, dict):
            normalized["fallbacks"] = {
                k: v for k, v in fb.items() if isinstance(k, str) and isinstance(v, int) and not isinstance(v, bool) and v > 0
            }
        elif not migrating:
            raise StoreInvalidError(f"verdict record {key} has invalid fallbacks")
        uip = raw.get("unknown_input_parts", [])
        if isinstance(uip, list):
            normalized["unknown_input_parts"] = sorted({p for p in uip if isinstance(p, str)})
        elif not migrating:
            raise StoreInvalidError(f"verdict record {key} has invalid unknown_input_parts")
        fco = raw.get("fixed_cost_outcome")
        valid_outcomes = {o.value for o in FixedCostOutcome}
        if fco is None and migrating:
            normalized["fixed_cost_outcome"] = FixedCostOutcome.SKIP.value
        elif isinstance(fco, str) and fco in valid_outcomes:
            normalized["fixed_cost_outcome"] = fco
        else:
            raise StoreInvalidError(f"verdict record {key} has invalid fixed_cost_outcome {fco!r}")
        if normalized["fixed_cost_outcome"] == FixedCostOutcome.SAMPLE.value:
            try:
                normalized["fixed_cost_sample"] = _validate_fixed_cost_sample(raw.get("fixed_cost_sample"))
            except StoreInvalidError:
                if migrating:
                    normalized["fixed_cost_outcome"] = FixedCostOutcome.SKIP.value
                    normalized["fixed_cost_sample"] = None
                else:
                    raise
        if normalized["judge_version"] is None:
            normalized["judge_version"] = 0 if migrating else JUDGE_VERSION
    elif state in FROZEN_STATES and normalized["judge_version"] is None:
        normalized["judge_version"] = 0 if migrating else JUDGE_VERSION
    return normalized


def validate_and_migrate_store(document: object) -> tuple[dict, int | None]:
    """Validate a store JSON document, migrating older schema versions forward rather than treating them as unreadable."""
    if not isinstance(document, dict):
        raise StoreInvalidError("store root must be a JSON object")
    version = document.get("schema_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 0 or version > SCHEMA_VERSION:
        raise StoreInvalidError(f"unsupported schema_version {version!r}")
    migrating = version < SCHEMA_VERSION
    repos_raw = document.get("repositories")
    if not isinstance(repos_raw, dict):
        raise StoreInvalidError("store repositories must be an object")
    repos_out: dict[str, dict] = {}
    for repo_name, repo_doc in repos_raw.items():
        if not isinstance(repo_name, str) or not isinstance(repo_doc, dict):
            raise StoreInvalidError(f"invalid repository entry {repo_name!r}")
        has_workflow = repo_doc.get("has_workflow", True)
        if not isinstance(has_workflow, bool):
            raise StoreInvalidError(f"{repo_name}: has_workflow must be boolean")
        listed_from = repo_doc.get("listed_from")
        listed_to = repo_doc.get("listed_to")
        for label, value in (("listed_from", listed_from), ("listed_to", listed_to)):
            if value is not None:
                if not isinstance(value, str):
                    raise StoreInvalidError(f"{repo_name}: {label} must be a timestamp string or null")
                try:
                    moment(value)
                except ValueError as error:
                    raise StoreInvalidError(f"{repo_name}: invalid {label}") from error
        records_raw = repo_doc.get("records", {})
        if not isinstance(records_raw, dict):
            raise StoreInvalidError(f"{repo_name}: records must be an object")
        records_out: dict[str, dict] = {}
        for rec_key, rec_val in records_raw.items():
            norm = _normalize_record(str(rec_key), rec_val, migrating=migrating)
            records_out[str(norm["run_id"])] = norm
        repos_out[repo_name] = {
            "has_workflow": has_workflow,
            "listed_from": listed_from,
            "listed_to": listed_to,
            "records": records_out,
        }
    generated_at = document.get("generated_at")
    if generated_at is not None:
        if not isinstance(generated_at, str):
            raise StoreInvalidError("generated_at must be a timestamp string")
        try:
            moment(generated_at)
        except ValueError as error:
            raise StoreInvalidError("invalid generated_at") from error
    migrated = {
        "schema_version": SCHEMA_VERSION,
        "judge_version": document.get("judge_version") if isinstance(document.get("judge_version"), int) else JUDGE_VERSION,
        "retention_days": DEFAULT_RETENTION_DAYS,
        "generated_at": generated_at,
        "repositories": repos_out,
    }
    return migrated, (version if migrating else None)


def prune_store(store: dict, now: datetime.datetime, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
    """Drop records older than ``retention_days`` before ``now`` and clamp listing bounds."""
    retention_cutoff = now - datetime.timedelta(days=retention_days)
    for repo_entry in store["repositories"].values():
        kept: dict[str, dict] = {}
        for key, rec in repo_entry["records"].items():
            if moment(rec["created_at"]) >= retention_cutoff:
                kept[key] = rec
        repo_entry["records"] = kept
        listed_to = repo_entry.get("listed_to")
        listed_from = repo_entry.get("listed_from")
        if listed_to is not None and moment(listed_to) < retention_cutoff:
            repo_entry["listed_from"] = None
            repo_entry["listed_to"] = None
        elif listed_from is not None and moment(listed_from) < retention_cutoff:
            repo_entry["listed_from"] = format_moment(retention_cutoff)


def write_store_atomic(path: Path, store: dict, now: datetime.datetime) -> None:
    """Atomically write the store JSON document so a partial run always leaves a consistent file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    store["schema_version"] = SCHEMA_VERSION
    store["judge_version"] = JUDGE_VERSION
    store["retention_days"] = DEFAULT_RETENTION_DAYS
    store["generated_at"] = format_moment(now)
    encoded = json.dumps(store, separators=(",", ":"), sort_keys=True) + "\n"
    tmp_path = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            tmp_path.unlink()
        raise


@dataclass(frozen=True)
class StoreLoadInfo:
    source: str
    artifact_id: int | None = None
    created_at: datetime.datetime | None = None
    skipped_invalid_ids: tuple[int, ...] = ()
    migrated_from: int | None = None
    is_buildup: bool = False
    path: str | None = None

    def age_seconds(self, now: datetime.datetime) -> float | None:
        if self.created_at is None:
            return None
        return max(0.0, (now - self.created_at).total_seconds())

    def format_line(self, now: datetime.datetime) -> str:
        extras: list[str] = []
        if self.skipped_invalid_ids:
            skipped = ", ".join(f"#{art_id}" for art_id in self.skipped_invalid_ids)
            extras.append(f"skipped invalid artifact(s): {skipped}")
        if self.migrated_from is not None:
            extras.append(f"migrated from schema v{self.migrated_from} to v{SCHEMA_VERSION}")
        suffix = f"; {'; '.join(extras)}" if extras else ""
        if self.source == "none":
            return f"Store: none (build-up started{suffix})."
        age = self.age_seconds(now)
        age_text = f", age {age / 3600:.1f}h" if age is not None else ""
        created_text = f"created {format_moment(self.created_at)}" if self.created_at is not None else "age unknown"
        if self.source == "artifact" and self.artifact_id is not None:
            return f"Store: artifact #{self.artifact_id} ({created_text}{age_text}{suffix})."
        return f"Store: `{self.path}` ({created_text}{age_text}{suffix})."

    def document(self, now: datetime.datetime) -> dict:
        return {
            "source": self.source,
            "artifact_id": self.artifact_id,
            "path": self.path,
            "created_at": format_moment(self.created_at) if self.created_at is not None else None,
            "age_seconds": round(age, 1) if (age := self.age_seconds(now)) is not None else None,
            "skipped_invalid_ids": list(self.skipped_invalid_ids),
            "migrated_from": self.migrated_from,
            "is_buildup": self.is_buildup,
        }


def load_store_from_file(path: Path) -> tuple[dict, StoreLoadInfo]:
    if not path.is_file():
        raise SetupError(f"store file not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        store, migrated_from = validate_and_migrate_store(raw)
    except (OSError, UnicodeDecodeError, ValueError, StoreInvalidError) as error:
        raise MeasurementError(f"could not read store file {path}: {error}") from error
    created_at = moment(store["generated_at"]) if store.get("generated_at") else None
    return store, StoreLoadInfo(
        source="file",
        path=str(path),
        created_at=created_at,
        migrated_from=migrated_from,
        is_buildup=False,
    )


def _extract_store_from_zip(raw: bytes) -> dict:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [name for name in archive.namelist() if name.endswith(".json")]
            if not names:
                raise StoreInvalidError("store zip contains no JSON file")
            chosen = MEASUREMENT_STORE_FILENAME if MEASUREMENT_STORE_FILENAME in names else names[0]
            document = json.loads(archive.read(chosen).decode("utf-8"))
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, ValueError) as error:
        raise StoreInvalidError(str(error)) from error
    return document


def load_store_from_artifact(own_api: Api, own_repository: str, now: datetime.datetime) -> tuple[dict, StoreLoadInfo]:
    """Read the newest readable ``measurement-records`` artifact on ``main``, falling back to older unexpired ones.
    HTTP/network errors and all-unreadable artifacts fail loudly so history is never overwritten by a build-up."""
    artifacts = own_api.pages(
        f"repos/{own_repository}/actions/artifacts?name={urllib.parse.quote(MEASUREMENT_STORE_ARTIFACT)}",
        "artifacts",
        allow_missing=False,
    )
    assert artifacts is not None
    candidates: list[tuple[datetime.datetime, int, dict]] = []
    for item in artifacts:
        if item.get("name") != MEASUREMENT_STORE_ARTIFACT or item.get("expired"):
            continue
        art_id = item.get("id")
        created_raw = item.get("created_at")
        workflow_run = item.get("workflow_run")
        if not isinstance(art_id, int) or isinstance(art_id, bool):
            continue
        if not isinstance(created_raw, str) or not created_raw:
            continue
        if not isinstance(workflow_run, dict) or workflow_run.get("head_branch") != "main":
            continue
        try:
            created_at = moment(created_raw)
        except ValueError:
            continue
        candidates.append((created_at, art_id, item))
    if not candidates:
        return empty_store(now), StoreLoadInfo(source="none", is_buildup=True)

    candidates.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    skipped_invalid: list[int] = []
    for created_at, art_id, _ in candidates:
        raw = own_api.get_bytes(f"repos/{own_repository}/actions/artifacts/{art_id}/zip")
        if not raw:
            skipped_invalid.append(art_id)
            continue
        try:
            document = _extract_store_from_zip(raw)
            store, migrated_from = validate_and_migrate_store(document)
        except StoreInvalidError:
            skipped_invalid.append(art_id)
            continue
        return store, StoreLoadInfo(
            source="artifact",
            artifact_id=art_id,
            created_at=created_at,
            skipped_invalid_ids=tuple(skipped_invalid),
            migrated_from=migrated_from,
            is_buildup=False,
        )

    skipped_text = ", ".join(f"#{art_id}" for art_id in skipped_invalid)
    raise MeasurementError(
        f"all {len(candidates)} unexpired {MEASUREMENT_STORE_ARTIFACT} artifact(s) on main were unreadable ({skipped_text})"
    )


# ---------------------------------------------------------------------------
# Fetching: listing with cap-splitting, and attempt-1 run judging
# ---------------------------------------------------------------------------


def _list_runs_range(
    api: Api,
    repository: str,
    workflow: str,
    start: datetime.datetime,
    end: datetime.datetime,
    *,
    use_ge_start: bool,
) -> list[dict] | None:
    if use_ge_start:
        created_expr = f">={start.strftime('%Y-%m-%d')}"
    else:
        created_expr = f"{format_moment(start)}..{format_moment(end)}"
    encoded_created = urllib.parse.quote(created_expr, safe="")
    base_path = (
        f"repos/{repository}/actions/workflows/{urllib.parse.quote(workflow)}/runs"
        f"?created={encoded_created}&exclude_pull_requests=true"
    )
    status, document = api.request_json("GET", f"{base_path}&per_page={PAGE}&page=1")
    if status == 404:
        return None
    if not isinstance(document, dict) or not isinstance(document.get("workflow_runs"), list):
        raise MeasurementError(f"GET {base_path}: expected workflow_runs list")

    total_count = document.get("total_count")
    first_page = [item for item in document["workflow_runs"] if isinstance(item, dict)]

    def split_or_fail() -> list[dict]:
        span_seconds = int((end - start).total_seconds())
        if span_seconds < 1:
            raise MeasurementError(
                f"workflow runs listing for {workflow} reached GitHub's {CREATED_SEARCH_CAP}-result cap under created={created_expr}"
            )
        mid = start + datetime.timedelta(seconds=span_seconds // 2)
        left = _list_runs_range(api, repository, workflow, start, mid, use_ge_start=False) or []
        right = _list_runs_range(api, repository, workflow, mid + datetime.timedelta(seconds=1), end, use_ge_start=False) or []
        by_id: dict[int, dict] = {}
        for run in left + right:
            rid = run.get("id")
            if isinstance(rid, int):
                by_id[rid] = run
        return list(by_id.values())

    if isinstance(total_count, int) and total_count >= CREATED_SEARCH_CAP:
        return split_or_fail()

    items = list(first_page)
    page = 2
    while len(first_page) >= PAGE:
        status, document = api.request_json("GET", f"{base_path}&per_page={PAGE}&page={page}")
        if status == 404:
            raise MeasurementError(f"GET {base_path}: HTTP 404")
        if not isinstance(document, dict) or not isinstance(document.get("workflow_runs"), list):
            raise MeasurementError(f"GET {base_path}: expected workflow_runs list")
        first_page = [item for item in document["workflow_runs"] if isinstance(item, dict)]
        items.extend(first_page)
        if len(items) >= CREATED_SEARCH_CAP:
            return split_or_fail()
        page += 1
    return items


def list_repository_runs(
    api: Api,
    repository: str,
    workflow: str,
    repo_entry: dict,
    cutoff: datetime.datetime,
    now: datetime.datetime,
) -> tuple[bool, set[int]]:
    """List the uncovered part of ``[cutoff, now]`` (plus overlap), upserting stub records into ``repo_entry``.
    Return ``(has_workflow, seen_run_ids)``."""
    listed_from_raw = repo_entry.get("listed_from")
    listed_to_raw = repo_entry.get("listed_to")
    ranges: list[tuple[datetime.datetime, datetime.datetime, bool]] = []
    if listed_from_raw is None or listed_to_raw is None:
        ranges.append((cutoff, now, True))
    else:
        listed_from = moment(listed_from_raw)
        listed_to = moment(listed_to_raw)
        overlap_start = max(cutoff, listed_to - datetime.timedelta(days=LISTING_OVERLAP_DAYS))
        ranges.append((overlap_start, now, True))
        if cutoff < listed_from:
            backfill_end = min(now, listed_from + datetime.timedelta(days=LISTING_OVERLAP_DAYS))
            ranges.append((cutoff, backfill_end, False))

    seen_ids: set[int] = set()
    all_runs: list[dict] = []
    for range_start, range_end, use_ge in ranges:
        found = _list_runs_range(api, repository, workflow, range_start, range_end, use_ge_start=use_ge)
        if found is None:
            repo_entry["has_workflow"] = False
            return False, set()
        all_runs.extend(found)

    repo_entry["has_workflow"] = True
    records: dict[str, dict] = repo_entry["records"]
    for run in all_runs:
        if run.get("event") in MANUAL_EVENTS:
            continue
        created_raw = run.get("created_at")
        if not isinstance(created_raw, str) or not created_raw:
            continue
        created_at = moment(created_raw)
        if created_at < cutoff or created_at > now:
            continue
        run_id = run.get("id")
        if not isinstance(run_id, int) or isinstance(run_id, bool):
            continue
        seen_ids.add(run_id)
        key = str(run_id)
        attempt = int(run.get("run_attempt") or 1)
        status = str(run.get("status") or "unknown")
        conclusion = run.get("conclusion") if isinstance(run.get("conclusion"), str) else None
        updated_at = run.get("updated_at") if isinstance(run.get("updated_at"), str) else None
        existing = records.get(key)
        if existing is None:
            records[key] = {
                "run_id": run_id,
                "created_at": created_raw,
                "run_attempt": attempt,
                "status": status,
                "conclusion": conclusion,
                "updated_at": updated_at,
                "state": "pending",
                "verdict_completed_at": None,
                "duration_seconds": None,
                "plan_found": None,
                "selects_no_tests": None,
                "fallbacks": {},
                "unknown_input_parts": [],
                "fixed_cost_outcome": None,
                "fixed_cost_sample": None,
                "judged_at": None,
                "judge_version": None,
            }
        elif existing["state"] in ("pending", "unfinished"):
            existing["run_attempt"] = attempt
            existing["status"] = status
            existing["conclusion"] = conclusion
            existing["updated_at"] = updated_at

    new_from = min(moment(listed_from_raw), cutoff) if listed_from_raw is not None else cutoff
    new_to = max(moment(listed_to_raw), now) if listed_to_raw is not None else now
    repo_entry["listed_from"] = format_moment(new_from)
    repo_entry["listed_to"] = format_moment(new_to)
    return True, seen_ids


def _latest_job_completed_at(jobs: list[dict] | None) -> datetime.datetime | None:
    latest: datetime.datetime | None = None
    for job in jobs or ():
        raw = job.get("completed_at")
        if isinstance(raw, str) and raw:
            try:
                ts = moment(raw)
            except ValueError:
                continue
            if latest is None or ts > latest:
                latest = ts
    return latest


def judge_record(
    api: Api,
    repository: str,
    record: dict,
    now: datetime.datetime,
    *,
    in_overlap: bool,
    plan_index: RepoPlanArtifactIndex,
) -> dict:
    """Fetch first-attempt jobs and plan for ``record`` and return its updated record dictionary."""
    updated = copy.deepcopy(record)
    run_id = int(updated["run_id"])
    created_at = moment(updated["created_at"])
    settle_delta = datetime.timedelta(minutes=SETTLE_MINUTES)

    # Refresh an older unfinished record that fell outside today's listing overlap.
    if updated["state"] == "unfinished" and not in_overlap:
        run_status, run_doc = api.request_json("GET", f"repos/{repository}/actions/runs/{run_id}")
        if run_status == 404:
            updated["state"] = "excluded-cancelled"
            updated["judged_at"] = format_moment(now)
            updated["judge_version"] = JUDGE_VERSION
            return updated
        if isinstance(run_doc, dict):
            updated["run_attempt"] = int(run_doc.get("run_attempt") or 1)
            updated["status"] = str(run_doc.get("status") or updated["status"])
            updated["conclusion"] = run_doc.get("conclusion") if isinstance(run_doc.get("conclusion"), str) else None
            updated["updated_at"] = run_doc.get("updated_at") if isinstance(run_doc.get("updated_at"), str) else None

    attempt = int(updated.get("run_attempt") or 1)
    if attempt > 1:
        attempt_path = f"repos/{repository}/actions/runs/{run_id}/attempts/1"
        attempt_status_code, attempt_doc = api.request_json("GET", attempt_path)
        if attempt_status_code == 404 or not isinstance(attempt_doc, dict):
            raise MeasurementError(f"GET {attempt_path}: expected attempt 1 object (HTTP {attempt_status_code})")
        attempt_status = attempt_doc.get("status")
        attempt_conclusion = attempt_doc.get("conclusion")
        attempt_updated_raw = attempt_doc.get("updated_at")
    else:
        attempt_status = updated.get("status")
        attempt_conclusion = updated.get("conclusion")
        attempt_updated_raw = updated.get("updated_at")

    if attempt_status != "completed":
        updated["state"] = "unfinished"
        updated["judged_at"] = format_moment(now)
        return updated

    if attempt_conclusion == "cancelled":
        completed_moment = moment(attempt_updated_raw) if isinstance(attempt_updated_raw, str) and attempt_updated_raw else created_at
        if (now - completed_moment) < settle_delta:
            updated["state"] = "unfinished"
            updated["judged_at"] = format_moment(now)
            return updated
        updated["state"] = "excluded-cancelled"
        updated["verdict_completed_at"] = None
        updated["duration_seconds"] = None
        updated["plan_found"] = None
        updated["selects_no_tests"] = None
        updated["fallbacks"] = {}
        updated["unknown_input_parts"] = []
        updated["fixed_cost_outcome"] = None
        updated["fixed_cost_sample"] = None
        updated["judged_at"] = format_moment(now)
        updated["judge_version"] = JUDGE_VERSION
        return updated

    # Always judge through attempts/1/jobs so a concurrent re-run never leaks attempt 2 jobs (F4).
    jobs = api.pages(f"repos/{repository}/actions/runs/{run_id}/attempts/1/jobs", "jobs", allow_missing=(attempt == 1))
    verdict = verdict_job(jobs) if jobs else None
    if verdict is None:
        completed_moment = (
            _latest_job_completed_at(jobs)
            or (moment(attempt_updated_raw) if isinstance(attempt_updated_raw, str) and attempt_updated_raw else None)
            or created_at
        )
        if (now - completed_moment) < settle_delta:
            updated["state"] = "unfinished"
            updated["judged_at"] = format_moment(now)
            return updated
        updated["state"] = "no-verdict"
        updated["verdict_completed_at"] = None
        updated["duration_seconds"] = None
        updated["plan_found"] = None
        updated["selects_no_tests"] = None
        updated["fallbacks"] = {}
        updated["unknown_input_parts"] = []
        updated["fixed_cost_outcome"] = None
        updated["fixed_cost_sample"] = None
        updated["judged_at"] = format_moment(now)
        updated["judge_version"] = JUDGE_VERSION
        return updated

    completed_at = moment(str(verdict["completed_at"]))
    if (now - completed_at) < settle_delta:
        updated["state"] = "unfinished"
        updated["judged_at"] = format_moment(now)
        return updated

    duration = max(0.0, (completed_at - created_at).total_seconds())
    # Always pass max_created_at = attempt 1 verdict completed_at so a later re-run's ci-plan never wins (F4).
    plan = plan_index.read_plan(run_id, completed_at)
    plan_found = plan is not None
    selects_no_tests = plan_selects_no_tests(plan) if plan is not None else None
    fallbacks = plan_fallbacks(plan) if plan is not None else {}
    unknown_parts = sorted(plan_unknown_input_parts(plan)) if plan is not None else []
    outcome, sample = fixed_cost_of_run(created_at, verdict, jobs or [], plan)

    updated["state"] = "verdict"
    updated["verdict_completed_at"] = str(verdict["completed_at"])
    updated["duration_seconds"] = duration
    updated["plan_found"] = plan_found
    updated["selects_no_tests"] = selects_no_tests
    updated["fallbacks"] = fallbacks
    updated["unknown_input_parts"] = unknown_parts
    updated["fixed_cost_outcome"] = outcome.value
    updated["fixed_cost_sample"] = sample.to_dict() if sample is not None else None
    updated["judged_at"] = format_moment(now)
    updated["judge_version"] = JUDGE_VERSION
    return updated


# ---------------------------------------------------------------------------
# Coverage & aggregation from store records
# ---------------------------------------------------------------------------


@dataclass
class CoverageInfo:
    complete: bool
    listed_from: str | None = None
    listed_to: str | None = None
    pending_runs: int = 0
    unchecked_unfinished_runs: int = 0
    stale_judge_runs: int = 0
    judge_versions: tuple[int, ...] = ()
    window_days: int = DEFAULT_WINDOW_DAYS
    covered_days: float | None = None

    def format_cell(self, *, show_judge_versions: bool = False) -> str:
        versions_text = ""
        if self.judge_versions and (show_judge_versions or any(v != JUDGE_VERSION for v in self.judge_versions)):
            versions = ", ".join(f"v{v}" for v in self.judge_versions)
            versions_text = f"; judge {versions}"
        if self.complete:
            since = self.listed_from[:10] if self.listed_from else "unknown"
            return f"complete (since {since}{versions_text})"
        reasons: list[str] = []
        if self.listed_from is None or self.listed_to is None:
            reasons.append("not listed")
        elif self.covered_days is not None and self.covered_days + 0.01 < self.window_days:
            reasons.append(f"listed {self.listed_from[:10]}..{self.listed_to[:10]} ({self.covered_days:.0f}/{self.window_days}d)")
        if self.pending_runs:
            reasons.append(f"build-up: {self.pending_runs} pending")
        if self.unchecked_unfinished_runs:
            reasons.append(f"{self.unchecked_unfinished_runs} unfinished unchecked")
        if self.stale_judge_runs:
            reasons.append(f"{self.stale_judge_runs} stale judge")
        detail = ", ".join(reasons) if reasons else "incomplete"
        return f"partial ({detail}{versions_text})"

    def document(self) -> dict:
        return {
            "complete": self.complete,
            "listed_from": self.listed_from,
            "listed_to": self.listed_to,
            "pending_runs": self.pending_runs,
            "unchecked_unfinished_runs": self.unchecked_unfinished_runs,
            "stale_judge_runs": self.stale_judge_runs,
            "judge_versions": list(self.judge_versions),
        }


@dataclass
class RepositoryMetrics:
    repository: str
    has_workflow: bool = True
    error: str | None = None
    coverage: CoverageInfo = field(default_factory=lambda: CoverageInfo(complete=True))
    verdict_runs: int = 0
    non_verdict_runs: int = 0
    within_3m_count: int = 0
    durations: list[float] = field(default_factory=list)
    no_tests_durations: list[float] = field(default_factory=list)
    fallbacks: dict[str, int] = field(default_factory=dict)
    unknown_input_parts: set[str] = field(default_factory=set)
    fixed_cost_samples: list[FixedCostSample] = field(default_factory=list)
    non_shard_decided_runs: int = 0
    fixed_cost_unmatched_runs: int = 0

    @property
    def within_3m_share(self) -> float | None:
        return (self.within_3m_count / self.verdict_runs) if self.verdict_runs else None

    @property
    def no_tests_median_seconds(self) -> float | None:
        return float(statistics.median(self.no_tests_durations)) if self.no_tests_durations else None

    @property
    def fixed_cost_p95(self) -> FixedCostSample | None:
        if not self.fixed_cost_samples:
            return None
        ordered = sorted(self.fixed_cost_samples, key=lambda sample: sample.fixed_cost)
        index = math.ceil(FIXED_COST_QUANTILE * len(ordered)) - 1
        return ordered[index]

    @property
    def language_step_p95_seconds(self) -> float | None:
        if not self.fixed_cost_samples:
            return None
        ordered = sorted(sample.language_step_seconds for sample in self.fixed_cost_samples)
        index = math.ceil(FIXED_COST_QUANTILE * len(ordered)) - 1
        return ordered[index]

    def document(self, assumed_fixed_cost: float, language_step_allowance: float) -> dict:
        sample = self.fixed_cost_p95
        language_step_p95 = self.language_step_p95_seconds
        return {
            "has_workflow": self.has_workflow,
            "error": self.error,
            "coverage": self.coverage.document(),
            "verdict_runs": self.verdict_runs,
            "non_verdict_runs": self.non_verdict_runs,
            "within_3m_count": self.within_3m_count,
            "within_3m_share": self.within_3m_share,
            "no_tests_runs": len(self.no_tests_durations),
            "no_tests_median_seconds": self.no_tests_median_seconds,
            "fixed_cost": {
                "assumed_seconds": assumed_fixed_cost,
                "runs": len(self.fixed_cost_samples),
                "non_shard_decided_runs": self.non_shard_decided_runs,
                "unmatched_runs": self.fixed_cost_unmatched_runs,
                "p95_seconds": round(sample.fixed_cost, 1) if sample is not None else None,
                "outside_entry_step_seconds": round(sample.outside_entry_step, 1) if sample is not None else None,
                "step_excess_seconds": round(sample.step_excess, 1) if sample is not None else None,
                "language_step_p95_seconds": round(language_step_p95, 1) if language_step_p95 is not None else None,
                "language_step_allowance_seconds": language_step_allowance,
                "phases": sample.phases_document() if sample is not None else None,
            },
            "fallbacks": dict(sorted(self.fallbacks.items())),
            "unknown_input_parts": sorted(self.unknown_input_parts),
        }


def metrics_from_store_repo(
    repository: str,
    repo_entry: dict | None,
    cutoff: datetime.datetime,
    now: datetime.datetime,
    window_days: int,
    *,
    error: str | None = None,
) -> RepositoryMetrics:
    if error is not None:
        return RepositoryMetrics(
            repository=repository,
            error=error,
            coverage=CoverageInfo(complete=False, window_days=window_days),
        )
    if repo_entry is None:
        return RepositoryMetrics(
            repository=repository,
            coverage=CoverageInfo(complete=False, window_days=window_days),
        )
    if not repo_entry.get("has_workflow", True):
        return RepositoryMetrics(
            repository=repository,
            has_workflow=False,
            coverage=CoverageInfo(
                complete=True,
                listed_from=repo_entry.get("listed_from"),
                listed_to=repo_entry.get("listed_to"),
                window_days=window_days,
            ),
        )

    listed_from_raw = repo_entry.get("listed_from")
    listed_to_raw = repo_entry.get("listed_to")
    listed_from = moment(listed_from_raw) if listed_from_raw else None
    listed_to = moment(listed_to_raw) if listed_to_raw else None
    listing_covers = (
        listed_from is not None
        and listed_to is not None
        and listed_from <= cutoff
        and listed_to >= now
    )
    covered_days: float | None = None
    if listed_from is not None and listed_to is not None:
        overlap_start = max(cutoff, listed_from)
        overlap_end = min(now, listed_to)
        covered_days = max(0.0, (overlap_end - overlap_start).total_seconds() / 86400.0)

    metrics = RepositoryMetrics(repository=repository, has_workflow=True)
    pending_runs = 0
    unchecked_unfinished_runs = 0
    stale_judge_runs = 0
    judge_versions: set[int] = set()
    rejudge_cutoff = now - datetime.timedelta(days=CI_PLAN_RETENTION_DAYS)

    for rec in repo_entry.get("records", {}).values():
        created_at = moment(rec["created_at"])
        if created_at < cutoff or created_at > now:
            continue
        state = rec["state"]
        if state == "pending":
            pending_runs += 1
            continue
        if state == "unfinished":
            judged_raw = rec.get("judged_at")
            if listed_to is not None and (judged_raw is None or moment(judged_raw) < listed_to):
                unchecked_unfinished_runs += 1
            continue
        jv = rec.get("judge_version")
        if isinstance(jv, int):
            judge_versions.add(jv)
            if jv != JUDGE_VERSION and created_at >= rejudge_cutoff:
                stale_judge_runs += 1
        if state == "excluded-cancelled":
            continue
        if state == "no-verdict":
            metrics.non_verdict_runs += 1
            continue
        if state == "verdict":
            duration = float(rec["duration_seconds"])
            metrics.verdict_runs += 1
            metrics.durations.append(duration)
            if duration <= THREE_MINUTE_SECONDS:
                metrics.within_3m_count += 1
            if rec.get("plan_found"):
                if rec.get("selects_no_tests") is True:
                    metrics.no_tests_durations.append(duration)
                for reason, count in (rec.get("fallbacks") or {}).items():
                    metrics.fallbacks[reason] = metrics.fallbacks.get(reason, 0) + count
                metrics.unknown_input_parts.update(rec.get("unknown_input_parts") or ())
            fco = rec.get("fixed_cost_outcome")
            if fco == FixedCostOutcome.SAMPLE.value and isinstance(rec.get("fixed_cost_sample"), dict):
                metrics.fixed_cost_samples.append(FixedCostSample.from_dict(rec["fixed_cost_sample"]))
            elif fco == FixedCostOutcome.NON_SHARD.value:
                metrics.non_shard_decided_runs += 1
            elif fco == FixedCostOutcome.UNMATCHED.value:
                metrics.fixed_cost_unmatched_runs += 1

    complete = listing_covers and pending_runs == 0 and unchecked_unfinished_runs == 0 and stale_judge_runs == 0
    metrics.coverage = CoverageInfo(
        complete=complete,
        listed_from=listed_from_raw,
        listed_to=listed_to_raw,
        pending_runs=pending_runs,
        unchecked_unfinished_runs=unchecked_unfinished_runs,
        stale_judge_runs=stale_judge_runs,
        judge_versions=tuple(sorted(judge_versions)),
        window_days=window_days,
        covered_days=covered_days,
    )
    return metrics


# ---------------------------------------------------------------------------
# Formatting, findings and issue reporting
# ---------------------------------------------------------------------------


def format_share(share: float | None, count: int, total: int) -> str:
    if share is None:
        return "n/a (0 runs)"
    return f"{share * 100:.1f}% ({count}/{total})"


def format_median(median_seconds: float | None, count: int) -> str:
    if median_seconds is None:
        return "n/a (0 runs)"
    return f"{median_seconds:.1f}s ({count} runs)"


def format_fallbacks(fallbacks: dict[str, int]) -> str:
    if not fallbacks:
        return "none"
    return ", ".join(f"`{reason}` ({count})" for reason, count in sorted(fallbacks.items()))


def format_unknown_inputs(parts: set[str]) -> str:
    if not parts:
        return "none"
    return ", ".join(f"`{part}`" for part in sorted(parts))


def format_fixed_cost(item: RepositoryMetrics, language_step_allowance: float) -> str:
    extras: list[str] = []
    if item.non_shard_decided_runs:
        extras.append(f"{item.non_shard_decided_runs} non-shard")
    if item.fixed_cost_unmatched_runs:
        extras.append(f"{item.fixed_cost_unmatched_runs} unmatched")
    suffix = f"; {', '.join(extras)}" if extras else ""
    sample = item.fixed_cost_p95
    if sample is None:
        return f"n/a (0 runs{suffix})"
    language_step_p95 = item.language_step_p95_seconds
    phases = (
        f"plan {sample.plan_seconds:.1f}s, queue {sample.queue_seconds:.1f}s, "
        f"shard->entry {sample.shard_to_entry_seconds:.1f}s, after {sample.after_entry_seconds:.1f}s, "
        f"results {sample.results_seconds:.1f}s, verdict {sample.verdict_seconds:.1f}s"
    )
    lang_text = (
        f"; language step p95 {language_step_p95:.1f}s (allowance {language_step_allowance:.0f}s)"
        if language_step_p95 is not None
        else ""
    )
    return (
        f"{sample.fixed_cost:.1f}s (outside {sample.outside_entry_step:.1f}s + "
        f"excess {sample.step_excess:+.1f}s; {len(item.fixed_cost_samples)} runs{suffix}{lang_text}; phases: {phases})"
    )


def report_markdown(
    results: list[RepositoryMetrics],
    window_days: int,
    cutoff: datetime.datetime,
    now: datetime.datetime,
    assumed_fixed_cost: float,
    language_step_allowance: float,
    workflow: str = DEFAULT_WORKFLOW,
    *,
    store_info: StoreLoadInfo | None = None,
    budget_stop_reason: str | None = None,
    show_judge_versions: bool = False,
) -> str:
    lines = [
        "## CI Goal Measurement",
        "",
        f"Window: {format_moment(cutoff)} to {format_moment(now)} ({window_days} days; verdict runs, re-run runs counted by first attempt).",
    ]
    if store_info is not None:
        lines.append(store_info.format_line(now))
    pending_total = sum(item.coverage.pending_runs for item in results if item.error is None)
    partial_repos = [item.repository for item in results if item.error is None and item.has_workflow and not item.coverage.complete]
    if partial_repos or budget_stop_reason:
        parts: list[str] = []
        if partial_repos:
            parts.append(f"{len(partial_repos)} repository(ies) partial ({pending_total} runs pending)")
        if budget_stop_reason:
            parts.append(f"fetch stopped: {budget_stop_reason}")
        lines.append(f"Coverage: {'; '.join(parts)}.")
    elif results:
        lines.append("Coverage: complete for every measured repository.")
    lines.append("")
    if not results:
        lines.append("No consumer repositories were measured.")
        return "\n".join(lines) + "\n"
    lines += [
        f"| Repository | Coverage | Verdict runs | Within 3 min (target 99%) | No-tests median (target 1 min) | Fixed cost p95 (assumed {assumed_fixed_cost:.0f}s) | Fallbacks by reason | Parts with unknown inputs |",
        "| --- | --- | ---: | --- | --- | --- | --- | --- |",
    ]
    for item in results:
        if item.error is not None:
            reason = item.error.replace("\n", " ").replace("|", "/")
            lines.append(f"| `{item.repository}` | error | n/a | could not be measured: {reason} | n/a | n/a | none | none |")
            continue
        if not item.has_workflow:
            lines.append(f"| `{item.repository}` | no workflow | n/a | no `{workflow}` workflow | n/a | n/a | none | none |")
            continue
        lines.append(
            f"| `{item.repository}` | {item.coverage.format_cell(show_judge_versions=show_judge_versions)} | {item.verdict_runs} "
            f"| {format_share(item.within_3m_share, item.within_3m_count, item.verdict_runs)} "
            f"| {format_median(item.no_tests_median_seconds, len(item.no_tests_durations))} "
            f"| {format_fixed_cost(item, language_step_allowance)} "
            f"| {format_fallbacks(item.fallbacks)} | {format_unknown_inputs(item.unknown_input_parts)} |"
        )
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Finding:
    key: str
    title: str
    body: str


def misses_of(
    item: RepositoryMetrics,
    window_days: int,
    assumed_fixed_cost: float,
    language_step_allowance: float,
    workflow: str = DEFAULT_WORKFLOW,
) -> list[Finding]:
    """Return one Finding for each goal target or coverage gap that ``item`` reports.
    Partial-coverage repositories open no finding except ``no-workflow``."""
    if item.error is not None:
        return []
    if not item.has_workflow:
        body = "\n".join([
            f"`{item.repository}` has the Measurement App installed but has no `{workflow}` workflow.",
            "",
            "Install the pipeline caller or remove the Measurement App from this repository.",
        ])
        return [Finding(
            key=f"no-workflow:{item.repository}",
            title=f"No {workflow} workflow: {item.repository}",
            body=body,
        )]
    if not item.coverage.complete:
        return []
    findings: list[Finding] = []
    if item.verdict_runs == 0 and item.non_verdict_runs > 0:
        body = "\n".join([
            f"`{item.repository}` had {item.non_verdict_runs} completed `{workflow}` run(s) in the last {window_days} days, none of which reached a verdict.",
            "",
            "Check whether the verdict job was renamed, skipped, or failed before completing.",
        ])
        findings.append(Finding(
            key=f"no-verdict:{item.repository}",
            title=f"No verdict reached in {window_days} days: {item.repository}",
            body=body,
        ))
    context = [
        f"Window: rolling {window_days} days (verdict runs, re-run runs counted by first attempt).",
        f"Fallbacks by reason: {format_fallbacks(item.fallbacks)}.",
        f"Parts with unknown inputs: {format_unknown_inputs(item.unknown_input_parts)}.",
    ]
    if item.within_3m_share is not None and item.within_3m_share < THREE_MINUTE_TARGET:
        body = "\n".join([
            f"`{item.repository}` missed the 3-minute CI goal target (99% of verdict runs within 3 minutes).",
            "",
            f"- Measured: {format_share(item.within_3m_share, item.within_3m_count, item.verdict_runs)}",
            "- Target: >= 99.0% within 3 minutes",
            "",
            *context,
        ])
        findings.append(Finding(
            key=f"miss:{item.repository}:within-3m",
            title=f"CI goal missed (99% within 3 minutes): {item.repository}",
            body=body,
        ))
    if item.no_tests_median_seconds is not None and item.no_tests_median_seconds > ONE_MINUTE_SECONDS:
        body = "\n".join([
            f"`{item.repository}` missed the 1-minute no-tests CI goal target (median of runs whose plan selects no tests within 1 minute).",
            "",
            f"- Measured: {format_median(item.no_tests_median_seconds, len(item.no_tests_durations))}",
            "- Target: <= 60.0s median",
            "",
            *context,
        ])
        findings.append(Finding(
            key=f"miss:{item.repository}:no-tests-median-1m",
            title=f"CI goal missed (no-tests median within 1 minute): {item.repository}",
            body=body,
        ))
    sample = item.fixed_cost_p95
    language_step_p95 = item.language_step_p95_seconds
    if sample is not None and sample.fixed_cost > assumed_fixed_cost:
        body = "\n".join([
            f"`{item.repository}` exceeded the assumed fixed cost ({assumed_fixed_cost:.0f}s) at p95 over runs decided by a Linux shard with entry files.",
            "",
            f"- Measured p95 fixed cost: {sample.fixed_cost:.1f}s ({len(item.fixed_cost_samples)} runs)",
            f"- Outside entry-file step (excluding Python language step): {sample.outside_entry_step:.1f}s",
            f"- Entry-file step excess over planned estimate: {sample.step_excess:+.1f}s",
            (
                f"- Python language step p95: {language_step_p95:.1f}s (allowance {language_step_allowance:.0f}s)"
                if language_step_p95 is not None
                else "- Python language step p95: n/a"
            ),
            f"- Phases: plan {sample.plan_seconds:.1f}s, queue {sample.queue_seconds:.1f}s, "
            f"shard start to entry step {sample.shard_to_entry_seconds:.1f}s, "
            f"after entry step {sample.after_entry_seconds:.1f}s, results {sample.results_seconds:.1f}s, verdict {sample.verdict_seconds:.1f}s",
            f"- Excluded runs: {item.non_shard_decided_runs} decided by a part, hygiene or Windows; {item.fixed_cost_unmatched_runs} unmatched",
            f"- Assumed fixed cost: {assumed_fixed_cost:.1f}s",
            "",
            *context,
        ])
        findings.append(Finding(
            key=f"miss:{item.repository}:fixed-cost",
            title=f"CI fixed cost above assumed {assumed_fixed_cost:.0f}s: {item.repository}",
            body=body,
        ))
    if language_step_p95 is not None and language_step_p95 > language_step_allowance:
        body = "\n".join([
            f"`{item.repository}` exceeded the Python language-step allowance ({language_step_allowance:.0f}s) at p95 over runs decided by a Linux shard with entry files.",
            "",
            f"- Measured Python language step p95: {language_step_p95:.1f}s ({len(item.fixed_cost_samples)} runs)",
            f"- Stated allowance: {language_step_allowance:.1f}s",
            "",
            *context,
        ])
        findings.append(Finding(
            key=f"miss:{item.repository}:language-step",
            title=f"Python language step above allowance {language_step_allowance:.0f}s: {item.repository}",
            body=body,
        ))
    return findings


def report_findings(
    api: Api,
    repository: str,
    findings: list[Finding],
    *,
    now: datetime.datetime | None = None,
    cooldown_days: int = 0,
) -> list[str]:
    """Open one issue per finding unless an issue with the same marker is open or closed within ``cooldown_days``."""
    if not findings:
        return []
    suppressed: set[str] = set()
    for issue in api.pages(f"repos/{repository}/issues?state=open") or []:
        suppressed.update(MARKER_PATTERN.findall(issue.get("body") or ""))
    if cooldown_days > 0 and now is not None:
        cooldown_cutoff = now - datetime.timedelta(days=cooldown_days)
        since = urllib.parse.quote(format_moment(cooldown_cutoff), safe="")
        for issue in api.pages(f"repos/{repository}/issues?state=closed&since={since}") or []:
            closed_raw = issue.get("closed_at")
            if isinstance(closed_raw, str) and closed_raw:
                try:
                    closed_at = moment(closed_raw)
                except ValueError:
                    continue
                if closed_at >= cooldown_cutoff:
                    suppressed.update(MARKER_PATTERN.findall(issue.get("body") or ""))
    opened: list[str] = []
    for finding in findings:
        if finding.key in suppressed:
            continue
        created = api.send("POST", f"repos/{repository}/issues", {
            "title": finding.title,
            "body": f"{finding.body}\n\n{MARKER.format(key=finding.key)}\n",
        })
        suppressed.add(finding.key)
        opened.append(f"Opened issue #{created.get('number')}: {finding.title}")
    return opened


def append_file(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(text)


# ---------------------------------------------------------------------------
# Commands: measure, report, failure
# ---------------------------------------------------------------------------


@dataclass
class _RepoProgress:
    repository: str
    started_at: float
    new_fetched: int = 0
    unfinished_rechecked: int = 0
    rejudged: int = 0
    remaining: int = 0
    reported: bool = False


def _print_progress(line: str) -> None:
    print(line, file=sys.stderr, flush=True)


def _needs_detail_fetch(rec: dict, now: datetime.datetime) -> str | None:
    state = rec["state"]
    if state == "pending":
        return "new"
    if state == "unfinished":
        return "unfinished"
    if state in FROZEN_STATES:
        jv = rec.get("judge_version")
        if jv != JUDGE_VERSION:
            rejudge_cutoff = now - datetime.timedelta(days=CI_PLAN_RETENTION_DAYS)
            if moment(rec["created_at"]) >= rejudge_cutoff:
                return "rejudge"
    return None


def run_measure(options: argparse.Namespace) -> int:
    start_monotonic = time.monotonic()
    now: datetime.datetime = options.now
    cutoff = now - datetime.timedelta(days=options.window_days)
    store_out_path = Path(options.store_out) if options.store_out else None

    own_api: Api | None = None
    if options.own_repository and not options.local:
        own_api = Api(options.api_url, token("GITHUB_TOKEN"))

    pacer = RatePacer(options.max_rps)
    deadline = (start_monotonic + options.time_budget) if options.time_budget > 0 else None
    budget = BudgetController(
        deadline=deadline,
        request_budget=options.request_budget if options.request_budget > 0 else None,
        ratelimit_reserve=options.ratelimit_reserve,
        min_secondary_wait=options.min_secondary_wait,
    )
    api = Api(options.api_url, token("GH_TOKEN"), pacer=pacer, budget=budget)

    try:
        # 1. Load store BEFORE measuring; any read failure or all-unreadable artifact aborts before writing store_out.
        if options.store_in:
            store, store_info = load_store_from_file(Path(options.store_in))
        elif own_api is not None:
            store, store_info = load_store_from_artifact(own_api, options.own_repository, now)
        else:
            store, store_info = empty_store(now), StoreLoadInfo(source="none", is_buildup=True)

        prune_store(store, now, DEFAULT_RETENTION_DAYS)

        # 2. Resolve target repositories.
        if options.local:
            if not options.repository:
                raise SetupError("the local mode names its repositories with --repository")
            repositories = [r for r in options.repository if r != options.own_repository]
        else:
            if options.repository:
                raise SetupError("without --local the repositories come from the App's installation; --repository belongs to the local mode")
            try:
                found = api.pages("installation/repositories", "repositories") or []
            except RateLimitError as error:
                budget.handle_rate_limit(error)
                raise MeasurementError(str(error)) from error
            except BudgetStoppedError as error:
                raise MeasurementError(str(error)) from error
            repositories = [
                str(item["full_name"])
                for item in found
                if not item.get("archived") and str(item["full_name"]) != options.own_repository
            ]

        # Snapshot pre-run entries so a repository that fails with MeasurementError never advances its coverage.
        pre_run_snapshots: dict[str, dict | None] = {
            repo: copy.deepcopy(store["repositories"].get(repo)) for repo in repositories
        }
        staged_repos: dict[str, dict] = {
            repo: copy.deepcopy(store["repositories"].get(repo) or _empty_repo_entry()) for repo in repositories
        }
        repo_errors: dict[str, str] = {}
        seen_in_overlap: dict[str, set[int]] = {repo: set() for repo in repositories}
        progress_by_repo: dict[str, _RepoProgress] = {
            repo: _RepoProgress(repository=repo, started_at=time.monotonic()) for repo in repositories
        }
        store_lock = threading.Lock()
        workers = max(1, int(options.workers))

        def checkpoint_store() -> None:
            if store_out_path is None:
                return
            with store_lock:
                prune_store(store, now, DEFAULT_RETENTION_DAYS)
                write_store_atomic(store_out_path, store, now)

        def emit_repo_progress(repo: str) -> None:
            prog = progress_by_repo[repo]
            if prog.reported:
                return
            prog.reported = True
            elapsed = time.monotonic() - prog.started_at
            entry = store["repositories"].get(repo) or staged_repos.get(repo) or _empty_repo_entry()
            records = entry.get("records", {})
            pending_left = sum(1 for r in records.values() if r["state"] == "pending")
            if repo in repo_errors:
                _print_progress(f"{repo}: error={repo_errors[repo]} ({elapsed:.1f}s)")
            else:
                _print_progress(
                    f"{repo}: new={prog.new_fetched} unfinished={prog.unfinished_rechecked} "
                    f"pending={pending_left} kept={len(records)} ({elapsed:.1f}s)"
                )

        # 3. Phase 1: List runs per repository in parallel.
        def list_one(repo: str) -> None:
            progress_by_repo[repo].started_at = time.monotonic()
            try:
                _, seen = list_repository_runs(api, repo, options.workflow, staged_repos[repo], cutoff, now)
                seen_in_overlap[repo] = seen
            except RateLimitError as error:
                budget.handle_rate_limit(error)
            except BudgetStoppedError:
                pass
            except MeasurementError as error:
                with store_lock:
                    repo_errors[repo] = str(error)
                    if pre_run_snapshots[repo] is None:
                        store["repositories"].pop(repo, None)
                    else:
                        store["repositories"][repo] = copy.deepcopy(pre_run_snapshots[repo])
                print(f"::error::{repo}: {error}", file=sys.stderr)

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            list_futures = [executor.submit(list_one, repo) for repo in repositories]
            for fut in concurrent.futures.as_completed(list_futures):
                fut.result()

        # Commit listed stubs of non-errored repos and checkpoint so pending stubs survive even if Phase 2 stops early.
        with store_lock:
            for repo in repositories:
                if repo not in repo_errors and (
                    staged_repos[repo].get("listed_to") is not None or not staged_repos[repo].get("has_workflow", True)
                ):
                    store["repositories"][repo] = staged_repos[repo]
        checkpoint_store()

        # 4. Phase 2: Collect work items across repositories, newest first, and fetch details in parallel.
        work_items: list[tuple[datetime.datetime, str, str, str]] = []
        per_repo_counts: dict[str, int] = {repo: 0 for repo in repositories}
        for repo in repositories:
            if repo in repo_errors:
                emit_repo_progress(repo)
                continue
            entry = staged_repos[repo]
            if not entry.get("has_workflow", True):
                emit_repo_progress(repo)
                continue
            for key, rec in entry["records"].items():
                kind = _needs_detail_fetch(rec, now)
                if kind is not None:
                    work_items.append((moment(rec["created_at"]), repo, key, kind))
                    per_repo_counts[repo] += 1
            progress_by_repo[repo].remaining = per_repo_counts[repo]
            if per_repo_counts[repo] == 0:
                emit_repo_progress(repo)

        # Newest first so recent runs finish first during a multi-run build-up.
        work_items.sort(key=lambda item: (item[0], int(item[2])), reverse=True)
        plan_indices = {
            repo: RepoPlanArtifactIndex(api, repo, per_repo_counts[repo])
            for repo in repositories
            if per_repo_counts[repo] > 0
        }

        def finish_one_item(repo: str, key: str, kind: str) -> None:
            with store_lock:
                if repo in repo_errors or budget.stop_event.is_set():
                    return
                record = staged_repos[repo]["records"][key]
                in_overlap = int(record["run_id"]) in seen_in_overlap[repo]
            try:
                judged = judge_record(
                    api,
                    repo,
                    record,
                    now,
                    in_overlap=in_overlap,
                    plan_index=plan_indices[repo],
                )
            except RateLimitError as error:
                budget.handle_rate_limit(error)
                return
            except BudgetStoppedError:
                return
            except MeasurementError as error:
                with store_lock:
                    if repo not in repo_errors:
                        repo_errors[repo] = str(error)
                        if pre_run_snapshots[repo] is None:
                            store["repositories"].pop(repo, None)
                        else:
                            store["repositories"][repo] = copy.deepcopy(pre_run_snapshots[repo])
                        print(f"::error::{repo}: {error}", file=sys.stderr)
                checkpoint_store()
                return

            repo_completed = False
            with store_lock:
                if repo in repo_errors:
                    return
                staged_repos[repo]["records"][key] = judged
                store["repositories"][repo] = staged_repos[repo]
                prog = progress_by_repo[repo]
                if kind == "new":
                    prog.new_fetched += 1
                elif kind == "unfinished":
                    prog.unfinished_rechecked += 1
                else:
                    prog.rejudged += 1
                prog.remaining -= 1
                if prog.remaining == 0:
                    repo_completed = True
            if repo_completed:
                checkpoint_store()
                emit_repo_progress(repo)

        if work_items and not budget.stop_event.is_set():
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                in_flight: set[concurrent.futures.Future[None]] = set()
                item_iter = iter(work_items)
                max_queued = workers * 2
                while True:
                    while len(in_flight) < max_queued and not budget.stop_event.is_set():
                        try:
                            _, repo, key, kind = next(item_iter)
                        except StopIteration:
                            break
                        with store_lock:
                            if repo in repo_errors:
                                continue
                        in_flight.add(executor.submit(finish_one_item, repo, key, kind))
                    if not in_flight:
                        break
                    done, in_flight = concurrent.futures.wait(in_flight, return_when=concurrent.futures.FIRST_COMPLETED)
                    for fut in done:
                        fut.result()

        # Final atomic checkpoint after all workers stop.
        checkpoint_store()
        for repo in repositories:
            emit_repo_progress(repo)

        # 5. Compute metrics from the store, open findings, and write report.
        results: list[RepositoryMetrics] = []
        for repo in repositories:
            if repo in repo_errors:
                results.append(metrics_from_store_repo(repo, None, cutoff, now, options.window_days, error=repo_errors[repo]))
            else:
                results.append(metrics_from_store_repo(repo, store["repositories"].get(repo), cutoff, now, options.window_days))

        assumed_fixed_cost = assumed_fixed_cost_seconds()
        language_step_allowance = language_step_allowance_seconds()
        findings = [
            finding
            for item in results
            for finding in misses_of(item, options.window_days, assumed_fixed_cost, language_step_allowance, workflow=options.workflow)
        ]
        opened: list[str] = []
        if own_api is not None:
            opened = report_findings(own_api, options.own_repository, findings, now=now, cooldown_days=FINDING_COOLDOWN_DAYS)

        total_pending = sum(item.coverage.pending_runs for item in results if item.error is None)
        total_elapsed = time.monotonic() - start_monotonic
        rem_text = str(api.ratelimit_remaining) if api.ratelimit_remaining is not None else "n/a"
        stop_text = f" stopped={budget.stop_reason}" if budget.stop_reason else ""
        _print_progress(
            f"Measurement totals: requests={api.request_count} ratelimit_remaining={rem_text} "
            f"pending={total_pending} elapsed={total_elapsed:.1f}s{stop_text}"
        )

        markdown = report_markdown(
            results,
            options.window_days,
            cutoff,
            now,
            assumed_fixed_cost,
            language_step_allowance,
            workflow=options.workflow,
            store_info=store_info,
            budget_stop_reason=budget.stop_reason,
        )
        if opened:
            markdown += "\n" + "\n".join(f"- {line}" for line in opened) + "\n"
        print(markdown, end="")
        append_file(options.summary, markdown)
        if options.json_output:
            destination = Path(options.json_output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "window_days": options.window_days,
                "cutoff": format_moment(cutoff),
                "now": format_moment(now),
                "store": store_info.document(now),
                "requests": api.request_count,
                "ratelimit_remaining": api.ratelimit_remaining,
                "budget_stop_reason": budget.stop_reason,
                "pending_runs": total_pending,
                "repositories": {item.repository: item.document(assumed_fixed_cost, language_step_allowance) for item in results},
            }
            destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")

        failed = bool(repo_errors) or (budget.rate_limit_failure is not None)
        if budget.rate_limit_failure is not None:
            print(f"::error::{budget.rate_limit_failure}", file=sys.stderr)
        return 1 if failed else 0
    finally:
        api.close()
        if own_api is not None:
            own_api.close()


def run_report(options: argparse.Namespace) -> int:
    store, store_info = load_store_from_file(Path(options.store))
    now: datetime.datetime
    if options.now is not None:
        now = options.now
    elif store.get("generated_at"):
        now = moment(str(store["generated_at"]))
    else:
        now = datetime.datetime.now(datetime.timezone.utc)
    if options.window_days <= 0:
        raise SetupError("--window-days must be a positive integer")
    cutoff = now - datetime.timedelta(days=options.window_days)
    if options.repository:
        repositories = list(options.repository)
    else:
        repositories = sorted(store["repositories"])
    results = [
        metrics_from_store_repo(repo, store["repositories"].get(repo), cutoff, now, options.window_days)
        for repo in repositories
    ]
    assumed_fixed_cost = assumed_fixed_cost_seconds()
    language_step_allowance = language_step_allowance_seconds()
    markdown = report_markdown(
        results,
        options.window_days,
        cutoff,
        now,
        assumed_fixed_cost,
        language_step_allowance,
        workflow=options.workflow,
        store_info=store_info,
        show_judge_versions=True,
    )
    print(markdown, end="")
    append_file(options.summary, markdown)
    if options.json_output:
        destination = Path(options.json_output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "window_days": options.window_days,
            "cutoff": format_moment(cutoff),
            "now": format_moment(now),
            "store": store_info.document(now),
            "repositories": {item.repository: item.document(assumed_fixed_cost, language_step_allowance) for item in results},
        }
        destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    return 0


def run_failure(options: argparse.Namespace) -> int:
    body = f"The run {options.run_url} of the CI goal measurement failed. Its log says which repository could not be measured and why."
    own_api = Api(options.api_url, token("GITHUB_TOKEN"))
    try:
        opened = report_findings(
            own_api,
            options.own_repository,
            [Finding("failure", "The CI goal measurement failed", body)],
        )
    finally:
        own_api.close()
    text = "## Measurement failure\n\n" + ("\n".join(f"- {line}" for line in opened) if opened else "An issue for a failed run is already open.") + "\n"
    print(text, end="")
    append_file(options.summary, text)
    return 0


def parser() -> argparse.ArgumentParser:
    main_parser = argparse.ArgumentParser(description="Measure the shared CI goal for every consumer repository.")
    commands = main_parser.add_subparsers(dest="command", required=True)

    measure = commands.add_parser("measure", help="measure every consumer repository incrementally and open issues on missed targets")
    measure.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"), help="the address of the GitHub API")
    measure.add_argument("--own-repository", default="", help="the repository of the run, as OWNER/NAME: excluded from measurement and receives miss issues")
    measure.add_argument("--local", action="store_true", help="run with the owner's own token on the named repositories")
    measure.add_argument("--repository", action="append", help="a repository of the local mode, as OWNER/NAME")
    measure.add_argument("--workflow", default=DEFAULT_WORKFLOW, help="the pipeline caller workflow file name")
    measure.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS, help="rolling window length in days")
    measure.add_argument("--now", type=moment, default=datetime.datetime.now(datetime.timezone.utc), help="the reference UTC timestamp of the measurement")
    measure.add_argument("--store-in", help="read the incremental records store from this local JSON file instead of the artifact")
    measure.add_argument("--store-out", help="write the updated incremental records store atomically to this JSON file")
    measure.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="maximum concurrent fetch workers")
    measure.add_argument("--max-rps", type=float, default=DEFAULT_MAX_RPS, help="maximum global API requests per second (0 disables pacing)")
    measure.add_argument("--time-budget", type=float, default=DEFAULT_TIME_BUDGET_SECONDS, help="global fetch time budget in seconds (0 disables)")
    measure.add_argument("--request-budget", type=int, default=DEFAULT_REQUEST_BUDGET, help="maximum API requests for fetching (0 disables)")
    measure.add_argument("--ratelimit-reserve", type=int, default=DEFAULT_RATELIMIT_RESERVE, help="stop fetching when x-ratelimit-remaining falls below this reserve")
    measure.add_argument("--min-secondary-wait", type=float, default=DEFAULT_MIN_SECONDARY_WAIT_SECONDS, help="minimum backoff seconds on a secondary rate limit")
    measure.add_argument("--summary", help="the file that receives the Markdown report")
    measure.add_argument("--json-output", help="the file that receives the structured JSON report")

    report = commands.add_parser("report", help="compute the CI goal report over a chosen window from a downloaded store file")
    report.add_argument("--store", required=True, help="the downloaded measurement-records JSON file")
    report.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS, help="rolling window length in days (up to 90)")
    report.add_argument("--now", type=moment, default=None, help="the reference UTC timestamp (defaults to the store's generated_at)")
    report.add_argument("--repository", action="append", help="limit the report to named repositories, as OWNER/NAME")
    report.add_argument("--workflow", default=DEFAULT_WORKFLOW, help="the pipeline caller workflow file name")
    report.add_argument("--summary", help="the file that receives the Markdown report")
    report.add_argument("--json-output", help="the file that receives the structured JSON report")

    failure = commands.add_parser("failure", help="open an issue for a failed measurement run")
    failure.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"), help="the address of the GitHub API")
    failure.add_argument("--own-repository", required=True, help="the repository of the run, as OWNER/NAME")
    failure.add_argument("--run-url", required=True, help="the address of the failed run")
    failure.add_argument("--summary", help="the file that receives the Markdown summary")
    return main_parser


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    try:
        return {"measure": run_measure, "report": run_report, "failure": run_failure}[options.command](options)
    except SetupError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2
    except MeasurementError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
