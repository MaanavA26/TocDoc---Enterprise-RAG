"""Tests for P3-PR2 — the map-reduce summariser node (default-OFF).

Covers:
- ``perform_search(fetch_all=True)`` pages past TOP_K (mock paged results)
  while the default call is byte-identical (TOP_K cap, same kwargs).
- The map step is offloaded onto the dedicated executor (NOT a bare
  gather-over-sync) and bounded by ``MAP_REDUCE_CONCURRENCY`` — asserted via a
  worker-thread check + a max-in-flight counter, with the executor sized larger
  than the semaphore so the bound is attributable to the semaphore.
- Batching: chunks split into ceil(n / MAP_REDUCE_BATCH_SIZE) map calls.
- Reduce combines the extracts and citations resolve via _norm_name/_stem.
- Best-effort fallback to standard_route on map/reduce failure (and a genuine
  standard failure still bubbles — P0-6).
- The sub-flag gate: map_reduce route only reaches the node when BOTH the
  master flag and QNA_AGENT_MAP_REDUCE are on; otherwise collapses to standard.

Env is set BEFORE importing app/config (validated at import time).
"""

import os
import threading

import pytest

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


# ===========================================================================
# perform_search fetch_all
# ===========================================================================
class _FakeSearchClient:
    """Records every search call's kwargs and returns a fixed row count."""

    def __init__(self, row_count: int):
        self._calls: list[dict] = []
        self._row_count = row_count

    def search(self, **kwargs):
        self._calls.append(kwargs)
        # The real SDK returns up to `top` rows; emulate that cap so the test
        # proves the lifted ceiling, not an arbitrary fixed list.
        n = min(self._row_count, kwargs["top"])
        return iter(
            [
                {"id": str(i), "content": f"c{i}", "filename": f"f{i}.md", "filepath": f"/d/f{i}.md"}
                for i in range(n)
            ]
        )


class _FakeAzureSearch:
    def __init__(self, row_count: int):
        self.search_client = _FakeSearchClient(row_count)


@pytest.mark.asyncio
async def test_perform_search_default_caps_at_top_k(monkeypatch):
    """Default call (no fetch_all) caps at TOP_K and sends byte-identical
    kwargs — the existing-caller contract is unchanged."""
    from src.services import search_service as ss

    monkeypatch.setattr(ss.localconfig, "AZURE_SEARCH_SEMANTIC_CONFIG", "", raising=False)
    azure = _FakeAzureSearch(row_count=200)

    results = await ss.perform_search(azure, "q", [0.1] * 3, "fr_read", "tenant-a")

    assert len(azure.search_client._calls) == 1
    call = azure.search_client._calls[0]
    assert call["top"] == ss.localconfig.TOP_K == 20
    assert len(results) == 20  # capped
    # Filter + isolation preserved.
    assert "fr_tag eq 'fr_read'" in call["filter"]
    assert "bot_tag eq 'tenant-a'" in call["filter"]


@pytest.mark.asyncio
async def test_perform_search_fetch_all_pages_past_top_k(monkeypatch):
    """fetch_all=True lifts the cap to MAP_REDUCE_MAX_CHUNKS so retrieval gets
    far more than TOP_K=20."""
    from src.services import search_service as ss

    monkeypatch.setattr(ss.localconfig, "AZURE_SEARCH_SEMANTIC_CONFIG", "", raising=False)
    monkeypatch.setattr(ss.localconfig, "MAP_REDUCE_MAX_CHUNKS", 500, raising=False)
    azure = _FakeAzureSearch(row_count=137)

    results = await ss.perform_search(azure, "q", [0.1] * 3, "fr_read", "tenant-a", fetch_all=True)

    call = azure.search_client._calls[0]
    assert call["top"] == 500  # lifted ceiling
    assert len(results) == 137  # well past TOP_K=20
    # The KNN k matches the lifted top too (not the TOP_K cap).
    assert call["vector_queries"][0].k_nearest_neighbors == 500


# ===========================================================================
# map_reduce node — batching + bounded executor fan-out
# ===========================================================================
def _chunks(n: int) -> list[dict]:
    return [
        {"id": str(i), "content": f"content {i}", "filename": f"f{i}.md", "filepath": f"/d/f{i}.md"}
        for i in range(n)
    ]


class _FakeAzure:
    """Sentinel azure holder; the LLM/search helpers are monkeypatched."""


def _patch_node_io(monkeypatch, *, chunks, reduce_answer):
    """Patch the node's retrieval preamble + reduce LLM; leave map patchable
    per-test. Returns nothing — tests patch map_extract_sync themselves."""
    from src.agents import map_reduce as mr

    async def fake_embedding(azure, text):
        return [0.1, 0.2, 0.3]

    async def fake_search(azure, query, vector, fr_mode, bot_tag, fetch_all=False):
        # Prove the node asked for ALL chunks and used the tag-prefixed mode.
        assert fetch_all is True
        assert fr_mode == "fr_read"
        assert bot_tag == "tenant-a"
        return chunks

    monkeypatch.setattr(mr, "get_embedding", fake_embedding, raising=True)
    monkeypatch.setattr(mr, "perform_search", fake_search, raising=True)

    def fake_reduce(azure, *, query, extracts, model=None):
        return reduce_answer

    monkeypatch.setattr(mr.openai_service, "reduce_combine_sync", fake_reduce, raising=True)


@pytest.mark.asyncio
async def test_map_reduce_batches_and_reduces(monkeypatch):
    """n chunks split into ceil(n/batch) map calls; reduce combines extracts
    and citations resolve via the tolerant filename matcher."""
    from src.agents import map_reduce as mr

    chunks = _chunks(45)  # 45 chunks
    monkeypatch.setattr(mr.localconfig, "MAP_REDUCE_BATCH_SIZE", 20, raising=False)
    monkeypatch.setattr(mr.localconfig, "MAP_REDUCE_CONCURRENCY", 4, raising=False)
    # Distinct map/reduce models so the split (ADR: map=mini, reduce=larger) is
    # observable on the model kwarg passed to each helper.
    monkeypatch.setattr(mr.localconfig, "AZURE_LLM_MODEL", "map-model", raising=False)
    monkeypatch.setattr(mr.localconfig, "AZURE_OPENAI_REDUCE_MODEL", "reduce-model", raising=False)

    captured = {"reduce_model": None}

    async def fake_embedding(azure, text):
        return [0.1, 0.2, 0.3]

    async def fake_search(azure, query, vector, fr_mode, bot_tag, fetch_all=False):
        assert fetch_all is True
        assert fr_mode == "fr_read"
        assert bot_tag == "tenant-a"
        return chunks

    def fake_reduce(azure, *, query, extracts, model=None):
        captured["reduce_model"] = model
        return "The combined summary.\n**Sources:** [f1.md; f3.md]"

    monkeypatch.setattr(mr, "get_embedding", fake_embedding, raising=True)
    monkeypatch.setattr(mr, "perform_search", fake_search, raising=True)
    monkeypatch.setattr(mr.openai_service, "reduce_combine_sync", fake_reduce, raising=True)

    map_calls = {"n": 0, "models": set()}

    def fake_map(azure, *, query, sources, model=None):
        map_calls["n"] += 1
        map_calls["models"].add(model)
        return f"extract for batch (len source chars={len(sources)})"

    monkeypatch.setattr(mr.openai_service, "map_extract_sync", fake_map, raising=True)

    out = await mr.map_reduce(
        {
            "query": "summarize everything",
            "fr_mode": "read",
            "bot_tag": "tenant-a",
            "azure": _FakeAzure(),
            "request_id": "r1",
        }
    )

    # 45 chunks / batch 20 => 3 map calls (3 batches).
    assert map_calls["n"] == 3
    assert out["final_answer"] == "The combined summary."
    # Citations resolved against the all-chunk file_map.
    assert out["citations"] == {"f1.md": "/d/f1.md", "f3.md": "/d/f3.md"}
    assert out["retrieved_chunks"] is chunks
    assert len(out["partial_answers"]) == 3
    # Model split: map uses AZURE_LLM_MODEL, reduce uses AZURE_OPENAI_REDUCE_MODEL.
    assert map_calls["models"] == {"map-model"}
    assert captured["reduce_model"] == "reduce-model"


@pytest.mark.asyncio
async def test_map_fan_out_is_offloaded_and_bounded(monkeypatch):
    """The map step runs on worker threads (offloaded, not bare-sync on the
    event loop) AND never exceeds MAP_REDUCE_CONCURRENCY in flight — proving
    the run_in_executor + semaphore design, not a gather-over-sync.

    Executor is sized LARGER than the concurrency limit so the observed bound
    is attributable to the SEMAPHORE, not the pool size.
    """
    from src.agents import map_reduce as mr

    chunks = _chunks(40)  # 8 batches at batch size 5
    monkeypatch.setattr(mr.localconfig, "MAP_REDUCE_BATCH_SIZE", 5, raising=False)
    monkeypatch.setattr(mr.localconfig, "MAP_REDUCE_CONCURRENCY", 2, raising=False)
    # Pool bigger than the semaphore bound so the bound can't come from it.
    from concurrent.futures import ThreadPoolExecutor

    monkeypatch.setattr(mr, "map_reduce_executor", ThreadPoolExecutor(max_workers=8), raising=True)

    _patch_node_io(monkeypatch, chunks=chunks, reduce_answer="done\n**Sources:** None")

    lock = threading.Lock()
    state = {"in_flight": 0, "max_in_flight": 0, "threads": set()}

    def fake_map(azure, *, query, sources, model=None):
        with lock:
            state["in_flight"] += 1
            state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
            state["threads"].add(threading.current_thread().name)
        # Hold long enough for concurrent calls to overlap.
        import time

        time.sleep(0.05)
        with lock:
            state["in_flight"] -= 1
        return "extract"

    monkeypatch.setattr(mr.openai_service, "map_extract_sync", fake_map, raising=True)

    await mr.map_reduce(
        {
            "query": "q",
            "fr_mode": "read",
            "bot_tag": "tenant-a",
            "azure": _FakeAzure(),
            "request_id": "r1",
        }
    )

    # (a) Offloaded: map ran on worker threads, never the main/event-loop thread.
    assert state["threads"], "map never ran"
    assert "MainThread" not in state["threads"]
    # (b) Bounded by the semaphore (2), even though the pool allows 8.
    assert state["max_in_flight"] <= 2
    # And it actually parallelised (more than one concurrent) to prove it's not
    # serial — with 8 batches and a 2-bound this must reach 2.
    assert state["max_in_flight"] == 2


@pytest.mark.asyncio
async def test_map_reduce_drops_irrelevant_extracts(monkeypatch):
    """NO_RELEVANT_INFORMATION extracts are filtered before the reduce step."""
    from src.agents import map_reduce as mr

    chunks = _chunks(6)
    monkeypatch.setattr(mr.localconfig, "MAP_REDUCE_BATCH_SIZE", 2, raising=False)

    captured = {}

    def fake_reduce(azure, *, query, extracts, model=None):
        captured["extracts"] = extracts
        return "ok\n**Sources:** None"

    async def fake_embedding(azure, text):
        return [0.1]

    async def fake_search(azure, query, vector, fr_mode, bot_tag, fetch_all=False):
        return chunks

    monkeypatch.setattr(mr, "get_embedding", fake_embedding, raising=True)
    monkeypatch.setattr(mr, "perform_search", fake_search, raising=True)
    monkeypatch.setattr(mr.openai_service, "reduce_combine_sync", fake_reduce, raising=True)

    outputs = iter(["real extract", "NO_RELEVANT_INFORMATION", "  "])

    def fake_map(azure, *, query, sources, model=None):
        return next(outputs)

    monkeypatch.setattr(mr.openai_service, "map_extract_sync", fake_map, raising=True)

    out = await mr.map_reduce(
        {"query": "q", "fr_mode": "read", "bot_tag": "tenant-a", "azure": _FakeAzure(), "request_id": "r1"}
    )

    # Only the one real extract survives into the reduce input and partial_answers.
    assert out["partial_answers"] == ["real extract"]
    assert "real extract" in captured["extracts"]
    assert "NO_RELEVANT_INFORMATION" not in captured["extracts"]


# ===========================================================================
# Best-effort fallback
# ===========================================================================
@pytest.mark.asyncio
async def test_map_failure_falls_back_to_standard(monkeypatch):
    """A map failure (after retries) delegates to standard_route rather than
    failing the request."""
    from src.agents import map_reduce as mr

    chunks = _chunks(4)
    monkeypatch.setattr(mr.localconfig, "MAP_REDUCE_BATCH_SIZE", 2, raising=False)
    # Skip real backoff sleeps.
    monkeypatch.setattr(mr.asyncio, "sleep", _no_sleep, raising=True)

    async def fake_embedding(azure, text):
        return [0.1]

    async def fake_search(azure, query, vector, fr_mode, bot_tag, fetch_all=False):
        return chunks

    monkeypatch.setattr(mr, "get_embedding", fake_embedding, raising=True)
    monkeypatch.setattr(mr, "perform_search", fake_search, raising=True)

    def boom_map(azure, *, query, sources, model=None):
        raise RuntimeError("map LLM down")

    monkeypatch.setattr(mr.openai_service, "map_extract_sync", boom_map, raising=True)

    fell_back = {"n": 0}

    async def fake_standard(state):
        fell_back["n"] += 1
        return {"final_answer": "standard answer", "citations": {"x.md": "/x.md"}}

    monkeypatch.setattr(mr, "standard_route", fake_standard, raising=True)

    out = await mr.map_reduce(
        {"query": "q", "fr_mode": "read", "bot_tag": "tenant-a", "azure": _FakeAzure(), "request_id": "r1"}
    )

    assert fell_back["n"] == 1
    assert out == {"final_answer": "standard answer", "citations": {"x.md": "/x.md"}}


@pytest.mark.asyncio
async def test_empty_chunks_falls_back_to_standard(monkeypatch):
    """Zero retrieved chunks → fall back to standard (never an empty 200)."""
    from src.agents import map_reduce as mr

    async def fake_embedding(azure, text):
        return [0.1]

    async def fake_search(azure, query, vector, fr_mode, bot_tag, fetch_all=False):
        return []

    monkeypatch.setattr(mr, "get_embedding", fake_embedding, raising=True)
    monkeypatch.setattr(mr, "perform_search", fake_search, raising=True)

    async def fake_standard(state):
        return {"final_answer": "std", "citations": {}}

    monkeypatch.setattr(mr, "standard_route", fake_standard, raising=True)

    out = await mr.map_reduce(
        {"query": "q", "fr_mode": "read", "bot_tag": "tenant-a", "azure": _FakeAzure(), "request_id": "r1"}
    )
    assert out["final_answer"] == "std"


@pytest.mark.asyncio
async def test_fallback_standard_failure_bubbles(monkeypatch):
    """If map fails AND the standard fallback also raises, the exception
    bubbles (P0-6 — never a 200-with-empty-answer)."""
    from src.agents import map_reduce as mr

    async def fake_embedding(azure, text):
        raise RuntimeError("embed down")  # forces the except path immediately

    monkeypatch.setattr(mr, "get_embedding", fake_embedding, raising=True)

    async def boom_standard(state):
        raise RuntimeError("standard also down")

    monkeypatch.setattr(mr, "standard_route", boom_standard, raising=True)

    with pytest.raises(RuntimeError, match="standard also down"):
        await mr.map_reduce(
            {
                "query": "q",
                "fr_mode": "read",
                "bot_tag": "tenant-a",
                "azure": _FakeAzure(),
                "request_id": "r1",
            }
        )


async def _no_sleep(*a, **k):
    return None


# ===========================================================================
# Router sub-flag gate
# ===========================================================================
@pytest.mark.asyncio
async def test_route_selector_gates_on_subflag(monkeypatch):
    """map_reduce route reaches the node only when QNA_AGENT_MAP_REDUCE is on;
    otherwise it collapses to standard."""
    from src.agents import router

    # Sub-flag OFF (default) → collapse to standard.
    monkeypatch.delenv("QNA_AGENT_MAP_REDUCE", raising=False)
    assert router._route_selector({"route": "map_reduce"}) == "standard"

    # Sub-flag ON → reach the map_reduce node.
    monkeypatch.setenv("QNA_AGENT_MAP_REDUCE", "true")
    assert router._route_selector({"route": "map_reduce"}) == "map_reduce"

    # react has no live node → always standard.
    assert router._route_selector({"route": "react"}) == "standard"
    # standard always standard.
    assert router._route_selector({"route": "standard"}) == "standard"


@pytest.mark.asyncio
async def test_graph_routes_to_map_reduce_when_subflag_on(monkeypatch):
    """End-to-end through the compiled graph: classifier says map_reduce +
    sub-flag on → the map_reduce node runs (not standard_route)."""
    from src.agents import router

    monkeypatch.setenv("QNA_AGENT_MAP_REDUCE", "true")

    async def fake_classify(azure, query):
        return "map_reduce"

    monkeypatch.setattr(router, "classify_route", fake_classify, raising=True)

    ran = {"map_reduce": 0, "standard": 0}

    async def fake_mr_node(state):
        ran["map_reduce"] += 1
        return {"final_answer": "mr answer", "citations": {"f.md": "/f.md"}}

    async def fake_std_node(state):
        ran["standard"] += 1
        return {"final_answer": "std", "citations": {}}

    # Patch the node functions the compiled graph already captured. The graph
    # holds references taken at compile, so patch via the router-bound names
    # used in _build_graph by rebuilding the graph against the patched nodes.
    monkeypatch.setattr(router, "map_reduce", fake_mr_node, raising=True)
    monkeypatch.setattr(router, "standard_route", fake_std_node, raising=True)
    monkeypatch.setattr(router, "_AGENT_GRAPH", router._build_graph(), raising=True)

    result = await router.agentic_generate_answer(
        "summarize everything", "read", bot_tag="tenant-a", history=[], azure=object(), request_id="r1"
    )

    assert ran["map_reduce"] == 1
    assert ran["standard"] == 0
    assert result == {"answer": "mr answer", "citation": {"f.md": "/f.md"}}
