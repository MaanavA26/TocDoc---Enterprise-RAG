######### Utils.py ##########

from pydantic import BaseModel, ConfigDict
from typing import Optional, List
import re


class BotQuery(BaseModel):
    """
    Normalized representation of a single turn in the bot conversation.

    Attributes:
        user_query: The user's input for this turn.
        bot_response: The bot's response for this turn (if present).
        answer: Optional alternate field used elsewhere for bot response content.

    Notes:
        - `model_config.extra = "allow"` permits additional fields without validation errors,
          preserving backward/forward compatibility with upstream payloads.
    """
    user_query: str
    bot_response: Optional[str] = None
    answer: Optional[str] = None
    model_config = ConfigDict(extra="allow")


class Payload(BaseModel):
    """
    Top-level request payload schema.

    Attributes:
        session_id: Correlation/session identifier.
        bot: Ordered list of conversation turns, oldest → newest.
        fr_tag: Feature/retrieval tag (e.g., 'read'/'layout' upstream).
        bot_tag: Bot identifier/tag.
    """
    session_id: str
    bot: List[BotQuery]
    fr_tag: str
    bot_tag: str


def _as_turn(obj) -> dict:
    """
    Return a normalized turn dict: {"user_query": str, "bot_response": str|None}
    for any supported shape (dict or object with attributes).

    Behavior (unchanged):
        - Prefer `bot_response`; fall back to `answer` if present.
        - Coerces empty/whitespace responses to `None`.
        - Trims `user_query`.
    """
    if isinstance(obj, dict):
        uq = (obj.get("user_query") or "").strip()
        br = obj.get("bot_response") or obj.get("answer")
    else:
        uq = (getattr(obj, "user_query", "") or "").strip()
        br = getattr(obj, "bot_response", None) or getattr(obj, "answer", None)

    return {
        "user_query": uq,
        "bot_response": (None if not br or not str(br).strip() else str(br)),
    }


def _field(obj, key: str):
    """
    Safely retrieve a field from either a dict or an object.

    Args:
        obj: Source container (dict or object with attributes).
        key: Field/attribute name to retrieve.

    Returns:
        The value if present, else `None`.
    """
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _latest_three_and_reply(history):
    """
    Extract the latest three user queries and the latest bot response.

    Returns a tuple:
        (latest_user_query, previous_user_query, previous_previous_user_query, latest_bot_response)

    Assumptions:
        - `history` is normalized with newest turn at the end.
        - When present, the "latest bot response" is taken from the **previous** turn
          (i.e., the bot's reply to the user's prior message).

    Missing items are returned as:
        - Empty string for the latest query when `history` is empty.
        - `None` for prior queries and bot response when unavailable.

    Examples:
        - len == 1 → (q0, None, None, r0)
        - len == 2 → (q1, q0, None, r1)
        - len >= 3 → (qN, qN-1, qN-2, rN)
    """
    if not history:
        return "", None, None, None

    latest_q = (_field(history[-1], "user_query") or "").strip()

    prev_q = (_field(history[-2], "user_query") or "").strip() if len(history) >= 2 else None
    prev_prev_q = (_field(history[-3], "user_query") or "").strip() if len(history) >= 3 else None

    last_resp = None
    if len(history) >= 2:
        val = _field(history[-2], "bot_response")
        if not val:
            val = _field(history[-2], "answer")
        last_resp = (str(val).strip() or None) if val else None

    return latest_q, (prev_q or None), (prev_prev_q or None), last_resp

def _norm_name(s: str) -> str:
    """
    Normalize a filename-ish string to improve matching robustness.
    - Trim whitespace
    - Lowercase
    - Collapse inner whitespace
    - Strip leading bullets/numbers ("- ", "* ", "1. ", "1) ", "• ")
    - Remove exotic quotes/backticks and trailing punctuation
    - Keep dots/dashes/underscores/spaces so extensions are preserved
    """
    if s is None:
        return ""
    s = s.strip()
    # strip common leading list markers
    s = re.sub(r'^\s*(?:[-*•]\s+|\d+[\.\)]\s+)', '', s)
    # collapse whitespace and lowercase
    s = re.sub(r'\s+', ' ', s).lower()
    # drop surrounding quotes/backticks
    s = s.strip('`"\'')
    # drop trailing punctuation that LLMs sometimes leave
    s = s.rstrip('.,;:')
    # remove any characters except letters/digits/space/._-
    s = re.sub(r'[^a-z0-9\.\- _]+', '', s)
    return s

def _stem(n: str) -> str:
    """
    A lightweight 'stem' (filename without extension) without importing os/pathlib.
    """
    return re.sub(r'\.[a-z0-9]{1,6}$', '', n)  # handles .pdf/.docx/.xlsx/etc.