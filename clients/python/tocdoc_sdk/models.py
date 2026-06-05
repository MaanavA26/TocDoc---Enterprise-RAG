"""Typed mirrors of the TocDoc QnA HTTP contract.

These models are a *standalone* copy of the server-side contract. The SDK does
NOT import the service code (``services/qna``); it reproduces the on-the-wire
shapes so the package can be installed and used in isolation.

Contract sources mirrored here (kept byte-compatible with the server):
- Request body — ``services/qna/src/utils/util.py`` ``Payload`` / ``BotQuery``.
- Success body — ``services/qna/src/core/responses.py`` ``QnASuccessResponse``
  (``{"answer": str, "citation": {filename: filepath}}``).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, RootModel


class CitationMap(RootModel[dict[str, str]]):
    """A citation mapping of ``filename -> filepath``.

    Mirrors the server's pydantic v2 ``RootModel`` so it round-trips a flat
    JSON object (``{"a.md": "/docs/a.md"}``) with no wrapper key.
    """

    root: dict[str, str] = Field(default_factory=dict)


class BotTurn(BaseModel):
    """A single conversation turn in a :class:`QnARequest`.

    Mirrors the server's ``BotQuery``: ``user_query`` is required; the rest are
    optional. ``extra="allow"`` matches the server so callers can attach
    forward-compatible fields without a validation error.
    """

    model_config = ConfigDict(extra="allow")

    user_query: str = Field(..., description="The user's input for this turn.")
    bot_response: str | None = Field(default=None, description="The bot's response for this turn, if any.")
    answer: str | None = Field(default=None, description="Alternate field for bot response content.")


class QnARequest(BaseModel):
    """Request body for ``POST /qna``.

    Mirrors the server's ``Payload``. All four fields are required by the
    service; ``bot`` must contain at least one turn (the service rejects an
    empty ``bot`` list with a 400).

    Attributes:
        session_id: Correlation/session identifier.
        bot: Ordered conversation turns, oldest -> newest. The last turn's
            ``user_query`` is the question that gets answered.
        fr_tag: Feature/retrieval tag.
        bot_tag: Bot identifier/tag (enforces tenant isolation server-side).
    """

    session_id: str = Field(..., description="Correlation/session identifier.")
    bot: list[BotTurn] = Field(..., description="Ordered conversation turns, oldest -> newest.")
    fr_tag: str = Field(..., description="Feature/retrieval tag.")
    bot_tag: str = Field(..., description="Bot identifier/tag.")


class QnAAnswer(BaseModel):
    """Typed success payload returned by ``POST /qna``.

    Mirrors the server's ``QnASuccessResponse`` success shape. ``extra="ignore"``
    is deliberate: the server model is ``extra="allow"`` and may emit defensive
    optional keys (e.g. ``request_id``, ``error``) on exceptional internal
    paths, so the client tolerates and drops unknown keys instead of breaking.

    Attributes:
        answer: The grounded answer text.
        citation: Mapping of cited ``filename -> filepath``.
    """

    model_config = ConfigDict(extra="ignore")

    answer: str = Field(..., description="Grounded answer text for the user's query.")
    citation: CitationMap = Field(
        default_factory=CitationMap,
        description="Mapping of cited filename -> filepath.",
    )

    @property
    def citations(self) -> dict[str, str]:
        """The citation mapping as a plain ``{filename: filepath}`` dict."""
        return self.citation.root


# ---------------------------------------------------------------------------
# Admin API (read-only) — mirrors services/ingestion/admin/models.py
# ---------------------------------------------------------------------------
#
# Standalone copies of the read-only admin response shapes. As with QnAAnswer,
# these use ``extra="ignore"`` so the SDK tolerates and drops forward-compatible
# keys the server may add, instead of failing validation.


class DocumentSummary(BaseModel):
    """One row in the document list — aggregated from chunk metadata.

    Nullable fields reflect that older indexed chunks may lack metadata added
    in later ingestion revisions.
    """

    model_config = ConfigDict(extra="ignore")

    document_id: str
    source_path: str | None = None
    source_type: str | None = None
    fr_tag: str | None = None
    chunk_count: int = Field(ge=0)
    first_ingested_at: str | None = None
    last_ingested_at: str | None = None


class DocumentListResponse(BaseModel):
    """Typed body for ``GET /admin/documents``."""

    model_config = ConfigDict(extra="ignore")

    bot_tag: str
    count: int = Field(ge=0)
    documents: list[DocumentSummary]


class ChunkSample(BaseModel):
    """A small per-chunk payload returned in the document detail response."""

    model_config = ConfigDict(extra="ignore")

    id: str
    chunk_index: int | None = None


class DocumentDetailResponse(BaseModel):
    """Typed body for ``GET /admin/documents/{document_id}``."""

    model_config = ConfigDict(extra="ignore")

    bot_tag: str
    document_id: str
    source_path: str | None = None
    source_type: str | None = None
    fr_tag: str | None = None
    chunk_count: int = Field(ge=0)
    ingestion_timestamps: list[str] = Field(default_factory=list)
    sample_chunks: list[ChunkSample] = Field(default_factory=list)


class IndexStatsResponse(BaseModel):
    """Typed body for ``GET /admin/index/stats``."""

    model_config = ConfigDict(extra="ignore")

    bot_tag: str
    document_count: int = Field(ge=0)
    chunk_count: int = Field(ge=0)
    source_types: dict[str, int] = Field(default_factory=dict)
    fr_modes: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Admin API (connector control-plane) — mirrors services/ingestion/admin/models.py
# ---------------------------------------------------------------------------
#
# Standalone copies of the connector sync/run-status shapes. As above, these use
# ``extra="ignore"`` so the SDK tolerates and drops forward-compatible keys the
# server may add (e.g. new run fields) instead of failing validation.


class ConnectorSyncResponse(BaseModel):
    """Typed run-handle for ``POST /admin/connectors/{source_type}/sync``.

    Returned with HTTP 202 Accepted — the sync runs as an in-process background
    task server-side, so the request does NOT block on the full
    enumerate -> fetch -> upload loop. ``run_id`` correlates the background run and
    is the key for the run-status getters below.
    """

    model_config = ConfigDict(extra="ignore")

    run_id: str
    source_type: str
    status: str = "started"


class ConnectorRunError(BaseModel):
    """Safe error summary attached to a failed connector run.

    Carries only the exception CLASS name and a generic category message — the
    server never emits raw exception text, secrets, or document content here.
    """

    model_config = ConfigDict(extra="ignore")

    error_class: str
    safe_message: str


class ConnectorRunStatusResponse(BaseModel):
    """Typed body for ``GET /admin/connectors/runs/{run_id}``.

    Reflects the server's in-process run-status store. ``status`` is one of
    ``started`` | ``completed`` | ``failed``; counts are populated on completion
    and ``error`` is present only on failure. Run state is in-process server-side
    and LOST on restart, so an unknown/evicted ``run_id`` raises ``ApiError``
    (404) rather than returning a record.
    """

    model_config = ConfigDict(extra="ignore")

    run_id: str
    status: str
    source_type: str
    bot_tag: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    processed_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    error: ConnectorRunError | None = None


class ConnectorRunListResponse(BaseModel):
    """Typed body for ``GET /admin/connectors/runs``.

    Admin-wide view across ``bot_tag``s (the operator triggering a sync is a
    privileged, bot_tag-agnostic role), newest first.
    """

    model_config = ConfigDict(extra="ignore")

    count: int = Field(ge=0)
    runs: list[ConnectorRunStatusResponse] = Field(default_factory=list)
