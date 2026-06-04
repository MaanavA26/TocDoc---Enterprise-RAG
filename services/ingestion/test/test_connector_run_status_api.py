"""GET /admin/connectors/runs/{run_id} (+ list) endpoint tests (P1-3 follow-up).

Covers:
- requires admin token (401 without).
- unknown run_id → 404 P0-6 ErrorEnvelope.
- after a (mocked) sync run, the status reflects completed + processed count.
- a mocked run that raises → status reflects failed + safe error summary.
- list endpoint returns recent runs newest-first.

No live Azure/Graph: run_connector is patched and get_rag_instance is overridden,
exactly like test_connector_trigger.py. The trigger schedules a SYNC background
task; TestClient executes BackgroundTasks after the response, so by the time the
POST returns the run-status store already reflects the run.
"""

import os
import pathlib
import sys

import pytest

os.environ["ADMIN_API_TOKEN"] = "test-admin-token"
os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "https://test.search.windows.net")
os.environ.setdefault("AZURE_SEARCH_KEY", "test-key")
os.environ.setdefault("INDEX_NAME", "test-index")

_INGESTION_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_INGESTION_ROOT) not in sys.path:
    sys.path.insert(0, str(_INGESTION_ROOT))

import admin.routes as routes_module  # noqa: E402
from admin.routes import get_rag_instance, router  # noqa: E402
from connectors.run_status import run_status_store  # noqa: E402
from errors import register_exception_handlers  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from observability import RequestIDMiddleware  # noqa: E402

VALID_HEADERS = {"X-Admin-Token": "test-admin-token"}


@pytest.fixture(autouse=True)
def _clean_store():
    """Each test starts with an empty run-status store (module singleton)."""
    run_status_store.clear()
    yield
    run_status_store.clear()


@pytest.fixture
def fake_rag():
    class _FakeRag:
        pass

    return _FakeRag()


@pytest.fixture
def client(fake_rag):
    app = FastAPI()
    app.include_router(router, prefix="/admin")
    app.dependency_overrides[get_rag_instance] = lambda: fake_rag
    app.add_middleware(RequestIDMiddleware)
    register_exception_handlers(app)
    return TestClient(app)


def _set_blob_env(monkeypatch, *, bot_tag="tenant-x"):
    monkeypatch.setenv("CONNECTOR_BOT_TAG", bot_tag)
    monkeypatch.setenv("BLOB_CONTAINER", "mycontainer")
    monkeypatch.setenv(
        "BLOB_STORAGE_CONNECTION_STRING",
        "DefaultEndpointsProtocol=https;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMA==;EndpointSuffix=core.windows.net",
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_get_run_requires_admin_token(client):
    resp = client.get("/admin/connectors/runs/anyid")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


def test_list_runs_requires_admin_token(client):
    resp = client.get("/admin/connectors/runs")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# Unknown run_id → 404 envelope
# ---------------------------------------------------------------------------


def test_get_unknown_run_returns_404_envelope(client):
    resp = client.get("/admin/connectors/runs/does-not-exist", headers=VALID_HEADERS)
    assert resp.status_code == 404
    body = resp.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert "request_id" in body["error"]
    assert resp.headers.get("X-Request-ID")


# ---------------------------------------------------------------------------
# No just-created 404 race: the run is recorded `started` SYNCHRONOUSLY by the
# POST handler, before the background task runs. We neutralize the background
# task entirely so the ONLY thing that can record the run is the synchronous
# handler call — on the old code (record_started lived in the background task)
# this GET would 404; on the fixed code it is 200 + "started".
# ---------------------------------------------------------------------------


def test_just_created_run_not_404_before_background_runs(client, monkeypatch):
    _set_blob_env(monkeypatch)

    # No-op the background driver so nothing but the synchronous handler can
    # record this run. Bare-name lookup in the handler resolves from module
    # globals at call time, so this patch takes effect.
    monkeypatch.setattr(routes_module, "_run_connector_background", lambda *a, **k: None)

    resp = client.post("/admin/connectors/blob/sync", headers=VALID_HEADERS)
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    status_resp = client.get(f"/admin/connectors/runs/{run_id}", headers=VALID_HEADERS)
    # Recorded synchronously before the 202 — never a just-created 404.
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["run_id"] == run_id
    assert body["status"] == "started"
    assert body["source_type"] == "blob"
    assert body["bot_tag"] == "tenant-x"
    assert body["finished_at"] is None


# ---------------------------------------------------------------------------
# After a mocked sync run, status reflects completed + counts
# ---------------------------------------------------------------------------


def test_status_reflects_completed_after_run(client, monkeypatch):
    _set_blob_env(monkeypatch)

    async def _fake_run_connector(connector, rag_instance, *, run_id=None):
        return {"processed": 4, "items": ["blob://mycontainer/a.pdf"] * 4}

    monkeypatch.setattr(routes_module, "run_connector", _fake_run_connector)

    resp = client.post("/admin/connectors/blob/sync", headers=VALID_HEADERS)
    assert resp.status_code == 202
    run_id = resp.json()["run_id"]

    # Background task has run by now (TestClient executes BackgroundTasks).
    status_resp = client.get(f"/admin/connectors/runs/{run_id}", headers=VALID_HEADERS)
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["run_id"] == run_id
    assert body["status"] == "completed"
    assert body["source_type"] == "blob"
    assert body["bot_tag"] == "tenant-x"
    assert body["processed_count"] == 4
    assert body["failed_count"] == 0
    assert body["finished_at"] is not None
    assert body["error"] is None


def test_status_reflects_failed_after_run_raises(client, monkeypatch):
    _set_blob_env(monkeypatch)

    async def _boom(connector, rag_instance, *, run_id=None):
        raise RuntimeError("internal detail that must not surface")

    monkeypatch.setattr(routes_module, "run_connector", _boom)

    resp = client.post("/admin/connectors/blob/sync", headers=VALID_HEADERS)
    run_id = resp.json()["run_id"]

    status_resp = client.get(f"/admin/connectors/runs/{run_id}", headers=VALID_HEADERS)
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] == "failed"
    # Safe error summary only — class name + generic message, never raw text.
    assert body["error"]["error_class"] == "RuntimeError"
    assert body["error"]["safe_message"] == "Connector sync run failed"
    assert "internal detail" not in body["error"]["safe_message"]


def test_list_recent_runs(client, monkeypatch):
    _set_blob_env(monkeypatch)

    async def _fake_run_connector(connector, rag_instance, *, run_id=None):
        return {"processed": 1, "items": []}

    monkeypatch.setattr(routes_module, "run_connector", _fake_run_connector)

    run_ids = []
    for _ in range(3):
        resp = client.post("/admin/connectors/blob/sync", headers=VALID_HEADERS)
        run_ids.append(resp.json()["run_id"])

    list_resp = client.get("/admin/connectors/runs", headers=VALID_HEADERS)
    assert list_resp.status_code == 200
    body = list_resp.json()
    assert body["count"] == 3
    # Newest first.
    returned = [r["run_id"] for r in body["runs"]]
    assert returned == list(reversed(run_ids))
