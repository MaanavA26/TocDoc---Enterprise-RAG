"""Service layer for admin operations against Azure Cognitive Search.

Owns all Azure Search interaction for the admin API: pagination, OData filter
construction, and aggregation of chunk-level data into document-level summaries.

Key invariants:
- Every query is filtered by `bot_tag` — we never read across tenants.
- Single quotes in OData filter values are escaped (defense in depth on top of
  the regex validation already enforced in the route layer).
- Pagination is handled explicitly via `.by_page()` so that document sets larger
  than the Azure Search per-page max (1000) are visited in full.

This module is synchronous; callers use `loop.run_in_executor(...)` to avoid
blocking the FastAPI event loop, matching the convention in
`services/qna/src/services/search_service.py`.
"""

import logging
import os
from typing import Iterable, Optional

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from fastapi import HTTPException, status

from .models import (
    ChunkSample,
    DocumentDetailResponse,
    DocumentListResponse,
    DocumentSummary,
    IndexStatsResponse,
)

logger = logging.getLogger(__name__)

# Fields fetched for each operation. Kept narrow so we never select content
# or vector fields (large payloads + nothing the admin needs).
_LIST_DOCS_SELECT = [
    "document_id",
    "source_path",
    "source_type",
    "fr_tag",
    "ingestion_timestamp",
]
_GET_DOC_SELECT = [
    "id",
    "document_id",
    "source_path",
    "source_type",
    "fr_tag",
    "ingestion_timestamp",
]
_STATS_SELECT = ["document_id", "source_type", "fr_tag"]

# NOTE on pagination:
# The Azure Cognitive Search REST API caps results at 1000 per page. The
# Python SDK exposes pagination via `SearchItemPaged.by_page()` which uses
# continuation tokens to fetch subsequent pages transparently.
#
# We deliberately do NOT pass `top=N` to `SearchClient.search(...)`. In the
# azure-search-documents SDK semantics, `top` corresponds to OData `$top`
# which is ambiguous between "items per page" and "total cap"; some service
# behaviors interpret it as a hard total cap, silently truncating results.
# Relying on `.by_page()` alone keeps us correct: every result that matches
# the filter is visited, with continuation handled by the SDK.


class SearchAdminService:
    """Read-only admin operations over Azure Cognitive Search."""

    def __init__(self, search_client: SearchClient) -> None:
        self._search_client = search_client

    @staticmethod
    def _escape_odata(value: str) -> str:
        """Escape single quotes in OData filter values per OData spec.

        Defense in depth: the route-layer regex validation should already
        reject single quotes in `bot_tag` and `document_id`, but we escape
        here too in case a future caller bypasses validation.
        """
        return value.replace("'", "''")

    @staticmethod
    def _strip_fr_prefix(fr_tag: Optional[str]) -> Optional[str]:
        """Strip the `fr_` prefix from an indexed `fr_tag` value.

        The index stores `fr_read` / `fr_layout`, but the admin API spec returns
        the bare mode name (`read` / `layout`) for cleaner consumer use.
        """
        if fr_tag and fr_tag.startswith("fr_"):
            return fr_tag[3:]
        return fr_tag

    @staticmethod
    def _extract_chunk_index(chunk_id: str) -> Optional[int]:
        """Parse the trailing zero-padded index from a deterministic chunk ID.

        Chunk IDs follow the format set in `custom_rag.py`:
            {bot_tag}_{document_id}_{mode}_{idx:05d}

        Returns None for malformed IDs so the caller can include the chunk
        sample without an index rather than failing the whole response.
        """
        try:
            tail = chunk_id.rsplit("_", 1)
            if len(tail) == 2:
                return int(tail[1])
        except (ValueError, AttributeError, TypeError):
            pass
        return None

    def _paged_search(
        self, filter_expr: str, select: list[str]
    ) -> Iterable[dict]:
        """Yield every result matching `filter_expr`, walking all pages.

        Pagination is handled entirely by `SearchItemPaged.by_page()` and the
        SDK's continuation-token mechanism. We do NOT pass `top` — see the
        module-level note above for why that parameter is unsafe here.
        """
        result = self._search_client.search(
            search_text="*",
            filter=filter_expr,
            select=select,
        )
        for page in result.by_page():
            for item in page:
                yield item

    def list_documents(self, bot_tag: str) -> DocumentListResponse:
        """Return one row per indexed document in the given bot_tag scope."""
        safe_tag = self._escape_odata(bot_tag)
        filter_expr = f"bot_tag eq '{safe_tag}'"

        docs: dict[str, dict] = {}
        for chunk in self._paged_search(filter_expr, _LIST_DOCS_SELECT):
            doc_id = chunk.get("document_id")
            if not doc_id:
                # Chunks indexed before P0-4 may lack document_id; skip rather
                # than fabricate a row.
                continue
            ts = chunk.get("ingestion_timestamp")
            existing = docs.get(doc_id)
            if existing is None:
                docs[doc_id] = {
                    "document_id": doc_id,
                    "source_path": chunk.get("source_path"),
                    "source_type": chunk.get("source_type"),
                    "fr_tag": self._strip_fr_prefix(chunk.get("fr_tag")),
                    "chunk_count": 1,
                    "first_ingested_at": ts,
                    "last_ingested_at": ts,
                }
            else:
                existing["chunk_count"] += 1
                if ts:
                    if (
                        not existing["first_ingested_at"]
                        or ts < existing["first_ingested_at"]
                    ):
                        existing["first_ingested_at"] = ts
                    if (
                        not existing["last_ingested_at"]
                        or ts > existing["last_ingested_at"]
                    ):
                        existing["last_ingested_at"] = ts

        documents = [DocumentSummary(**d) for d in docs.values()]
        return DocumentListResponse(
            bot_tag=bot_tag,
            count=len(documents),
            documents=documents,
        )

    def get_document(
        self, bot_tag: str, document_id: str
    ) -> Optional[DocumentDetailResponse]:
        """Return the detail summary for one document, or None if absent.

        Returns None (caller maps to 404) when no chunks exist for the given
        (bot_tag, document_id) pair. Critically, both filters are applied
        together — a document indexed under a different bot_tag is treated as
        not-found, never leaked across tenants.
        """
        safe_tag = self._escape_odata(bot_tag)
        safe_doc = self._escape_odata(document_id)
        filter_expr = (
            f"bot_tag eq '{safe_tag}' and document_id eq '{safe_doc}'"
        )

        chunks = list(self._paged_search(filter_expr, _GET_DOC_SELECT))
        if not chunks:
            return None

        timestamps = sorted(
            {
                c.get("ingestion_timestamp")
                for c in chunks
                if c.get("ingestion_timestamp")
            }
        )
        # Cap sample at 5 to keep the response small; first 5 ordered by SDK
        # return order is sufficient for an operator quick-look.
        sample_chunks = [
            ChunkSample(
                id=c["id"],
                chunk_index=self._extract_chunk_index(c.get("id", "")),
            )
            for c in chunks[:5]
            if c.get("id")
        ]

        first = chunks[0]
        return DocumentDetailResponse(
            bot_tag=bot_tag,
            document_id=document_id,
            source_path=first.get("source_path"),
            source_type=first.get("source_type"),
            fr_tag=self._strip_fr_prefix(first.get("fr_tag")),
            chunk_count=len(chunks),
            ingestion_timestamps=list(timestamps),
            sample_chunks=sample_chunks,
        )

    def get_index_stats(self, bot_tag: str) -> IndexStatsResponse:
        """Return aggregate stats for the given bot_tag scope.

        Cost note: this scans every chunk in the bot_tag scope. For very large
        scopes this may take several seconds and cost a corresponding number
        of search query units. Acceptable for an admin/debug tool; a future PR
        could persist precomputed stats if call volume grows.
        """
        safe_tag = self._escape_odata(bot_tag)
        filter_expr = f"bot_tag eq '{safe_tag}'"

        # source_type and fr_mode are document-level attributes; we count once
        # per unique document_id rather than per chunk.
        seen_docs: dict[str, dict] = {}
        chunk_count = 0

        for chunk in self._paged_search(filter_expr, _STATS_SELECT):
            chunk_count += 1
            doc_id = chunk.get("document_id")
            if not doc_id or doc_id in seen_docs:
                continue
            seen_docs[doc_id] = {
                "source_type": chunk.get("source_type"),
                "fr_tag": chunk.get("fr_tag"),
            }

        source_types: dict[str, int] = {}
        fr_modes: dict[str, int] = {}
        for d in seen_docs.values():
            st = d["source_type"]
            if st:
                source_types[st] = source_types.get(st, 0) + 1
            fr = d["fr_tag"]
            if fr:
                # Spec example shows "layout" / "read" (without the "fr_" prefix
                # the index stores). Strip the prefix in the response.
                mode = fr[3:] if fr.startswith("fr_") else fr
                fr_modes[mode] = fr_modes.get(mode, 0) + 1

        return IndexStatsResponse(
            bot_tag=bot_tag,
            document_count=len(seen_docs),
            chunk_count=chunk_count,
            source_types=source_types,
            fr_modes=fr_modes,
        )


# ---------------------------------------------------------------------------
# Module-level singleton + FastAPI dependency
# ---------------------------------------------------------------------------

_service_singleton: Optional[SearchAdminService] = None


def get_admin_service() -> SearchAdminService:
    """FastAPI dependency: return the lazily-constructed admin service.

    The singleton is constructed on first request from the same env vars the
    rest of the ingestion service uses (`AZURE_SEARCH_ENDPOINT`,
    `AZURE_SEARCH_KEY`, `INDEX_NAME`). This avoids touching the existing
    `custom_rag` startup flow.

    Raises:
        HTTPException(503): if any required env var is missing. We surface
            this as 503 (not RuntimeError) so it propagates as a clean HTTP
            response with no internal detail leakage. A RuntimeError raised
            inside dependency resolution would escape the route's
            `try/except AzureError` and turn into a generic 500.

    Tests should override this dependency via
    `app.dependency_overrides[get_admin_service]` to inject a mocked service.
    """
    global _service_singleton
    if _service_singleton is None:
        endpoint = os.getenv("AZURE_SEARCH_ENDPOINT")
        key = os.getenv("AZURE_SEARCH_KEY")
        index_name = os.getenv("INDEX_NAME")
        if not (endpoint and key and index_name):
            # Log the specific cause internally; return a generic safe message
            # to the client. Each missing var is named in the log so operators
            # can fix the deployment without reading source.
            missing = [
                name for name, value in (
                    ("AZURE_SEARCH_ENDPOINT", endpoint),
                    ("AZURE_SEARCH_KEY", key),
                    ("INDEX_NAME", index_name),
                ) if not value
            ]
            logger.error(
                "Admin service misconfigured — missing env vars: %s",
                ", ".join(missing),
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Admin search service not configured",
            )
        client = SearchClient(
            endpoint=endpoint,
            index_name=index_name,
            credential=AzureKeyCredential(key),
        )
        _service_singleton = SearchAdminService(client)
        logger.info("Admin SearchClient initialized for index %r", index_name)
    return _service_singleton
