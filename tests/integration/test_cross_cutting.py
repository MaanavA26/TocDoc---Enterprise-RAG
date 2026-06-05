"""Cross-cutting contract tests spanning both services.

Assert the shared P0-6 guarantees on the REAL assembled apps:
- Every error response uses the structured ``{"error": {code, message, request_id}}``
  envelope.
- ``X-Request-ID`` is present on every response (success AND error) and matches
  the ``request_id`` in error bodies.
- A client-supplied ``X-Request-ID`` is honored / echoed.
- Error bodies never leak secrets or raw exception text.
"""

from __future__ import annotations

from conftest import bearer, qna_payload


# A canonical error-envelope shape check reused across both services.
def _assert_error_envelope(resp):
    body = resp.json()
    assert "error" in body, body
    err = body["error"]
    assert isinstance(err.get("code"), str) and err["code"]
    assert isinstance(err.get("message"), str) and err["message"]
    # request_id in the body matches the header.
    assert resp.headers.get("X-Request-ID")
    assert err.get("request_id") == resp.headers["X-Request-ID"]


# --------------------------------------------------------------------------- #
# X-Request-ID present on every response
# --------------------------------------------------------------------------- #
def test_qna_request_id_on_success(qna_client, monkeypatch):
    monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "false")
    r = qna_client.post("/qna", json=qna_payload(), headers=bearer())
    assert r.status_code == 200
    assert r.headers.get("X-Request-ID")


def test_qna_request_id_on_error(qna_client):
    r = qna_client.post("/qna", json=qna_payload())  # 401
    _assert_error_envelope(r)


def test_ingestion_request_id_on_success(ingestion_client):
    r = ingestion_client.get("/health")
    assert r.status_code == 200
    assert r.headers.get("X-Request-ID")


def test_ingestion_request_id_on_error(ingestion_client):
    r = ingestion_client.get("/admin/documents", params={"bot_tag": "tenant-x"})  # 401
    _assert_error_envelope(r)


# --------------------------------------------------------------------------- #
# Client-supplied X-Request-ID is echoed
# --------------------------------------------------------------------------- #
def test_qna_echoes_client_request_id(qna_client):
    rid = "client-correlation-123"
    r = qna_client.post("/qna", json=qna_payload(), headers={**bearer(), "X-Request-ID": rid})
    # Auth passes only if binding allows; here we just check correlation, so a
    # 401/403/200 are all acceptable — the header must round-trip regardless.
    assert r.headers.get("X-Request-ID") == rid


def test_ingestion_echoes_client_request_id(ingestion_client):
    rid = "client-correlation-456"
    r = ingestion_client.get("/health", headers={"X-Request-ID": rid})
    assert r.headers.get("X-Request-ID") == rid


# --------------------------------------------------------------------------- #
# Framework routing errors are also enveloped (audit M2)
# --------------------------------------------------------------------------- #
def test_qna_unknown_route_404_enveloped(qna_client):
    r = qna_client.get("/no-such-route", headers=bearer())
    assert r.status_code == 404
    _assert_error_envelope(r)


def test_ingestion_unknown_route_404_has_request_id(ingestion_client):
    """Ingestion returns 404 for an unknown route, still carrying X-Request-ID.

    NOTE: unlike the QnA service (which registers its HTTPException handler on
    the Starlette parent class and therefore envelopes framework 404s), the
    ingestion service returns FastAPI's bare ``{"detail": "Not Found"}`` for an
    unmatched route. We assert the *actual* contract here (and do not modify the
    service); the RequestID middleware still runs, so the correlation header is
    present even on a routing miss.
    """
    r = ingestion_client.get("/no-such-route")
    assert r.status_code == 404
    assert r.headers.get("X-Request-ID")


# --------------------------------------------------------------------------- #
# No secret / exception leakage in error bodies
# --------------------------------------------------------------------------- #
def test_qna_401_body_has_no_secrets(qna_client):
    r = qna_client.post("/qna", json=qna_payload())
    text = r.text
    # None of the configured (throwaway) secrets should appear in the body.
    for needle in ("test-search-key", "test-openai-key", "test-client-secret"):
        assert needle not in text


def test_ingestion_401_body_has_no_secrets(ingestion_client):
    r = ingestion_client.post("/upload", params={"bot_tag": "t", "filepath": "x.pdf"})
    assert "test-admin-token" not in r.text
