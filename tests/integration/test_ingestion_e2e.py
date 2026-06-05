"""End-to-end integration tests for the ingestion service.

Exercise the REAL ``services/ingestion/app.py:app`` in-process via ``TestClient``.
The connector run path is mocked at the ``run_connector`` seam and the RAG
instance is overridden via FastAPI ``dependency_overrides``; admin Search reads
are mocked via the ``get_admin_service`` dependency. No live Azure / Graph, no
network.
"""

from __future__ import annotations

from conftest import admin_headers


# --------------------------------------------------------------------------- #
# /health (public)
# --------------------------------------------------------------------------- #
def test_health_ok(ingestion_client):
    r = ingestion_client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy"}


# --------------------------------------------------------------------------- #
# /upload — admin token required
# --------------------------------------------------------------------------- #
def test_upload_requires_admin_token_401(ingestion_client):
    """/upload without X-Admin-Token → 401 (the H2 fix)."""
    r = ingestion_client.post(
        "/upload",
        params={"bot_tag": "tenant-x", "filepath": "doc.pdf"},
    )
    assert r.status_code == 401


def test_upload_wrong_admin_token_401(ingestion_client):
    r = ingestion_client.post(
        "/upload",
        params={"bot_tag": "tenant-x", "filepath": "doc.pdf"},
        headers={"X-Admin-Token": "wrong"},
    )
    assert r.status_code == 401


def test_upload_with_admin_token_reaches_route_200(ingestion_client, tmp_path, monkeypatch):
    """With a valid admin token, /upload passes auth and ingests a file.

    ``rag_instance.upload`` is patched so no Azure Document Intelligence /
    embedding / index call runs; the path-containment guard is pointed at a
    temp dir so ``resolve_upload_path`` accepts the file. A 200 here proves the
    full assembled stack (admin auth + size middleware + route) is exercised.
    """
    import sys

    # The real ingestion app module was loaded under this unique name by the
    # conftest loader; patch the singleton RAG instance it actually uses.
    ingestion_app_module = sys.modules["ingestion_app_under_test"]

    # Point the allowed upload root at a temp dir and place a supported file in it.
    monkeypatch.setenv("INGESTION_ALLOWED_UPLOAD_ROOT", str(tmp_path))
    doc = tmp_path / "doc.txt"
    doc.write_text("hello world")

    async def _fake_upload(file, bot_tag, fr_mode, filepath, request_id=None):
        return {"status": "ok", "indexed_chunks": 1}

    monkeypatch.setattr(ingestion_app_module.rag_instance, "upload", _fake_upload)

    with open(doc, "rb") as fh:
        r = ingestion_client.post(
            "/upload",
            params={"bot_tag": "tenant-x", "filepath": "doc.txt", "fr_mode": "read"},
            headers=admin_headers(),
            files={"file": ("doc.txt", fh, "text/plain")},
        )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "successfully indexed"


def test_upload_unsupported_type_415(ingestion_client, tmp_path, monkeypatch):
    """A valid admin token but an unsupported extension → clean 415, not 500."""
    monkeypatch.setenv("INGESTION_ALLOWED_UPLOAD_ROOT", str(tmp_path))
    blob = tmp_path / "data.bin"
    blob.write_bytes(b"\x00\x01")
    with open(blob, "rb") as fh:
        r = ingestion_client.post(
            "/upload",
            params={"bot_tag": "tenant-x", "filepath": "data.bin"},
            headers=admin_headers(),
            files={"file": ("data.bin", fh, "application/octet-stream")},
        )
    assert r.status_code == 415
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


# --------------------------------------------------------------------------- #
# Admin read endpoints
# --------------------------------------------------------------------------- #
def test_admin_documents_requires_token_401(ingestion_client):
    r = ingestion_client.get("/admin/documents", params={"bot_tag": "tenant-x"})
    assert r.status_code == 401


def test_admin_documents_read_ok(ingestion_client, ingestion_app, monkeypatch):
    """Authed admin read returns the mocked SearchAdminService result."""
    from unittest.mock import MagicMock

    import admin.routes as admin_routes
    from admin.models import DocumentListResponse, DocumentSummary
    from admin.search_admin_service import SearchAdminService, get_admin_service

    fake_svc = MagicMock(spec=SearchAdminService)
    fake_svc.list_documents.return_value = DocumentListResponse(
        bot_tag="tenant-x",
        count=1,
        documents=[DocumentSummary(document_id="d1", source_path="/d1.pdf", chunk_count=4)],
    )
    ingestion_app.dependency_overrides[get_admin_service] = lambda: fake_svc

    r = ingestion_client.get("/admin/documents", params={"bot_tag": "tenant-x"}, headers=admin_headers())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 1
    assert body["documents"][0]["document_id"] == "d1"

    ingestion_app.dependency_overrides.pop(get_admin_service, None)
    # Note: get_rag_instance override is restored by the ingestion_client fixture.
    assert admin_routes  # keep import referenced


def test_admin_bad_bot_tag_422(ingestion_client):
    """A bot_tag that violates the pattern → 422 (validated before any service)."""
    r = ingestion_client.get(
        "/admin/documents",
        params={"bot_tag": "bad tag!"},
        headers=admin_headers(),
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"


# --------------------------------------------------------------------------- #
# Connector sync trigger + run-status round-trip
# --------------------------------------------------------------------------- #
def _set_blob_env(monkeypatch, *, bot_tag="tenant-x"):
    monkeypatch.setenv("CONNECTOR_BOT_TAG", bot_tag)
    monkeypatch.setenv("CONNECTOR_FR_MODE", "read")
    monkeypatch.setenv("BLOB_CONTAINER", "mycontainer")
    # A well-formed (devstore) connection string so the BlobConnector's
    # ContainerClient constructs offline. No network call happens here —
    # ``run_connector`` (which would enumerate/fetch) is mocked.
    monkeypatch.setenv(
        "BLOB_STORAGE_CONNECTION_STRING",
        "DefaultEndpointsProtocol=https;AccountName=devstoreaccount1;"
        "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
        "K1SZFPTOtr/KBHBeksoGMA==;EndpointSuffix=core.windows.net",
    )


def test_connector_sync_requires_token_401(ingestion_client):
    r = ingestion_client.post("/admin/connectors/blob/sync")
    assert r.status_code == 401


def test_connector_sync_bad_source_type_400(ingestion_client):
    r = ingestion_client.post("/admin/connectors/ftp/sync", headers=admin_headers())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_REQUEST"


def test_connector_sync_round_trip_completed(ingestion_client, ingestion_run_state, monkeypatch):
    """Trigger a blob sync → 202 with run_id, then GET run-status shows completed.

    ``TestClient`` runs the SYNC background task after the response, so by the
    time the POST returns the in-process store already reflects the (mocked) run.
    """
    _set_blob_env(monkeypatch)
    ingestion_run_state["processed"] = 5

    r = ingestion_client.post("/admin/connectors/blob/sync", headers=admin_headers())
    assert r.status_code == 202, r.text
    trigger = r.json()
    run_id = trigger["run_id"]
    assert trigger["source_type"] == "blob"

    status = ingestion_client.get(f"/admin/connectors/runs/{run_id}", headers=admin_headers())
    assert status.status_code == 200, status.text
    rec = status.json()
    assert rec["run_id"] == run_id
    assert rec["status"] == "completed"
    assert rec["processed_count"] == 5

    # Run shows up in the list view too.
    listing = ingestion_client.get("/admin/connectors/runs", headers=admin_headers())
    assert listing.status_code == 200
    assert any(run["run_id"] == run_id for run in listing.json()["runs"])


def test_connector_sync_failure_records_failed(ingestion_client, ingestion_run_state, monkeypatch):
    """A run that raises → status reflects failed with a safe error summary."""
    _set_blob_env(monkeypatch)
    ingestion_run_state["raise"] = RuntimeError("connection string leaked here")

    r = ingestion_client.post("/admin/connectors/blob/sync", headers=admin_headers())
    assert r.status_code == 202
    run_id = r.json()["run_id"]

    status = ingestion_client.get(f"/admin/connectors/runs/{run_id}", headers=admin_headers())
    assert status.status_code == 200
    rec = status.json()
    assert rec["status"] == "failed"
    # The raw exception message must NOT leak into the recorded status.
    assert "leaked here" not in status.text


def test_connector_unknown_run_id_404(ingestion_client):
    r = ingestion_client.get("/admin/connectors/runs/nonexistentrun", headers=admin_headers())
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


def test_connector_missing_config_400(ingestion_client, monkeypatch):
    """Enforcement of env-derived connector config: missing bot_tag → 400."""
    monkeypatch.delenv("CONNECTOR_BOT_TAG", raising=False)
    r = ingestion_client.post("/admin/connectors/blob/sync", headers=admin_headers())
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_REQUEST"
