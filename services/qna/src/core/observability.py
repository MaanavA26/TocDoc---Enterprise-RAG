"""Request-ID middleware and structured-event logging helpers (Phase 2 Workstream B PR-1).

Provides the minimum production observability primitive: every HTTP request
gets a correlation ID (read from `X-Request-ID` header or generated as UUID4),
attached to `request.state.request_id`, echoed in the response, and used to
tag every structured log event emitted by request-scoped code.

Also provides `log_event()` — a structured single-line JSON logger that:
- Always includes `event` and `request_id` fields (resolved from arg or
  ContextVar so callers don't have to thread it through every function).
- Drops keys whose value is `None`, so callers can pass conditionally
  without `if`/`else`.
- Truncates string field values past 200 chars by default — defense against
  accidentally logging full answers, document content, or histories.

Constraints (per `docs/architect_phase_2/02_OBSERVABILITY_SPEC.md`):
- NEVER log secrets, JWTs, raw answers, raw document content, or full
  conversation histories.
- `request_failed` events log a `safe_message` category, NOT `str(exc)`,
  because exception text may contain sensitive content.

This file is duplicated at `services/ingestion/observability.py` with
identical contents so that each service's Docker build context stays
self-contained. Field names and signatures must remain in sync between
copies — see also `docs/architect_phase_2/02_OBSERVABILITY_SPEC.md`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

# X-Request-ID validation: alphanumeric, dash, underscore; max 128 chars.
# Defends against log injection via header (newline/control characters).
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Default truncation cap for string log fields.
_MAX_FIELD_LEN = 200

# Logger name used by middleware. Kept stable so operators can target it in
# log-routing configuration.
_MIDDLEWARE_LOGGER_NAME = "observability.middleware"

# ContextVar so background tasks and helpers can resolve the current
# request_id without it being threaded through every function signature.
_current_request_id: ContextVar[Optional[str]] = ContextVar(
    "tocdoc_current_request_id", default=None
)


def _ensure_handler_attached(logger: logging.Logger) -> None:
    """Idempotently attach a stdout handler at INFO if the logger has none.

    The QnA app sets `propagate = False` on its module logger and does not
    install handlers on the root logger. Without this safeguard, our INFO-level
    `request_started` / `request_completed` events would propagate to root,
    find no handler there, and be silently dropped (root falls back to a
    WARNING-level stderr `lastResort` handler).

    We only attach when no handler exists on the named logger AND its
    propagate=False ancestor chain has no handler either, so we don't double-emit
    in environments that DO configure logging properly.
    """
    if logger.handlers:
        return  # already configured by caller / test harness
    # Walk up to root looking for any configured handler (respecting propagate)
    current = logger
    while current is not None:
        if current.handlers:
            return
        if not current.propagate:
            break
        current = current.parent

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    logger.addHandler(handler)
    # Honor LOG_LEVEL env if set; default INFO so request lifecycle events ship.
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))


def _validate_request_id(value: Optional[str]) -> Optional[str]:
    """Return value if it matches the allowed pattern, else None."""
    if value and _REQUEST_ID_PATTERN.match(value):
        return value
    return None


def _generate_request_id() -> str:
    """Generate a fresh UUID4 request ID."""
    return str(uuid.uuid4())


def get_current_request_id() -> Optional[str]:
    """Return the current request's ID if any (set by RequestIDMiddleware)."""
    return _current_request_id.get()


def _truncate(value: Any, max_len: int) -> Any:
    """Truncate a string to max_len chars (with `...` suffix); pass non-strings through."""
    if max_len > 0 and isinstance(value, str) and len(value) > max_len:
        return value[:max_len] + "..."
    return value


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    request_id: Optional[str] = None,
    level: int = logging.INFO,
    max_field_len: int = _MAX_FIELD_LEN,
    **fields: Any,
) -> None:
    """Emit a structured JSON log line for one event.

    Properties:
    - Always includes `event`. Always includes `request_id` if available
      (from arg or ContextVar).
    - Drops fields whose value is `None`.
    - Truncates string values to `max_field_len` chars (default 200; pass 0
      to disable). Truncation is the primary safeguard against accidentally
      logging long answers or document content.
    - Never raises — log emission is best-effort. If JSON serialization fails
      for any reason, falls back to a plain key=value line.

    Args:
        logger: Where to emit. Use the caller's module-level logger.
        event: Event name (e.g., "request_started").
        request_id: Optional override; otherwise resolved from ContextVar.
        level: Log level (default INFO).
        max_field_len: String truncation cap. 0 disables truncation.
        **fields: Additional event fields. None values are dropped.

    Returns:
        None.
    """
    rid = request_id if request_id is not None else _current_request_id.get()

    payload: dict[str, Any] = {"event": event}
    if rid is not None:
        payload["request_id"] = rid

    for key, value in fields.items():
        if value is None:
            continue
        payload[key] = _truncate(value, max_field_len)

    try:
        line = json.dumps(payload, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        # Last-ditch fallback so log_event NEVER raises.
        line = f"event={event} request_id={rid} (json_serialization_failed)"

    logger.log(level, line)


class RequestIDMiddleware(BaseHTTPMiddleware):
    """Ensure every request has an X-Request-ID; emit lifecycle events.

    Behavior:
    - Reads `X-Request-ID` from the request; validates against the allowed
      pattern. If missing or malformed, generates a UUID4.
    - Sets `request.state.request_id` for downstream handlers.
    - Sets the ContextVar so `log_event()` can resolve `request_id`
      without explicit passing.
    - Adds `X-Request-ID` to the response header.
    - Emits `request_started` on entry, then exactly one of
      `request_completed` (normal path) or `request_failed`
      (handler raised an unhandled exception).

    Registration order:
      This middleware MUST be added AFTER any auth middleware in code so
      that it becomes the OUTERMOST layer. FastAPI/Starlette execute
      last-added middleware FIRST on incoming requests. Outer position is
      required so `request_id` is available when auth runs (so auth-failure
      logs can include it).
    """

    def __init__(self, app, logger: Optional[logging.Logger] = None) -> None:
        super().__init__(app)
        self._logger = logger or logging.getLogger(_MIDDLEWARE_LOGGER_NAME)
        # Ensure events ship to stdout even when consumers haven't configured
        # the root logger. Idempotent — does nothing if handlers exist.
        _ensure_handler_attached(self._logger)

    async def dispatch(self, request: Request, call_next):
        incoming = request.headers.get("X-Request-ID")
        if incoming and not _validate_request_id(incoming):
            self._logger.warning(
                "Invalid X-Request-ID header rejected; generating a fresh UUID4"
            )
        request_id = _validate_request_id(incoming) or _generate_request_id()

        request.state.request_id = request_id
        token = _current_request_id.set(request_id)

        log_event(
            self._logger,
            "request_started",
            request_id=request_id,
            path=request.url.path,
            method=request.method,
        )

        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            latency_ms = round((time.perf_counter() - start) * 1000, 2)
            log_event(
                self._logger,
                "request_failed",
                request_id=request_id,
                level=logging.ERROR,
                error_class=type(exc).__name__,
                # safe_message is a generic category, NOT str(exc) — exception
                # text may contain sensitive details (paths, query content, etc).
                safe_message="Request handler raised an unhandled exception",
                latency_ms=latency_ms,
            )
            _current_request_id.reset(token)
            raise

        latency_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        log_event(
            self._logger,
            "request_completed",
            request_id=request_id,
            status_code=response.status_code,
            latency_ms=latency_ms,
        )
        _current_request_id.reset(token)
        return response
