"""P3 LangGraph agentic layer (scaffold).

This package wraps the existing QnA pipeline in a LangGraph ``StateGraph``
without disturbing the operational invariants the P0/P1 work established. It
is gated behind the default-OFF ``QNA_AGENT_ENABLED`` flag (see
``src.config.config``); with the flag unset, ``/qna`` behaviour is byte-for-byte
identical to the legacy direct call and nothing in this package is imported.

PR0 scope (per docs/architect_phase_2/07_P3_LANGGRAPH_ADR.md, plan item 1):
behaviour-preserving scaffold + a linear ``START → classifier → standard_route
→ verifier → END`` graph. The classifier always routes ``standard`` (a stub —
real routing is a later PR) and the verifier is a pass-through no-op. The only
answer strategy wired is ``standard_route``, which calls the *unchanged*
``qna_pipeline.generate_answer`` and returns the same ``{answer, citation}``
shape.
"""
