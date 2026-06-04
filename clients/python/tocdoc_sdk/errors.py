"""Client-side error type mirroring the TocDoc structured error envelope.

The server returns every 4xx/5xx as the P0-6 envelope
(``services/qna/src/core/errors.py``)::

    {"error": {"code": "...", "message": "...", "request_id": "...", "errors": [...]}}

:class:`ApiError` parses that envelope. It is defensive: a non-envelope body
(an HTML 502 from a proxy, or JSON missing the ``error`` key) is degraded into
a synthesized ``ApiError`` rather than raising a ``KeyError`` while handling an
error.
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """Raised for any non-2xx response from the QnA API.

    Attributes:
        status_code: HTTP status code of the response.
        code: Stable error code from ``error.code`` (e.g. ``"UNAUTHORIZED"``),
            or a synthesized ``"HTTP_<status>"`` when the body is not a valid
            envelope.
        message: Human-readable safe message from ``error.message``.
        request_id: Correlation ID from ``error.request_id`` (matches the
            ``X-Request-ID`` header), or ``None`` if absent.
        errors: Structured per-field validation errors (only present for
            ``VALIDATION_ERROR`` responses), or ``None``.
    """

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        request_id: str | None = None,
        errors: list[dict[str, Any]] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        self.request_id = request_id
        self.errors = errors
        super().__init__(f"[{status_code}] {code}: {message}")

    @classmethod
    def from_response(cls, status_code: int, body: Any) -> ApiError:
        """Build an :class:`ApiError` from a parsed response body.

        Args:
            status_code: HTTP status code of the response.
            body: The parsed JSON body (any type). May be a well-formed
                envelope dict, some other JSON, or ``None`` if the body was
                not JSON-decodable.

        Returns:
            An :class:`ApiError`. When ``body`` is a conforming envelope, the
            fields are taken from ``error``. Otherwise a fallback is
            synthesized so callers always get usable ``code``/``message``.
        """
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and "code" in error and "message" in error:
            return cls(
                status_code=status_code,
                code=str(error["code"]),
                message=str(error["message"]),
                request_id=error.get("request_id"),
                errors=error.get("errors"),
            )

        # Non-envelope body (e.g. proxy HTML, gateway JSON without `error`).
        # Synthesize a stable code/message so error handling never crashes.
        return cls(
            status_code=status_code,
            code=f"HTTP_{status_code}",
            message=f"Unexpected non-envelope error response (HTTP {status_code})",
            request_id=None,
            errors=None,
        )
