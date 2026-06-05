"""tocdoc-sdk — a typed, dependency-light Python client for the TocDoc API.

Standalone client: it mirrors the server's request/response/error contracts but
does NOT import any ``services/`` code. Provides:

- :class:`TocDocClient` / :class:`AsyncTocDocClient` — sync/async QnA clients.
- :class:`AdminClient` — read-only admin API client (``X-Admin-Token`` auth).
"""

from __future__ import annotations

from .admin import AdminClient
from .async_client import AsyncTocDocClient
from .client import TocDocClient
from .errors import ApiError
from .models import (
    BotTurn,
    ChunkSample,
    CitationMap,
    DocumentDetailResponse,
    DocumentListResponse,
    DocumentSummary,
    IndexStatsResponse,
    QnAAnswer,
    QnARequest,
)

__all__ = [
    "AdminClient",
    "ApiError",
    "AsyncTocDocClient",
    "BotTurn",
    "ChunkSample",
    "CitationMap",
    "DocumentDetailResponse",
    "DocumentListResponse",
    "DocumentSummary",
    "IndexStatsResponse",
    "QnAAnswer",
    "QnARequest",
    "TocDocClient",
]

__version__ = "0.1.0"
