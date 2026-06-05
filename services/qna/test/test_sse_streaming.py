"""Tests for the SSE streaming endpoint ``POST /qna/stream``.

Covers the streaming contract added alongside the SDK's ``stream_ask`` client:

- The endpoint yields answer tokens, then a final citation event, then the
  OpenAI-style ``[DONE]`` sentinel — in the wire format the SDK's
  ``tocdoc_sdk._sse`` parser understands.
- The SAME auth (RS256 JWT middleware) and tenant-binding enforcement gate the
  stream route exactly as they gate ``/qna``.
- ``stream_openai_response`` drives a *true* token stream (``stream=True``) off
  the event loop via a worker thread + asyncio queue, and surfaces a mid-stream
  failure to the consumer.
- The non-streaming ``/qna`` path is unaffected (a focused regression check
  lives here too; the byte-identity lock remains in ``test.py``).

No live Azure: the OpenAI streaming client is a fake yielding chunk objects.
"""

import asyncio
import json
import os

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt

# ---------------------------------------------------------------------------
# Required env BEFORE importing the app (config validates at import time).
# ---------------------------------------------------------------------------
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

import app as appmod  # noqa: E402
from app import app  # noqa: E402
from src.config import config as cfg  # noqa: E402
from src.utils.util import Payload  # noqa: E402

# Minimal SSE parser used as the wire-compat oracle. This is a faithful,
# self-contained reimplementation of the subset the SDK's `tocdoc_sdk._sse`
# parser implements (the SDK lives outside services/qna, so we do not import
# it here): blank line terminates an event, `data:` fields (one leading space
# stripped) join with "\n", `:` comments and non-`data` fields are ignored, and
# a `data` payload equal to `[DONE]` terminates the stream WITHOUT being yielded.
# If this recovers tokens then the citation JSON and stops at [DONE], the server
# matches the SDK contract.
_DONE_SENTINEL = "[DONE]"


def iter_sse_data(lines):
    data_lines: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                payload = "\n".join(data_lines)
                data_lines = []
                if payload == _DONE_SENTINEL:
                    return
                yield payload
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if field != "data":
            continue
        if value.startswith(" "):
            value = value[1:]
        data_lines.append(value)
    if data_lines:
        payload = "\n".join(data_lines)
        if payload != _DONE_SENTINEL:
            yield payload


# ---------------------------------------------------------------------------
# Fakes for the Azure clients (mirrors test.py, plus a streaming completions).
# ---------------------------------------------------------------------------
class _Delta:
    def __init__(self, content):
        self.content = content


class _StreamChoice:
    def __init__(self, content):
        self.delta = _Delta(content)


class _StreamChunk:
    def __init__(self, content):
        self.choices = [_StreamChoice(content)]


class _FakeStreamingCompletions:
    """Returns a streaming iterator when ``stream=True``; a normal response
    otherwise so the same fake serves both the /qna and /qna/stream paths."""

    def __init__(self, tokens, fail_after=None):
        self._tokens = tokens
        self._fail_after = fail_after

    def create(self, *args, stream=False, **kwargs):
        if not stream:

            class _Msg:
                content = "".join(self._tokens)

            class _Choice:
                message = _Msg()

            class _Resp:
                choices = [_Choice()]

            return _Resp()

        def _gen():
            # A role-only first chunk with no content (defensive-path coverage).
            yield _StreamChunk(None)
            for i, tok in enumerate(self._tokens):
                if self._fail_after is not None and i == self._fail_after:
                    raise RuntimeError("upstream stream blew up")
                yield _StreamChunk(tok)

        return _gen()


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeOpenAIClient:
    def __init__(self, completions):
        self.chat = _FakeChat(completions)


class _FakeEmbeddingClient:
    def embed_query(self, text):
        return [0.01, 0.02, 0.03]


class _FakeSearchClient:
    def search(self, **kwargs):
        yield {
            "id": "1",
            "content": "chunk content",
            "section_header": "sec",
            "filename": "fileA.md",
            "filepath": "/docs/fileA.md",
        }


class FakeAzure:
    def __init__(self, tokens, fail_after=None):
        self.embedding_client = _FakeEmbeddingClient()
        self.openai_client = _FakeOpenAIClient(_FakeStreamingCompletions(tokens, fail_after=fail_after))
        self.search_client = _FakeSearchClient()


# Answer tokens whose concatenation carries a parseable ``**Sources:**`` block
# so the citation extractor resolves fileA.md (matching the non-streaming path).
ANSWER_TOKENS = ["This ", "is ", "the ", "answer.\n\n**Sources:**\n[fileA.md]"]


def make_token(email="user@acme.com", tid=None):
    iss = f"https://sts.windows.net/{cfg.settings.AZURE_TENANT_ID}/"
    payload = {"iss": iss, "aud": cfg.settings.AUDIENCE_ID}
    if email:
        payload["upn"] = email
    if tid:
        payload["tid"] = tid
    return jwt.encode(payload, key="dummy", algorithm="HS256")


def url(p):
    return f"{app.root_path}{p}"


# ---------------------------------------------------------------------------
# Autouse fixtures: no Key Vault, fake token validation, pass-through rephrase.
# ---------------------------------------------------------------------------
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

    async def _fake_validate(token, tenant_id, audience):
        from jose import jwt as jose_jwt

        try:
            return jose_jwt.decode(
                token,
                key="dummy",
                algorithms=["HS256"],
                options={"verify_signature": False, "verify_aud": False},
            )
        except Exception:
            raise tv.TokenValidationError("Invalid token") from None

    monkeypatch.setattr(auth_module, "validate_token", _fake_validate, raising=True)
    yield


@pytest.fixture(autouse=True)
def _pass_through_rephrase(monkeypatch):
    from src.services import openai_service as oai

    async def fake_rephrase(*args, **kwargs):
        return {
            "rephrased_query": kwargs.get("current_query") or "hi",
            "is_greeting": False,
            "original_response": "ok",
            "extracted_snippet": "",
            "is_followup": False,
            "was_rephrased": False,
        }

    monkeypatch.setattr(oai, "rephrase_queries", fake_rephrase, raising=True)
    yield


@pytest.fixture(autouse=True)
def _disable_tenant_binding(monkeypatch):
    # Default-ON binding would 403 these tokens (no tid). Opt out except in the
    # tests that explicitly exercise binding.
    monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "false")
    yield


def _attach_azure(tokens=ANSWER_TOKENS, fail_after=None):
    app.state.azure = FakeAzure(tokens, fail_after=fail_after)


def _payload():
    return {
        "session_id": "s1",
        "fr_tag": "read",
        "bot_tag": "toc",
        "bot": [{"user_query": "What is in fileA?"}],
    }


# ===========================================================================
# Happy path: tokens -> citation event -> [DONE], in SDK-parseable framing.
# ===========================================================================
@pytest.mark.asyncio
async def test_stream_yields_tokens_then_citation_then_done():
    _attach_azure()
    token = make_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna/stream"), headers=headers, json=_payload())

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")

    raw = r.text
    # Raw framing: an explicit citation event, an explicit [DONE] sentinel.
    assert "event: citation" in raw
    assert raw.rstrip().endswith("data: [DONE]")

    # Feed the raw bytes through the SDK parser. It yields every data payload
    # (it ignores `event:` fields) and stops at [DONE], so we get the answer
    # tokens followed by the citation JSON, and nothing after [DONE].
    payloads = list(iter_sse_data(raw.split("\n")))
    # The citation JSON is the last yielded payload.
    citation_payload = json.loads(payloads[-1])
    assert citation_payload["citation"]["fileA.md"] == "/docs/fileA.md"

    # Everything before the citation reconstructs the streamed answer tokens.
    streamed_answer = "".join(payloads[:-1])
    assert streamed_answer == "".join(ANSWER_TOKENS)

    # The post-[DONE] sentinel is never surfaced.
    assert "after-done" not in raw


@pytest.mark.asyncio
async def test_stream_token_count_matches_emitted_tokens():
    """Each non-empty model delta becomes its own SSE data event (true token
    streaming), and the empty role-only chunk is skipped."""
    _attach_azure(tokens=["a", "b", "c"])
    token = make_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna/stream"), headers=headers, json=_payload())

    payloads = list(iter_sse_data(r.text.split("\n")))
    # 3 token events + 1 citation event.
    assert payloads[:3] == ["a", "b", "c"]
    assert len(payloads) == 4


# ===========================================================================
# Auth is enforced on the stream route exactly like /qna.
# ===========================================================================
@pytest.mark.asyncio
async def test_stream_requires_bearer():
    _attach_azure()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna/stream"), json=_payload())
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_stream_rejects_missing_email_claim():
    _attach_azure()
    token = make_token(email=None)
    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna/stream"), headers=headers, json=_payload())
    assert r.status_code == 401


# ===========================================================================
# Request-boundary validation parity with /qna (errors BEFORE the first token
# come back as the normal structured envelope, not an SSE stream).
# ===========================================================================
@pytest.mark.asyncio
async def test_stream_400_on_bad_fr_tag_is_envelope_not_stream():
    _attach_azure()
    token = make_token()
    headers = {"Authorization": f"Bearer {token}"}
    bad = _payload() | {"fr_tag": "nonsense"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna/stream"), headers=headers, json=bad)
    assert r.status_code == 400
    # Structured envelope, NOT an event-stream.
    assert not r.headers["content-type"].startswith("text/event-stream")
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


@pytest.mark.asyncio
async def test_stream_400_on_empty_bot():
    _attach_azure()
    token = make_token()
    headers = {"Authorization": f"Bearer {token}"}
    bad = _payload() | {"bot": []}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna/stream"), headers=headers, json=bad)
    assert r.status_code == 400
    assert "Bot list cannot be empty" in r.text


# ===========================================================================
# Tenant-binding is enforced on the stream route at the same call-site as /qna.
# ===========================================================================
@pytest.mark.asyncio
async def test_stream_tenant_binding_rejects_unmapped_bot_tag(monkeypatch):
    monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "true")
    monkeypatch.setenv("QNA_TENANT_BOT_TAG_MAP", '{"tenant-aaaa": ["workspace-a"]}')
    _attach_azure()
    token = make_token(tid="tenant-aaaa")
    headers = {"Authorization": f"Bearer {token}"}
    bad = _payload() | {"bot_tag": "workspace-b"}  # not allowed for this tid
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna/stream"), headers=headers, json=bad)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "UNAUTHORIZED"
    # No stream opened — generic message, no tenant data echoed.
    assert "workspace-b" not in r.text


@pytest.mark.asyncio
async def test_stream_tenant_binding_allows_mapped_bot_tag(monkeypatch):
    monkeypatch.setenv("QNA_ENFORCE_TENANT_BINDING", "true")
    monkeypatch.setenv("QNA_TENANT_BOT_TAG_MAP", '{"tenant-aaaa": ["workspace-a"]}')
    _attach_azure()
    token = make_token(tid="tenant-aaaa")
    headers = {"Authorization": f"Bearer {token}"}
    ok = _payload() | {"bot_tag": "workspace-a"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna/stream"), headers=headers, json=ok)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert "data: [DONE]" in r.text


# ===========================================================================
# Mid-stream failure: after the first token, a terminal error event then [DONE].
# ===========================================================================
@pytest.mark.asyncio
async def test_stream_midstream_failure_emits_terminal_error_event():
    # Fail on the 3rd token (index 2) so at least one token streams first,
    # forcing the mid-stream (post-headers) error path rather than an envelope.
    _attach_azure(tokens=["a", "b", "c", "d"], fail_after=2)
    token = make_token()
    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna/stream"), headers=headers, json=_payload())

    # The response status is 200 (it began streaming); the failure is in-band.
    assert r.status_code == 200
    raw = r.text
    assert "event: error" in raw
    assert raw.rstrip().endswith("data: [DONE]")
    # No raw exception text leaks into the stream.
    assert "blew up" not in raw

    # The error payload carries the safe envelope-shaped body.
    payloads = list(iter_sse_data(raw.split("\n")))
    err = json.loads(payloads[-1])
    assert err["error"]["code"] == "INTERNAL_ERROR"
    assert err["error"]["message"] == "Streaming failed"


# ===========================================================================
# stream_openai_response unit: true off-loop token streaming + error surfacing.
# ===========================================================================
@pytest.mark.asyncio
async def test_stream_openai_response_yields_tokens_off_loop():
    from src.services.openai_service import stream_openai_response

    azure = FakeAzure(tokens=["x", "y", "z"])
    out = []
    async for tok in stream_openai_response("q", [], False, False, azure):
        out.append(tok)
    assert out == ["x", "y", "z"]


@pytest.mark.asyncio
async def test_stream_openai_response_surfaces_worker_exception():
    from src.services.openai_service import stream_openai_response

    azure = FakeAzure(tokens=["x", "y", "z"], fail_after=1)
    out = []
    with pytest.raises(RuntimeError):
        async for tok in stream_openai_response("q", [], False, False, azure):
            out.append(tok)
    # The first token streamed before the failure (true mid-stream surfacing).
    assert out == ["x"]


# ===========================================================================
# Pre-first-token (priming) failure: retrieval blows up BEFORE any token, so the
# handler returns the normal structured envelope — NOT a half-open SSE stream.
# This locks the "errors before the first token return the normal envelope" half
# of the contract: the pipeline generator is primed (`__anext__`) inside the
# handler before StreamingResponse is returned, so a search/embedding failure
# propagates to the global exception handler.
# ===========================================================================
class _BoomSearchClient:
    def search(self, **kwargs):
        raise RuntimeError("search backend exploded")


@pytest.mark.asyncio
async def test_stream_retrieval_failure_before_first_token_is_envelope_not_stream():
    azure = FakeAzure(tokens=ANSWER_TOKENS)
    azure.search_client = _BoomSearchClient()
    app.state.azure = azure

    token = make_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
    # raise_app_exceptions=False so the test client returns the enveloped 500
    # response (produced by the global handler) instead of re-raising — exactly
    # what a real HTTP client over the wire would receive.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna/stream"), headers=headers, json=_payload())

    # A retrieval failure during priming -> structured 500 envelope, no stream.
    assert r.status_code == 500
    assert not r.headers["content-type"].startswith("text/event-stream")
    body = r.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    # X-Request-ID present on the envelope (P0-6 contract) and no token leaked.
    assert "X-Request-ID" in r.headers
    assert "data: [DONE]" not in r.text
    # No raw exception text leaks.
    assert "exploded" not in r.text


# ===========================================================================
# Concurrency gate releases its slot after the stream finishes (no leak).
# Two SEQUENTIAL streaming requests with a cap of 1 must BOTH succeed; a leaked
# slot would make the second return 429.
# ===========================================================================
@pytest.mark.asyncio
async def test_stream_concurrency_slot_released_after_stream(monkeypatch):
    monkeypatch.setenv("QNA_MAX_CONCURRENCY", "1")
    token = make_token()
    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        _attach_azure()
        r1 = await ac.post(url("/qna/stream"), headers=headers, json=_payload())
        assert r1.status_code == 200
        # Drain the first stream fully so its slot is released.
        assert "data: [DONE]" in r1.text
        _attach_azure()
        r2 = await ac.post(url("/qna/stream"), headers=headers, json=_payload())
    # If the gate leaked the slot, this second request would be 429.
    assert r2.status_code == 200
    assert "data: [DONE]" in r2.text


# ===========================================================================
# Non-streaming /qna remains unaffected by the streaming addition.
# ===========================================================================
@pytest.mark.asyncio
async def test_non_streaming_qna_still_works():
    _attach_azure()
    token = make_token()
    headers = {"Authorization": f"Bearer {token}"}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna"), headers=headers, json=_payload())
    assert r.status_code == 200
    body = r.json()
    # Byte-identical top-level shape preserved.
    assert set(body.keys()) == {"answer", "citation"}
    assert body["citation"]["fileA.md"] == "/docs/fileA.md"


# ===========================================================================
# HIGH fix 1 — SSE backpressure: a full queue must NOT drop tokens, and the
# terminal sentinel must survive (no sentinel-loss deadlock). With the queue
# shrunk to maxsize=1 a slow consumer forces the worker to PARK on `put`; the
# old fire-and-forget `put_nowait` dropped tokens here (and could drop the
# sentinel, hanging forever). Every consumption is `wait_for`-bounded so a
# regression surfaces as a fast test failure, not a suite-wide hang.
# ===========================================================================
@pytest.mark.asyncio
async def test_stream_full_queue_does_not_drop_tokens_or_sentinel(monkeypatch):
    from src.services import openai_service as oai

    # Shrink the bridge queue so a slow consumer guarantees the worker parks on
    # a blocking put (the backpressure path) for nearly every token.
    monkeypatch.setattr(oai, "_STREAM_QUEUE_MAXSIZE", 1, raising=True)

    tokens = [f"t{i}" for i in range(50)]
    azure = FakeAzure(tokens=tokens)

    received = []
    agen = oai.stream_openai_response("q", [], False, False, azure)
    try:
        while True:
            # wait_for is the assertion for "sentinel survived": if the terminal
            # sentinel were dropped this get would hang and time out.
            try:
                tok = await asyncio.wait_for(agen.__anext__(), timeout=5.0)
            except StopAsyncIteration:
                break
            received.append(tok)
            # Yield control each token so the queue stays full and the worker
            # is forced to block on put (exercise real backpressure).
            await asyncio.sleep(0)
    finally:
        await agen.aclose()

    # No drops, correct order, and the stream terminated cleanly (sentinel
    # delivered) rather than hanging.
    assert received == tokens


# ===========================================================================
# HIGH fix 2 — cooperative cancel: a consumer that abandons the stream
# (disconnect / generator close) must STOP the worker promptly instead of
# letting it drain the whole upstream response (executor-slot + denial-of-wallet
# leak). The fake stream yields effectively unbounded tokens into a shared list;
# after the consumer reads a couple and closes, that list must STOP growing far
# below the unbounded ceiling. `future.cancel()` alone could not achieve this.
# ===========================================================================
class _UnboundedStreamCompletions:
    """Streaming completions whose iterator yields tokens forever (until the
    worker cooperatively stops), recording each produced token so the test can
    observe whether the worker kept running after the consumer left."""

    def __init__(self, produced, ceiling=100000):
        self._produced = produced
        self._ceiling = ceiling

    def create(self, *args, stream=False, **kwargs):
        produced = self._produced
        ceiling = self._ceiling

        def _gen():
            i = 0
            while i < ceiling:
                tok = f"tok{i}"
                produced.append(tok)
                yield _StreamChunk(tok)
                i += 1

        return _gen()


@pytest.mark.asyncio
async def test_stream_consumer_cancel_stops_worker(monkeypatch):
    from src.services import openai_service as oai

    # Small queue so the worker is forced to keep pace with the consumer (it
    # parks on put once the consumer stops reading) — makes the "stop growing"
    # signal sharp.
    monkeypatch.setattr(oai, "_STREAM_QUEUE_MAXSIZE", 4, raising=True)

    produced: list[str] = []
    azure = FakeAzure(tokens=[])
    azure.openai_client = _FakeOpenAIClient(_UnboundedStreamCompletions(produced))

    agen = oai.stream_openai_response("q", [], False, False, azure)
    read = []
    for _ in range(2):
        read.append(await asyncio.wait_for(agen.__anext__(), timeout=5.0))
    # Abandon the stream: this triggers the consumer `finally` (stop event set +
    # queue drained) which must release and stop the worker.
    await agen.aclose()
    assert read == ["tok0", "tok1"]

    # Give the worker a moment, then confirm production has STOPPED (it does not
    # keep climbing toward the ceiling). Sample twice with a yield in between.
    await asyncio.sleep(0.2)
    count_after_close = len(produced)
    await asyncio.sleep(0.2)
    assert len(produced) == count_after_close, (
        "worker kept producing after consumer cancelled — cooperative stop failed"
    )
    # And it stopped promptly: nowhere near the unbounded ceiling.
    assert count_after_close < 1000


# ===========================================================================
# Re-audit HIGH — client disconnect on the OUTER SSE body must NOT raise
# ``RuntimeError: async generator ignored GeneratorExit`` AND must stop the
# inner worker promptly. The pre-fix `finally: yield _SSE_DONE` re-raises during
# GeneratorExit teardown, killing the outer generator before the inner stream is
# closed (the cooperative-stop finally then runs only at GC). The existing
# `test_stream_consumer_cancel_stops_worker` only aclose()s the INNER
# `stream_openai_response`, never the outer `event_source`/StreamingResponse
# body — that blind spot is how the bug slipped through. This test drives the
# OUTER body to a token then aclose()s it.
# ===========================================================================
def _bare_request(payload):
    """Minimal Starlette Request that carries app+state for the stream handler.

    Tenant binding is disabled by the autouse fixture, so the handler only reads
    ``request.app.state.azure`` and ``request.state.request_id`` off this scope.
    """
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http",
        "method": "POST",
        "path": url("/qna/stream"),
        "headers": [],
        "query_string": b"",
        "app": app,
        "state": {"request_id": "disconnect-test"},
    }

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return StarletteRequest(scope, _receive)


@pytest.mark.asyncio
async def test_stream_outer_body_disconnect_no_runtime_error_and_stops_worker(monkeypatch):
    from app import custom_rag_qna_stream

    # Small queue so the worker parks on put once the consumer stops reading,
    # making the "production stopped" signal sharp.
    from src.services import openai_service as oai

    monkeypatch.setattr(oai, "_STREAM_QUEUE_MAXSIZE", 4, raising=True)

    produced: list[str] = []
    azure = FakeAzure(tokens=[])
    azure.openai_client = _FakeOpenAIClient(_UnboundedStreamCompletions(produced))
    app.state.azure = azure

    payload = Payload(**_payload())
    request = _bare_request(payload)

    response = await custom_rag_qna_stream(payload, request)
    body = response.body_iterator

    # Drive the OUTER body to its first real SSE chunk (a token), then abandon
    # it exactly as Starlette does on client disconnect: aclose() the body
    # iterator, which throws GeneratorExit into the suspended `yield`.
    first_chunk = await asyncio.wait_for(body.__anext__(), timeout=5.0)
    assert "data:" in first_chunk

    # Pre-fix: this raises RuntimeError("async generator ignored GeneratorExit")
    # because the `finally: yield _SSE_DONE` yields during GeneratorExit teardown.
    # Post-fix: aclose() propagates GeneratorExit cleanly down to the inner
    # stream's cooperative-stop finally, with no yield-in-finally.
    await asyncio.wait_for(body.aclose(), timeout=5.0)

    # The inner worker must have stopped promptly (cooperative cancel ran as part
    # of disconnect teardown, not deferred to GC).
    await asyncio.sleep(0.2)
    count_after_close = len(produced)
    await asyncio.sleep(0.2)
    assert len(produced) == count_after_close, (
        "inner worker kept producing after outer body disconnect — cooperative stop did not run on aclose()"
    )
    assert count_after_close < 1000


# ===========================================================================
# Re-audit HIGH — END-TO-END concurrency-gate slot release on a MID-STREAM
# client disconnect, through the REAL ASGI / Depends(concurrency_gate_dependency)
# stack. The existing disconnect tests call ``custom_rag_qna_stream`` directly,
# bypassing the route's ``Depends`` — so the gate's acquire-on-admit /
# release-on-teardown is never exercised when a client disconnects mid-stream.
# And ``test_stream_concurrency_slot_released_after_stream`` drains the stream to
# completion first, which cannot distinguish "released on disconnect" from
# "released on normal completion".
#
# Why this needs raw ASGI for the in-flight request: httpx ``ASGITransport``
# buffers the entire response body before the first ``aiter_bytes`` chunk is
# yielded, so a stream opened through it always RUNS TO COMPLETION before the
# caller regains control — there is no real "mid-stream" moment to disconnect at,
# and a release-after-completion test would be vacuous. So request 1 is driven
# via ``app(scope, receive, send)`` directly with a ``receive`` that emits
# ``http.disconnect`` on demand, giving a deterministic mid-stream disconnect
# through the full dependency stack. Requests 2 and 3 use the normal client.
#
# Non-vacuity is built in: with the cap at 1, while request 1 holds the slot a
# concurrent request 2 MUST be rejected 429 (proving both that the cap is
# effective and that an in-flight stream genuinely holds the slot). The 429 is
# asserted to be the CONCURRENCY gate specifically (its distinct message), with
# the rate limiter disabled so the two 429-producers cannot be confused. Only
# after request 1 is torn down by the disconnect does request 3 get admitted.
# ===========================================================================
@pytest.fixture(autouse=True)
def _reset_throttle_state():
    # ``_concurrency_gate`` / ``_rate_limiter`` are module globals shared across
    # the whole suite; reset around this test so a leaked slot elsewhere cannot
    # make the assertions spurious (and we leave them clean for later tests).
    appmod._concurrency_gate.reset()
    appmod._rate_limiter.reset()
    yield
    appmod._concurrency_gate.reset()
    appmod._rate_limiter.reset()


def _stream_scope(token, body: bytes):
    """Raw ASGI HTTP scope mirroring what httpx puts on the wire for the route
    (the doubled ``/qna/qna/stream`` path is real — ``root_path`` is ``/qna`` and
    the route is ``/qna/stream``)."""
    path = url("/qna/stream")
    return {
        "type": "http",
        "method": "POST",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("testclient", 50000),
        "headers": [
            (b"host", b"test"),
            (b"authorization", f"Bearer {token}".encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"accept", b"text/event-stream"),
        ],
    }


def _attach_unbounded_azure():
    """Attach an azure whose answer stream never ends on its own, so request 1
    genuinely holds the concurrency slot until we disconnect it (rather than
    completing instantly the way the bounded ANSWER_TOKENS fake would)."""
    produced: list[str] = []
    azure = FakeAzure(tokens=[])
    azure.openai_client = _FakeOpenAIClient(_UnboundedStreamCompletions(produced))
    app.state.azure = azure
    return produced


@pytest.mark.asyncio
async def test_stream_gate_slot_released_on_midstream_disconnect_end_to_end(monkeypatch):
    monkeypatch.setenv("QNA_MAX_CONCURRENCY", "1")
    # Disable the sliding-window rate limiter so the ONLY thing that can produce
    # a 429 below is the concurrency gate (both raise INVALID_REQUEST/429).
    monkeypatch.setenv("QNA_RATE_LIMIT_PER_MIN", "0")
    # Small bridge queue: the worker parks once the consumer stops reading.
    from src.services import openai_service as oai

    monkeypatch.setattr(oai, "_STREAM_QUEUE_MAXSIZE", 4, raising=True)

    token = make_token()
    body = json.dumps(_payload()).encode()

    # --- Request 1: admit a streaming request and hold it mid-stream. ---
    _attach_unbounded_azure()

    slot_held = asyncio.Event()  # set once the body has started streaming
    trigger_disconnect = asyncio.Event()  # set to fire http.disconnect
    status_box: dict[str, int] = {}
    recv_calls = {"n": 0}

    async def receive():
        recv_calls["n"] += 1
        # Call 1: FastAPI parses the request body. Call 2 (Starlette's disconnect
        # listener) parks until we choose to disconnect mid-stream.
        if recv_calls["n"] == 1:
            return {"type": "http.request", "body": body, "more_body": False}
        await trigger_disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            status_box["status"] = message["status"]
        elif message["type"] == "http.response.body" and message.get("body"):
            slot_held.set()  # first real body chunk -> stream is in flight

    req1_task = asyncio.create_task(app(_stream_scope(token, body), receive, send))
    # Once the first chunk is out, request 1 has acquired and is HOLDING the slot.
    await asyncio.wait_for(slot_held.wait(), timeout=5.0)
    assert status_box.get("status") == 200

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test", timeout=10.0) as ac:
        headers = {"Authorization": f"Bearer {token}"}

        # --- Request 2 (NON-VACUITY GUARD): while the slot is held, a second
        # request is rejected with the CONCURRENCY gate's 429. If this did NOT
        # 429, the cap would be ineffective / the slot not actually held, and the
        # disconnect assertion below would prove nothing. ---
        r2 = await ac.post(url("/qna/stream"), headers=headers, json=_payload())
        assert r2.status_code == 429
        assert r2.json()["error"]["message"] == "Server is at capacity. Retry shortly."

        # --- Disconnect request 1 mid-stream and wait for full teardown. The
        # gate's `finally` (slot release) runs on the dependency unwind whether
        # Starlette returns or raises on disconnect, so gather() guarantees the
        # release has happened before we proceed — no sleeps needed. ---
        trigger_disconnect.set()
        await asyncio.wait_for(asyncio.gather(req1_task, return_exceptions=True), timeout=5.0)

        # --- Request 3: a fresh request is now ADMITTED (200), proving the slot
        # was released on the mid-stream disconnect. Use the bounded fake so the
        # normal (non-streaming) read terminates. ---
        _attach_azure()
        r3 = await ac.post(url("/qna/stream"), headers=headers, json=_payload())
    assert r3.status_code == 200
    assert "data: [DONE]" in r3.text
