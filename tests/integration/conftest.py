"""Shared fixtures for the hermetic end-to-end integration suite.

These tests exercise each service's REAL FastAPI ``app`` object in-process via
Starlette's ``TestClient`` (no live server, no network). Every Azure dependency
is mocked at the *service-entry seam* rather than at the Azure-SDK edge:

- QnA auth: ``validate_token`` is patched so no JWKS fetch / RS256 verification
  ever touches Azure AD. A fixture lets each test choose the decoded claims (or
  raise ``TokenValidationError``) the middleware sees.
- QnA retrieval + LLM: ``src.pipeline.qna_pipeline.generate_answer`` is patched
  to return a realistic ``{answer, citation}`` payload, so the test asserts the
  real ``QnASuccessResponse`` serialization and the full assembled middleware
  stack (auth middleware, RequestID middleware, the three exception handlers)
  without any embedding / search / completion call.
- QnA startup: the lifespan's Key Vault load and Azure client construction are
  mocked so ``app.state.azure`` is populated without touching Azure.
- Ingestion: ``run_connector`` is patched and the RAG instance is overridden via
  FastAPI ``dependency_overrides`` so a connector sync round-trip runs entirely
  in-process. ``/upload`` admin auth uses the static-token guard already in the
  service.

Why import the real ``app.py`` (and not re-mount routers on a fresh app): the
cross-cutting guarantees under test — 401 from the auth middleware, an
``X-Request-ID`` on *every* response, a generic (non-leaking) 500 envelope — are
properties of the fully assembled application. Reconstructing the app would not
exercise them.

Both services define a top-level ``app.py``. To load both in one pytest process
without the module cache colliding, each ``app.py`` is loaded via
``importlib.util.spec_from_file_location`` under a unique module name, with the
respective service directory placed on ``sys.path`` first (QnA uses ``src.*``
imports; ingestion uses flat ``errors`` / ``observability`` / ``custom_rag``
imports — the two namespaces do not overlap).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[2]
_QNA_DIR = _REPO_ROOT / "services" / "qna"
_INGESTION_DIR = _REPO_ROOT / "services" / "ingestion"

# A static admin token used by the ingestion admin-auth guard across tests.
ADMIN_TOKEN = "test-admin-token"

# A fixed Azure AD tenant id used as the validated token's ``tid`` claim.
TEST_TID = "11111111-1111-1111-1111-111111111111"


def _set_required_env() -> None:
    """Populate the env vars the services validate at *import* time.

    ``services/qna/src/config/config.py`` raises at import if any required var
    is unset, and ``load_dotenv(override=True)`` runs there too. The ingestion
    admin guard returns 503 (not 401) when ``ADMIN_API_TOKEN`` is unset. All
    values are throwaway — ``validate_token`` and the Azure clients are mocked,
    so nothing here is ever used to reach Azure.
    """
    os.environ.update(
        {
            # QnA import-time required vars.
            "AZURE_OPENAI_ENDPOINT": "https://test.openai.azure.com",
            "AZURE_OPENAI_KEY": "test-openai-key",
            "AZURE_OPENAI_VERSION": "2024-02-01",
            "AZURE_OPENAI_EMBEDDING_MODEL": "text-embedding-3-small",
            "AZURE_SEARCH_ENDPOINT": "https://test.search.windows.net",
            "AZURE_SEARCH_KEY": "test-search-key",
            "INDEX_NAME": "test-index",
            "AUDIENCE_ID": "api://test-audience",
            "AZURE_TENANT_ID": TEST_TID,
            "AZURE_CLIENT_ID": "test-client-id",
            "AZURE_CLIENT_SECRET": "test-client-secret",
            # Ingestion admin-token guard.
            "ADMIN_API_TOKEN": ADMIN_TOKEN,
        }
    )


def _load_module(path: Path, module_name: str, *extra_syspath: Path):
    """Load a module from a file path under a unique name.

    The service directory is prepended to ``sys.path`` so the module's own
    intra-service imports resolve, then the file is loaded under ``module_name``
    so two same-named ``app.py`` files coexist in one process.
    """
    for entry in extra_syspath:
        s = str(entry)
        if s not in sys.path:
            sys.path.insert(0, s)
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# Session-scoped real app objects
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session", autouse=True)
def _env() -> None:
    """Ensure required env is set before any app module is imported."""
    _set_required_env()


@pytest.fixture(scope="session")
def qna_app(_env):
    """The REAL QnA FastAPI app, loaded once per session."""
    mod = _load_module(_QNA_DIR / "app.py", "qna_app_under_test", _QNA_DIR)
    return mod.app


@pytest.fixture(scope="session")
def ingestion_app(_env):
    """The REAL ingestion FastAPI app, loaded once per session."""
    mod = _load_module(_INGESTION_DIR / "app.py", "ingestion_app_under_test", _INGESTION_DIR)
    return mod.app


# --------------------------------------------------------------------------- #
# QnA client + seam mocks
# --------------------------------------------------------------------------- #
@pytest.fixture
def qna_token_state():
    """Mutable holder controlling what the patched ``validate_token`` returns.

    Tests set ``qna_token_state["claims"]`` to a decoded-claims dict (auth
    succeeds) or ``qna_token_state["error"]`` to a ``TokenValidationError``
    instance (auth fails). Defaults to a valid token for ``TEST_TID``.
    """
    return {
        "claims": {
            "upn": "alice@example.com",
            "tid": TEST_TID,
        },
        "error": None,
    }


@pytest.fixture
def qna_pipeline_result():
    """Mutable holder for what the patched ``generate_answer`` returns."""
    return {
        "answer": "The retention period is seven years.",
        "citation": {"policy.md": "/docs/policy.md"},
    }


@pytest.fixture
def qna_client(qna_app, qna_token_state, qna_pipeline_result, monkeypatch):
    """A ``TestClient`` over the real QnA app with all Azure seams mocked.

    Patches:
    - ``validate_token`` (auth middleware) → returns claims / raises per
      ``qna_token_state``; no JWKS, no network.
    - lifespan Key Vault load + ``AzureOpenAIHandler`` → so startup populates
      ``app.state.azure`` without touching Azure.
    - ``generate_answer`` (the QnA pipeline entrypoint) → returns
      ``qna_pipeline_result`` without any embedding / search / completion call.
    """
    from unittest.mock import AsyncMock, MagicMock

    import src.core.lifecycle as lifecycle
    import src.core.token_validator as token_validator
    import src.pipeline.qna_pipeline as pipeline
    from fastapi.testclient import TestClient

    async def _fake_validate_token(token, tenant_id, audience):
        if qna_token_state["error"] is not None:
            raise qna_token_state["error"]
        return qna_token_state["claims"]

    # Patch the symbol bound INSIDE the auth module (it imports the name).
    import src.core.auth as auth_module

    monkeypatch.setattr(auth_module, "validate_token", _fake_validate_token)
    # Belt-and-suspenders: also patch the source so any other importer is safe.
    monkeypatch.setattr(token_validator, "validate_token", _fake_validate_token)

    # Startup: no Key Vault, no real Azure clients.
    monkeypatch.setattr(
        lifecycle.settings,
        "load_secrets_from_keyvault",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(lifecycle, "AzureOpenAIHandler", MagicMock())

    async def _fake_generate_answer(*args, **kwargs):
        # Return the mutable fixture dict directly so a test can tweak the
        # answer/citation before issuing the request.
        return qna_pipeline_result

    monkeypatch.setattr(pipeline, "generate_answer", _fake_generate_answer)

    # raise_server_exceptions=False so a handler-produced 500 envelope is
    # returned to the test (the real client contract) instead of TestClient
    # re-raising the original exception — that is exactly what the
    # "500 does not leak" assertion needs to inspect.
    with TestClient(qna_app, raise_server_exceptions=False) as client:
        # Both services mount under a non-empty ``root_path`` (QnA: "/qna",
        # ingestion: "/upload_pipeline"). Starlette routes against the FULL path
        # including that prefix, so requests MUST carry it (the service's own
        # tests do the same via ``f"{app.root_path}{path}"``). Attach a small
        # path-prefixing wrapper so tests can call the bare route path.
        _attach_root_path(client, qna_app.root_path)
        yield client


def _attach_root_path(client, root_path: str) -> None:
    """Make a TestClient prefix every request path with ``root_path``.

    Both services mount under a non-empty ``root_path`` (QnA: ``/qna``,
    ingestion: ``/upload_pipeline``), so Starlette routes against the FULL path
    including that prefix — a bare ``POST /qna`` would 405. The services' own
    tests prefix every path with ``app.root_path``; we do the same transparently
    so tests can call the bare route path (``/qna``, ``/health``, ``/admin/...``).

    Every verb helper (``get``/``post``/...) funnels through ``client.request``,
    so wrapping that single method covers them all. A path already carrying the
    prefix is passed through unchanged.
    """
    if not root_path:
        return
    original_request = client.request

    def _request(method, url, *args, **kwargs):
        # Always prepend the mount prefix. A bare route path like "/qna" becomes
        # "/qna/qna" (the QnA POST route under root_path "/qna"); "/health"
        # becomes "/qna/health". We do NOT treat a path that merely equals the
        # prefix as already-prefixed — "/qna" IS a real route under "/qna".
        path = str(url)
        if not path.startswith(root_path + "/"):
            url = f"{root_path}{path}"
        return original_request(method, url, *args, **kwargs)

    client.request = _request


def qna_payload(
    *,
    query: str = "How long do we retain records?",
    bot_tag: str = "workspace-a",
    fr_tag: str = "read",
) -> dict:
    """Build a valid ``/qna`` request body (a ``Payload``)."""
    return {
        "session_id": "sess-1",
        "bot": [{"user_query": query}],
        "fr_tag": fr_tag,
        "bot_tag": bot_tag,
    }


def bearer(token: str = "any-token") -> dict:
    """Authorization header. The token value is irrelevant — validation is mocked."""
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Ingestion client + seam mocks
# --------------------------------------------------------------------------- #
@pytest.fixture
def ingestion_run_state():
    """Controls the patched ``run_connector``: success counts or an exception."""
    return {"processed": 3, "raise": None}


@pytest.fixture
def ingestion_client(ingestion_app, ingestion_run_state, monkeypatch):
    """A ``TestClient`` over the real ingestion app with connector seams mocked.

    - ``run_connector`` (driven on a worker thread by the background task) is
      patched to a no-network async fn that returns a processed count or raises.
    - The RAG instance used by the sync trigger is overridden so no Azure client
      is constructed.
    - The in-process run-status store is cleared around each test.
    """
    import admin.routes as admin_routes
    from connectors.run_status import run_status_store
    from fastapi.testclient import TestClient

    run_status_store.clear()

    async def _fake_run_connector(connector, rag_instance, *, run_id):
        if ingestion_run_state["raise"] is not None:
            raise ingestion_run_state["raise"]
        return {"processed": ingestion_run_state["processed"], "items": []}

    # Patch the name bound inside admin.routes (it imports ``run_connector``).
    monkeypatch.setattr(admin_routes, "run_connector", _fake_run_connector)

    # Override the RAG dependency so no Azure client is constructed.
    class _FakeRag:
        pass

    ingestion_app.dependency_overrides[admin_routes.get_rag_instance] = lambda: _FakeRag()

    with TestClient(ingestion_app) as client:
        _attach_root_path(client, ingestion_app.root_path)
        yield client

    ingestion_app.dependency_overrides.clear()
    run_status_store.clear()


def admin_headers() -> dict:
    """Header carrying the valid admin token configured in the env."""
    return {"X-Admin-Token": ADMIN_TOKEN}
