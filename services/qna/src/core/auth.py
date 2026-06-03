"""JWT authentication middleware for the QnA service.

Validates Azure AD-issued Bearer tokens (RS256, JWKS-backed) on every
authenticated request. Public routes (CORS preflight, `/health`,
Swagger assets) bypass auth.

Error contract (P0-6): every auth failure returns the standard
`ErrorEnvelope` shape via `build_error_response`, including
`X-Request-ID` in both the body and the response header. We do NOT raise
`HTTPException` from inside this middleware — that path is unsafe because
middleware exceptions may bypass FastAPI's HTTPException handler and fall
through to Starlette's `ServerErrorMiddleware` as a non-enveloped 500.
"""

import logging

from fastapi import Request

from src.config.config import settings
from src.core.errors import ApiErrorCode, build_error_response
from src.core.observability import log_event
from src.core.token_validator import TokenValidationError, validate_token

logger = logging.getLogger(__name__)


def _classify_token_failure(err: "TokenValidationError") -> str:
    """Map a TokenValidationError to a coarse, safe `failure_type` label.

    We classify from the signals the validator actually exposes
    (`status_code` + `message`). The token value itself is never inspected
    here and is NEVER logged. `invalid_audience` is intentionally NOT a
    separate label: python-jose collapses audience mismatches into the
    generic "Invalid token" JWTError path, so it is not separable without
    changing the validator's exception contract — those land in
    `invalid_token`.
    """
    if err.status_code == 503:
        return "jwks_unavailable"
    message = (err.message or "").lower()
    if "expired" in message:
        return "expired_token"
    if "issuer" in message:
        return "invalid_issuer"
    if "malformed" in message or "header" in message or "kid" in message:
        return "malformed_token"
    return "invalid_token"


class AuthUtils:
    """
    Authentication utilities.

    Exposes a FastAPI middleware that:
      - Skips auth for public routes (CORS preflight, `/health`, swagger assets).
      - Expects an ``Authorization: Bearer <token>`` header.
      - Validates the JWT cryptographically via Azure AD JWKS (RS256).
      - Extracts a user email from common claim names and attaches it to
        ``request.state.email``.
    """

    # Middleware
    async def auth_middleware(request: Request, call_next):
        """
        FastAPI HTTP middleware for JWT-based authentication.

        Behavior:
            - Allows CORS preflight/health routes to pass when path is ``/health``.
            - Validates presence and shape of Authorization header.
            - Decodes and verifies the token via ``validate_token()`` using RS256
              against the Azure AD JWKS endpoint.
            - Extracts email from ``upn`` / ``preferred_username`` / ``email`` claims.
            - Attaches ``request.state.email`` for downstream usage.

        Returns:
            starlette.responses.Response: The next handler's response, or an
            envelope-shaped error response (401/503/500) for auth failures.
        """
        path = request.url.path or "/"
        request_id = getattr(request.state, "request_id", None)

        # ---- Public routes / methods ----------------------------------------
        if (
            request.method == "OPTIONS"  # CORS preflight
            or path.endswith("/health")  # health endpoint
            or path in {"/docs", "/redoc", "/openapi.json"}  # swagger assets
        ):
            return await call_next(request)

        # ---- Authorization header presence/shape check ----------------------
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            log_event(
                logger,
                "auth_failure",
                request_id=request_id,
                level=logging.WARNING,
                failure_type="missing_token",
            )
            return build_error_response(
                request,
                code=ApiErrorCode.UNAUTHORIZED,
                message="Missing or invalid Authorization header",
                status_code=401,
            )

        token = auth_header.split(" ")[1]

        try:
            # Full RS256 signature validation against Azure AD JWKS.
            # NOTE: the token value is never logged.
            decoded = await validate_token(
                token,
                settings.AZURE_TENANT_ID,
                settings.AUDIENCE_ID,
            )

            # Extract email from common claim aliases
            email = decoded.get("upn") or decoded.get("preferred_username") or decoded.get("email")
            if not email:
                log_event(
                    logger,
                    "auth_failure",
                    request_id=request_id,
                    level=logging.WARNING,
                    failure_type="missing_email_claim",
                )
                return build_error_response(
                    request,
                    code=ApiErrorCode.UNAUTHORIZED,
                    message="Email claim not found in token",
                    status_code=401,
                )

            # Attach email to request.state (downstream handlers can rely on it)
            request.state.email = email

        except TokenValidationError as e:
            # `e.status_code` is 401 for client-side token issues (expired,
            # wrong audience, malformed) and 503 for JWKS-unavailable cases.
            # `e.message` is set by token_validator and is safe to surface.
            # The token value itself is NEVER logged — only a coarse label.
            log_event(
                logger,
                "auth_failure",
                request_id=request_id,
                level=logging.WARNING,
                failure_type=_classify_token_failure(e),
            )
            code = ApiErrorCode.UPSTREAM_UNAVAILABLE if e.status_code == 503 else ApiErrorCode.UNAUTHORIZED
            return build_error_response(
                request,
                code=code,
                message=e.message,
                status_code=e.status_code,
            )
        except Exception as e:
            # Never include `str(e)` in the response — log type only.
            logger.error("Auth middleware unexpected error: %s", type(e).__name__)
            log_event(
                logger,
                "auth_failure",
                request_id=request_id,
                level=logging.ERROR,
                failure_type="auth_internal_error",
            )
            return build_error_response(
                request,
                code=ApiErrorCode.INTERNAL_ERROR,
                message="Authentication error",
                status_code=500,
            )

        # Auth passed — emit success (no token, no email value beyond presence).
        log_event(
            logger,
            "auth_success",
            request_id=request_id,
        )
        return await call_next(request)
