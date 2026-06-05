import asyncio
import inspect
import re
from concurrent.futures import ThreadPoolExecutor

from src.core.logger import logger

# ---------------------------------------------------------------------------
# Thread pool for CPU-bound text parsing (kept as-is)
# ---------------------------------------------------------------------------
text_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="text")


async def extract_answer_and_filenames_from_text(text: str) -> tuple[str, list[str]]:
    """
    Extract the final answer text and referenced filenames from a model response.

    Behavior (unchanged):
      - Runs the synchronous extractor `_extract_sync` in a thread executor.
      - Defensively awaits if a coroutine is accidentally returned.
      - Validates the return shape and types; falls back to `(text.strip(), [])` on error.

    Args:
        text: The raw model response string that may contain a "**Sources:" section.

    Returns:
        Tuple[str, List[str]]: `(answer_text, filenames)` where:
            - `answer_text` is the portion before the "**Sources:" marker.
            - `filenames` is a list of filenames parsed from the sources section.
    """
    # NEVER log the raw answer text — even at DEBUG it bypasses the log_event
    # truncation/hygiene net and violates the observability policy (audit L-Q7).
    # Log metadata only; length is enough to debug extraction issues.
    logger.debug("Extracting answer and filenames from text (length=%d)", len(text or ""))
    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(text_executor, _extract_sync, text)

        # Defensive guard: if some future refactor accidentally returns a coroutine, await it.
        if inspect.isawaitable(result):
            logger.warning("Extractor returned a awaitable; awaiting it defensively.")
            result = await result

        # Validate & coerce to the expected shape
        if not (isinstance(result, tuple) and len(result) == 2):
            logger.error(f"Extractor returned unexpected type: {type(result)} value: {result!r}")
            return text.strip(), []

        answer_text, filenames = result
        if not isinstance(answer_text, str) or not isinstance(filenames, list):
            logger.error(f"Bad return types from extractor: {type(answer_text)}, {type(filenames)}")
            return str(answer_text).strip(), list(filenames)

        return answer_text, filenames

    except Exception as e:
        logger.error(f"Error extracting answer and filenames: {e}")
        return text.strip(), []


def _extract_sync(text: str) -> tuple[str, list[str]]:
    """
    Synchronous, CPU-bound parser. MUST return (answer_text, filenames) and NOT a coroutine.

    Tolerant parsing:
    - Accepts "Sources:" or "Source:", any case.
    - Optional bold markers around the header, before and/or after the colon (e.g., "**Sources:**").
    - Captures everything after the header (across lines).
    - Extracts filenames from:
        * Markdown links: [filename](url)
        * Bracket blocks: [a.md; b.pdf]
        * Plain lines (strips bullets/numbers), one per line.
    """

    # --- 1) Find the split point (answer vs sources block) ---
    # Header variants we accept:
    #   Sources: | Source: | **Sources:** | **Source:** |   (case-insensitive)
    # Optional bold (** ... **), optional spaces, optional extra asterisks after colon.
    sources_hdr = r"(?:\*\*)?\s*(?:sources?|SOURCE|Sources?)\s*:\s*(?:\*\*)?"
    m = re.search(rf"^(.*?)(?:{sources_hdr})(.+)$", text, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        # No sources header: return the whole text as the answer, no filenames.
        return text.strip(), []

    answer_text = m.group(1).strip()
    filenames_raw = m.group(2).strip()

    # --- 2) Extract filenames from common formats ---

    filenames: list[str] = []

    # 2a) Markdown links: [filename](url)
    md_links = re.findall(r"\[([^\]]+?)\]\([^)]+?\)", filenames_raw)
    filenames.extend(md_links)

    # 2b) Bracket-blocks: [a.md; b.pdf]  (split on ';' inside the block)
    bracket_blocks = re.findall(r"\[([^\]]+)\]", filenames_raw)
    for blk in bracket_blocks:
        parts = [p.strip() for p in blk.split(";")]
        for p in parts:
            if p:
                filenames.append(p)

    # 2c) Fallback: plain lines (strip bullets/numbers and whitespace)
    if not filenames:
        lines = [ln.strip() for ln in filenames_raw.splitlines() if ln.strip()]
        for ln in lines:
            # remove common list markers
            ln = re.sub(r"^\s*[-*]\s*", "", ln)  # - item / * item
            ln = re.sub(r"^\s*\d+[\.\)]\s*", "", ln)  # 1. item / 1) item
            # if still a markdown link-ish "[text]" remains, keep text inside []
            m2 = re.match(r"\[([^\]]+)\]", ln)
            if m2:
                ln = m2.group(1)
            if ln:
                filenames.append(ln.strip())

    # Final sanitize: trim quotes/backticks and trailing punctuation that often sneaks in
    clean = []
    for f in filenames:
        f = f.strip().strip("`\"'").rstrip(".,;:")
        if f:
            clean.append(f)

    return answer_text, clean
