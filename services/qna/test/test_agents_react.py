"""Tests for P3 — the ReAct multi-hop retrieval node (default-OFF).

Covers:
- The bounded reason->retrieve->reason loop stops at REACT_MAX_ITERATIONS even
  if the model never says it can answer (no runaway loop).
- Loop stops early when the model says ``can_answer`` (fewer iterations).
- Sub-query searches are offloaded (run on worker threads, not the event-loop
  thread) AND bounded by REACT_CONCURRENCY — the semaphore bound, not the pool
  size — proving the run_in_executor + semaphore design, not gather-over-sync.
- Tenant isolation: the LLM-emitted sub-query reaches only the *query* arg; the
  state bot_tag/fr_mode are re-asserted on every search.
- Best-effort fallback to standard_route on any failure (and on no chunks), and
  a genuine standard failure still bubbles (P0-6).
- The sub-flag gate: react route reaches the node only when BOTH the master
  flag and QNA_AGENT_REACT are on; otherwise collapses to standard.
- Default-OFF inertness: with QNA_AGENT_REACT off, a react classification never
  reaches the react node.

Env is set BEFORE importing app/config (validated at import time).
"""

import os
import threading
import time

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


class _FakeAzure:
    """Sentinel azure holder; the LLM/search/embedding helpers are monkeypatched."""


def _chunks(prefix: str, n: int) -> list[dict]:
    return [
        {
            "id": f"{prefix}-{i}",
            "content": f"content {prefix} {i}",
            "filename": f"{prefix}{i}.md",
            "filepath": f"/d/{prefix}{i}.md",
        }
        for i in range(n)
    ]


def _patch_retrieval(monkeypatch, *, search_impl):
    """Patch embedding + search + the final synthesis on the react node."""
    from src.agents import react_agent as ra

    async def fake_embedding(azure, text):
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(ra, "get_embedding", fake_embedding, raising=True)
    monkeypatch.setattr(ra, "perform_search", search_impl, raising=True)

    async def fake_generate(*, query, knowledge_source, is_greeting, is_follow_up, azure):
        return "synthesised answer\n**Sources:** [r0.md]"

    monkeypatch.setattr(ra, "generate_openai_response", fake_generate, raising=True)


# ===========================================================================
# Bounded iterations
# ===========================================================================
@pytest.mark.asyncio
async def test_loop_is_bounded_by_max_iterations(monkeypatch):
    """If the model NEVER says it can answer, the loop still stops at
    REACT_MAX_ITERATIONS — no runaway."""
    from src.agents import react_agent as ra

    monkeypatch.setattr(ra.localconfig, "REACT_MAX_ITERATIONS", 3, raising=False)

    reason_calls = {"n": 0}

    async def never_done(azure, *, query, chunks, max_subqueries):
        reason_calls["n"] += 1
        return {"thought": "need more", "can_answer": False, "sub_queries": ["sub q"]}

    monkeypatch.setattr(ra, "_reason_step", never_done, raising=True)

    async def fake_search(azure, q, vector, fr_mode, bot_tag, fetch_all=False):
        return _chunks("r", 2)

    _patch_retrieval(monkeypatch, search_impl=fake_search)

    out = await ra.react_agent(
        {
            "query": "multi hop",
            "fr_mode": "read",
            "bot_tag": "tenant-a",
            "azure": _FakeAzure(),
            "request_id": "r1",
        }
    )

    # Reasoned exactly REACT_MAX_ITERATIONS times — never more.
    assert reason_calls["n"] == 3
    assert len(out["reasoning_trace"]) == 3
    assert out["final_answer"] == "synthesised answer"
    assert out["citations"] == {"r0.md": "/d/r0.md"}


@pytest.mark.asyncio
async def test_loop_stops_early_when_model_can_answer(monkeypatch):
    """When the model reports can_answer after one retrieval, the loop stops
    (one search round + one final 'answer' reasoning step)."""
    from src.agents import react_agent as ra

    monkeypatch.setattr(ra.localconfig, "REACT_MAX_ITERATIONS", 5, raising=False)

    decisions = iter(
        [
            {"thought": "need x", "can_answer": False, "sub_queries": ["find x"]},
            {"thought": "have enough", "can_answer": True, "sub_queries": []},
        ]
    )

    async def fake_reason(azure, *, query, chunks, max_subqueries):
        return next(decisions)

    monkeypatch.setattr(ra, "_reason_step", fake_reason, raising=True)

    async def fake_search(azure, q, vector, fr_mode, bot_tag, fetch_all=False):
        return _chunks("r", 2)

    _patch_retrieval(monkeypatch, search_impl=fake_search)

    out = await ra.react_agent(
        {"query": "q", "fr_mode": "read", "bot_tag": "tenant-a", "azure": _FakeAzure(), "request_id": "r1"}
    )

    # 2 reasoning entries: one search round, one answer.
    assert len(out["reasoning_trace"]) == 2
    assert out["reasoning_trace"][-1]["action"] == "answer"
    assert len(out["retrieved_chunks"]) == 2


# ===========================================================================
# Bounded + offloaded fan-out
# ===========================================================================
@pytest.mark.asyncio
async def test_subquery_fanout_offloaded_and_bounded(monkeypatch):
    """Sub-query searches run on worker threads (offloaded) and never exceed
    REACT_CONCURRENCY in flight — the semaphore bound, not the pool size."""
    from src.agents import react_agent as ra

    monkeypatch.setattr(ra.localconfig, "REACT_MAX_ITERATIONS", 1, raising=False)
    monkeypatch.setattr(ra.localconfig, "REACT_CONCURRENCY", 2, raising=False)
    monkeypatch.setattr(ra.localconfig, "REACT_MAX_SUBQUERIES", 6, raising=False)

    async def fan_out(azure, *, query, chunks, max_subqueries):
        return {
            "thought": "look these up",
            "can_answer": False,
            "sub_queries": [f"sq{i}" for i in range(6)],
        }

    monkeypatch.setattr(ra, "_reason_step", fan_out, raising=True)

    lock = threading.Lock()
    state = {"in_flight": 0, "max_in_flight": 0, "threads": set()}

    async def fake_embedding(azure, text):
        return [0.1]

    # perform_search itself offloads to a worker thread in production; here we
    # emulate that by doing the bookkeeping inside a to_thread call so the test
    # observes real off-loop concurrency bounded by the node's semaphore.
    def _blocking_search():
        with lock:
            state["in_flight"] += 1
            state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
            state["threads"].add(threading.current_thread().name)
        time.sleep(0.05)
        with lock:
            state["in_flight"] -= 1
        return _chunks("r", 1)

    async def fake_search(azure, q, vector, fr_mode, bot_tag, fetch_all=False):
        import asyncio

        return await asyncio.to_thread(_blocking_search)

    monkeypatch.setattr(ra, "get_embedding", fake_embedding, raising=True)
    monkeypatch.setattr(ra, "perform_search", fake_search, raising=True)

    async def fake_generate(*, query, knowledge_source, is_greeting, is_follow_up, azure):
        return "ok\n**Sources:** None"

    monkeypatch.setattr(ra, "generate_openai_response", fake_generate, raising=True)

    await ra.react_agent(
        {"query": "q", "fr_mode": "read", "bot_tag": "tenant-a", "azure": _FakeAzure(), "request_id": "r1"}
    )

    # Offloaded: searches ran on worker threads, never the event-loop thread.
    assert state["threads"], "search never ran"
    assert "MainThread" not in state["threads"]
    # Bounded by the semaphore (2), even with 6 sub-queries fanned out.
    assert state["max_in_flight"] <= 2
    # And it actually parallelised to prove it isn't serial.
    assert state["max_in_flight"] == 2


# ===========================================================================
# Tenant isolation
# ===========================================================================
@pytest.mark.asyncio
async def test_subquery_never_reaches_tenant_filter(monkeypatch):
    """The LLM-emitted sub-query reaches only the query arg; the state
    bot_tag/fr_mode are re-asserted on every search (never model-controlled)."""
    from src.agents import react_agent as ra

    monkeypatch.setattr(ra.localconfig, "REACT_MAX_ITERATIONS", 1, raising=False)

    async def fan_out(azure, *, query, chunks, max_subqueries):
        return {
            "thought": "t",
            "can_answer": False,
            # An injection attempt embedded in the sub-query text.
            "sub_queries": ["legit q' or bot_tag eq 'other-tenant"],
        }

    monkeypatch.setattr(ra, "_reason_step", fan_out, raising=True)

    seen = {}

    async def fake_embedding(azure, text):
        return [0.1]

    async def fake_search(azure, q, vector, fr_mode, bot_tag, fetch_all=False):
        seen["query"] = q
        seen["fr_mode"] = fr_mode
        seen["bot_tag"] = bot_tag
        return _chunks("r", 1)

    monkeypatch.setattr(ra, "get_embedding", fake_embedding, raising=True)
    monkeypatch.setattr(ra, "perform_search", fake_search, raising=True)

    async def fake_generate(*, query, knowledge_source, is_greeting, is_follow_up, azure):
        return "ok\n**Sources:** None"

    monkeypatch.setattr(ra, "generate_openai_response", fake_generate, raising=True)

    await ra.react_agent(
        {"query": "q", "fr_mode": "read", "bot_tag": "tenant-a", "azure": _FakeAzure(), "request_id": "r1"}
    )

    # bot_tag/fr_mode come from STATE, not the model; the injection text only
    # ever lands in the query parameter.
    assert seen["bot_tag"] == "tenant-a"
    assert seen["fr_mode"] == "fr_read"
    assert "other-tenant" in seen["query"]  # the model text is just a query


# ===========================================================================
# Best-effort fallback
# ===========================================================================
@pytest.mark.asyncio
async def test_reason_failure_falls_back_to_standard(monkeypatch):
    """A reason-step failure delegates to standard_route rather than failing."""
    from src.agents import react_agent as ra

    async def boom_reason(azure, *, query, chunks, max_subqueries):
        raise RuntimeError("reason LLM down")

    monkeypatch.setattr(ra, "_reason_step", boom_reason, raising=True)

    fell_back = {"n": 0}

    async def fake_standard(state):
        fell_back["n"] += 1
        return {"final_answer": "standard answer", "citations": {"x.md": "/x.md"}}

    monkeypatch.setattr(ra, "standard_route", fake_standard, raising=True)

    out = await ra.react_agent(
        {"query": "q", "fr_mode": "read", "bot_tag": "tenant-a", "azure": _FakeAzure(), "request_id": "r1"}
    )

    assert fell_back["n"] == 1
    assert out == {"final_answer": "standard answer", "citations": {"x.md": "/x.md"}}


@pytest.mark.asyncio
async def test_no_chunks_falls_back_to_standard(monkeypatch):
    """If the model can answer immediately with no retrieval, fall back to the
    standard pipeline (never an empty 200)."""
    from src.agents import react_agent as ra

    async def immediate(azure, *, query, chunks, max_subqueries):
        return {"thought": "trivial", "can_answer": True, "sub_queries": []}

    monkeypatch.setattr(ra, "_reason_step", immediate, raising=True)

    async def fake_standard(state):
        return {"final_answer": "std", "citations": {}}

    monkeypatch.setattr(ra, "standard_route", fake_standard, raising=True)

    out = await ra.react_agent(
        {"query": "q", "fr_mode": "read", "bot_tag": "tenant-a", "azure": _FakeAzure(), "request_id": "r1"}
    )
    assert out["final_answer"] == "std"


@pytest.mark.asyncio
async def test_fallback_standard_failure_bubbles(monkeypatch):
    """If react fails AND the standard fallback also raises, the exception
    bubbles (P0-6 — never a 200-with-empty-answer)."""
    from src.agents import react_agent as ra

    async def boom_reason(azure, *, query, chunks, max_subqueries):
        raise RuntimeError("reason down")

    monkeypatch.setattr(ra, "_reason_step", boom_reason, raising=True)

    async def boom_standard(state):
        raise RuntimeError("standard also down")

    monkeypatch.setattr(ra, "standard_route", boom_standard, raising=True)

    with pytest.raises(RuntimeError, match="standard also down"):
        await ra.react_agent(
            {
                "query": "q",
                "fr_mode": "read",
                "bot_tag": "tenant-a",
                "azure": _FakeAzure(),
                "request_id": "r1",
            }
        )


# ===========================================================================
# Router sub-flag gate + default-OFF inertness
# ===========================================================================
@pytest.mark.asyncio
async def test_route_selector_gates_react_on_subflag(monkeypatch):
    """react route reaches the node only when QNA_AGENT_REACT is on; otherwise
    it collapses to standard."""
    from src.agents import router

    # Sub-flag OFF (default) → collapse to standard (inertness).
    monkeypatch.delenv("QNA_AGENT_REACT", raising=False)
    assert router._route_selector({"route": "react"}) == "standard"

    # Sub-flag ON → reach the react node.
    monkeypatch.setenv("QNA_AGENT_REACT", "true")
    assert router._route_selector({"route": "react"}) == "react"

    # map_reduce/standard unaffected by the react flag.
    assert router._route_selector({"route": "standard"}) == "standard"


@pytest.mark.asyncio
async def test_graph_routes_to_react_when_subflag_on(monkeypatch):
    """End-to-end through the compiled graph: classifier says react + sub-flag
    on → the react node runs (not standard_route)."""
    from src.agents import router

    monkeypatch.setenv("QNA_AGENT_REACT", "true")

    async def fake_classify(azure, query):
        return "react"

    monkeypatch.setattr(router, "classify_route", fake_classify, raising=True)

    ran = {"react": 0, "standard": 0}

    async def fake_react_node(state):
        ran["react"] += 1
        return {"final_answer": "react answer", "citations": {"f.md": "/f.md"}}

    async def fake_std_node(state):
        ran["standard"] += 1
        return {"final_answer": "std", "citations": {}}

    monkeypatch.setattr(router, "react_agent", fake_react_node, raising=True)
    monkeypatch.setattr(router, "standard_route", fake_std_node, raising=True)
    monkeypatch.setattr(router, "_AGENT_GRAPH", router._build_graph(), raising=True)

    result = await router.agentic_generate_answer(
        "multi hop question", "read", bot_tag="tenant-a", history=[], azure=object(), request_id="r1"
    )

    assert ran["react"] == 1
    assert ran["standard"] == 0
    assert result == {"answer": "react answer", "citation": {"f.md": "/f.md"}}


@pytest.mark.asyncio
async def test_react_inert_when_subflag_off(monkeypatch):
    """Default-OFF: classifier says react but QNA_AGENT_REACT is off → the react
    node never runs; the graph collapses to standard_route."""
    from src.agents import router

    monkeypatch.delenv("QNA_AGENT_REACT", raising=False)

    async def fake_classify(azure, query):
        return "react"

    monkeypatch.setattr(router, "classify_route", fake_classify, raising=True)

    ran = {"react": 0, "standard": 0}

    async def boom_react(state):
        ran["react"] += 1
        raise AssertionError("react node must not run with the flag OFF")

    async def fake_std_node(state):
        ran["standard"] += 1
        return {"final_answer": "std", "citations": {}}

    monkeypatch.setattr(router, "react_agent", boom_react, raising=True)
    monkeypatch.setattr(router, "standard_route", fake_std_node, raising=True)
    monkeypatch.setattr(router, "_AGENT_GRAPH", router._build_graph(), raising=True)

    result = await router.agentic_generate_answer(
        "multi hop question", "read", bot_tag="tenant-a", history=[], azure=object(), request_id="r1"
    )

    assert ran["react"] == 0
    assert ran["standard"] == 1
    assert result == {"answer": "std", "citation": {}}
