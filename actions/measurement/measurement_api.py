"""GitHub REST API client with per-thread keep-alive reuse, global pacing, budgets and rate-limit handling."""

from __future__ import annotations

import datetime
import enum
import http.client
import io
import json
import os
import ssl
import threading
import time
import urllib.parse
import zipfile
from collections.abc import Iterator

API_TIMEOUT = 30
PAGE = 100
DEFAULT_MAX_RPS = 10.0
DEFAULT_TIME_BUDGET_SECONDS = 480.0
DEFAULT_REQUEST_BUDGET = 4000
DEFAULT_RATELIMIT_RESERVE = 500
DEFAULT_MIN_SECONDARY_WAIT_SECONDS = 60.0
MAX_REDIRECTS = 5


class SetupError(Exception):
    """The command arguments or tokens cannot be used; the message says why."""


class MeasurementError(Exception):
    """A repository could not be measured or an API request failed; the message says why."""


class BudgetStoppedError(Exception):
    """Fetching stopped cleanly because a time, request or rate-limit budget was reached."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RateLimitKind(enum.Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"


class RateLimitError(MeasurementError):
    """GitHub returned HTTP 403 or 429 for a primary or secondary rate limit."""

    def __init__(self, message: str, *, kind: RateLimitKind, retry_after: float | None = None) -> None:
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


def extract_json_from_zip(raw: bytes, preferred_name: str, *, error_type: type[Exception]) -> object:
    """Read ``preferred_name`` (or the first ``.json`` member) from an artifact zip archive."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = [name for name in archive.namelist() if name.endswith(".json")]
            if not names:
                raise error_type("archive contains no JSON file")
            chosen = preferred_name if preferred_name in names else names[0]
            return json.loads(archive.read(chosen).decode("utf-8"))
    except error_type:
        raise
    except (zipfile.BadZipFile, OSError, UnicodeDecodeError, ValueError) as error:
        raise error_type(str(error)) from error


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
            if error.kind is RateLimitKind.SECONDARY and not self._secondary_waited:
                self._secondary_waited = True
                requested_wait = max(0.0, error.retry_after) if error.retry_after is not None else self.min_secondary_wait
                if self.deadline is None or (time.monotonic() + requested_wait) <= self.deadline:
                    wait_for = requested_wait
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
        for connection in connections:
            try:
                connection.close()
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
        connection: http.client.HTTPConnection | None = getattr(self._local, "conn", None)
        if connection is None:
            connection = self._open_connection(self._scheme, self._host, self._port)
            self._local.conn = connection
            with self._connections_lock:
                self._connections.append(connection)
        return connection

    def _reset_connection(self) -> None:
        connection: http.client.HTTPConnection | None = getattr(self._local, "conn", None)
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
            self._local.conn = None

    def _open_connection(self, scheme: str, host: str, port: int | None) -> http.client.HTTPConnection:
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=API_TIMEOUT, context=self._ssl_context)
        return http.client.HTTPConnection(host, port, timeout=API_TIMEOUT)

    def _record_ratelimit(self, headers: http.client.HTTPMessage) -> int | None:
        remaining_header = headers.get("X-RateLimit-Remaining")
        reset_header = headers.get("X-RateLimit-Reset")
        remaining: int | None = None
        if remaining_header is not None:
            try:
                remaining = int(remaining_header.strip())
            except ValueError:
                remaining = None
        reset_timestamp: int | None = None
        if reset_header is not None:
            try:
                reset_timestamp = int(reset_header.strip())
            except ValueError:
                reset_timestamp = None
        with self._metrics_lock:
            if remaining is not None:
                self.ratelimit_remaining = remaining
            if reset_timestamp is not None:
                self.ratelimit_reset = reset_timestamp
        return remaining

    def _before_request(self) -> None:
        if self.budget is not None:
            with self._metrics_lock:
                count = self.request_count
                remaining = self.ratelimit_remaining
            self.budget.check_before_request(count, remaining)
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
        retry_after_header = headers.get("Retry-After")
        retry_after: float | None = None
        if retry_after_header is not None:
            try:
                retry_after = float(retry_after_header.strip())
            except ValueError:
                retry_after = None
        message_lower = ""
        try:
            document = json.loads(body_bytes.decode("utf-8", errors="replace"))
            if isinstance(document, dict) and isinstance(document.get("message"), str):
                message_lower = document["message"].lower()
        except ValueError:
            message_lower = body_bytes.decode("utf-8", errors="replace").lower()
        if remaining == 0:
            return RateLimitError(
                f"{method} {path}: HTTP {status} primary rate limit exceeded",
                kind=RateLimitKind.PRIMARY,
            )
        if status == 429 or retry_after is not None or "secondary rate limit" in message_lower:
            return RateLimitError(
                f"{method} {path}: HTTP {status} secondary rate limit exceeded",
                kind=RateLimitKind.SECONDARY,
                retry_after=retry_after,
            )
        if "rate limit" in message_lower:
            return RateLimitError(
                f"{method} {path}: HTTP {status} primary rate limit exceeded",
                kind=RateLimitKind.PRIMARY,
            )
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
            connection = self._get_connection()
            try:
                connection.request(method, target, body=body, headers=headers)
                response = connection.getresponse()
                payload = response.read()
                response_headers = response.headers
                status = response.status
                if response.will_close:
                    self._reset_connection()
                return status, response_headers, payload
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
        status, response_headers, payload = self._raw_request(method, path, headers=headers, body=data)
        remaining = self._record_ratelimit(response_headers)
        if status == 404:
            return 404, None
        rate_error = self._check_rate_limit_error(method, path, status, response_headers, payload, remaining)
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

    def iter_pages(
        self,
        path: str,
        key: str | None = None,
        *,
        allow_missing: bool = False,
    ) -> Iterator[tuple[int, int | None, list[dict]]]:
        """Yield ``(page_number, total_count, items)`` across pages; stop immediately on 404 when ``allow_missing``."""
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            status, document = self.request_json("GET", f"{path}{separator}per_page={PAGE}&page={page}")
            if status == 404:
                if allow_missing and page == 1:
                    return
                raise MeasurementError(f"GET {path}: HTTP 404")
            total_count = (
                document.get("total_count")
                if isinstance(document, dict) and isinstance(document.get("total_count"), int) and not isinstance(document.get("total_count"), bool)
                else None
            )
            found = document.get(key) if key and isinstance(document, dict) else document
            if not isinstance(found, list):
                raise MeasurementError(f"GET {path}: expected a list")
            items = [item for item in found if isinstance(item, dict)]
            yield page, total_count, items
            if len(found) < PAGE:
                return
            page += 1

    def pages(self, path: str, key: str | None = None, *, allow_missing: bool = False) -> list[dict] | None:
        collected: list[dict] = []
        saw_page = False
        for _, _, items in self.iter_pages(path, key, allow_missing=allow_missing):
            saw_page = True
            collected.extend(items)
        if not saw_page and allow_missing:
            return None
        return collected

    def get_bytes(self, path: str) -> bytes | None:
        """Download binary content from ``path`` (e.g. an artifact zip), returning None on 404/410."""
        headers = self._headers()
        status, response_headers, payload = self._raw_request("GET", path, headers=headers)
        remaining = self._record_ratelimit(response_headers)
        current_url = f"{self.base}/{path.lstrip('/')}"
        for _ in range(MAX_REDIRECTS):
            if status in (301, 302, 303, 307, 308):
                location = response_headers.get("Location")
                if not location:
                    raise MeasurementError(f"GET {path}: HTTP {status} without Location")
                next_url = urllib.parse.urljoin(current_url, location)
                parsed = urllib.parse.urlsplit(next_url)
                redirect_headers = self._headers() if parsed.netloc == self._origin else {"User-Agent": "SharedWorkflows-Measurement"}
                connection = self._open_connection(parsed.scheme.lower(), parsed.hostname or "", parsed.port)
                try:
                    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
                    connection.request("GET", target or "/", headers=redirect_headers)
                    response = connection.getresponse()
                    status = response.status
                    response_headers = response.headers
                    payload = response.read()
                except (http.client.HTTPException, OSError) as error:
                    raise MeasurementError(f"GET {path}: {error}") from error
                finally:
                    try:
                        connection.close()
                    except OSError:
                        pass
                current_url = next_url
                continue
            break
        if status in (404, 410):
            return None
        rate_error = self._check_rate_limit_error("GET", path, status, response_headers, payload, remaining)
        if rate_error is not None:
            raise rate_error
        if status < 200 or status >= 300:
            raise MeasurementError(f"GET {path}: HTTP {status}")
        return payload
