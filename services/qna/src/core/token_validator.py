"""
token_validator.py — Azure AD RS256 JWT validation with JWKS caching.

Fetches signing keys from Azure AD's JWKS endpoint, caches them with a
configurable TTL, and validates incoming JWTs with full cryptographic
signature verification.
"""

import asyncio
import json
import logging
import time
from collections import OrderedDict
from functools import partial
from urllib import request as urllib_request
from urllib.error import URLError

from jose import ExpiredSignatureError, JWTError, jwt

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

# --- Unknown-`kid` refetch throttle + negative cache (M1) -------------------
# An attacker can present a syntactically valid JWT *header* carrying a bogus
# `kid` before any signature check runs. Without throttling, each such request
# forces a fresh outbound JWKS fetch to login.microsoftonline.com — a pre-auth
# auth-availability DoS with ~1:1 outbound amplification. To bound that:
#
#   * We only trigger an unknown-`kid` refresh if the cached JWKS is older than
#     `_UNKNOWN_KID_REFRESH_INTERVAL` (legitimate key rotation is rare, so a
#     short floor is safe and still picks up rotations quickly).
#   * We remember recently-seen unresolved `(tenant_id, kid)` pairs for
#     `_NEGATIVE_KID_TTL` and short-circuit them without any fetch.
#   * On refresh we update the cache in place (never `pop`), so a fetch failure
#     keeps serving the existing keys instead of evicting good state.
_UNKNOWN_KID_REFRESH_INTERVAL = 60.0  # seconds
_NEGATIVE_KID_TTL = 60.0  # seconds
# Hard cap on the negative cache so an attacker streaming endless DISTINCT bogus
# `kid`s cannot grow it without bound (the entries are attacker-controlled keys)
# — that would re-open the very memory-DoS this throttle exists to close. We
# evict the oldest entry (FIFO via OrderedDict) once the cap is hit. The TTL is
# short, so legitimate churn never approaches the cap.
_NEGATIVE_KID_MAX_ENTRIES = 1024
# {(tenant_id, kid): first_seen_unresolved_at} — insertion-ordered for FIFO evict.
_negative_kid_cache: "OrderedDict[tuple[str, str], float]" = OrderedDict()


def _record_unresolved_kid(tenant_id: str, kid: str) -> None:
    """Remember an unresolved ``(tenant_id, kid)`` for the negative-cache TTL,
    evicting the oldest entry when the bounded cache is full so the dict cannot
    grow without limit under an attacker-controlled flood of distinct kids."""
    key = (tenant_id, kid)
    # Refresh ordering if already present so it counts as recently seen.
    _negative_kid_cache.pop(key, None)
    _negative_kid_cache[key] = time.time()
    while len(_negative_kid_cache) > _NEGATIVE_KID_MAX_ENTRIES:
        _negative_kid_cache.popitem(last=False)  # evict oldest (FIFO)


def _reset_jwks_state() -> None:
    """Clear all module-level JWKS caches. Test-only helper so the throttle /
    negative-cache state does not leak across tests."""
    _jwks_cache.clear()
    _negative_kid_cache.clear()


def _fetch_jwks_sync(url: str) -> list:
    """Fetch JWKS keys synchronously using urllib (stdlib, no extra deps)."""
    # Defensive: signing keys are only ever fetched over HTTPS. Without this
    # guard urlopen would also accept file:// or other local schemes (bandit B310).
    if not url.lower().startswith("https://"):
        raise TokenValidationError("JWKS URL must use HTTPS", status_code=500)
    try:
        with urllib_request.urlopen(url, timeout=10) as resp:  # nosec B310 - https scheme enforced above; fixed Azure AD JWKS endpoint
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
        raise TokenValidationError("Unable to retrieve Azure AD signing keys", status_code=503) from exc
    except Exception as exc:
        logger.error("Unexpected error fetching JWKS from %s: %s", url, exc)
        raise TokenValidationError("Unable to retrieve Azure AD signing keys", status_code=503) from exc


def _find_key(jwks_keys: list, kid: str) -> dict | None:
    """Return the JWK whose ``kid`` matches, or ``None``."""
    for key in jwks_keys:
        if key.get("kid") == kid:
            return key
    return None


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

    jwks_url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
    logger.info("Fetching JWKS from %s", jwks_url)

    # Run the synchronous urllib call in a thread executor so we don't block
    # the event loop.
    loop = asyncio.get_running_loop()
    keys = await loop.run_in_executor(None, partial(_fetch_jwks_sync, jwks_url))

    _jwks_cache[tenant_id] = {"keys": keys, "fetched_at": now}
    return keys


async def _refresh_jwks_in_place(tenant_id: str) -> list:
    """Force a fresh JWKS fetch and update the cache **in place**.

    Unlike ``_get_jwks`` this ignores the TTL freshness short-circuit (the
    caller has already decided a refresh is warranted) but, critically, it does
    NOT evict the existing cache before fetching: on a fetch failure the prior
    keys remain cached so the service keeps validating already-known ``kid``s
    (M1 — refresh-in-place, not ``pop``).
    """
    jwks_url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
    logger.info("Refreshing JWKS from %s", jwks_url)
    loop = asyncio.get_running_loop()
    keys = await loop.run_in_executor(None, partial(_fetch_jwks_sync, jwks_url))
    _jwks_cache[tenant_id] = {"keys": keys, "fetched_at": time.time()}
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

    matching_key = _find_key(jwks_keys, kid)

    if matching_key is None:
        # Unknown kid. This is reachable pre-signature-check with only a bogus
        # token header, so the refetch is throttled to bound a pre-auth DoS (M1).
        now = time.time()

        # 1) Negative cache: a kid we recently failed to resolve is short-circuited
        #    with NO outbound fetch for a short TTL.
        neg_key = (tenant_id, kid)
        neg_seen = _negative_kid_cache.get(neg_key)
        if neg_seen is not None and (now - neg_seen) < _NEGATIVE_KID_TTL:
            raise TokenValidationError("No matching signing key found for token 'kid'")

        # 2) Refetch throttle: only refresh if the cached JWKS is older than the
        #    minimum interval. Legitimate key rotation is rare, so this still
        #    picks up rotated keys promptly while collapsing a flood of bogus-kid
        #    requests into at most one fetch per interval.
        cached = _jwks_cache.get(tenant_id)
        cache_age = (now - cached["fetched_at"]) if cached else None
        if cache_age is None or cache_age >= _UNKNOWN_KID_REFRESH_INTERVAL:
            logger.info("kid '%s' not found in cache; refreshing JWKS (throttled)", kid)
            try:
                # Refresh in place — keep serving existing keys on fetch failure.
                jwks_keys = await _refresh_jwks_in_place(tenant_id)
            except TokenValidationError:
                raise
            matching_key = _find_key(jwks_keys, kid)
        else:
            logger.info(
                "kid '%s' not found; skipping refetch (cache age %.1fs < %.0fs throttle)",
                kid,
                cache_age,
                _UNKNOWN_KID_REFRESH_INTERVAL,
            )

    if matching_key is None:
        # Remember this unresolved kid briefly so repeated probes don't re-fetch
        # (bounded + FIFO-evicted so the cache itself can't be a memory DoS).
        _record_unresolved_kid(tenant_id, kid)
        raise TokenValidationError("No matching signing key found for token 'kid'")

    # ------------------------------------------------------------------
    # Step 4: Decode and verify with python-jose.  Signature verification
    # is ON by default (the opposite of the old placeholder).
    # ------------------------------------------------------------------

    # Read unverified claims to discover which issuer format the token uses.
    # This is intentionally unverified — it's only used to pick the right
    # expected_issuer string; the actual iss validation happens in jwt.decode().
    try:
        unverified_claims = jwt.get_unverified_claims(token)
    except JWTError as exc:
        raise TokenValidationError("Malformed token claims") from exc

    token_iss = unverified_claims.get("iss", "")
    v1_issuer = f"https://sts.windows.net/{tenant_id}/"
    v2_issuer = f"https://login.microsoftonline.com/{tenant_id}/v2.0"

    if token_iss == v1_issuer:
        expected_issuer = v1_issuer
    elif token_iss == v2_issuer:
        expected_issuer = v2_issuer
    else:
        raise TokenValidationError("Invalid token issuer")

    try:
        claims = jwt.decode(
            token,
            matching_key,
            algorithms=["RS256"],
            audience=audience,
            issuer=expected_issuer,
            # leeway: 10-second clock skew tolerance.
            # require_aud: make audience verification MANDATORY (L-Q1) — reject a
            # signed token that omits the `aud` claim rather than skipping the
            # audience check. Azure AD always issues `aud`, so this only closes a
            # hypothetical aud-less-token gap with no impact on valid traffic.
            options={"leeway": 10, "require_aud": True},
        )
    except ExpiredSignatureError as exc:
        raise TokenValidationError("Token has expired") from exc
    except JWTError as exc:
        logger.warning("JWT decode failed: %s", exc)
        raise TokenValidationError("Invalid token") from exc
    except Exception as exc:
        logger.error("Unexpected error during JWT decode: %s", exc)
        raise TokenValidationError("Token validation error") from exc

    return claims
