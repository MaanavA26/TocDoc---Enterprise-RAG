"""Tests for the TocDoc client SDK.

All HTTP is mocked with ``httpx.MockTransport`` — there is no live network.
The injectable ``sleep`` keeps retry tests instant and deterministic.
"""

from __future__ import annotations

import httpx
import pytest
from tocdoc_sdk import ApiError, QnAAnswer, TocDocClient

BASE_URL = "https://qna.example.test"


def _make_client(handler, *, token=None, max_retries=2):
    """Build a client wired to a MockTransport handler with no-op sleep."""
    return TocDocClient(
        BASE_URL,
        token=token,
        max_retries=max_retries,
        backoff_base=0.0,
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
    )


def test_ask_happy_path_returns_typed_answer():
    """A 2xx envelope deserializes into a typed QnAAnswer with flat citations."""

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

    with _make_client(handler) as client:
        result = client.ask(
            session_id="s-1",
            bot_tag="acme",
            fr_tag="read",
            query="What is the refund policy?",
        )

    assert isinstance(result, QnAAnswer)
    assert result.answer == "Refunds are processed within 30 days."
    # citation is a flat {filename: filepath} mapping.
    assert result.citations == {"policy.md": "/docs/policy.md"}
    assert result.citation.root == {"policy.md": "/docs/policy.md"}


def test_ask_sends_request_body_matching_contract():
    """The POST body matches the server's Payload shape."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"answer": "ok", "citation": {}})

    with _make_client(handler) as client:
        client.ask(session_id="s-9", bot_tag="acme", fr_tag="read", query="hi")

    assert captured["session_id"] == "s-9"
    assert captured["bot_tag"] == "acme"
    assert captured["fr_tag"] == "read"
    assert captured["bot"] == [{"user_query": "hi", "bot_response": None, "answer": None}]


def test_ask_with_full_history():
    """A full `bot` history is forwarded; dict turns are accepted."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"answer": "ok", "citation": {}})

    with _make_client(handler) as client:
        client.ask(
            session_id="s-2",
            bot_tag="acme",
            fr_tag="read",
            bot=[
                {"user_query": "first?", "bot_response": "answer 1"},
                {"user_query": "second?"},
            ],
        )

    assert len(captured["bot"]) == 2
    assert captured["bot"][0]["user_query"] == "first?"
    assert captured["bot"][1]["user_query"] == "second?"


def test_ask_requires_exactly_one_of_query_or_bot():
    """Passing neither or both of query/bot is a client-side ValueError."""
    with _make_client(lambda r: httpx.Response(200)) as client:
        with pytest.raises(ValueError):
            client.ask(session_id="s", bot_tag="b", fr_tag="f")
        with pytest.raises(ValueError):
            client.ask(
                session_id="s",
                bot_tag="b",
                fr_tag="f",
                query="q",
                bot=[{"user_query": "x"}],
            )


def test_bearer_token_sent_in_authorization_header():
    """The bearer token is sent in the Authorization header."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"answer": "ok", "citation": {}})

    with _make_client(handler, token="s3cr3t-token") as client:
        client.ask(session_id="s", bot_tag="b", fr_tag="f", query="q")

    assert seen["auth"] == "Bearer s3cr3t-token"


def test_no_authorization_header_without_token():
    """No Authorization header is sent when no token is configured."""
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"answer": "ok", "citation": {}})

    with _make_client(handler) as client:
        client.ask(session_id="s", bot_tag="b", fr_tag="f", query="q")

    assert seen["auth"] is None


@pytest.mark.parametrize(
    ("status", "code", "message"),
    [
        (401, "UNAUTHORIZED", "Missing or invalid token"),
        (400, "INVALID_REQUEST", "Bot tag cannot be empty"),
        (500, "INTERNAL_ERROR", "Internal server error"),
    ],
)
def test_error_envelope_raises_api_error(status, code, message):
    """4xx/5xx envelopes raise ApiError carrying code/message/request_id/status."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={
                "error": {
                    "code": code,
                    "message": message,
                    "request_id": "req-abc-123",
                }
            },
        )

    # max_retries=0 so the 500 case raises immediately rather than retrying.
    with _make_client(handler, max_retries=0) as client, pytest.raises(ApiError) as excinfo:
        client.ask(session_id="s", bot_tag="b", fr_tag="f", query="q")

    err = excinfo.value
    assert err.status_code == status
    assert err.code == code
    assert err.message == message
    assert err.request_id == "req-abc-123"


def test_validation_error_envelope_carries_errors_list():
    """A 422 VALIDATION_ERROR exposes the structured `errors` list."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed",
                    "request_id": "req-v-1",
                    "errors": [{"loc": ["body", "session_id"], "type": "missing", "msg": "Field required"}],
                }
            },
        )

    with _make_client(handler, max_retries=0) as client, pytest.raises(ApiError) as excinfo:
        client.ask(session_id="s", bot_tag="b", fr_tag="f", query="q")

    assert excinfo.value.code == "VALIDATION_ERROR"
    assert excinfo.value.errors[0]["type"] == "missing"


def test_non_envelope_error_body_is_handled_defensively():
    """A non-envelope error body (e.g. proxy HTML) degrades to a synthesized ApiError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>502 Bad Gateway</html>")

    with _make_client(handler, max_retries=0) as client, pytest.raises(ApiError) as excinfo:
        client.ask(session_id="s", bot_tag="b", fr_tag="f", query="q")

    err = excinfo.value
    assert err.status_code == 502
    assert err.code == "HTTP_502"
    assert err.request_id is None


def test_transient_503_is_retried_then_raises():
    """A persistent 503 is retried max_retries times, then raises ApiError."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            503,
            json={"error": {"code": "UPSTREAM_UNAVAILABLE", "message": "down", "request_id": "r"}},
        )

    with _make_client(handler, max_retries=2) as client, pytest.raises(ApiError) as excinfo:
        client.ask(session_id="s", bot_tag="b", fr_tag="f", query="q")

    # initial attempt + 2 retries = 3 total calls.
    assert calls["n"] == 3
    assert excinfo.value.status_code == 503


def test_transient_503_then_success():
    """A 503 followed by a 200 succeeds after retrying."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                503,
                json={"error": {"code": "UPSTREAM_UNAVAILABLE", "message": "down", "request_id": "r"}},
            )
        return httpx.Response(200, json={"answer": "recovered", "citation": {}})

    with _make_client(handler, max_retries=2) as client:
        result = client.ask(session_id="s", bot_tag="b", fr_tag="f", query="q")

    assert calls["n"] == 2
    assert result.answer == "recovered"


def test_400_is_not_retried():
    """A 4xx is never retried — exactly one call is made."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            400,
            json={"error": {"code": "INVALID_REQUEST", "message": "bad", "request_id": "r"}},
        )

    with _make_client(handler, max_retries=3) as client, pytest.raises(ApiError):
        client.ask(session_id="s", bot_tag="b", fr_tag="f", query="q")

    assert calls["n"] == 1


def test_connect_error_is_retried_then_raises():
    """A persistent connect error is retried then re-raised."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused", request=request)

    with _make_client(handler, max_retries=2) as client, pytest.raises(httpx.ConnectError):
        client.ask(session_id="s", bot_tag="b", fr_tag="f", query="q")

    assert calls["n"] == 3


def test_extra_keys_in_success_body_are_ignored():
    """The server may emit defensive optional keys; the client tolerates them."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "answer": "ok",
                "citation": {"a.md": "/docs/a.md"},
                "request_id": "stray",
                "error": None,
            },
        )

    with _make_client(handler) as client:
        result = client.ask(session_id="s", bot_tag="b", fr_tag="f", query="q")

    assert result.answer == "ok"
    assert result.citations == {"a.md": "/docs/a.md"}
