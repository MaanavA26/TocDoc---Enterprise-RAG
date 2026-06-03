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
from src.core.token_validator import TokenValidationError, validate_token

logger = logging.getLogger(__name__)


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
            return build_error_response(
                request,
                code=ApiErrorCode.INTERNAL_ERROR,
                message="Authentication error",
                status_code=500,
            )

        return await call_next(request)
