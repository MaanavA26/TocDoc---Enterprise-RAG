"""End-to-end integration tests for the QnA service.

Exercise the REAL ``services/qna/app.py:app`` in-process via ``TestClient`` with
auth (``validate_token``), startup (Key Vault + Azure clients), and retrieval/LLM
(``generate_answer``) all mocked at the service-entry seam. No live Azure, no
network.
"""

from __future__ import annotations

import pytest
from conftest import TEST_TID, bearer, qna_payload


@pytest.fixture
def token_validation_error():
    """The QnA service's TokenValidationError class (imported lazily)."""
    from src.core.token_validator import TokenValidationError

    return TokenValidationError


# --------------------------------------------------------------------------- #
# /health
# --------------------------------------------------------------------------- #
def test_health_ok(qna_client):
    """/health is public (no auth) and reports the pipeline module is loaded."""
    r = qna_client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["qna_module"] == "loaded"


# --------------------------------------------------------------------------- #
# Happy path
# --------------------------------------------------------------------------- #
def test_qna_happy_path_returns_answer_and_citations(qna_client, monkeypatch):
    """Authed POST /qna returns the mocked answer + citation map verbatim.

    Tenant binding is default-ON; map the token's tid to the requested bot_tag so
    the request is permitted and reaches the (mocked) pipeline.
    """
    monkeypatch.setenv("QNA_TENANT_BOT_TAG_MAP", f'{{"{TEST_TID}": ["workspace-a"]}}')

    r = qna_client.post("/qna", json=qna_payload(bot_tag="workspace-a"), headers=bearer())
    assert r.status_code == 200
    body = r.json()
    assert body["answer"] == "The retention period is seven years."
    assert body["citation"] == {"policy.md": "/docs/policy.md"}
    # Success path stays byte-identical to {answer, citation}: no null extras.
    assert "request_id" not in body
    assert "error" not in body
    # Correlation header present on success too.
    assert r.headers.get("X-Request-ID")


def test_qna_happy_path_with_binding_disabled(qna_client, monkeypatch):
    """With tenant binding explicitly OFF, any bot_tag is accepted."""
    monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "false")

    r = qna_client.post("/qna", json=qna_payload(bot_tag="any-workspace"), headers=bearer())
    assert r.status_code == 200
    assert r.json()["answer"]


# --------------------------------------------------------------------------- #
# 401 — unauthenticated
# --------------------------------------------------------------------------- #
def test_qna_missing_auth_header_401(qna_client):
    """No Authorization header → 401 envelope from the auth middleware."""
    r = qna_client.post("/qna", json=qna_payload())
    assert r.status_code == 401
    err = r.json()["error"]
    assert err["code"] == "UNAUTHORIZED"
    assert r.headers.get("X-Request-ID")
    assert err["request_id"] == r.headers["X-Request-ID"]


def test_qna_invalid_token_401(qna_client, qna_token_state, token_validation_error):
    """A token the validator rejects → 401 envelope, generic safe message."""
    qna_token_state["error"] = token_validation_error("Invalid token", status_code=401)

    r = qna_client.post("/qna", json=qna_payload(), headers=bearer("bogus"))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


# --------------------------------------------------------------------------- #
# 403 — tenant binding fails closed
# --------------------------------------------------------------------------- #
def test_qna_tenant_binding_fail_closed_403(qna_client, monkeypatch):
    """Authenticated, but the requested bot_tag is not allowed for the tid → 403.

    The pipeline must NOT be reached (fail closed before retrieval). The generic
    message must not echo the bot_tag, tid, or allowlist contents.
    """
    monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "true")
    monkeypatch.setenv("QNA_TENANT_BOT_TAG_MAP", f'{{"{TEST_TID}": ["workspace-a"]}}')

    r = qna_client.post("/qna", json=qna_payload(bot_tag="workspace-z"), headers=bearer())
    assert r.status_code == 403
    err = r.json()["error"]
    assert err["code"] == "UNAUTHORIZED"
    assert "workspace-z" not in err["message"]
    assert TEST_TID not in err["message"]
    assert r.headers.get("X-Request-ID")


def test_qna_tenant_binding_missing_map_fail_closed_403(qna_client, monkeypatch):
    """Enforcement ON but no map configured → fail closed (403)."""
    monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "true")
    monkeypatch.delenv("QNA_TENANT_BOT_TAG_MAP", raising=False)

    r = qna_client.post("/qna", json=qna_payload(), headers=bearer())
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


# --------------------------------------------------------------------------- #
# 400 — bad fr_tag (precedes tenant binding in the handler)
# --------------------------------------------------------------------------- #
def test_qna_bad_fr_tag_400(qna_client, monkeypatch):
    """An fr_tag outside the allow-list → 400 INVALID_REQUEST."""
    monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "false")

    r = qna_client.post("/qna", json=qna_payload(fr_tag="not-a-mode"), headers=bearer())
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "INVALID_REQUEST"
    # The message names the allowed modes (a useful, non-sensitive 400).
    assert "read" in err["message"] and "layout" in err["message"]


def test_qna_empty_bot_list_400(qna_client, monkeypatch):
    """An empty conversation list → 400 INVALID_REQUEST."""
    monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "false")

    payload = qna_payload()
    payload["bot"] = []
    r = qna_client.post("/qna", json=payload, headers=bearer())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


# --------------------------------------------------------------------------- #
# 422 — schema validation
# --------------------------------------------------------------------------- #
def test_qna_malformed_body_422(qna_client):
    """A body missing required fields → 422 VALIDATION_ERROR envelope."""
    r = qna_client.post("/qna", json={"bot_tag": "x"}, headers=bearer())
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    # Structured per-field errors are present; raw user input is NOT echoed.
    assert isinstance(err.get("errors"), list) and err["errors"]


# --------------------------------------------------------------------------- #
# Cross-cutting: generic 500 does not leak exception text
# --------------------------------------------------------------------------- #
def test_qna_pipeline_exception_does_not_leak(qna_client, qna_pipeline_result, monkeypatch):
    """A pipeline failure becomes a generic 500 envelope — no exception text.

    The mocked pipeline raises with a secret-looking message; the response body
    must contain only the generic "Internal server error" and carry an
    X-Request-ID.
    """
    monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "false")

    import src.pipeline.qna_pipeline as pipeline

    secret = "AZURE_SEARCH_KEY=super-secret-leak"

    async def _boom(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(pipeline, "generate_answer", _boom)

    r = qna_client.post("/qna", json=qna_payload(), headers=bearer())
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["code"] == "INTERNAL_ERROR"
    assert err["message"] == "Internal server error"
    assert secret not in r.text
    assert "RuntimeError" not in r.text
    assert r.headers.get("X-Request-ID")
    assert err["request_id"] == r.headers["X-Request-ID"]
