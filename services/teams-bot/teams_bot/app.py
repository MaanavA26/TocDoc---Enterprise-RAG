"""aiohttp host + Bot Framework messaging endpoint for the Teams adapter.

This module wires the runtime: it builds the ``BotFrameworkAdapter`` (which
performs the **inbound Bot Framework JWT validation** — the adapter's trust
boundary), constructs the :class:`TocDocTeamsBot` with its injected QnA client
and token provider, and exposes ``POST /api/messages``.

The trust boundary
------------------
``BotFrameworkAdapter.process_activity(body, auth_header, logic)`` validates the
inbound Bot Framework JWT (issuer ``https://api.botframework.com``, audience =
the bot's ``MicrosoftAppId``) *before* invoking ``logic``. The bot's
``on_message_activity`` only runs for activities that passed that check, which
is what makes ``channelData.tenant.id`` trustworthy and the derived ``bot_tag``
unspoofable. An activity with a missing/invalid auth header is rejected here
and never reaches the handler.

Building this host requires bot app-registration credentials and (for real use)
the OBO provider + a reachable QnA service — all deployment-time concerns. The
factory below is import-safe and unit-testable; ``main`` is the live entrypoint.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from http import HTTPStatus

from aiohttp import web
from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings, TurnContext
from botbuilder.schema import Activity
from tocdoc_sdk import TocDocClient

from .bot import TocDocTeamsBot
from .config import AdapterConfig, load_config
from .tokens import OnBehalfOfTokenProvider, TokenProvider

logger = logging.getLogger("teams_bot.app")


def build_message_handler(
    *,
    adapter: BotFrameworkAdapter,
    bot: TocDocTeamsBot,
):
    """Return an aiohttp handler for ``POST /api/messages``.

    The handler deserializes the activity, extracts the ``Authorization``
    header, and hands both to ``adapter.process_activity`` which validates the
    inbound JWT before calling the bot.
    """

    async def messages(req: web.Request) -> web.Response:
        if "application/json" not in req.headers.get("Content-Type", ""):
            return web.Response(status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE)

        body = await req.json()
        activity = Activity().deserialize(body)
        auth_header = req.headers.get("Authorization", "")

        async def logic(turn_context: TurnContext) -> None:
            await bot.on_turn(turn_context)

        # process_activity performs the inbound Bot Framework JWT validation
        # (issuer + audience = the bot's MicrosoftAppId) BEFORE invoking logic.
        # A missing/invalid/expired token makes _authenticate_request raise
        # PermissionError; we map that to 401 so a forged activity is rejected
        # at the trust boundary and never reaches the bot. We catch ONLY the
        # auth-failure type here so genuine in-turn errors still surface (they
        # are handled by adapter.on_turn_error, not masked as a 401).
        try:
            invoke_response = await adapter.process_activity(activity, auth_header, logic)
        except PermissionError:
            logger.warning("Rejected inbound activity: Bot Framework JWT authentication failed.")
            return web.Response(status=HTTPStatus.UNAUTHORIZED)
        if invoke_response:
            return web.json_response(data=invoke_response.body, status=invoke_response.status)
        return web.Response(status=HTTPStatus.OK)

    return messages


def build_token_provider(config: AdapterConfig, env: dict[str, str]) -> TokenProvider:
    """Construct the production OBO token provider (deployment seam).

    Reads the bot/OBO client credentials from the environment. The provider is
    a documented stub until the live OBO exchange is wired (see README).
    """
    return OnBehalfOfTokenProvider(
        tenant_id=config.azure_tenant_id,
        client_id=env.get("MICROSOFT_APP_ID", ""),
        client_secret=env.get("MICROSOFT_APP_PASSWORD", ""),
        qna_scope=f"api://{config.audience_id}/.default",
    )


def create_app(
    *,
    config: AdapterConfig,
    token_provider: TokenProvider,
    app_id: str,
    app_password: str,
    adapter: BotFrameworkAdapter | None = None,
    client_factory: Callable[[str], TocDocClient] | None = None,
) -> web.Application:
    """Build the aiohttp application (the live host).

    Args:
        config: Validated adapter config (already asserts a concrete tenant).
        token_provider: The OBO token provider (injectable for tests).
        app_id: The bot's ``MicrosoftAppId`` (used to validate inbound JWTs).
        app_password: The bot app password.
        adapter: Optional pre-built adapter, the inbound-auth seam. In
            production this is left ``None`` and a real ``BotFrameworkAdapter``
            is constructed from ``app_id``/``app_password`` so JWT validation is
            fully enforced. Tests inject a fake adapter to exercise the
            authenticated/unauthenticated paths offline WITHOUT weakening that
            production validation (the real adapter is never swapped in prod).
        client_factory: Optional per-turn QnA client factory ``(bearer) -> client``.
            Left ``None`` in production (a real ``TocDocClient`` factory is built);
            tests inject a recording fake so an authenticated turn can be asserted
            to reach the QnA call with the server-derived ``bot_tag``.
    """
    if adapter is None:
        settings = BotFrameworkAdapterSettings(app_id=app_id, app_password=app_password)
        adapter = BotFrameworkAdapter(settings)

    async def on_error(turn_context: TurnContext, error: Exception) -> None:
        # Never leak internal detail to the user; reply with a generic message.
        logger.exception("Unhandled adapter error: %s", type(error).__name__)
        await turn_context.send_activity("Something went wrong. Please try again.")

    adapter.on_turn_error = on_error

    # A per-turn client factory carries the OBO bearer token into the SDK
    # client. The base URL is the network-private QnA service.
    if client_factory is None:

        def client_factory(bearer_token: str) -> TocDocClient:
            return TocDocClient(config.qna_base_url, token=bearer_token)

    bot = TocDocTeamsBot(
        qna_client=TocDocClient(config.qna_base_url),
        token_provider=token_provider,
        tenant_bot_tag_map=config.tenant_bot_tag_map,
        fr_tag=config.fr_tag,
        client_factory=client_factory,
    )

    app = web.Application()
    app.router.add_post("/api/messages", build_message_handler(adapter=adapter, bot=bot))
    return app


def main() -> None:  # pragma: no cover - live entrypoint
    """Live entrypoint: load config, build the app, serve.

    Requires bot app-registration credentials and a reachable QnA service; not
    exercised by the unit tests.
    """
    import os

    logging.basicConfig(level=logging.INFO)
    env = dict(os.environ)
    config = load_config(env)
    token_provider = build_token_provider(config, env)

    app_id = env.get("MICROSOFT_APP_ID", "")
    app_password = env.get("MICROSOFT_APP_PASSWORD", "")
    if not app_id:
        print("MICROSOFT_APP_ID is required to run the adapter.", file=sys.stderr)
        raise SystemExit(1)

    app = create_app(
        config=config,
        token_provider=token_provider,
        app_id=app_id,
        app_password=app_password,
    )
    web.run_app(app, host="0.0.0.0", port=int(env.get("PORT", "3978")))  # noqa: S104


if __name__ == "__main__":  # pragma: no cover
    main()
