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
