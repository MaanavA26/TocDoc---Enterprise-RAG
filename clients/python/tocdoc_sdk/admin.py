"""Synchronous client for the TocDoc read-only admin API.

The admin endpoints live on the **ingestion** service and authenticate with a
static ``X-Admin-Token`` header (NOT the QnA bearer token), so this is a
separate client with its own ``base_url`` and ``admin_token`` rather than a
method bolted onto :class:`TocDocClient`. If a deployment fronts both services
behind one proxy, pass the same URL to both clients; if they are separate
hosts, pass different ones — either way is correct.

All three wrapped endpoints are read-only GETs scoped by ``bot_tag`` (tenant
isolation is enforced server-side):

- ``GET /admin/documents?bot_tag=...``            -> :class:`DocumentListResponse`
- ``GET /admin/documents/{id}?bot_tag=...``       -> :class:`DocumentDetailResponse`
- ``GET /admin/index/stats?bot_tag=...``          -> :class:`IndexStatsResponse`

Transport, retry policy, and :class:`ApiError` semantics are shared with the
QnA clients. The admin token is sent as a header and is NEVER logged.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import httpx

from ._retry import (
    DEFAULT_BACKOFF_BASE,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT,
    RETRYABLE_EXC,
    RETRYABLE_STATUS,
    safe_json,
)
from .errors import ApiError
from .models import DocumentDetailResponse, DocumentListResponse, IndexStatsResponse


class AdminClient:
    """A typed, dependency-light client for the TocDoc read-only admin API.

    Example:
        >>> with AdminClient("https://ingestion.example.com", admin_token="secret") as admin:
        ...     stats = admin.index_stats(bot_tag="acme")
        ...     print(stats.document_count, stats.chunk_count)

    Usable as a context manager; close it when done to release the connection
    pool.

    Args:
        base_url: Base URL of the ingestion service (any host/proxy prefix).
            Requests go to ``base_url`` + ``/admin/...``.
        admin_token: Static admin token. Sent as the ``X-Admin-Token`` header.
            It is NEVER logged.
        timeout: Per-request timeout in seconds.
        max_retries: Maximum number of *retries* (beyond the first attempt) on
            transient failures. Total attempts = ``max_retries + 1``.
        backoff_base: Base seconds for exponential backoff between retries.
        transport: Optional ``httpx`` transport (used by tests to mock HTTP).
        sleep: Injectable sleep function (defaults to ``time.sleep``); override
            in tests to avoid real delays.
    """

    def __init__(
        self,
        base_url: str,
        *,
        admin_token: str,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if not admin_token:
            raise ValueError("admin_token is required")

        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep

        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Accept": "application/json", "X-Admin-Token": admin_token},
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> AdminClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def list_documents(self, *, bot_tag: str) -> DocumentListResponse:
        """List indexed documents in a ``bot_tag`` scope.

        Wraps ``GET /admin/documents``. One row per document, aggregated from
        chunk-level metadata.

        Raises:
            ApiError: On any non-2xx response (carries the envelope fields).
        """
        body = self._get("/admin/documents", params={"bot_tag": bot_tag})
        return DocumentListResponse.model_validate(body)

    def get_document(self, *, bot_tag: str, document_id: str) -> DocumentDetailResponse:
        """Get one document's summary in a ``bot_tag`` scope.

        Wraps ``GET /admin/documents/{document_id}``.

        Raises:
            ApiError: On any non-2xx response (e.g. 404 when the document is not
                in this scope), carrying the envelope fields.
        """
        body = self._get(f"/admin/documents/{document_id}", params={"bot_tag": bot_tag})
        return DocumentDetailResponse.model_validate(body)

    def index_stats(self, *, bot_tag: str) -> IndexStatsResponse:
        """Get aggregate index stats for one ``bot_tag`` scope.

        Wraps ``GET /admin/index/stats``: document/chunk counts plus
        per-source-type and per-mode breakdowns.

        Raises:
            ApiError: On any non-2xx response (carries the envelope fields).
        """
        body = self._get("/admin/index/stats", params={"bot_tag": bot_tag})
        return IndexStatsResponse.model_validate(body)

    def _get(self, path: str, *, params: dict[str, Any]) -> Any:
        """GET ``path`` with the shared retry policy; return the parsed body or raise.

        Returns the parsed JSON on 2xx. On any non-2xx, raises
        :class:`ApiError` (degrading non-envelope bodies, e.g. a FastAPI
        ``{"detail": ...}`` 503/404, to a synthesized ``HTTP_<status>``).
        """
        response = self._request_with_retries("GET", path, params=params)
        if 200 <= response.status_code < 300:
            return response.json()
        raise ApiError.from_response(response.status_code, safe_json(response))

    def _request_with_retries(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send ``method`` to ``path``, retrying transient failures only.

        Same policy as the QnA clients: 5xx + connect/timeout retried with
        exponential backoff, 4xx never retried.
        """
        last_exc: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.request(method, path, **kwargs)
            except RETRYABLE_EXC as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    self._sleep(self._backoff_base * (2**attempt))
                    continue
                raise
            else:
                if response.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                    self._sleep(self._backoff_base * (2**attempt))
                    continue
                return response

        # Unreachable: the loop either returns a response or raises.
        raise last_exc  # pragma: no cover
