"""Pydantic request and response models for the admin API.

Response shapes match `docs/architect_phase_2/01_ADMIN_API_SPEC.md` exactly.
Field nullability reflects the reality that older indexed chunks may not have
the metadata fields added in P0-4 (deterministic chunk IDs PR).
"""

from typing import Optional

from pydantic import BaseModel, Field


class DocumentSummary(BaseModel):
    """One row in the document list — aggregated from chunk metadata."""

    document_id: str
    source_path: Optional[str] = None
    source_type: Optional[str] = None
    fr_tag: Optional[str] = None
    chunk_count: int = Field(ge=0)
    first_ingested_at: Optional[str] = None
    last_ingested_at: Optional[str] = None


class DocumentListResponse(BaseModel):
    """Response shape for GET /admin/documents."""

    bot_tag: str
    count: int = Field(ge=0)
    documents: list[DocumentSummary]


class ChunkSample(BaseModel):
    """A small per-chunk payload returned in the document detail response."""

    id: str
    chunk_index: Optional[int] = None


class DocumentDetailResponse(BaseModel):
    """Response shape for GET /admin/documents/{document_id}."""

    bot_tag: str
    document_id: str
    source_path: Optional[str] = None
    source_type: Optional[str] = None
    fr_tag: Optional[str] = None
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
