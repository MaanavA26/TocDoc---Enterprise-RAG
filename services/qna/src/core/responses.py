"""
Typed success-response contract for the QnA service.

This module is the *public success contract* of the `/qna` endpoint — the
same role `src.core.errors.ErrorEnvelope` plays for the failure contract.
It exists so downstream consumers (the planned Python SDK, RAGAS context
extraction) can target a stable, typed shape instead of an untyped dict.

Backward-compatibility guarantee
--------------------------------
These models are wired in as the FastAPI ``response_model`` for the `/qna`
route. They are designed to serialize **byte-for-byte identically** to the
historical payload::

    {"answer": "<text>", "citation": {"<filename>": "<filepath>", ...}}

To preserve that, the route uses ``response_model_exclude_none=True`` so the
defensive optional fields (`request_id`, `error`) never appear as ``null`` on
the normal success path. `CitationMap` is a pydantic ``RootModel`` so it
serializes to a *flat* ``{filename: filepath}`` object, never a wrapped
``{"root": {...}}`` shape.

Page-level citations (P2-1 groundwork)
--------------------------------------
``page_citations`` is an **optional, additive** sibling that maps a cited
``filename -> ordered-unique list of page strings`` (e.g.
``{"a.md": ["3", "7"]}``). It defaults to ``None`` and is excluded by
``response_model_exclude_none``, so until ingestion populates ``page_number``
on chunks the wire payload stays byte-for-byte identical to the historical
``{answer, citation}`` shape. ``CitationMap`` is intentionally left UNCHANGED.

Out of scope
------------
The ingestion reindex / read-mode layout extraction that actually *populates*
``page_number`` is gated on a separate spike and is NOT part of this change.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, RootModel


class CitationMap(RootModel[dict[str, str]]):
    """A citation mapping of ``filename -> filepath``.

    Modeled as a pydantic v2 ``RootModel`` so it serializes to a plain JSON
    object (``{"a.md": "/docs/a.md"}``) with no wrapper key — identical to the
    bare ``dict[str, str]`` the pipeline has always emitted under
    ``"citation"``.

    Example:
        >>> CitationMap({"a.md": "/docs/a.md"}).model_dump()
        {'a.md': '/docs/a.md'}
    """

    root: dict[str, str] = Field(default_factory=dict)


class QnASuccessResponse(BaseModel):
    """Typed success payload returned by ``POST /qna``.

    This IS the public success contract the SDK and RAGAS tooling target.

    Attributes:
        answer: The grounded answer text produced by the model.
        citation: Mapping of cited ``filename -> filepath``. Accepts either a
            :class:`CitationMap` or a plain ``dict[str, str]`` on input and
            always serializes to a flat ``{filename: filepath}`` object.
        page_citations: Optional mapping of cited ``filename -> ordered-unique
            list of page strings`` (P2-1 groundwork). ``None`` (the default)
            until ingestion populates ``page_number``; excluded by
            ``response_model_exclude_none`` so the payload stays byte-identical.
        request_id: Optional correlation ID. Defensive/optional only — the
            normal success path does not emit it, and ``response_model_exclude_none``
            keeps it out of the wire payload when unset.
        error: Optional error marker carried only by historical/internal
            exceptional paths. Defensive/optional only; excluded when unset.

    Notes:
        - ``response_model_exclude_none=True`` on the route guarantees the
          optional fields never serialize as ``null``, keeping the success
          JSON byte-for-byte identical to the historical ``{answer, citation}``.
        - ``extra="allow"`` keeps the contract forgiving: any stray key on an
          exceptional internal path is preserved rather than triggering a
          response-validation 500.
    """

    model_config = ConfigDict(extra="allow")

    answer: str = Field(..., description="Grounded answer text for the user's query.")
    citation: CitationMap = Field(
        default_factory=CitationMap,
        description="Mapping of cited filename -> filepath. Serializes flat.",
    )
    page_citations: dict[str, list[str]] | None = Field(
        default=None,
        description=(
            "Optional cited filename -> ordered-unique page strings (P2-1 "
            "groundwork). None until ingestion populates page_number; excluded "
            "by response_model_exclude_none so the payload stays byte-identical."
        ),
    )
    request_id: str | None = Field(
        default=None,
        description="Optional correlation ID; only present on exceptional/internal paths.",
    )
    error: Any | None = Field(
        default=None,
        description="Optional error marker carried only by historical/internal paths.",
    )
