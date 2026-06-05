"""Tests for the SSE streaming helper (sync + async) and the SSE parser.

All HTTP is mocked with ``httpx.MockTransport`` returning a canned SSE byte
stream — there is no live server. These tests run in BOTH the core ``[dev]``
install and the ``[dev,langchain]`` install (no langchain import here).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from tocdoc_sdk import ApiError, AsyncTocDocClient, TocDocClient
from tocdoc_sdk._sse import iter_sse_data, iter_sse_events

BASE_URL = "https://qna.example.test"

# A canned SSE stream exercising the framing rules: a comment/heartbeat line, a
# bare-`data` token, a multi-`data` event (joined with "\n"), and the [DONE]
# terminator. All token events (no `event:` field => default "message").
SSE_BYTES = (
    b": heartbeat\n"
    b"data: Hello\n"
    b"\n"
    b"data: multi-1\n"
    b"data: multi-2\n"
    b"\n"
    b"data: world\n"
    b"\n"
    b"data: [DONE]\n"
    b"\n"
    b"data: after-done-should-not-appear\n"
    b"\n"
)

# The real server wire format (services/qna/app.py /qna/stream): bare `data:`
# token events, ONE `event: citation` event carrying the citation JSON, then the
# `[DONE]` sentinel. Mirrors a successful streamed answer.
CITATION_PAYLOAD = {"citation": {"policy.md": "/docs/policy.md"}, "page_citations": {"policy.md": [2]}}
SSE_WITH_CITATION = (
    b"data: The refund\n"
    b"\n"
    b"data:  policy is X.\n"
    b"\n"
    b"event: citation\n"
    b"data: " + json.dumps(CITATION_PAYLOAD).encode() + b"\n"
    b"\n"
    b"data: [DONE]\n"
    b"\n"
)

# A mid-stream failure: a token, then the terminal `event: error` envelope, then
# `[DONE]` (the server emits error-then-DONE in its `finally`).
ERROR_ENVELOPE = {"error": {"code": "INTERNAL_ERROR", "message": "Streaming failed", "request_id": "r-99"}}
SSE_WITH_ERROR = (
    b"data: partial answer\n"
    b"\n"
    b"event: error\n"
    b"data: " + json.dumps(ERROR_ENVELOPE).encode() + b"\n"
    b"\n"
    b"data: [DONE]\n"
    b"\n"
)


# ---------------------------------------------------------------------------
# Pure parser unit tests
# ---------------------------------------------------------------------------


def test_parser_yields_token_data_joins_multiline_and_skips_comments():
    # iter_sse_data is the backward-compatible token view: only default events.
    lines = SSE_BYTES.decode().split("\n")
    assert list(iter_sse_data(lines)) == ["Hello", "multi-1\nmulti-2", "world"]


def test_parser_events_tracks_event_type_and_defaults_to_message():
    lines = SSE_WITH_CITATION.decode().split("\n")
    events = list(iter_sse_events(lines))
    # "data:  policy..." -> one framing space stripped, leaving " policy..." so
    # joining tokens reconstructs the answer with its word boundary.
    assert events == [
        ("message", "The refund"),
        ("message", " policy is X."),
        ("citation", json.dumps(CITATION_PAYLOAD)),
    ]


def test_parser_event_type_resets_between_blocks():
    # A tagged event must not leak its type onto a following bare-data event.
    lines = ["event: citation", "data: c", "", "data: t", ""]
    assert list(iter_sse_events(lines)) == [("citation", "c"), ("message", "t")]


def test_parser_data_view_skips_tagged_events():
    lines = SSE_WITH_CITATION.decode().split("\n")
    # The citation event must NOT appear in the token view (no JSON in answer).
    assert list(iter_sse_data(lines)) == ["The refund", " policy is X."]


def test_parser_strips_only_single_leading_space():
    # "data:  x" -> one framing space stripped, leaving " x".
    assert list(iter_sse_data(["data:  x", ""])) == [" x"]
    # "data:y" (no space) -> "y".
    assert list(iter_sse_data(["data:y", ""])) == ["y"]


def test_parser_dispatches_trailing_event_without_blank_line():
    assert list(iter_sse_data(["data: tail"])) == ["tail"]


def test_parser_done_sentinel_terminates_and_is_not_yielded():
    assert list(iter_sse_data(["data: a", "", "data: [DONE]", "", "data: b", ""])) == ["a"]


def test_parser_ignores_non_data_fields():
    assert list(iter_sse_data(["event: ping", "id: 1", "retry: 500", ""])) == []


# ---------------------------------------------------------------------------
# Sync client streaming
# ---------------------------------------------------------------------------


def _sync_client(handler, **kwargs):
    return TocDocClient(
        BASE_URL,
        max_retries=0,
        backoff_base=0.0,
        transport=httpx.MockTransport(handler),
        sleep=lambda _s: None,
        **kwargs,
    )


def test_stream_ask_yields_tokens_and_posts_to_stream_path():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["accept"] = request.headers.get("accept")
        return httpx.Response(200, content=SSE_BYTES)

    with _sync_client(handler) as client:
        tokens = list(client.stream_ask(session_id="s-1", bot_tag="acme", fr_tag="read", query="hi"))

    assert tokens == ["Hello", "multi-1\nmulti-2", "world"]
    assert seen["path"] == "/qna/stream"
    assert seen["accept"] == "text/event-stream"


def test_stream_ask_requires_exactly_one_of_query_or_bot():
    with _sync_client(lambda r: httpx.Response(200, content=b"")) as client:
        with pytest.raises(ValueError):
            list(client.stream_ask(session_id="s", bot_tag="b", fr_tag="f"))
        with pytest.raises(ValueError):
            list(
                client.stream_ask(
                    session_id="s", bot_tag="b", fr_tag="f", query="q", bot=[{"user_query": "x"}]
                )
            )


def test_stream_ask_non_2xx_raises_api_error_before_yielding():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"error": {"code": "UNAUTHORIZED", "message": "nope", "request_id": "r-1"}},
        )

    with _sync_client(handler) as client, pytest.raises(ApiError) as excinfo:
        list(client.stream_ask(session_id="s", bot_tag="b", fr_tag="f", query="q"))

    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "UNAUTHORIZED"


def test_stream_ask_answer_is_only_tokens_and_citation_surfaced_separately():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SSE_WITH_CITATION)

    citations: list[dict] = []
    with _sync_client(handler) as client:
        answer = "".join(
            client.stream_ask(
                session_id="s-1",
                bot_tag="acme",
                fr_tag="read",
                query="hi",
                on_citation=citations.append,
            )
        )

    # The answer is ONLY token text — no citation JSON concatenated onto it.
    assert answer == "The refund policy is X."
    assert "{" not in answer
    # The citation payload is surfaced out-of-band, exactly once.
    assert citations == [CITATION_PAYLOAD]


def test_stream_ask_without_callback_drops_citation_from_answer():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SSE_WITH_CITATION)

    with _sync_client(handler) as client:
        tokens = list(client.stream_ask(session_id="s", bot_tag="b", fr_tag="read", query="q"))

    assert tokens == ["The refund", " policy is X."]


def test_stream_ask_mid_stream_error_event_raises_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SSE_WITH_ERROR)

    collected: list[str] = []
    with _sync_client(handler) as client, pytest.raises(ApiError) as excinfo:
        for tok in client.stream_ask(session_id="s", bot_tag="b", fr_tag="read", query="q"):
            collected.append(tok)

    # Tokens before the error are still delivered; then the error event raises
    # (it is NOT swallowed by the [DONE] terminator that follows it).
    assert collected == ["partial answer"]
    assert excinfo.value.status_code == 500
    assert excinfo.value.code == "INTERNAL_ERROR"
    assert excinfo.value.request_id == "r-99"


# ---------------------------------------------------------------------------
# Async client streaming
# ---------------------------------------------------------------------------


def test_async_stream_ask_yields_tokens():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/qna/stream"
        return httpx.Response(200, content=SSE_BYTES)

    async def run() -> list[str]:
        async with AsyncTocDocClient(
            BASE_URL,
            max_retries=0,
            transport=httpx.MockTransport(handler),
        ) as client:
            return [
                tok
                async for tok in client.stream_ask(
                    session_id="s-1", bot_tag="acme", fr_tag="read", query="hi"
                )
            ]

    assert asyncio.run(run()) == ["Hello", "multi-1\nmulti-2", "world"]


def test_async_stream_ask_non_2xx_raises_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": {"code": "INTERNAL_ERROR", "message": "boom", "request_id": "r"}},
        )

    async def run() -> None:
        async with AsyncTocDocClient(
            BASE_URL, max_retries=0, transport=httpx.MockTransport(handler)
        ) as client:
            async for _ in client.stream_ask(session_id="s", bot_tag="b", fr_tag="f", query="q"):
                pass

    with pytest.raises(ApiError) as excinfo:
        asyncio.run(run())
    assert excinfo.value.status_code == 500


def test_async_stream_ask_answer_is_only_tokens_and_citation_surfaced():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SSE_WITH_CITATION)

    citations: list[dict] = []

    async def run() -> str:
        async with AsyncTocDocClient(
            BASE_URL, max_retries=0, transport=httpx.MockTransport(handler)
        ) as client:
            parts = [
                tok
                async for tok in client.stream_ask(
                    session_id="s", bot_tag="b", fr_tag="read", query="q", on_citation=citations.append
                )
            ]
        return "".join(parts)

    answer = asyncio.run(run())
    assert answer == "The refund policy is X."
    assert "{" not in answer
    assert citations == [CITATION_PAYLOAD]


def test_async_stream_ask_mid_stream_error_event_raises_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SSE_WITH_ERROR)

    collected: list[str] = []

    async def run() -> None:
        async with AsyncTocDocClient(
            BASE_URL, max_retries=0, transport=httpx.MockTransport(handler)
        ) as client:
            async for tok in client.stream_ask(session_id="s", bot_tag="b", fr_tag="read", query="q"):
                collected.append(tok)

    with pytest.raises(ApiError) as excinfo:
        asyncio.run(run())
    assert collected == ["partial answer"]
    assert excinfo.value.status_code == 500
    assert excinfo.value.code == "INTERNAL_ERROR"
    assert excinfo.value.request_id == "r-99"
