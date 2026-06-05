"""Unspoofable identity -> bot_tag derivation (the central invariant).

This module is the security spine of the adapter and is deliberately *pure*:
it imports no Bot Framework or HTTP code, so it is trivially unit-testable and
so that the one invariant that matters can be made true *by construction*.

The invariant
-------------
``bot_tag`` is derived **server-side** from the Microsoft-signed, service-
stamped ``channelData.tenant.id`` of a *verified* inbound activity, via an
admin-configured tenant -> bot_tag map. The end user never types, names, sees,
or supplies a ``bot_tag``.

The function in this module takes a ``tenant_id`` (a string the caller has
already extracted from the *verified* activity) and returns a ``bot_tag``. It
**does not accept the user's message text at all** — so "a user's message can
never change the derived bot_tag" is true by signature, not by a runtime guard
that could be bypassed. The caller (``bot.py``) keeps the message text and the
tenant id in separate variables and only ever passes the tenant id here.

Failure modes are explicit and fail-closed:

- An unknown tenant (not present in the map) raises :class:`UnknownTenantError`
  and **no QnA call is made**. A user in an unmapped tenant is never served a
  default ``bot_tag``.
- A resolved value that does not match the decision-record format regex
  ``^[A-Za-z0-9_-]{1,128}$`` raises :class:`InvalidBotTagError`. This validates
  the *operator-configured* value (a misconfiguration) before it can reach the
  downstream OData filter.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# Decision-record bot_tag format (docs/architect_phase_2/04_BOT_TAG_DECISION_RECORD.md,
# Validation rules). Rejects quotes, spaces, semicolons, OData operators,
# path-traversal, and over-long values before they can reach the search filter.
BOT_TAG_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class IdentityResolutionError(Exception):
    """Base class for failures deriving a bot_tag from a verified identity."""


class UnknownTenantError(IdentityResolutionError):
    """The verified tenant id is not present in the configured map (fail-closed)."""

    def __init__(self, tenant_id: str) -> None:
        # Note: tenant_id is a Microsoft GUID, not a secret; safe to include for
        # operator debugging. No tokens or message text are ever logged here.
        self.tenant_id = tenant_id
        super().__init__("No bot_tag mapping configured for the requesting tenant.")


class InvalidBotTagError(IdentityResolutionError):
    """A configured bot_tag value fails the format/length validation."""

    def __init__(self, bot_tag: str) -> None:
        self.bot_tag = bot_tag
        super().__init__("Configured bot_tag does not satisfy the required format.")


def resolve_bot_tag(tenant_id: str, tenant_bot_tag_map: Mapping[str, str]) -> str:
    """Derive the ``bot_tag`` for a *verified* tenant id.

    Args:
        tenant_id: The ``channelData.tenant.id`` from an inbound activity whose
            Bot Framework JWT has **already been validated** by the caller. This
            value is Microsoft-signed and service-stamped; it is never read from
            the user's message text.
        tenant_bot_tag_map: Admin-configured ``{tenant_id: bot_tag}`` mapping.

    Returns:
        The resolved, format-validated ``bot_tag``.

    Raises:
        UnknownTenantError: ``tenant_id`` is not in the map (fail-closed).
        InvalidBotTagError: The mapped value fails ``BOT_TAG_PATTERN``.

    Note:
        This function intentionally has no parameter for message text. The
        anti-spoof property — a user's message can never select another
        tenant's bot_tag — holds because text is not in this function's scope.
    """
    if not tenant_id:
        # An empty/absent tenant id on a *verified* activity is itself a
        # fail-closed condition: we cannot resolve a scope, so we reject.
        raise UnknownTenantError(tenant_id)

    bot_tag = tenant_bot_tag_map.get(tenant_id)
    if bot_tag is None:
        raise UnknownTenantError(tenant_id)

    if not BOT_TAG_PATTERN.match(bot_tag):
        raise InvalidBotTagError(bot_tag)

    return bot_tag
