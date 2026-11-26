"""Incremental measurement store: schema, validation, migration, pruning, persistence and artifact discovery."""

from __future__ import annotations

import datetime
import enum
import json
import os
import threading
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from measurement_api import (
    Api,
    MeasurementError,
    SetupError,
    extract_json_from_zip,
    format_moment,
    moment,
)

SCHEMA_VERSION = 1
JUDGE_VERSION = 2
DEFAULT_RETENTION_DAYS = 90
MEASUREMENT_STORE_ARTIFACT = "measurement-records"
MEASUREMENT_STORE_FILENAME = "measurement-records.json"


class StoreInvalidError(Exception):
    """A measurement store file or artifact failed structural or schema validation."""


class RecordState(enum.Enum):
    PENDING = "pending"
    UNFINISHED = "unfinished"
    VERDICT = "verdict"
    NO_VERDICT = "no-verdict"
    EXCLUDED_CANCELLED = "excluded-cancelled"
    EXCLUDED_EDITED = "excluded-edited"
    DELETED = "deleted"


FROZEN_STATES = frozenset({
    RecordState.VERDICT,
    RecordState.NO_VERDICT,
    RecordState.EXCLUDED_CANCELLED,
    RecordState.EXCLUDED_EDITED,
    RecordState.DELETED,
})


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


def make_record(
    *,
    run_id: int,
    created_at: str,
    run_attempt: int,
    status: str,
    conclusion: str | None = None,
    updated_at: str | None = None,
    state: RecordState = RecordState.PENDING,
    verdict_completed_at: str | None = None,
    duration_seconds: float | None = None,
    plan_found: bool | None = None,
    selects_no_tests: bool | None = None,
    fallbacks: dict[str, int] | None = None,
    unknown_input_parts: list[str] | None = None,
    fixed_cost_outcome: FixedCostOutcome | None = None,
    fixed_cost_sample: dict[str, float] | None = None,
    judged_at: str | None = None,
    judge_version: int | None = None,
) -> dict:
    """Create a normalized store record dictionary."""
    return {
        "run_id": run_id,
        "created_at": created_at,
        "run_attempt": run_attempt,
        "status": status,
        "conclusion": conclusion,
        "updated_at": updated_at,
        "state": state.value,
        "verdict_completed_at": verdict_completed_at,
        "duration_seconds": duration_seconds,
        "plan_found": plan_found,
        "selects_no_tests": selects_no_tests,
        "fallbacks": dict(fallbacks) if fallbacks else {},
        "unknown_input_parts": list(unknown_input_parts) if unknown_input_parts else [],
        "fixed_cost_outcome": fixed_cost_outcome.value if fixed_cost_outcome is not None else None,
        "fixed_cost_sample": fixed_cost_sample,
        "judged_at": judged_at,
        "judge_version": judge_version,
    }


def record_state(record: dict) -> RecordState:
    return RecordState(record["state"])


def empty_store(now: datetime.datetime) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "judge_version": JUDGE_VERSION,
        "retention_days": DEFAULT_RETENTION_DAYS,
        "generated_at": format_moment(now),
        "repositories": {},
    }


def empty_repo_entry() -> dict:
    return {
        "has_workflow": True,
        "listed_from": None,
        "listed_to": None,
        "records": {},
    }


def require_moment(value: object, label: str) -> datetime.datetime:
    if not isinstance(value, str) or not value:
        raise StoreInvalidError(f"{label} must be a non-empty timestamp string")
    try:
        return moment(value)
    except ValueError as error:
        raise StoreInvalidError(f"{label} has an invalid timestamp") from error


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
        value = raw.get(key)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise StoreInvalidError(f"fixed_cost_sample.{key} must be numeric")
        out[key] = float(value)
    return out


def _normalize_record(key: str, raw: object, *, migrating: bool) -> dict:
    if not isinstance(raw, dict):
        raise StoreInvalidError(f"record {key} must be an object")
    run_id = raw.get("run_id")
    if not isinstance(run_id, int) or isinstance(run_id, bool):
        raise StoreInvalidError(f"record {key} lacks integer run_id")
    created_raw = raw.get("created_at")
    require_moment(created_raw, f"record {key} created_at")
    assert isinstance(created_raw, str)

    raw_state = raw.get("state")
    try:
        parsed_state = RecordState(raw_state)
    except ValueError as error:
        if migrating:
            parsed_state = RecordState.PENDING
        else:
            raise StoreInvalidError(f"record {key} has invalid state {raw_state!r}") from error

    attempt = raw.get("run_attempt", 1)
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        if migrating:
            attempt = 1
        else:
            raise StoreInvalidError(f"record {key} has invalid run_attempt")
    status_raw = raw.get("status")
    status = status_raw if isinstance(status_raw, str) else "unknown"
    conclusion_raw = raw.get("conclusion")
    conclusion = conclusion_raw if isinstance(conclusion_raw, str) else None
    updated_raw = raw.get("updated_at")
    updated_at = updated_raw if isinstance(updated_raw, str) else None

    judged_at = raw.get("judged_at")
    if judged_at is not None:
        require_moment(judged_at, f"record {key} judged_at")

    judge_version = raw.get("judge_version")
    if judge_version is not None and (not isinstance(judge_version, int) or isinstance(judge_version, bool)):
        if migrating:
            judge_version = 0
        else:
            raise StoreInvalidError(f"record {key} has invalid judge_version")

    normalized = make_record(
        run_id=run_id,
        created_at=created_raw,
        run_attempt=attempt,
        status=status,
        conclusion=conclusion,
        updated_at=updated_at,
        state=parsed_state,
        judged_at=judged_at,
        judge_version=judge_version,
    )
    if parsed_state is RecordState.VERDICT:
        verdict_completed_raw = raw.get("verdict_completed_at")
        duration_raw = raw.get("duration_seconds")
        valid_verdict = isinstance(duration_raw, (int, float)) and not isinstance(duration_raw, bool)
        if valid_verdict:
            try:
                require_moment(verdict_completed_raw, f"verdict record {key} verdict_completed_at")
            except StoreInvalidError:
                valid_verdict = False
        if not valid_verdict:
            if migrating:
                normalized["state"] = RecordState.PENDING.value
                normalized["judge_version"] = None
                return normalized
            raise StoreInvalidError(f"verdict record {key} lacks valid verdict_completed_at/duration_seconds")
        assert isinstance(verdict_completed_raw, str)
        assert isinstance(duration_raw, (int, float))
        normalized["verdict_completed_at"] = verdict_completed_raw
        normalized["duration_seconds"] = float(duration_raw)

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

        fallbacks = raw.get("fallbacks", {})
        if isinstance(fallbacks, dict):
            normalized["fallbacks"] = {
                reason: count
                for reason, count in fallbacks.items()
                if isinstance(reason, str) and isinstance(count, int) and not isinstance(count, bool) and count > 0
            }
        elif not migrating:
            raise StoreInvalidError(f"verdict record {key} has invalid fallbacks")

        unknown_input_parts = raw.get("unknown_input_parts", [])
        if isinstance(unknown_input_parts, list):
            normalized["unknown_input_parts"] = sorted({part for part in unknown_input_parts if isinstance(part, str)})
        elif not migrating:
            raise StoreInvalidError(f"verdict record {key} has invalid unknown_input_parts")

        fixed_cost_outcome_raw = raw.get("fixed_cost_outcome")
        if fixed_cost_outcome_raw is None and migrating:
            parsed_outcome = FixedCostOutcome.SKIP
        else:
            try:
                parsed_outcome = FixedCostOutcome(fixed_cost_outcome_raw)
            except ValueError as error:
                raise StoreInvalidError(f"verdict record {key} has invalid fixed_cost_outcome {fixed_cost_outcome_raw!r}") from error
        normalized["fixed_cost_outcome"] = parsed_outcome.value

        if parsed_outcome is FixedCostOutcome.SAMPLE:
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
    elif parsed_state in FROZEN_STATES and normalized["judge_version"] is None:
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
                require_moment(value, f"{repo_name}: {label}")
        records_raw = repo_doc.get("records", {})
        if not isinstance(records_raw, dict):
            raise StoreInvalidError(f"{repo_name}: records must be an object")
        records_out: dict[str, dict] = {}
        for rec_key, rec_val in records_raw.items():
            normalized = _normalize_record(str(rec_key), rec_val, migrating=migrating)
            records_out[str(normalized["run_id"])] = normalized
        repos_out[repo_name] = {
            "has_workflow": has_workflow,
            "listed_from": listed_from,
            "listed_to": listed_to,
            "records": records_out,
        }
    generated_at = document.get("generated_at")
    if generated_at is not None:
        require_moment(generated_at, "generated_at")
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
        for key, record in repo_entry["records"].items():
            if moment(record["created_at"]) >= retention_cutoff:
                kept[key] = record
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
    malformed_entries: int = 0
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
        if self.malformed_entries:
            extras.append(f"skipped {self.malformed_entries} malformed artifact entry(ies)")
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
            "malformed_entries": self.malformed_entries,
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


def load_store_from_artifact(own_api: Api, own_repository: str, now: datetime.datetime) -> tuple[dict, StoreLoadInfo]:
    """Read the newest readable ``measurement-records`` artifact on ``main``, falling back to older unexpired ones.
    HTTP/network errors and unreadable or malformed unexpired artifacts fail loudly so history is never overwritten."""
    artifacts = own_api.pages(
        f"repos/{own_repository}/actions/artifacts?name={urllib.parse.quote(MEASUREMENT_STORE_ARTIFACT)}",
        "artifacts",
        allow_missing=False,
    )
    assert artifacts is not None
    candidates: list[tuple[datetime.datetime, int, dict]] = []
    malformed_entries = 0
    for item in artifacts:
        if item.get("name") != MEASUREMENT_STORE_ARTIFACT or item.get("expired"):
            continue
        workflow_run = item.get("workflow_run")
        head_branch = workflow_run.get("head_branch") if isinstance(workflow_run, dict) else None
        if isinstance(head_branch, str) and head_branch and head_branch != "main":
            continue
        artifact_id = item.get("id")
        created_raw = item.get("created_at")
        if (
            head_branch != "main"
            or not isinstance(artifact_id, int)
            or isinstance(artifact_id, bool)
            or not isinstance(created_raw, str)
            or not created_raw
        ):
            malformed_entries += 1
            continue
        try:
            created_at = moment(created_raw)
        except ValueError:
            malformed_entries += 1
            continue
        candidates.append((created_at, artifact_id, item))

    if not candidates:
        if malformed_entries:
            raise MeasurementError(
                f"{malformed_entries} unexpired {MEASUREMENT_STORE_ARTIFACT} artifact entry(ies) were malformed and unreadable"
            )
        return empty_store(now), StoreLoadInfo(source="none", is_buildup=True)

    candidates.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    skipped_invalid: list[int] = []
    for created_at, artifact_id, _ in candidates:
        raw = own_api.get_bytes(f"repos/{own_repository}/actions/artifacts/{artifact_id}/zip")
        if not raw:
            skipped_invalid.append(artifact_id)
            continue
        try:
            document = extract_json_from_zip(raw, MEASUREMENT_STORE_FILENAME, error_type=StoreInvalidError)
            store, migrated_from = validate_and_migrate_store(document)
        except StoreInvalidError:
            skipped_invalid.append(artifact_id)
            continue
        return store, StoreLoadInfo(
            source="artifact",
            artifact_id=artifact_id,
            created_at=created_at,
            skipped_invalid_ids=tuple(skipped_invalid),
            malformed_entries=malformed_entries,
            migrated_from=migrated_from,
            is_buildup=False,
        )

    skipped_text = ", ".join(f"#{art_id}" for art_id in skipped_invalid)
    raise MeasurementError(
        f"all {len(candidates)} unexpired {MEASUREMENT_STORE_ARTIFACT} artifact(s) on main were unreadable ({skipped_text})"
    )
