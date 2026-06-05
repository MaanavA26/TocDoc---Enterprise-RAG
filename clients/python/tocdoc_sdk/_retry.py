"""Shared transient-retry policy for the sync, async, and admin clients.

Centralizes the definition of "transient" (retryable) failures, the backoff
computation, and the defaults so every client applies the *same* policy.
Importing this module never builds an HTTP client; it only defines constants
and small helpers.

Method-awareness (L-SDK1)
-------------------------
Retry safety depends on whether re-sending a request can cause a *duplicate
side effect*, not on the raw HTTP verb. We classify by **idempotency**:

- **Idempotent** requests (all admin GETs, and the ``POST /qna`` query — a read
  that produces no server-side state) retry on a transient 5xx *and* on every
  transient connect/read/write timeout: re-sending only ever re-fetches the
  same answer.
- **Non-idempotent** requests (state-changing POSTs such as
  ``trigger_connector_sync``) retry **only** on connect-phase errors
  (``ConnectError`` / ``ConnectTimeout`` / ``PoolTimeout``) — failures that
  prove the request never reached the server. They are **never** retried on a
  5xx or on a post-send ``ReadTimeout`` / ``WriteTimeout``, because there the
  server may already have accepted the request and a blind retry would launch a
  second sync run (there is no server-side idempotency key).

Backoff jitter (L-SDK2)
-----------------------
:func:`compute_backoff` applies full jitter to the exponential schedule so a
fleet of clients that begin retrying together do not re-hit a recovering server
in synchronized waves (thundering herd). The RNG is injectable so tests stay
deterministic.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

import httpx

# Status codes treated as transient and therefore retryable (idempotent only).
RETRYABLE_STATUS: frozenset[int] = frozenset({500, 502, 503, 504})

# httpx exceptions treated as transient for *idempotent* requests: any
# connect-phase or post-send read/write timeout is safe to re-send.
RETRYABLE_EXC: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
)

# Connect-phase exceptions only: the request provably never reached the server,
# so re-sending cannot duplicate a side effect. This is the *only* class of
# failure a non-idempotent request may be retried on.
CONNECT_PHASE_EXC: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_BACKOFF_BASE = 0.5

# RNG used by compute_backoff: a no-arg callable returning a float in [0.0, 1.0).
# Defaults to the stdlib; tests inject a deterministic stub.
Rng = Callable[[], float]


def should_retry_exception(exc: Exception, *, idempotent: bool) -> bool:
    """Whether a transport exception should be retried under the policy.

    Idempotent requests retry on any :data:`RETRYABLE_EXC`. Non-idempotent
    requests retry only on :data:`CONNECT_PHASE_EXC` (the request never reached
    the server), never on a post-send read/write timeout.
    """
    if idempotent:
        return isinstance(exc, RETRYABLE_EXC)
    return isinstance(exc, CONNECT_PHASE_EXC)


def should_retry_status(status_code: int, *, idempotent: bool) -> bool:
    """Whether a response status should be retried under the policy.

    Only idempotent requests retry on a transient 5xx. A non-idempotent request
    is never retried on a 5xx: the server may already have accepted it.
    """
    return idempotent and status_code in RETRYABLE_STATUS


def compute_backoff(
    attempt: int,
    *,
    base: float,
    rng: Rng = random.random,
) -> float:
    """Full-jitter exponential backoff for retry ``attempt`` (0-indexed).

    Returns a sleep duration uniformly distributed in
    ``[0, base * 2**attempt]``. Full jitter (vs. fixed exponential) de-syncs a
    recovering fleet. ``rng`` is injectable (a no-arg callable returning a float
    in ``[0, 1)``) so tests are deterministic; it defaults to
    :func:`random.random`.
    """
    ceiling = base * (2**attempt)
    return rng() * ceiling


def safe_json(response: httpx.Response) -> Any:
    """Parse a response body as JSON, returning ``None`` if it isn't JSON."""
    try:
        return response.json()
    except ValueError:
        return None
