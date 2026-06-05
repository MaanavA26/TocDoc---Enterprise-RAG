"""Tests for the method-aware retry policy and jittered backoff (L-SDK1/L-SDK2).

All HTTP is mocked with ``httpx.MockTransport`` — there is no live network.
Injected no-op sleeps keep retry tests instant; an injected RNG makes the
backoff jitter deterministic.

The policy splits by *idempotency*, not raw verb:

- Idempotent (admin GETs, the ``POST /qna`` query) retry on 5xx and on any
  transient connect/read/write timeout.
- Non-idempotent (``trigger_connector_sync`` POST) retry ONLY on connect-phase
  errors — never on a 5xx or a post-send read/write timeout.
"""

from __future__ import annotations

import httpx
import pytest
from tocdoc_sdk import AdminClient, ApiError, TocDocClient
from tocdoc_sdk._retry import compute_backoff

ADMIN_URL = "https://ingestion.example.test"
QNA_URL = "https://qna.example.test"


def _admin_client(handler, *, max_retries=2):
    """Admin client wired to a MockTransport with no-op sleep + fixed RNG."""
    return AdminClient(
        ADMIN_URL,
        admin_token="admin-secret",
        max_retries=max_retries,
        backoff_base=0.0,
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
        rng=lambda: 0.5,
    )


def _qna_client(handler, *, max_retries=2):
    """QnA client wired to a MockTransport with no-op sleep + fixed RNG."""
    return TocDocClient(
        QNA_URL,
        max_retries=max_retries,
        backoff_base=0.0,
        transport=httpx.MockTransport(handler),
        sleep=lambda _seconds: None,
        rng=lambda: 0.5,
    )


# ---------------------------------------------------------------------------
# L-SDK1 — non-idempotent POST (trigger_connector_sync)
# ---------------------------------------------------------------------------


def test_nonidempotent_post_not_retried_on_5xx():
    """A connector-sync POST is NOT retried on a 5xx (would duplicate the run)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            503,
            json={"error": {"code": "UPSTREAM_UNAVAILABLE", "message": "down", "request_id": "r"}},
        )

    with _admin_client(handler, max_retries=3) as admin, pytest.raises(ApiError) as excinfo:
        admin.trigger_connector_sync("blob")

    # Exactly one attempt — the 503 is surfaced immediately, never retried.
    assert calls["n"] == 1
    assert excinfo.value.status_code == 503


def test_nonidempotent_post_not_retried_on_read_timeout():
    """A connector-sync POST is NOT retried on a post-send ReadTimeout."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    with _admin_client(handler, max_retries=3) as admin, pytest.raises(httpx.ReadTimeout):
        admin.trigger_connector_sync("blob")

    # The server may already have accepted the request, so it is not re-sent.
    assert calls["n"] == 1


def test_nonidempotent_post_not_retried_on_write_timeout():
    """A connector-sync POST is NOT retried on a WriteTimeout."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.WriteTimeout("write timed out", request=request)

    with _admin_client(handler, max_retries=3) as admin, pytest.raises(httpx.WriteTimeout):
        admin.trigger_connector_sync("blob")

    assert calls["n"] == 1


def test_nonidempotent_post_is_retried_on_connect_timeout():
    """A connector-sync POST IS retried on a connect-phase error (never reached server)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("connect timed out", request=request)

    with _admin_client(handler, max_retries=2) as admin, pytest.raises(httpx.ConnectTimeout):
        admin.trigger_connector_sync("blob")

    # initial attempt + 2 retries = 3 total: connect-phase is safe to re-send.
    assert calls["n"] == 3


def test_nonidempotent_post_is_retried_on_connect_error():
    """A connector-sync POST IS retried on a ConnectError, then succeeds."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(202, json={"run_id": "r1", "source_type": "blob", "status": "started"})

    with _admin_client(handler, max_retries=2) as admin:
        run = admin.trigger_connector_sync("blob")

    assert calls["n"] == 2
    assert run.run_id == "r1"


# ---------------------------------------------------------------------------
# L-SDK1 — idempotent GET keeps full transient policy
# ---------------------------------------------------------------------------


def test_idempotent_get_is_retried_on_5xx():
    """An admin GET still retries on a transient 5xx (idempotent read)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            503,
            json={"error": {"code": "UPSTREAM_UNAVAILABLE", "message": "down", "request_id": "r"}},
        )

    with _admin_client(handler, max_retries=2) as admin, pytest.raises(ApiError) as excinfo:
        admin.index_stats(bot_tag="acme")

    # initial attempt + 2 retries = 3 total.
    assert calls["n"] == 3
    assert excinfo.value.status_code == 503


def test_idempotent_get_is_retried_on_read_timeout():
    """An admin GET retries on a post-send ReadTimeout (idempotent read)."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("read timed out", request=request)

    with _admin_client(handler, max_retries=2) as admin, pytest.raises(httpx.ReadTimeout):
        admin.index_stats(bot_tag="acme")

    assert calls["n"] == 3


def test_idempotent_qna_post_still_retried_on_5xx():
    """The /qna query POST is idempotent, so it still retries on 5xx then succeeds."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                503,
                json={"error": {"code": "UPSTREAM_UNAVAILABLE", "message": "down", "request_id": "r"}},
            )
        return httpx.Response(200, json={"answer": "recovered", "citation": {}})

    with _qna_client(handler, max_retries=2) as client:
        result = client.ask(session_id="s", bot_tag="b", fr_tag="f", query="q")

    assert calls["n"] == 2
    assert result.answer == "recovered"


# ---------------------------------------------------------------------------
# L-SDK2 — jittered backoff
# ---------------------------------------------------------------------------


def test_compute_backoff_applies_full_jitter_deterministically():
    """compute_backoff scales the exponential ceiling by the injected RNG value."""
    # rng returns 0.5, so each sleep is half of base * 2**attempt.
    assert compute_backoff(0, base=1.0, rng=lambda: 0.5) == 0.5
    assert compute_backoff(1, base=1.0, rng=lambda: 0.5) == 1.0
    assert compute_backoff(2, base=1.0, rng=lambda: 0.5) == 2.0

    # rng=0.0 floors to 0; rng just below 1.0 approaches the full ceiling.
    assert compute_backoff(3, base=0.5, rng=lambda: 0.0) == 0.0
    assert compute_backoff(0, base=2.0, rng=lambda: 0.25) == 0.5


def test_compute_backoff_never_exceeds_exponential_ceiling():
    """With rng in [0, 1), the jittered delay stays within [0, base * 2**attempt]."""
    for attempt in range(4):
        ceiling = 0.5 * (2**attempt)
        # Sample the extremes of the RNG range.
        assert compute_backoff(attempt, base=0.5, rng=lambda: 0.0) == 0.0
        assert compute_backoff(attempt, base=0.5, rng=lambda: 0.999999) <= ceiling


def test_client_backoff_uses_injected_rng_for_jitter():
    """The sync client routes its retry sleep through compute_backoff(rng=...)."""
    sleeps: list[float] = []
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            503,
            json={"error": {"code": "UPSTREAM_UNAVAILABLE", "message": "down", "request_id": "r"}},
        )

    # base=4.0, rng=0.5 -> sleeps are 0.5*4*2**attempt = [2.0, 4.0] across 2 retries.
    client = TocDocClient(
        QNA_URL,
        max_retries=2,
        backoff_base=4.0,
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        rng=lambda: 0.5,
    )
    with client, pytest.raises(ApiError):
        client.ask(session_id="s", bot_tag="b", fr_tag="f", query="q")

    assert calls["n"] == 3
    assert sleeps == [2.0, 4.0]
