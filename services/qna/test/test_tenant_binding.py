"""Tests for the default-OFF within-tenant bot_tag<->tid binding guard (R1)
and the health-check stack-trace exposure fix (CodeQL py/stack-trace-exposure).

Covers:
  * Health endpoint: a failure in the readiness probe leaks no exception text.
  * Binding OFF (default): request path is unchanged — the QnA call still runs.
  * Binding ON + mismatched bot_tag: rejected via the envelope, NO QnA call.
  * Binding ON + allowed bot_tag: request passes through to the QnA call.
  * Binding ON + unmapped tid / missing tid / malformed map: fail closed.

The guard is exercised both as a unit (fake request) and end-to-end through a
minimal FastAPI app that mounts a tiny middleware to set `request.state.tid`
(the real `validate_token` is never invoked — no network call).
"""

import os
import pathlib
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Env setup — required BEFORE importing src.* (config enforces env at import).
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

from src.core.errors import ApiErrorCode, register_exception_handlers  # noqa: E402
from src.core.observability import RequestIDMiddleware  # noqa: E402
from src.core.tenant_binding import enforce_tenant_bot_tag_binding  # noqa: E402

TID_A = "tenant-aaaa"
MAP_JSON = '{"tenant-aaaa": ["workspace-a", "workspace-a2"], "tenant-bbbb": ["workspace-b"]}'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class _FakeState:
    pass


class _FakeRequest:
    """Minimal stand-in for a Starlette Request with a `.state`."""

    def __init__(self, tid=None, request_id="rid-test"):
        self.state = _FakeState()
        if tid is not None:
            self.state.tid = tid
        self.state.request_id = request_id


# ===========================================================================
# Part 1 — health-check stack-trace exposure fix
# ===========================================================================
class TestHealthCheckNoStackTrace:
    @pytest.fixture
    def client(self) -> TestClient:
        import app as qna_app

        return TestClient(qna_app.app, raise_server_exceptions=False)

    def test_health_failure_leaks_no_exception_text(self, client: TestClient):
        """When the readiness probe raises, the response must carry a generic
        message — never `str(e)` (CodeQL py/stack-trace-exposure) — AND return
        HTTP 503 so probes pull the instance from rotation (audit L-Q5)."""
        secret = "sensitive internal detail zzz999"

        class _Boom:
            def now(self):
                raise RuntimeError(secret)

        # Force the success branch (hasattr is True) into the except path by
        # making datetime.now() raise inside the handler.
        with patch("app.datetime", _Boom()):
            r = client.get("/health")

        # Unhealthy branch now returns 503 (L-Q5) while preserving the body
        # shape for external monitors.
        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "error"
        # No exception text / class anywhere in the response.
        assert secret not in r.text
        assert "RuntimeError" not in r.text
        assert body["qna_module"] == "unavailable"


# ===========================================================================
# Part 2 — production wiring: auth middleware populates request.state.tid
# ===========================================================================
class TestAuthAttachesTid:
    """The guard reads `request.state.tid`; this verifies the auth middleware
    actually populates it from the validated token's `tid` claim. Without this
    the feature would fail closed silently in prod when enforcement is ON.
    `validate_token` is mocked — no JWKS / network call.
    """

    def test_middleware_sets_tid_from_validated_claims(self):
        import asyncio

        from src.core.auth import AuthUtils

        captured = {}

        async def call_next(request):
            captured["tid"] = getattr(request.state, "tid", "UNSET")
            captured["email"] = getattr(request.state, "email", "UNSET")
            return "downstream-response"

        req = _FakeRequest(tid=None)
        req.method = "GET"

        class _URL:
            path = "/qna"

        req.url = _URL()
        req.headers = {"Authorization": "Bearer fake.jwt.token"}

        claims = {"upn": "user@example.com", "tid": TID_A}
        with patch("src.core.auth.validate_token", new=AsyncMock(return_value=claims)):
            result = asyncio.run(AuthUtils.auth_middleware(req, call_next))

        assert result == "downstream-response"
        assert captured["tid"] == TID_A
        assert captured["email"] == "user@example.com"


# ===========================================================================
# Part 2 — guard unit tests (fake request)
# ===========================================================================
class TestGuardUnit:
    def test_off_is_inert_even_with_no_tid_and_no_map(self, monkeypatch):
        """Default OFF: guard returns immediately; missing tid / map irrelevant."""
        monkeypatch.delenv("QNA_ENFORCE_TENANT_BINDING", raising=False)
        monkeypatch.delenv("QNA_TENANT_BOT_TAG_MAP", raising=False)
        # No raise == pass.
        enforce_tenant_bot_tag_binding(_FakeRequest(tid=None), "anything-goes")

    def test_off_does_not_parse_malformed_map(self, monkeypatch):
        """A malformed map must never affect the OFF path."""
        monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "false")
        monkeypatch.setenv("QNA_TENANT_BOT_TAG_MAP", "{not valid json")
        enforce_tenant_bot_tag_binding(_FakeRequest(tid=TID_A), "workspace-a")

    def test_on_allowed_bot_tag_passes(self, monkeypatch):
        monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "true")
        monkeypatch.setenv("QNA_TENANT_BOT_TAG_MAP", MAP_JSON)
        enforce_tenant_bot_tag_binding(_FakeRequest(tid=TID_A), "workspace-a")
        enforce_tenant_bot_tag_binding(_FakeRequest(tid=TID_A), "workspace-a2")

    def test_on_mismatched_bot_tag_rejected(self, monkeypatch):
        """bot_tag belonging to another tenant within the map is rejected."""
        monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "true")
        monkeypatch.setenv("QNA_TENANT_BOT_TAG_MAP", MAP_JSON)
        with pytest.raises(HTTPException) as ei:
            enforce_tenant_bot_tag_binding(_FakeRequest(tid=TID_A), "workspace-b")
        assert ei.value.status_code == 403
        assert ei.value.detail["code"] == ApiErrorCode.UNAUTHORIZED
        # Generic message — never echoes the bot_tag or tid.
        assert "workspace-b" not in ei.value.detail["message"]
        assert TID_A not in ei.value.detail["message"]

    def test_on_unmapped_tid_fails_closed(self, monkeypatch):
        monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "true")
        monkeypatch.setenv("QNA_TENANT_BOT_TAG_MAP", MAP_JSON)
        with pytest.raises(HTTPException) as ei:
            enforce_tenant_bot_tag_binding(_FakeRequest(tid="tenant-unknown"), "workspace-a")
        assert ei.value.status_code == 403

    def test_on_missing_tid_fails_closed(self, monkeypatch):
        monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "true")
        monkeypatch.setenv("QNA_TENANT_BOT_TAG_MAP", MAP_JSON)
        with pytest.raises(HTTPException) as ei:
            enforce_tenant_bot_tag_binding(_FakeRequest(tid=None), "workspace-a")
        assert ei.value.status_code == 403

    def test_on_malformed_map_fails_closed(self, monkeypatch):
        """Enforcement on + unparseable map must reject (not 500)."""
        monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "true")
        monkeypatch.setenv("QNA_TENANT_BOT_TAG_MAP", "{not valid json")
        with pytest.raises(HTTPException) as ei:
            enforce_tenant_bot_tag_binding(_FakeRequest(tid=TID_A), "workspace-a")
        assert ei.value.status_code == 403

    def test_on_missing_map_fails_closed(self, monkeypatch):
        monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "true")
        monkeypatch.delenv("QNA_TENANT_BOT_TAG_MAP", raising=False)
        with pytest.raises(HTTPException) as ei:
            enforce_tenant_bot_tag_binding(_FakeRequest(tid=TID_A), "workspace-a")
        assert ei.value.status_code == 403

    def test_on_non_list_allowlist_fails_closed(self, monkeypatch):
        """A tid mapped to a non-list value is normalised to empty → reject."""
        monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "true")
        monkeypatch.setenv("QNA_TENANT_BOT_TAG_MAP", '{"tenant-aaaa": "workspace-a"}')
        with pytest.raises(HTTPException) as ei:
            enforce_tenant_bot_tag_binding(_FakeRequest(tid=TID_A), "workspace-a")
        assert ei.value.status_code == 403


# ===========================================================================
# Part 2 — end-to-end guard placement: no QnA call on reject, call on allow
# ===========================================================================
class _MiniApp:
    """Builds a minimal app exposing the guard at the same call-site position
    as the real `/qna` handler, with both QnA entrypoints mockable so we can
    assert the load-bearing "no search on reject" property.
    """

    def __init__(self, tid):
        self.app = FastAPI()
        register_exception_handlers(self.app)

        @self.app.middleware("http")
        async def set_tid(request: Request, call_next):
            if tid is not None:
                request.state.tid = tid
            return await call_next(request)

        self.app.add_middleware(RequestIDMiddleware)

        # AsyncMock entrypoints standing in for the legacy + agentic QnA calls.
        self.legacy = AsyncMock(return_value={"answer": "a", "citation": {}})
        self.agentic = AsyncMock(return_value={"answer": "a", "citation": {}})

        @self.app.post("/qna")
        async def qna(request: Request):
            body = await request.json()
            bot_tag = (body.get("bot_tag") or "").strip()
            # Same call-site order as app.custom_rag_qna: guard before fork.
            enforce_tenant_bot_tag_binding(request, bot_tag)
            return await self.legacy(bot_tag=bot_tag)


def _post(mini, bot_tag):
    client = TestClient(mini.app, raise_server_exceptions=False)
    return client.post("/qna", json={"bot_tag": bot_tag})


class TestGuardInRequestPath:
    def test_off_behaviour_unchanged_qna_runs(self, monkeypatch):
        monkeypatch.delenv("QNA_ENFORCE_TENANT_BINDING", raising=False)
        mini = _MiniApp(tid=TID_A)
        r = _post(mini, "any-bot-tag")
        assert r.status_code == 200
        mini.legacy.assert_awaited_once()

    def test_on_allowed_qna_runs(self, monkeypatch):
        monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "true")
        monkeypatch.setenv("QNA_TENANT_BOT_TAG_MAP", MAP_JSON)
        mini = _MiniApp(tid=TID_A)
        r = _post(mini, "workspace-a")
        assert r.status_code == 200
        mini.legacy.assert_awaited_once()

    def test_on_mismatch_rejected_no_qna_call(self, monkeypatch):
        monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "true")
        monkeypatch.setenv("QNA_TENANT_BOT_TAG_MAP", MAP_JSON)
        mini = _MiniApp(tid=TID_A)
        r = _post(mini, "workspace-b")
        assert r.status_code == 403
        body = r.json()
        assert body["error"]["code"] == ApiErrorCode.UNAUTHORIZED
        # The load-bearing assertion: NO QnA / search call happened.
        mini.legacy.assert_not_called()

    def test_on_unmapped_tid_rejected_no_qna_call(self, monkeypatch):
        monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "true")
        monkeypatch.setenv("QNA_TENANT_BOT_TAG_MAP", MAP_JSON)
        mini = _MiniApp(tid="tenant-unknown")
        r = _post(mini, "workspace-a")
        assert r.status_code == 403
        mini.legacy.assert_not_called()


# ===========================================================================
# Full /qna integration: guard rejects before the real pipeline is invoked
# ===========================================================================
class TestRealAppGuardShortCircuits:
    """Drive the real `app.custom_rag_qna` and assert that, on a binding
    rejection, the legacy QnA pipeline is never invoked (no search).
    """

    def test_real_handler_rejects_before_pipeline(self, monkeypatch):
        monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "true")
        monkeypatch.setenv("QNA_TENANT_BOT_TAG_MAP", MAP_JSON)
        import asyncio

        import app as qna_app
        from src.utils.util import Payload

        payload = Payload(
            session_id="s1",
            bot=[{"user_query": "hello", "bot_response": None}],
            bot_tag="workspace-b",  # not allowed for TID_A
            fr_tag="read",
        )

        # Fake request carrying a validated tid + the azure client holder the
        # handler reads off request.app.state.azure.
        req = _FakeRequest(tid=TID_A)
        req.app = MagicMock()
        req.app.state.azure = MagicMock()

        with patch.object(
            qna_app.src.pipeline.qna_pipeline,
            "generate_answer",
            new=AsyncMock(return_value={"answer": "x", "citation": {}}),
        ) as legacy:
            with pytest.raises(HTTPException) as ei:
                asyncio.run(qna_app.custom_rag_qna(payload, req))  # type: ignore[arg-type]
            assert ei.value.status_code == 403
            legacy.assert_not_called()

    def test_real_handler_allows_mapped_bot_tag(self, monkeypatch):
        """The allowed path reaches the (mocked) pipeline — proves the guard
        does not over-reject when bot_tag is in the tenant's allowlist."""
        monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "true")
        monkeypatch.setenv("QNA_TENANT_BOT_TAG_MAP", MAP_JSON)
        import asyncio

        import app as qna_app
        from src.utils.util import Payload

        payload = Payload(
            session_id="s1",
            bot=[{"user_query": "hello", "bot_response": None}],
            bot_tag="workspace-a",  # allowed for TID_A
            fr_tag="read",
        )
        req = _FakeRequest(tid=TID_A)
        req.app = MagicMock()
        req.app.state.azure = MagicMock()

        with patch.object(
            qna_app.src.pipeline.qna_pipeline,
            "generate_answer",
            new=AsyncMock(return_value={"answer": "x", "citation": {}}),
        ) as legacy:
            result = asyncio.run(qna_app.custom_rag_qna(payload, req))  # type: ignore[arg-type]
        legacy.assert_awaited_once()
        assert result == {"answer": "x", "citation": {}}
