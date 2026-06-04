"""Pydantic request and response models for the admin API.

Response shapes match `docs/architect_phase_2/01_ADMIN_API_SPEC.md` exactly.
Field nullability reflects the reality that older indexed chunks may not have
the metadata fields added in P0-4 (deterministic chunk IDs PR).
"""

from datetime import datetime

from pydantic import BaseModel, Field


class DocumentSummary(BaseModel):
    """One row in the document list — aggregated from chunk metadata."""

    document_id: str
    source_path: str | None = None
    source_type: str | None = None
    fr_tag: str | None = None
    chunk_count: int = Field(ge=0)
    first_ingested_at: str | None = None
    last_ingested_at: str | None = None


class DocumentListResponse(BaseModel):
    """Response shape for GET /admin/documents."""

    bot_tag: str
    count: int = Field(ge=0)
    documents: list[DocumentSummary]


class ChunkSample(BaseModel):
    """A small per-chunk payload returned in the document detail response."""

    id: str
    chunk_index: int | None = None


class DocumentDetailResponse(BaseModel):
    """Response shape for GET /admin/documents/{document_id}."""

    bot_tag: str
    document_id: str
    source_path: str | None = None
    source_type: str | None = None
    fr_tag: str | None = None
    chunk_count: int = Field(ge=0)
    ingestion_timestamps: list[str]
    sample_chunks: list[ChunkSample]


class IndexStatsResponse(BaseModel):
    """Response shape for GET /admin/index/stats."""

    bot_tag: str
    document_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    source_types: dict[str, int]
    fr_modes: dict[str, int]


class DeleteDocumentResponse(BaseModel):
    """Response shape for DELETE /admin/documents/{document_id}."""

    bot_tag: str
    document_id: str
    deleted_chunks: int = Field(ge=0)
    status: str = "deleted"


class DeleteTenantResponse(BaseModel):
    """Response shape for DELETE /admin/bots/{bot_tag}/documents."""

    bot_tag: str
    deleted_chunks: int = Field(ge=0)
    deleted_documents: int = Field(ge=0)
    status: str = "deleted"


class ReindexResponse(BaseModel):
    """Response shape for POST /admin/documents/{document_id}/reindex.

    Reindex is a documented 501 stub until source persistence exists; this is
    a normal payload (returned with HTTP 501), NOT an error envelope.
    """

    status: str = "not_implemented"
    reason: str = "Reindex requires source persistence or connector metadata. Use delete + ingest for now."


class ConnectorSyncResponse(BaseModel):
    """Response shape for POST /admin/connectors/{source_type}/sync.

    Returned with HTTP 202 Accepted — the sync runs as an in-process background
    task, so the request does NOT block on the full enumerate→fetch→upload loop.
    `run_id` correlates the background run's structured log events.
    """

    run_id: str
    source_type: str
    status: str = "started"


class ConnectorRunError(BaseModel):
    """Safe error summary attached to a failed run.

    Carries only the exception CLASS name and a generic category message —
    never raw exception text, secrets, or document content.
    """

    error_class: str
    safe_message: str


class ConnectorRunStatusResponse(BaseModel):
    """Response shape for GET /admin/connectors/runs/{run_id}.

    Reflects the in-process run-status store. `status` is one of
    started | completed | failed. Counts are populated on completion;
    `error` is present only on failure. State is in-process and LOST on
    restart (in-process v1 — not a durable distributed store).
    """

    run_id: str
    status: str
    source_type: str
    bot_tag: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    processed_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    error: ConnectorRunError | None = None


class ConnectorRunListResponse(BaseModel):
    """Response shape for GET /admin/connectors/runs (recent runs, newest first).

    Admin-wide view across bot_tags — the operator triggering a sync is already
    a privileged, bot_tag-agnostic role (see require_admin_token).
    """

    count: int = Field(ge=0)
    runs: list[ConnectorRunStatusResponse]
