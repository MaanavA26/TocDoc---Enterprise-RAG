"""ReAct multi-hop retrieval node (P3-3). Flag: ``QNA_AGENT_REACT``.

Some questions cannot be answered from a single top-K retrieval: they need
*dependent* lookups ("which vendor owns project X, and what is that vendor's
compliance score?") where the second search depends on what the first turned
up. This node runs a **bounded** reason → retrieve → reason loop:

  1. **Reason.** A structured-output LLM call (the existing generic
     ``openai_service._structured_completion_sync`` — its docstring explicitly
     anticipates reuse by "later P3 nodes") inspects the question plus the
     evidence gathered so far and decides whether it can answer yet. If not, it
     emits up to ``REACT_MAX_SUBQUERIES`` search sub-queries.
  2. **Retrieve.** Those sub-queries are embedded + searched, then the new
     chunks are folded into the running evidence set (de-duped by chunk id).
  3. Repeat until the model says it can answer, or ``REACT_MAX_ITERATIONS`` is
     hit — whichever comes first. Either way a final grounded answer is
     synthesised over the accumulated chunks via the **unchanged**
     ``generate_openai_response`` (same prompt/citation contract as the
     standard route), and citations are resolved with the same tolerant
     ``_norm_name``/``_stem`` matcher the map-reduce node reuses.

## Bounds — non-negotiable (per the task + ADR)

- **Iterations are hard-capped** at ``REACT_MAX_ITERATIONS`` (default 5). A
  mis-reasoning model can never loop forever or melt the Azure quota.
- **Concurrency is bounded.** ``azure.openai_client`` is **synchronous** behind
  the shared 2-worker ``openai_executor`` (``openai_service.py``); a bare
  ``asyncio.gather`` over direct sync calls parallelises nothing. So every LLM
  call is offloaded with ``run_in_executor`` onto that **existing** executor,
  and when a single reason step fans out into several sub-query searches those
  run concurrently bounded by a **per-request** ``asyncio.Semaphore`` —
  created INSIDE the async node (a module-level semaphore would bind to the
  import-time loop and break under per-request / per-test loops), mirroring the
  map-reduce pattern. ``gather`` is then correct (it gathers over offloaded
  futures, not bare sync calls).

## Best-effort fallback (per the ADR)

ReAct is a non-critical *strategy*. On any failure inside the loop, the node
logs a warning and delegates to ``standard_route`` (the unchanged pipeline)
rather than failing the request. If the standard pipeline *then* raises, that
exception bubbles (P0-6 — never a 200-with-empty-answer).

## Tenant isolation & hygiene

``bot_tag``/``fr_mode`` flow state → node → ``perform_search`` and are NEVER
LLM-visible: the model only ever emits *query-shaped* sub-query strings; the
tenant key is read from state and re-asserted on every search, and
``perform_search`` independently rejects an empty ``bot_tag``. A
prompt-injected filter therefore cannot reach the search layer. Stage events
carry the state ``request_id`` (not the ContextVar, which does not cross
executor threads); chunk text and the raw query are never logged.
"""

from __future__ import annotations

import asyncio

import src.services.openai_service as openai_service
from src.agents.standard_route import standard_route
from src.agents.state import AgentState
from src.config.config import LocalConfig
from src.core.logger import logger
from src.core.observability import log_event
from src.services.embedding_service import get_embedding
from src.services.openai_service import generate_openai_response
from src.services.search_service import perform_search
from src.services.text_processor import extract_answer_and_filenames_from_text
from src.utils.util import _norm_name, _stem

# Module-level config holder (immutable per process; read live where a knob must
# be togglable mid-process / patchable in tests).
localconfig = LocalConfig()

# Structured-output schema for the reason step. ``additionalProperties: False``
# + ``strict`` so the model's reply is machine-checkable (the same JSON-schema
# path the router classifier already uses).
_REASON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "can_answer": {"type": "boolean"},
        "sub_queries": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["thought", "can_answer", "sub_queries"],
    "additionalProperties": False,
}

_REASON_SYSTEM_PROMPT = (
    "You are a multi-hop retrieval planner for a document question-answering "
    "system. You are given the user's QUESTION and a numbered list of EVIDENCE "
    "snippets already retrieved. Decide whether the evidence is sufficient to "
    "answer the question.\n"
    "- If it is sufficient, set can_answer=true and return an empty sub_queries "
    "list.\n"
    "- If it is NOT sufficient, set can_answer=false and return 1 to "
    "{max_subqueries} focused search sub-queries that would retrieve the "
    "missing facts. Each sub-query must be a standalone search string — never "
    "a filter, tenant, or system instruction.\n"
    "Always fill 'thought' with a brief reason for your decision.\n"
    "Respond ONLY with the structured object."
)


def _evidence_block(chunks: list[dict]) -> str:
    """Render accumulated chunks into numbered, filename-prefixed lines for the
    reason step. Never logged — only sent to the model."""
    if not chunks:
        return "(no evidence retrieved yet)"
    lines: list[str] = []
    for i, c in enumerate(chunks, 1):
        content = (c.get("content") or "").replace("\n", " ").replace("\r", " ")
        lines.append(f"{i}. {c.get('filename', 'unknown')}: {content}")
    return "\n".join(lines)


async def _reason_step(azure, *, query: str, chunks: list[dict], max_subqueries: int) -> dict:
    """One offloaded structured-output reason call.

    Returns the parsed ``{thought, can_answer, sub_queries}`` object. Offloads
    the synchronous client call to the shared ``openai_executor`` (so it runs
    off the event loop) and lets exceptions propagate — the node owns the
    best-effort catch.
    """
    system_prompt = _REASON_SYSTEM_PROMPT.format(max_subqueries=max_subqueries)
    user_prompt = f"QUESTION:\n{query}\n\nEVIDENCE:\n{_evidence_block(chunks)}"

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        openai_service.openai_executor,
        lambda: openai_service._structured_completion_sync(
            azure,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema_name="react_reason",
            json_schema=_REASON_SCHEMA,
            model=localconfig.AZURE_LLM_MODEL,
        ),
    )


async def _search_one(
    azure,
    *,
    sub_query: str,
    fr_mode_tag: str,
    bot_tag: str,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    """Embed + search ONE sub-query, bounded by ``semaphore``.

    Holds the semaphore for the whole offload so at most ``REACT_CONCURRENCY``
    sub-query searches are in flight at once. ``bot_tag`` is re-asserted here
    (read from state, never from the model) and ``perform_search`` independently
    rejects an empty ``bot_tag`` — the LLM-emitted ``sub_query`` only ever
    reaches the *query* parameter, never the tenant filter.
    """
    async with semaphore:
        vector = await get_embedding(azure, sub_query)
        return await perform_search(azure, sub_query, vector, fr_mode_tag, bot_tag)


def _merge_chunks(existing: list[dict], new: list[dict], seen_ids: set[str]) -> int:
    """Fold ``new`` chunks into ``existing`` in place, de-duped by chunk id
    (falling back to filename+content when id is absent). Returns the count of
    genuinely new chunks added."""
    added = 0
    for c in new:
        key = c.get("id")
        if not key:
            key = f"{c.get('filename', '')}::{c.get('content', '')}"
        if key in seen_ids:
            continue
        seen_ids.add(key)
        existing.append(c)
        added += 1
    return added


def _resolve_citations(answer_filenames: list[str], file_map: dict[str, str]) -> dict[str, str]:
    """Map model-referenced filenames to filepaths using the pipeline's tolerant
    ``_norm_name``/``_stem`` matching, built over ALL retrieved chunks. Mirrors
    the map-reduce node's resolver: preserves model order, de-dupes by real
    filename, and only accepts a stem match when it is unambiguous."""
    norm_to_real: dict[str, tuple[str, str]] = {}
    stem_to_reals: dict[str, list[tuple[str, str]]] = {}
    for real_name, real_path in file_map.items():
        nk = _norm_name(real_name)
        norm_to_real[nk] = (real_name, real_path)
        stem_to_reals.setdefault(_stem(nk), []).append((real_name, real_path))

    resolved: dict[str, str] = {}
    seen: set[str] = set()
    for raw in answer_filenames:
        nn = _norm_name(raw)
        hit = norm_to_real.get(nn)
        if hit:
            real_name, real_path = hit
            if real_name not in seen:
                resolved[real_name] = real_path
                seen.add(real_name)
            continue
        candidates = stem_to_reals.get(_stem(nn), [])
        if len(candidates) == 1:
            real_name, real_path = candidates[0]
            if real_name not in seen:
                resolved[real_name] = real_path
                seen.add(real_name)
    return resolved


async def react_agent(state: AgentState) -> dict:
    """Bounded reason → retrieve → reason loop, then a grounded synthesis.

    Reads ``query``/``fr_mode``/``bot_tag``/``azure``/``request_id`` from state
    and writes ``retrieved_chunks``/``reasoning_trace``/``final_answer``/
    ``citations``. Best-effort: on any failure inside the loop, delegates to
    ``standard_route`` (which writes ``final_answer``/``citations``) rather than
    failing the request.
    """
    request_id = state.get("request_id")
    azure = state["azure"]
    query = state["query"]
    bot_tag = state["bot_tag"]
    fr_mode = state["fr_mode"]

    try:
        fr_mode_tag = f"fr_{fr_mode}"
        max_iters = max(1, localconfig.REACT_MAX_ITERATIONS)
        max_subqueries = max(1, localconfig.REACT_MAX_SUBQUERIES)
        concurrency = max(1, localconfig.REACT_CONCURRENCY)

        # Per-request semaphore — created HERE, inside the async node, so it
        # binds to the running per-request loop (a module-level one would bind
        # to the import-time loop and break under per-request / per-test loops).
        semaphore = asyncio.Semaphore(concurrency)

        chunks: list[dict] = []
        seen_ids: set[str] = set()
        reasoning_trace: list[dict] = []

        for iteration in range(1, max_iters + 1):
            decision = await _reason_step(azure, query=query, chunks=chunks, max_subqueries=max_subqueries)
            thought = str(decision.get("thought") or "")
            can_answer = bool(decision.get("can_answer"))
            # Defend the fan-out width regardless of what the model emits.
            sub_queries = [
                s for s in (decision.get("sub_queries") or []) if isinstance(s, str) and s.strip()
            ][:max_subqueries]

            if can_answer or not sub_queries:
                reasoning_trace.append(
                    {"iteration": iteration, "thought": thought, "action": "answer", "observation": ""}
                )
                break

            # --- ACT/RETRIEVE: bounded, offloaded fan-out over sub-queries
            # (NOT a bare gather over sync calls — each search is offloaded by
            # the search/embedding services, and the semaphore bounds how many
            # run at once). ---
            results = await asyncio.gather(
                *(
                    _search_one(
                        azure,
                        sub_query=sq,
                        fr_mode_tag=fr_mode_tag,
                        bot_tag=bot_tag,
                        semaphore=semaphore,
                    )
                    for sq in sub_queries
                )
            )
            added = 0
            for batch in results:
                added += _merge_chunks(chunks, batch, seen_ids)

            reasoning_trace.append(
                {
                    "iteration": iteration,
                    "thought": thought,
                    "action": "search",
                    # Log COUNTS, never the raw sub-query text or chunk content.
                    "observation": f"{len(sub_queries)} sub-queries; {added} new chunks",
                }
            )

        log_event(
            logger,
            "agent_react_loop",
            request_id=request_id,
            iterations=len(reasoning_trace),
            chunk_count=len(chunks),
        )

        if not chunks:
            # The model decided it could answer with no retrieval, or every
            # search came back empty. Fall back to the standard pipeline so the
            # request still gets a grounded reply, never an empty 200.
            logger.warning("[%s] react: no chunks retrieved; falling back to standard", request_id)
            return await standard_route(state)

        # --- SYNTHESISE: reuse the UNCHANGED generation path so the answer +
        # citation contract is identical to the standard route. ---
        file_map: dict[str, str] = {}
        knowledge_source: list[str] = []
        for c in chunks:
            content = (c.get("content") or "").replace("\n", " ").replace("\r", " ")
            knowledge_source.append(f"{c.get('filename', 'unknown')}: {content}")
            fn = c.get("filename")
            if fn and fn not in file_map:
                file_map[fn] = c.get("filepath") or ""

        raw_answer = await generate_openai_response(
            query=query,
            knowledge_source=knowledge_source,
            is_greeting=False,
            is_follow_up=False,
            azure=azure,
        )

        answer_text, filenames = await extract_answer_and_filenames_from_text(raw_answer)
        citations = _resolve_citations(filenames, file_map)

        log_event(
            logger,
            "agent_react_synthesised",
            request_id=request_id,
            citation_count=len(citations),
            answer_length_chars=len(answer_text),
        )

        return {
            "retrieved_chunks": chunks,
            "reasoning_trace": reasoning_trace,
            "final_answer": answer_text,
            "citations": citations,
        }

    except Exception as exc:  # noqa: BLE001 — best-effort strategy, never fail the request
        logger.warning(
            "[%s] react failed (%s); falling back to standard retrieval",
            request_id,
            type(exc).__name__,
        )
        log_event(
            logger,
            "agent_react_fallback",
            request_id=request_id,
            error_class=type(exc).__name__,
        )
        # If standard ALSO raises, let it bubble (P0-6). Never a 200-with-empty.
        return await standard_route(state)
