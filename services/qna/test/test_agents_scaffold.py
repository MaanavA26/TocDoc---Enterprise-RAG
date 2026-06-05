"""Tests for the P3 PR0 LangGraph agentic scaffold (default-OFF dark seam).

Covers:
- Flag OFF (default): /qna uses the legacy direct generate_answer path and
  emits the byte-identical {answer, citation} shape — the existing contract.
- Flag ON (pipeline mocked): /qna routes through the graph and returns the
  same shape; the legacy generate_answer is NOT called directly by the handler.
- The real classifier sets route from a (mocked) structured-output call,
  defaults to "standard" on any exception, and logs route_decision without
  the raw query.
- The verifier node is a pass-through (writes no keys).
- agentic_generate_answer returns the same dict shape as generate_answer
  (with generate_answer mocked), mapping internal state keys back onto the
  frozen wire contract.

Env is set BEFORE importing the app, mirroring test.py (config validates
required env at import time).
"""

import os

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt

os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://fake-openai.example.com")
os.environ.setdefault("AZURE_OPENAI_KEY", "fake-key")
os.environ.setdefault("AZURE_OPENAI_VERSION", "2024-06-01")
os.environ.setdefault("AZURE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "https://fake-search.example.com")
os.environ.setdefault("AZURE_SEARCH_KEY", "fake-search-key")
os.environ.setdefault("INDEX_NAME", "fake-index")
os.environ.setdefault("AZURE_KEY_VAULT", "fakevault")
os.environ.setdefault("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")
os.environ.setdefault("AUDIENCE_ID", "api://fake-audience-id")

from app import app  # noqa: E402
from src.config import config as cfg  # noqa: E402


# ---------------------------------------------------------------------------
# Minimal Azure fake (mirrors test.py)
# ---------------------------------------------------------------------------
class FakeAzure:
    """Sentinel azure holder; the pipeline is mocked so its clients are unused."""


def make_token(email: str | None = "user@acme.com") -> str:
    iss = f"https://sts.windows.net/{cfg.settings.AZURE_TENANT_ID}/"
    aud = cfg.settings.AUDIENCE_ID
    payload = {"iss": iss, "aud": aud}
    if email:
        payload["upn"] = email
    return jwt.encode(payload, key="dummy", algorithm="HS256")


def url(p: str) -> str:
    return f"{app.root_path}{p}"


@pytest.fixture(autouse=True)
def _patch_startup(monkeypatch):
    async def _no_kv():
        return {}

    monkeypatch.setattr(cfg.settings, "load_secrets_from_keyvault", _no_kv, raising=True)
    yield


@pytest.fixture(autouse=True)
def _patch_validate_token(monkeypatch):
    import src.core.auth as auth_module
    import src.core.token_validator as tv

    async def _fake_validate(token: str, tenant_id: str, audience: str) -> dict:
        from jose import jwt as jose_jwt

        try:
            claims = jose_jwt.decode(
                token,
                key="dummy",
                algorithms=["HS256"],
                options={"verify_signature": False, "verify_aud": False},
            )
        except Exception:
            raise tv.TokenValidationError("Invalid token") from None
        return claims

    monkeypatch.setattr(auth_module, "validate_token", _fake_validate, raising=True)
    yield


@pytest.fixture(autouse=True)
async def _attach_fake_azure():
    app.state.azure = FakeAzure()
    yield
    app.state.azure = None


@pytest.fixture(autouse=True)
def _disable_tenant_binding(monkeypatch):
    """Tenant binding now defaults ON (H1). These scaffold tests use tokens
    without a `tid` claim and don't exercise binding, so opt out explicitly so
    they keep testing the agentic-routing behaviour."""
    monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "false")
    yield


_PAYLOAD = {
    "session_id": "s1",
    "fr_tag": "read",
    "bot_tag": "toc",
    "bot": [{"user_query": "What is in fileA?"}],
}


def _headers():
    return {"Authorization": f"Bearer {make_token()}"}


# ---------------------------------------------------------------------------
# Flag default — OFF
# ---------------------------------------------------------------------------
def test_flag_defaults_off(monkeypatch):
    """QNA_AGENT_ENABLED unset → is_agent_enabled() is False."""
    monkeypatch.delenv("QNA_AGENT_ENABLED", raising=False)
    assert cfg.is_agent_enabled() is False


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "", "  "])
def test_flag_falsy_values(monkeypatch, value):
    """The literal string 'false' (and friends) must be falsy."""
    monkeypatch.setenv("QNA_AGENT_ENABLED", value)
    assert cfg.is_agent_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", "On"])
def test_flag_truthy_values(monkeypatch, value):
    monkeypatch.setenv("QNA_AGENT_ENABLED", value)
    assert cfg.is_agent_enabled() is True


@pytest.mark.asyncio
async def test_qna_flag_off_uses_legacy_path(monkeypatch):
    """Flag OFF → handler calls the legacy generate_answer directly and the
    agentic router is never invoked. Shape is the historical {answer, citation}.
    """
    monkeypatch.delenv("QNA_AGENT_ENABLED", raising=False)

    import src.pipeline.qna_pipeline as pipe

    legacy_called = {"n": 0}

    async def fake_generate_answer(**kwargs):
        legacy_called["n"] += 1
        return {"answer": "legacy answer", "citation": {"fileA.md": "/docs/fileA.md"}}

    monkeypatch.setattr(pipe, "generate_answer", fake_generate_answer, raising=True)

    # If the agentic path were taken, this would blow up the test.
    import src.agents.router as router

    async def _boom(*a, **k):
        raise AssertionError("agentic path must not run with the flag OFF")

    monkeypatch.setattr(router, "agentic_generate_answer", _boom, raising=True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna"), headers=_headers(), json=_PAYLOAD)

    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"answer", "citation"}
    assert body["answer"] == "legacy answer"
    assert body["citation"] == {"fileA.md": "/docs/fileA.md"}
    assert legacy_called["n"] == 1


# ---------------------------------------------------------------------------
# Flag ON — routes through the graph
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_qna_flag_on_routes_through_graph(monkeypatch):
    """Flag ON → /qna routes through the agent graph; the legacy pipeline is
    reached only via standard_route (the unchanged generate_answer), and the
    wire shape is still exactly {answer, citation}.
    """
    monkeypatch.setenv("QNA_AGENT_ENABLED", "true")

    import src.pipeline.qna_pipeline as pipe

    seen = {}

    async def fake_generate_answer(**kwargs):
        seen.update(kwargs)
        return {"answer": "graph answer", "citation": {"fileA.md": "/docs/fileA.md"}}

    monkeypatch.setattr(pipe, "generate_answer", fake_generate_answer, raising=True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna"), headers=_headers(), json=_PAYLOAD)

    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"answer", "citation"}
    assert body["answer"] == "graph answer"
    assert body["citation"] == {"fileA.md": "/docs/fileA.md"}
    # standard_route preserved tenant isolation: bot_tag flowed through to the
    # pipeline unchanged, and the middleware request_id was threaded.
    assert seen["bot_tag"] == "toc"
    assert seen["fr_mode"] == "read"
    assert seen.get("request_id")


# ---------------------------------------------------------------------------
# Node-level unit tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_classifier_sets_route_from_structured_output(monkeypatch):
    """Real classifier writes the route returned by the structured-output call."""
    from src.agents import router

    async def fake_classify_route(azure, query):
        return "map_reduce"

    monkeypatch.setattr(router, "classify_route", fake_classify_route, raising=True)

    out = await router.classifier({"request_id": "r1", "query": "summarize everything", "azure": object()})
    assert out == {"route": "map_reduce"}


@pytest.mark.asyncio
async def test_classifier_defaults_to_standard_on_exception(monkeypatch):
    """Any classifier exception → best-effort default route='standard', never raises.

    Returns exactly {"route": "standard"} (no extra keys leak into state).
    """
    from src.agents import router

    async def boom(azure, query):
        raise RuntimeError("classifier LLM unavailable")

    monkeypatch.setattr(router, "classify_route", boom, raising=True)

    out = await router.classifier({"request_id": "r1", "query": "anything", "azure": object()})
    assert out == {"route": "standard"}


@pytest.mark.asyncio
async def test_classifier_defaults_to_standard_on_missing_azure():
    """A state with no/invalid azure client must not fail the request — the
    best-effort catch is broad enough to swallow the AttributeError/KeyError
    and default to 'standard'.
    """
    from src.agents.router import classifier

    # No "azure" key at all (KeyError on state["azure"]).
    out = await classifier({"request_id": "r1", "query": "anything"})
    assert out == {"route": "standard"}


@pytest.mark.asyncio
async def test_route_decision_logged_without_raw_query(monkeypatch):
    """The route_decision log event carries route + request_id but NEVER the
    raw query text.
    """
    from src.agents import router

    async def fake_classify_route(azure, query):
        return "react"

    monkeypatch.setattr(router, "classify_route", fake_classify_route, raising=True)

    captured = {}

    def fake_log_event(logger, event, *, request_id=None, **fields):
        if event == "agent_route_decision":
            captured["request_id"] = request_id
            captured["fields"] = fields

    monkeypatch.setattr(router, "log_event", fake_log_event, raising=True)

    secret_query = "my-very-secret-private-query-text"
    await router.classifier({"request_id": "r1", "query": secret_query, "azure": object()})

    assert captured["request_id"] == "r1"
    assert captured["fields"].get("route") == "react"
    # The raw query must not appear in any logged field value.
    assert all(secret_query not in str(v) for v in captured["fields"].values())


@pytest.mark.asyncio
async def test_classify_route_validates_against_label_set(monkeypatch):
    """classify_route collapses an off-schema route value to 'standard'."""
    from src.services import openai_service

    def fake_structured(azure, **kwargs):
        return {"route": "totally-bogus"}

    monkeypatch.setattr(openai_service, "_structured_completion_sync", fake_structured, raising=True)

    route = await openai_service.classify_route(object(), "q")
    assert route == "standard"


@pytest.mark.asyncio
async def test_classify_route_passes_through_valid_label(monkeypatch):
    from src.services import openai_service

    def fake_structured(azure, **kwargs):
        return {"route": "map_reduce"}

    monkeypatch.setattr(openai_service, "_structured_completion_sync", fake_structured, raising=True)

    route = await openai_service.classify_route(object(), "q")
    assert route == "map_reduce"


@pytest.mark.asyncio
async def test_verifier_is_passthrough_noop():
    from src.agents.verifier import verifier

    out = await verifier({"request_id": "r1", "route": "standard", "final_answer": "x"})
    assert out == {}


@pytest.mark.asyncio
async def test_standard_route_unpacks_pipeline_result(monkeypatch):
    import src.pipeline.qna_pipeline as pipe
    from src.agents.standard_route import standard_route

    async def fake_generate_answer(**kwargs):
        return {"answer": "A", "citation": {"f.md": "/f.md"}}

    monkeypatch.setattr(pipe, "generate_answer", fake_generate_answer, raising=True)

    out = await standard_route(
        {
            "query": "q",
            "fr_mode": "read",
            "bot_tag": "toc",
            "history": [],
            "azure": object(),
            "request_id": "r1",
        }
    )
    # Node writes only its own keys (no answer/citation contract keys here).
    assert out == {"final_answer": "A", "citations": {"f.md": "/f.md"}}


@pytest.mark.asyncio
async def test_agentic_generate_answer_returns_contract_shape(monkeypatch):
    """The wrapper returns the SAME dict shape as generate_answer."""
    import src.pipeline.qna_pipeline as pipe
    from src.agents.router import agentic_generate_answer

    async def fake_generate_answer(**kwargs):
        return {"answer": "wrapped", "citation": {"f.md": "/f.md"}}

    monkeypatch.setattr(pipe, "generate_answer", fake_generate_answer, raising=True)

    result = await agentic_generate_answer(
        "q",
        "read",
        bot_tag="toc",
        history=[],
        azure=object(),
        request_id="r1",
    )
    assert set(result.keys()) == {"answer", "citation"}
    assert result == {"answer": "wrapped", "citation": {"f.md": "/f.md"}}


@pytest.mark.asyncio
async def test_nonstandard_route_collapses_to_standard_route(monkeypatch):
    """A non-standard classified route (e.g. map_reduce) is traversed by the
    conditional edge and — since the map_reduce/react nodes don't exist yet —
    lands at standard_route, still returning the {answer, citation} shape.

    This is the assertion that proves the add_conditional_edges wiring, not
    just that compile() succeeded.
    """
    import src.pipeline.qna_pipeline as pipe
    from src.agents import router

    async def fake_classify_route(azure, query):
        return "map_reduce"

    monkeypatch.setattr(router, "classify_route", fake_classify_route, raising=True)

    reached = {"n": 0}

    async def fake_generate_answer(**kwargs):
        reached["n"] += 1
        return {"answer": "via standard_route", "citation": {"f.md": "/f.md"}}

    monkeypatch.setattr(pipe, "generate_answer", fake_generate_answer, raising=True)

    result = await router.agentic_generate_answer(
        "summarize everything",
        "read",
        bot_tag="toc",
        history=[],
        azure=object(),
        request_id="r1",
    )

    # The map_reduce edge resolved to standard_route, which ran the pipeline.
    assert reached["n"] == 1
    assert result == {"answer": "via standard_route", "citation": {"f.md": "/f.md"}}


@pytest.mark.asyncio
async def test_node_exception_propagates(monkeypatch):
    """Hard node exceptions are not swallowed by the graph/wrapper (P0-6)."""
    import src.pipeline.qna_pipeline as pipe
    from src.agents.router import agentic_generate_answer

    async def boom(**kwargs):
        raise RuntimeError("pipeline blew up")

    monkeypatch.setattr(pipe, "generate_answer", boom, raising=True)

    with pytest.raises(RuntimeError, match="pipeline blew up"):
        await agentic_generate_answer("q", "read", bot_tag="toc", history=[], azure=object(), request_id="r1")
