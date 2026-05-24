"""Structured error contract for the QnA service (P0-6).

Provides:
- `ErrorEnvelope` / `ErrorBody` — Pydantic response models matching the shape
  documented in `docs/architect_phase_2/02_OBSERVABILITY_SPEC.md`:

      { "error": { "code": "INVALID_REQUEST",
                   "message": "Human-readable safe message",
                   "request_id": "uuid",
                   "errors": [ ... optional, for validation failures ... ] } }

- `ApiErrorCode` — small registry of stable codes returned to clients.
  Kept deliberately narrow; add codes as concrete callsites need them.

- `raise_api_error(code, message, status_code)` — preferred helper for new
  code paths. Raises `HTTPException(detail=dict)` which the handler below
  unpacks into the envelope. **New code should use this instead of raising
  `HTTPException(status_code, detail="string")` directly** so the `code`
  field stays meaningful.

- `register_exception_handlers(app)` — installs three handlers on the
  FastAPI app:
    1. `HTTPException` — handles both new dict-detail callsites and
       existing string-detail callsites (back-compat). Code defaults from
       status_code when not explicitly set.
    2. `RequestValidationError` — produces a 422 with `code=VALIDATION_ERROR`
       and a structured `errors` list derived from FastAPI's `.errors()`.
    3. `Exception` (catch-all) — produces a 500 with `code=INTERNAL_ERROR`.
       This closes the deferred PR #8 gap: unhandled-exception 5xx now
       carries `X-Request-ID` in both the body and the response header.

- `build_error_response(...)` — public helper for any code path that needs
  the envelope shape directly without going through `raise HTTPException`.
  **Use this from middleware** (e.g., the auth middleware in
  `services/qna/src/core/auth.py`, or the upload-size middleware in
  `services/ingestion/app.py`). Raising `HTTPException` from inside HTTP
  middleware does NOT route through FastAPI's `HTTPException` handler;
  the exception can fall through to Starlette's `ServerErrorMiddleware`
  and produce a non-enveloped 500. Returning a `JSONResponse` from
  `build_error_response` keeps the contract uniform.

- `default_error_responses` — OpenAPI responses dict for use via
  `**default_error_responses` on route decorators; documents 4xx/5xx
  envelope shape without per-route boilerplate.

## Cross-service consistency

The ingestion service ships a sibling file at `services/ingestion/errors.py`.
Field names, codes, helper names, and public behavior must remain in sync
between the two copies. They are NOT byte-identical (differences in
status-to-code map, docstring references), but every consumer of either
module should see the same observable behavior.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# Per-field truncation cap for validation error messages — mirrors the
# observability module's defensive default against accidentally logging
# large payloads via field locations.
_MAX_ERROR_FIELD_LEN = 200


class ApiErrorCode:
    """Stable error codes returned to clients in the `error.code` field.

    Six codes is enough; add more only when a concrete callsite needs a
    distinct value. Treat these as part of the public API contract — do
    not rename without an explicit contract-breaking PR.
    """

    INVALID_REQUEST = "INVALID_REQUEST"
    UNAUTHORIZED = "UNAUTHORIZED"
    NOT_FOUND = "NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


# Status code → default code mapping for back-compat with existing
# `HTTPException(status, detail="string")` callsites.
_STATUS_TO_CODE = {
    400: ApiErrorCode.INVALID_REQUEST,
    401: ApiErrorCode.UNAUTHORIZED,
    403: ApiErrorCode.UNAUTHORIZED,
    404: ApiErrorCode.NOT_FOUND,
    409: ApiErrorCode.INVALID_REQUEST,
    422: ApiErrorCode.VALIDATION_ERROR,
    503: ApiErrorCode.UPSTREAM_UNAVAILABLE,
}


class ErrorBody(BaseModel):
    """Inner body of an error response."""

    code: str = Field(..., description="Stable error code; see ApiErrorCode.")
    message: str = Field(..., description="Human-readable safe message.")
    request_id: Optional[str] = Field(
        None,
        description="Correlation ID; matches the X-Request-ID response header when available.",
    )
    errors: Optional[list[dict[str, Any]]] = Field(
        None,
        description="Structured per-field validation errors (only present for VALIDATION_ERROR).",
    )


class ErrorEnvelope(BaseModel):
    """Top-level error response envelope returned for every 4xx/5xx."""

    error: ErrorBody


def raise_api_error(
    code: str,
    message: str,
    status_code: int,
    headers: Optional[dict[str, str]] = None,
) -> None:
    """Raise an `HTTPException` whose detail is a dict carrying the code.

    Preferred over `raise HTTPException(status_code, detail="msg")` for any
    new code path that wants a specific `code` value in the response
    envelope. Existing string-detail callsites continue to work via the
    handler's back-compat path.

    Args:
        code: A value from `ApiErrorCode` (or a new stable string).
        message: Safe human-readable message — never includes secrets or
            raw exception text.
        status_code: HTTP status code to return.
        headers: Optional response headers (the handler always adds
            X-Request-ID; pass anything else here, e.g., WWW-Authenticate).
    """
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
        headers=headers,
    )


def _resolve_request_id(request: Request) -> str:
    """Pull the request_id set by RequestIDMiddleware; generate one if absent.

    Errors raised before RequestIDMiddleware runs (extremely rare — would
    require an exception inside Starlette's request parsing) would leave
    `request.state.request_id` unset. We generate a fresh UUID4 inline so
    operators always have something to grep, even if it won't appear in
    `request_started` log records.
    """
    rid = getattr(request.state, "request_id", None)
    if rid:
        return rid
    return str(uuid.uuid4())


def build_error_response(
    request: Request,
    *,
    code: str,
    message: str,
    status_code: int,
    extra_headers: Optional[dict[str, str]] = None,
    validation_errors: Optional[list[dict[str, Any]]] = None,
) -> JSONResponse:
    """Construct an error `JSONResponse` matching the envelope contract.

    Public helper for any code path that needs the envelope shape directly
    without going through `raise HTTPException`. The route-level exception
    handlers below use it; middleware should use it too — see the module
    docstring on why raising HTTPException from inside middleware is unsafe.

    Behavior:
    - Sets `X-Request-ID` from `request.state.request_id` if available;
      generates a fresh UUID4 otherwise so the header is never missing.
    - Includes the same `request_id` value in `body.error.request_id`.
    - Body is the `ErrorEnvelope` shape; `errors` field is omitted from
      the wire response unless `validation_errors` is provided.

    Args:
        request: The incoming Starlette/FastAPI request. Used to read
            `request.state.request_id`.
        code: A value from `ApiErrorCode` (or a stable new string).
        message: Safe human-readable message. Never include raw exception
            text, secrets, tokens, or user input.
        status_code: HTTP status code to return.
        extra_headers: Optional additional response headers (e.g.,
            WWW-Authenticate). `X-Request-ID` is always added by this
            helper and overrides any caller-supplied value.
        validation_errors: Per-field structured errors for VALIDATION_ERROR
            responses. Omitted from the body when None.
    """
    request_id = _resolve_request_id(request)

    body = ErrorBody(
        code=code,
        message=message,
        request_id=request_id,
        errors=validation_errors,
    )
    envelope = ErrorEnvelope(error=body)

    headers: dict[str, str] = {}
    if extra_headers:
        headers.update(extra_headers)
    # X-Request-ID is set last so it always wins over any caller value —
    # we want the body field and the header to match.
    headers["X-Request-ID"] = request_id

    return JSONResponse(
        status_code=status_code,
        # exclude_none keeps the wire payload tight — `errors` field
        # only appears on validation responses.
        content=envelope.model_dump(exclude_none=True),
        headers=headers,
    )


async def http_exception_handler(
    request: Request, exc: HTTPException
) -> JSONResponse:
    """Convert any `HTTPException` (string-detail or dict-detail) into the envelope.

    Back-compat: if `detail` is a dict containing `code` / `message`, those
    are used directly. Otherwise the existing string detail becomes the
    envelope `message` and `code` is derived from `status_code`.
    """
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        code = str(detail["code"])
        message = str(detail["message"])
    else:
        code = _STATUS_TO_CODE.get(exc.status_code, ApiErrorCode.INTERNAL_ERROR)
        # Fallback: string detail (existing callsites). If detail is a non-
        # string non-conforming dict, stringify it safely.
        message = detail if isinstance(detail, str) else str(detail or "Error")

    return build_error_response(
        request,
        status_code=exc.status_code,
        code=code,
        message=message,
        extra_headers=dict(exc.headers) if exc.headers else None,
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """422 handler — exposes structured per-field errors safely.

    Returns FastAPI's `.errors()` list (location, field, message, type)
    truncated to a sane per-message length. We do NOT echo back the
    `input` field from each error record — that could leak large user
    payloads into the response.
    """
    safe_errors: list[dict[str, Any]] = []
    for err in exc.errors():
        msg = err.get("msg", "")
        if isinstance(msg, str) and len(msg) > _MAX_ERROR_FIELD_LEN:
            msg = msg[:_MAX_ERROR_FIELD_LEN] + "..."
        safe_errors.append({
            "loc": list(err.get("loc", [])),
            "type": err.get("type", ""),
            "msg": msg,
        })

    return build_error_response(
        request,
        status_code=422,
        code=ApiErrorCode.VALIDATION_ERROR,
        message="Request validation failed",
        validation_errors=safe_errors,
    )


async def unhandled_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Catch-all: any unhandled exception becomes a 500 envelope.

    This is the path that closes the deferred PR #8 gap — unhandled
    exceptions now produce a structured 500 with `X-Request-ID` in both
    the body and the response header.

    The full exception (with stack trace) is logged server-side via
    `logger.exception` for ops debugging. Only the safe envelope reaches
    the client — `str(exc)` is never returned.
    """
    request_id = getattr(request.state, "request_id", None)
    logger.exception(
        "Unhandled exception in request handler (request_id=%s, error_class=%s)",
        request_id, type(exc).__name__,
    )
    return build_error_response(
        request,
        status_code=500,
        code=ApiErrorCode.INTERNAL_ERROR,
        message="Internal server error",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Install the three exception handlers on the FastAPI app.

    Call once at app startup. Order does not matter — FastAPI dispatches
    to the handler registered for the matching exception type.
    """
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


# OpenAPI responses surface — spread via `**default_error_responses` on each
# route decorator so clients see the actual envelope shape in the docs
# without 50 lines of duplication.
default_error_responses: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "Invalid request"},
    401: {"model": ErrorEnvelope, "description": "Unauthorized"},
    404: {"model": ErrorEnvelope, "description": "Not found"},
    422: {"model": ErrorEnvelope, "description": "Request validation failed"},
    500: {"model": ErrorEnvelope, "description": "Internal server error"},
    503: {"model": ErrorEnvelope, "description": "Upstream dependency unavailable"},
}
