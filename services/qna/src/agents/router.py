"""Agentic entrypoint + classifier node + compiled graph.

``agentic_generate_answer`` is the flag-ON counterpart to the legacy
``qna_pipeline.generate_answer``: it builds a fresh request-scoped
``AgentState``, runs the compiled graph, and returns the **same**
``{answer, citation}`` dict shape so the ``/qna`` wire contract (the #28
CitationMap contract) is preserved.

P3-PR1 graph: ``START → classifier → {route} → standard_route → verifier → END``.
The classifier is now a **real** structured-output node (PR0's always-"standard"
stub is gone): it calls ``services.openai_service.classify_route`` to set
``state["route"] ∈ {standard, map_reduce, react}`` and the graph branches on it
via ``add_conditional_edges``. Because the ``map_reduce``/``react`` answer nodes
ship in later PRs, all three route labels are mapped to ``standard_route`` for
now (the *classified* route is still logged truthfully — see ``_route_selector``).
So the flag-ON path routes for real but, downstream, behaves identically to the
legacy pipeline. The verifier is still a pass-through no-op.

Everything stays behind the existing default-OFF ``QNA_AGENT_ENABLED`` flag
(checked in ``app.py``); no new flag is introduced.

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
from src.services.openai_service import classify_route


async def classifier(state: AgentState) -> dict:
    """Real structured-output routing classifier (best-effort).

    Calls the structured-output ``classify_route`` helper to pick one of
    ``{standard, map_reduce, react}`` and writes it to ``state["route"]``.

    Best-effort per the ADR: on **any** exception (including a malformed/
    missing ``azure`` client) this logs a warning and defaults
    ``route="standard"`` — a classifier failure must never fail the request.
    The error path returns exactly ``{"route": "standard"}`` (no extra keys).

    Double-rephrasal guard: this node does **not** compute ``effective_query``
    on the live path. Everything currently collapses to ``standard_route``,
    which already rephrases internally via ``rephrase_queries``; rephrasing
    here too would double-rephrase. Warm-start rephrase for the non-self-
    rephrasing routes (map_reduce/react) arrives with those nodes in later PRs.

    Log hygiene: the ``route_decision`` event carries only ``route`` +
    ``request_id`` — never the raw query (it is not passed to ``log_event``).
    """
    request_id = state.get("request_id")
    try:
        route = await classify_route(state["azure"], state["query"])
    except Exception as exc:
        # Best-effort: warn and default to the safe route. NEVER raise.
        logger.warning(
            "Route classifier failed (%s); defaulting to 'standard'",
            type(exc).__name__,
        )
        log_event(
            logger,
            "agent_route_decision",
            request_id=request_id,
            route="standard",
            classifier_failed=True,
        )
        return {"route": "standard"}

    log_event(
        logger,
        "agent_route_decision",
        request_id=request_id,
        route=route,
    )
    return {"route": route}


def _route_selector(state: AgentState) -> str:
    """Conditional-edge selector — returns the classified route label.

    Returns the value the classifier wrote (defaulting to ``"standard"`` if,
    defensively, the key is missing). The returned label is mapped to a node
    by the ``path_map`` in ``_build_graph``; in PR1 every label maps to
    ``standard_route`` because the other answer nodes do not exist yet — but
    the *classified* route is preserved in state and logs, so wiring the real
    nodes later needs no classifier change.
    """
    return state.get("route", "standard")


def _build_graph():
    """Compile the routing graph once at import time.

    The compiled graph is stateless and immutable; per-request state is passed
    to ``ainvoke`` and never stored here.
    """
    graph = StateGraph(AgentState)
    graph.add_node("classifier", classifier)
    graph.add_node("standard_route", standard_route)
    graph.add_node("verifier", verifier)

    graph.add_edge(START, "classifier")
    # Branch on the classified route. map_reduce/react nodes ship in later PRs,
    # so for now all three labels resolve to standard_route — an edge to a
    # not-yet-added node would fail compile(). Documented collapse, not a bug.
    graph.add_conditional_edges(
        "classifier",
        _route_selector,
        {
            "standard": "standard_route",
            "map_reduce": "standard_route",
            "react": "standard_route",
        },
    )
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
