"""Tests for the optional within-tenant answer cache (default-OFF).

Two layers:

1. Unit tests of ``InMemoryTTLLRUCache`` (TTL expiry, LRU eviction, copy
   semantics) driven with an injected fake clock — deterministic, no sleeping.

2. Integration tests through ``generate_answer`` using the same mock pattern as
   ``test_pipeline_isolation.py``, asserting on LLM mock call-counts so a hit is
   provably skipping the fan-out:
     - hit returns the cached payload AND emits a ``cache_hit`` event,
     - miss populates the cache,
     - a different ``bot_tag`` NEVER hits another tenant's entry,
     - with the flag OFF the cache is a no-op (output byte-identical to a
       no-cache run, LLM called every time).
"""

import contextlib
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Required env vars must be set before any local imports
# ---------------------------------------------------------------------------
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://fake-openai.example.com")
os.environ.setdefault("AZURE_OPENAI_KEY", "fake-key")
os.environ.setdefault("AZURE_OPENAI_VERSION", "2024-06-01")
os.environ.setdefault("AZURE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "https://fake-search.example.com")
os.environ.setdefault("AZURE_SEARCH_KEY", "fake-search-key")
os.environ.setdefault("INDEX_NAME", "fake-index")
os.environ.setdefault("AZURE_KEY_VAULT", "fakevault")
os.environ.setdefault("AZURE_TENANT_ID", "11111111-1111-1111-1111-111111111111")
os.environ.setdefault("AUDIENCE_ID", "api://fake-audience-id")

from src.cache.answer_cache import (  # noqa: E402
    CacheBackend,
    InMemoryTTLLRUCache,
    make_cache_key,
    normalize_query,
    reset_cache,
)


class _FakeClock:
    """Manually-advanced monotonic clock for deterministic TTL tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _clean_cache_singleton():
    """Reset the module-level cache singleton and cache env around each test.

    Without this the process-wide singleton would bleed entries (and stale
    config) across tests.
    """
    reset_cache()
    for var in ("QNA_CACHE_ENABLED", "QNA_CACHE_TTL_SECONDS", "QNA_CACHE_MAX_ENTRIES"):
        os.environ.pop(var, None)
    yield
    reset_cache()
    for var in ("QNA_CACHE_ENABLED", "QNA_CACHE_TTL_SECONDS", "QNA_CACHE_MAX_ENTRIES"):
        os.environ.pop(var, None)


# ===========================================================================
# Unit: key construction & normalization
# ===========================================================================


def test_normalize_query_collapses_whitespace_and_case():
    assert normalize_query("  Hello   World  ") == "hello world"
    assert normalize_query("Hello World") == normalize_query("hello   world")


def test_normalize_query_handles_none_and_empty():
    assert normalize_query(None) == ""
    assert normalize_query("") == ""


def test_make_cache_key_is_tuple_with_bot_tag_first():
    key = make_cache_key("tenant-a", "read", "What is X?")
    assert isinstance(key, tuple)
    assert key[0] == "tenant-a"
    assert key[1] == "read"
    # Query normalized; bot_tag/fr_mode verbatim.
    assert key[2] == "what is x?"


def test_cache_key_no_cross_tenant_collision_via_separator():
    """A bot_tag containing a delimiter must NOT collide with another tenant.

    A naive f"{bot_tag}:{fr_mode}:{query}" key could let
    bot_tag="a" + query="b:read:q" alias bot_tag="a:read" etc. The tuple key
    makes that structurally impossible.
    """
    k1 = make_cache_key("a", "read", "b:read:q")
    k2 = make_cache_key("a:read", "read", "q")  # only plausible string-collision
    assert k1 != k2


def test_default_backend_satisfies_protocol():
    cache = InMemoryTTLLRUCache(ttl_seconds=10, max_entries=4)
    assert isinstance(cache, CacheBackend)


# ===========================================================================
# Unit: InMemoryTTLLRUCache TTL + LRU + copy semantics
# ===========================================================================


def test_cache_get_miss_returns_none():
    cache = InMemoryTTLLRUCache(ttl_seconds=10, max_entries=4)
    assert cache.get(("t", "read", "q")) is None


def test_cache_set_then_get_hit():
    cache = InMemoryTTLLRUCache(ttl_seconds=10, max_entries=4)
    key = ("t", "read", "q")
    cache.set(key, {"answer": "A", "citation": {}})
    assert cache.get(key) == {"answer": "A", "citation": {}}


def test_cache_ttl_expiry_with_fake_clock():
    clock = _FakeClock()
    cache = InMemoryTTLLRUCache(ttl_seconds=5, max_entries=4, clock=clock)
    key = ("t", "read", "q")
    cache.set(key, {"answer": "A"})

    # Still within TTL.
    clock.advance(4.9)
    assert cache.get(key) == {"answer": "A"}

    # Past TTL -> miss, and the entry is dropped.
    clock.advance(0.2)  # total 5.1 >= 5
    assert cache.get(key) is None
    assert len(cache) == 0


def test_cache_lru_eviction():
    cache = InMemoryTTLLRUCache(ttl_seconds=100, max_entries=2)
    cache.set(("t", "read", "q1"), {"answer": "1"})
    cache.set(("t", "read", "q2"), {"answer": "2"})

    # Access q1 so q2 becomes least-recently-used.
    assert cache.get(("t", "read", "q1")) == {"answer": "1"}

    # Insert q3 -> evicts q2 (LRU), keeps q1 and q3.
    cache.set(("t", "read", "q3"), {"answer": "3"})
    assert cache.get(("t", "read", "q2")) is None
    assert cache.get(("t", "read", "q1")) == {"answer": "1"}
    assert cache.get(("t", "read", "q3")) == {"answer": "3"}
    assert len(cache) == 2


def test_cache_returns_copy_not_reference():
    cache = InMemoryTTLLRUCache(ttl_seconds=10, max_entries=4)
    key = ("t", "read", "q")
    payload = {"answer": "A", "citation": {"a.md": "/a.md"}}
    cache.set(key, payload)

    # Mutating the input after set must not corrupt the stored entry.
    payload["answer"] = "MUTATED"
    got = cache.get(key)
    assert got["answer"] == "A"

    # Mutating the returned dict must not corrupt the stored entry either.
    got["answer"] = "ALSO MUTATED"
    assert cache.get(key)["answer"] == "A"


def test_cache_rejects_nonpositive_config():
    with pytest.raises(ValueError):
        InMemoryTTLLRUCache(ttl_seconds=0, max_entries=4)
    with pytest.raises(ValueError):
        InMemoryTTLLRUCache(ttl_seconds=10, max_entries=0)


# ===========================================================================
# Integration: generate_answer with the cache wired behind the flag
# ===========================================================================


class _FakeSearchClient:
    def search(self, **kwargs):
        yield {
            "id": "1",
            "content": "fake content",
            "filename": "doc.md",
            "filepath": "/docs/doc.md",
        }


class _FakeAzure:
    def __init__(self):
        self.search_client = _FakeSearchClient()
        self.embedding_client = MagicMock()
        self.openai_client = MagicMock()


@contextlib.contextmanager
def _pipeline_mocks(qna_pipeline, llm_mock):
    """Patch the leaf calls so generate_answer runs without real Azure.

    Uses the greeting path (is_greeting=True) to skip embedding/search so the
    only expensive call we count is the LLM (`generate_openai_response`).
    """
    with (
        patch.object(
            qna_pipeline,
            "rephrase_queries",
            new=AsyncMock(
                return_value={
                    "rephrased_query": "rephrased",
                    "is_greeting": True,
                    "extracted_snippet": "",
                    "is_followup": False,
                    "was_rephrased": False,
                }
            ),
        ),
        patch.object(qna_pipeline, "generate_openai_response", new=llm_mock),
        patch.object(
            qna_pipeline,
            "extract_answer_and_filenames_from_text",
            new=AsyncMock(return_value=("Answer text.", [])),
        ),
    ):
        yield


@pytest.mark.asyncio
async def test_cache_hit_returns_cached_and_emits_event(monkeypatch):
    """Second identical call hits the cache: LLM invoked once, cache_hit emitted."""
    from src.pipeline import qna_pipeline

    monkeypatch.setenv("QNA_CACHE_ENABLED", "true")
    reset_cache()

    llm = AsyncMock(return_value="Answer text.")
    events = []

    def capture_log_event(logger, event, **fields):
        events.append((event, fields))

    history = [{"user_query": "Hello there", "bot_response": None}]
    azure = _FakeAzure()

    with (
        _pipeline_mocks(qna_pipeline, llm),
        patch.object(qna_pipeline, "log_event", side_effect=capture_log_event),
    ):
        first = await qna_pipeline.generate_answer(
            query="Hello there", fr_mode="read", bot_tag="tenant-a", history=history, azure=azure
        )
        second = await qna_pipeline.generate_answer(
            query="Hello there", fr_mode="read", bot_tag="tenant-a", history=history, azure=azure
        )

    assert first == second
    # LLM called exactly once across two identical requests -> second was a hit.
    assert llm.await_count == 1
    assert any(e[0] == "cache_hit" for e in events), "cache_hit event not emitted"
    # The event is metadata-only: carries bot_tag/fr_mode, never the query/answer.
    hit = next(f for e, f in events if e == "cache_hit")
    assert hit["bot_tag"] == "tenant-a"
    assert hit["fr_mode"] == "read"
    assert "query" not in hit and "answer" not in hit


@pytest.mark.asyncio
async def test_cache_miss_populates(monkeypatch):
    """A first call populates the cache so a subsequent normalized-equal query hits."""
    from src.cache import answer_cache
    from src.pipeline import qna_pipeline

    monkeypatch.setenv("QNA_CACHE_ENABLED", "true")
    reset_cache()

    llm = AsyncMock(return_value="Answer text.")
    history = [{"user_query": "Hello there", "bot_response": None}]
    azure = _FakeAzure()

    with _pipeline_mocks(qna_pipeline, llm):
        await qna_pipeline.generate_answer(
            query="Hello there", fr_mode="read", bot_tag="tenant-a", history=history, azure=azure
        )
        # Stored under the normalized key.
        key = make_cache_key("tenant-a", "read", "Hello there")
        assert answer_cache.get_cache().get(key) is not None

        # A surface variant (extra whitespace / case) normalizes to the same key
        # and hits without another LLM call.
        await qna_pipeline.generate_answer(
            query="hello   THERE", fr_mode="read", bot_tag="tenant-a", history=history, azure=azure
        )

    assert llm.await_count == 1


@pytest.mark.asyncio
async def test_cache_never_hits_across_bot_tag(monkeypatch):
    """Same query + fr_mode but a different bot_tag must NEVER serve a cached answer."""
    from src.pipeline import qna_pipeline

    monkeypatch.setenv("QNA_CACHE_ENABLED", "true")
    reset_cache()

    llm = AsyncMock(return_value="Answer text.")
    history = [{"user_query": "Hello there", "bot_response": None}]
    azure = _FakeAzure()

    with _pipeline_mocks(qna_pipeline, llm):
        await qna_pipeline.generate_answer(
            query="Hello there", fr_mode="read", bot_tag="tenant-a", history=history, azure=azure
        )
        await qna_pipeline.generate_answer(
            query="Hello there", fr_mode="read", bot_tag="tenant-b", history=history, azure=azure
        )

    # Two DIFFERENT tenants -> two LLM calls; no cross-tenant hit.
    assert llm.await_count == 2


@pytest.mark.asyncio
async def test_cache_off_is_noop_and_byte_identical(monkeypatch):
    """Flag OFF (default): cache never consulted; output identical to no-cache run."""
    from src.pipeline import qna_pipeline

    monkeypatch.delenv("QNA_CACHE_ENABLED", raising=False)
    reset_cache()

    llm = AsyncMock(return_value="Answer text.")
    history = [{"user_query": "Hello there", "bot_response": None}]
    azure = _FakeAzure()

    with _pipeline_mocks(qna_pipeline, llm):
        first = await qna_pipeline.generate_answer(
            query="Hello there", fr_mode="read", bot_tag="tenant-a", history=history, azure=azure
        )
        second = await qna_pipeline.generate_answer(
            query="Hello there", fr_mode="read", bot_tag="tenant-a", history=history, azure=azure
        )

    # No caching: LLM called every time, and the two payloads are equal because
    # the pipeline is deterministic under the mocks (byte-identical behaviour).
    assert llm.await_count == 2
    assert first == second
    assert first == {"answer": "Answer text.", "citation": {}}


@pytest.mark.asyncio
async def test_cache_ttl_expiry_through_pipeline(monkeypatch):
    """An expired entry forces a fresh LLM call on the next identical request."""
    from src.cache import answer_cache
    from src.pipeline import qna_pipeline

    monkeypatch.setenv("QNA_CACHE_ENABLED", "true")

    clock = _FakeClock()
    # Inject a fake-clock cache as the singleton so we can expire deterministically.
    answer_cache._cache_singleton = InMemoryTTLLRUCache(ttl_seconds=5, max_entries=16, clock=clock)

    llm = AsyncMock(return_value="Answer text.")
    history = [{"user_query": "Hello there", "bot_response": None}]
    azure = _FakeAzure()

    with _pipeline_mocks(qna_pipeline, llm):
        await qna_pipeline.generate_answer(
            query="Hello there", fr_mode="read", bot_tag="tenant-a", history=history, azure=azure
        )
        # Within TTL -> hit, no new LLM call.
        await qna_pipeline.generate_answer(
            query="Hello there", fr_mode="read", bot_tag="tenant-a", history=history, azure=azure
        )
        assert llm.await_count == 1

        # Expire the entry.
        clock.advance(6)
        await qna_pipeline.generate_answer(
            query="Hello there", fr_mode="read", bot_tag="tenant-a", history=history, azure=azure
        )
        assert llm.await_count == 2
