"""Outbound token acquisition seam (Teams SSO -> On-Behalf-Of exchange).

The adapter must call ``POST /qna`` with a genuine Azure AD **user** token
whose ``aud == AUDIENCE_ID`` (the QnA API app registration), obtained via the
On-Behalf-Of (OBO) flow. OBO is mandatory: it preserves the per-user
``upn``/``preferred_username`` claim that the QnA P0-1 middleware requires
(an app-only token has no email claim and is rejected 401).

Wiring a real OBO exchange requires live Azure (a bot app registration, the
OBO client credentials, and a reachable AAD token endpoint), which is a
*deployment* step and cannot be exercised here. So token acquisition lives
behind a small, swappable interface:

- :class:`TokenProvider` — the protocol the bot depends on.
- :class:`OnBehalfOfTokenProvider` — the production implementation seam. It is
  deliberately a stub that raises until wired, so an unconfigured deployment
  fails *loudly* rather than silently sending no token.
- :class:`StaticTokenProvider` — a trivial provider used by tests (and by
  local development against a manually-minted token).

Tests inject a fake provider; nothing here logs token material.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TokenProvider(Protocol):
    """Supplies an Azure AD bearer token for the QnA API for a given turn.

    Implementations must return a token whose ``aud`` is the QnA API app
    registration (``AUDIENCE_ID``) and whose ``iss`` is the customer tenant's
    v1/v2 issuer — i.e. a token the unchanged QnA middleware will accept.
    """

    def get_qna_token(self, *, user_token: str | None) -> str:
        """Return a QnA-valid bearer token.

        Args:
            user_token: The Teams SSO token for the current user, if available
                on the turn. The production provider exchanges this via OBO;
                test/static providers may ignore it.

        Returns:
            A bearer token string (never logged).
        """
        ...


class TokenAcquisitionError(Exception):
    """Raised when a QnA-valid token cannot be obtained for the turn."""


class StaticTokenProvider:
    """Returns a fixed, pre-supplied bearer token.

    Used by tests (with a fake token) and for local development against a
    manually-minted user token. NOT for production multi-user use: it does not
    perform a per-user OBO exchange, so every turn would carry the same
    identity.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def get_qna_token(self, *, user_token: str | None = None) -> str:  # noqa: ARG002
        return self._token


class OnBehalfOfTokenProvider:
    """Production OBO provider seam (live Azure wiring deferred).

    The real implementation performs an OBO exchange (``grant_type=
    urn:ietf:params:oauth:grant-type:jwt-bearer`` via MSAL
    ``acquire_token_on_behalf_of``) requesting scope
    ``api://<qna-app-id>/.default`` so the resulting token's ``aud`` is the QnA
    API app registration — **not** the bot's own app id. See the README's "OBO
    wiring" section for the exact deployment steps.

    This class is intentionally a stub: it raises until wired so a half-
    configured deployment fails loudly instead of sending no/invalid tokens.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        qna_scope: str,
    ) -> None:
        self._tenant_id = tenant_id
        self._client_id = client_id
        # Held only in memory, never logged. Sourced from a secret store at
        # deploy time, never committed.
        self._client_secret = client_secret
        self._qna_scope = qna_scope

    def get_qna_token(self, *, user_token: str | None) -> str:
        # Deferred: requires live Azure (AAD token endpoint + a registered bot
        # app + OBO consent). Wiring is a documented deployment step. Raise so
        # the failure is loud and attributable rather than a silent 401.
        raise TokenAcquisitionError(
            "OnBehalfOfTokenProvider is a deployment seam; wire the live OBO "
            "exchange (see README) before enabling it."
        )
