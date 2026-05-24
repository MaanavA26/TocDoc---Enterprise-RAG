"""Tests for the structured error contract (P0-6) — ingestion service.

Sibling of `services/qna/test/test_error_contract.py`. Verifies the same
three handlers + raise_api_error helper against the ingestion service's
copy of the error module. Adds an integration test suite that mounts the
real `RequestIDMiddleware` + `limit_upload_size` middleware + handlers
to prove the full middleware stack produces enveloped responses.

Uses minimal FastAPI apps — avoids importing `app.py` (which pulls heavy
deps like PyMuPDF + langchain via `custom_rag`). The upload-size middleware
is imported directly from `middleware.py`, which is a standalone module
specifically for this reason.
"""

import pathlib
import sys
import uuid

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

# Make the per-service `errors` and `middleware` modules importable when
# running pytest from `services/ingestion/`.
_INGESTION_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_INGESTION_ROOT) not in sys.path:
    sys.path.insert(0, str(_INGESTION_ROOT))

from errors import (  # noqa: E402
    register_exception_handlers,
    raise_api_error,
    ApiErrorCode,
)
from middleware import limit_upload_size, MAX_UPLOAD_BYTES  # noqa: E402
from observability import RequestIDMiddleware  # noqa: E402


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
        raise HTTPException(status_code=401, detail="Invalid admin token")

    @a.get("/string-detail-404")
    def string_detail_404():
        raise HTTPException(status_code=404, detail="Document not found")

    @a.get("/string-detail-413")
    def string_detail_413():
        raise HTTPException(status_code=413, detail="File too large")

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
    return TestClient(app, raise_server_exceptions=False)


class TestHttpExceptionHandling:

    def test_string_detail_400_envelope(self, client: TestClient):
        r = client.get("/string-detail-400")
        assert r.status_code == 400
        body = r.json()
        assert body["error"]["code"] == ApiErrorCode.INVALID_REQUEST
        assert body["error"]["message"] == "bot_tag cannot be empty"
        assert body["error"]["request_id"] == r.headers["X-Request-ID"]

    def test_string_detail_401_maps_to_unauthorized(self, client: TestClient):
        r = client.get("/string-detail-401")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == ApiErrorCode.UNAUTHORIZED

    def test_string_detail_404_maps_to_not_found(self, client: TestClient):
        r = client.get("/string-detail-404")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == ApiErrorCode.NOT_FOUND

    def test_string_detail_413_maps_to_invalid_request(self, client: TestClient):
        """Payload-too-large (413) was the only ingestion-specific status
        the qna copy doesn't need to test. Verify mapping."""
        r = client.get("/string-detail-413")
        assert r.status_code == 413
        assert r.json()["error"]["code"] == ApiErrorCode.INVALID_REQUEST

    def test_string_detail_503_maps_to_upstream(self, client: TestClient):
        r = client.get("/string-detail-503")
        assert r.status_code == 503
        assert r.json()["error"]["code"] == ApiErrorCode.UPSTREAM_UNAVAILABLE

    def test_dict_detail_uses_explicit_code(self, client: TestClient):
        r = client.get("/dict-detail")
        assert r.status_code == 503
        body = r.json()
        assert body["error"]["code"] == ApiErrorCode.UPSTREAM_UNAVAILABLE
        assert body["error"]["message"] == "Search index down"


class TestUnhandledException:

    def test_returns_500_envelope(self, client: TestClient):
        r = client.get("/unhandled")
        assert r.status_code == 500
        body = r.json()
        assert body["error"]["code"] == ApiErrorCode.INTERNAL_ERROR
        assert body["error"]["message"] == "Internal server error"

    def test_no_exception_text_in_response(self, client: TestClient):
        r = client.get("/unhandled")
        assert "simulated handler failure" not in r.text
        assert "sensitive details xyz123" not in r.text
        assert "RuntimeError" not in r.text

    def test_x_request_id_in_body_and_header_match(self, client: TestClient):
        r = client.get("/unhandled")
        body = r.json()
        assert body["error"]["request_id"]
        assert "X-Request-ID" in r.headers
        assert body["error"]["request_id"] == r.headers["X-Request-ID"]
        parsed = uuid.UUID(body["error"]["request_id"])
        assert parsed.version == 4


class TestValidationError:

    def test_returns_422_envelope_with_errors_list(self, client: TestClient):
        r = client.post("/validate", json={"name": "x"})
        assert r.status_code == 422
        body = r.json()
        assert body["error"]["code"] == ApiErrorCode.VALIDATION_ERROR
        assert body["error"]["message"] == "Request validation failed"
        assert isinstance(body["error"]["errors"], list)
        assert len(body["error"]["errors"]) > 0
        err0 = body["error"]["errors"][0]
        assert set(err0.keys()) == {"loc", "type", "msg"}
        assert isinstance(err0["loc"], list)

    def test_errors_field_absent_on_non_validation_responses(
        self, client: TestClient
    ):
        r = client.get("/string-detail-400")
        assert "errors" not in r.json()["error"]


class TestRequestIdPropagation:

    def test_uses_preset_state_request_id(self, client: TestClient):
        r = client.get("/preset-request-id")
        body = r.json()
        assert body["error"]["request_id"] == "my-test-id-001"
        assert r.headers["X-Request-ID"] == "my-test-id-001"

    def test_auto_generates_uuid4_when_state_missing(self, client: TestClient):
        r = client.get("/string-detail-400")
        rid = r.json()["error"]["request_id"]
        assert uuid.UUID(rid).version == 4
        assert r.headers["X-Request-ID"] == rid


class TestRaiseApiError:

    def test_raises_http_exception_with_dict_detail(self):
        with pytest.raises(HTTPException) as excinfo:
            raise_api_error("CUSTOM_CODE", "the message", 418)
        assert excinfo.value.status_code == 418
        assert excinfo.value.detail == {"code": "CUSTOM_CODE", "message": "the message"}


class TestApiErrorCodeStability:

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
# without RequestIDMiddleware or upload-size middleware. These integration
# tests mount the actual middleware to verify:
#   1. RequestIDMiddleware sets request.state.request_id BEFORE the
#      exception handlers run, so handler responses carry the same ID in
#      body and header.
#   2. limit_upload_size middleware uses build_error_response (NOT
#      `raise HTTPException`) — its 413 response is envelope-shaped.
#   3. Client-supplied X-Request-ID propagates through every error path.

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
        assert rid_header == rid_body
        assert uuid.UUID(rid_header).version == 4

    def test_client_supplied_request_id_flows_to_500(
        self, client: TestClient,
    ):
        r = client.get("/boom", headers={"X-Request-ID": "client-supplied-001"})
        assert r.status_code == 500
        assert r.headers["X-Request-ID"] == "client-supplied-001"
        assert r.json()["error"]["request_id"] == "client-supplied-001"

    def test_client_supplied_request_id_flows_to_400(
        self, client: TestClient,
    ):
        r = client.get("/bad", headers={"X-Request-ID": "client-supplied-002"})
        assert r.status_code == 400
        assert r.headers["X-Request-ID"] == "client-supplied-002"
        assert r.json()["error"]["request_id"] == "client-supplied-002"


class TestUploadSizeMiddlewareEnvelope:
    """`limit_upload_size` middleware must return the envelope shape for
    oversized requests (architect blocker #2 on PR #10) and NOT bypass
    the contract by raising HTTPException."""

    @pytest.fixture
    def app(self) -> FastAPI:
        a = FastAPI()
        register_exception_handlers(a)
        a.middleware("http")(limit_upload_size)
        a.add_middleware(RequestIDMiddleware)

        @a.post("/upload-mock")
        def upload_mock():
            # If middleware lets the request through, this handler runs and
            # confirms it. We never want the test to reach here for oversize.
            return {"ok": True}

        return a

    @pytest.fixture
    def client(self, app: FastAPI) -> TestClient:
        return TestClient(app, raise_server_exceptions=False)

    def test_oversized_content_length_returns_413_envelope(
        self, client: TestClient,
    ):
        # Send a content-length one byte over the limit. We use a 1-byte body
        # but lie about content-length so the middleware fires on the header
        # check before reading any body.
        oversized = MAX_UPLOAD_BYTES + 1
        r = client.post(
            "/upload-mock",
            content=b"x",
            headers={"content-length": str(oversized)},
        )
        assert r.status_code == 413
        body = r.json()
        # CRITICAL: envelope shape, NOT a Starlette default 500 or a
        # `{"detail": "..."}` from a raised-but-not-handled HTTPException.
        assert "error" in body
        assert "detail" not in body
        assert body["error"]["code"] == ApiErrorCode.INVALID_REQUEST
        assert "File too large" in body["error"]["message"]
        # X-Request-ID matches in body + header
        assert body["error"]["request_id"] == r.headers["X-Request-ID"]

    def test_oversized_with_client_supplied_request_id_propagates(
        self, client: TestClient,
    ):
        oversized = MAX_UPLOAD_BYTES + 1
        r = client.post(
            "/upload-mock",
            content=b"x",
            headers={
                "content-length": str(oversized),
                "X-Request-ID": "trace-oversize-001",
            },
        )
        assert r.status_code == 413
        assert r.headers["X-Request-ID"] == "trace-oversize-001"
        assert r.json()["error"]["request_id"] == "trace-oversize-001"

    def test_normal_size_passes_through(self, client: TestClient):
        """A request within the limit reaches the handler."""
        r = client.post(
            "/upload-mock",
            content=b"x",
            headers={"content-length": "1"},
        )
        assert r.status_code == 200
        # X-Request-ID still set by RequestIDMiddleware on the 200.
        assert "X-Request-ID" in r.headers

    def test_malformed_content_length_passes_through(self, client: TestClient):
        """A non-numeric content-length is ignored defensively."""
        r = client.post(
            "/upload-mock",
            content=b"x",
            headers={"content-length": "not-a-number"},
        )
        # No 413, no 500 — middleware silently passed through.
        assert r.status_code == 200
