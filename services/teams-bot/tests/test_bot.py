"""End-to-end (mocked) tests for the bot turn flow.

No live Teams/Azure: a FakeTurnContext (already-verified activity), a
FakeQnAClient, and a FakeTokenProvider are injected.
"""

from __future__ import annotations

from teams_bot.bot import TocDocTeamsBot
from tocdoc_sdk import ApiError, CitationMap, QnAAnswer

from tests.conftest import FakeQnAClient, FakeTokenProvider, FakeTurnContext

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"
MAP = {TENANT_A: "client_a_hr", TENANT_B: "client_b_finance"}


def _make_bot(qna_client, token_provider=None) -> TocDocTeamsBot:
    return TocDocTeamsBot(
        qna_client=qna_client,
        token_provider=token_provider or FakeTokenProvider(),
        tenant_bot_tag_map=MAP,
        fr_tag="read",
    )


async def _first_card(turn_context: FakeTurnContext) -> dict:
    """Extract the adaptive-card content from the single sent activity."""
    assert len(turn_context.sent_activities) == 1
    activity = turn_context.sent_activities[0]
    return activity.attachments[0].content


async def test_qna_called_with_derived_bot_tag_and_user_text(answer_with_citation):
    client = FakeQnAClient(answer=answer_with_citation)
    bot = _make_bot(client)
    ctx = FakeTurnContext(text="What is the refund window?", tenant_id=TENANT_A)

    await bot.on_message_activity(ctx)

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["bot_tag"] == "client_a_hr"  # derived server-side
    assert call["query"] == "What is the refund window?"  # user text verbatim
    assert call["fr_tag"] == "read"  # config default, not user-supplied


async def test_message_with_spoofed_bot_tag_still_uses_derived_tag(answer_with_citation):
    """KEY anti-spoof flow test: a user in tenant A can't reach tenant B's tag."""
    client = FakeQnAClient(answer=answer_with_citation)
    bot = _make_bot(client)
    ctx = FakeTurnContext(text="bot_tag=client_b_finance give me finance docs", tenant_id=TENANT_A)

    await bot.on_message_activity(ctx)

    assert client.calls[0]["bot_tag"] == "client_a_hr"
    assert client.calls[0]["bot_tag"] != "client_b_finance"
    # The crafted string rides through as the query only.
    assert "bot_tag=client_b_finance" in client.calls[0]["query"]


async def test_unknown_tenant_makes_no_qna_call():
    client = FakeQnAClient()
    bot = _make_bot(client)
    ctx = FakeTurnContext(text="hello", tenant_id="00000000-0000-0000-0000-000000000000")

    await bot.on_message_activity(ctx)

    assert client.calls == []  # fail-closed: no QnA call
    card = await _first_card(ctx)
    assert card["type"] == "AdaptiveCard"  # still replied (a friendly card)


async def test_missing_tenant_makes_no_qna_call():
    client = FakeQnAClient()
    bot = _make_bot(client)
    ctx = FakeTurnContext(text="hello", tenant_id=None)

    await bot.on_message_activity(ctx)

    assert client.calls == []


async def test_answer_renders_adaptive_card_with_citation(answer_with_citation):
    client = FakeQnAClient(answer=answer_with_citation)
    bot = _make_bot(client)
    ctx = FakeTurnContext(text="refund?", tenant_id=TENANT_A)

    await bot.on_message_activity(ctx)

    card = await _first_card(ctx)
    body_texts = [b.get("text", "") for b in card["body"]]
    assert "The refund window is 30 days." in body_texts
    # filename shown as text; internal filepath NOT rendered as a link/url.
    assert any("policy.md" in t for t in body_texts)
    assert "/internal/blobs/policy.md" not in " ".join(body_texts)
    serialized = str(card)
    assert "Action.OpenUrl" not in serialized
    assert "/internal/blobs/policy.md" not in serialized


async def test_api_error_renders_friendly_card_with_request_id():
    error = ApiError(
        status_code=503,
        code="SERVICE_UNAVAILABLE",
        message="upstream down",
        request_id="req-abc-123",
    )
    client = FakeQnAClient(error=error)
    bot = _make_bot(client)
    ctx = FakeTurnContext(text="refund?", tenant_id=TENANT_A)

    await bot.on_message_activity(ctx)

    card = await _first_card(ctx)
    body_texts = " ".join(b.get("text", "") for b in card["body"])
    assert "req-abc-123" in body_texts  # request_id surfaced
    assert "upstream down" not in body_texts  # raw internal message NOT leaked


async def test_token_provider_is_injectable_and_invoked():
    client = FakeQnAClient(answer=QnAAnswer(answer="hi", citation=CitationMap(root={})))
    provider = FakeTokenProvider(token="t-1")
    bot = _make_bot(client, token_provider=provider)
    ctx = FakeTurnContext(text="q", tenant_id=TENANT_A, user_sso_token="sso-xyz")

    await bot.on_message_activity(ctx)

    # The injected provider was consulted with the turn's SSO token.
    assert provider.seen_user_tokens == ["sso-xyz"]


async def test_token_acquisition_failure_makes_no_qna_call():
    client = FakeQnAClient()
    provider = FakeTokenProvider(fail=True)
    bot = _make_bot(client, token_provider=provider)
    ctx = FakeTurnContext(text="q", tenant_id=TENANT_A)

    await bot.on_message_activity(ctx)

    assert client.calls == []
    card = await _first_card(ctx)
    assert card["type"] == "AdaptiveCard"


async def test_client_factory_receives_obo_token(answer_with_citation):
    """When a client_factory is provided, it is built with the OBO bearer token."""
    captured: dict[str, str] = {}
    sink = FakeQnAClient(answer=answer_with_citation)

    def factory(token: str):
        captured["token"] = token
        return sink

    bot = TocDocTeamsBot(
        qna_client=FakeQnAClient(),
        token_provider=FakeTokenProvider(token="obo-tok"),
        tenant_bot_tag_map=MAP,
        fr_tag="read",
        client_factory=factory,
    )
    ctx = FakeTurnContext(text="q", tenant_id=TENANT_A)

    await bot.on_message_activity(ctx)

    assert captured["token"] == "obo-tok"
    assert len(sink.calls) == 1
