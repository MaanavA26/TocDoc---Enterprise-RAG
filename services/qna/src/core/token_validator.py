"""
token_validator.py — Azure AD RS256 JWT validation with JWKS caching.

Fetches signing keys from Azure AD's JWKS endpoint, caches them with a
configurable TTL, and validates incoming JWTs with full cryptographic
signature verification.
"""

import time
import json
import logging
import asyncio
from functools import partial
from typing import Optional
from urllib import request as urllib_request
from urllib.error import URLError

from jose import jwt, JWTError, ExpiredSignatureError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class TokenValidationError(Exception):
    """Raised when a JWT cannot be validated. Carries an HTTP status code."""

    def __init__(self, message: str, status_code: int = 401):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


# ---------------------------------------------------------------------------
# JWKS cache
# ---------------------------------------------------------------------------

# {tenant_id: {"keys": [...], "fetched_at": float}}
_jwks_cache: dict = {}
_CACHE_TTL = 3600  # seconds


def _fetch_jwks_sync(url: str) -> list:
    """Fetch JWKS keys synchronously using urllib (stdlib, no extra deps)."""
    try:
        with urllib_request.urlopen(url, timeout=10) as resp:
            body = resp.read()
        data = json.loads(body)
        keys = data.get("keys", [])
        if not keys:
            raise TokenValidationError("JWKS response contained no keys", status_code=503)
        return keys
    except TokenValidationError:
        raise
    except URLError as exc:
        logger.error("Failed to fetch JWKS from %s: %s", url, exc)
        raise TokenValidationError(
            "Unable to retrieve Azure AD signing keys", status_code=503
        ) from exc
    except Exception as exc:
        logger.error("Unexpected error fetching JWKS from %s: %s", url, exc)
        raise TokenValidationError(
            "Unable to retrieve Azure AD signing keys", status_code=503
        ) from exc


async def _get_jwks(tenant_id: str) -> list:
    """
    Return Azure AD JWKS keys for the given tenant, using a TTL cache.

    Keys are fetched from:
        https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys

    The result is cached for _CACHE_TTL seconds to avoid per-request fetches
    and to handle key rotation gracefully.
    """
    now = time.time()
    cached = _jwks_cache.get(tenant_id)
    if cached and (now - cached["fetched_at"]) < _CACHE_TTL:
        return cached["keys"]

    jwks_url = (
        f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
    )
    logger.info("Fetching JWKS from %s", jwks_url)

    # Run the synchronous urllib call in a thread executor so we don't block
    # the event loop.
    loop = asyncio.get_running_loop()
    keys = await loop.run_in_executor(None, partial(_fetch_jwks_sync, jwks_url))

    _jwks_cache[tenant_id] = {"keys": keys, "fetched_at": now}
    return keys


# ---------------------------------------------------------------------------
# Public validation function
# ---------------------------------------------------------------------------

async def validate_token(token: str, tenant_id: str, audience: str) -> dict:
    """
    Validate an Azure AD JWT with full RS256 signature verification.

    Steps:
        1. Read the ``kid`` (key ID) from the unverified token header.
        2. Fetch (or use cached) JWKS keys for the tenant.
        3. Find the matching key by ``kid``.
        4. Decode and verify the token using python-jose with RS256.
        5. Return the verified claims dict.

    Args:
        token:     Raw JWT string (without the "Bearer " prefix).
        tenant_id: Azure AD tenant ID (used to build the JWKS URL and iss check).
        audience:  Expected ``aud`` claim value.

    Returns:
        dict: Verified JWT claims.

    Raises:
        TokenValidationError: On any validation failure (invalid signature,
            expired token, wrong issuer/audience, key not found, etc.).
    """
    # ------------------------------------------------------------------
    # Step 1: Read the kid from the unverified header so we can look up
    # the right key.  This read is intentionally unverified — it is only
    # used to select the public key; the actual verification happens in
    # jwt.decode below.
    # ------------------------------------------------------------------
    try:
        unverified_header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise TokenValidationError("Malformed token header") from exc

    kid = unverified_header.get("kid")
    if not kid:
        raise TokenValidationError("Token header missing 'kid' field")

    # ------------------------------------------------------------------
    # Step 2 & 3: Fetch keys and find the matching key.
    # ------------------------------------------------------------------
    try:
        jwks_keys = await _get_jwks(tenant_id)
    except TokenValidationError:
        raise

    matching_key: Optional[dict] = None
    for key in jwks_keys:
        if key.get("kid") == kid:
            matching_key = key
            break

    if matching_key is None:
        # The key might have been rotated since we cached — clear cache and retry once.
        logger.info("kid '%s' not found in cache; refreshing JWKS", kid)
        _jwks_cache.pop(tenant_id, None)
        try:
            jwks_keys = await _get_jwks(tenant_id)
        except TokenValidationError:
            raise
        for key in jwks_keys:
            if key.get("kid") == kid:
                matching_key = key
                break

    if matching_key is None:
        raise TokenValidationError(
            "No matching signing key found for token 'kid'"
        )

    # ------------------------------------------------------------------
    # Step 4: Decode and verify with python-jose.  Signature verification
    # is ON by default (the opposite of the old placeholder).
    # ------------------------------------------------------------------
    expected_issuer = f"https://sts.windows.net/{tenant_id}/"

    try:
        claims = jwt.decode(
            token,
            matching_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=expected_issuer,
            options={"leeway": 10},  # 10-second clock skew tolerance
        )
    except ExpiredSignatureError as exc:
        raise TokenValidationError("Token has expired") from exc
    except JWTError as exc:
        # Covers wrong audience, wrong issuer, invalid signature, etc.
        raise TokenValidationError(f"Token validation failed: {exc}") from exc
    except Exception as exc:
        logger.error("Unexpected error during JWT decode: %s", exc)
        raise TokenValidationError("Token validation error") from exc

    return claims
