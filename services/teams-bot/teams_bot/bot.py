"""The Bot Framework activity handler for the TocDoc Teams adapter.

Per-turn flow (the inbound Bot Framework JWT is validated by the adapter layer
in ``app.py`` *before* this handler runs — that is the trust boundary that makes
``channelData.tenant.id`` trustworthy):

1. Read the verified ``channelData.tenant.id`` and the user's message text.
   These are kept in separate variables; the text is treated as the natural-
   language query ONLY and is never inspected for a bot_tag.
2. Derive ``bot_tag`` server-side from the tenant id via the configured map
   (:func:`teams_bot.identity.resolve_bot_tag`). Fail closed on an unknown
   tenant — no QnA call is made.
3. Acquire a QnA-valid user token via the injected :class:`TokenProvider`
   (OBO seam).
4. Call ``POST /qna`` through the injected QnA client with the derived
   ``bot_tag`` + the user's text as the bot turn.
5. Render ``{answer, citation}`` as an adaptive card, or map a P0-6 ``ApiError``
   to a friendly card surfacing the ``request_id``.

Dependency injection: both the QnA client and the token provider are injected
so tests can supply fakes and assert on the call arguments without any live
Teams/Azure.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping

from botbuilder.core import ActivityHandler, CardFactory, MessageFactory, TurnContext
from tocdoc_sdk import ApiError, QnAAnswer

from .cards import render_answer_card, render_error_card
from .identity import IdentityResolutionError, resolve_bot_tag
from .tokens import TokenAcquisitionError, TokenProvider

logger = logging.getLogger("teams_bot.bot")

# Generic, user-safe messages. Internal detail (stack traces, tenant ids) is
# never surfaced to the Teams user.
_MSG_UNKNOWN_TENANT = (
    "This workspace isn't set up to answer questions yet. Please contact your administrator."
)
_MSG_AUTH = "I couldn't verify your access just now. Please try again shortly."
_MSG_GENERIC = "Something went wrong answering that. Please try again."


class QnAClientProtocol:
    """Structural type the bot depends on (a subset of ``tocdoc_sdk.TocDocClient``).

    Defined for documentation/readability; the bot duck-types on ``ask`` so any
    object with a matching signature (the real SDK client or a test fake) works.
    """

    def ask(
        self,
        *,
        session_id: str,
        bot_tag: str,
        fr_tag: str,
        query: str,
    ) -> QnAAnswer:  # pragma: no cover - protocol illustration
        ...


class TocDocTeamsBot(ActivityHandler):
    """Bot Framework handler that bridges Teams turns to the QnA API."""

    def __init__(
        self,
        *,
        qna_client: QnAClientProtocol,
        token_provider: TokenProvider,
        tenant_bot_tag_map: Mapping[str, str],
        fr_tag: str,
        client_factory: Callable[[str], QnAClientProtocol] | None = None,
    ) -> None:
        """Create the handler.

        Args:
            qna_client: A QnA client used when ``client_factory`` is not given
                (e.g. tests, or a deployment that supplies the bearer token out
                of band).
            token_provider: Injected token provider (the OBO seam).
            tenant_bot_tag_map: Admin-configured ``{tenant_id: bot_tag}`` map.
            fr_tag: The default ``fr_tag`` for QnA requests (config-sourced,
                never user-supplied).
            client_factory: Optional factory ``(bearer_token) -> client`` used to
                build a per-turn client carrying the OBO token. When omitted the
                static ``qna_client`` is reused (its bearer is set elsewhere).
        """
        super().__init__()
        self._qna_client = qna_client
        self._token_provider = token_provider
        self._tenant_bot_tag_map = dict(tenant_bot_tag_map)
        self._fr_tag = fr_tag
        self._client_factory = client_factory

    @staticmethod
    def _tenant_id(turn_context: TurnContext) -> str:
        """Extract ``channelData.tenant.id`` from the (already-verified) activity."""
        channel_data = turn_context.activity.channel_data or {}
        tenant = channel_data.get("tenant") if isinstance(channel_data, dict) else None
        if isinstance(tenant, dict):
            return str(tenant.get("id") or "")
        return ""

    @staticmethod
    def _user_sso_token(turn_context: TurnContext) -> str | None:
        """Best-effort read of a Teams SSO token attached to the turn, if any.

        The real OBO provider exchanges this. It is never logged.
        """
        channel_data = turn_context.activity.channel_data or {}
        if isinstance(channel_data, dict):
            token = channel_data.get("user_sso_token")
            if isinstance(token, str):
                return token
        return None

    async def on_message_activity(self, turn_context: TurnContext) -> None:
        """Handle one inbound message turn; always replies with a card."""
        tenant_id = self._tenant_id(turn_context)
        # The user's text is the query ONLY. It is never parsed for a bot_tag.
        query = (turn_context.activity.text or "").strip()

        # 1. Derive bot_tag server-side. Fail closed on unknown/invalid tenant —
        #    no QnA call is made in that case.
        try:
            bot_tag = resolve_bot_tag(tenant_id, self._tenant_bot_tag_map)
        except IdentityResolutionError:
            logger.warning("Rejected turn: no valid bot_tag mapping for tenant.")
            await self._reply_error(turn_context, _MSG_UNKNOWN_TENANT, request_id=None)
            return

        # 2. Acquire a QnA-valid user token (OBO seam).
        try:
            token = self._token_provider.get_qna_token(user_token=self._user_sso_token(turn_context))
        except TokenAcquisitionError:
            logger.warning("Rejected turn: could not acquire QnA token.")
            await self._reply_error(turn_context, _MSG_AUTH, request_id=None)
            return

        client = self._client_factory(token) if self._client_factory else self._qna_client
        session_id = self._session_id(turn_context)

        # 3. Call QnA off the event loop (the SDK client is synchronous).
        try:
            answer = await self._ask(client, session_id=session_id, bot_tag=bot_tag, query=query)
        except ApiError as exc:
            logger.warning(
                "QnA returned an error envelope.",
                extra={"qna_request_id": exc.request_id, "status_code": exc.status_code},
            )
            await self._reply_error(turn_context, _MSG_GENERIC, request_id=exc.request_id)
            return
        except Exception:
            logger.exception("Unexpected error calling QnA.")
            await self._reply_error(turn_context, _MSG_GENERIC, request_id=None)
            return

        # 4. Render the answer as an adaptive card.
        card = render_answer_card(answer.answer, answer.citations)
        await turn_context.send_activity(MessageFactory.attachment(CardFactory.adaptive_card(card)))

    async def _ask(
        self, client: QnAClientProtocol, *, session_id: str, bot_tag: str, query: str
    ) -> QnAAnswer:
        """Invoke the synchronous QnA client without blocking the event loop.

        ``fr_tag`` is the config-sourced default held on the instance — never
        derived from user input. ``query`` is the user's message text, sent as
        the (single) bot turn.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: client.ask(
                session_id=session_id,
                bot_tag=bot_tag,
                fr_tag=self._fr_tag,
                query=query,
            ),
        )

    async def _reply_error(self, turn_context: TurnContext, message: str, *, request_id: str | None) -> None:
        card = render_error_card(message, request_id)
        await turn_context.send_activity(MessageFactory.attachment(CardFactory.adaptive_card(card)))

    @staticmethod
    def _session_id(turn_context: TurnContext) -> str:
        """Use the service-stamped conversation id as the QnA session id."""
        conversation = turn_context.activity.conversation
        if conversation is not None and getattr(conversation, "id", None):
            return str(conversation.id)
        return "teams-session"
