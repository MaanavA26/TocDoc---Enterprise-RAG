"""Agentic entrypoint + classifier node + compiled graph.

``agentic_generate_answer`` is the flag-ON counterpart to the legacy
``qna_pipeline.generate_answer``: it builds a fresh request-scoped
``AgentState``, runs the compiled graph, and returns the **same**
``{answer, citation}`` dict shape so the ``/qna`` wire contract (the #28
CitationMap contract) is preserved.

PR0 graph (linear): ``START → classifier → standard_route → verifier → END``.
The classifier is a stub that always sets ``route="standard"`` — real
structured-output routing with ``add_conditional_edges`` is PR2 (flag
``QNA_AGENT_ROUTER``). The verifier is a pass-through no-op.

The only module-level objects are the compiled, stateless graph and (later)
the bounded executor — both immutable. State is never held at module level;
request-scoping is structural, matching the ``generate_answer`` contract.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from src.agents.standard_route import standard_route
from src.agents.state import AgentState
from src.agents.verifier import verifier
from src.core.logger import logger
from src.core.observability import log_event


async def classifier(state: AgentState) -> dict:
    """Routing classifier — PR0 stub.

    Always selects the standard route. A later PR replaces this with a
    structured-output classifier (flag ``QNA_AGENT_ROUTER``) and
    ``add_conditional_edges``; until then routing is deterministic so the
    flag-ON path behaves identically to the legacy pipeline.
    """
    log_event(
        logger,
        "agent_route_decision",
        request_id=state.get("request_id"),
        route="standard",
        stub=True,
    )
    return {"route": "standard"}


def _build_graph():
    """Compile the PR0 linear graph once at import time.

    The compiled graph is stateless and immutable; per-request state is passed
    to ``ainvoke`` and never stored here.
    """
    graph = StateGraph(AgentState)
    graph.add_node("classifier", classifier)
    graph.add_node("standard_route", standard_route)
    graph.add_node("verifier", verifier)

    graph.add_edge(START, "classifier")
    graph.add_edge("classifier", "standard_route")
    graph.add_edge("standard_route", "verifier")
    graph.add_edge("verifier", END)

    return graph.compile()


# Module-level, stateless, immutable compiled graph (built once per process).
_AGENT_GRAPH = _build_graph()


async def agentic_generate_answer(
    query: str,
    fr_mode: str,
    *,
    bot_tag: str,
    history: list[dict],
    azure: Any,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Flag-ON entrypoint: run the agent graph, return ``{answer, citation}``.

    Mirrors the ``generate_answer`` contract (request-scoped, everything
    explicit, no module-level mutable globals) and returns the **same** wire
    shape so ``/qna`` stays byte-identical for clients.

    Hard exceptions from any node are intentionally not caught here; they
    bubble to the global handler in ``core/errors.py`` for a 500 envelope with
    ``X-Request-ID`` (the P0-6 contract).
    """
    state: AgentState = {
        "query": query,
        "fr_mode": fr_mode,
        "bot_tag": bot_tag,
        "history": history or [],
        "azure": azure,
    }
    if request_id is not None:
        state["request_id"] = request_id

    log_event(logger, "agent_graph_start", request_id=request_id)

    final_state = await _AGENT_GRAPH.ainvoke(state)

    # Map the graph's internal state keys back onto the frozen wire contract.
    # The contract keys (answer/citation) live in exactly one place.
    return {
        "answer": final_state.get("final_answer", ""),
        "citation": final_state.get("citations", {}) or {},
    }
