"""Tests for the async QnA client and the read-only admin client.

All HTTP is mocked with ``httpx.MockTransport`` — there is no live network.
Async tests are driven with ``asyncio.run`` so no pytest-asyncio plugin or
extra dependency is needed. Injected no-op sleeps keep retry tests instant.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from tocdoc_sdk import (
    AdminClient,
    ApiError,
    AsyncTocDocClient,
    DocumentDetailResponse,
    DocumentListResponse,
    IndexStatsResponse,
    QnAAnswer,
)

QNA_URL = "https://qna.example.test"
ADMIN_URL = "https://ingestion.example.test"


# ---------------------------------------------------------------------------
# Async QnA client
# ---------------------------------------------------------------------------


def _make_async_client(handler, *, token=None, max_retries=2):
    """Build an async client wired to a MockTransport handler with no-op sleep."""
    return AsyncTocDocClient(
        QNA_URL,
        token=token,
        max_retries=max_retries,
        backoff_base=0.0,
        transport=httpx.MockTransport(handler),
        sleep=_noop_async_sleep,
    )


async def _noop_async_sleep(_seconds: float) -> None:
    """Async no-op sleep so retry backoff is instant in tests."""
    return None


def test_async_ask_happy_path_returns_typed_answer():
    """A 2xx envelope deserializes into a typed QnAAnswer over the async client."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/qna"
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={
                "answer": "Refunds are processed within 30 days.",
                "citation": {"policy.md": "/docs/policy.md"},
            },
        )

    async def _body() -> QnAAnswer:
        async with _make_async_client(handler) as client:
            return await client.ask(
                session_id="s-1",
                bot_tag="acme",
                fr_tag="read",
                query="What is the refund policy?",
            )

    result = asyncio.run(_body())
    assert isinstance(result, QnAAnswer)
    assert result.answer == "Refunds are processed within 30 days."
    assert result.citations == {"policy.md": "/docs/policy.md"}


def test_async_bearer_token_sent_in_authorization_header():
    """The bearer token is sent in the Authorization header by the async client."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"answer": "ok", "citation": {}})

    async def _body() -> None:
        async with _make_async_client(handler, token="s3cr3t-token") as client:
            await client.ask(session_id="s", bot_tag="b", fr_tag="f", query="q")

    asyncio.run(_body())
    assert seen["auth"] == "Bearer s3cr3t-token"


def test_async_requires_exactly_one_of_query_or_bot():
    """Passing neither or both of query/bot is a client-side ValueError (async)."""

    async def _body() -> None:
        async with _make_async_client(lambda r: httpx.Response(200)) as client:
            with pytest.raises(ValueError):
                await client.ask(session_id="s", bot_tag="b", fr_tag="f")
            with pytest.raises(ValueError):
                await client.ask(
                    session_id="s",
                    bot_tag="b",
                    fr_tag="f",
                    query="q",
                    bot=[{"user_query": "x"}],
                )

    asyncio.run(_body())


def test_async_transient_503_then_success():
    """A 503 followed by a 200 succeeds after retrying (async, awaited backoff)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                503,
                json={"error": {"code": "UPSTREAM_UNAVAILABLE", "message": "down", "request_id": "r"}},
            )
        return httpx.Response(200, json={"answer": "recovered", "citation": {}})

    async def _body() -> QnAAnswer:
        async with _make_async_client(handler, max_retries=2) as client:
            return await client.ask(session_id="s", bot_tag="b", fr_tag="f", query="q")

    result = asyncio.run(_body())
    assert calls["n"] == 2
    assert result.answer == "recovered"


def test_async_error_envelope_raises_api_error():
    """A 401 envelope raises ApiError carrying the envelope fields (async)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"code": "UNAUTHORIZED", "message": "nope", "request_id": "req-1"}},
        )

    async def _body() -> None:
        async with _make_async_client(handler, max_retries=0) as client:
            await client.ask(session_id="s", bot_tag="b", fr_tag="f", query="q")

    with pytest.raises(ApiError) as excinfo:
        asyncio.run(_body())
    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "UNAUTHORIZED"
    assert excinfo.value.request_id == "req-1"


# ---------------------------------------------------------------------------
# Admin client
# ---------------------------------------------------------------------------


def _make_admin_client(handler, *, admin_token="admin-secret", max_retries=2):
    """Build an admin client wired to a MockTransport handler with no-op sleep."""
    return AdminClient(
        ADMIN_URL,
        admin_token=admin_token,
        max_retries=max_retries,
        backoff_base=0.0,
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
    )


def test_admin_index_stats_happy_path():
    """index_stats sends X-Admin-Token + bot_tag and returns a typed response."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["bot_tag"] = request.url.params.get("bot_tag", "")
        seen["admin_token"] = request.headers.get("x-admin-token", "")
        seen["method"] = request.method
        return httpx.Response(
            200,
            json={
                "bot_tag": "acme",
                "document_count": 3,
                "chunk_count": 42,
                "source_types": {"blob": 2, "sharepoint": 1},
                "fr_modes": {"read": 3},
            },
        )

    with _make_admin_client(handler) as admin:
        stats = admin.index_stats(bot_tag="acme")

    assert seen["path"] == "/admin/index/stats"
    assert seen["method"] == "GET"
    assert seen["bot_tag"] == "acme"
    assert seen["admin_token"] == "admin-secret"
    assert isinstance(stats, IndexStatsResponse)
    assert stats.document_count == 3
    assert stats.chunk_count == 42
    assert stats.source_types == {"blob": 2, "sharepoint": 1}
    assert stats.fr_modes == {"read": 3}


def test_admin_list_documents_happy_path():
    """list_documents returns a typed list of document summaries."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/admin/documents"
        return httpx.Response(
            200,
            json={
                "bot_tag": "acme",
                "count": 1,
                "documents": [
                    {
                        "document_id": "doc-1",
                        "source_path": "/docs/a.md",
                        "source_type": "blob",
                        "fr_tag": "read",
                        "chunk_count": 5,
                        "first_ingested_at": "2026-01-01T00:00:00Z",
                        "last_ingested_at": "2026-02-01T00:00:00Z",
                    }
                ],
            },
        )

    with _make_admin_client(handler) as admin:
        result = admin.list_documents(bot_tag="acme")

    assert isinstance(result, DocumentListResponse)
    assert result.count == 1
    assert result.documents[0].document_id == "doc-1"
    assert result.documents[0].chunk_count == 5


def test_admin_get_document_happy_path():
    """get_document hits the per-id path and returns a typed detail response."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/admin/documents/doc-1"
        assert request.url.params.get("bot_tag") == "acme"
        return httpx.Response(
            200,
            json={
                "bot_tag": "acme",
                "document_id": "doc-1",
                "source_path": "/docs/a.md",
                "source_type": "blob",
                "fr_tag": "read",
                "chunk_count": 2,
                "ingestion_timestamps": ["2026-01-01T00:00:00Z"],
                "sample_chunks": [{"id": "doc-1-0", "chunk_index": 0}],
            },
        )

    with _make_admin_client(handler) as admin:
        detail = admin.get_document(bot_tag="acme", document_id="doc-1")

    assert isinstance(detail, DocumentDetailResponse)
    assert detail.document_id == "doc-1"
    assert detail.chunk_count == 2
    assert detail.sample_chunks[0].id == "doc-1-0"


def test_admin_404_envelope_raises_api_error():
    """A 404 envelope (document not in scope) raises ApiError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={"error": {"code": "NOT_FOUND", "message": "missing", "request_id": "req-9"}},
        )

    with _make_admin_client(handler, max_retries=0) as admin, pytest.raises(ApiError) as excinfo:
        admin.get_document(bot_tag="acme", document_id="nope")

    assert excinfo.value.status_code == 404
    assert excinfo.value.code == "NOT_FOUND"


def test_admin_non_envelope_503_degraded():
    """A FastAPI {"detail": ...} 503 (non-envelope) degrades to HTTP_503."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "Search index temporarily unavailable"})

    with _make_admin_client(handler, max_retries=0) as admin, pytest.raises(ApiError) as excinfo:
        admin.index_stats(bot_tag="acme")

    assert excinfo.value.status_code == 503
    assert excinfo.value.code == "HTTP_503"


def test_admin_requires_admin_token():
    """Constructing an AdminClient without a token is a ValueError."""
    with pytest.raises(ValueError):
        AdminClient(ADMIN_URL, admin_token="")
