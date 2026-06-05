"""Tests for the /upload path-containment guard (CodeQL py/path-injection)
and the folder-mode stack-trace fix (CodeQL py/stack-trace-exposure).

Mirrors the pattern in `test_error_contract.py`: imports the standalone
`path_safety` module directly (no `app.py`, which pulls heavy deps via
`custom_rag`) and also mounts the guard on a minimal FastAPI app to prove the
rejection routes through the structured error envelope.
"""

import pathlib
import sys

import pytest
from fastapi import FastAPI, Query
from fastapi.testclient import TestClient

_INGESTION_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_INGESTION_ROOT) not in sys.path:
    sys.path.insert(0, str(_INGESTION_ROOT))

from errors import ApiErrorCode, register_exception_handlers  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from path_safety import ALLOWED_UPLOAD_ROOT_ENV, resolve_upload_path  # noqa: E402


class TestResolveUploadPathUnit:
    def test_valid_in_root_path_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ALLOWED_UPLOAD_ROOT_ENV, str(tmp_path))
        sub = tmp_path / "docs"
        sub.mkdir()
        # An absolute path that resolves inside the root is allowed.
        resolved = resolve_upload_path(str(sub))
        assert resolved == str(sub.resolve())

    def test_relative_in_root_path_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ALLOWED_UPLOAD_ROOT_ENV, str(tmp_path))
        (tmp_path / "docs").mkdir()
        resolved = resolve_upload_path("docs")
        assert resolved == str((tmp_path / "docs").resolve())

    def test_root_itself_is_accepted(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ALLOWED_UPLOAD_ROOT_ENV, str(tmp_path))
        assert resolve_upload_path(str(tmp_path)) == str(tmp_path.resolve())

    def test_dotdot_traversal_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ALLOWED_UPLOAD_ROOT_ENV, str(tmp_path))
        with pytest.raises(HTTPException) as exc:
            resolve_upload_path("../../etc/passwd")
        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == ApiErrorCode.INVALID_REQUEST

    def test_absolute_path_outside_root_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ALLOWED_UPLOAD_ROOT_ENV, str(tmp_path))
        with pytest.raises(HTTPException) as exc:
            resolve_upload_path("/etc/passwd")
        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == ApiErrorCode.INVALID_REQUEST

    def test_sibling_prefix_dir_is_rejected(self, tmp_path, monkeypatch):
        """Guards the `/app` vs `/app-evil` prefix bug — a sibling whose name
        starts with the root path string must not be treated as contained."""
        root = tmp_path / "app"
        root.mkdir()
        evil = tmp_path / "app-evil"
        evil.mkdir()
        monkeypatch.setenv(ALLOWED_UPLOAD_ROOT_ENV, str(root))
        with pytest.raises(HTTPException) as exc:
            resolve_upload_path(str(evil))
        assert exc.value.status_code == 400

    def test_escaping_symlink_is_rejected(self, tmp_path, monkeypatch):
        """realpath resolution must follow symlinks so a link inside the root
        pointing outside it is rejected (symlink-safe containment)."""
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        link = root / "escape"
        link.symlink_to(outside)
        monkeypatch.setenv(ALLOWED_UPLOAD_ROOT_ENV, str(root))
        with pytest.raises(HTTPException) as exc:
            resolve_upload_path("escape")
        assert exc.value.status_code == 400

    def test_empty_path_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ALLOWED_UPLOAD_ROOT_ENV, str(tmp_path))
        with pytest.raises(HTTPException) as exc:
            resolve_upload_path("   ")
        assert exc.value.status_code == 400


class TestResolveUploadPathEnvelope:
    """Mount the guard on a minimal app + the real handlers to prove a
    rejection is returned as the structured envelope, leaking no internals."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch) -> TestClient:
        monkeypatch.setenv(ALLOWED_UPLOAD_ROOT_ENV, str(tmp_path))
        a = FastAPI()
        register_exception_handlers(a)

        @a.get("/probe")
        def probe(filepath: str = Query(...)):
            resolved = resolve_upload_path(filepath)
            return {"status": "ok", "resolved": resolved}

        return TestClient(a, raise_server_exceptions=False)

    def test_traversal_returns_envelope(self, client: TestClient):
        r = client.get("/probe", params={"filepath": "../../etc/passwd"})
        assert r.status_code == 400
        body = r.json()
        assert body["error"]["code"] == ApiErrorCode.INVALID_REQUEST
        assert "detail" not in body
        assert body["error"]["request_id"] == r.headers["X-Request-ID"]

    def test_absolute_outside_root_returns_envelope(self, client: TestClient):
        r = client.get("/probe", params={"filepath": "/etc/passwd"})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == ApiErrorCode.INVALID_REQUEST

    def test_rejection_leaks_no_internal_detail(self, client: TestClient):
        r = client.get("/probe", params={"filepath": "/etc/passwd"})
        # No path internals, exception class names, or stack frames in the body.
        assert "/etc/passwd" not in r.text
        assert "Traceback" not in r.text
        assert "ValueError" not in r.text

    def test_valid_in_root_path_passes_through(self, tmp_path, client: TestClient):
        (tmp_path / "docs").mkdir()
        r = client.get("/probe", params={"filepath": "docs"})
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


class TestUploadEndpointFolderMode:
    """End-to-end against the real `app.py` /upload route. `custom_rag` is
    stubbed in sys.modules so the module's `rag_instance = custom_rag.rag()`
    returns a mock and the heavy deps (PyMuPDF, langchain) are never imported.

    Verifies (a) the guard rejects traversal on the live endpoint with the
    envelope, and (b) folder-mode per-file failures return the generic
    "Failed to process file." message — the raw exception text never reaches
    the client (CodeQL py/stack-trace-exposure)."""

    @pytest.fixture
    def app_client(self, tmp_path, monkeypatch):
        import types
        from unittest import mock

        monkeypatch.setenv(ALLOWED_UPLOAD_ROOT_ENV, str(tmp_path))

        # Stub `custom_rag` before importing `app` so no heavy deps load.
        # `rag` must be a class: `app.py` calls `custom_rag.rag()` and
        # `admin/routes.py` uses `custom_rag.rag | None` as a type annotation
        # (evaluated eagerly), so a bare lambda would break the `|` operator.
        stub = types.ModuleType("custom_rag")
        rag_mock = mock.MagicMock()

        class _RagStub:
            def __new__(cls):
                return rag_mock

        stub.rag = _RagStub
        monkeypatch.setitem(sys.modules, "custom_rag", stub)
        # Ensure a fresh import of `app` that binds to the stub.
        monkeypatch.delitem(sys.modules, "app", raising=False)

        import app as app_module

        return TestClient(app_module.app, raise_server_exceptions=False), rag_mock

    def test_folder_mode_traversal_rejected_with_envelope(self, app_client):
        client, _ = app_client
        r = client.post(
            "/upload",
            params={"bot_tag": "t1", "filepath": "../../etc"},
        )
        assert r.status_code == 400
        body = r.json()
        assert body["error"]["code"] == ApiErrorCode.INVALID_REQUEST
        assert "detail" not in body

    def test_folder_mode_per_file_error_is_generic(self, tmp_path, app_client):
        client, rag_mock = app_client
        # A real in-root folder containing a PDF.
        folder = tmp_path / "batch"
        folder.mkdir()
        (folder / "a.pdf").write_bytes(b"%PDF-1.4 fake")

        # Make the pipeline raise with sensitive text in the message.
        async def _boom(*args, **kwargs):
            raise RuntimeError("secret stacktrace detail token-xyz789")

        rag_mock.upload = _boom

        r = client.post(
            "/upload",
            params={"bot_tag": "t1", "filepath": "batch"},
        )
        assert r.status_code == 200
        results = r.json()
        assert isinstance(results, list) and len(results) == 1
        entry = results[0]
        # Shape preserved: same keys, generic value.
        assert set(entry.keys()) == {"file", "status", "error"}
        assert entry["status"] == "error"
        assert entry["error"] == "Failed to process file."
        # No raw exception text or class name leaked anywhere in the body.
        assert "secret stacktrace detail" not in r.text
        assert "token-xyz789" not in r.text
        assert "RuntimeError" not in r.text
