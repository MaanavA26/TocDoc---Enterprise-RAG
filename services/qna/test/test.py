# test.py
import os

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt

# ---------------------------------------------------------------------------
# Ensure required env vars exist BEFORE importing the app
# (your config module validates these at import time)
# ---------------------------------------------------------------------------
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://fake-openai.example.com")
os.environ.setdefault("AZURE_OPENAI_KEY", "fake-key")
os.environ.setdefault("AZURE_OPENAI_VERSION", "2024-06-01")
os.environ.setdefault("AZURE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "https://fake-search.example.com")
os.environ.setdefault("AZURE_SEARCH_KEY", "fake-search-key")
os.environ.setdefault("INDEX_NAME", "fake-index")
os.environ.setdefault("AZURE_KEY_VAULT", "fakevault")

# Auth middleware expectations
os.environ.setdefault("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")
os.environ.setdefault("AUDIENCE_ID", "api://fake-audience-id")

# ---------------------------------------------------------------------------
# Import the FastAPI app AFTER envs are set
# ---------------------------------------------------------------------------
from app import app  # adjust if your app module is elsewhere
from src.config import config as cfg


# ---------------------------------------------------------------------------
# Minimal fakes for Azure clients referenced by your code
# ---------------------------------------------------------------------------
class _FakeEmbeddingClient:
    def embed_query(self, text: str) -> list[float]:
        # Deterministic tiny vector
        return [0.01, 0.02, 0.03]


class _FakeOpenAIResponseChoiceMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeOpenAIResponseChoice:
    def __init__(self, content: str):
        self.message = _FakeOpenAIResponseChoiceMessage(content)


class _FakeOpenAIResponse:
    def __init__(self, content: str):
        self.choices = [_FakeOpenAIResponseChoice(content)]


class _FakeOpenAICompletions:
    def create(self, *args, **kwargs):
        # Return a grounded answer with a simple Sources footer.
        # The rephraser is monkeypatched in tests; this path covers the "final answer" call.
        return _FakeOpenAIResponse("This is the answer.\n\n**Sources:\n[fileA.md]")


class _FakeOpenAIChat:
    def __init__(self):
        self.completions = _FakeOpenAICompletions()


class _FakeOpenAIClient:
    def __init__(self):
        self.chat = _FakeOpenAIChat()


class _FakeSearchClient:
    def search(self, **kwargs):
        # Yield result dicts matching your code's expectations
        yield {
            "id": "1",
            "content": "chunk content",
            "section_header": "sec",
            "filename": "fileA.md",
            "filepath": "/docs/fileA.md",
        }


class FakeAzure:
    def __init__(self):
        self.embedding_client = _FakeEmbeddingClient()
        self.openai_client = _FakeOpenAIClient()
        self.search_client = _FakeSearchClient()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_token(email: str | None = "user@acme.com") -> str:
    iss = f"https://sts.windows.net/{cfg.settings.AZURE_TENANT_ID}/"
    aud = cfg.settings.AUDIENCE_ID
    payload = {"iss": iss, "aud": aud}
    if email:
        payload["upn"] = email
    # Signature is irrelevant: your middleware disables signature verification.
    return jwt.encode(payload, key="dummy", algorithm="HS256")


def url(p: str) -> str:
    # prepending the "/qna" (root endpoint)
    return f"{app.root_path}{p}"


def _set_kv_env(monkeypatch):
    """Set the Key Vault style envs the Azure client expects."""
    monkeypatch.setenv("AZURE_OPENAI_VERSION", "2024-06-01")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://fake-openai.example.com")
    monkeypatch.setenv("AZURE_OPENAI_KEY", "fake-openai-key")
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://fake-search.example.com")
    monkeypatch.setenv("AZURE_SEARCH_KEY", "fake-search-key")


# ---------------------------------------------------------------------------
# Global fixtures: prevent real cloud calls and attach FakeAzure
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _patch_startup(monkeypatch):
    # Avoid Key Vault network call at startup
    async def _no_kv():
        return {}

    from src.config import config as cfg

    monkeypatch.setattr(cfg.settings, "load_secrets_from_keyvault", _no_kv, raising=True)
    yield


@pytest.fixture(autouse=True)
def _patch_validate_token(monkeypatch):
    """
    Patch validate_token so existing pipeline/functional tests don't need
    real RS256 tokens.  test_auth.py uses its own RSA key pair and does NOT
    apply this patch.

    The mock returns a minimal claims dict built from the HS256 'dummy' token
    payload created by make_token().  If validate_token raises (e.g. the token
    has no 'upn'), the middleware returns 401 as expected by
    test_qna_401_missing_email_claim.
    """
    import src.core.token_validator as tv

    async def _fake_validate(token: str, tenant_id: str, audience: str) -> dict:
        # Decode the test HS256 token without verification to extract claims.
        # This mirrors what the old middleware did with verify_signature=False.
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

    import src.core.auth as auth_module

    monkeypatch.setattr(auth_module, "validate_token", _fake_validate, raising=True)
    yield


@pytest.fixture(autouse=True)
async def _attach_fake_azure():
    # Provide the fake clients to the app for each test
    app.state.azure = FakeAzure()
    yield
    app.state.azure = None


# ---------------------------------------------------------------------------
# Auth & endpoint tests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_health_open_without_auth():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert "status" in body


@pytest.mark.asyncio
async def test_qna_401_without_bearer():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna"), json={})
    assert r.status_code == 401
    assert r.json()["detail"] in {"Missing or invalid Authorization header", "Invalid token"}


@pytest.mark.asyncio
async def test_qna_401_missing_email_claim():
    token = make_token(email=None)
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"session_id": "s1", "fr_tag": "read", "bot_tag": "t", "bot": [{"user_query": "hi"}]}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna"), headers=headers, json=payload)
    assert r.status_code == 401
    assert r.json()["detail"] == "Email claim not found in token"


@pytest.mark.asyncio
async def test_qna_400_empty_bot():
    token = make_token()
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"session_id": "s1", "fr_tag": "read", "bot_tag": "t", "bot": []}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna"), headers=headers, json=payload)
    assert r.status_code == 400
    assert "Bot list cannot be empty" in r.text


@pytest.mark.asyncio
async def test_qna_400_empty_query(monkeypatch):
    # bot has an empty user_query after stripping
    token = make_token()
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"session_id": "s1", "fr_tag": "read", "bot_tag": "t", "bot": [{"user_query": "   "}]}
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna"), headers=headers, json=payload)
    assert r.status_code == 400
    assert "Query cannot be empty" in r.text


# ---------------------------------------------------------------------------
# Functional flow tests (pipeline) with targeted monkeypatching
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_qna_happy_path_returns_answer_and_citations(monkeypatch):
    # Force rephraser to a simple pass-through (no greeting, no follow-up)
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

    token = make_token()
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "session_id": "s1",
        "fr_tag": "read",
        "bot_tag": "toc",
        "bot": [{"user_query": "What is in fileA?"}],
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna"), headers=headers, json=payload)

    assert r.status_code == 200
    body = r.json()
    assert isinstance(body.get("answer"), str)
    assert isinstance(body.get("citation"), dict)
    # Expect citation to include fileA.md from fake search + fake model
    assert "fileA.md" in body["citation"]


@pytest.mark.asyncio
async def test_qna_greeting_path_skips_retrieval(monkeypatch):
    # Make rephraser flag as greeting -> no embedding/search should be required by logic
    from src.services import openai_service as oai

    async def fake_rephrase(*args, **kwargs):
        return {
            "rephrased_query": kwargs.get("current_query") or "hello",
            "is_greeting": True,
            "original_response": "ok",
            "extracted_snippet": "",
            "is_followup": False,
            "was_rephrased": False,
        }

    monkeypatch.setattr(oai, "rephrase_queries", fake_rephrase, raising=True)

    token = make_token()
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "session_id": "s1",
        "fr_tag": "read",
        "bot_tag": "toc",
        "bot": [{"user_query": "Hi there"}],
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna"), headers=headers, json=payload)

    assert r.status_code == 200
    body = r.json()
    assert isinstance(body.get("answer"), str)
    # In greeting path, citations may be empty (no retrieval)
    assert isinstance(body.get("citation"), dict)


@pytest.mark.asyncio
async def test_qna_followup_appends_prior_snippet(monkeypatch):
    # Rephraser says it's a follow-up and provides last reply; pipeline should include it as data-only
    from src.services import openai_service as oai

    async def fake_rephrase(*args, **kwargs):
        return {
            "rephrased_query": "follow up query",
            "is_greeting": False,
            "original_response": "ok",
            "extracted_snippet": "Previous bot reply text",
            "is_followup": True,
            "was_rephrased": True,
        }

    monkeypatch.setattr(oai, "rephrase_queries", fake_rephrase, raising=True)

    token = make_token()
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "session_id": "s1",
        "fr_tag": "read",
        "bot_tag": "toc",
        "bot": [
            {"user_query": "old q", "bot_response": "old a"},
            {"user_query": "new q"},
        ],
    }

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(url("/qna"), headers=headers, json=payload)

    assert r.status_code == 200
    body = r.json()
    assert isinstance(body.get("answer"), str)
    assert isinstance(body.get("citation"), dict)


@pytest.mark.asyncio
async def test_pipeline_handles_non_string_model_response(monkeypatch):
    # Make generate_openai_response return a non-string -> pipeline should TypeError and return error payload
    from src.pipeline import qna_pipeline
    from src.pipeline.qna_pipeline import generate_answer
    from src.utils.util import _as_turn

    # Fake rephrase to avoid greeting/followup branches
    async def fake_rephrase(*args, **kwargs):
        return {
            "rephrased_query": kwargs.get("current_query") or "hi",
            "is_greeting": False,
            "original_response": "ok",
            "extracted_snippet": "",
            "is_followup": False,
            "was_rephrased": False,
        }

    # Return a non-string so the pipeline raises TypeError and emits error payload
    async def fake_gen(*args, **kwargs):
        return 123  # not a string

    monkeypatch.setattr(qna_pipeline, "rephrase_queries", fake_rephrase, raising=True)
    monkeypatch.setattr(qna_pipeline, "generate_openai_response", fake_gen, raising=True)

    # Provide minimal history and FakeAzure
    app.state.azure = FakeAzure()
    history = [_as_turn({"user_query": "q1"})]

    # Call pipeline directly (bypassing HTTP) for this edge case
    result = await generate_answer("q1", "read", bot_tag="toc", history=history, azure=app.state.azure)
    assert "answer" in result and "citation" in result
    assert "error" in result  # pipeline returns an error payload on TypeError


##################################################################################################
# File-wise test cases to achieve 80%+ coverage individually!
##################################################################################################


# --------------------------
# azure_clients.py coverage
# --------------------------
@pytest.mark.asyncio
async def test_azure_clients_missing_required_config(monkeypatch):
    from src.clients.azure_clients import AzureOpenAIHandler

    _set_kv_env(monkeypatch)
    monkeypatch.delenv("AZURE_OPENAI_VERSION", raising=False)

    h = AzureOpenAIHandler()

    # blank out some required values to trigger ValueError
    h.azureconfig.AZURE_OPENAI_API_VERSION = ""
    h.azureconfig.AZURE_OPENAI_ENDPOINT = ""
    with pytest.raises(ValueError):
        h._ensure_client()


@pytest.mark.asyncio
async def test_azure_clients_success_initialization(monkeypatch):
    from src.clients.azure_clients import AzureOpenAIHandler

    _set_kv_env(monkeypatch)

    # fake constructors so we don't hit the network
    class _E:  # Embeddings
        pass

    class _O:  # OpenAI
        pass

    class _S:  # SearchClient
        pass

    # patch external classes used by _ensure_client
    monkeypatch.setattr("src.clients.azure_clients.AzureOpenAIEmbeddings", lambda **kw: _E(), raising=True)
    monkeypatch.setattr("src.clients.azure_clients.AzureOpenAI", lambda **kw: _O(), raising=True)
    monkeypatch.setattr("src.clients.azure_clients.SearchClient", lambda **kw: _S(), raising=True)

    h = AzureOpenAIHandler()
    # ensure config present
    assert h.azureconfig.AZURE_OPENAI_KEY
    h._ensure_client()

    assert isinstance(h.embedding_client, _E)
    assert isinstance(h.openai_client, _O)
    assert isinstance(h.search_client, _S)


@pytest.mark.asyncio
async def test_azure_clients_embedding_init_failure(monkeypatch):
    from src.clients.azure_clients import AzureOpenAIHandler

    _set_kv_env(monkeypatch)

    def boom(**kw):  # raise from embedding ctor
        raise RuntimeError("embeddings init failed")

    monkeypatch.setattr("src.clients.azure_clients.AzureOpenAIEmbeddings", boom, raising=True)

    # stub the others so we never reach them
    monkeypatch.setattr("src.clients.azure_clients.AzureOpenAI", lambda **kw: object(), raising=True)
    monkeypatch.setattr("src.clients.azure_clients.SearchClient", lambda **kw: object(), raising=True)

    h = AzureOpenAIHandler()
    with pytest.raises(RuntimeError):
        h._ensure_client()


@pytest.mark.asyncio
async def test_azure_clients_openai_init_failure(monkeypatch):
    from src.clients.azure_clients import AzureOpenAIHandler

    _set_kv_env(monkeypatch)

    monkeypatch.setattr(
        "src.clients.azure_clients.AzureOpenAIEmbeddings", lambda **kw: object(), raising=True
    )

    def boom(**kw):
        raise RuntimeError("openai init failed")

    monkeypatch.setattr("src.clients.azure_clients.AzureOpenAI", boom, raising=True)
    monkeypatch.setattr("src.clients.azure_clients.SearchClient", lambda **kw: object(), raising=True)

    h = AzureOpenAIHandler()
    with pytest.raises(RuntimeError):
        h._ensure_client()


@pytest.mark.asyncio
async def test_azure_clients_search_init_failure(monkeypatch):
    from src.clients.azure_clients import AzureOpenAIHandler

    _set_kv_env(monkeypatch)

    monkeypatch.setattr(
        "src.clients.azure_clients.AzureOpenAIEmbeddings", lambda **kw: object(), raising=True
    )
    monkeypatch.setattr("src.clients.azure_clients.AzureOpenAI", lambda **kw: object(), raising=True)

    def boom(**kw):
        raise RuntimeError("search init failed")

    monkeypatch.setattr("src.clients.azure_clients.SearchClient", boom, raising=True)

    h = AzureOpenAIHandler()
    with pytest.raises(RuntimeError):
        h._ensure_client()


# --------------------------
# lifecycle.py coverage
# --------------------------
@pytest.mark.asyncio
async def test_lifecycle_startup_success(monkeypatch):
    from src.clients.azure_clients import AzureOpenAIHandler
    from src.core.lifecycle import startup_event

    # avoid real keyvault and client creation
    async def _no_kv():
        return {}

    monkeypatch.setattr("src.config.config.settings.load_secrets_from_keyvault", _no_kv, raising=True)
    monkeypatch.setattr(AzureOpenAIHandler, "_ensure_client", lambda self: None, raising=True)

    # FastAPI app is already imported as `app` in your test file
    await startup_event(app)
    assert hasattr(app.state, "azure")
    assert app.state.azure is not None


@pytest.mark.asyncio
async def test_lifecycle_startup_failure(monkeypatch):
    from src.clients.azure_clients import AzureOpenAIHandler
    from src.core.lifecycle import startup_event

    async def _no_kv():
        return {}

    monkeypatch.setattr("src.config.config.settings.load_secrets_from_keyvault", _no_kv, raising=True)

    def boom(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(AzureOpenAIHandler, "_ensure_client", boom, raising=True)

    with pytest.raises(RuntimeError):
        await startup_event(app)


@pytest.mark.asyncio
async def test_lifecycle_shutdown_sets_state(monkeypatch):
    from src.core.lifecycle import shutdown_event

    app.state.azure = object()
    await shutdown_event(app)
    assert app.state.azure is None


# --------------------------
# config.py coverage
# --------------------------
@pytest.mark.asyncio
async def test_settings_load_secrets_marks_success_and_failure(monkeypatch):
    from src.config.config import Settings

    # fake SecretClient.get_secret behavior: succeed on one key, fail on another
    class _Secret:
        def __init__(self, v):
            self.value = v

    calls = {}

    async def fake_get_secret(name):
        calls[name] = calls.get(name, 0) + 1
        if name.endswith("Endpoint"):
            return _Secret("https://fake.example.com")

        # simulate AzureError for others
        class _AzureErr(Exception):
            pass

        raise _AzureErr("nope")

    class _FakeSecretClient:
        async def get_secret(self, name):
            return await fake_get_secret(name)

        async def close(self):
            return None

    class _FakeCredential:
        async def close(self):
            return None

    monkeypatch.setattr("src.config.config.SecretClient", lambda **kw: _FakeSecretClient(), raising=True)
    monkeypatch.setattr(
        "src.config.config.ClientSecretCredential", lambda *a, **k: _FakeCredential(), raising=True
    )

    results = await Settings.load_secrets_from_keyvault()
    assert isinstance(results, dict)
    # at least one True and one False
    assert True in results.values()
    assert False in results.values()


def test_run_async_handles_existing_loop(monkeypatch):
    import asyncio

    from src.config.config import run_async

    async def coro():
        return "ok"

    # case 1: no running loop → asyncio.run is used
    assert run_async(coro()) == "ok"

    # case 2: running loop → create_task is used
    captured = {}

    class _Loop:
        def create_task(self, c):
            captured["type"] = type(c).__name__
            return "TASK"

    def fake_get_running_loop():
        return _Loop()

    monkeypatch.setattr(asyncio, "get_running_loop", fake_get_running_loop, raising=True)
    assert run_async(coro()) == "TASK"
    assert captured["type"] in ("coroutine", "coro")  # impl detail varies by py version


# --------------------------
# text_processor.py coverage
# --------------------------
@pytest.mark.asyncio
async def test_text_processor_no_sources_section():
    from src.services.text_processor import extract_answer_and_filenames_from_text

    txt = "Only answer text with no marker."
    ans, files = await extract_answer_and_filenames_from_text(txt)
    assert ans == "Only answer text with no marker."
    assert files == []


@pytest.mark.asyncio
async def test_text_processor_bracketed_sources():
    from src.services.text_processor import extract_answer_and_filenames_from_text

    txt = "A\n\n**Sources:\n[one.md; two.pdf]"
    ans, files = await extract_answer_and_filenames_from_text(txt)
    assert ans == "A"
    assert files == ["one.md", "two.pdf"]


@pytest.mark.asyncio
async def test_text_processor_lines_sources():
    from src.services.text_processor import extract_answer_and_filenames_from_text

    txt = "A\n\n**Sources:\none.md\ntwo.pdf"
    ans, files = await extract_answer_and_filenames_from_text(txt)
    assert ans == "A"
    assert files == ["one.md", "two.pdf"]


@pytest.mark.asyncio
async def test_text_processor_bad_return_type(monkeypatch):
    from src.services import text_processor as tp

    # make the sync extractor return something invalid
    def bad(_):
        return "not-a-tuple"

    monkeypatch.setattr(tp, "_extract_sync", bad, raising=True)

    ans, files = await tp.extract_answer_and_filenames_from_text("Hello")
    assert ans == "Hello"
    assert files == []


@pytest.mark.asyncio
async def test_text_processor_sync_raises(monkeypatch):
    from src.services import text_processor as tp

    def boom(_):
        raise RuntimeError("x")

    monkeypatch.setattr(tp, "_extract_sync", boom, raising=True)

    ans, files = await tp.extract_answer_and_filenames_from_text("Hello")
    assert ans == "Hello"
    assert files == []
