"""Self-critique / verifier convergence node (P3-4). Flag: ``QNA_AGENT_VERIFY``.

The ADR's converged topology routes every answer strategy through a single
verifier before ``END`` so self-critique is authored once. This node grades the
draft ``final_answer`` for **groundedness + citation support** against the
``retrieved_chunks`` the answer was built from, and — per the task's "trigger
one bounded refine/retry" requirement (the ADR's Option B, which the task
overrides Option A with) — if the answer fails the bar it runs **exactly one**
refine pass and re-grades. The refine is NON-DESTRUCTIVE: the original answer is
kept unless the refined answer clears the acceptance bar (it is not a score
comparison — a refine that scores higher but still fails the bar is discarded).

The refine re-runs the grounded generation (``generate_openai_response``) over
the same ``retrieved_chunks`` as a single bounded second attempt, so the refined
answer obeys the identical prompt + ``**Sources:**`` citation contract as the
original. It is a deliberate re-generation (one retry), not a different
"stricter" prompt — the bar is re-applied by re-grading, and if the retry still
fails the original answer is kept unchanged (non-destructive).

**Known limitation (documented):** the refine is a re-generation, not a
claim-targeted repair. The grader's ``unsupported_claims`` are surfaced to the
caller but are NOT fed back into the refine prompt, so the only thing that can
make the refined answer differ from the original is sampling variance. The
original synthesis runs at the API-default temperature; the refine therefore
pins an explicit non-zero ``_REFINE_TEMPERATURE`` so a genuinely *different*
sample is at least possible (without it the re-roll risks an effectively
identical answer — wasted cost/latency with no remediation mechanism). A
claim-targeted critique prompt that feeds ``unsupported_claims`` back into the
generation is a deliberately deferred follow-up, out of scope here.

The refine is gated to the ``react`` route only: it feeds ALL retrieved chunks
into one generation, which on the ``map_reduce`` route would be the wide-context
corpus slice map_reduce exists to avoid. On ``map_reduce`` a failing grade keeps
the original answer and flags it unverified rather than attempting that
over-context single-shot synthesis.

## Default-OFF inertness (the merge gate)

With ``QNA_AGENT_VERIFY`` unset/empty/falsy this node is a **pass-through
no-op**: it writes no keys and the graph output is byte-for-byte identical to
the flag-off behaviour. It is also a no-op when there is no evidence to judge
against — the **standard route** does not expose ``retrieved_chunks`` in v1
(``qna_pipeline`` returns only ``{answer, citation}``), so a verifier with no
chunks cannot grade and adds nothing. Only the ``map_reduce`` / ``react`` routes
(which write ``retrieved_chunks``) actually exercise the grader.

## Best-effort (per the ADR)

Verification is a non-critical step. Any grader/refine exception is caught,
logged, and the **original** answer is returned unchanged — a verifier failure
must never fail the request and never blank an answer.

## Hygiene

Reads ``request_id`` from state (not the ContextVar, which does not cross
executor threads). The draft answer and chunk text are sent to the grader model
(they must be, to grade) but are NEVER logged: events carry only the verdict,
the score, and counts.
"""

from __future__ import annotations

import asyncio

import src.services.openai_service as openai_service
from src.agents.state import AgentState
from src.config.config import LocalConfig, is_verify_enabled
from src.core.logger import logger
from src.core.observability import log_event
from src.services.openai_service import generate_openai_response
from src.services.text_processor import extract_answer_and_filenames_from_text

localconfig = LocalConfig()

# Sampling temperature for the single bounded refine pass. The refine re-grounds
# over the SAME chunks with the SAME prompt — it has no signal directing it at
# the specific unsupported claims (a known limitation; see the module docstring),
# so its only lever to differ from the original draft is sampling variance. The
# original synthesis path runs at the API-default temperature; pinning a distinct
# non-zero value here makes a *different* sample at least possible (a true
# re-roll) rather than re-requesting an effectively identical generation.
_REFINE_TEMPERATURE = 0.7

# Structured-output schema for the groundedness grade. ``strict`` +
# ``additionalProperties: False`` so the verdict is machine-checkable (the same
# JSON-schema path the router classifier reuses).
_GRADE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "supported": {"type": "boolean"},
        "score": {"type": "integer"},
        "unsupported_claims": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["supported", "score", "unsupported_claims"],
    "additionalProperties": False,
}

_GRADE_SYSTEM_PROMPT = (
    "You are a strict groundedness grader for a document question-answering "
    "system. You are given a user QUESTION, a candidate ANSWER, and the SOURCES "
    "(document excerpts) the answer was supposed to be grounded in. Judge ONLY "
    "whether every factual claim in the ANSWER is directly supported by the "
    "SOURCES — do NOT judge whether the answer is well written.\n"
    "- score: an integer 0-100 — the percentage of the answer's factual claims "
    "that are directly supported by the SOURCES (100 = fully grounded).\n"
    "- supported: true only if the answer is fully grounded with no fabricated "
    "or unsupported claims.\n"
    "- unsupported_claims: the specific claims that are NOT supported by the "
    "SOURCES (empty if fully grounded).\n"
    "Respond ONLY with the structured object."
)


def _sources_block(chunks: list[dict]) -> str:
    """Render retrieved chunks into filename-prefixed source lines for grading.
    Never logged — only sent to the grader model."""
    lines: list[str] = []
    for c in chunks:
        content = (c.get("content") or "").replace("\n", " ").replace("\r", " ")
        lines.append(f"{c.get('filename', 'unknown')}: {content}")
    return "\n".join(lines)


def _knowledge_source(chunks: list[dict]) -> list[str]:
    """Render chunks into the ``generate_openai_response`` knowledge-source
    shape (filename-prefixed lines), reused for the refine pass so the refined
    answer obeys the same prompt + citation contract as the original."""
    lines: list[str] = []
    for c in chunks:
        content = (c.get("content") or "").replace("\n", " ").replace("\r", " ")
        lines.append(f"{c.get('filename', 'unknown')}: {content}")
    return lines


async def _grade(azure, *, query: str, answer: str, chunks: list[dict]) -> dict:
    """One offloaded structured-output grade call.

    Offloads the synchronous client call to the shared ``openai_executor`` (so
    it runs off the event loop) and lets exceptions propagate — the node owns
    the best-effort catch.
    """
    user_prompt = f"QUESTION:\n{query}\n\nANSWER:\n{answer}\n\nSOURCES:\n{_sources_block(chunks)}"
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        openai_service.openai_executor,
        lambda: openai_service._structured_completion_sync(
            azure,
            system_prompt=_GRADE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            schema_name="groundedness_grade",
            json_schema=_GRADE_SCHEMA,
            model=localconfig.AZURE_OPENAI_VERIFIER_MODEL,
        ),
    )


def _passes(grade: dict) -> bool:
    """Whether a grade clears the acceptance bar: explicitly supported AND at or
    above ``VERIFY_MIN_SCORE``. Defensive about score type."""
    if not bool(grade.get("supported")):
        return False
    try:
        score = int(grade.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    return score >= localconfig.VERIFY_MIN_SCORE


async def verifier(state: AgentState) -> dict:
    """Grade groundedness; on failure run ONE bounded refine; keep the original
    unless the refined answer clears the acceptance bar.

    Default-OFF no-op (writes ``{}``) when ``QNA_AGENT_VERIFY`` is off OR there
    are no ``retrieved_chunks`` to judge against. Otherwise writes ``verified``,
    ``unsupported_claims``, and (if a refine improved the answer)
    ``final_answer``/``citations``.
    """
    request_id = state.get("request_id")

    # --- Default-OFF inertness: byte-identical pass-through when the sub-flag
    # is off. This is the merge gate; keep it the very first check. ---
    if not is_verify_enabled():
        return {}

    chunks = state.get("retrieved_chunks") or []
    answer = state.get("final_answer") or ""

    # No evidence (standard route in v1) or no answer to grade → nothing to do.
    if not chunks or not answer.strip():
        log_event(
            logger,
            "agent_verifier_skipped",
            request_id=request_id,
            route=state.get("route"),
            reason="no_chunks" if not chunks else "no_answer",
        )
        return {}

    azure = state["azure"]
    query = state["query"]

    try:
        grade = await _grade(azure, query=query, answer=answer, chunks=chunks)
        unsupported = [c for c in (grade.get("unsupported_claims") or []) if isinstance(c, str)]

        log_event(
            logger,
            "agent_verifier_graded",
            request_id=request_id,
            route=state.get("route"),
            supported=bool(grade.get("supported")),
            score=grade.get("score"),
            unsupported_count=len(unsupported),
        )

        if _passes(grade):
            return {"verified": True, "unsupported_claims": []}

        # --- Refine-scope guard. The refine re-grounds over ALL retrieved
        # chunks in ONE generation. On the map_reduce route ``retrieved_chunks``
        # is the full fetched corpus slice (bounded only by MAP_REDUCE_MAX_CHUNKS,
        # default 1000) — the exact wide-context shape map_reduce exists to
        # avoid. Stuffing that into a single synthesis call risks cost/latency/
        # quota blowups and silent SDK truncation (a degraded refine). So gate
        # the single-shot refine to the ``react`` route (whose retrieval is
        # TOP_K-bounded); on map_reduce keep the original answer and flag it
        # unverified rather than attempt an over-context refine. ---
        if state.get("route") != "react":
            log_event(
                logger,
                "agent_verifier_refine_skipped",
                request_id=request_id,
                route=state.get("route"),
                reason="route_not_refinable",
                chunk_count=len(chunks),
            )
            return {"verified": False, "unsupported_claims": unsupported}

        # --- One bounded refine pass (Option B). Re-ground the answer with the
        # SAME generation path/contract, then re-grade. Keep the original unless
        # the refine clears the bar. The best-effort catch below keeps the
        # original answer if the refine raises, so it degrades safely rather
        # than failing the request.
        #
        # LIMITATION (documented, not silently accepted): this is a deliberate
        # re-generation, NOT a stricter/critique-driven prompt — the grader's
        # ``unsupported_claims`` are surfaced to the caller but are not fed back
        # into the prompt, so the refine has no signal targeting the specific
        # groundedness failure. Its only lever is sampling variance, so we set an
        # explicit non-zero ``_REFINE_TEMPERATURE`` (vs. the original draft's
        # API-default sample) so a genuinely different sample is at least
        # possible; without it the re-roll could draw an effectively identical
        # answer and waste the pass. A claim-targeted critique prompt is a
        # deliberately deferred follow-up. ---
        refined_raw = await generate_openai_response(
            query=query,
            knowledge_source=_knowledge_source(chunks),
            is_greeting=False,
            is_follow_up=False,
            azure=azure,
            temperature=_REFINE_TEMPERATURE,
        )
        refined_answer, refined_filenames = await extract_answer_and_filenames_from_text(refined_raw)

        if not refined_answer.strip():
            # Refine produced nothing usable — keep the original, flag unverified.
            log_event(
                logger,
                "agent_verifier_refine_empty",
                request_id=request_id,
            )
            return {"verified": False, "unsupported_claims": unsupported}

        refined_grade = await _grade(azure, query=query, answer=refined_answer, chunks=chunks)
        refined_unsupported = [
            c for c in (refined_grade.get("unsupported_claims") or []) if isinstance(c, str)
        ]
        refined_passes = _passes(refined_grade)

        log_event(
            logger,
            "agent_verifier_refined",
            request_id=request_id,
            refined_supported=bool(refined_grade.get("supported")),
            refined_score=refined_grade.get("score"),
            accepted=refined_passes,
        )

        if refined_passes:
            # The refine cleared the bar — replace the answer + recompute its
            # citations against the same file_map the answer route built.
            file_map: dict[str, str] = {}
            for c in chunks:
                fn = c.get("filename")
                if fn and fn not in file_map:
                    file_map[fn] = c.get("filepath") or ""
            citations = _resolve_citations(refined_filenames, file_map)
            return {
                "verified": True,
                "unsupported_claims": [],
                "final_answer": refined_answer,
                "citations": citations,
            }

        # Neither cleared the bar — keep the ORIGINAL answer (non-destructive),
        # surface the verdict so the client/operator can see it failed.
        return {"verified": False, "unsupported_claims": refined_unsupported or unsupported}

    except Exception as exc:  # noqa: BLE001 — best-effort: never fail the request
        logger.warning(
            "[%s] verifier failed (%s); leaving answer unchanged",
            request_id,
            type(exc).__name__,
        )
        log_event(
            logger,
            "agent_verifier_error",
            request_id=request_id,
            error_class=type(exc).__name__,
        )
        # Leave the answer untouched; do not set ``verified`` (unknown).
        return {}


def _resolve_citations(answer_filenames: list[str], file_map: dict[str, str]) -> dict[str, str]:
    """Tolerant filename→filepath resolution for a refined answer, mirroring the
    map-reduce / react resolver (``_norm_name``/``_stem``). Imported lazily to
    keep the verifier's hot no-op path free of the util import."""
    from src.utils.util import _norm_name, _stem

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
