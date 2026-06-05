"""Map-reduce summariser node (P3-2). Flag: ``QNA_AGENT_MAP_REDUCE``.

For broad summarisation/aggregation questions ("summarise every policy"), a
single top-K retrieval is the wrong shape: the answer needs the *whole* corpus
slice. This node retrieves **all** chunks for ``(bot_tag, fr_mode)`` (via the
new ``perform_search(fetch_all=True)`` — today's ``TOP_K=20`` silently caps it),
batches them, **maps** an extract LLM call over each batch, then **reduces** the
partial extracts into one grounded answer.

## Concurrency — code-grounded, per the ADR (load-bearing)

The Azure OpenAI client is **synchronous** (``azure.openai_client``), invoked
behind a small ThreadPoolExecutor. A bare ``asyncio.gather`` over direct sync
calls parallelises **nothing** (the calls block the event-loop thread one after
another) and an unbounded executor would melt the Azure quota. So each map call
is offloaded with ``loop.run_in_executor`` onto a **dedicated, module-level,
config-sized** executor, the fan-out is bounded by a **per-request**
``asyncio.Semaphore(MAP_REDUCE_CONCURRENCY)``, and each call has bounded
exponential-backoff retry for transient errors. ``gather`` is then correct — it
gathers over the *executor futures*, not over bare sync calls.

The semaphore is created **inside the async node** (not at module level): a
module-level semaphore binds to the import-time event loop and breaks under the
per-request / per-test loops. The executor is loop-agnostic and stays module
level.

## Best-effort fallback (per the ADR)

Map/reduce is a non-critical *strategy*. On any failure inside map+reduce, the
node logs a warning and delegates to ``standard_route`` (the unchanged
pipeline) rather than failing the request. If the standard pipeline *then*
raises, that exception bubbles (P0-6 — never a 200-with-empty-answer).

## Isolation & hygiene

``bot_tag``/``fr_mode`` flow state → node → ``perform_search`` and are NEVER
LLM-visible. Stage events (``chunk_count``, ``batch_count``) carry the
state ``request_id`` (not the ContextVar, which does not cross executor
threads); chunk text is never logged.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

import src.services.openai_service as openai_service
from src.agents.standard_route import standard_route
from src.agents.state import AgentState
from src.config.config import LocalConfig
from src.core.logger import logger
from src.core.observability import log_event
from src.services.embedding_service import get_embedding
from src.services.search_service import perform_search
from src.services.text_processor import extract_answer_and_filenames_from_text
from src.utils.util import _norm_name, _stem

# Module-level config holder (immutable per process; read live where a knob
# must be togglable mid-process, e.g. the semaphore size in tests).
localconfig = LocalConfig()

# Dedicated, config-sized executor for the map fan-out. Module-level is correct
# (a ThreadPoolExecutor is NOT bound to an event loop). Sized off the same
# concurrency knob so executor capacity never silently throttles below the
# semaphore bound. Kept separate from the openai/search/embedding pools so the
# fan-out does not contend with the standard request path (per the ADR).
map_reduce_executor = ThreadPoolExecutor(
    max_workers=max(1, localconfig.MAP_REDUCE_CONCURRENCY),
    thread_name_prefix="mapreduce",
)

# Per-batch retry policy for transient map failures (before giving up to the
# standard-retrieval fallback).
_MAP_MAX_ATTEMPTS = 3
_MAP_BACKOFF_BASE_S = 0.2


def _batch(items: list[dict], size: int) -> list[list[dict]]:
    """Split ``items`` into consecutive batches of at most ``size``."""
    size = max(1, size)
    return [items[i : i + size] for i in range(0, len(items), size)]


def _sources_block(chunks: list[dict]) -> str:
    """Render a batch of chunks into filename-prefixed source lines.

    Mirrors the pipeline's ``f"{filename}: {content}"`` framing (newlines
    stripped). Never returned to logs — only sent to the model.
    """
    lines: list[str] = []
    for c in chunks:
        content = (c.get("content") or "").replace("\n", " ").replace("\r", " ")
        lines.append(f"{c.get('filename', 'unknown')}: {content}")
    return "\n".join(lines)


async def _map_one_batch(
    azure,
    *,
    query: str,
    chunks: list[dict],
    model: str | None,
    semaphore: asyncio.Semaphore,
) -> str:
    """Run one map (extract) LLM call for one batch, bounded + with backoff.

    Holds ``semaphore`` for the whole offload so at most ``MAP_REDUCE_CONCURRENCY``
    map calls are in flight at once, and offloads the **synchronous** client
    call to the dedicated executor so it actually runs off the event loop.
    Retries transient errors with exponential backoff; re-raises on final
    failure so the node can fall back to standard retrieval.
    """
    sources = _sources_block(chunks)
    loop = asyncio.get_running_loop()

    async with semaphore:
        last_exc: Exception | None = None
        for attempt in range(1, _MAP_MAX_ATTEMPTS + 1):
            try:
                return await loop.run_in_executor(
                    map_reduce_executor,
                    lambda: openai_service.map_extract_sync(azure, query=query, sources=sources, model=model),
                )
            except Exception as exc:  # noqa: BLE001 — bounded retry then re-raise
                last_exc = exc
                if attempt < _MAP_MAX_ATTEMPTS:
                    await asyncio.sleep(_MAP_BACKOFF_BASE_S * (2 ** (attempt - 1)))
        # Exhausted retries — re-raise so the node's best-effort fallback fires.
        raise last_exc  # type: ignore[misc]


def _resolve_citations(answer_filenames: list[str], file_map: dict[str, str]) -> dict[str, str]:
    """Map model-referenced filenames to filepaths, reusing the pipeline's
    tolerant ``_norm_name``/``_stem`` matching (qna_pipeline citation logic).

    Built over a ``file_map`` of ALL retrieved chunks so any cited document
    resolves. Preserves model order, de-dupes by real filename, and only
    accepts a stem match when it is unambiguous.
    """
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


async def map_reduce(state: AgentState) -> dict:
    """Retrieve-all → batched map (extract) → reduce (synthesise) answer node.

    Reads ``query``/``fr_mode``/``bot_tag``/``azure``/``request_id`` from state
    and writes ``retrieved_chunks``/``partial_answers``/``final_answer``/
    ``citations``. Best-effort: on any map/reduce failure, delegates to
    ``standard_route`` (which writes ``final_answer``/``citations``) rather than
    failing the request.
    """
    request_id = state.get("request_id")
    azure = state["azure"]
    query = state["query"]
    bot_tag = state["bot_tag"]
    fr_mode = state["fr_mode"]

    try:
        # Replicate the pipeline's retrieval preamble: tag form + embedding.
        fr_mode_tag = f"fr_{fr_mode}"
        vector = await get_embedding(azure, query)

        # Retrieve ALL chunks for this tenant slice (lifts the TOP_K cap).
        chunks = await perform_search(azure, query, vector, fr_mode_tag, bot_tag, fetch_all=True)

        batches = _batch(chunks, localconfig.MAP_REDUCE_BATCH_SIZE)
        log_event(
            logger,
            "agent_map_reduce_retrieved",
            request_id=request_id,
            chunk_count=len(chunks),  # proves the TOP_K cap was lifted
            batch_count=len(batches),
        )

        if not chunks:
            # Nothing to map over — fall back to the standard pipeline so the
            # request still gets a (no-context) grounded reply, never an empty
            # 200. (standard_route writes final_answer/citations.)
            logger.warning("[%s] map_reduce: no chunks retrieved; falling back to standard", request_id)
            return await standard_route(state)

        # Build a filename→filepath map over ALL chunks for citation resolution.
        file_map: dict[str, str] = {}
        for c in chunks:
            fn = c.get("filename")
            if fn and fn not in file_map:
                file_map[fn] = c.get("filepath") or ""

        # --- MAP: bounded, executor-offloaded fan-out (NOT a bare gather over
        # sync calls). Semaphore is created HERE, inside the async node, so it
        # binds to the running per-request loop. ---
        # Read concurrency live so the bound is togglable (and patchable in
        # tests) without a redeploy.
        concurrency = max(1, localconfig.MAP_REDUCE_CONCURRENCY)
        semaphore = asyncio.Semaphore(concurrency)
        reduce_model = localconfig.AZURE_OPENAI_REDUCE_MODEL

        partials = await asyncio.gather(
            *(
                _map_one_batch(
                    azure,
                    query=query,
                    chunks=batch,
                    model=localconfig.AZURE_LLM_MODEL,
                    semaphore=semaphore,
                )
                for batch in batches
            )
        )

        # Drop empty / explicitly-irrelevant extracts before the reduce step.
        extracts = [p for p in partials if p and p.strip() and p.strip() != "NO_RELEVANT_INFORMATION"]

        log_event(
            logger,
            "agent_map_reduce_mapped",
            request_id=request_id,
            batch_count=len(batches),
            nonempty_extract_count=len(extracts),
        )

        # --- REDUCE: combine extracts into the final grounded answer. ---
        loop = asyncio.get_running_loop()
        reduce_input = "\n\n".join(extracts) if extracts else "NO_RELEVANT_INFORMATION"
        raw_answer = await loop.run_in_executor(
            map_reduce_executor,
            lambda: openai_service.reduce_combine_sync(
                azure, query=query, extracts=reduce_input, model=reduce_model
            ),
        )

        answer_text, filenames = await extract_answer_and_filenames_from_text(raw_answer)
        citations = _resolve_citations(filenames, file_map)

        log_event(
            logger,
            "agent_map_reduce_reduced",
            request_id=request_id,
            citation_count=len(citations),
            answer_length_chars=len(answer_text),
        )

        return {
            "retrieved_chunks": chunks,
            "partial_answers": extracts,
            "final_answer": answer_text,
            "citations": citations,
        }

    except Exception as exc:  # noqa: BLE001 — best-effort strategy, never fail the request
        logger.warning(
            "[%s] map_reduce failed (%s); falling back to standard retrieval",
            request_id,
            type(exc).__name__,
        )
        log_event(
            logger,
            "agent_map_reduce_fallback",
            request_id=request_id,
            error_class=type(exc).__name__,
        )
        # If standard ALSO raises, let it bubble (P0-6). Never a 200-with-empty.
        return await standard_route(state)
