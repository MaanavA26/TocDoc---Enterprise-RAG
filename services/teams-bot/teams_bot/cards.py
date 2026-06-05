"""Adaptive-card rendering for QnA answers and error envelopes.

The QnA success body is ``{answer, citation}`` with ``citation`` a flat
``{filename: filepath}`` map. This module turns that into a Teams adaptive card
and turns a P0-6 ``ApiError`` into a friendly card that surfaces the
``request_id`` for support correlation.

Citation rendering — deliberate ADR-aligned choice
---------------------------------------------------
``filepath`` is an **internal blob/source path, not a user-clickable URL**.
Per the ADR (citation rendering section) we render ``filename`` as **plain
text**, NOT as an ``Action.OpenUrl`` link: a blind link to an internal path is
either broken or an over-permissive leak. Clickable citations are gated on a
future permission-aware resolver (``filepath`` -> an authorized SAS/SharePoint
URL honoring the user's permissions). Until that exists, text only.

This intentionally departs from the build brief's parenthetical "citations as
links"; the ADR's security rationale wins. The departure is called out in the
PR.

The functions return plain ``dict`` adaptive-card payloads (schema
``adaptivecards.io`` 1.4) so they are testable without botbuilder; the bot
wraps them with ``CardFactory.adaptive_card``.
"""

from __future__ import annotations

from collections.abc import Mapping

ADAPTIVE_CARD_VERSION = "1.4"
_SCHEMA = "http://adaptivecards.io/schemas/adaptive-card.json"


def _citation_block(citations: Mapping[str, str]) -> list[dict]:
    """Build the card body elements for the citation map (filenames as text)."""
    if not citations:
        return []

    items: list[dict] = [
        {
            "type": "TextBlock",
            "text": "Sources",
            "weight": "Bolder",
            "spacing": "Medium",
            "wrap": True,
        }
    ]
    # Iterate generically (one entry per cited document) so the card is
    # forward-compatible: a future page-aware citation shape can extend the per-
    # entry text without changing this loop. filename rendered as plain,
    # non-navigating text — never a link to the internal filepath.
    for filename in citations:
        items.append(
            {
                "type": "TextBlock",
                "text": f"• {filename}",
                "wrap": True,
                "spacing": "Small",
                "isSubtle": True,
            }
        )
    return items


def render_answer_card(answer: str, citations: Mapping[str, str]) -> dict:
    """Render a ``{answer, citation}`` QnA result as an adaptive card payload.

    Args:
        answer: The grounded answer text (card body).
        citations: ``{filename: filepath}`` map; only ``filename`` is shown,
            as plain text.

    Returns:
        An adaptive-card dict (``$schema``/``type``/``version``/``body``).
    """
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": answer,
            "wrap": True,
        }
    ]
    body.extend(_citation_block(citations))

    return {
        "$schema": _SCHEMA,
        "type": "AdaptiveCard",
        "version": ADAPTIVE_CARD_VERSION,
        "body": body,
    }


def render_error_card(message: str, request_id: str | None) -> dict:
    """Render a friendly error card surfacing the ``request_id`` for support.

    Args:
        message: A safe, human-readable message (never raw exception detail).
        request_id: The QnA ``request_id`` from the P0-6 envelope, if any.

    Returns:
        An adaptive-card dict.
    """
    body: list[dict] = [
        {
            "type": "TextBlock",
            "text": message,
            "wrap": True,
            "weight": "Bolder",
        }
    ]
    if request_id:
        body.append(
            {
                "type": "TextBlock",
                "text": f"Reference ID: {request_id}",
                "wrap": True,
                "isSubtle": True,
                "spacing": "Small",
            }
        )

    return {
        "$schema": _SCHEMA,
        "type": "AdaptiveCard",
        "version": ADAPTIVE_CARD_VERSION,
        "body": body,
    }
