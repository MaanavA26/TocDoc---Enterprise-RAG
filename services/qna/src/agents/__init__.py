"""P3 LangGraph agentic layer (scaffold).

This package wraps the existing QnA pipeline in a LangGraph ``StateGraph``
without disturbing the operational invariants the P0/P1 work established. It
is gated behind the default-OFF ``QNA_AGENT_ENABLED`` flag (see
``src.config.config``); with the flag unset, ``/qna`` behaviour is byte-for-byte
identical to the legacy direct call and nothing in this package is imported.

Scope (per docs/architect_phase_2/07_P3_LANGGRAPH_ADR.md): a
``START → classifier → {route} → standard_route → verifier → END`` graph.
The classifier is a **real** structured-output node that sets
``state["route"] ∈ {standard, map_reduce, react}`` and the graph branches on it
via ``add_conditional_edges``. The ``map_reduce``/``react`` answer nodes ship in
later PRs, so all three route labels currently resolve to ``standard_route`` —
the classified route is still logged truthfully, so wiring the real nodes later
needs no classifier change. The verifier is a pass-through no-op. The only
answer strategy wired is ``standard_route``, which calls the *unchanged*
``qna_pipeline.generate_answer`` and returns the same ``{answer, citation}``
shape.
"""
