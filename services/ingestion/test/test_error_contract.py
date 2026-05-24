"""Tests for the structured error contract (P0-6) — ingestion service.

Mirror of `services/qna/test/test_error_contract.py`. Verifies the same
three handlers + raise_api_error helper against the ingestion service's
copy of the error module.

Uses a minimal FastAPI app to avoid importing custom_rag (which pulls heavy
deps like PyMuPDF + langchain that aren't available in the no-PyPI CI).
"""

import pathlib
import sys
import uuid

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

# Make the per-service `errors` module importable when running pytest
# from `services/ingestion/`.
_INGESTION_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_INGESTION_ROOT) not in sys.path:
    sys.path.insert(0, str(_INGESTION_ROOT))

from errors import (  # noqa: E402
    register_exception_handlers,
    raise_api_error,
    ApiErrorCode,
)


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
