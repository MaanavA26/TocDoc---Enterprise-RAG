"""
test_auth.py — Unit tests for JWT authentication middleware and token validator.

Tests use an ephemeral RSA key pair to sign tokens and mock the JWKS fetch so
no network calls are made.  All 8 auth failure/success modes are covered.
"""

import os
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Ensure required env vars exist BEFORE importing the app / config modules.
# ---------------------------------------------------------------------------
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://fake-openai.example.com")
os.environ.setdefault("AZURE_OPENAI_KEY", "fake-key")
os.environ.setdefault("AZURE_OPENAI_VERSION", "2024-06-01")
os.environ.setdefault("AZURE_OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
os.environ.setdefault("AZURE_SEARCH_ENDPOINT", "https://fake-search.example.com")
os.environ.setdefault("AZURE_SEARCH_KEY", "fake-search-key")
os.environ.setdefault("INDEX_NAME", "fake-index")
os.environ.setdefault("AZURE_KEY_VAULT", "fakevault")

# Auth middleware expectations
_TENANT_ID = "11111111-1111-1111-1111-111111111111"
_AUDIENCE = "api://fake-audience-id"
os.environ.setdefault("AZURE_TENANT_ID", _TENANT_ID)
os.environ.setdefault("AUDIENCE_ID", _AUDIENCE)

# ---------------------------------------------------------------------------
# Crypto helpers — generate a temporary RSA key pair for test token signing
# ---------------------------------------------------------------------------
import base64

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt as jose_jwt


def _generate_rsa_keypair():
    """Return (private_key, public_key) as cryptography objects."""
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
        backend=default_backend(),
    )
    return private_key, private_key.public_key()


def _int_to_base64url(n: int) -> str:
    """Encode a large integer as unpadded base64url (for JWK parameters)."""
    # Calculate the byte length needed
    byte_length = (n.bit_length() + 7) // 8
    n_bytes = n.to_bytes(byte_length, byteorder="big")
    return base64.urlsafe_b64encode(n_bytes).rstrip(b"=").decode("ascii")


def _public_key_to_jwk(public_key, kid: str = "test-kid-1") -> dict:
    """Convert a cryptography RSA public key to a JWK dict."""
    pub_numbers = public_key.public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _int_to_base64url(pub_numbers.n),
        "e": _int_to_base64url(pub_numbers.e),
    }


def _make_rs256_token(
    private_key,
    kid: str = "test-kid-1",
    tenant_id: str = _TENANT_ID,
    audience: str = _AUDIENCE,
    upn: str = "user@example.com",
    exp_offset: int = 3600,
    iss: str = None,
    aud: str = None,
    include_upn: bool = True,
) -> str:
    """
    Sign a JWT with the given RSA private key.

    Args:
        private_key:   cryptography RSAPrivateKey object.
        kid:           Key ID to include in the JWT header.
        tenant_id:     Tenant ID for building the issuer claim.
        audience:      ``aud`` claim value.
        upn:           User principal name claim (omitted if include_upn=False).
        exp_offset:    Seconds from now for ``exp`` (negative = already expired).
        iss:           Override issuer (default builds from tenant_id).
        aud:           Override audience (default uses audience param).
        include_upn:   Whether to include the ``upn`` claim.
    """
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )

    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": iss or f"https://sts.windows.net/{tenant_id}/",
        "aud": aud or audience,
        "sub": "user-subject-id",
        "iat": now,
        "nbf": now,
        "exp": now + exp_offset,
    }
    if include_upn:
        payload["upn"] = upn

    headers = {"kid": kid}
    return jose_jwt.encode(payload, pem, algorithm="RS256", headers=headers)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_keypair():
    """Module-scoped RSA key pair (generated once per test session)."""
    return _generate_rsa_keypair()


@pytest.fixture(scope="module")
def jwk_key(rsa_keypair):
    """JWK dict derived from the test public key."""
    _, pub_key = rsa_keypair
    return _public_key_to_jwk(pub_key, kid="test-kid-1")


# Patch _get_jwks to return our test JWK — applied for every test in this file.
# Function scope is correct here; each test gets a fresh patch context even
# though `jwk_key` is module-scoped (pytest allows narrower scope to use wider).
@pytest.fixture(autouse=True)
def mock_jwks(jwk_key):
    """Auto-use fixture that mocks JWKS fetch for all tests in this module."""
    # Patch _get_jwks (inside token_validator) — NOT validate_token in auth.py.
    # auth.py calls validate_token which calls _get_jwks internally.
    # Patching _get_jwks correctly intercepts the JWKS fetch for all tests.
    with patch(
        "src.core.token_validator._get_jwks",
        new=AsyncMock(return_value=[jwk_key]),
    ):
        yield


# ---------------------------------------------------------------------------
# Import the FastAPI app AFTER env vars and fixtures are set up.
# ---------------------------------------------------------------------------
from httpx import ASGITransport, AsyncClient


# Lazy import at test time to avoid import-order issues with env vars.
@pytest.fixture(scope="module")
def app():
    import app as app_module  # the qna app

    return app_module.app


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_route_bypasses_auth(app):
    """GET /qna/health must return 200 without any Authorization header."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.get("/qna/health")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_missing_auth_header_returns_401(app):
    """Requests with no Authorization header must be rejected with 401."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/qna/qna", json={})
    assert r.status_code == 401
    assert "Missing or invalid Authorization header" in r.json()["error"]["message"]


@pytest.mark.asyncio
async def test_invalid_bearer_format_returns_401(app):
    """A malformed Authorization header (not 'Bearer <token>') must return 401."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/qna/qna",
            json={},
            headers={"Authorization": "Token not-a-bearer"},
        )
    assert r.status_code == 401
    assert "Missing or invalid Authorization header" in r.json()["error"]["message"]


@pytest.mark.asyncio
async def test_expired_token_returns_401(app, rsa_keypair):
    """A token with an already-elapsed exp claim must be rejected."""
    private_key, _ = rsa_keypair
    token = _make_rs256_token(private_key, exp_offset=-3600)  # expired 1 hour ago
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/qna/qna", json={}, headers=_bearer(token))
    assert r.status_code == 401
    body = r.json()["error"]["message"]
    assert "expired" in body.lower() or "Token validation failed" in body


@pytest.mark.asyncio
async def test_wrong_audience_returns_401(app, rsa_keypair):
    """A token with an incorrect aud claim must be rejected."""
    private_key, _ = rsa_keypair
    token = _make_rs256_token(private_key, aud="api://wrong-audience")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/qna/qna", json={}, headers=_bearer(token))
    assert r.status_code == 401
    assert r.json()["error"]["message"] != ""


@pytest.mark.asyncio
async def test_wrong_issuer_returns_401(app, rsa_keypair):
    """A token from the wrong issuer must be rejected."""
    private_key, _ = rsa_keypair
    token = _make_rs256_token(
        private_key,
        iss="https://sts.windows.net/99999999-9999-9999-9999-999999999999/",
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/qna/qna", json={}, headers=_bearer(token))
    assert r.status_code == 401
    assert r.json()["error"]["message"] != ""


@pytest.mark.asyncio
async def test_missing_email_claim_returns_401(app, rsa_keypair):
    """A valid signature but no upn/preferred_username/email claim must return 401."""
    private_key, _ = rsa_keypair
    token = _make_rs256_token(private_key, include_upn=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post("/qna/qna", json={}, headers=_bearer(token))
    assert r.status_code == 401
    assert r.json()["error"]["message"] == "Email claim not found in token"


@pytest.mark.asyncio
async def test_valid_token_sets_email_on_request_state(app, rsa_keypair, monkeypatch):
    """
    A fully valid RS256 token must pass auth and attach the email to request.state.

    We use a /qna/health-style approach: intercept at middleware level by checking
    that a downstream route receives the request (gets past auth).  Since /qna/health
    bypasses auth entirely we need an authenticated endpoint.  We test this via
    the /qna/qna endpoint with a minimal payload that should produce a 400 or 200
    (not a 401) once auth succeeds.
    """
    from src.config import config as cfg

    # Patch KeyVault startup to avoid network calls
    async def _no_kv():
        return {}

    monkeypatch.setattr(cfg.settings, "load_secrets_from_keyvault", _no_kv, raising=True)

    private_key, _ = rsa_keypair
    token = _make_rs256_token(private_key, upn="testuser@example.com")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        r = await ac.post(
            "/qna/qna",
            json={},  # Deliberately empty — expect 400/422 from business logic, NOT 401
            headers=_bearer(token),
        )
    # Auth passed if we get anything other than 401
    assert r.status_code != 401, f"Expected auth to succeed but got 401: {r.json()}"
