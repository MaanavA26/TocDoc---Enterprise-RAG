"""Pure request-building and response-validation helpers for the load-test suite.

This module is deliberately free of any Locust or network dependency: every
function here is a pure transform over plain dicts / objects so it can be
unit-tested in CI without a running Locust worker or a reachable deployment.

The Locust user classes in :mod:`locustfile` import these helpers and call them
inside ``catch_response=True`` blocks; the validators below decide whether a
sampled response counts as a success or a failure.

Nothing in this module reads environment variables or performs I/O at import
time, so ``import helpers`` is always safe (CI imports it directly).
"""

from __future__ import annotations

from typing import Any

# bot_tag charset mirrors the server-side contract
# (services/ingestion/admin/routes.py BOT_TAG_PATTERN and the qna pipeline's
# tenant-isolation guard). Kept here as documentation of the wire contract; the
# load test only ever sends operator-provided tags, never validates them.
BOT_TAG_MAX_LEN = 128


def bearer_header(token: str | None) -> dict[str, str]:
    """Build an ``Authorization: Bearer <token>`` header dict.

    Args:
        token: The raw JWT (no ``Bearer`` prefix). May be ``None``/empty when
            the operator has not supplied a token; in that case an empty dict is
            returned so the caller sends an unauthenticated request (which the
            deployment will reject with 401 — useful for negative scenarios).

    Returns:
        A header dict suitable for merging into a request, or ``{}`` when no
        token is available.
    """
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def admin_header(admin_token: str | None) -> dict[str, str]:
    """Build an ``X-Admin-Token`` header dict for the admin / upload endpoints.

    Args:
        admin_token: The raw admin token. ``None``/empty yields an empty dict.

    Returns:
        A header dict, or ``{}`` when no admin token is available.
    """
    if not admin_token:
        return {}
    return {"X-Admin-Token": admin_token}


def build_qna_payload(
    question: str,
    bot_tag: str,
    fr_tag: str,
    *,
    session_id: str,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the JSON body for ``POST /qna``.

    Mirrors ``services/qna/src/utils/util.Payload``: ``{session_id, bot, fr_tag,
    bot_tag}`` where ``bot`` is an ordered list of conversation turns (oldest →
    newest), each turn ``{user_query, bot_response}``.

    Args:
        question: The latest user question; appended as the newest turn.
        bot_tag: Tenant/workspace identifier forwarded to the search layer.
        fr_tag: Feature/retrieval tag (e.g. ``read``/``layout``).
        session_id: Correlation/session identifier.
        history: Optional prior turns (each a dict with at least ``user_query``).
            The current ``question`` is always appended as the final turn.

    Returns:
        A JSON-serializable dict matching the server's ``Payload`` schema.
    """
    turns: list[dict[str, Any]] = list(history or [])
    turns.append({"user_query": question, "bot_response": None})
    return {
        "session_id": session_id,
        "bot": turns,
        "fr_tag": fr_tag,
        "bot_tag": bot_tag,
    }


def build_admin_params(bot_tag: str) -> dict[str, str]:
    """Build the query-string params shared by the read-only admin endpoints.

    The admin list/stats endpoints take a ``bot_tag`` query parameter
    (see ``services/ingestion/admin/routes.py``).

    Args:
        bot_tag: Tenant/workspace identifier.

    Returns:
        A params dict, e.g. ``{"bot_tag": "demo"}``.
    """
    return {"bot_tag": bot_tag}


def build_upload_params(bot_tag: str, filepath: str, fr_mode: str) -> dict[str, str]:
    """Build the query-string params for ``POST /upload``.

    The upload endpoint takes ``bot_tag``, ``filepath`` and ``fr_mode`` as query
    parameters (see ``services/ingestion/app.upload_file``).

    Args:
        bot_tag: Tenant/workspace identifier.
        filepath: Server-side absolute file or folder path.
        fr_mode: Feature/retrieval mode tag.

    Returns:
        A params dict for the upload request.
    """
    return {"bot_tag": bot_tag, "filepath": filepath, "fr_mode": fr_mode}


class ResponseLike:
    """Structural type for the subset of a response object the validators use.

    Both ``requests.Response`` and Locust's response wrapper expose
    ``status_code``, ``.json()`` and ``.text``; the validators only touch those,
    so unit tests can pass a tiny stand-in instead of a real HTTP response.
    """

    status_code: int

    def json(self) -> Any:  # pragma: no cover - structural only
        ...

    @property
    def text(self) -> str:  # pragma: no cover - structural only
        ...


def validate_qna_response(response: Any) -> tuple[bool, str | None]:
    """Validate a ``POST /qna`` response.

    A response is considered a success only when the status is 200 **and** the
    JSON body carries a non-empty ``answer`` string (the public success contract
    is ``{answer, citation}`` — see ``QnASuccessResponse``).

    Args:
        response: Any object exposing ``status_code`` and ``.json()`` (a real
            HTTP response or a test stand-in).

    Returns:
        ``(ok, reason)`` where ``ok`` is ``True`` on success and ``reason`` is a
        short human-readable failure description otherwise (``None`` on success).
    """
    status = getattr(response, "status_code", None)
    if status != 200:
        return False, f"unexpected status {status}"
    try:
        body = response.json()
    except (ValueError, TypeError):
        return False, "response body was not valid JSON"
    if not isinstance(body, dict):
        return False, "response body was not a JSON object"
    answer = body.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        return False, "missing or empty 'answer' field"
    return True, None


def validate_admin_list_response(response: Any) -> tuple[bool, str | None]:
    """Validate a read-only admin list/stats response.

    Success is a 200 whose JSON body is either a list (documents) or a dict
    (stats / single document). Anything else — non-200, non-JSON, or a scalar
    body — is a failure.

    Args:
        response: Any object exposing ``status_code`` and ``.json()``.

    Returns:
        ``(ok, reason)`` as in :func:`validate_qna_response`.
    """
    status = getattr(response, "status_code", None)
    if status != 200:
        return False, f"unexpected status {status}"
    try:
        body = response.json()
    except (ValueError, TypeError):
        return False, "response body was not valid JSON"
    if not isinstance(body, (list, dict)):
        return False, "expected a JSON list or object"
    return True, None


def validate_accepted_response(
    response: Any,
    *,
    ok_statuses: tuple[int, ...] = (200, 201, 202),
) -> tuple[bool, str | None]:
    """Validate a write/upload response by status code only.

    Upload responses vary (success / degraded / accepted) so the body is not
    asserted; only the status class is checked. ``429`` (rate-limited) is treated
    as a **non-failure** for load-test purposes — back-pressure is expected
    behaviour under load and should not pollute the error rate — but it is
    reported via the reason string so it can be surfaced separately if desired.

    Args:
        response: Any object exposing ``status_code``.
        ok_statuses: Status codes considered a clean success.

    Returns:
        ``(ok, reason)``. ``ok`` is ``True`` for ``ok_statuses`` and for ``429``
        (with a ``"rate-limited"`` reason); ``False`` otherwise.
    """
    status = getattr(response, "status_code", None)
    if status in ok_statuses:
        return True, None
    if status == 429:
        return True, "rate-limited (back-pressure, not counted as failure)"
    return False, f"unexpected status {status}"
