# TocDoc Teams Bot adapter (P4-1)

A thin **Microsoft Bot Framework adapter** that lets end users ask TocDoc
questions from inside Microsoft Teams. It is a **new, standalone service** and
is **purely additive**: on the happy path it requires no change to the QnA
service, the search layer, the config module, or the SDK contract.

This is a **v1 scaffold**. The structure, the unspoofable identity → `bot_tag`
derivation, adaptive-card rendering, and the QnA call are implemented and
unit-tested with mocks. **Live Teams/Azure are not exercised here** — the
inbound Bot Framework JWT validation, the On-Behalf-Of (OBO) token exchange,
and bot registration are deployment steps documented below.

Design source of truth: `docs/architect_phase_2/10_P4_1_TEAMS_BOT_ADR.md`.

## What it does, per turn

1. Receives a Bot Connector–signed `Activity` and **validates the inbound Bot
   Framework JWT** (`BotFrameworkAdapter.process_activity`, issuer
   `https://api.botframework.com`, audience = the bot's `MicrosoftAppId`)
   **before** the handler reads any field. This is the adapter's trust
   boundary; an unsigned/invalid activity never reaches the handler.
2. **Derives `bot_tag` server-side** from the Microsoft-signed
   `channelData.tenant.id` via the admin-configured `TENANT_BOT_TAG_MAP`.
3. Acquires an Azure AD **user** token for the QnA API via the injected token
   provider (the OBO seam).
4. Calls `POST /qna` through the typed SDK (`tocdoc_sdk.TocDocClient`) with the
   derived `bot_tag` + the user's message text as the single bot turn.
5. Renders `{answer, citation}` as a Teams **adaptive card**, or maps a P0-6
   `ApiError` envelope to a friendly card that surfaces the `request_id`.

## The identity → bot_tag model (unspoofable)

The end user **never types, names, sees, or supplies** a `bot_tag`. The whole
isolation guarantee rests on this:

- `bot_tag` is derived **only** from the *verified* `channelData.tenant.id` — a
  Microsoft-signed, service-stamped field — via `TENANT_BOT_TAG_MAP`.
- The derivation function (`teams_bot.identity.resolve_bot_tag`) takes a tenant
  id and a map. **It has no parameter for the user's message text.** So "a
  user's message can never select another tenant's `bot_tag`" is true *by
  construction*, not by a runtime guard that could be bypassed. The handler
  keeps the message text and the tenant id in separate variables and only ever
  passes the tenant id to the resolver.
- An **unknown tenant** (absent from the map) is rejected **fail-closed** — no
  QnA call is made, and the user is never served a default `bot_tag`.
- The resolved value is validated against the decision-record regex
  `^[A-Za-z0-9_-]{1,128}$` **before** it could reach the downstream OData
  filter, rejecting quotes/spaces/operators/path-traversal/over-long values.

This is the central, tested invariant (see `tests/test_identity.py` and the
anti-spoof flow test in `tests/test_bot.py`).

## Configuration

Environment variables (UPPER_SNAKE, matching the QnA service convention). No
secrets are defaulted or logged.

| Var | Purpose |
| --- | --- |
| `AZURE_TENANT_ID` | The client's **concrete tenant GUID**. Asserted at startup to never be `common`/`organizations`/`consumers` (those break the QnA issuer pin and reopen cross-tenant replay). |
| `AUDIENCE_ID` | The QnA API app registration id (the OBO token's target audience). |
| `QNA_BASE_URL` | Base URL of the (network-private) QnA service. |
| `TEAMS_FR_TAG` | Default `fr_tag` for QnA requests. Config-sourced, **never** user-supplied. Defaults to `read`. |
| `TENANT_BOT_TAG_MAP` | JSON object `{tenant_id: bot_tag}`. The admin-configured server-side mapping. |
| `MICROSOFT_APP_ID` / `MICROSOFT_APP_PASSWORD` | Bot app registration credentials (used to validate inbound JWTs). Never logged. |

`TENANT_BOT_TAG_MAP` example (single-tenant-single-workspace — the common
case — is one entry):

```json
{"11111111-1111-1111-1111-111111111111": "client_a_hr"}
```

For a multi-workspace tenant the ADR describes an evolution to an
operator-owned, fail-closed, no-wildcard binding store keyed on the
conversation reference; that is out of scope for this v1 scaffold.

## The OBO seam (live wiring deferred)

The adapter must call `/qna` with a genuine Azure AD **user** token (OBO is
mandatory — an app-only token has no `upn`/`preferred_username`/`email` claim
and is rejected `401` by the QnA P0-1 middleware). Acquiring that token needs
live Azure, so it lives behind a swappable interface in `teams_bot/tokens.py`:

- `TokenProvider` — the protocol the bot depends on.
- `OnBehalfOfTokenProvider` — the production seam. It is a **documented stub
  that raises until wired**, so a half-configured deployment fails *loudly*
  rather than silently sending no token.
- `StaticTokenProvider` — a fixed-token provider for tests and local dev.

Tests inject a fake provider. **Wiring the real OBO exchange is a deployment
step:**

1. Register the bot app in the client tenant; configure Teams SSO so the Teams
   client returns the user's AAD token to the adapter.
2. In `OnBehalfOfTokenProvider.get_qna_token`, perform an OBO exchange (e.g.
   MSAL `acquire_token_on_behalf_of`) requesting scope
   `api://<qna-app-id>/.default` so the resulting token's `aud` is the **QnA
   API** app registration — **not** the bot's own app id (a bot-app-id audience
   silently 401s on a mismatch that looks like a generic auth failure).
3. The resulting token's `iss` is the customer tenant's v1/v2 issuer, both of
   which the unchanged QnA token validator already handles. The adapter
   forwards it as `Authorization: Bearer`; **no QnA P0-1 code change is needed**.

## Citation rendering — deliberate ADR-aligned choice

The ADR's brief mentions "citations as links," but the ADR's own citation
section forbids exactly that: `filepath` is an **internal blob/source path, not
a user-clickable URL**. A blind `Action.OpenUrl` to it produces either broken
links or an over-permissive leak. So this adapter renders `filename` as **plain,
non-navigating text**. Clickable citations are gated on a future
permission-aware resolver (`filepath` → an authorized SAS/SharePoint URL
honoring the user's permissions; ADR open question #4). The card iterates
citations generically so a future page-aware shape renders without code change.

## Layout

```
services/teams-bot/
  requirements.txt          # botbuilder-core, aiohttp, -e ../../clients/python, httpx, pydantic
  requirements-dev.txt      # + pytest, pytest-asyncio
  pytest.ini
  teams_bot/
    __init__.py
    app.py                  # aiohttp host + /api/messages; inbound JWT validation seam
    bot.py                  # ActivityHandler: derive bot_tag, call QnA, render card
    cards.py                # adaptive-card rendering ({answer, citation}; ApiError)
    config.py               # env config + concrete-tenant startup assertion
    identity.py             # PURE, unspoofable bot_tag derivation (the invariant)
    tokens.py               # OBO token-provider seam (injectable)
  tests/                    # mocked — no live Teams/Azure
```

## Develop / test

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

## CI note

The repo CI gate currently covers `qna`, `ingestion`, the SDK, and `eval` — it
does **not** yet run `teams-bot`. Adding `services/teams-bot` to the CI matrix
is a tracked follow-up.
