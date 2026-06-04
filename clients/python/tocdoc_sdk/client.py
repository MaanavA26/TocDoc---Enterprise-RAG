"""Synchronous HTTP client for the TocDoc QnA API.

Wraps the ``POST /qna`` endpoint with typed request/response models and the
P0-6 error contract. Transport is ``httpx``; no service code is imported.

Retry policy: only *transient* failures are retried — 5xx responses and
connect/timeout errors. A 4xx is never retried (it will not succeed on a
repeat). Backoff is exponential with a configurable base; ``sleep`` is
injectable so tests stay fast and deterministic.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from .errors import ApiError
from .models import BotTurn, QnAAnswer, QnARequest

# Status codes treated as transient and therefore retryable.
_RETRYABLE_STATUS: frozenset[int] = frozenset({500, 502, 503, 504})
# httpx exceptions treated as transient connect/read failures.
_RETRYABLE_EXC: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
)

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_BASE = 0.5


class TocDocClient:
    """A typed, dependency-light client for the TocDoc QnA API.

    Example:
        >>> client = TocDocClient("https://api.example.com", token="secret")
        >>> answer = client.ask(
        ...     session_id="s-1",
        ...     bot_tag="acme",
        ...     fr_tag="read",
        ...     query="What is the refund policy?",
        ... )
        >>> answer.answer
        '...'
        >>> answer.citations
        {'policy.md': '/docs/policy.md'}

    The client is usable as a context manager and should be closed when done
    to release the underlying connection pool.

    Args:
        base_url: Base URL of the QnA service (any host/proxy prefix). The
            client POSTs to ``base_url`` + ``/qna``.
        token: Optional bearer token. Sent as ``Authorization: Bearer <token>``.
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
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")

        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep

        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    def __enter__(self) -> TocDocClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def ask(
        self,
        *,
        session_id: str,
        bot_tag: str,
        fr_tag: str,
        query: str | None = None,
        bot: Iterable[BotTurn | dict[str, Any]] | None = None,
    ) -> QnAAnswer:
        """Ask the QnA service a question and return the typed answer.

        Provide either a single ``query`` (wrapped into a one-turn
        conversation) or a full ``bot`` history. The last turn's ``user_query``
        is the question the service answers.

        Args:
            session_id: Correlation/session identifier.
            bot_tag: Bot identifier/tag (tenant isolation).
            fr_tag: Feature/retrieval tag.
            query: Convenience single-question input. Mutually exclusive with
                ``bot``.
            bot: Full conversation history (turns, oldest -> newest). Each item
                may be a :class:`~tocdoc_sdk.models.BotTurn` or a plain dict.

        Returns:
            A typed :class:`~tocdoc_sdk.models.QnAAnswer`.

        Raises:
            ValueError: If neither or both of ``query`` / ``bot`` are given.
            ApiError: On any non-2xx response (carries the envelope fields).
        """
        if (query is None) == (bot is None):
            raise ValueError("provide exactly one of `query` or `bot`")

        if bot is None:
            turns = [BotTurn(user_query=query)]  # type: ignore[arg-type]
        else:
            turns = [t if isinstance(t, BotTurn) else BotTurn(**t) for t in bot]

        request = QnARequest(
            session_id=session_id,
            bot=turns,
            fr_tag=fr_tag,
            bot_tag=bot_tag,
        )

        response = self._post_with_retries("/qna", request.model_dump())

        if 200 <= response.status_code < 300:
            return QnAAnswer.model_validate(response.json())

        # Non-2xx: parse the P0-6 envelope (defensively) and raise.
        raise ApiError.from_response(response.status_code, self._safe_json(response))

    def _post_with_retries(self, path: str, json_body: dict[str, Any]) -> httpx.Response:
        """POST ``json_body`` to ``path``, retrying transient failures only.

        Retries on 5xx responses and connect/timeout errors, up to
        ``max_retries`` times with exponential backoff. A 4xx response is
        returned immediately (never retried). The last transient exception is
        re-raised if all attempts are exhausted.
        """
        last_exc: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.post(path, json=json_body)
            except _RETRYABLE_EXC as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    self._sleep(self._backoff_base * (2**attempt))
                    continue
                raise
            else:
                if response.status_code in _RETRYABLE_STATUS and attempt < self._max_retries:
                    self._sleep(self._backoff_base * (2**attempt))
                    continue
                return response

        # Unreachable: the loop either returns a response or raises. Present
        # only to satisfy static analysis of the function's return type.
        raise last_exc  # pragma: no cover

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        """Parse a response body as JSON, returning ``None`` if it isn't JSON."""
        try:
            return response.json()
        except ValueError:
            return None
