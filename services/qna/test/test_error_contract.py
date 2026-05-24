"""Tests for the structured error contract (P0-6).

Verifies the three exception handlers (HTTPException, RequestValidationError,
generic Exception), the `raise_api_error` helper, and the integration
contract between `RequestIDMiddleware`, `AuthUtils.auth_middleware`, and the
exception handlers.

Uses minimal FastAPI apps — no service startup, no Azure clients. The
unit-level tests omit the middleware stack to exercise the handler's
auto-UUID fallback. The integration tests at the bottom mount real
middleware to prove the full stack produces enveloped responses.
"""

import os
import pathlib
import sys
import uuid

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Env setup — required BEFORE any `src.core.auth` import, because the
# config module enforces a fixed env-var contract at import time. Values
# are deliberately fake; no Azure call is ever attempted in these tests.
# ---------------------------------------------------------------------------
os.environ.setdefault("AzureOpenaiAccountEndpoint", "https://fake.openai.example.com")
os.environ.setdefault("TocdocOpenAIKey", "fake-openai-key")
os.environ.setdefault("AzureOpenaiApiVersion", "2024-02-01")
os.environ.setdefault("AZURE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
os.environ.setdefault("AzureSearchEndpoint", "https://fake.search.example.com")
os.environ.setdefault("AzureSearchKey", "fake-search-key")
os.environ.setdefault("INDEX_NAME", "fake-index")
os.environ.setdefault("AZURE_KEY_VAULT", "fakevault")
os.environ.setdefault("TocdocSPTenantID", "11111111-1111-1111-1111-111111111111")
os.environ.setdefault("AUDIENCE_ID", "api://fake-audience")

# Make src/ importable when running pytest from services/qna/
_QNA_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_QNA_ROOT) not in sys.path:
    sys.path.insert(0, str(_QNA_ROOT))

from src.core.errors import (  # noqa: E402
    register_exception_handlers,
    raise_api_error,
    ApiErrorCode,
)
from src.core.observability import RequestIDMiddleware  # noqa: E402
from src.core.auth import AuthUtils  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _Item(BaseModel):
    name: str
    count: int


@pytest.fixture
def app() -> FastAPI:
    a = FastAPI()
    register_exception_handlers(a)

    @a.get("/string-detail-400")
    def string_detail_400():
        raise HTTPException(status_code=400, detail="bot_tag cannot be empty")

    @a.get("/string-detail-401")
    def string_detail_401():
        raise HTTPException(status_code=401, detail="Invalid token")

    @a.get("/string-detail-404")
    def string_detail_404():
        raise HTTPException(status_code=404, detail="Document not found")

    @a.get("/string-detail-503")
    def string_detail_503():
        raise HTTPException(status_code=503, detail="Search backend down")

    @a.get("/dict-detail")
    def dict_detail():
        raise_api_error(ApiErrorCode.UPSTREAM_UNAVAILABLE, "Search index down", 503)

    @a.get("/unhandled")
    def unhandled():
        raise RuntimeError("simulated handler failure with sensitive details xyz123")

    @a.post("/validate")
    def validate(item: _Item):
        return {"ok": True}

    @a.get("/preset-request-id")
    def preset_request_id(request: Request):
        request.state.request_id = "my-test-id-001"
        raise HTTPException(status_code=400, detail="bad")

    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    # raise_server_exceptions=False so the 500 from /unhandled becomes a
    # response we can assert against instead of crashing the test runner.
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# HTTPException handling — back-compat (string detail) + new (dict detail)
# ---------------------------------------------------------------------------

class TestHttpExceptionHandling:
    """Back-compat string-detail callsites still produce the envelope."""

    def test_string_detail_400_envelope(self, client: TestClient):
        r = client.get("/string-detail-400")
        assert r.status_code == 400
        body = r.json()
        assert body == {
            "error": {
                "code": "INVALID_REQUEST",
                "message": "bot_tag cannot be empty",
                "request_id": body["error"]["request_id"],
            }
        }
        # request_id auto-generated (no middleware set it) and matches header
        assert body["error"]["request_id"] == r.headers["X-Request-ID"]

    def test_string_detail_401_maps_to_unauthorized(self, client: TestClient):
        r = client.get("/string-detail-401")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "UNAUTHORIZED"

    def test_string_detail_404_maps_to_not_found(self, client: TestClient):
        r = client.get("/string-detail-404")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"

    def test_string_detail_503_maps_to_upstream(self, client: TestClient):
        r = client.get("/string-detail-503")
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"

    def test_dict_detail_uses_explicit_code(self, client: TestClient):
        r = client.get("/dict-detail")
        assert r.status_code == 503
        body = r.json()
        # Note: even though the status default for 503 is UPSTREAM_UNAVAILABLE,
        # we picked the same code here on purpose — this test asserts that the
        # dict-detail path uses the EXPLICIT code, not the status default.
        assert body["error"]["code"] == ApiErrorCode.UPSTREAM_UNAVAILABLE
        assert body["error"]["message"] == "Search index down"


# ---------------------------------------------------------------------------
# Unhandled exception → 500 envelope WITH X-Request-ID — closes PR #8 debt
# ---------------------------------------------------------------------------

class TestUnhandledException:

    def test_returns_500_envelope(self, client: TestClient):
        r = client.get("/unhandled")
        assert r.status_code == 500
        body = r.json()
        assert body["error"]["code"] == ApiErrorCode.INTERNAL_ERROR
        assert body["error"]["message"] == "Internal server error"

    def test_no_exception_text_in_response(self, client: TestClient):
        """The raw exception message and any embedded sensitive details
        MUST NOT appear anywhere in the response body or headers."""
        r = client.get("/unhandled")
        assert "simulated handler failure" not in r.text
        assert "sensitive details xyz123" not in r.text
        assert "RuntimeError" not in r.text

    def test_x_request_id_present_in_body_and_header(self, client: TestClient):
        """This is the gap closed by P0-6 — unhandled-exception 5xx now
        carries X-Request-ID in BOTH the body and the response header,
        and the two values match."""
        r = client.get("/unhandled")
        body = r.json()
        assert body["error"]["request_id"]  # present
        assert "X-Request-ID" in r.headers
        assert body["error"]["request_id"] == r.headers["X-Request-ID"]
        # The auto-generated id is a UUID4
        parsed = uuid.UUID(body["error"]["request_id"])
        assert parsed.version == 4


# ---------------------------------------------------------------------------
# RequestValidationError → 422 envelope with structured `errors` list
# ---------------------------------------------------------------------------

class TestValidationError:

    def test_returns_422_envelope_with_errors_list(self, client: TestClient):
        r = client.post("/validate", json={"name": "x"})  # missing 'count'
        assert r.status_code == 422
        body = r.json()
        assert body["error"]["code"] == ApiErrorCode.VALIDATION_ERROR
        assert body["error"]["message"] == "Request validation failed"
        assert isinstance(body["error"]["errors"], list)
        assert len(body["error"]["errors"]) > 0
        err0 = body["error"]["errors"][0]
        assert set(err0.keys()) == {"loc", "type", "msg"}
        # Verify `loc` is a list (FastAPI's structured field location).
        assert isinstance(err0["loc"], list)

    def test_errors_field_absent_on_non_validation_responses(
        self, client: TestClient
    ):
        """`errors` is exclude_none in the response — only validation 422s carry it."""
        r = client.get("/string-detail-400")
        body = r.json()
        assert "errors" not in body["error"]


# ---------------------------------------------------------------------------
# X-Request-ID propagation
# ---------------------------------------------------------------------------

class TestRequestIdPropagation:

    def test_uses_preset_state_request_id(self, client: TestClient):
        """When `request.state.request_id` is set (in production, by
        RequestIDMiddleware), the handler reuses it for both the body
        and the header."""
        r = client.get("/preset-request-id")
        body = r.json()
        assert body["error"]["request_id"] == "my-test-id-001"
        assert r.headers["X-Request-ID"] == "my-test-id-001"

    def test_auto_generates_uuid4_when_state_missing(self, client: TestClient):
        """No middleware set request_id → handler generates UUID4."""
        r = client.get("/string-detail-400")
        rid = r.json()["error"]["request_id"]
        assert uuid.UUID(rid).version == 4
        assert r.headers["X-Request-ID"] == rid


# ---------------------------------------------------------------------------
# raise_api_error helper
# ---------------------------------------------------------------------------

class TestRaiseApiError:

    def test_raises_http_exception_with_dict_detail(self):
        with pytest.raises(HTTPException) as excinfo:
            raise_api_error("CUSTOM_CODE", "the message", 418)
        assert excinfo.value.status_code == 418
        assert excinfo.value.detail == {"code": "CUSTOM_CODE", "message": "the message"}

    def test_preserves_headers_arg(self):
        with pytest.raises(HTTPException) as excinfo:
            raise_api_error(
                "UNAUTHORIZED", "bad", 401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        assert excinfo.value.headers == {"WWW-Authenticate": "Bearer"}


# ---------------------------------------------------------------------------
# ApiErrorCode stability — codes are part of the public API contract
# ---------------------------------------------------------------------------

class TestApiErrorCodeStability:
    """Guard against accidental renames of the public code values."""

    def test_codes_unchanged(self):
        assert ApiErrorCode.INVALID_REQUEST == "INVALID_REQUEST"
        assert ApiErrorCode.UNAUTHORIZED == "UNAUTHORIZED"
        assert ApiErrorCode.NOT_FOUND == "NOT_FOUND"
        assert ApiErrorCode.VALIDATION_ERROR == "VALIDATION_ERROR"
        assert ApiErrorCode.UPSTREAM_UNAVAILABLE == "UPSTREAM_UNAVAILABLE"
        assert ApiErrorCode.INTERNAL_ERROR == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Integration tests — real middleware stack
# ---------------------------------------------------------------------------
# Per architect review on PR #10: the unit tests above use a minimal app
# without RequestIDMiddleware or auth. These integration tests mount the
# actual middleware classes to verify:
#   1. RequestIDMiddleware sets request.state.request_id BEFORE the
#      exception handlers run, so handler responses carry the same ID in
#      body and header.
#   2. Auth middleware uses build_error_response (not legacy JSONResponse
#      with {"detail": ...}) — its responses are envelope-shaped.
#   3. Client-supplied X-Request-ID propagates end-to-end through every
#      error path.

class TestRequestIdMiddlewareIntegration:
    """RequestIDMiddleware + register_exception_handlers — full stack."""

    @pytest.fixture
    def app(self) -> FastAPI:
        a = FastAPI()
        register_exception_handlers(a)
        a.add_middleware(RequestIDMiddleware)

        @a.get("/boom")
        def boom():
            raise RuntimeError("simulated unhandled failure")

        @a.get("/bad")
        def bad():
            raise HTTPException(status_code=400, detail="bot_tag cannot be empty")

        return a

    @pytest.fixture
    def client(self, app: FastAPI) -> TestClient:
        return TestClient(app, raise_server_exceptions=False)

    def test_unhandled_500_carries_request_id_from_middleware(
        self, client: TestClient,
    ):
        r = client.get("/boom")
        assert r.status_code == 500
        rid_header = r.headers["X-Request-ID"]
        rid_body = r.json()["error"]["request_id"]
        # The middleware set request.state.request_id; the handler resolved
        # the same value for both the header and the body.
        assert rid_header == rid_body
        # Server-generated → UUID4
        assert uuid.UUID(rid_header).version == 4

    def test_client_supplied_request_id_flows_to_500(self, client: TestClient):
        r = client.get("/boom", headers={"X-Request-ID": "client-supplied-001"})
        assert r.status_code == 500
        assert r.headers["X-Request-ID"] == "client-supplied-001"
        assert r.json()["error"]["request_id"] == "client-supplied-001"

    def test_client_supplied_request_id_flows_to_400(self, client: TestClient):
        r = client.get("/bad", headers={"X-Request-ID": "client-supplied-002"})
        assert r.status_code == 400
        assert r.headers["X-Request-ID"] == "client-supplied-002"
        assert r.json()["error"]["request_id"] == "client-supplied-002"


class TestAuthMiddlewareEnvelope:
    """`AuthUtils.auth_middleware` must use the envelope shape for every
    failure path (architect blocker #1 on PR #10).

    `validate_token` is never invoked in these tests because each case
    fails before the JWKS path. No network call is made.
    """

    @pytest.fixture
    def app(self) -> FastAPI:
        a = FastAPI()
        register_exception_handlers(a)

        @a.middleware("http")
        async def auth_mw(request: Request, call_next):
            return await AuthUtils.auth_middleware(request, call_next)

        a.add_middleware(RequestIDMiddleware)

        @a.get("/protected")
        def protected():
            return {"ok": True}

        return a

    @pytest.fixture
    def client(self, app: FastAPI) -> TestClient:
        return TestClient(app, raise_server_exceptions=False)

    def test_missing_authorization_returns_envelope_401(
        self, client: TestClient,
    ):
        r = client.get("/protected")
        assert r.status_code == 401
        body = r.json()
        # CRITICAL: envelope shape, NOT the legacy `{"detail": "..."}`.
        assert "error" in body
        assert "detail" not in body
        assert body["error"]["code"] == ApiErrorCode.UNAUTHORIZED
        assert "Missing or invalid Authorization header" in body["error"]["message"]
        # X-Request-ID matches in body + header
        assert body["error"]["request_id"] == r.headers["X-Request-ID"]

    def test_malformed_authorization_returns_envelope_401(
        self, client: TestClient,
    ):
        # "Basic" header instead of "Bearer" — same 401 envelope path.
        r = client.get("/protected", headers={"Authorization": "Basic abc123"})
        assert r.status_code == 401
        body = r.json()
        assert body["error"]["code"] == ApiErrorCode.UNAUTHORIZED
        assert "detail" not in body

    def test_client_supplied_request_id_propagates_to_auth_failure(
        self, client: TestClient,
    ):
        r = client.get(
            "/protected",
            headers={"X-Request-ID": "trace-auth-fail-001"},
        )
        assert r.status_code == 401
        body = r.json()
        assert body["error"]["request_id"] == "trace-auth-fail-001"
        assert r.headers["X-Request-ID"] == "trace-auth-fail-001"

    def test_health_path_bypasses_auth(self, client: TestClient, app: FastAPI):
        @app.get("/qna/health")
        def health():
            return {"status": "ok"}

        # No auth header — should pass through the middleware's bypass path.
        r = client.get("/qna/health")
        assert r.status_code == 200
        # Even on success, X-Request-ID is set by RequestIDMiddleware.
        assert "X-Request-ID" in r.headers
