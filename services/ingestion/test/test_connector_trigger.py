"""PR-5 operator connector-sync trigger endpoint tests.

POST /admin/connectors/{source_type}/sync — in-stack, behind require_admin_token.

Covers:
- requires admin token (401 without; 503 when server unconfigured).
- bad source_type → 400 P0-6 ErrorEnvelope.
- valid request → 202 with {run_id, source_type, status:"started"} and the
  background task is scheduled (mocked run_connector is invoked with the
  connector + run_id).
- missing connector config → 400 ErrorEnvelope (no background task scheduled).
- bot_tag scoping: the connector is built from env CONNECTOR_BOT_TAG and that
  exact bot_tag reaches run_connector — never read from the request.
- an invalid bot_tag in env → 400 ErrorEnvelope (ConnectorConfig rejects it).

No live Azure/Graph: run_connector is patched and get_rag_instance is
overridden. For the SharePoint happy path, SharePointConnector._build_client is
stubbed (it would otherwise call ClientSecretCredential.get_token eagerly at
init); for the Blob happy path a fake connection string lets the ContainerClient
construct without any network call. Config-error cases short-circuit before any
client is built.
"""

import os
import pathlib
import sys

import pytest

# Admin auth + search env must be set before importing the admin package.
os.environ["ADMIN_API_TOKEN"] = "test-admin-token"
os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "https://test.search.windows.net")
os.environ.setdefault("AZURE_SEARCH_KEY", "test-key")
os.environ.setdefault("INDEX_NAME", "test-index")

_INGESTION_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_INGESTION_ROOT) not in sys.path:
    sys.path.insert(0, str(_INGESTION_ROOT))

import admin.routes as routes_module  # noqa: E402
from admin.routes import get_rag_instance, router  # noqa: E402
from errors import register_exception_handlers  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from observability import RequestIDMiddleware  # noqa: E402

VALID_HEADERS = {"X-Admin-Token": "test-admin-token"}


@pytest.fixture
def fake_rag():
    """A stand-in RAG instance — never actually called (run_connector mocked)."""

    class _FakeRag:
        pass

    return _FakeRag()


@pytest.fixture
def captured(monkeypatch):
    """Patch run_connector in the routes module and capture its invocation."""
    calls = {}

    async def _fake_run_connector(connector, rag_instance, *, run_id=None):
        calls["connector"] = connector
        calls["rag_instance"] = rag_instance
        calls["run_id"] = run_id
        return {"processed": 0, "items": []}

    monkeypatch.setattr(routes_module, "run_connector", _fake_run_connector)
    return calls


@pytest.fixture
def client(fake_rag):
    """Test app: admin router + P0-6 handlers + RequestIDMiddleware (in-stack)."""
    app = FastAPI()
    app.include_router(router, prefix="/admin")
    app.dependency_overrides[get_rag_instance] = lambda: fake_rag
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)
    return TestClient(app)


def _set_sharepoint_env(monkeypatch, *, bot_tag="tenant-x"):
    monkeypatch.setenv("CONNECTOR_BOT_TAG", bot_tag)
    monkeypatch.setenv("CONNECTOR_FR_MODE", "read")
    monkeypatch.setenv("SHAREPOINT_SITE_ID", "site-1")
    monkeypatch.setenv("SHAREPOINT_DRIVE_ID", "drive-1")
    monkeypatch.setenv("SHAREPOINT_TENANT_ID", "tenant")
    monkeypatch.setenv("SHAREPOINT_CLIENT_ID", "client")
    monkeypatch.setenv("SHAREPOINT_CLIENT_SECRET", "secret")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_sync_requires_admin_token(client, monkeypatch):
    _set_sharepoint_env(monkeypatch)
    resp = client.post("/admin/connectors/sharepoint/sync")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# Bad source_type → 400 envelope
# ---------------------------------------------------------------------------


def test_sync_bad_source_type_returns_400_envelope(client, monkeypatch, captured):
    _set_sharepoint_env(monkeypatch)
    resp = client.post("/admin/connectors/ftp/sync", headers=VALID_HEADERS)
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "INVALID_REQUEST"
    assert "request_id" in body["error"]
    # No background run was scheduled for an invalid source_type.
    assert "connector" not in captured


# ---------------------------------------------------------------------------
# Missing config → 400 envelope
# ---------------------------------------------------------------------------


def test_sync_missing_bot_tag_returns_400_envelope(client, monkeypatch, captured):
    monkeypatch.delenv("CONNECTOR_BOT_TAG", raising=False)
    monkeypatch.setenv("SHAREPOINT_SITE_ID", "site-1")
    monkeypatch.setenv("SHAREPOINT_DRIVE_ID", "drive-1")
    resp = client.post("/admin/connectors/sharepoint/sync", headers=VALID_HEADERS)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"
    assert "connector" not in captured


def test_sync_missing_site_drive_returns_400_envelope(client, monkeypatch, captured):
    monkeypatch.setenv("CONNECTOR_BOT_TAG", "tenant-x")
    monkeypatch.delenv("SHAREPOINT_SITE_ID", raising=False)
    monkeypatch.delenv("SHAREPOINT_DRIVE_ID", raising=False)
    resp = client.post("/admin/connectors/sharepoint/sync", headers=VALID_HEADERS)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"
    assert "connector" not in captured


def test_sync_invalid_bot_tag_in_env_returns_400_envelope(client, monkeypatch, captured):
    """An env bot_tag failing BOT_TAG_PATTERN is rejected at construction."""
    _set_sharepoint_env(monkeypatch, bot_tag="bad tag with spaces")
    resp = client.post("/admin/connectors/sharepoint/sync", headers=VALID_HEADERS)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_REQUEST"
    assert "connector" not in captured


# ---------------------------------------------------------------------------
# Happy path → 202, run scheduled, bot_tag scoping enforced
# ---------------------------------------------------------------------------


def test_sync_valid_returns_202_and_schedules_run(client, monkeypatch, captured, fake_rag):
    _set_sharepoint_env(monkeypatch, bot_tag="tenant-x")
    # Stub the Graph client build so construction succeeds without live auth.
    import httpx
    from connectors.sharepoint import SharePointConnector

    monkeypatch.setattr(
        SharePointConnector,
        "_build_client",
        lambda self: httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
    )
    resp = client.post("/admin/connectors/sharepoint/sync", headers=VALID_HEADERS)
    assert resp.status_code == 202
    body = resp.json()
    assert body["source_type"] == "sharepoint"
    assert body["status"] == "started"
    assert body["run_id"]
    # X-Request-ID is present on the response (inherited middleware).
    assert resp.headers.get("X-Request-ID")

    # Background task ran after the response (TestClient executes BackgroundTasks).
    assert "connector" in captured, "run_connector was not scheduled"
    connector = captured["connector"]
    assert connector.source_type == "sharepoint"
    # bot_tag scoping: the connector binds the env bot_tag, never a request value.
    assert connector.bot_tag == "tenant-x"
    assert captured["rag_instance"] is fake_rag
    # The generated run_id threaded into the background run matches the response.
    assert captured["run_id"] == body["run_id"]


def test_sync_blob_source_type_supported(client, monkeypatch, captured):
    """source_type=blob is accepted; construction uses env config too."""
    monkeypatch.setenv("CONNECTOR_BOT_TAG", "tenant-x")
    monkeypatch.setenv("BLOB_CONTAINER", "mycontainer")
    # Avoid building a real ContainerClient: inject a fake via BlobConnector?
    # _build_connector calls BlobConnector(config, container) which builds a
    # client from env. Provide a connection string so construction succeeds
    # without network (no listing happens — run_connector is mocked).
    monkeypatch.setenv(
        "BLOB_STORAGE_CONNECTION_STRING",
        "DefaultEndpointsProtocol=https;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMA==;EndpointSuffix=core.windows.net",
    )
    resp = client.post("/admin/connectors/blob/sync", headers=VALID_HEADERS)
    assert resp.status_code == 202
    assert resp.json()["source_type"] == "blob"
    assert captured["connector"].source_type == "blob"
    assert captured["connector"].bot_tag == "tenant-x"


def test_sync_unconfigured_admin_returns_503(monkeypatch, fake_rag):
    """No ADMIN_API_TOKEN → 503 (refuse rather than bypass auth)."""
    monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(router, prefix="/admin")
    app.dependency_overrides[get_rag_instance] = lambda: fake_rag
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)
    c = TestClient(app)
    resp = c.post("/admin/connectors/sharepoint/sync", headers=VALID_HEADERS)
    assert resp.status_code == 503
