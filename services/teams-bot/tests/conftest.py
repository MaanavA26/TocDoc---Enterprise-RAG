"""Shared test fakes and fixtures (mocked — no live Teams/Azure).

The fakes here let us exercise the bot's full turn flow without a Bot Connector,
an AAD token endpoint, or a running QnA service:

- ``FakeQnAClient`` records ``ask`` call args and returns a canned
  ``QnAAnswer`` (or raises a canned ``ApiError``), so tests assert *what* the
  bot sent and *how* it renders the result.
- ``FakeTokenProvider`` returns a fixed token and records the SSO token it was
  handed (the OBO seam, injected).
- ``FakeTurnContext`` stands in for a *verified* ``TurnContext``: it carries a
  Microsoft-signed-equivalent ``channelData.tenant.id`` and message text, and
  captures the activities the bot sends back. Constructing one models an
  activity that has *already passed* inbound JWT validation.
"""

from __future__ import annotations

from typing import Any

import pytest
from teams_bot.tokens import TokenAcquisitionError
from tocdoc_sdk import ApiError, CitationMap, QnAAnswer


class FakeQnAClient:
    """Records ``ask`` calls; returns a canned answer or raises a canned error."""

    def __init__(
        self,
        *,
        answer: QnAAnswer | None = None,
        error: ApiError | None = None,
    ) -> None:
        self._answer = answer
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def ask(self, *, session_id: str, bot_tag: str, fr_tag: str, query: str) -> QnAAnswer:
        self.calls.append(
            {
                "session_id": session_id,
                "bot_tag": bot_tag,
                "fr_tag": fr_tag,
                "query": query,
            }
        )
        if self._error is not None:
            raise self._error
        if self._answer is not None:
            return self._answer
        return QnAAnswer(answer="(default)", citation=CitationMap(root={}))


class FakeTokenProvider:
    """Injected fake OBO provider: returns a fixed token, records the SSO input."""

    def __init__(self, *, token: str = "fake-obo-token", fail: bool = False) -> None:
        self._token = token
        self._fail = fail
        self.seen_user_tokens: list[str | None] = []

    def get_qna_token(self, *, user_token: str | None) -> str:
        self.seen_user_tokens.append(user_token)
        if self._fail:
            raise TokenAcquisitionError("no token")
        return self._token


class _FakeConversation:
    def __init__(self, conversation_id: str) -> None:
        self.id = conversation_id


class _FakeActivity:
    def __init__(self, *, text: str, channel_data: dict, conversation_id: str) -> None:
        self.type = "message"
        self.text = text
        self.channel_data = channel_data
        self.conversation = _FakeConversation(conversation_id)


class FakeTurnContext:
    """Stand-in for a *verified* TurnContext; captures sent activities.

    Constructing one represents an activity that has already passed inbound Bot
    Framework JWT validation, so its tenant id is trusted.
    """

    def __init__(
        self,
        *,
        text: str,
        tenant_id: str | None,
        conversation_id: str = "conv-1",
        user_sso_token: str | None = None,
    ) -> None:
        channel_data: dict[str, Any] = {}
        if tenant_id is not None:
            channel_data["tenant"] = {"id": tenant_id}
        if user_sso_token is not None:
            channel_data["user_sso_token"] = user_sso_token
        self.activity = _FakeActivity(text=text, channel_data=channel_data, conversation_id=conversation_id)
        self.sent_activities: list[Any] = []

    async def send_activity(self, activity: Any) -> None:
        self.sent_activities.append(activity)


@pytest.fixture
def answer_with_citation() -> QnAAnswer:
    return QnAAnswer(
        answer="The refund window is 30 days.",
        citation=CitationMap(root={"policy.md": "/internal/blobs/policy.md"}),
    )
