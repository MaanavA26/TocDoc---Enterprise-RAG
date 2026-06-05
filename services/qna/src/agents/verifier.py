"""Verifier convergence node — no-op in PR0.

The ADR's converged topology routes every answer strategy through a single
verifier before ``END`` so self-critique is authored once. In PR0 this node is
a **pass-through no-op**: the only wired strategy is ``standard_route``, which
calls ``generate_answer`` — and that pipeline exposes only ``{answer, citation}``,
not ``retrieved_chunks`` (``qna_pipeline``). A verifier with no evidence cannot
judge, so it adds nothing in v1 and intentionally leaves ``verified`` unset.

The real structured-output verifier (flag ``QNA_AGENT_VERIFY``) and the
follow-up that threads ``retrieved_chunks`` out of the pipeline are later PRs
(plan items 6 and 9). Keeping the node in the graph now means the topology does
not change when verification is enabled.
"""

from __future__ import annotations

from src.agents.state import AgentState
from src.core.logger import logger
from src.core.observability import log_event


async def verifier(state: AgentState) -> dict:
    """Pass state through unchanged (PR0 no-op).

    Writes no keys. Emits a structured event for traceability, pulling
    ``request_id`` from state (not the ContextVar, which does not cross
    executor threads).
    """
    log_event(
        logger,
        "agent_verifier_noop",
        request_id=state.get("request_id"),
        route=state.get("route"),
    )
    return {}
