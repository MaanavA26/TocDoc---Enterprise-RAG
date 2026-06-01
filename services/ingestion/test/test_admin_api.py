"""Tests for the admin API (Phase 2 Workstream A PR-1).

These tests exercise:
1. The route layer (auth, validation, error mapping, response shape) using a
   minimal FastAPI app + a mocked `SearchAdminService` via `dependency_overrides`.
2. The service layer (pagination, grouping, OData escape) using a mocked
   `SearchClient`.

No real Azure clients are constructed; the tests must run on any machine
without network access.
"""

import os
import pathlib
import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Test environment setup — must run BEFORE importing the admin package.
# ---------------------------------------------------------------------------
os.environ["ADMIN_API_TOKEN"] = "test-admin-token"
os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "https://test.search.windows.net")
os.environ.setdefault("AZURE_SEARCH_KEY", "test-key")
os.environ.setdefault("INDEX_NAME", "test-index")

# Make the `admin` package importable when running pytest from
# `services/ingestion/`.
_INGESTION_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_INGESTION_ROOT) not in sys.path:
    sys.path.insert(0, str(_INGESTION_ROOT))

from admin.models import (  # noqa: E402
    ChunkSample,
    DocumentDetailResponse,
    DocumentListResponse,
    DocumentSummary,
    IndexStatsResponse,
)
from admin.routes import router  # noqa: E402
from admin.search_admin_service import (  # noqa: E402
    SearchAdminService,
    get_admin_service,
)
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_svc() -> MagicMock:
    """A mock `SearchAdminService` for route-layer tests."""
    return MagicMock(spec=SearchAdminService)


@pytest.fixture
def client(mock_svc: MagicMock) -> TestClient:
    """A minimal FastAPI app with only the admin router mounted, mocked service."""
    app = FastAPI()
    app.include_router(router, prefix="/admin")
    app.dependency_overrides[get_admin_service] = lambda: mock_svc
    return TestClient(app)


VALID_HEADERS = {"X-Admin-Token": "test-admin-token"}


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


class TestAdminServiceMisconfiguration:
    """When required search env vars are missing, the admin dependency must
    return a clean 503 — NOT escape as a generic 500 via RuntimeError."""

    def test_missing_search_env_returns_503(self, monkeypatch: pytest.MonkeyPatch):
        # Reset the module-level singleton so get_admin_service re-evaluates env.
        import admin.search_admin_service as svc_module

        monkeypatch.setattr(svc_module, "_service_singleton", None)

        # Unset the required vars
        monkeypatch.delenv("AZURE_SEARCH_ENDPOINT", raising=False)
        monkeypatch.delenv("AZURE_SEARCH_KEY", raising=False)
        monkeypatch.delenv("INDEX_NAME", raising=False)

        from admin.search_admin_service import get_admin_service
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            get_admin_service()
        assert excinfo.value.status_code == 503
        assert "not configured" in excinfo.value.detail.lower()


class TestAdminTokenAuth:
    def test_missing_token_returns_401(self, client: TestClient):
        r = client.get("/admin/documents", params={"bot_tag": "client_a"})
        assert r.status_code == 401
        assert "Invalid or missing admin token" in r.json()["detail"]

    def test_wrong_token_returns_401(self, client: TestClient):
        r = client.get(
            "/admin/documents",
            params={"bot_tag": "client_a"},
            headers={"X-Admin-Token": "definitely-not-the-real-token"},
        )
        assert r.status_code == 401

    def test_unset_server_token_returns_503(self, client: TestClient, monkeypatch: pytest.MonkeyPatch):
        """When ADMIN_API_TOKEN is unset on the server we refuse rather than bypass."""
        monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)
        r = client.get("/admin/documents", params={"bot_tag": "client_a"}, headers=VALID_HEADERS)
        assert r.status_code == 503
        assert "Admin API not configured" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Input validation tests
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_missing_bot_tag_returns_422(self, client: TestClient):
        r = client.get("/admin/documents", headers=VALID_HEADERS)
        assert r.status_code == 422

    def test_invalid_bot_tag_with_quote_returns_422(self, client: TestClient):
        r = client.get(
            "/admin/documents",
            params={"bot_tag": "client'; DROP TABLE--"},
            headers=VALID_HEADERS,
        )
        assert r.status_code == 422

    def test_invalid_bot_tag_with_space_returns_422(self, client: TestClient):
        r = client.get(
            "/admin/documents",
            params={"bot_tag": "has space"},
            headers=VALID_HEADERS,
        )
        assert r.status_code == 422

    def test_bot_tag_too_long_returns_422(self, client: TestClient):
        r = client.get(
            "/admin/documents",
            params={"bot_tag": "a" * 129},
            headers=VALID_HEADERS,
        )
        assert r.status_code == 422

    def test_invalid_document_id_with_quote_returns_422(self, client: TestClient):
        r = client.get(
            "/admin/documents/abc'def",
            params={"bot_tag": "client_a"},
            headers=VALID_HEADERS,
        )
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Route-layer behavior tests (mocked SearchAdminService)
# ---------------------------------------------------------------------------


class TestListDocuments:
    def test_returns_grouped_response(self, client: TestClient, mock_svc: MagicMock):
        mock_svc.list_documents.return_value = DocumentListResponse(
            bot_tag="client_a",
            count=2,
            documents=[
                DocumentSummary(
                    document_id="doc1",
                    source_path="handbook.pdf",
                    source_type="upload",
                    fr_tag="layout",
                    chunk_count=12,
                    first_ingested_at="2026-05-09T09:00:00Z",
                    last_ingested_at="2026-05-09T09:00:00Z",
                ),
                DocumentSummary(
                    document_id="doc2",
                    source_path="policy.pdf",
                    source_type="upload",
                    fr_tag="read",
                    chunk_count=4,
                    first_ingested_at="2026-05-09T10:00:00Z",
                    last_ingested_at="2026-05-09T10:00:00Z",
                ),
            ],
        )
        r = client.get(
            "/admin/documents",
            params={"bot_tag": "client_a"},
            headers=VALID_HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["bot_tag"] == "client_a"
        assert body["count"] == 2
        assert len(body["documents"]) == 2
        assert body["documents"][0]["chunk_count"] == 12
        mock_svc.list_documents.assert_called_once_with("client_a")


class TestGetDocument:
    def test_returns_detail_when_found(self, client: TestClient, mock_svc: MagicMock):
        mock_svc.get_document.return_value = DocumentDetailResponse(
            bot_tag="client_a",
            document_id="abc123",
            source_path="handbook.pdf",
            source_type="upload",
            fr_tag="layout",
            chunk_count=24,
            ingestion_timestamps=["2026-05-09T09:00:00Z"],
            sample_chunks=[ChunkSample(id="client_a_abc123_layout_00000", chunk_index=0)],
        )
        r = client.get(
            "/admin/documents/abc123",
            params={"bot_tag": "client_a"},
            headers=VALID_HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["document_id"] == "abc123"
        assert body["chunk_count"] == 24

    def test_returns_404_when_doc_in_different_bot_tag(self, client: TestClient, mock_svc: MagicMock):
        # Service returns None when filter matches nothing.
        mock_svc.get_document.return_value = None
        r = client.get(
            "/admin/documents/abc123",
            params={"bot_tag": "different_tenant"},
            headers=VALID_HEADERS,
        )
        assert r.status_code == 404
        # Critically, the service must be called with the (bot_tag, document_id)
        # pair the caller provided, NOT a relaxed filter.
        mock_svc.get_document.assert_called_once_with("different_tenant", "abc123")


class TestIndexStats:
    def test_returns_stats(self, client: TestClient, mock_svc: MagicMock):
        mock_svc.get_index_stats.return_value = IndexStatsResponse(
            bot_tag="client_a",
            document_count=12,
            chunk_count=540,
            source_types={"upload": 12},
            fr_modes={"layout": 8, "read": 4},
        )
        r = client.get(
            "/admin/index/stats",
            params={"bot_tag": "client_a"},
            headers=VALID_HEADERS,
        )
        assert r.status_code == 200
        body = r.json()
        assert body["document_count"] == 12
        assert body["chunk_count"] == 540
        assert body["source_types"] == {"upload": 12}
        assert body["fr_modes"] == {"layout": 8, "read": 4}


# ---------------------------------------------------------------------------
# Service layer tests (mocked SearchClient)
# ---------------------------------------------------------------------------


def _make_paged_search_result(pages: list[list[dict]]) -> MagicMock:
    """Build a mock search result whose `.by_page()` yields the given pages."""
    mock_result = MagicMock()
    mock_result.by_page.return_value = iter([iter(p) for p in pages])
    return mock_result


class TestPagination:
    """Verify the service walks past the 1000-item per-page limit AND does not
    rely on `top=` (which the SDK may interpret as a total cap)."""

    def test_search_call_does_not_pass_top_parameter(self):
        """`top` in azure-search-documents corresponds to OData $top, which
        is ambiguous between "items per page" and "total cap". Some service
        behaviors silently truncate when `top` is set. The service MUST rely
        on `.by_page()` continuation-token pagination instead — any test run
        with `top` in the search kwargs fails this check.
        """
        mock_client = MagicMock()
        mock_client.search.return_value = _make_paged_search_result([[]])
        svc = SearchAdminService(mock_client)
        svc.list_documents("client_a")

        kwargs = mock_client.search.call_args.kwargs
        assert "top" not in kwargs, (
            f"SearchAdminService passed `top` to search(): {kwargs.get('top')!r}. "
            "Use .by_page() pagination only — `top` may silently cap total results."
        )

    def test_walks_all_pages_for_2500_chunks(self):
        # Simulate 2500 chunks split as 1000 + 1000 + 500 across 3 pages.
        # All chunks are part of the same bot_tag scope; spread across 3 docs.
        page1 = [
            {
                "document_id": "doc_a",
                "ingestion_timestamp": "2026-05-09T09:00:00Z",
                "source_type": "upload",
                "fr_tag": "fr_read",
                "source_path": "a.pdf",
            }
            for _ in range(1000)
        ]
        page2 = [
            {
                "document_id": "doc_b",
                "ingestion_timestamp": "2026-05-09T09:00:00Z",
                "source_type": "upload",
                "fr_tag": "fr_read",
                "source_path": "b.pdf",
            }
            for _ in range(1000)
        ]
        page3 = [
            {
                "document_id": "doc_c",
                "ingestion_timestamp": "2026-05-09T09:00:00Z",
                "source_type": "upload",
                "fr_tag": "fr_layout",
                "source_path": "c.pdf",
            }
            for _ in range(500)
        ]

        mock_client = MagicMock()
        mock_client.search.return_value = _make_paged_search_result([page1, page2, page3])

        svc = SearchAdminService(mock_client)
        result = svc.list_documents("client_a")

        assert result.count == 3
        chunk_total = sum(d.chunk_count for d in result.documents)
        assert chunk_total == 2500, f"Expected to visit all 2500 chunks across pages, got {chunk_total}"

    def test_index_stats_counts_per_document_not_per_chunk(self):
        # 5 chunks across 2 documents. source_types/fr_modes should count docs not chunks.
        chunks = [
            {"document_id": "d1", "source_type": "upload", "fr_tag": "fr_read"},
            {"document_id": "d1", "source_type": "upload", "fr_tag": "fr_read"},
            {"document_id": "d1", "source_type": "upload", "fr_tag": "fr_read"},
            {"document_id": "d2", "source_type": "upload", "fr_tag": "fr_layout"},
            {"document_id": "d2", "source_type": "upload", "fr_tag": "fr_layout"},
        ]
        mock_client = MagicMock()
        mock_client.search.return_value = _make_paged_search_result([chunks])
        svc = SearchAdminService(mock_client)

        result = svc.get_index_stats("client_a")
        assert result.document_count == 2
        assert result.chunk_count == 5
        assert result.source_types == {"upload": 2}
        # fr_ prefix should be stripped to match spec response shape
        assert result.fr_modes == {"read": 1, "layout": 1}


class TestOdataEscape:
    """Defense-in-depth escape function — even though regex blocks quotes."""

    def test_normal_string_unchanged(self):
        assert SearchAdminService._escape_odata("normal_value") == "normal_value"

    def test_single_quote_doubled(self):
        assert SearchAdminService._escape_odata("a'b") == "a''b"

    def test_multiple_quotes_doubled(self):
        assert SearchAdminService._escape_odata("a'b'c") == "a''b''c"


class TestServiceFiltersByBoth:
    """get_document MUST scope by (bot_tag, document_id) jointly — never relax."""

    def test_filter_includes_both_bot_tag_and_document_id(self):
        mock_client = MagicMock()
        mock_client.search.return_value = _make_paged_search_result([[]])
        svc = SearchAdminService(mock_client)

        svc.get_document("client_a", "doc1")

        # Inspect the actual filter passed to SearchClient.search
        kwargs = mock_client.search.call_args.kwargs
        filter_expr = kwargs.get("filter") or (
            mock_client.search.call_args.args[0] if mock_client.search.call_args.args else None
        )
        # Should contain both clauses joined with `and`
        assert "bot_tag eq 'client_a'" in filter_expr
        assert "document_id eq 'doc1'" in filter_expr
        assert " and " in filter_expr


class TestChunkIndexExtraction:
    """Defensive parsing of chunk_index from the deterministic ID format."""

    def test_extracts_index_from_well_formed_id(self):
        assert SearchAdminService._extract_chunk_index("client_a_abc123_layout_00042") == 42

    def test_returns_none_for_malformed_id(self):
        assert SearchAdminService._extract_chunk_index("malformed") is None
        assert SearchAdminService._extract_chunk_index("") is None
        assert SearchAdminService._extract_chunk_index("a_b_notanumber") is None
