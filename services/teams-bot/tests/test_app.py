"""Inbound-auth tests for the ``/api/messages`` aiohttp host (mocked adapter).

These exercise the *trust boundary* without live Microsoft keys. A fake adapter
stands in at the auth-verification seam (the same boundary the real
``BotFrameworkAdapter.process_activity`` enforces):

- When authentication fails, the adapter raises ``PermissionError`` — exactly
  what ``BotFrameworkAdapter._authenticate_request`` raises for an absent token
  or a not-authenticated claim (verified against the installed
  botframework-connector 4.17.1 source; the not-authenticated / audience /
  issuer / identity paths all raise ``PermissionError``). These tests assert
  the *host* maps that verdict to **401** and that the bot never runs (no QnA
  call) — for a missing header and a malformed ``Bearer`` header. The live
  signature/issuer/audience check itself needs Microsoft keys + an OpenID
  metadata fetch and is a deploy-time concern (see the README).
- A valid (mocked) auth header lets the adapter invoke ``logic``; the activity
  is processed, the server-derived ``bot_tag`` reaches the QnA call, and a 200
  is returned.

The production validation is NOT weakened: ``create_app`` still builds a real
``BotFrameworkAdapter`` when no adapter is injected; tests mock only at the
adapter boundary (and inject a recording QnA client factory so no network call
is made).
"""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from aiohttp.test_utils import TestClient, TestServer
from botbuilder.core import TurnContext
from teams_bot.app import create_app
from teams_bot.config import AdapterConfig

from tests.conftest import FakeQnAClient, FakeTokenProvider

TENANT_A = "11111111-1111-1111-1111-111111111111"

_VALID_ACTIVITY = {
    "type": "message",
    "text": "What is the refund window?",
    "channelData": {"tenant": {"id": TENANT_A}},
    "conversation": {"id": "conv-1"},
    "serviceUrl": "https://smba.example/teams",
}


class FakeAdapter:
    """Stands in for ``BotFrameworkAdapter`` at the inbound-auth boundary.

    Mirrors ``process_activity`` semantics: it authenticates the activity from
    the ``auth_header`` and only then invokes ``logic`` (inside a real
    ``TurnContext`` so the bot's ``on_turn`` runs unchanged). ``authenticate``
    decides accept/reject so a test can assert that the bot runs only on a
    verified activity.
    """

    def __init__(self, *, authenticate: bool) -> None:
        self._authenticate = authenticate
        self.on_turn_error: Any = None
        self.logic_invocations = 0

    async def process_activity(self, activity: Any, auth_header: str, logic: Any) -> None:
        # The real adapter raises PermissionError BEFORE running logic when the
        # JWT is missing/invalid. Model exactly that.
        if not self._authenticate:
            raise PermissionError("Unauthorized Access. Request is not authorized")
        context = TurnContext(self, activity)
        self.logic_invocations += 1
        await logic(context)
        return None

    async def send_activities(self, context: Any, activities: Any) -> list:  # noqa: ARG002
        # The bot replies with a card on the happy path; record nothing, just
        # satisfy the TurnContext send path.
        return [type("ResourceResponse", (), {"id": "1"})() for _ in activities]


def _build(*, authenticate: bool, qna: FakeQnAClient) -> tuple[Any, FakeAdapter]:
    config = AdapterConfig(
        azure_tenant_id=TENANT_A,
        audience_id="api-aud",
        qna_base_url="https://qna.internal",
        fr_tag="read",
        tenant_bot_tag_map={TENANT_A: "client_a_hr"},
    )
    adapter = FakeAdapter(authenticate=authenticate)
    app = create_app(
        config=config,
        token_provider=FakeTokenProvider(),
        app_id="bot-app-id",
        app_password="bot-app-password",
        adapter=adapter,
        client_factory=lambda _token: qna,  # recording fake; no network
    )
    return app, adapter


async def test_missing_auth_header_returns_401_and_no_qna_call():
    qna = FakeQnAClient()
    app, adapter = _build(authenticate=False, qna=qna)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/messages", json=_VALID_ACTIVITY)  # no Authorization header
    assert resp.status == HTTPStatus.UNAUTHORIZED
    assert adapter.logic_invocations == 0  # bot never ran
    assert qna.calls == []  # no QnA call on an unauthenticated activity


async def test_invalid_auth_header_returns_401():
    qna = FakeQnAClient()
    app, adapter = _build(authenticate=False, qna=qna)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/messages",
            json=_VALID_ACTIVITY,
            headers={"Authorization": "Bearer forged.invalid.token"},
        )
    assert resp.status == HTTPStatus.UNAUTHORIZED
    assert adapter.logic_invocations == 0
    assert qna.calls == []


async def test_valid_auth_processes_activity_with_server_derived_bot_tag():
    qna = FakeQnAClient()
    app, adapter = _build(authenticate=True, qna=qna)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/messages",
            json=_VALID_ACTIVITY,
            headers={"Authorization": "Bearer valid.mocked.token"},
        )
    # logic ran (the activity passed the auth boundary) and the verified
    # tenant id was resolved server-side to the configured bot_tag before the
    # QnA call — the central invariant, proven with auth mocked offline.
    assert resp.status == HTTPStatus.OK
    assert adapter.logic_invocations == 1
    assert len(qna.calls) == 1
    assert qna.calls[0]["bot_tag"] == "client_a_hr"
    assert qna.calls[0]["query"] == "What is the refund window?"


async def test_non_json_content_type_rejected_before_auth():
    qna = FakeQnAClient()
    app, adapter = _build(authenticate=True, qna=qna)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/messages",
            data=b"not json",
            headers={"Content-Type": "text/plain", "Authorization": "Bearer x"},
        )
    assert resp.status == HTTPStatus.UNSUPPORTED_MEDIA_TYPE
    assert adapter.logic_invocations == 0
