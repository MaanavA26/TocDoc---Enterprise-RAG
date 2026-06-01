"""Structured error contract for the ingestion service (P0-6).

Sibling of `services/qna/src/core/errors.py` — kept per-service so each
service's Docker build context stays self-contained (same pattern as the
observability modules). Field names, codes, helper names, and public
behavior MUST remain in sync between the two copies; they are not
byte-identical (the status-to-code map differs because ingestion has 413
upload-size responses that QnA doesn't). See
`docs/productization_backlog/06_API_Harden_error_contracts_request_validation_and_response_schema.md`
and `docs/architect_phase_2/02_OBSERVABILITY_SPEC.md`.

Provides:
- `ErrorEnvelope` / `ErrorBody` — Pydantic response models.
- `ApiErrorCode` — stable code registry.
- `raise_api_error(code, message, status_code)` — preferred helper for new code.
- `build_error_response(request, ...)` — public helper for middleware paths
  that need to return the envelope directly without raising HTTPException.
  **Use this from any HTTP middleware** (e.g., `limit_upload_size` below).
  Raising HTTPException from inside middleware does NOT route through
  FastAPI's HTTPException handler; the exception can fall through to
  Starlette's `ServerErrorMiddleware` and produce a non-enveloped 500.
- `register_exception_handlers(app)` — installs the three handlers on the FastAPI app.
- `default_error_responses` — OpenAPI responses dict for use via spread.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_MAX_ERROR_FIELD_LEN = 200


class ApiErrorCode:
    """Stable error codes returned to clients in the `error.code` field."""

    INVALID_REQUEST = "INVALID_REQUEST"
    UNAUTHORIZED = "UNAUTHORIZED"
    NOT_FOUND = "NOT_FOUND"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_STATUS_TO_CODE = {
    400: ApiErrorCode.INVALID_REQUEST,
    401: ApiErrorCode.UNAUTHORIZED,
    403: ApiErrorCode.UNAUTHORIZED,
    404: ApiErrorCode.NOT_FOUND,
    409: ApiErrorCode.INVALID_REQUEST,
    413: ApiErrorCode.INVALID_REQUEST,
    422: ApiErrorCode.VALIDATION_ERROR,
    503: ApiErrorCode.UPSTREAM_UNAVAILABLE,
}


class ErrorBody(BaseModel):
    """Inner body of an error response."""

    code: str = Field(..., description="Stable error code; see ApiErrorCode.")
    message: str = Field(..., description="Human-readable safe message.")
    request_id: str | None = Field(
        None,
        description="Correlation ID; matches the X-Request-ID response header when available.",
    )
    errors: list[dict[str, Any]] | None = Field(
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
    headers: dict[str, str] | None = None,
) -> None:
    """Raise an `HTTPException` whose detail dict carries the error code.

    Preferred over `raise HTTPException(status_code, detail="msg")` for new
    code paths. Existing string-detail callsites continue to work via the
    handler's back-compat path.
    """
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
        headers=headers,
    )


def _resolve_request_id(request: Request) -> str:
    """Pull request.state.request_id; generate a fresh UUID4 if absent."""
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
    extra_headers: dict[str, str] | None = None,
    validation_errors: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    """Construct an error `JSONResponse` matching the envelope contract.

    Public helper for any code path that needs the envelope shape directly
    without going through `raise HTTPException`. **Use this from HTTP
    middleware** — raising HTTPException from inside middleware does NOT
    route through FastAPI's HTTPException handler.

    Behavior:
    - Sets `X-Request-ID` from `request.state.request_id` if available;
      generates a fresh UUID4 otherwise so the header is never missing.
    - Includes the same `request_id` value in `body.error.request_id`.
    - Body is the `ErrorEnvelope` shape; `errors` field omitted unless
      `validation_errors` is provided.

    Args:
        request: The incoming Starlette/FastAPI request.
        code: A value from `ApiErrorCode` (or a stable new string).
        message: Safe human-readable message. Never include raw exception
            text, secrets, tokens, or user input.
        status_code: HTTP status code.
        extra_headers: Optional additional response headers. `X-Request-ID`
            is added by this helper and overrides any caller-supplied value.
        validation_errors: Per-field structured errors for VALIDATION_ERROR.
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
    headers["X-Request-ID"] = request_id

    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(exclude_none=True),
        headers=headers,
    )


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Convert any HTTPException into the envelope (string- or dict-detail)."""
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail and "message" in detail:
        code = str(detail["code"])
        message = str(detail["message"])
    else:
        code = _STATUS_TO_CODE.get(exc.status_code, ApiErrorCode.INTERNAL_ERROR)
        message = detail if isinstance(detail, str) else str(detail or "Error")

    return build_error_response(
        request,
        status_code=exc.status_code,
        code=code,
        message=message,
        extra_headers=dict(exc.headers) if exc.headers else None,
    )


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """422 handler — exposes safe structured per-field errors."""
    safe_errors: list[dict[str, Any]] = []
    for err in exc.errors():
        msg = err.get("msg", "")
        if isinstance(msg, str) and len(msg) > _MAX_ERROR_FIELD_LEN:
            msg = msg[:_MAX_ERROR_FIELD_LEN] + "..."
        safe_errors.append(
            {
                "loc": list(err.get("loc", [])),
                "type": err.get("type", ""),
                "msg": msg,
            }
        )

    return build_error_response(
        request,
        status_code=422,
        code=ApiErrorCode.VALIDATION_ERROR,
        message="Request validation failed",
        validation_errors=safe_errors,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all — unhandled exceptions become 500 envelopes with X-Request-ID."""
    request_id = getattr(request.state, "request_id", None)
    logger.exception(
        "Unhandled exception in request handler (request_id=%s, error_class=%s)",
        request_id,
        type(exc).__name__,
    )
    return build_error_response(
        request,
        status_code=500,
        code=ApiErrorCode.INTERNAL_ERROR,
        message="Internal server error",
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Install the three exception handlers on the FastAPI app."""
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


default_error_responses: dict[int | str, dict[str, Any]] = {
    400: {"model": ErrorEnvelope, "description": "Invalid request"},
    401: {"model": ErrorEnvelope, "description": "Unauthorized"},
    404: {"model": ErrorEnvelope, "description": "Not found"},
    413: {"model": ErrorEnvelope, "description": "Payload too large"},
    422: {"model": ErrorEnvelope, "description": "Request validation failed"},
    500: {"model": ErrorEnvelope, "description": "Internal server error"},
    503: {"model": ErrorEnvelope, "description": "Upstream dependency unavailable"},
}
