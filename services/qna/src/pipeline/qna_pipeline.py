"""
# This module orchestrates the QnA flow:

- (Optionally) incorporates conversation history to rephrase the latest query for better retrieval.
- Generates an embedding and performs a search against the indexed corpus.
- Builds a grounded prompt from top matches (and an optional prior bot snippet as data-only).
- Calls the model to produce an answer.
- Parses the answer to extract citations and returns the final payload.

Conversation history and bot_tag are passed explicitly as parameters to
generate_answer() — there is no module-level mutable global. This ensures
concurrent requests cannot contaminate each other's conversation context.
Expected history shape: List[{"user_query": str, "bot_response": Optional[str]}]
"""

import os
import time
import traceback
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from src.cache.answer_cache import get_cache, make_cache_key
from src.config.config import LocalConfig, is_cache_enabled
from src.core.logger import logger
from src.core.observability import log_event
from src.core.responses import CitationMap, QnASuccessResponse
from src.services.embedding_service import get_embedding
from src.services.openai_service import (
    generate_openai_response,
    rephrase_queries,
    stream_openai_response,
)
from src.services.search_service import perform_search
from src.services.text_processor import extract_answer_and_filenames_from_text
from src.utils.util import _latest_three_and_reply, _norm_name, _stem

# ---------------------------------------------------------------------------
# Executors / globals
# ---------------------------------------------------------------------------
process_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="process")

# Module-level config holder so we can report the configured model / top_k in
# structured stage events without re-reading env on every call.
_localconfig = LocalConfig()


async def generate_answer(
    query: str,
    fr_mode: str,
    bot_tag: str,
    history: list[dict[str, str | None]],
    azure,
    request_id: str | None = None,
) -> dict[str, Any]:
    """
    Generate a grounded answer for the user's query.

    Args:
        query: Latest user utterance (may be rephrased for retrieval).
        fr_mode: 'read' or 'layout' (affects retrieval strategy).
        bot_tag: Bot/tenant identifier forwarded to the search layer to enforce
            tenant isolation. Must be non-empty.
        history: Ordered conversation turns (oldest → newest), each with
            {"user_query": str, "bot_response": Optional[str]}. May be empty
            for a first-turn request; None is treated as [].
        azure: Azure client holder (OpenAI, embeddings, search).
        request_id: Optional correlation ID threaded from the HTTP layer
            (`request.state.request_id`). When omitted, a local `gen_<ts>` id
            is minted to preserve backward-compatible behavior for direct
            callers/tests. Threading the middleware request_id lets pipeline
            stage events share the same correlation ID as the request
            lifecycle events.

    Returns:
        A dict with:
            - "answer": str
            - "citation": {filename: filepath, ...}
            - ("request_id"/"error" only in exceptional cases)

    Notes:
        - Rephrasal is best-effort; the pipeline proceeds if it fails.
        - History is never stored in module-level state; each call is isolated.
    """
    # Reuse the HTTP-layer correlation ID when provided so pipeline stage
    # events share the request_started/request_completed correlation ID.
    # Fall back to the legacy locally-minted id for direct callers/tests.
    request_id = request_id or f"gen_{int(time.time() * 1000)}"
    if not bot_tag or not bot_tag.strip():
        raise ValueError("bot_tag is required for tenant isolation")
    # Metadata-only start event — NEVER log the raw query (PII/confidential).
    # fr_mode is a bounded enum ("read"/"layout"), safe to log.
    log_event(
        logger,
        "answer_generation_started",
        request_id=request_id,
        fr_mode=fr_mode,
        query_length=len(query or ""),
    )
    start_time = time.time()

    # Defensive normalisation: treat None as an empty list.
    history = history or []

    try:
        if fr_mode not in ("read", "layout"):
            msg = f"Invalid fr_mode: {fr_mode}. Must be 'read' or 'layout'."
            logger.error(f"[{request_id}] {msg}")
            raise ValueError(msg)

        # ---- Optional within-tenant answer cache (default OFF) ----
        # Lookup happens here — after fr_mode is validated and the tenant-scoped
        # key fields (bot_tag, fr_mode, query) are known — so a hit skips the
        # entire rephrase → embed → search → LLM fan-out below. The key is the
        # tuple (bot_tag, fr_mode, normalized_query); a cached answer is NEVER
        # served across bot_tag boundaries (the key's first element). When
        # QNA_CACHE_ENABLED is unset/falsy this block is fully inert and the
        # path is byte-identical to the historical pipeline. History is NOT part
        # of the key (documented trade-off; see src/cache/answer_cache.py).
        _cache_enabled = is_cache_enabled()
        _cache_key = None
        if _cache_enabled:
            _cache_key = make_cache_key(bot_tag, fr_mode, query)
            cached = get_cache().get(_cache_key)
            if cached is not None:
                # Metadata-only event — NEVER log the query or answer body.
                log_event(
                    logger,
                    "cache_hit",
                    request_id=request_id,
                    bot_tag=bot_tag,
                    fr_mode=fr_mode,
                )
                # get() already returns a copy; return it directly.
                return cached

        # Pull: current user query, the two prior user queries, and
        # the latest bot response.
        latest_q, prev_q, prev_prev_q, latest_bot_reply = _latest_three_and_reply(history)

        # Metadata-only history event. NEVER log raw queries, the prior-turn
        # dict, or the latest bot reply — these routinely carry customer PII and
        # confidential document content, and interpolating them as bare strings
        # is also a log-injection vector (audit H6 + M4). Log only counts and
        # booleans; the observability policy forbids the raw values.
        log_event(
            logger,
            "history_normalized",
            request_id=request_id,
            history_turns=len(history),
            has_prev_query=bool(prev_q),
            has_prev_prev_query=bool(prev_prev_q),
            has_prev_reply=bool(latest_bot_reply),
        )

        # Use the caller's `query` as source of truth for the current turn.
        effective_query = query
        extracted_snippet: str = ""

        # Default setting
        is_greeting = False
        is_followup = False
        file_map: dict[str, str] = {}
        # P2-1 groundwork: accumulate filename -> ordered-unique page strings
        # from retrieved chunks BEFORE filename dedup. Stays empty (-> None) for
        # chunks ingested before page_number was populated, so the response is
        # byte-identical to today's {answer, citation} shape.
        file_pages: dict[str, list[str]] = {}

        # Best-effort rephrasal using full, normalized history.
        try:
            logger.info(f"[{request_id}] Rephrasing path enabled ")
            _rephrase_start = time.perf_counter()
            rq = await rephrase_queries(
                azure=azure,
                current_query=latest_q,
                prev_query=prev_q,
                prev_prev_query=prev_prev_q,
                latest_bot_reply=latest_bot_reply,
                full_history=history,
            )
            log_event(
                logger,
                "query_rephrased",
                request_id=request_id,
                history_turns_used=len(history),
                latency_ms=round((time.perf_counter() - _rephrase_start) * 1000, 2),
            )
            if isinstance(rq, dict):
                effective_query = rq.get("rephrased_query")
                if effective_query and effective_query != query:
                    logger.info(f"[{request_id}] Query rephrased for retrieval")

                # Prefer a snippet extracted by the rephraser; fall back to
                # the latest bot reply.
                snippet = rq.get("extracted_snippet") or ""
                if not snippet and latest_bot_reply:
                    snippet = latest_bot_reply
                if snippet:
                    extracted_snippet = snippet
                    logger.info(
                        f"[{request_id}] Prior bot snippet detected (will be added as data-only line)"
                    )
                is_greeting = rq.get("is_greeting")
                if is_greeting:
                    logger.info("[%s] Is greeting: %s", request_id, is_greeting)
                is_followup = rq.get("is_followup")
                if is_followup:
                    logger.info("[%s] Is Follow-up : %s", request_id, is_followup)

        except Exception as re_err:
            # Rephrasal is best-effort; retrieval proceeds regardless.
            logger.warning(f"[{request_id}] Rephrase path skipped due to error: {re_err}")

        knowledge_source: list[str] = []

        # If it's a greeting rather than a question we don't send it for
        # knowledge retrieval!
        if not is_greeting:
            # Embed and search
            fr_mode_tag = f"fr_{fr_mode}"
            logger.info(f"[{request_id}] Step 1: Generating embedding")
            vector = await get_embedding(azure, effective_query)

            logger.info(f"[{request_id}] Step 2: Performing search")
            _retrieval_start = time.perf_counter()
            results = await perform_search(azure, effective_query, vector, fr_mode_tag, bot_tag)
            _retrieval_latency_ms = round((time.perf_counter() - _retrieval_start) * 1000, 2)

            # Build knowledge base lines and a filename→filepath map
            # for citation resolution
            logger.info(f"[{request_id}] Step 3: Processing search results")

            # Collect source provenance for the structured retrieval event.
            # De-duplicate (many chunks share one document) while preserving
            # first-seen order. NEVER collect chunk text here — only IDs/paths.
            _doc_ids: list[str] = []
            _source_paths: list[str] = []
            for r in results:
                # r: {"filename", "filepath", "content", "document_id", "source_path", ...}
                content = (r["content"] or "").replace("\n", "").replace("\r", "")
                knowledge_source.append(f"{r['filename']}: {content}")
                file_map[r["filename"]] = r["filepath"]

                # Accumulate page provenance (ordered-unique, non-empty) per
                # filename. `.get` tolerates old chunks lacking the field; the
                # value is normalized to a trimmed string and ints are coerced.
                page_raw = r.get("page_number")
                if page_raw is not None:
                    page_str = str(page_raw).strip()
                    if page_str:
                        pages = file_pages.setdefault(r["filename"], [])
                        if page_str not in pages:
                            pages.append(page_str)

                doc_id = r.get("document_id")
                if doc_id and doc_id not in _doc_ids:
                    _doc_ids.append(doc_id)
                # Prefer the indexed source_path; fall back to filepath.
                src_path = r.get("source_path") or r.get("filepath")
                if src_path and src_path not in _source_paths:
                    _source_paths.append(src_path)

            log_event(
                logger,
                "retrieval_completed",
                request_id=request_id,
                bot_tag=bot_tag,
                fr_tag=fr_mode_tag,
                retrieved_chunk_count=len(results),
                top_k=_localconfig.TOP_K,
                latency_ms=_retrieval_latency_ms,
                source_document_ids=_doc_ids,
                source_paths=_source_paths,
            )

            # Optionally append the latest bot reply as data-only (not cited)
            if extracted_snippet:
                try:
                    sanitized = extracted_snippet.replace("\n", " ").replace("\r", " ")
                    knowledge_source.append(
                        f"PrevAnswer.md (previous assistant reply; data-only, do not cite): \n{sanitized}"
                    )
                except Exception as snip_err:
                    logger.warning(f"[{request_id}] Skipped appending prior snippet: {snip_err}")
        else:
            # Greeting path
            logger.info(f"[{request_id}] Greeting Detected, skipping retrieval & calling model directly!")

        logger.debug(f"[{request_id}] KB lines: {len(knowledge_source)} | file_map: {len(file_map)}")

        # Model call
        logger.info(f"[{request_id}] Step 4: Generating model response")
        _answer_start = time.perf_counter()
        ans = await generate_openai_response(
            effective_query,
            knowledge_source,
            is_greeting=is_greeting,
            is_follow_up=is_followup,
            azure=azure,
        )
        _answer_latency_ms = round((time.perf_counter() - _answer_start) * 1000, 2)

        # Extract the final answer text and the filenames the model referenced
        logger.info(f"[{request_id}] Step 5: Extracting answer and sources")
        if not isinstance(ans, str):
            raise TypeError(f"Model response type must be str, got {type(ans)}")

        answer_text, filenames = await extract_answer_and_filenames_from_text(ans)

        # Map filenames to filepaths for the final citation payload
        logger.info(f"[{request_id}] Step 6: Creating citation mapping")

        # ---- Robust filename → filepath mapping (tolerant to bullets/case/spaces) ----
        # Build normalized lookups from the actual search results we have
        norm_to_real: dict[str, tuple[str, str]] = {}
        stem_to_reals: dict[str, list[tuple[str, str]]] = {}

        for real_name, real_path in file_map.items():
            nk = _norm_name(real_name)
            norm_to_real[nk] = (real_name, real_path)
            st = _stem(nk)
            stem_to_reals.setdefault(st, []).append((real_name, real_path))

        extracted_filepath: dict[str, str] = {}
        misses: list[tuple[str, str]] = []  # (original, normalized)

        # Preserve model order; de-duplicate by real_name
        seen = set()

        for raw in filenames:
            nn = _norm_name(raw)

            # 1) Exact normalized match
            hit = norm_to_real.get(nn)
            if hit:
                real_name, real_path = hit
                if real_name not in seen:
                    extracted_filepath[real_name] = real_path
                    seen.add(real_name)
                continue

            # 2) Unique stem match (only if it resolves to exactly one candidate)
            st = _stem(nn)
            candidates = stem_to_reals.get(st, [])
            if len(candidates) == 1:
                real_name, real_path = candidates[0]
                if real_name not in seen:
                    extracted_filepath[real_name] = real_path
                    seen.add(real_name)
                continue

            # 3) No safe match
            misses.append((raw, nn))

        # Helpful debugging (keeps INFO/ERROR noise low)
        if misses:
            logger.debug("[%s] Citation mapping misses (model → normalized): %s", request_id, misses)

        # P2-1 groundwork: build page_citations for ONLY the filenames that
        # survived into the citation map, reusing the ordered-unique pages
        # accumulated during retrieval. Coalesce empty -> None so the response
        # is byte-identical to {answer, citation} when no chunk carried a
        # page_number (today's state) or no cited file had page data.
        page_citations: dict[str, list[str]] | None = {
            real_name: file_pages[real_name] for real_name in extracted_filepath if file_pages.get(real_name)
        } or None

        total_time = time.time() - start_time
        logger.info(f"[{request_id}] Answer generation completed in {total_time:.4f}s")
        logger.info(
            f"[{request_id}] Answer length: {len(answer_text)} chars | citations: {len(extracted_filepath)}"
        )

        # Structured answer event — metadata only. The answer body is NEVER
        # logged here; a short preview is emitted only when QNA_DEBUG_LOG_PREVIEW
        # is explicitly enabled (off by default, capped at 200 chars).
        _preview = None
        if os.getenv("QNA_DEBUG_LOG_PREVIEW", "").lower() in ("1", "true", "yes"):
            _preview = (answer_text or "")[:200]
        log_event(
            logger,
            "answer_generated",
            request_id=request_id,
            model=_localconfig.AZURE_LLM_MODEL,
            latency_ms=_answer_latency_ms,
            citation_count=len(extracted_filepath),
            answer_length_chars=len(answer_text),
            answer_preview=_preview,
        )

        # Give the payload a typed home (the public success contract; see
        # src.core.responses). We construct the model to validate the shape,
        # then return its dict form so direct callers/tests that do
        # `result["answer"]` / `"citation" in result` keep working unchanged.
        # `exclude_none=True` drops the defensive optional fields so the dict
        # stays byte-identical to the historical `{answer, citation}` payload.
        response = QnASuccessResponse(
            answer=answer_text,
            citation=CitationMap(extracted_filepath),
            page_citations=page_citations,
        )
        result = response.model_dump(exclude_none=True)

        # Populate the cache on the SUCCESS path only (never in `finally`), so a
        # failed request is never cached. Caches the whole returned dict so
        # page_citations round-trips on a hit. set() stores its own copy.
        if _cache_enabled and _cache_key is not None:
            get_cache().set(_cache_key, result)

        return result

    except Exception as e:
        # Log with full traceback for server-side debugging, then re-raise.
        # Previously this path returned a 200 response with an `error`-shaped
        # payload masquerading as a successful answer — a P0-6 contract bug.
        # The re-raise propagates to the global exception handler
        # (`services/qna/src/core/errors.py`) which produces a 500
        # ErrorEnvelope with `code=INTERNAL_ERROR` plus X-Request-ID in the
        # body and the response header.
        logger.error(f"[{request_id}] Error in generate_answer: {type(e).__name__}")
        logger.error(f"[{request_id}] Traceback: {traceback.format_exc()}")
        raise


def _map_filenames_to_citation(filenames: list[str], file_map: dict[str, str]) -> dict[str, str]:
    """Resolve model-emitted filenames to a ``{filename: filepath}`` citation map.

    Extracted verbatim from the non-streaming `generate_answer` citation step so
    the streaming path produces a byte-identical citation map: exact normalized
    match first, then a unique-stem fallback, preserving model order and
    de-duplicating by real filename. Tolerant to bullets/case/spacing via
    `_norm_name`/`_stem`. Unresolvable names are dropped (never guessed).
    """
    norm_to_real: dict[str, tuple[str, str]] = {}
    stem_to_reals: dict[str, list[tuple[str, str]]] = {}
    for real_name, real_path in file_map.items():
        nk = _norm_name(real_name)
        norm_to_real[nk] = (real_name, real_path)
        st = _stem(nk)
        stem_to_reals.setdefault(st, []).append((real_name, real_path))

    extracted_filepath: dict[str, str] = {}
    seen: set[str] = set()
    for raw in filenames:
        nn = _norm_name(raw)
        hit = norm_to_real.get(nn)
        if hit:
            real_name, real_path = hit
            if real_name not in seen:
                extracted_filepath[real_name] = real_path
                seen.add(real_name)
            continue
        st = _stem(nn)
        candidates = stem_to_reals.get(st, [])
        if len(candidates) == 1:
            real_name, real_path = candidates[0]
            if real_name not in seen:
                extracted_filepath[real_name] = real_path
                seen.add(real_name)
    return extracted_filepath


async def generate_answer_stream(
    query: str,
    fr_mode: str,
    bot_tag: str,
    history: list[dict[str, str | None]],
    azure,
    request_id: str | None = None,
) -> AsyncIterator[tuple[str, Any]]:
    """Streaming sibling of `generate_answer` for the SSE `/qna/stream` route.

    Reuses the SAME leaf helpers as the non-streaming path (rephrasal,
    embedding, search, the grounded prompt, and the answer/citation extractor)
    so retrieval and citation behaviour match `/qna`. Only the final
    answer-generation step streams.

    Failure split (the SSE error contract): everything that can fail *before the
    first token* — fr_mode validation, rephrasal, embedding, and search — runs
    eagerly here, BEFORE the first token is yielded. Any failure in that phase
    raises so the HTTP handler can return the normal structured error envelope
    (no stream is opened). Once token generation begins, a failure from the LLM
    stream propagates out of the generator mid-stream so the handler emits a
    terminal SSE error event.

    Yields:
        ``("token", str)`` for each answer token in order, then exactly one
        ``("citation", dict)`` carrying the final ``{filename: filepath}`` map
        (byte-identical to the `/qna` ``citation`` field) and, when present,
        ``page_citations``.
    """
    request_id = request_id or f"gen_{int(time.time() * 1000)}"
    if not bot_tag or not bot_tag.strip():
        raise ValueError("bot_tag is required for tenant isolation")
    if fr_mode not in ("read", "layout"):
        raise ValueError(f"Invalid fr_mode: {fr_mode}. Must be 'read' or 'layout'.")

    log_event(
        logger,
        "answer_stream_started",
        request_id=request_id,
        fr_mode=fr_mode,
        query_length=len(query or ""),
    )

    history = history or []
    latest_q, prev_q, prev_prev_q, latest_bot_reply = _latest_three_and_reply(history)

    effective_query = query
    extracted_snippet = ""
    is_greeting = False
    is_followup = False
    file_map: dict[str, str] = {}
    file_pages: dict[str, list[str]] = {}

    # Best-effort rephrasal — same policy as the non-streaming path (failures are
    # swallowed and retrieval proceeds with the original query).
    try:
        rq = await rephrase_queries(
            azure=azure,
            current_query=latest_q,
            prev_query=prev_q,
            prev_prev_query=prev_prev_q,
            latest_bot_reply=latest_bot_reply,
            full_history=history,
        )
        if isinstance(rq, dict):
            effective_query = rq.get("rephrased_query") or query
            snippet = rq.get("extracted_snippet") or ""
            if not snippet and latest_bot_reply:
                snippet = latest_bot_reply
            if snippet:
                extracted_snippet = snippet
            is_greeting = rq.get("is_greeting")
            is_followup = rq.get("is_followup")
    except Exception as re_err:
        logger.warning(f"[{request_id}] Rephrase path skipped due to error: {re_err}")

    knowledge_source: list[str] = []
    # Retrieval runs eagerly here so a search/embedding failure surfaces BEFORE
    # the first token (-> normal error envelope), not mid-stream.
    if not is_greeting:
        fr_mode_tag = f"fr_{fr_mode}"
        vector = await get_embedding(azure, effective_query)
        results = await perform_search(azure, effective_query, vector, fr_mode_tag, bot_tag)
        for r in results:
            content = (r["content"] or "").replace("\n", "").replace("\r", "")
            knowledge_source.append(f"{r['filename']}: {content}")
            file_map[r["filename"]] = r["filepath"]
            page_raw = r.get("page_number")
            if page_raw is not None:
                page_str = str(page_raw).strip()
                if page_str:
                    pages = file_pages.setdefault(r["filename"], [])
                    if page_str not in pages:
                        pages.append(page_str)
        if extracted_snippet:
            try:
                sanitized = extracted_snippet.replace("\n", " ").replace("\r", " ")
                knowledge_source.append(
                    f"PrevAnswer.md (previous assistant reply; data-only, do not cite): \n{sanitized}"
                )
            except Exception as snip_err:
                logger.warning(f"[{request_id}] Skipped appending prior snippet: {snip_err}")

    # Token generation: from here on, failures are mid-stream. We accumulate the
    # full text so the SAME extractor can build the citation map after the
    # stream, matching the non-streaming `answer`/`citation` derivation.
    accumulated: list[str] = []
    async for token in stream_openai_response(
        effective_query,
        knowledge_source,
        is_greeting=is_greeting,
        is_follow_up=is_followup,
        azure=azure,
    ):
        accumulated.append(token)
        yield ("token", token)

    full_text = "".join(accumulated)
    _answer_text, filenames = await extract_answer_and_filenames_from_text(full_text)
    extracted_filepath = _map_filenames_to_citation(filenames, file_map)
    page_citations: dict[str, list[str]] | None = {
        real_name: file_pages[real_name] for real_name in extracted_filepath if file_pages.get(real_name)
    } or None

    log_event(
        logger,
        "answer_stream_completed",
        request_id=request_id,
        model=_localconfig.AZURE_LLM_MODEL,
        citation_count=len(extracted_filepath),
        answer_length_chars=len(full_text),
    )

    citation_payload: dict[str, Any] = {"citation": extracted_filepath}
    if page_citations is not None:
        citation_payload["page_citations"] = page_citations
    yield ("citation", citation_payload)
