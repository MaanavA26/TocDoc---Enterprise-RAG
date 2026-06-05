"""Within-tenant ``bot_tag`` <-> ``tid`` binding guard (threat-model R1).

## The gap this closes

The ``/qna`` request body carries a ``bot_tag`` that selects which workspace's
documents the search layer retrieves (it flows into the Azure Cognitive Search
OData filter in ``src/services/search_service.py``). Authentication proves the
caller belongs to an Azure AD tenant (the validated JWT's ``tid`` claim), but
nothing today ties the *requested* ``bot_tag`` to that tenant. So a caller
authenticated for tenant ``T`` can pass **any** ``bot_tag`` and read another
workspace's data that happens to live under the same tenant. This is the
threat-model **R1** within-tenant isolation gap.

## What this guard does (and does not do)

This is a **fail-closed, default-ON** server-side binding check, mirroring the
Teams-bot server-side binding pattern: the trusted ``tid`` comes from the
validated token (never the request body), and the requested ``bot_tag`` is
checked against a config-driven allowlist for that ``tid``.

- ``QNA_ENFORCE_TENANT_BINDING`` (default **true**) — master switch. When unset
  the guard is **ON**, so a multi-workspace deployment is isolated out of the
  box. Set it to ``false``/``0``/``no``/``off`` to explicitly opt out (only a
  genuinely single-workspace deployment that derives ``bot_tag`` elsewhere
  should do so).
- ``QNA_TENANT_BOT_TAG_MAP`` — JSON object mapping each ``tid`` to the list of
  ``bot_tag`` values that tenant is allowed to query, e.g.::

      {"11111111-1111-1111-1111-111111111111": ["workspace-a", "workspace-b"],
       "22222222-2222-2222-2222-222222222222": ["workspace-c"]}

  When enforcement is ON this map is **required**: a missing or unparseable
  map means no tenant has an allowlist, so every request fails closed. A
  single-workspace deployment must therefore either configure this map (one
  ``tid`` → its one ``bot_tag``) or explicitly opt out via the flag above.

When enforcement is ON the guard **fails closed**: a malformed/missing map, a
missing ``tid``, an unmapped ``tid``, or a ``bot_tag`` not in the tenant's
allowlist all reject the request via the structured error envelope (403) and
**no QnA / search call is made**. The rejection message is generic and never
echoes the map contents or the offending values; a distinct operator-facing
log fires when the failure is a misconfiguration (missing/empty map) so the
"refuse to serve" cause is diagnosable without leaking tenant data.

## v1 seam

This is a deliberately small v1 seam: a process-config allowlist read live per
request. A later version can source the mapping from a control-plane store
instead of an env var without changing the call site — the guard stays a single
dependency in the request path (see ``app.py`` ``custom_rag_qna``).
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import Request

from src.config.config import is_tenant_binding_enforced
from src.core.errors import ApiErrorCode, raise_api_error

logger = logging.getLogger(__name__)


def _load_tenant_bot_tag_map() -> dict[str, list[str]]:
    """Parse ``QNA_TENANT_BOT_TAG_MAP`` JSON into a ``{tid: [bot_tag, ...]}`` map.

    Read live from the environment so the allowlist can change without a
    redeploy. Returns an empty dict on any problem (unset, malformed JSON,
    wrong top-level type) — callers MUST treat an empty/partial map as
    "no allowlist for this tid" and therefore fail closed. Non-list values
    for a tid are normalised to an empty list (again, fail closed) rather
    than raising.

    This is only ever called on the enforcement-ON path, so a malformed map
    can never affect the default-OFF behaviour.
    """
    raw = os.getenv("QNA_TENANT_BOT_TAG_MAP")
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        # Do NOT log the raw value — it is operator config, but keeping it out
        # of logs avoids accidentally surfacing tenant identifiers in log sinks.
        logger.error("QNA_ENFORCE_TENANT_BINDING is on but QNA_TENANT_BOT_TAG_MAP is not valid JSON")
        return {}
    if not isinstance(parsed, dict):
        logger.error("QNA_TENANT_BOT_TAG_MAP must be a JSON object mapping tid -> [bot_tag, ...]")
        return {}
    normalised: dict[str, list[str]] = {}
    for tid, tags in parsed.items():
        if isinstance(tags, list):
            normalised[str(tid)] = [str(t) for t in tags]
        else:
            # Malformed entry: treat as an empty allowlist (fail closed) rather
            # than raising or silently allowing.
            normalised[str(tid)] = []
    return normalised


def enforce_tenant_bot_tag_binding(request: Request, bot_tag: str) -> None:
    """Guard: when enforcement is ON, require ``bot_tag`` to be allowed for ``tid``.

    Placed in the ``/qna`` request path BEFORE any search/pipeline call so it
    covers both the legacy and agentic routes and rejects before retrieval.

    Behaviour:
        - Enforcement ON (default): the token's ``tid`` (set on
          ``request.state`` by the auth middleware from the validated JWT) must
          be present, mapped, and its allowlist must contain ``bot_tag``. A
          missing/unparseable ``QNA_TENANT_BOT_TAG_MAP`` means no allowlist
          exists, so the request fails closed (and an operator-facing log fires
          so the misconfiguration is diagnosable). Any failure rejects via the
          structured error envelope (403 / ``UNAUTHORIZED``) with a generic
          message — fail closed, no search.
        - Enforcement explicitly OFF: returns immediately. Fully inert — the map
          is never parsed; the caller is responsible for scoping ``bot_tag``
          some other way (single-workspace deployments only).

    Args:
        request: Incoming request; ``request.state.tid`` carries the validated
            tenant id.
        bot_tag: The (already non-empty, stripped) ``bot_tag`` from the request
            body.

    Raises:
        HTTPException: 403 with the envelope contract on any binding failure.
            The caller never reaches the QnA pipeline on rejection.
    """
    if not is_tenant_binding_enforced():
        return

    tid = getattr(request.state, "tid", None)
    request_id = getattr(request.state, "request_id", None)

    # The trusted tid comes from the validated token, never the request body.
    # A token without a tid claim cannot be bound to an allowlist → fail closed.
    if not tid:
        logger.warning("[%s] tenant-binding: rejected request with no tid claim", request_id)
        _reject()

    tenant_map = _load_tenant_bot_tag_map()
    # An empty map while enforcement is ON is a deployment misconfiguration
    # ("refuse to serve / clear error if missing"): the map is REQUIRED. Surface
    # it as a distinct operator-facing error (no tenant data echoed) so the
    # fail-closed cause is diagnosable, then reject like any other failure.
    if not tenant_map:
        logger.error(
            "[%s] tenant-binding: enforcement is ON but QNA_TENANT_BOT_TAG_MAP is "
            "missing/empty/unparseable — refusing to serve. Configure the map or "
            "set QNA_ENFORCE_TENANT_BINDING=false for a single-workspace deployment.",
            request_id,
        )
        _reject()

    allowed = tenant_map.get(str(tid))
    # Unmapped tid, or bot_tag not in this tenant's allowlist → fail closed.
    if not allowed or bot_tag not in allowed:
        logger.warning("[%s] tenant-binding: bot_tag not permitted for tenant", request_id)
        _reject()


def _reject() -> None:
    """Raise the standard envelope rejection for a binding failure.

    Generic message — never echoes the bot_tag, tid, or allowlist contents.
    """
    raise_api_error(
        ApiErrorCode.UNAUTHORIZED,
        "Requested bot_tag is not permitted for this tenant",
        403,
    )
