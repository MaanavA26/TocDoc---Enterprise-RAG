"""Temporary admin token authentication for /admin/* routes.

This is an interim measure documented in
`docs/architect_phase_2/01_ADMIN_API_SPEC.md` (Security requirements section).
A static shared-secret token is read from `ADMIN_API_TOKEN` env var and
compared against the `X-Admin-Token` request header using constant-time
comparison.

A future PR should replace this with the same Azure AD JWT mechanism the
QnA service uses (see `services/qna/src/core/auth.py`). Until then:

- The dependency is applied ONLY to `/admin/*` routes via FastAPI `Depends`.
- Existing `/upload` and `/health` endpoints remain entirely unauthenticated
  (consistent with prior behavior).
- The env var is checked at request time, NOT import time, so importing this
  module never fails. A request with the env unset gets a 503 (server
  misconfigured) rather than silently bypassing auth.
"""

import os
import secrets
from typing import Annotated, Optional

from fastapi import Header, HTTPException, status


def _get_admin_token() -> Optional[str]:
    """Read `ADMIN_API_TOKEN` from env. Returns None if unset."""
    return os.getenv("ADMIN_API_TOKEN")


async def require_admin_token(
    x_admin_token: Annotated[Optional[str], Header(alias="X-Admin-Token")] = None,
) -> None:
    """FastAPI dependency: enforce a valid X-Admin-Token header.

    Raises:
        HTTPException(503): if the server has no `ADMIN_API_TOKEN` configured.
            We refuse rather than allow auth bypass.
        HTTPException(401): if the header is missing or the value is wrong.
            The error detail does NOT distinguish between "missing" and "wrong"
            to avoid token enumeration via probing.
    """
    expected = _get_admin_token()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin API not configured",
        )

    if not x_admin_token or not secrets.compare_digest(
        x_admin_token.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing admin token",
        )
