"""Standard-route node — the thin wrapper around the unchanged pipeline.

This node calls the **unchanged** ``qna_pipeline.generate_answer(...)`` and
unpacks its ``{answer, citation}`` result into the agent state. It reimplements
none of the P0 guarantees (tenant isolation, request-scoping, the P0-6 error
contract) — they live in the pipeline and search layer and are reused verbatim.

Error flow (per the ADR): hard exceptions are **not** caught here. A pipeline
failure propagates through ``ainvoke`` → ``agentic_generate_answer`` (no
try/except) → the naked ``await`` in ``app.py`` → the global handler in
``core/errors.py`` → a 500 envelope with ``X-Request-ID``.
"""

from __future__ import annotations

import src.pipeline.qna_pipeline
from src.agents.state import AgentState
from src.core.logger import logger
from src.core.observability import log_event


async def standard_route(state: AgentState) -> dict:
    """Run the legacy pipeline and unpack its result into state keys.

    Reads ``query``/``fr_mode``/``bot_tag``/``history``/``azure``/``request_id``
    from state and writes only ``final_answer`` and ``citations``.

    Returns the partial-state update (LangGraph merges it into the running
    state); never the wire ``{answer, citation}`` shape — that mapping happens
    in ``agentic_generate_answer`` so the contract keys live in exactly one
    place.
    """
    request_id = state.get("request_id")
    log_event(logger, "agent_standard_route", request_id=request_id)

    ans = await src.pipeline.qna_pipeline.generate_answer(
        query=state["query"],
        fr_mode=state["fr_mode"],
        bot_tag=state["bot_tag"],
        history=state.get("history") or [],
        azure=state["azure"],
        # This version of generate_answer takes request_id explicitly, so the
        # inner pipeline stage events share the middleware correlation ID.
        request_id=request_id,
    )

    return {
        "final_answer": ans.get("answer", ""),
        "citations": ans.get("citation", {}) or {},
    }
