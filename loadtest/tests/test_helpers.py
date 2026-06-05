"""Unit tests for the pure request-building / response-validation helpers.

These run in CI with no Locust runtime and no live deployment: every response
is a tiny stand-in object, every input a plain value.
"""

from __future__ import annotations

import helpers
import pytest


class FakeResponse:
    """Minimal stand-in for an HTTP response used by the validators.

    Exposes ``status_code`` and ``.json()``; ``json()`` raises ``ValueError``
    when constructed with ``raises=True`` to simulate a non-JSON body.
    """

    def __init__(self, status_code, body=None, *, raises=False):
        self.status_code = status_code
        self._body = body
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not JSON")
        return self._body


# --------------------------------------------------------------------------- #
# Header builders
# --------------------------------------------------------------------------- #
def test_bearer_header_with_token():
    assert helpers.bearer_header("abc.def.ghi") == {"Authorization": "Bearer abc.def.ghi"}


def test_bearer_header_without_token_is_empty():
    assert helpers.bearer_header(None) == {}
    assert helpers.bearer_header("") == {}


def test_admin_header_with_token():
    assert helpers.admin_header("secret-token") == {"X-Admin-Token": "secret-token"}


def test_admin_header_without_token_is_empty():
    assert helpers.admin_header(None) == {}
    assert helpers.admin_header("") == {}


# --------------------------------------------------------------------------- #
# Payload / params builders
# --------------------------------------------------------------------------- #
def test_build_qna_payload_shape():
    payload = helpers.build_qna_payload(
        "What is the policy?",
        "demo-bot",
        "read",
        session_id="sess-1",
    )
    assert payload["session_id"] == "sess-1"
    assert payload["bot_tag"] == "demo-bot"
    assert payload["fr_tag"] == "read"
    assert payload["bot"] == [{"user_query": "What is the policy?", "bot_response": None}]


def test_build_qna_payload_appends_to_history():
    history = [{"user_query": "earlier?", "bot_response": "yes"}]
    payload = helpers.build_qna_payload(
        "and now?",
        "demo-bot",
        "read",
        session_id="sess-2",
        history=history,
    )
    assert len(payload["bot"]) == 2
    assert payload["bot"][0] == {"user_query": "earlier?", "bot_response": "yes"}
    assert payload["bot"][-1] == {"user_query": "and now?", "bot_response": None}
    # history list passed in must not be mutated
    assert len(history) == 1


def test_build_admin_params():
    assert helpers.build_admin_params("tenant-x") == {"bot_tag": "tenant-x"}


def test_build_upload_params():
    params = helpers.build_upload_params("tenant-x", "/srv/docs/a.pdf", "read")
    assert params == {"bot_tag": "tenant-x", "filepath": "/srv/docs/a.pdf", "fr_mode": "read"}


# --------------------------------------------------------------------------- #
# QnA response validation
# --------------------------------------------------------------------------- #
def test_validate_qna_success():
    resp = FakeResponse(200, {"answer": "Here is the grounded answer.", "citation": {}})
    ok, reason = helpers.validate_qna_response(resp)
    assert ok is True
    assert reason is None


def test_validate_qna_non_200():
    resp = FakeResponse(500, {"answer": "x"})
    ok, reason = helpers.validate_qna_response(resp)
    assert ok is False
    assert "500" in reason


def test_validate_qna_empty_answer():
    resp = FakeResponse(200, {"answer": "   "})
    ok, reason = helpers.validate_qna_response(resp)
    assert ok is False
    assert "answer" in reason


def test_validate_qna_missing_answer():
    resp = FakeResponse(200, {"citation": {}})
    ok, reason = helpers.validate_qna_response(resp)
    assert ok is False


def test_validate_qna_non_json_body():
    resp = FakeResponse(200, raises=True)
    ok, reason = helpers.validate_qna_response(resp)
    assert ok is False
    assert "JSON" in reason


def test_validate_qna_non_object_body():
    resp = FakeResponse(200, ["not", "an", "object"])
    ok, reason = helpers.validate_qna_response(resp)
    assert ok is False


# --------------------------------------------------------------------------- #
# Admin response validation
# --------------------------------------------------------------------------- #
def test_validate_admin_list_ok():
    assert helpers.validate_admin_list_response(FakeResponse(200, [{"id": "1"}]))[0] is True
    assert helpers.validate_admin_list_response(FakeResponse(200, {"count": 3}))[0] is True


def test_validate_admin_list_non_200():
    ok, reason = helpers.validate_admin_list_response(FakeResponse(403, []))
    assert ok is False
    assert "403" in reason


def test_validate_admin_list_scalar_body():
    ok, reason = helpers.validate_admin_list_response(FakeResponse(200, 42))
    assert ok is False


def test_validate_admin_list_non_json():
    ok, reason = helpers.validate_admin_list_response(FakeResponse(200, raises=True))
    assert ok is False
    assert "JSON" in reason


# --------------------------------------------------------------------------- #
# Accepted (upload/write) response validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("status", [200, 201, 202])
def test_validate_accepted_success(status):
    ok, reason = helpers.validate_accepted_response(FakeResponse(status))
    assert ok is True
    assert reason is None


def test_validate_accepted_rate_limited_is_not_failure():
    ok, reason = helpers.validate_accepted_response(FakeResponse(429))
    assert ok is True
    assert "rate-limited" in reason


def test_validate_accepted_failure():
    ok, reason = helpers.validate_accepted_response(FakeResponse(500))
    assert ok is False
    assert "500" in reason
