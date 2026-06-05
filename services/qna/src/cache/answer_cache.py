"""Optional within-tenant answer cache for the non-streaming ``/qna`` path.

This module provides a **pluggable, default-OFF** cache for the
``{answer, citation, ...}`` payload of the non-streaming QnA pipeline. It exists
to short-circuit the expensive rephrase → embed → search → LLM fan-out when the
*same* tenant asks the *same* normalized question in the *same* retrieval mode.

Design constraints (deliberate, see the feature spec):

- **Within-tenant only.** The cache key is the tuple
  ``(bot_tag, fr_mode, normalized_query)``. ``bot_tag`` is the FIRST element and
  the key is a real tuple (never a delimiter-joined string), so two tenants can
  never collide on a shared separator — a cached answer is NEVER served across
  ``bot_tag`` boundaries. This is the security-critical invariant of the module.
- **Exact-match on a normalized query**, NOT vector similarity. "Semantic" here
  means the answer payload is cached, not that retrieval embeddings are compared.
  Normalization collapses surrounding/inner whitespace and case-folds so trivial
  surface variants ("Hello   World" / "hello world") hit the same entry.
- **No new infra dependency.** The default backend is a process-local
  ``OrderedDict`` LRU with per-entry TTL. The :class:`CacheBackend` protocol
  lets a deployment swap in another backend later without touching the pipeline.
- **Default OFF.** Wiring in the pipeline is guarded by ``is_cache_enabled()``;
  when the flag is off the cache is never consulted or populated and behaviour is
  byte-identical to the historical pipeline.

Conversation history is intentionally EXCLUDED from the key: a follow-up whose
surface text matches an earlier turn would serve the earlier answer. That is the
documented trade-off and the reason the feature ships default-OFF.
"""

from __future__ import annotations

import copy
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

# A cache key is the ordered tuple (bot_tag, fr_mode, normalized_query). Using a
# tuple — not a joined string — makes cross-tenant collisions structurally
# impossible regardless of what characters a bot_tag contains.
CacheKey = tuple[str, str, str]


def normalize_query(query: str) -> str:
    """Normalize a user query for exact-match cache lookup.

    Collapses all runs of whitespace to single spaces, trims the ends, and
    case-folds, so trivial surface variants of the same question map to one
    entry. This is deliberately conservative — it never strips punctuation or
    reorders tokens, so it cannot merge two genuinely different questions.

    Returns an empty string for ``None``/empty input (the caller guards on
    ``bot_tag``, not query, before reaching here).
    """
    return " ".join((query or "").split()).casefold()


def make_cache_key(bot_tag: str, fr_mode: str, query: str) -> CacheKey:
    """Build the tenant-scoped cache key tuple.

    ``bot_tag`` and ``fr_mode`` are kept verbatim (they are bounded
    identifiers); only the free-text ``query`` is normalized. The resulting
    tuple is used directly as a dict key.
    """
    return (bot_tag, fr_mode, normalize_query(query))


@runtime_checkable
class CacheBackend(Protocol):
    """Pluggable answer-cache backend.

    A backend stores opaque ``dict`` payloads keyed by a :data:`CacheKey`. The
    default implementation is in-memory (:class:`InMemoryTTLLRUCache`); a
    deployment could provide a shared backend later without changing the
    pipeline hook, as long as it honours this protocol.

    Implementations MUST:
      - return ``None`` on a miss (including expired entries),
      - return a value the caller may safely mutate (i.e. a copy),
      - never raise from ``get``/``set`` (the cache is best-effort and must
        never break the request path).
    """

    def get(self, key: CacheKey) -> dict[str, Any] | None:
        """Return the cached payload for ``key`` or ``None`` on miss/expiry."""
        ...

    def set(self, key: CacheKey, value: dict[str, Any]) -> None:
        """Store ``value`` under ``key`` (evicting/expiring as needed)."""
        ...


class InMemoryTTLLRUCache:
    """Process-local LRU cache with per-entry TTL.

    Backed by an ``OrderedDict`` ordered from least- to most-recently used. A
    successful ``get`` (and every ``set``) moves the key to the most-recent end;
    an insert past ``max_entries`` evicts the least-recently-used key. Each entry
    also carries an absolute expiry; an expired entry is treated as a miss and
    dropped lazily on access.

    Thread-safe via a single lock (the FastAPI app may serve concurrent
    requests). The lock is held only for the O(1) dict operations.

    Args:
        ttl_seconds: Entry lifetime in seconds. Must be positive.
        max_entries: Maximum number of live entries. Must be positive; the
            least-recently-used entry is evicted on overflow.
        clock: Monotonic time source (seconds). Injectable so TTL/expiry can be
            tested deterministically without sleeping. Defaults to
            ``time.monotonic`` — monotonic (not wall-clock) so the TTL is immune
            to system clock adjustments.

    On hit the stored payload is returned as a deep copy (and stored as one on
    ``set``) so a caller mutating the returned dict — including nested
    ``citation``/``page_citations`` maps — cannot corrupt the cached entry or
    contaminate other hits, honouring the :class:`CacheBackend` contract.
    """

    def __init__(
        self,
        ttl_seconds: float,
        max_entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self._ttl = float(ttl_seconds)
        self._max = int(max_entries)
        self._clock = clock
        # key -> (expiry_monotonic, payload)
        self._store: OrderedDict[CacheKey, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: CacheKey) -> dict[str, Any] | None:
        """Return a copy of the live payload for ``key`` or ``None``.

        A miss, or an entry whose TTL has elapsed, returns ``None``; an expired
        entry is dropped in passing. A hit refreshes the entry's LRU recency.
        """
        now = self._clock()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expiry, payload = entry
            if now >= expiry:
                # Expired: drop lazily and report a miss.
                del self._store[key]
                return None
            # Refresh recency on a live hit.
            self._store.move_to_end(key)
            # Deep copy so the caller cannot mutate the cached entry through
            # nested citation/page_citations maps (CacheBackend contract).
            return copy.deepcopy(payload)

    def set(self, key: CacheKey, value: dict[str, Any]) -> None:
        """Store a copy of ``value`` under ``key`` with a fresh TTL.

        Re-setting an existing key refreshes both its payload/TTL and its LRU
        recency. Inserting a new key past ``max_entries`` evicts the
        least-recently-used entry.
        """
        expiry = self._clock() + self._ttl
        stored = copy.deepcopy(value)
        with self._lock:
            if key in self._store:
                # Refresh payload + recency for an existing key.
                self._store[key] = (expiry, stored)
                self._store.move_to_end(key)
                return
            self._store[key] = (expiry, stored)
            # Evict LRU entries until within capacity (loop guards against a
            # capacity lowered between inserts; normally runs at most once).
            while len(self._store) > self._max:
                self._store.popitem(last=False)

    def __len__(self) -> int:
        """Number of stored entries (including any not-yet-evicted expired ones)."""
        with self._lock:
            return len(self._store)

    def clear(self) -> None:
        """Drop all entries. Primarily for test isolation."""
        with self._lock:
            self._store.clear()


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------
# Built lazily from config on first use so import order / env timing don't
# matter, and rebuildable for tests via reset_cache().
_cache_singleton: CacheBackend | None = None
_singleton_lock = threading.Lock()


def get_cache() -> CacheBackend:
    """Return the process-wide cache backend, building it from config once.

    Reads ``QNA_CACHE_TTL_SECONDS`` / ``QNA_CACHE_MAX_ENTRIES`` (via the config
    resolvers) the first time it is called. The flag ``QNA_CACHE_ENABLED`` is
    checked by the *caller* (the pipeline), not here — this just provides the
    backend instance.
    """
    global _cache_singleton
    if _cache_singleton is None:
        with _singleton_lock:
            if _cache_singleton is None:
                # Imported here (not at module top) to avoid any import-time
                # coupling between the cache module and config bootstrapping.
                from src.config.config import cache_max_entries, cache_ttl_seconds

                _cache_singleton = InMemoryTTLLRUCache(
                    ttl_seconds=cache_ttl_seconds(),
                    max_entries=cache_max_entries(),
                )
    return _cache_singleton


def reset_cache() -> None:
    """Drop the singleton so the next ``get_cache()`` rebuilds from config.

    Intended for tests that change cache env vars or need a clean cache between
    cases. Not used on the production request path.
    """
    global _cache_singleton
    with _singleton_lock:
        _cache_singleton = None
