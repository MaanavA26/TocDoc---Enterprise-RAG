"""Asynchronous HTTP client for the TocDoc QnA API.

An ``asyncio`` mirror of :class:`tocdoc_sdk.TocDocClient`: same request/response
models, the same transient-retry policy, and the same :class:`ApiError`
semantics — over ``httpx.AsyncClient`` instead of ``httpx.Client``. No service
code is imported.

The only behavioral difference from the sync client is that ``ask`` is a
coroutine and the backoff sleep is awaited (defaults to ``asyncio.sleep`` so it
yields the event loop instead of blocking it).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
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
from .models import BotTurn, QnAAnswer, QnARequest


class AsyncTocDocClient:
    """An async, typed, dependency-light client for the TocDoc QnA API.

    Example:
        >>> async with AsyncTocDocClient("https://api.example.com", token="s") as client:
        ...     answer = await client.ask(
        ...         session_id="s-1",
        ...         bot_tag="acme",
        ...         fr_tag="read",
        ...         query="What is the refund policy?",
        ...     )
        ...     print(answer.answer)

    Use as an async context manager (``async with``) or call :meth:`aclose`
    when done to release the underlying connection pool.

    Args:
        base_url: Base URL of the QnA service. The client POSTs to
            ``base_url`` + ``/qna``.
        token: Optional bearer token. Sent as ``Authorization: Bearer <token>``.
            It is NEVER logged.
        timeout: Per-request timeout in seconds.
        max_retries: Maximum number of *retries* (beyond the first attempt) on
            transient failures. Total attempts = ``max_retries + 1``.
        backoff_base: Base seconds for exponential backoff between retries.
        transport: Optional ``httpx`` async transport (used by tests to mock
            HTTP, e.g. ``httpx.MockTransport``).
        sleep: Injectable *async* sleep coroutine (defaults to ``asyncio.sleep``);
            override in tests to avoid real delays.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")

        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._sleep = sleep

        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    async def __aenter__(self) -> AsyncTocDocClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()

    async def ask(
        self,
        *,
        session_id: str,
        bot_tag: str,
        fr_tag: str,
        query: str | None = None,
        bot: Iterable[BotTurn | dict[str, Any]] | None = None,
    ) -> QnAAnswer:
        """Ask the QnA service a question and return the typed answer.

        Async mirror of :meth:`tocdoc_sdk.TocDocClient.ask`; see that method for
        full argument semantics. Provide exactly one of ``query`` or ``bot``.

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

        response = await self._request_with_retries("POST", "/qna", json=request.model_dump())

        if 200 <= response.status_code < 300:
            return QnAAnswer.model_validate(response.json())

        # Non-2xx: parse the P0-6 envelope (defensively) and raise.
        raise ApiError.from_response(response.status_code, safe_json(response))

    async def _request_with_retries(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send ``method`` to ``path`` asynchronously, retrying transient failures.

        Same policy as the sync client (5xx + connect/timeout retried, 4xx
        never), but the backoff sleep is awaited so it yields the event loop.
        """
        last_exc: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(method, path, **kwargs)
            except RETRYABLE_EXC as exc:
                last_exc = exc
                if attempt < self._max_retries:
                    await self._sleep(self._backoff_base * (2**attempt))
                    continue
                raise
            else:
                if response.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                    await self._sleep(self._backoff_base * (2**attempt))
                    continue
                return response

        # Unreachable: the loop either returns a response or raises. Present
        # only to satisfy static analysis of the function's return type.
        raise last_exc  # pragma: no cover
