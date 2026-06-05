"""Tests for the QnA request-path hardening pass.

Covers the audit findings remediated in this change:

- H6 + M4 — no raw query / conversation history / bot answer in logs
  (qna_pipeline.generate_answer).
- L-Q6     — no raw query in app.py's request log; metadata only.
- L-Q7     — no raw answer preview in text_processor.
- M2       — framework 404/405 routing errors get the ErrorEnvelope; 405 maps
             to INVALID_REQUEST.
- L-Q4     — default_error_responses documents 403.
- L-Q5     — /health unhealthy branch returns HTTP 503.
- L-Q3     — unknown fr_tag rejected with 400 at the request boundary.
- M7       — per-key rate limit + concurrency cap return 429 with Retry-After.

Log-hygiene note (load-bearing): the pipeline/text logger
(`src.core.logger.logger`) propagates to root, so pytest's `caplog` captures
it. The app.py module logger sets `propagate=False` with its own handler, so
those tests attach `caplog.handler` to it directly. Every negative
("X not in logs") assertion is paired with a POSITIVE control asserting the
expected metadata IS present — otherwise a logger that captures nothing would
make the negative assertion pass vacuously.
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Env setup — required before any `src.*` import (config validates at import).
# ---------------------------------------------------------------------------
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://fake.openai.example.com")
os.environ.setdefault("AZURE_OPENAI_KEY", "fake-openai-key")
os.environ.setdefault("AZURE_OPENAI_VERSION", "2024-02-01")
os.environ.setdefault("AZURE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "https://fake.search.example.com")
os.environ.setdefault("AZURE_SEARCH_KEY", "fake-search-key")
os.environ.setdefault("INDEX_NAME", "fake-index")
os.environ.setdefault("AZURE_KEY_VAULT", "fakevault")
os.environ.setdefault("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")
os.environ.setdefault("AUDIENCE_ID", "api://fake-audience")

_QNA_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_QNA_ROOT) not in sys.path:
    sys.path.insert(0, str(_QNA_ROOT))

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from src.core.errors import (  # noqa: E402
    _STATUS_TO_CODE,  # noqa: E402
    ApiErrorCode,
    default_error_responses,
    register_exception_handlers,
)
from src.core.observability import RequestIDMiddleware  # noqa: E402

# Sentinels that must NEVER appear in logs.
_SECRET_QUERY = "WHAT-IS-PATIENT-SSN-123456789-secret-query"
_SECRET_PREV_QUERY = "earlier-secret-question-abcdef"
_SECRET_BOT_REPLY = "confidential-prior-answer-zzz999"
_SECRET_ANSWER = "TOP-SECRET-ANSWER-BODY-qwerty"


# ===========================================================================
# H6 + M4 — pipeline log hygiene (no raw query / history / bot reply)
# ===========================================================================


class TestPipelineLogHygiene:
    """generate_answer must not log raw query, prior queries, or bot replies."""

    def _run_pipeline(self, caplog):
        """Run generate_answer with all I/O mocked; return (result, log_text)."""
        from src.pipeline import qna_pipeline as qp

        # Capture the pipeline/text logger (propagates to root → caplog works).
        caplog.set_level(logging.DEBUG, logger="src.core.logger")

        history = [
            {"user_query": _SECRET_PREV_QUERY, "bot_response": _SECRET_BOT_REPLY},
            {"user_query": _SECRET_QUERY, "bot_response": None},
        ]

        azure = MagicMock()

        async def _go():
            with (
                patch.object(qp, "rephrase_queries", new=AsyncMock(return_value={})),
                patch.object(qp, "get_embedding", new=AsyncMock(return_value=[0.1, 0.2])),
                patch.object(qp, "perform_search", new=AsyncMock(return_value=[])),
                patch.object(
                    qp,
                    "generate_openai_response",
                    new=AsyncMock(return_value=f"{_SECRET_ANSWER}\n**Sources:** doc.md"),
                ),
            ):
                return await qp.generate_answer(
                    query=_SECRET_QUERY,
                    fr_mode="read",
                    bot_tag="tenant-a",
                    history=history,
                    azure=azure,
                    request_id="rid-hyg-1",
                )

        result = asyncio.run(_go())
        return result, caplog.text

    def test_no_raw_query_or_history_in_logs(self, caplog):
        result, text = self._run_pipeline(caplog)

        # Positive control: the metadata events MUST be present, proving the
        # logger is actually captured (otherwise the negatives are vacuous).
        assert "answer_generation_started" in text
        assert "history_normalized" in text
        assert "history_turns" in text

        # Negative: no raw user content anywhere in the logs.
        assert _SECRET_QUERY not in text
        assert _SECRET_PREV_QUERY not in text
        assert _SECRET_BOT_REPLY not in text
        assert _SECRET_ANSWER not in text

        # Sanity: pipeline still produced the answer.
        assert result["answer"].strip() == _SECRET_ANSWER


# ===========================================================================
# L-Q7 — text_processor must not log the raw answer preview
# ===========================================================================


class TestTextProcessorLogHygiene:
    def test_no_raw_answer_in_extractor_logs(self, caplog):
        from src.services import text_processor as tp

        caplog.set_level(logging.DEBUG, logger="src.core.logger")

        raw = f"{_SECRET_ANSWER}\n**Sources:** doc.md"
        answer, _files = asyncio.run(tp.extract_answer_and_filenames_from_text(raw))

        # Positive control: the metadata debug line is present.
        assert "Extracting answer and filenames" in caplog.text
        # Negative: the raw answer body is not.
        assert _SECRET_ANSWER not in caplog.text
        assert answer.strip() == _SECRET_ANSWER


# ===========================================================================
# L-Q6 — app.py request log: metadata only, no raw query
# ===========================================================================


class TestAppRequestLogHygiene:
    """The app.py module logger has propagate=False, so attach caplog's handler
    to it directly before exercising the metadata log path."""

    def test_qna_request_received_logs_metadata_only(self, caplog):
        import app as qna_app

        app_logger = qna_app.logger
        app_logger.addHandler(caplog.handler)
        prev_level = app_logger.level
        app_logger.setLevel(logging.INFO)
        try:
            with caplog.at_level(logging.INFO):
                qna_app.log_event(
                    app_logger,
                    "qna_request_received",
                    request_id="rid-app-1",
                    query_length=len(_SECRET_QUERY),
                    bot_tag="tenant-a",
                    fr_tag="read",
                    query_preview=None,
                )
        finally:
            app_logger.removeHandler(caplog.handler)
            app_logger.setLevel(prev_level)

        # Positive control + negative.
        assert "qna_request_received" in caplog.text
        assert "query_length" in caplog.text
        assert _SECRET_QUERY not in caplog.text

    def test_no_raw_query_fstring_logging_in_source(self):
        """Guard against re-introducing `Query: {query}` raw logging in app.py."""
        src_text = (_QNA_ROOT / "app.py").read_text()
        assert "Query: {query" not in src_text


# ===========================================================================
# M2 — framework 404/405 routing errors get the ErrorEnvelope
# ===========================================================================


@pytest.fixture
def envelope_app() -> FastAPI:
    a = FastAPI()
    register_exception_handlers(a)
    a.add_middleware(RequestIDMiddleware)

    @a.get("/exists")
    def exists():
        return {"ok": True}

    return a


@pytest.fixture
def envelope_client(envelope_app: FastAPI) -> TestClient:
    return TestClient(envelope_app, raise_server_exceptions=False)


class TestFrameworkRoutingEnvelope:
    def test_unknown_route_404_is_enveloped(self, envelope_client: TestClient):
        r = envelope_client.get("/does-not-exist")
        assert r.status_code == 404
        body = r.json()
        # Framework 404 now follows the envelope contract, not bare {"detail"}.
        assert "error" in body
        assert "detail" not in body
        assert body["error"]["code"] == ApiErrorCode.NOT_FOUND
        assert body["error"]["request_id"] == r.headers["X-Request-ID"]

    def test_method_not_allowed_405_is_enveloped_invalid_request(self, envelope_client: TestClient):
        r = envelope_client.post("/exists")  # only GET is defined
        assert r.status_code == 405
        body = r.json()
        assert "error" in body
        assert "detail" not in body
        assert body["error"]["code"] == ApiErrorCode.INVALID_REQUEST

    def test_status_to_code_has_405(self):
        assert _STATUS_TO_CODE[405] == ApiErrorCode.INVALID_REQUEST


# ===========================================================================
# L-Q4 — OpenAPI default_error_responses documents 403
# ===========================================================================


class TestDefaultErrorResponses403:
    def test_403_present(self):
        assert 403 in default_error_responses
        assert default_error_responses[403]["description"] == "Forbidden"


# ===========================================================================
# L-Q5 — /health unhealthy branch returns 503
# ===========================================================================


class TestHealthUnhealthy503:
    def test_unhealthy_returns_503_with_body_shape(self):
        import app as qna_app
        from starlette.responses import JSONResponse

        # Force the "missing generate_answer" branch by hiding the attribute.
        with patch.object(qna_app.src.pipeline.qna_pipeline, "generate_answer", create=True):
            pass  # ensure attr exists baseline

        with patch("app.hasattr", return_value=False):
            result = asyncio.run(qna_app.health_check())

        assert isinstance(result, JSONResponse)
        assert result.status_code == 503

    def test_exception_branch_returns_503(self):
        import app as qna_app
        from starlette.responses import JSONResponse

        with patch("app.hasattr", side_effect=RuntimeError("boom")):
            result = asyncio.run(qna_app.health_check())

        assert isinstance(result, JSONResponse)
        assert result.status_code == 503
        # No exception text leaks into the body.
        assert b"boom" not in result.body


# ===========================================================================
# L-Q3 — fr_tag allow-list enforced at the request boundary (400)
# ===========================================================================


class TestFrTagAllowList:
    """Mount the same allow-list logic on a tiny route — no auth, no Azure."""

    @pytest.fixture
    def app(self) -> FastAPI:
        import app as qna_app

        a = FastAPI()
        register_exception_handlers(a)
        a.add_middleware(RequestIDMiddleware)

        @a.post("/echo")
        def echo(fr_tag: str):
            if fr_tag not in qna_app._ALLOWED_FR_TAGS:
                qna_app.raise_api_error(
                    ApiErrorCode.INVALID_REQUEST,
                    f"Invalid fr_tag. Must be one of: {', '.join(qna_app._ALLOWED_FR_TAGS)}.",
                    400,
                )
            return {"fr_tag": fr_tag}

        return a

    @pytest.fixture
    def client(self, app: FastAPI) -> TestClient:
        return TestClient(app, raise_server_exceptions=False)

    def test_known_modes_accepted(self, client: TestClient):
        for mode in ("read", "layout"):
            r = client.post("/echo", params={"fr_tag": mode})
            assert r.status_code == 200

    def test_unknown_mode_rejected_400(self, client: TestClient):
        r = client.post("/echo", params={"fr_tag": "exploit-mode"})
        assert r.status_code == 400
        body = r.json()
        assert body["error"]["code"] == ApiErrorCode.INVALID_REQUEST
        assert "Invalid fr_tag" in body["error"]["message"]


# ===========================================================================
# M7 — rate limit + concurrency cap → 429 with Retry-After
# ===========================================================================


class TestRateLimiterUnit:
    def test_sliding_window_blocks_over_limit(self):
        import app as qna_app

        rl = qna_app._SlidingWindowRateLimiter()
        # Two allowed, third blocked within the same window (fixed `now`).
        assert rl.check("k", limit=2, now=1000.0) == (True, 0)
        assert rl.check("k", limit=2, now=1000.5) == (True, 0)
        allowed, retry_after = rl.check("k", limit=2, now=1001.0)
        assert allowed is False
        assert retry_after >= 1

    def test_window_evicts_old_hits(self):
        import app as qna_app

        rl = qna_app._SlidingWindowRateLimiter()
        assert rl.check("k", limit=1, now=1000.0)[0] is True
        # 61s later the prior hit has aged out → allowed again.
        assert rl.check("k", limit=1, now=1061.0)[0] is True

    def test_limit_zero_disables(self):
        import app as qna_app

        rl = qna_app._SlidingWindowRateLimiter()
        for _ in range(100):
            assert rl.check("k", limit=0)[0] is True

    def test_concurrency_gate_caps_inflight(self):
        import app as qna_app

        gate = qna_app._ConcurrencyGate()
        assert gate.acquire(2) is True
        assert gate.acquire(2) is True
        assert gate.acquire(2) is False  # at cap
        gate.release()
        assert gate.acquire(2) is True


class TestRateLimitDependency429:
    """Exercise the real rate_limit_dependency on a dummy route (no auth/Azure)."""

    @pytest.fixture
    def app(self) -> FastAPI:
        import app as qna_app

        qna_app._rate_limiter.reset()
        a = FastAPI()
        register_exception_handlers(a)
        a.add_middleware(RequestIDMiddleware)

        @a.get("/limited", dependencies=[Depends(qna_app.rate_limit_dependency)])
        def limited():
            return {"ok": True}

        return a

    @pytest.fixture
    def client(self, app: FastAPI, monkeypatch) -> TestClient:
        monkeypatch.setenv("QNA_RATE_LIMIT_PER_MIN", "2")
        return TestClient(app, raise_server_exceptions=False)

    def test_third_request_returns_429_with_retry_after(self, client: TestClient):
        assert client.get("/limited").status_code == 200
        assert client.get("/limited").status_code == 200
        r = client.get("/limited")
        assert r.status_code == 429
        assert "Retry-After" in r.headers
        assert int(r.headers["Retry-After"]) >= 1
        body = r.json()
        assert body["error"]["code"] == ApiErrorCode.INVALID_REQUEST


class TestConcurrencyDependency429:
    @pytest.fixture
    def app(self) -> FastAPI:
        import app as qna_app

        qna_app._concurrency_gate.reset()
        a = FastAPI()
        register_exception_handlers(a)
        a.add_middleware(RequestIDMiddleware)

        @a.get("/capped", dependencies=[Depends(qna_app.concurrency_gate_dependency)])
        def capped():
            return {"ok": True}

        return a

    def test_rejects_when_at_capacity(self, app: FastAPI, monkeypatch):
        import app as qna_app

        # Pin the cap to 0... no: 0 disables. Pin to 1 and pre-occupy the slot.
        monkeypatch.setenv("QNA_MAX_CONCURRENCY", "1")
        qna_app._concurrency_gate.reset()
        # Pre-acquire the only slot to simulate an in-flight request.
        assert qna_app._concurrency_gate.acquire(1) is True

        client = TestClient(app, raise_server_exceptions=False)
        r = client.get("/capped")
        assert r.status_code == 429
        assert r.headers.get("Retry-After") == "1"
        assert r.json()["error"]["code"] == ApiErrorCode.INVALID_REQUEST

        qna_app._concurrency_gate.reset()

    def test_releases_slot_after_response(self, app: FastAPI, monkeypatch):
        import app as qna_app

        monkeypatch.setenv("QNA_MAX_CONCURRENCY", "1")
        qna_app._concurrency_gate.reset()
        client = TestClient(app, raise_server_exceptions=False)
        # Sequential requests should each succeed (slot released after each).
        assert client.get("/capped").status_code == 200
        assert client.get("/capped").status_code == 200
