"""tocdoc-sdk — a typed, dependency-light Python client for the TocDoc QnA API.

Standalone client: it mirrors the server's request/response/error contracts but
does NOT import any ``services/`` code.
"""

from __future__ import annotations

from .client import TocDocClient
from .errors import ApiError
from .models import BotTurn, CitationMap, QnAAnswer, QnARequest

__all__ = [
    "ApiError",
    "BotTurn",
    "CitationMap",
    "QnAAnswer",
    "QnARequest",
    "TocDocClient",
]

__version__ = "0.1.0"
