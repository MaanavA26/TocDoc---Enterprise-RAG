import logging
from fastapi import Request
from fastapi.responses import JSONResponse
from src.config.config import settings
from src.core.token_validator import validate_token, TokenValidationError

logger = logging.getLogger(__name__)


class AuthUtils:
    """
    Authentication utilities.

    Exposes a FastAPI middleware that:
      - Skips auth for public routes (CORS preflight, /qna/health, swagger assets).
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
            - Allows CORS preflight/health routes to pass when path is ``/qna/health``.
            - Validates presence and shape of Authorization header.
            - Decodes and verifies the token via ``validate_token()`` using RS256
              against the Azure AD JWKS endpoint.
            - Extracts email from ``upn`` / ``preferred_username`` / ``email`` claims.
            - Attaches ``request.state.email`` for downstream usage.

        Returns:
            starlette.responses.Response: The next handler's response or a JSON
            401/500 error response.
        """
        path = request.url.path or "/"

        # ---- Public routes / methods ----------------------------------------
        if (
            request.method == "OPTIONS"                           # CORS preflight
            or path.endswith("/qna/health")                       # health endpoint
            or path in {"/docs", "/redoc", "/openapi.json"}      # swagger assets
        ):
            return await call_next(request)

        # ---- Authorization header presence/shape check ----------------------
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
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
            email = (
                decoded.get("upn")
                or decoded.get("preferred_username")
                or decoded.get("email")
            )
            if not email:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Email claim not found in token"},
                )

            # Attach email to request.state (downstream handlers can rely on it)
            request.state.email = email

        except TokenValidationError as e:
            return JSONResponse(
                status_code=e.status_code,
                content={"detail": e.message},
            )
        except Exception as e:
            logger.error("Auth middleware unexpected error: %s", str(e))
            return JSONResponse(
                status_code=500,
                content={"detail": "Authentication error"},
            )

        return await call_next(request)
