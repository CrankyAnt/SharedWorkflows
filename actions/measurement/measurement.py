#!/usr/bin/env python3
"""Measure the shared CI goal for every consumer repository.

    python3 measurement.py measure [--own-repository OWNER/NAME] [common options]
    python3 measurement.py measure --local --repository OWNER/NAME [--repository ...] [common options]
    python3 measurement.py failure --own-repository OWNER/NAME --run-url URL [--api-url URL] [--summary FILE]

    common options: [--api-url URL] [--workflow FILE] [--window-days DAYS] [--now TIME]
                    [--summary FILE] [--json-output FILE]
"""

from __future__ import annotations

import argparse
import datetime
import enum
import io
import json
import math
import os
import re
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

API_TIMEOUT = 30
PAGE = 100
CREATED_SEARCH_CAP = 1000
DEFAULT_WINDOW_DAYS = 28
DEFAULT_WORKFLOW = "ci.yml"
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


class Api:
    """The GitHub REST API, asked with one token. A 404 returns None; any other error raises MeasurementError."""

    def __init__(self, base: str, token: str) -> None:
        self.base, self.token = base.rstrip("/"), token

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        return {
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self.token}",
        }

    def request_json(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        headers = self._headers()
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(f"{self.base}/{path}", data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:  # noqa: S310 - the API host of the run
                text = response.read().decode("utf-8")
                return response.status, json.loads(text) if text else None
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return 404, None
            raise MeasurementError(f"{method} {path}: HTTP {error.code}") from error
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise MeasurementError(f"{method} {path}: {getattr(error, 'reason', error)}") from error

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
        origin = urllib.parse.urlsplit(self.base).netloc

        class _RedirectHandler(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
                if redirected is not None and urllib.parse.urlsplit(newurl).netloc != origin:
                    redirected.remove_header("Authorization")
                return redirected

        opener = urllib.request.build_opener(_RedirectHandler)
        request = urllib.request.Request(f"{self.base}/{path}", headers=self._headers(), method="GET")
        try:
            with opener.open(request, timeout=API_TIMEOUT) as response:  # noqa: S310 - the API host of the run
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code in (404, 410):
                return None
            raise MeasurementError(f"GET {path}: HTTP {error.code}") from error
        except (urllib.error.URLError, OSError) as error:
            raise MeasurementError(f"GET {path}: {getattr(error, 'reason', error)}") from error


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
    candidates = [item for item in artifacts if item.get("name") == PLAN_ARTIFACT_NAME and not item.get("expired")]
    if max_created_at is not None:
        qualified: list[tuple[datetime.datetime, dict]] = []
        for item in candidates:
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
        artifact = qualified[0][1]
    else:
        artifact = candidates[0] if candidates else None
    if artifact is None or not isinstance(artifact.get("id"), int):
        return None
    raw = api.get_bytes(f"repos/{repository}/actions/artifacts/{artifact['id']}/zip")
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


def moment(text: str) -> datetime.datetime:
    parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def token(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SetupError(f"the environment holds no token in {name}")
    return value


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


@dataclass
class RepositoryMetrics:
    repository: str
    has_workflow: bool = True
    error: str | None = None
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


def measure_repository(api: Api, repository: str, workflow: str, cutoff: datetime.datetime, now: datetime.datetime) -> RepositoryMetrics:
    metrics = RepositoryMetrics(repository=repository)
    since = cutoff.strftime("%Y-%m-%d")
    created_query = urllib.parse.quote(f">={since}", safe="")
    runs = api.pages(
        f"repos/{repository}/actions/workflows/{urllib.parse.quote(workflow)}/runs?created={created_query}",
        "workflow_runs",
        allow_missing=True,
    )
    if runs is None:
        metrics.has_workflow = False
        return metrics
    if len(runs) >= CREATED_SEARCH_CAP:
        raise MeasurementError(
            f"workflow runs listing for {workflow} reached GitHub's {CREATED_SEARCH_CAP}-result cap under created=>={since}"
        )
    for run in runs:
        if run.get("event") in MANUAL_EVENTS:
            continue
        created_raw = run.get("created_at")
        if not isinstance(created_raw, str) or not created_raw:
            continue
        created_at = moment(created_raw)
        if created_at < cutoff or created_at > now:
            continue
        run_id = run.get("id")
        if not isinstance(run_id, int):
            continue
        attempt = int(run.get("run_attempt") or 1)
        if attempt > 1:
            attempt_path = f"repos/{repository}/actions/runs/{run_id}/attempts/1"
            attempt_status, attempt_doc = api.request_json("GET", attempt_path)
            if attempt_status == 404 or not isinstance(attempt_doc, dict):
                raise MeasurementError(f"GET {attempt_path}: expected attempt 1 object (HTTP {attempt_status})")
            if attempt_doc.get("status") != "completed":
                continue
            if attempt_doc.get("conclusion") == "cancelled":
                continue
            jobs = api.pages(f"{attempt_path}/jobs", "jobs", allow_missing=False)
        else:
            if run.get("status") != "completed":
                continue
            if run.get("conclusion") == "cancelled":
                continue
            jobs = api.pages(f"repos/{repository}/actions/runs/{run_id}/jobs", "jobs", allow_missing=True)
        if not jobs:
            metrics.non_verdict_runs += 1
            continue
        verdict = verdict_job(jobs)
        if verdict is None:
            metrics.non_verdict_runs += 1
            continue
        completed_at = moment(str(verdict["completed_at"]))
        duration = max(0.0, (completed_at - created_at).total_seconds())
        metrics.verdict_runs += 1
        metrics.durations.append(duration)
        if duration <= THREE_MINUTE_SECONDS:
            metrics.within_3m_count += 1
        plan = read_plan_artifact(api, repository, run_id, max_created_at=completed_at if attempt > 1 else None)
        if plan is not None:
            if plan_selects_no_tests(plan):
                metrics.no_tests_durations.append(duration)
            for reason, count in plan_fallbacks(plan).items():
                metrics.fallbacks[reason] = metrics.fallbacks.get(reason, 0) + count
            metrics.unknown_input_parts.update(plan_unknown_input_parts(plan))
        outcome, sample = fixed_cost_of_run(created_at, verdict, jobs, plan)
        if outcome is FixedCostOutcome.SAMPLE and sample is not None:
            metrics.fixed_cost_samples.append(sample)
        elif outcome is FixedCostOutcome.NON_SHARD:
            metrics.non_shard_decided_runs += 1
        elif outcome is FixedCostOutcome.UNMATCHED:
            metrics.fixed_cost_unmatched_runs += 1
    return metrics


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
) -> str:
    lines = [
        "## CI Goal Measurement",
        "",
        f"Window: {cutoff.strftime('%Y-%m-%dT%H:%M:%SZ')} to {now.strftime('%Y-%m-%dT%H:%M:%SZ')} ({window_days} days; verdict runs, re-run runs counted by first attempt).",
        "",
    ]
    if not results:
        lines.append("No consumer repositories were measured.")
        return "\n".join(lines) + "\n"
    lines += [
        f"| Repository | Verdict runs | Within 3 min (target 99%) | No-tests median (target 1 min) | Fixed cost p95 (assumed {assumed_fixed_cost:.0f}s) | Fallbacks by reason | Parts with unknown inputs |",
        "| --- | ---: | --- | --- | --- | --- | --- |",
    ]
    for item in results:
        if item.error is not None:
            reason = item.error.replace("\n", " ").replace("|", "/")
            lines.append(f"| `{item.repository}` | n/a | could not be measured: {reason} | n/a | n/a | none | none |")
            continue
        if not item.has_workflow:
            lines.append(f"| `{item.repository}` | n/a | no `{workflow}` workflow | n/a | n/a | none | none |")
            continue
        lines.append(
            f"| `{item.repository}` | {item.verdict_runs} | {format_share(item.within_3m_share, item.within_3m_count, item.verdict_runs)} "
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
    """Return one Finding for each goal target or coverage gap that ``item`` reports."""
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


def report_findings(api: Api, repository: str, findings: list[Finding]) -> list[str]:
    """Open one issue per finding unless an issue with the same marker is already open."""
    if not findings:
        return []
    still_open: set[str] = set()
    for issue in api.pages(f"repos/{repository}/issues?state=open") or []:
        still_open.update(MARKER_PATTERN.findall(issue.get("body") or ""))
    opened: list[str] = []
    for finding in findings:
        if finding.key in still_open:
            continue
        created = api.send("POST", f"repos/{repository}/issues", {
            "title": finding.title,
            "body": f"{finding.body}\n\n{MARKER.format(key=finding.key)}\n",
        })
        still_open.add(finding.key)
        opened.append(f"Opened issue #{created.get('number')}: {finding.title}")
    return opened


def append_file(path: str | None, text: str) -> None:
    if path:
        with open(path, "a", encoding="utf-8", newline="\n") as stream:
            stream.write(text)


def run_measure(options: argparse.Namespace) -> int:
    api = Api(options.api_url, token("GH_TOKEN"))
    if options.local:
        if not options.repository:
            raise SetupError("the local mode names its repositories with --repository")
        repositories = list(options.repository)
    else:
        if options.repository:
            raise SetupError("without --local the repositories come from the App's installation; --repository belongs to the local mode")
        found = api.pages("installation/repositories", "repositories") or []
        repositories = [str(item["full_name"]) for item in found if not item.get("archived")]
    now = options.now
    cutoff = now - datetime.timedelta(days=options.window_days)
    results: list[RepositoryMetrics] = []
    failed = False
    for repository in repositories:
        if repository == options.own_repository:
            continue
        try:
            results.append(measure_repository(api, repository, options.workflow, cutoff, now))
        except MeasurementError as error:
            failed = True
            results.append(RepositoryMetrics(repository=repository, error=str(error)))
            print(f"::error::{repository}: {error}", file=sys.stderr)
    assumed_fixed_cost = assumed_fixed_cost_seconds()
    language_step_allowance = language_step_allowance_seconds()
    findings = [
        finding
        for item in results
        for finding in misses_of(item, options.window_days, assumed_fixed_cost, language_step_allowance, workflow=options.workflow)
    ]
    opened: list[str] = []
    if options.own_repository and not options.local:
        opened = report_findings(Api(options.api_url, token("GITHUB_TOKEN")), options.own_repository, findings)
    markdown = report_markdown(
        results,
        options.window_days,
        cutoff,
        now,
        assumed_fixed_cost,
        language_step_allowance,
        workflow=options.workflow,
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
            "cutoff": cutoff.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "now": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "repositories": {item.repository: item.document(assumed_fixed_cost, language_step_allowance) for item in results},
        }
        destination.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    return 1 if failed else 0


def run_failure(options: argparse.Namespace) -> int:
    body = f"The run {options.run_url} of the CI goal measurement failed. Its log says which repository could not be measured and why."
    opened = report_findings(
        Api(options.api_url, token("GITHUB_TOKEN")),
        options.own_repository,
        [Finding("failure", "The CI goal measurement failed", body)],
    )
    text = "## Measurement failure\n\n" + ("\n".join(f"- {line}" for line in opened) if opened else "An issue for a failed run is already open.") + "\n"
    print(text, end="")
    append_file(options.summary, text)
    return 0


def parser() -> argparse.ArgumentParser:
    main_parser = argparse.ArgumentParser(description="Measure the shared CI goal for every consumer repository.")
    commands = main_parser.add_subparsers(dest="command", required=True)
    measure = commands.add_parser("measure", help="measure every consumer repository and open issues on missed targets")
    measure.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"), help="the address of the GitHub API")
    measure.add_argument("--own-repository", default="", help="the repository of the run, as OWNER/NAME: excluded from measurement and receives miss issues")
    measure.add_argument("--local", action="store_true", help="run with the owner's own token on the named repositories")
    measure.add_argument("--repository", action="append", help="a repository of the local mode, as OWNER/NAME")
    measure.add_argument("--workflow", default=DEFAULT_WORKFLOW, help="the pipeline caller workflow file name")
    measure.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS, help="rolling window length in days")
    measure.add_argument("--now", type=moment, default=datetime.datetime.now(datetime.timezone.utc), help="the reference UTC timestamp of the measurement")
    measure.add_argument("--summary", help="the file that receives the Markdown report")
    measure.add_argument("--json-output", help="the file that receives the structured JSON report")
    failure = commands.add_parser("failure", help="open an issue for a failed measurement run")
    failure.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"), help="the address of the GitHub API")
    failure.add_argument("--own-repository", required=True, help="the repository of the run, as OWNER/NAME")
    failure.add_argument("--run-url", required=True, help="the address of the failed run")
    failure.add_argument("--summary", help="the file that receives the Markdown summary")
    return main_parser


def main(argv: list[str] | None = None) -> int:
    options = parser().parse_args(argv)
    try:
        return {"measure": run_measure, "failure": run_failure}[options.command](options)
    except SetupError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2
    except MeasurementError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
