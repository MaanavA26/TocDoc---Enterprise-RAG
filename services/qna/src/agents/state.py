"""Request-scoped LangGraph state for the P3 agentic layer.

``AgentState`` is a single ``TypedDict`` constructed fresh per request inside
``agents.router.agentic_generate_answer`` and never stored at module level —
request-scoping is structural, mirroring the ``generate_answer`` contract
(``qna_pipeline.generate_answer``). ``total=False`` keeps every key optional so
partial graphs are valid and the schema does not balloon.

Invariant (documented, enforced by convention): **each node reads its declared
inputs and writes only its own output keys** — no two nodes write the same key.
This directly answers the overlapping-mutation risk.

Two deliberate consequences (per the ADR):
  1. ``azure`` and ``request_id`` live *in state* — nodes never construct
     clients and never trust the ``ContextVar`` (which does not reliably cross
     the executor threads search/LLM calls offload onto).
  2. Because state carries the live ``azure`` client, ``AgentState`` is
     non-JSON-serializable; this intentionally rules out the LangGraph
     checkpointer. ``bot_tag``/``fr_mode`` are state-only and (in later PRs)
     bound into tool closures, never surfaced as LLM-visible tool parameters.

The output keys below the router/answer/verifier sections are populated by
later PRs; in PR0 only ``route`` (classifier stub), ``final_answer`` and
``citations`` (standard_route) are written.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict


class AgentState(TypedDict, total=False):
    # --- Required on entry (set by the wrapper; mirror generate_answer's contract) ---
    query: str  # latest user utterance
    fr_mode: str  # 'read' | 'layout'
    bot_tag: str  # tenant key — flows state->node->perform_search; NEVER LLM-visible
    history: list[dict]  # conversation turns (windowed by the handler)
    azure: Any  # the live Azure client holder from request.app.state.azure
    request_id: str  # from request.state.request_id; passed explicitly to log_event()

    # --- Router output (written only by the classifier node) ---
    route: Literal["standard", "map_reduce", "react"]
    effective_query: str  # warm-start rephrase; set ONLY on non-self-rephrasing routes
    is_followup: bool

    # --- Answer-node outputs (each key written by exactly one node) ---
    retrieved_chunks: list[dict]  # map_reduce / react; ABSENT on standard route in v1
    partial_answers: list[str]  # map_reduce map step only
    final_answer: str  # whichever answer node ran
    citations: dict[str, str]  # whichever answer node ran
    reasoning_trace: list[dict]  # react only (thought/action/observation per iter)

    # --- Verifier output ---
    verified: bool
    unsupported_claims: list[str]

    # --- Soft logical-error sentinel (checked by the wrapper post-invoke) ---
    error: dict | None
