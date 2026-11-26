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
import json
import math
import os
import re
import statistics
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from measurement_api import (  # noqa: E402
    API_TIMEOUT,
    DEFAULT_MAX_RPS,
    DEFAULT_MIN_SECONDARY_WAIT_SECONDS,
    DEFAULT_RATELIMIT_RESERVE,
    DEFAULT_REQUEST_BUDGET,
    DEFAULT_TIME_BUDGET_SECONDS,
    PAGE,
    Api,
    BudgetController,
    BudgetStoppedError,
    MeasurementError,
    RateLimitError,
    RateLimitKind,
    RatePacer,
    SetupError,
    extract_json_from_zip,
    format_moment,
    moment,
    token,
)
from measurement_store import (  # noqa: E402
    DEFAULT_RETENTION_DAYS,
    FROZEN_STATES,
    JUDGE_VERSION,
    MEASUREMENT_STORE_ARTIFACT,
    MEASUREMENT_STORE_FILENAME,
    SCHEMA_VERSION,
    FixedCostOutcome,
    FixedCostSample,
    RecordState,
    StoreInvalidError,
    StoreLoadInfo,
    empty_repo_entry,
    empty_store,
    load_store_from_artifact,
    load_store_from_file,
    make_record,
    prune_store,
    record_state,
    write_store_atomic,
)

CREATED_SEARCH_CAP = 1000
DEFAULT_WINDOW_DAYS = 28
CI_PLAN_RETENTION_DAYS = 30
SETTLE_MINUTES = 30
LISTING_OVERLAP_DAYS = 1
FINDING_COOLDOWN_DAYS = 7
DEFAULT_WORKFLOW = "ci.yml"
DEFAULT_WORKERS = 6
CHECKPOINT_INTERVAL_ITEMS = 25
CHECKPOINT_INTERVAL_SECONDS = 30.0

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

__all__ = [
    "API_TIMEOUT",
    "DEFAULT_MAX_RPS",
    "ENTRY_FILE_STEP",
    "FixedCostOutcome",
    "FixedCostSample",
    "JUDGE_VERSION",
    "MEASUREMENT_STORE_ARTIFACT",
    "MEASUREMENT_STORE_FILENAME",
    "PYTHON_LANGUAGE_STEP",
    "RateLimitKind",
    "RecordState",
    "SCHEMA_VERSION",
    "StoreInvalidError",
    "assumed_fixed_cost_seconds",
    "language_step_allowance_seconds",
    "main",
    "parser",
]


class WorkKind(enum.Enum):
    NEW = "new"
    UNFINISHED = "unfinished"
    REJUDGE = "rejudge"


@dataclass(frozen=True)
class MeasurementWindow:
    cutoff: datetime.datetime
    now: datetime.datetime
    window_days: int


@dataclass(frozen=True)
class FixedCostThresholds:
    assumed_fixed_cost: float
    language_step_allowance: float


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


def current_thresholds() -> FixedCostThresholds:
    return FixedCostThresholds(
        assumed_fixed_cost=assumed_fixed_cost_seconds(),
        language_step_allowance=language_step_allowance_seconds(),
    )


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
        document = extract_json_from_zip(raw, "ci-plan.json", error_type=ValueError)
    except ValueError:
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
            all_items: list[dict] = []
            for page_number, total_count, items in self.api.iter_pages(path, "artifacts", allow_missing=True):
                if page_number == 1:
                    total_pages = math.ceil(total_count / PAGE) if total_count and total_count > 0 else 1
                    if total_pages > self.planned_runs:
                        self._use_per_run = True
                        self._loaded = True
                        return
                all_items.extend(items)
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
    created_expr = f">={start.strftime('%Y-%m-%d')}" if use_ge_start else f"{format_moment(start)}..{format_moment(end)}"
    encoded_created = urllib.parse.quote(created_expr, safe="")
    base_path = (
        f"repos/{repository}/actions/workflows/{urllib.parse.quote(workflow)}/runs"
        f"?created={encoded_created}&exclude_pull_requests=true"
    )

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
            run_id = run.get("id")
            if isinstance(run_id, int):
                by_id[run_id] = run
        return list(by_id.values())

    items: list[dict] = []
    saw_page = False
    for page_number, total_count, page_items in api.iter_pages(base_path, "workflow_runs", allow_missing=True):
        saw_page = True
        if page_number == 1 and total_count is not None and total_count >= CREATED_SEARCH_CAP:
            return split_or_fail()
        items.extend(page_items)
        if len(items) >= CREATED_SEARCH_CAP:
            return split_or_fail()
    if not saw_page:
        return None
    return items


def list_repository_runs(
    api: Api,
    repository: str,
    workflow: str,
    repo_entry: dict,
    window: MeasurementWindow,
) -> tuple[bool, set[int]]:
    """List the uncovered part of ``window`` (plus overlap), upserting stub records into ``repo_entry``.
    Return ``(has_workflow, seen_run_ids)``."""
    listed_from_raw = repo_entry.get("listed_from")
    listed_to_raw = repo_entry.get("listed_to")
    ranges: list[tuple[datetime.datetime, datetime.datetime, bool]] = []
    if listed_from_raw is None or listed_to_raw is None:
        ranges.append((window.cutoff, window.now, True))
    else:
        listed_from = moment(listed_from_raw)
        listed_to = moment(listed_to_raw)
        overlap_start = max(window.cutoff, listed_to - datetime.timedelta(days=LISTING_OVERLAP_DAYS))
        ranges.append((overlap_start, window.now, True))
        if window.cutoff < listed_from:
            backfill_end = min(window.now, listed_from + datetime.timedelta(days=LISTING_OVERLAP_DAYS))
            ranges.append((window.cutoff, backfill_end, False))

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
        if created_at < window.cutoff or created_at > window.now:
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
            records[key] = make_record(
                run_id=run_id,
                created_at=created_raw,
                run_attempt=attempt,
                status=status,
                conclusion=conclusion,
                updated_at=updated_at,
                state=RecordState.PENDING,
            )
        elif record_state(existing) in (RecordState.PENDING, RecordState.UNFINISHED):
            existing["run_attempt"] = attempt
            existing["status"] = status
            existing["conclusion"] = conclusion
            existing["updated_at"] = updated_at

    new_from = min(moment(listed_from_raw), window.cutoff) if listed_from_raw is not None else window.cutoff
    new_to = max(moment(listed_to_raw), window.now) if listed_to_raw is not None else window.now
    repo_entry["listed_from"] = format_moment(new_from)
    repo_entry["listed_to"] = format_moment(new_to)
    return True, seen_ids


def _latest_job_completed_at(jobs: list[dict] | None) -> datetime.datetime | None:
    latest: datetime.datetime | None = None
    for job in jobs or ():
        raw = job.get("completed_at")
        if isinstance(raw, str) and raw:
            try:
                timestamp = moment(raw)
            except ValueError:
                continue
            if latest is None or timestamp > latest:
                latest = timestamp
    return latest


def _mark_unfinished(record: dict, now: datetime.datetime) -> dict:
    record["state"] = RecordState.UNFINISHED.value
    record["judged_at"] = format_moment(now)
    return record


def _mark_non_verdict_terminal(record: dict, state: RecordState, now: datetime.datetime) -> dict:
    record["state"] = state.value
    record["verdict_completed_at"] = None
    record["duration_seconds"] = None
    record["plan_found"] = None
    record["selects_no_tests"] = None
    record["fallbacks"] = {}
    record["unknown_input_parts"] = []
    record["fixed_cost_outcome"] = None
    record["fixed_cost_sample"] = None
    record["judged_at"] = format_moment(now)
    record["judge_version"] = JUDGE_VERSION
    return record


def judge_record(
    api: Api,
    repository: str,
    record: dict,
    now: datetime.datetime,
    *,
    in_overlap: bool,
    plan_index: RepoPlanArtifactIndex,
    is_rejudge: bool = False,
) -> dict:
    """Fetch first-attempt jobs and plan for ``record`` and return its updated record dictionary."""
    updated = copy.deepcopy(record)
    run_id = int(updated["run_id"])
    created_at = moment(updated["created_at"])
    settle_delta = datetime.timedelta(minutes=SETTLE_MINUTES)

    # Refresh an older unfinished record that fell outside today's listing overlap.
    if record_state(updated) is RecordState.UNFINISHED and not in_overlap:
        run_status, run_doc = api.request_json("GET", f"repos/{repository}/actions/runs/{run_id}")
        if run_status == 404:
            return _mark_non_verdict_terminal(updated, RecordState.DELETED, now)
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
        return _mark_unfinished(updated, now)

    if attempt_conclusion == "cancelled":
        completed_moment = moment(attempt_updated_raw) if isinstance(attempt_updated_raw, str) and attempt_updated_raw else created_at
        if (now - completed_moment) < settle_delta:
            return _mark_unfinished(updated, now)
        return _mark_non_verdict_terminal(updated, RecordState.EXCLUDED_CANCELLED, now)

    # Always judge through attempts/1/jobs so a concurrent re-run never leaks attempt 2 jobs.
    jobs = api.pages(f"repos/{repository}/actions/runs/{run_id}/attempts/1/jobs", "jobs", allow_missing=(attempt == 1))
    verdict = verdict_job(jobs) if jobs else None
    if verdict is None:
        completed_moment = (
            _latest_job_completed_at(jobs)
            or (moment(attempt_updated_raw) if isinstance(attempt_updated_raw, str) and attempt_updated_raw else None)
            or created_at
        )
        if (now - completed_moment) < settle_delta:
            return _mark_unfinished(updated, now)
        return _mark_non_verdict_terminal(updated, RecordState.NO_VERDICT, now)

    completed_at = moment(str(verdict["completed_at"]))
    if (now - completed_at) < settle_delta:
        return _mark_unfinished(updated, now)

    # Always pass max_created_at = attempt 1 verdict completed_at so a later re-run's ci-plan never wins.
    plan = plan_index.read_plan(run_id, completed_at)
    # During a judge-version re-judge, if the ci-plan artifact has already expired, keep the old record intact.
    if is_rejudge and record.get("plan_found") is True and plan is None:
        return copy.deepcopy(record)

    duration = max(0.0, (completed_at - created_at).total_seconds())
    plan_found = plan is not None
    selects_no_tests = plan_selects_no_tests(plan) if plan is not None else None
    fallbacks = plan_fallbacks(plan) if plan is not None else {}
    unknown_parts = sorted(plan_unknown_input_parts(plan)) if plan is not None else []
    outcome, sample = fixed_cost_of_run(created_at, verdict, jobs or [], plan)

    updated["state"] = RecordState.VERDICT.value
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
    deleted_runs: int = 0
    judge_versions: tuple[int, ...] = ()
    window_days: int = DEFAULT_WINDOW_DAYS
    covered_days: float | None = None

    def format_cell(self, *, show_judge_versions: bool = False) -> str:
        notes: list[str] = []
        if self.deleted_runs:
            notes.append(f"{self.deleted_runs} deleted")
        if self.judge_versions and (show_judge_versions or any(version != JUDGE_VERSION for version in self.judge_versions)):
            versions = ", ".join(f"v{version}" for version in self.judge_versions)
            notes.append(f"judge {versions}")
        notes_suffix = f"; {'; '.join(notes)}" if notes else ""
        if self.complete:
            since = self.listed_from[:10] if self.listed_from else "unknown"
            return f"complete (since {since}{notes_suffix})"
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
        return f"partial ({detail}{notes_suffix})"

    def document(self) -> dict:
        return {
            "complete": self.complete,
            "listed_from": self.listed_from,
            "listed_to": self.listed_to,
            "pending_runs": self.pending_runs,
            "unchecked_unfinished_runs": self.unchecked_unfinished_runs,
            "stale_judge_runs": self.stale_judge_runs,
            "deleted_runs": self.deleted_runs,
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
    deleted_runs: int = 0
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

    def document(self, thresholds: FixedCostThresholds) -> dict:
        sample = self.fixed_cost_p95
        language_step_p95 = self.language_step_p95_seconds
        return {
            "has_workflow": self.has_workflow,
            "error": self.error,
            "coverage": self.coverage.document(),
            "verdict_runs": self.verdict_runs,
            "non_verdict_runs": self.non_verdict_runs,
            "deleted_runs": self.deleted_runs,
            "within_3m_count": self.within_3m_count,
            "within_3m_share": self.within_3m_share,
            "no_tests_runs": len(self.no_tests_durations),
            "no_tests_median_seconds": self.no_tests_median_seconds,
            "fixed_cost": {
                "assumed_seconds": thresholds.assumed_fixed_cost,
                "runs": len(self.fixed_cost_samples),
                "non_shard_decided_runs": self.non_shard_decided_runs,
                "unmatched_runs": self.fixed_cost_unmatched_runs,
                "p95_seconds": round(sample.fixed_cost, 1) if sample is not None else None,
                "outside_entry_step_seconds": round(sample.outside_entry_step, 1) if sample is not None else None,
                "step_excess_seconds": round(sample.step_excess, 1) if sample is not None else None,
                "language_step_p95_seconds": round(language_step_p95, 1) if language_step_p95 is not None else None,
                "language_step_allowance_seconds": thresholds.language_step_allowance,
                "phases": sample.phases_document() if sample is not None else None,
            },
            "fallbacks": dict(sorted(self.fallbacks.items())),
            "unknown_input_parts": sorted(self.unknown_input_parts),
        }


def metrics_from_store_repo(
    repository: str,
    repo_entry: dict | None,
    window: MeasurementWindow,
    *,
    error: str | None = None,
) -> RepositoryMetrics:
    if error is not None:
        return RepositoryMetrics(
            repository=repository,
            error=error,
            coverage=CoverageInfo(complete=False, window_days=window.window_days),
        )
    if repo_entry is None:
        return RepositoryMetrics(
            repository=repository,
            coverage=CoverageInfo(complete=False, window_days=window.window_days),
        )
    if not repo_entry.get("has_workflow", True):
        return RepositoryMetrics(
            repository=repository,
            has_workflow=False,
            coverage=CoverageInfo(
                complete=True,
                listed_from=repo_entry.get("listed_from"),
                listed_to=repo_entry.get("listed_to"),
                window_days=window.window_days,
            ),
        )

    listed_from_raw = repo_entry.get("listed_from")
    listed_to_raw = repo_entry.get("listed_to")
    listed_from = moment(listed_from_raw) if listed_from_raw else None
    listed_to = moment(listed_to_raw) if listed_to_raw else None
    listing_covers = (
        listed_from is not None
        and listed_to is not None
        and listed_from <= window.cutoff
        and listed_to >= window.now
    )
    covered_days: float | None = None
    if listed_from is not None and listed_to is not None:
        overlap_start = max(window.cutoff, listed_from)
        overlap_end = min(window.now, listed_to)
        covered_days = max(0.0, (overlap_end - overlap_start).total_seconds() / 86400.0)

    metrics = RepositoryMetrics(repository=repository, has_workflow=True)
    pending_runs = 0
    unchecked_unfinished_runs = 0
    stale_judge_runs = 0
    judge_versions: set[int] = set()
    rejudge_cutoff = window.now - datetime.timedelta(days=CI_PLAN_RETENTION_DAYS)

    for record in repo_entry.get("records", {}).values():
        created_at = moment(record["created_at"])
        if created_at < window.cutoff or created_at > window.now:
            continue
        state = record_state(record)
        if state is RecordState.PENDING:
            pending_runs += 1
            continue
        if state is RecordState.UNFINISHED:
            judged_raw = record.get("judged_at")
            if listed_to is not None and (judged_raw is None or moment(judged_raw) < listed_to):
                unchecked_unfinished_runs += 1
            continue
        judge_version = record.get("judge_version")
        if isinstance(judge_version, int):
            judge_versions.add(judge_version)
            if judge_version != JUDGE_VERSION and created_at >= rejudge_cutoff:
                stale_judge_runs += 1
        if state is RecordState.EXCLUDED_CANCELLED:
            continue
        if state is RecordState.DELETED:
            metrics.deleted_runs += 1
            continue
        if state is RecordState.NO_VERDICT:
            metrics.non_verdict_runs += 1
            continue
        if state is RecordState.VERDICT:
            duration = float(record["duration_seconds"])
            metrics.verdict_runs += 1
            metrics.durations.append(duration)
            if duration <= THREE_MINUTE_SECONDS:
                metrics.within_3m_count += 1
            if record.get("plan_found"):
                if record.get("selects_no_tests") is True:
                    metrics.no_tests_durations.append(duration)
                for reason, count in (record.get("fallbacks") or {}).items():
                    metrics.fallbacks[reason] = metrics.fallbacks.get(reason, 0) + count
                metrics.unknown_input_parts.update(record.get("unknown_input_parts") or ())
            fixed_cost_outcome_raw = record.get("fixed_cost_outcome")
            if fixed_cost_outcome_raw is not None:
                fixed_cost_outcome = FixedCostOutcome(fixed_cost_outcome_raw)
                if fixed_cost_outcome is FixedCostOutcome.SAMPLE and isinstance(record.get("fixed_cost_sample"), dict):
                    metrics.fixed_cost_samples.append(FixedCostSample.from_dict(record["fixed_cost_sample"]))
                elif fixed_cost_outcome is FixedCostOutcome.NON_SHARD:
                    metrics.non_shard_decided_runs += 1
                elif fixed_cost_outcome is FixedCostOutcome.UNMATCHED:
                    metrics.fixed_cost_unmatched_runs += 1

    complete = listing_covers and pending_runs == 0 and unchecked_unfinished_runs == 0 and stale_judge_runs == 0
    metrics.coverage = CoverageInfo(
        complete=complete,
        listed_from=listed_from_raw,
        listed_to=listed_to_raw,
        pending_runs=pending_runs,
        unchecked_unfinished_runs=unchecked_unfinished_runs,
        stale_judge_runs=stale_judge_runs,
        deleted_runs=metrics.deleted_runs,
        judge_versions=tuple(sorted(judge_versions)),
        window_days=window.window_days,
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
    window: MeasurementWindow,
    thresholds: FixedCostThresholds,
    workflow: str = DEFAULT_WORKFLOW,
    *,
    store_info: StoreLoadInfo | None = None,
    budget_stop_reason: str | None = None,
    show_judge_versions: bool = False,
) -> str:
    lines = [
        "## CI Goal Measurement",
        "",
        f"Window: {format_moment(window.cutoff)} to {format_moment(window.now)} ({window.window_days} days; verdict runs, re-run runs counted by first attempt).",
    ]
    if store_info is not None:
        lines.append(store_info.format_line(window.now))
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
        f"| Repository | Coverage | Verdict runs | Within 3 min (target 99%) | No-tests median (target 1 min) | Fixed cost p95 (assumed {thresholds.assumed_fixed_cost:.0f}s) | Fallbacks by reason | Parts with unknown inputs |",
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
            f"| {format_fixed_cost(item, thresholds.language_step_allowance)} "
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
    thresholds: FixedCostThresholds,
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
    if sample is not None and sample.fixed_cost > thresholds.assumed_fixed_cost:
        body = "\n".join([
            f"`{item.repository}` exceeded the assumed fixed cost ({thresholds.assumed_fixed_cost:.0f}s) at p95 over runs decided by a Linux shard with entry files.",
            "",
            f"- Measured p95 fixed cost: {sample.fixed_cost:.1f}s ({len(item.fixed_cost_samples)} runs)",
            f"- Outside entry-file step (excluding Python language step): {sample.outside_entry_step:.1f}s",
            f"- Entry-file step excess over planned estimate: {sample.step_excess:+.1f}s",
            (
                f"- Python language step p95: {language_step_p95:.1f}s (allowance {thresholds.language_step_allowance:.0f}s)"
                if language_step_p95 is not None
                else "- Python language step p95: n/a"
            ),
            f"- Phases: plan {sample.plan_seconds:.1f}s, queue {sample.queue_seconds:.1f}s, "
            f"shard start to entry step {sample.shard_to_entry_seconds:.1f}s, "
            f"after entry step {sample.after_entry_seconds:.1f}s, results {sample.results_seconds:.1f}s, verdict {sample.verdict_seconds:.1f}s",
            f"- Excluded runs: {item.non_shard_decided_runs} decided by a part, hygiene or Windows; {item.fixed_cost_unmatched_runs} unmatched",
            f"- Assumed fixed cost: {thresholds.assumed_fixed_cost:.1f}s",
            "",
            *context,
        ])
        findings.append(Finding(
            key=f"miss:{item.repository}:fixed-cost",
            title=f"CI fixed cost above assumed {thresholds.assumed_fixed_cost:.0f}s: {item.repository}",
            body=body,
        ))
    if language_step_p95 is not None and language_step_p95 > thresholds.language_step_allowance:
        body = "\n".join([
            f"`{item.repository}` exceeded the Python language-step allowance ({thresholds.language_step_allowance:.0f}s) at p95 over runs decided by a Linux shard with entry files.",
            "",
            f"- Measured Python language step p95: {language_step_p95:.1f}s ({len(item.fixed_cost_samples)} runs)",
            f"- Stated allowance: {thresholds.language_step_allowance:.1f}s",
            "",
            *context,
        ])
        findings.append(Finding(
            key=f"miss:{item.repository}:language-step",
            title=f"Python language step above allowance {thresholds.language_step_allowance:.0f}s: {item.repository}",
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


def write_json_report(path: str | None, payload: dict) -> None:
    if not path:
        return
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# Measurement session orchestration and CLI commands
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


def _needs_detail_fetch(record: dict, now: datetime.datetime) -> WorkKind | None:
    state = record_state(record)
    if state is RecordState.PENDING:
        return WorkKind.NEW
    if state is RecordState.UNFINISHED:
        return WorkKind.UNFINISHED
    if state in FROZEN_STATES:
        judge_version = record.get("judge_version")
        if judge_version != JUDGE_VERSION:
            rejudge_cutoff = now - datetime.timedelta(days=CI_PLAN_RETENTION_DAYS)
            if moment(record["created_at"]) >= rejudge_cutoff:
                return WorkKind.REJUDGE
    return None


class _MeasureSession:
    """Coordinates parallel listing, newest-first detail fetching, error rollback and atomic checkpointing."""

    def __init__(
        self,
        *,
        api: Api,
        budget: BudgetController,
        store: dict,
        repositories: list[str],
        workflow: str,
        window: MeasurementWindow,
        workers: int,
        store_out_path: Path | None,
    ) -> None:
        self.api = api
        self.budget = budget
        self.store = store
        self.repositories = repositories
        self.workflow = workflow
        self.window = window
        self.workers = max(1, workers)
        self.store_out_path = store_out_path
        self.pre_run_snapshots: dict[str, dict | None] = {
            repo: copy.deepcopy(store["repositories"].get(repo)) for repo in repositories
        }
        self.staged_repos: dict[str, dict] = {
            repo: copy.deepcopy(store["repositories"].get(repo) or empty_repo_entry()) for repo in repositories
        }
        self.repo_errors: dict[str, str] = {}
        self.seen_in_overlap: dict[str, set[int]] = {repo: set() for repo in repositories}
        self.progress_by_repo: dict[str, _RepoProgress] = {
            repo: _RepoProgress(repository=repo, started_at=time.monotonic()) for repo in repositories
        }
        self._lock = threading.Lock()
        self._items_since_checkpoint = 0
        self._last_checkpoint_at = time.monotonic()

    def checkpoint_store(self) -> None:
        if self.store_out_path is None:
            return
        with self._lock:
            prune_store(self.store, self.window.now, DEFAULT_RETENTION_DAYS)
            write_store_atomic(self.store_out_path, self.store, self.window.now)
            self._items_since_checkpoint = 0
            self._last_checkpoint_at = time.monotonic()

    def _record_repo_error_locked(self, repo: str, error: MeasurementError) -> bool:
        if repo in self.repo_errors:
            return False
        self.repo_errors[repo] = str(error)
        snapshot = self.pre_run_snapshots[repo]
        if snapshot is None:
            self.store["repositories"].pop(repo, None)
        else:
            self.store["repositories"][repo] = copy.deepcopy(snapshot)
        print(f"::error::{repo}: {error}", file=sys.stderr)
        return True

    def emit_repo_progress(self, repo: str) -> None:
        progress = self.progress_by_repo[repo]
        if progress.reported:
            return
        progress.reported = True
        elapsed = time.monotonic() - progress.started_at
        entry = self.store["repositories"].get(repo) or self.staged_repos.get(repo) or empty_repo_entry()
        records = entry.get("records", {})
        pending_left = sum(1 for rec in records.values() if record_state(rec) is RecordState.PENDING)
        if repo in self.repo_errors:
            _print_progress(f"{repo}: error={self.repo_errors[repo]} ({elapsed:.1f}s)")
        else:
            _print_progress(
                f"{repo}: new={progress.new_fetched} unfinished={progress.unfinished_rechecked} "
                f"pending={pending_left} kept={len(records)} ({elapsed:.1f}s)"
            )

    def _list_one(self, repo: str) -> None:
        self.progress_by_repo[repo].started_at = time.monotonic()
        try:
            _, seen = list_repository_runs(self.api, repo, self.workflow, self.staged_repos[repo], self.window)
            self.seen_in_overlap[repo] = seen
        except RateLimitError as error:
            self.budget.handle_rate_limit(error)
        except BudgetStoppedError:
            pass
        except MeasurementError as error:
            with self._lock:
                self._record_repo_error_locked(repo, error)

    def list_all(self) -> None:
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = [executor.submit(self._list_one, repo) for repo in self.repositories]
            for future in concurrent.futures.as_completed(futures):
                future.result()
        with self._lock:
            for repo in self.repositories:
                if repo not in self.repo_errors and (
                    self.staged_repos[repo].get("listed_to") is not None or not self.staged_repos[repo].get("has_workflow", True)
                ):
                    self.store["repositories"][repo] = self.staged_repos[repo]
        self.checkpoint_store()

    def fetch_details(self) -> None:
        work_items: list[tuple[datetime.datetime, str, str, WorkKind]] = []
        per_repo_counts: dict[str, int] = {repo: 0 for repo in self.repositories}
        for repo in self.repositories:
            if repo in self.repo_errors:
                self.emit_repo_progress(repo)
                continue
            entry = self.staged_repos[repo]
            if not entry.get("has_workflow", True):
                self.emit_repo_progress(repo)
                continue
            for key, record in entry["records"].items():
                work_kind = _needs_detail_fetch(record, self.window.now)
                if work_kind is not None:
                    work_items.append((moment(record["created_at"]), repo, key, work_kind))
                    per_repo_counts[repo] += 1
            self.progress_by_repo[repo].remaining = per_repo_counts[repo]
            if per_repo_counts[repo] == 0:
                self.emit_repo_progress(repo)

        work_items.sort(key=lambda item: (item[0], int(item[2])), reverse=True)
        plan_indices = {
            repo: RepoPlanArtifactIndex(self.api, repo, per_repo_counts[repo])
            for repo in self.repositories
            if per_repo_counts[repo] > 0
        }

        def finish_one_item(repo: str, key: str, work_kind: WorkKind) -> None:
            with self._lock:
                if repo in self.repo_errors or self.budget.stop_event.is_set():
                    return
                record = self.staged_repos[repo]["records"][key]
                in_overlap = int(record["run_id"]) in self.seen_in_overlap[repo]
            try:
                judged = judge_record(
                    self.api,
                    repo,
                    record,
                    self.window.now,
                    in_overlap=in_overlap,
                    plan_index=plan_indices[repo],
                    is_rejudge=(work_kind is WorkKind.REJUDGE),
                )
            except RateLimitError as error:
                self.budget.handle_rate_limit(error)
                return
            except BudgetStoppedError:
                return
            except MeasurementError as error:
                with self._lock:
                    recorded = self._record_repo_error_locked(repo, error)
                if recorded:
                    self.checkpoint_store()
                return

            repo_completed = False
            periodic_checkpoint_due = False
            with self._lock:
                if repo in self.repo_errors:
                    return
                self.staged_repos[repo]["records"][key] = judged
                self.store["repositories"][repo] = self.staged_repos[repo]
                progress = self.progress_by_repo[repo]
                if work_kind is WorkKind.NEW:
                    progress.new_fetched += 1
                elif work_kind is WorkKind.UNFINISHED:
                    progress.unfinished_rechecked += 1
                else:
                    progress.rejudged += 1
                progress.remaining -= 1
                self._items_since_checkpoint += 1
                if progress.remaining == 0:
                    repo_completed = True
                elif (
                    self._items_since_checkpoint >= CHECKPOINT_INTERVAL_ITEMS
                    or (time.monotonic() - self._last_checkpoint_at) >= CHECKPOINT_INTERVAL_SECONDS
                ):
                    periodic_checkpoint_due = True
            if repo_completed or periodic_checkpoint_due:
                self.checkpoint_store()
            if repo_completed:
                self.emit_repo_progress(repo)

        if work_items and not self.budget.stop_event.is_set():
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
                in_flight: set[concurrent.futures.Future[None]] = set()
                item_iter = iter(work_items)
                max_queued = self.workers * 2
                while True:
                    while len(in_flight) < max_queued and not self.budget.stop_event.is_set():
                        try:
                            _, repo, key, work_kind = next(item_iter)
                        except StopIteration:
                            break
                        with self._lock:
                            if repo in self.repo_errors:
                                continue
                        in_flight.add(executor.submit(finish_one_item, repo, key, work_kind))
                    if not in_flight:
                        break
                    done, in_flight = concurrent.futures.wait(in_flight, return_when=concurrent.futures.FIRST_COMPLETED)
                    for future in done:
                        future.result()

        self.checkpoint_store()
        for repo in self.repositories:
            self.emit_repo_progress(repo)


def run_measure(options: argparse.Namespace) -> int:
    start_monotonic = time.monotonic()
    now: datetime.datetime = options.now
    window = MeasurementWindow(
        cutoff=now - datetime.timedelta(days=options.window_days),
        now=now,
        window_days=options.window_days,
    )
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
        # Load store BEFORE measuring; any read failure or unreadable artifact aborts before writing store_out.
        if options.store_in:
            store, store_info = load_store_from_file(Path(options.store_in))
        elif own_api is not None:
            store, store_info = load_store_from_artifact(own_api, options.own_repository, now)
        else:
            store, store_info = empty_store(now), StoreLoadInfo(source="none", is_buildup=True)

        prune_store(store, now, DEFAULT_RETENTION_DAYS)

        if options.local:
            if not options.repository:
                raise SetupError("the local mode names its repositories with --repository")
            repositories = [repo for repo in options.repository if repo != options.own_repository]
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

        session = _MeasureSession(
            api=api,
            budget=budget,
            store=store,
            repositories=repositories,
            workflow=options.workflow,
            window=window,
            workers=int(options.workers),
            store_out_path=store_out_path,
        )
        session.list_all()
        session.fetch_details()

        results: list[RepositoryMetrics] = []
        for repo in repositories:
            if repo in session.repo_errors:
                results.append(metrics_from_store_repo(repo, None, window, error=session.repo_errors[repo]))
            else:
                results.append(metrics_from_store_repo(repo, store["repositories"].get(repo), window))

        thresholds = current_thresholds()
        findings = [
            finding
            for item in results
            for finding in misses_of(item, window.window_days, thresholds, workflow=options.workflow)
        ]
        opened: list[str] = []
        if own_api is not None:
            opened = report_findings(own_api, options.own_repository, findings, now=now, cooldown_days=FINDING_COOLDOWN_DAYS)

        total_pending = sum(item.coverage.pending_runs for item in results if item.error is None)
        total_elapsed = time.monotonic() - start_monotonic
        remaining_text = str(api.ratelimit_remaining) if api.ratelimit_remaining is not None else "n/a"
        stop_text = f" stopped={budget.stop_reason}" if budget.stop_reason else ""
        _print_progress(
            f"Measurement totals: requests={api.request_count} ratelimit_remaining={remaining_text} "
            f"pending={total_pending} elapsed={total_elapsed:.1f}s{stop_text}"
        )

        markdown = report_markdown(
            results,
            window,
            thresholds,
            workflow=options.workflow,
            store_info=store_info,
            budget_stop_reason=budget.stop_reason,
        )
        if opened:
            markdown += "\n" + "\n".join(f"- {line}" for line in opened) + "\n"
        print(markdown, end="")
        append_file(options.summary, markdown)
        write_json_report(options.json_output, {
            "window_days": window.window_days,
            "cutoff": format_moment(window.cutoff),
            "now": format_moment(window.now),
            "store": store_info.document(window.now),
            "requests": api.request_count,
            "ratelimit_remaining": api.ratelimit_remaining,
            "budget_stop_reason": budget.stop_reason,
            "pending_runs": total_pending,
            "repositories": {item.repository: item.document(thresholds) for item in results},
        })

        failed = bool(session.repo_errors) or (budget.rate_limit_failure is not None)
        if budget.rate_limit_failure is not None:
            print(f"::error::{budget.rate_limit_failure}", file=sys.stderr)
        return 1 if failed else 0
    finally:
        api.close()
        if own_api is not None:
            own_api.close()


def run_report(options: argparse.Namespace) -> int:
    store, store_info = load_store_from_file(Path(options.store))
    if options.now is not None:
        now = options.now
    elif store.get("generated_at"):
        now = moment(str(store["generated_at"]))
    else:
        now = datetime.datetime.now(datetime.timezone.utc)
    if options.window_days <= 0 or options.window_days > DEFAULT_RETENTION_DAYS:
        raise SetupError(f"--window-days must be between 1 and {DEFAULT_RETENTION_DAYS}")
    window = MeasurementWindow(
        cutoff=now - datetime.timedelta(days=options.window_days),
        now=now,
        window_days=options.window_days,
    )
    repositories = list(options.repository) if options.repository else sorted(store["repositories"])
    results = [metrics_from_store_repo(repo, store["repositories"].get(repo), window) for repo in repositories]
    thresholds = current_thresholds()
    markdown = report_markdown(
        results,
        window,
        thresholds,
        workflow=options.workflow,
        store_info=store_info,
        show_judge_versions=True,
    )
    print(markdown, end="")
    append_file(options.summary, markdown)
    write_json_report(options.json_output, {
        "window_days": window.window_days,
        "cutoff": format_moment(window.cutoff),
        "now": format_moment(window.now),
        "store": store_info.document(window.now),
        "repositories": {item.repository: item.document(thresholds) for item in results},
    })
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


def _add_report_arguments(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument("--workflow", default=DEFAULT_WORKFLOW, help="the pipeline caller workflow file name")
    subparser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS, help="rolling window length in days")
    subparser.add_argument("--summary", help="the file that receives the Markdown report")
    subparser.add_argument("--json-output", help="the file that receives the structured JSON report")


def parser() -> argparse.ArgumentParser:
    main_parser = argparse.ArgumentParser(description="Measure the shared CI goal for every consumer repository.")
    commands = main_parser.add_subparsers(dest="command", required=True)

    measure = commands.add_parser("measure", help="measure every consumer repository incrementally and open issues on missed targets")
    measure.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"), help="the address of the GitHub API")
    measure.add_argument("--own-repository", default="", help="the repository of the run, as OWNER/NAME: excluded from measurement and receives miss issues")
    measure.add_argument("--local", action="store_true", help="run with the owner's own token on the named repositories")
    measure.add_argument("--repository", action="append", help="a repository of the local mode, as OWNER/NAME")
    measure.add_argument("--now", type=moment, default=datetime.datetime.now(datetime.timezone.utc), help="the reference UTC timestamp of the measurement")
    measure.add_argument("--store-in", help="read the incremental records store from this local JSON file instead of the artifact")
    measure.add_argument("--store-out", help="write the updated incremental records store atomically to this JSON file")
    measure.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="maximum concurrent fetch workers")
    measure.add_argument("--max-rps", type=float, default=DEFAULT_MAX_RPS, help="maximum global API requests per second (0 disables pacing)")
    measure.add_argument("--time-budget", type=float, default=DEFAULT_TIME_BUDGET_SECONDS, help="global fetch time budget in seconds (0 disables)")
    measure.add_argument("--request-budget", type=int, default=DEFAULT_REQUEST_BUDGET, help="maximum API requests for fetching (0 disables)")
    measure.add_argument("--ratelimit-reserve", type=int, default=DEFAULT_RATELIMIT_RESERVE, help="stop fetching when x-ratelimit-remaining falls below this reserve")
    measure.add_argument("--min-secondary-wait", type=float, default=DEFAULT_MIN_SECONDARY_WAIT_SECONDS, help="minimum backoff seconds on a secondary rate limit without Retry-After")
    _add_report_arguments(measure)

    report = commands.add_parser("report", help="compute the CI goal report over a chosen window (up to 90 days) from a downloaded store file")
    report.add_argument("--store", required=True, help="the downloaded measurement-records JSON file")
    report.add_argument("--now", type=moment, default=None, help="the reference UTC timestamp (defaults to the store's generated_at)")
    report.add_argument("--repository", action="append", help="limit the report to named repositories, as OWNER/NAME")
    _add_report_arguments(report)

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
