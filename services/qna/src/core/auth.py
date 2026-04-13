import jwt  # NOTE: Imported but shadowed by `from jose import jwt` below. Kept to preserve imports.
import logging
from fastapi import Request
from fastapi.responses import JSONResponse
from jose import jwt, JWTError  # This `jwt` shadows the PyJWT import above (intentional per constraints).
from src.config.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Expected Azure AD values (configured via environment / Settings)
# ---------------------------------------------------------------------------
EXPECTED_ISS = f"https://sts.windows.net/{settings.AZURE_TENANT_ID}/"
EXPECTED_AUD = settings.AUDIENCE_ID


class AuthUtils:
    """
    Authentication utilities.

    Currently exposes a FastAPI middleware that:
      - Skips auth for `/health`.
      - Expects an `Authorization: Bearer <token>` header.
      - Decodes the JWT **without signature validation** (as implemented),
        while still checking issuer/audience via the provided arguments.
      - Extracts a user email from common claim names and attaches it to
        `request.state.email`.

    NOTE:
        - Signature verification is explicitly disabled (`verify_signature=False`).
          See the suggestions section if you plan to enable proper validation.
    """

    # 🔹 Middleware
    async def auth_middleware(request: Request, call_next):
        """
        FastAPI HTTP middleware for JWT-based authentication.

        Behavior (unaltered):
            - Allows CORS preflight/health routes to pass when path is `/health`.
            - Validates presence and shape of Authorization header.
            - Decodes token with `jose.jwt.decode` and `verify_signature=False`.
            - Extracts email from `upn` / `preferred_username` / `email`.
            - Attaches `request.state.email` for downstream usage.

        Returns:
            starlette.responses.Response: The next handler's response or a JSON 401/500.
        """
        path = request.url.path or "/"

        # ---- Public routes / methods
        if (
            request.method == "OPTIONS"                      # CORS preflight
            or path.endswith("/qna/health")                      # /health, /qna/health, /api/qna/health, etc.
            or path in {"/docs", "/redoc", "/openapi.json"}  # (optional) swagger assets
        ):
            return await call_next(request)

        # Authorization header presence/shape check
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid Authorization header"},
            )

        token = auth_header.split(" ")[1]

        try:
            # Decode without signature validation (kept as-is by design).
            decoded = jwt.decode(
                token,
                key="",  # No key for now, as signature validation is skipped
                options={"verify_signature": False, "leeway": 10},
                issuer=EXPECTED_ISS,
                audience=EXPECTED_AUD,
                algorithms=["RS256"],
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

        except JWTError as e:
            logger.error(f"Token decoding failed: {str(e)}")
            return JSONResponse(status_code=401, content={"detail": "Invalid token"})
        except Exception as e:
            logger.error(f"Auth middleware error: {str(e)}")
            return JSONResponse(status_code=500, content={"detail": "Authentication error"})

        return await call_next(request)