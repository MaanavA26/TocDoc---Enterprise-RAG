import asyncio
import json
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from inspect import isawaitable

from src.config.config import LocalConfig
from src.core.logger import logger
from src.llm.prompts import (
    generate_bot,
    map_extract_prompt,
    reduce_combine_prompt,
    rephrasal_prompt,
)

# ---------------------------------------------------------------------------
# Executors / local config
# ---------------------------------------------------------------------------
openai_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="openai")
localconfig = LocalConfig()


async def generate_openai_response(
    query: str,
    knowledge_source: list[str],
    is_greeting: bool,
    is_follow_up: bool,
    azure,
) -> str:
    """
    Generate an OpenAI response using a grounded prompt.

    Offloads the synchronous LLM call to a thread pool via `run_in_executor`
    to avoid blocking the event loop.

    Args:
        query: The user's query (possibly rephrased upstream).
        knowledge_source: List of source lines (filename-prefixed) to ground the prompt.
        is_greeting: Whether the turn was detected as a greeting.
        is_follow_up: Whether the turn was detected as a follow-up.
        azure: Holder with `openai_client` (AzureOpenAI).

    Returns:
        The model-composed answer as a string.

    Raises:
        Exception: Any error during invocation is logged and re-raised.
    """
    logger.info("Generating OpenAI response for query: '%s'", query)
    logger.debug(f"Knowledge source contains {len(knowledge_source)} items")

    try:
        start_time = time.time()

        loop = asyncio.get_running_loop()
        ans = await loop.run_in_executor(
            openai_executor,
            _generate_response_sync,
            query,
            knowledge_source,
            is_greeting,
            is_follow_up,
            azure,
        )

        if isawaitable(ans):
            ans = await ans

        generation_time = time.time() - start_time
        logger.info(f"OpenAI response generated in {generation_time:.4f}s")
        logger.debug(f"Response length: {len(ans)} characters")

        return ans

    except Exception as e:
        logger.error(f"Error generating OpenAI response: {str(e)}")
        logger.error(f"Traceback: {traceback.format_exc()}")
        raise


def _generate_response_sync(
    query: str,
    knowledge_source: list[str],
    is_greeting: bool,
    is_follow_up: bool,
    azure,
) -> str:
    """
    Synchronous helper that composes the grounded prompt and calls the chat API.

    Args:
        query: Current user query.
        knowledge_source: Grounding lines for the prompt.
        is_greeting: Greeting flag.
        is_follow_up: Follow-up flag.
        azure: Holder with `openai_client`.

    Returns:
        str: The model's message content.
    """
    deployment_name = localconfig.AZURE_LLM_MODEL
    logger.debug(f"Using deployment: {deployment_name}")

    response = azure.openai_client.chat.completions.create(
        messages=[
            {
                "role": "user",
                "content": generate_bot.format(
                    query=query,
                    sources="\n".join(knowledge_source),
                    is_greeting=is_greeting,
                    is_follow_up=is_follow_up,
                ),
            }
        ],
        model=localconfig.AZURE_LLM_MODEL,
    )

    if isawaitable(response):
        logger.debug("chat.completions.create returned awaitable; running to completion in sync context")
        response = asyncio.run(response)

    return response.choices[0].message.content


async def rephrase_queries(
    azure,
    current_query: str,
    prev_query: str | None = None,
    prev_prev_query: str | None = None,
    latest_bot_reply: str | None = None,
    full_history: list[dict[str, str | None]] | None = None,
) -> dict[str, object]:
    """
    Optionally rephrase the current user query using recent conversation context.

    Inputs:
      - current_query: latest user query (mandatory)
      - prev_query: previous user query (optional)
      - prev_prev_query: one more previous user query (optional)
      - latest_bot_reply: most recent bot response (optional)
      - full_history: normalized history list (optional; for prompt context/stats)

    Returns:
      {
        'rephrased_query': str,
        'is_greeting': bool,
        'original_response': str,
        'extracted_snippet': str,   # prior bot reply if follow-up
        'is_followup': bool,
        'was_rephrased': bool
      }
    """
    query = (current_query or "").strip()
    prev_q = (prev_query or "").strip()
    prev_prev_q = (prev_prev_query or "").strip()
    last_reply = (latest_bot_reply or "").strip()

    bot_convo = {
        "user_query": query,
        "bot_response": last_reply,
    }
    context_for_model = {
        "has_prev_reply": bool(last_reply),
        "history_count": len(full_history) if full_history else 0,
    }

    rendered = rephrasal_prompt.format(
        query=query,
        prev_query=prev_q,
        prev_prev_query=prev_prev_q,
        latest_bot_reply=last_reply,
        bot_queries=bot_convo,
        context_for_model=context_for_model,
        full_history=full_history or [],
    )

    is_greeting: bool = False
    is_followup: bool = False
    extracted_snippet: str = ""
    response_content: str = ""

    try:
        resp = azure.openai_client.chat.completions.create(
            messages=[{"role": "user", "content": rendered}],
            model=localconfig.AZURE_LLM_MODEL,
        )
        if isawaitable(resp):
            resp = await resp

        response_content = resp.choices[0].message.content or ""
        logger.info(f"Response from rephrasal LLM: {response_content}")

        pattern = r'\["([^"]+)"\](?:\[(greeting|followup)\])?'
        match = re.search(pattern, response_content, re.IGNORECASE)

        if match:
            extracted_query = match.group(1)
            tag = match.group(2) or ""

            is_greeting = tag.lower() == "greeting"
            is_followup = tag.lower() == "followup"
            was_rephrased = extracted_query.strip() != query
            extracted_snippet = last_reply if (is_followup and last_reply) else ""

            logger.info(f"Extracted Snippet: {extracted_snippet}")

            return {
                "rephrased_query": extracted_query,
                "is_greeting": is_greeting,
                "original_response": response_content,
                "extracted_snippet": extracted_snippet,
                "is_followup": is_followup,
                "was_rephrased": was_rephrased,
            }
        else:
            return {
                "rephrased_query": query,
                "is_greeting": False,
                "original_response": response_content,
                "extracted_snippet": extracted_snippet,
                "is_followup": is_followup,
                "was_rephrased": False,
            }

    except Exception as e:
        logger.error(f"Error in rephrasing: {e}")
        return {
            "rephrased_query": query,
            "is_greeting": False,
            "original_response": response_content,
            "extracted_snippet": extracted_snippet,
            "is_followup": False,
            "was_rephrased": False,
        }


# ---------------------------------------------------------------------------
# Structured-output helper (NEW in P3) — JSON-schema-constrained classification
# ---------------------------------------------------------------------------
# Before P3 the service only ever regex-parsed free-text LLM output (see
# `rephrase_queries` above). The agentic router needs a *machine-checkable*
# decision, so this is the service's first `response_format`/JSON-schema call.
# It is deliberately generic (caller supplies the schema) so later P3 nodes
# (verifier, etc.) can reuse the same path rather than re-inventing it.

# Routes the classifier may choose. Kept here next to the helper so callers and
# the schema share one source of truth; the router validates against this set.
ROUTE_LABELS: tuple[str, ...] = ("standard", "map_reduce", "react")


def _structured_completion_sync(
    azure,
    *,
    system_prompt: str,
    user_prompt: str,
    schema_name: str,
    json_schema: dict,
    model: str | None = None,
) -> dict:
    """Synchronous structured-output chat call returning a parsed JSON object.

    Uses Azure OpenAI's ``response_format`` with a strict JSON schema so the
    model's reply is guaranteed to be a JSON object matching ``json_schema``.
    Mirrors the sync-client call style of the other helpers in this module
    (``azure.openai_client.chat.completions.create(...)``); the caller offloads
    it to a thread when needed.

    Raises on any transport/parse error — the caller (best-effort node) is
    responsible for catching and defaulting.
    """
    deployment_name = model or localconfig.AZURE_LLM_MODEL

    response = azure.openai_client.chat.completions.create(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        model=deployment_name,
        temperature=0,
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": json_schema,
            },
        },
    )

    if isawaitable(response):
        logger.debug("structured create returned awaitable; running to completion in sync context")
        response = asyncio.run(response)

    content = response.choices[0].message.content or "{}"
    return json.loads(content)


def map_extract_sync(azure, *, query: str, sources: str, model: str | None = None) -> str:
    """Synchronous **map** step: extract the facts in one batch of chunks that
    are relevant to ``query``.

    Module-level (not nested) so the P3-2 map-reduce node can offload it via
    ``run_in_executor`` and tests can monkeypatch it to observe the
    bounded-executor / semaphore fan-out. Returns the model's free-text extract
    ("NO_RELEVANT_INFORMATION" when the batch is irrelevant, by prompt
    contract). Raises on transport error — the caller owns retry/backoff.

    The chat call mirrors the sync-client style used elsewhere in this module
    (``azure.openai_client.chat.completions.create(...)``).
    """
    deployment_name = model or localconfig.AZURE_LLM_MODEL
    prompt = map_extract_prompt.format(query=query, sources=sources)

    response = azure.openai_client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model=deployment_name,
    )
    if isawaitable(response):
        logger.debug("map_extract create returned awaitable; running to completion in sync context")
        response = asyncio.run(response)

    return response.choices[0].message.content or ""


def reduce_combine_sync(azure, *, query: str, extracts: str, model: str | None = None) -> str:
    """Synchronous **reduce** step: combine the per-batch extracts into the
    final grounded answer (with a ``**Sources:`` section the existing
    ``extract_answer_and_filenames_from_text`` parser understands).

    Module-level + monkeypatchable like ``map_extract_sync``. Uses the
    (typically larger) reduce model by default. Raises on transport error.
    """
    deployment_name = model or localconfig.AZURE_OPENAI_REDUCE_MODEL
    prompt = reduce_combine_prompt.format(query=query, extracts=extracts)

    response = azure.openai_client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model=deployment_name,
    )
    if isawaitable(response):
        logger.debug("reduce_combine create returned awaitable; running to completion in sync context")
        response = asyncio.run(response)

    return response.choices[0].message.content or ""


async def classify_route(azure, query: str) -> str:
    """Classify a query into one of ``ROUTE_LABELS`` via structured output.

    Returns a route string guaranteed to be a member of ``ROUTE_LABELS``;
    any off-schema / unexpected value collapses to ``"standard"``. This helper
    itself does NOT swallow transport errors — it offloads the sync call to the
    shared OpenAI executor and lets exceptions propagate so the caller (the
    best-effort classifier node) owns the catch-and-default policy.

    The query is sent to the model (it must be, to classify it) but is never
    logged here; log hygiene is the caller's responsibility.
    """
    system_prompt = (
        "You are a routing classifier for a document question-answering system. "
        "Choose the single best strategy to answer the user's question:\n"
        "- 'standard': a focused question answerable from a few relevant passages "
        "(the default; prefer it when unsure).\n"
        "- 'map_reduce': a broad summarization/aggregation question that needs the "
        "whole corpus (e.g. 'summarize all documents', 'list every policy').\n"
        "- 'react': a multi-hop question requiring several dependent lookups or "
        "entity reasoning across documents.\n"
        "Respond ONLY with the structured object."
    )
    json_schema = {
        "type": "object",
        "properties": {
            "route": {
                "type": "string",
                "enum": list(ROUTE_LABELS),
            }
        },
        "required": ["route"],
        "additionalProperties": False,
    }

    loop = asyncio.get_running_loop()
    parsed = await loop.run_in_executor(
        openai_executor,
        lambda: _structured_completion_sync(
            azure,
            system_prompt=system_prompt,
            user_prompt=query,
            schema_name="route_decision",
            json_schema=json_schema,
        ),
    )

    route = parsed.get("route")
    if route in ROUTE_LABELS:
        return route
    # Off-schema / unexpected value — never trust it (don't log the raw
    # model-returned value), fall back to the safe route.
    logger.warning("Classifier returned an unexpected route; defaulting to 'standard'")
    return "standard"
