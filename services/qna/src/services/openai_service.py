import asyncio
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from inspect import isawaitable

from src.config.config import LocalConfig
from src.core.logger import logger
from src.llm.prompts import generate_bot, rephrasal_prompt

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
