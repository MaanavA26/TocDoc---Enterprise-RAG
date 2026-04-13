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

import time
from typing import Dict, Any, Optional, List
from src.core.logger import logger
from src.services.embedding_service import get_embedding
from src.services.search_service import perform_search
from src.services.openai_service import generate_openai_response, rephrase_queries
from src.services.text_processor import extract_answer_and_filenames_from_text
from src.utils.util import _latest_three_and_reply, _norm_name, _stem
import traceback
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------------------
# Executors / globals
# ---------------------------------------------------------------------------
process_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="process")


async def generate_answer(
    query: str,
    fr_mode: str,
    bot_tag: str,
    history: List[Dict[str, Optional[str]]],
    azure,
) -> Dict[str, Any]:
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

    Returns:
        A dict with:
            - "answer": str
            - "citation": {filename: filepath, ...}
            - ("request_id"/"error" only in exceptional cases)

    Notes:
        - Rephrasal is best-effort; the pipeline proceeds if it fails.
        - History is never stored in module-level state; each call is isolated.
    """
    request_id = f"gen_{int(time.time() * 1000)}"
    logger.info(f"[{request_id}] Starting answer generation")
    logger.info(f"[{request_id}] Query: {query!r}, fr_mode: {fr_mode!r}")
    start_time = time.time()

    # Defensive normalisation: treat None as an empty list.
    history = history or []

    try:
        if fr_mode not in ("read", "layout"):
            msg = f"Invalid fr_mode: {fr_mode}. Must be 'read' or 'layout'."
            logger.error(f"[{request_id}] {msg}")
            raise ValueError(msg)

        # Pull: current user query, the two prior user queries, and
        # the latest bot response.
        latest_q, prev_q, prev_prev_q, latest_bot_reply = _latest_three_and_reply(
            history
        )

        logger.info(
            f"[{request_id}] history types -> "
            f"{[type(x).__name__ for x in history]}"
        )
        logger.info(
            f"[{request_id}] prev turn sample -> "
            f"{history[-2] if len(history) >= 2 else 'n/a'}"
        )
        logger.info(
            f"[{request_id}] Latest Query: {latest_q},\n"
            f" Previous Query: {prev_q},\n"
            f" Previous Previous Query: {prev_prev_q},\n"
            f" Latest Bot Reply: {latest_bot_reply}"
        )

        # Use the caller's `query` as source of truth for the current turn.
        effective_query = query
        extracted_snippet: str = ""

        # Default setting
        is_greeting = False
        is_followup = False
        file_map: Dict[str, str] = {}

        # Best-effort rephrasal using full, normalized history.
        try:
            logger.info(f"[{request_id}] Rephrasing path enabled ")
            rq = await rephrase_queries(
                azure=azure,
                current_query=latest_q,
                prev_query=prev_q,
                prev_prev_query=prev_prev_q,
                latest_bot_reply=latest_bot_reply,
                full_history=history,
            )
            if isinstance(rq, dict):
                effective_query = rq.get("rephrased_query")
                if effective_query:
                    if effective_query != query:
                        logger.info(
                            f"[{request_id}] Query rephrased for retrieval"
                        )

                # Prefer a snippet extracted by the rephraser; fall back to
                # the latest bot reply.
                snippet = rq.get("extracted_snippet") or ""
                if not snippet and latest_bot_reply:
                    snippet = latest_bot_reply
                if snippet:
                    extracted_snippet = snippet
                    logger.info(
                        f"[{request_id}] Prior bot snippet detected "
                        f"(will be added as data-only line)"
                    )
                is_greeting = rq.get("is_greeting")
                if is_greeting:
                    logger.info("[%s] Is greeting: %s", request_id, is_greeting)
                is_followup = rq.get("is_followup")
                if is_followup:
                    logger.info("[%s] Is Follow-up : %s", request_id, is_followup)

        except Exception as re_err:
            # Rephrasal is best-effort; retrieval proceeds regardless.
            logger.warning(
                f"[{request_id}] Rephrase path skipped due to error: {re_err}"
            )

        knowledge_source: List[str] = []

        # If it's a greeting rather than a question we don't send it for
        # knowledge retrieval!
        if not is_greeting:
            # Embed and search
            fr_mode_tag = f"fr_{fr_mode}"
            logger.info(f"[{request_id}] Step 1: Generating embedding")
            vector = await get_embedding(azure, effective_query)

            logger.info(f"[{request_id}] Step 2: Performing search")
            results = await perform_search(
                azure, effective_query, vector, fr_mode_tag, bot_tag
            )

            # Build knowledge base lines and a filename→filepath map
            # for citation resolution
            logger.info(f"[{request_id}] Step 3: Processing search results")

            for r in results:
                # r: {"filename", "filepath", "content", ...}
                content = (r["content"] or "").replace("\n", "").replace("\r", "")
                knowledge_source.append(f"{r['filename']}: {content}")
                file_map[r["filename"]] = r["filepath"]

            # Optionally append the latest bot reply as data-only (not cited)
            if extracted_snippet:
                try:
                    sanitized = extracted_snippet.replace("\n", " ").replace(
                        "\r", " "
                    )
                    knowledge_source.append(
                        "PrevAnswer.md (previous assistant reply; "
                        "data-only, do not cite): \n"
                        f"{sanitized}"
                    )
                except Exception as snip_err:
                    logger.warning(
                        f"[{request_id}] Skipped appending prior snippet: {snip_err}"
                    )
        else:
            # Greeting path
            logger.info(
                f"[{request_id}] Greeting Detected, skipping retrieval & "
                f"calling model directly!"
            )

        logger.debug(
            f"[{request_id}] KB lines: {len(knowledge_source)} | "
            f"file_map: {len(file_map)}"
        )

        # Model call
        logger.info(f"[{request_id}] Step 4: Generating model response")
        ans = await generate_openai_response(
            effective_query,
            knowledge_source,
            is_greeting=is_greeting,
            is_follow_up=is_followup,
            azure=azure,
        )

        # Extract the final answer text and the filenames the model referenced
        logger.info(f"[{request_id}] Step 5: Extracting answer and sources")
        if not isinstance(ans, str):
            raise TypeError(
                f"Model response type must be str, got {type(ans)}"
            )

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

        extracted_filepath: Dict[str, str] = {}
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
            logger.debug(
                "[%s] Citation mapping misses (model → normalized): %s",
                request_id, misses
            )

        total_time = time.time() - start_time
        logger.info(
            f"[{request_id}] Answer generation completed in {total_time:.4f}s"
        )
        logger.info(
            f"[{request_id}] Answer length: {len(answer_text)} chars | "
            f"citations: {len(extracted_filepath)}"
        )

        return {
            "answer": answer_text,
            "citation": extracted_filepath,
        }

    except Exception as e:
        logger.error(f"[{request_id}] Error in generate_answer: {e}")
        logger.error(f"[{request_id}] Traceback: {traceback.format_exc()}")
        return {
            "answer": (
                "An error occurred while generating the answer. "
                "Please try again."
            ),
            "citation": {},
            "error": str(e),
            "request_id": request_id,
        }