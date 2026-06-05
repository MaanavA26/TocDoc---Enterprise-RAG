"""Shared transient-retry policy for the sync, async, and admin clients.

Centralizes the definition of "transient" (retryable) failures and the
defaults so every client applies the *same* policy. Importing this module
never builds an HTTP client; it only defines constants and a tiny helper.
"""

from __future__ import annotations

from typing import Any

import httpx

# Status codes treated as transient and therefore retryable.
RETRYABLE_STATUS: frozenset[int] = frozenset({500, 502, 503, 504})

# httpx exceptions treated as transient connect/read failures.
RETRYABLE_EXC: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
)

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_BASE = 0.5


def safe_json(response: httpx.Response) -> Any:
    """Parse a response body as JSON, returning ``None`` if it isn't JSON."""
    try:
        return response.json()
    except ValueError:
        return None
