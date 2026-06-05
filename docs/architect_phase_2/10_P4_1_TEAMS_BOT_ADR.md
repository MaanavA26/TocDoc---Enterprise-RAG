> **Status:** DRAFT — produced by a multi-agent design council (3 identity->bot_tag approaches: AAD-tenant-map / channel-binding / SSO-OBO; 3 judges). Pending architect review. Not yet implemented.

# P4-1 Microsoft Teams Bot — Architecture Decision Record

## Context & constraints

TocDoc ships as a two-service product (ingestion + QnA) deployed into each client's
own Azure subscription. P4-1 adds a Microsoft Teams front end so end users can ask
questions in Teams instead of calling the API directly. The hard requirement is the
one that governs the whole product: **a Teams user must never be able to retrieve
another tenant's — or another workspace's — documents.** Everything below is
subordinate to that.

The Teams bot does not get a new trust model. It must fit the contract that already
exists in the QnA service, verified against the codebase:

- **Request contract.** `POST /qna` takes a `Payload(session_id, bot[], fr_tag,
  bot_tag)` (`services/qna/src/utils/util.py`); all fields required, `bot[]` must be
  non-empty, blank `bot_tag`/`fr_tag` is a 400. The endpoint is stateless and
  idempotent — conversation history rides in `bot[]` every turn, there is no
  server-side session.
- **Auth.** The middleware in `services/qna/src/core/auth.py` validates an Azure AD
  RS256 JWT via `services/qna/src/core/token_validator.py` (JWKS cached 1h with a
  single key-rotation refresh+retry), checks audience and issuer, and extracts the
  user email from `upn`/`preferred_username`/`email`. **No email claim → 401**
  (`auth.py:119-120`). `request.state.email` is attached for audit/observability.
- **Issuer pin.** `token_validator.py:182-190` builds the expected issuer from
  `settings.AZURE_TENANT_ID` — v1 `https://sts.windows.net/{tenant}/` or v2
  `https://login.microsoftonline.com/{tenant}/v2.0` — and rejects any other issuer
  with `TokenValidationError` → 401. This is the cross-tenant guarantee, and it
  **fails closed**.
- **Isolation (P0-2).** `bot_tag` is passed explicitly into the search layer and
  becomes the OData filter `fr_tag eq '<fr>' and bot_tag eq '<bot_tag>'`
  (`services/qna/src/services/search_service.py:110-112`); empty `bot_tag` raises
  `ValueError` before any search runs (`search_service.py:47-48`).
- **Response contract.** Success is `{answer, citation}` where `citation` is
  `CitationMap = RootModel[dict[str, str]]` serializing flat to `{filename:
  filepath}` (`services/qna/src/core/responses.py`). `page_number` is explicitly out
  of scope today (P2-1, not implemented).
- **Error contract (P0-6).** Every 4xx/5xx is an `ErrorEnvelope` with
  `code`/`message`/`request_id` plus an `X-Request-ID` header, threaded from
  `RequestIDMiddleware`.
- **SDK.** `clients/python/tocdoc_sdk` mirrors the contract (`QnARequest`,
  `QnAAnswer`, `CitationMap`, `ApiError`). Its retry set is
  `_RETRYABLE_STATUS = {500, 502, 503, 504}` (`client.py:24`) — **5xx and timeouts
  retried, 4xx fails fast, including 429.**

The crux is what the brief and the bot_tag decision record
(`docs/architect_phase_2/04_BOT_TAG_DECISION_RECORD.md`) leave open: **how a Teams
identity becomes a `bot_tag`.** No code does this today. The decision record also
establishes the granularity trap — a single client tenant routinely owns *many*
`bot_tag` workspaces (`client_a_hr`, `client_a_legal`, `client_b_finance`; §3,
lines 70-78), and its open questions #1 (one deployment → many workspaces?), #2
(workspace ↔ AAD groups?), #3 (infer workspace from claims vs payload?), and #4
(workspace metadata in a DB?) are all deferred, not settled.

## Decision (recommended architecture)

Build a **thin Bot Framework adapter** as a new standalone service deployed into the
**same client Azure subscription** as QnA. It is purely additive: on the happy path
it requires **no change to the QnA service, the search layer, the config module, or
the SDK contract**. The adapter speaks the Bot Framework protocol on its inbound
edge and the existing Azure AD JWT contract on its outbound edge.

Per turn the adapter:

1. Receives a Bot Connector–signed `Activity` and **validates the inbound Bot
   Framework JWT** (issuer `https://api.botframework.com`) before reading any field.
2. **Derives `bot_tag` server-side** from a Microsoft-signed, service-stamped field
   on that activity (see the next section). The user's message text is treated as
   natural language only and can never carry a `bot_tag`.
3. Obtains an Azure AD **user** token for the QnA API via **Teams SSO +
   On-Behalf-Of (OBO)** exchange.
4. Builds the `Payload(session_id, bot[], fr_tag, bot_tag)` per `util.Payload` and
   calls `POST /qna` through the existing SDK (or raw HTTPS) with the OBO token as
   `Authorization: Bearer`.
5. Renders the `{answer, citation}` envelope as a Teams adaptive card, and maps any
   P0-6 `ApiError` envelope to a friendly Teams message that surfaces the
   `request_id`.

Three judges scored the candidate mappings and split. On inspection the two
top-scoring proposals are not rivals: **AAD-tenant-map (Proposal 1)** and
**channel/conversation binding (Proposal 2)** are the *same* architecture at two
points on two axes — **granularity** (tenant vs channel/group) and **storage**
(config vs operator-owned store). Both put the unspoofable spine in exactly the same
place: `bot_tag` derived from a Microsoft-signed, service-stamped field, never from
the request body. The Chair's decision therefore **unifies** them rather than
choosing:

- **Default (matches the brief's deployment model and Phase-2 scope):** derive
  `bot_tag` from the verified `channelData.tenant.id` via an admin-configured
  **tenant → bot_tag map baked into adapter config** (Proposal 1). For the common
  single-tenant-single-workspace deployment the map is one entry.
- **Evolution (multi-workspace clients):** when one tenant owns several workspaces,
  replace the config map with an **operator-owned, fail-closed, no-wildcard,
  audit-logged binding store** keyed on the service-stamped conversation reference
  `(teams_tenant_id, channel/conversation id) → bot_tag` (Proposal 2). This directly
  answers decision-record OQ#4 (DB-backed metadata) and closes the brief's
  no-hot-reload risk (#10).
- **Cross-cutting QnA-side defense-in-depth (grafted from Proposal 3, corrected):**
  add a check in QnA that the verified token `tid` is authorized for the requested
  `bot_tag` and return **403** on mismatch. This is the single best isolation idea
  across the three proposals — but it must bind `tid → a permitted *set* of
  bot_tags`, **not** Proposal 3's `tid == bot_tag`, which would reject legitimate
  multi-workspace requests. This is **new behavior, not shipped today**, and is
  sequenced explicitly below.

The token decision is forced, not preferential: **OBO (a delegated user token) is
the only path that reuses P0-1 unchanged.** An app-only / client-credentials token
carries no `upn`/`preferred_username`/`email`, so `auth.py:119-120` rejects it 401.
OBO is mandatory; the service-credential alternative is in Rejected Alternatives.

## Identity → bot_tag mapping (unspoofable; the crux)

The end user **never types, names, sees, or supplies** a `bot_tag`. The adapter
derives it server-side. Unspoofability comes from the trust chain, not from
trusting the client:

1. **Signed envelope.** The entire inbound `Activity` — including
   `channelData.tenant.id` and the conversation reference — is signed by Microsoft's
   Bot Connector (inbound Bot Framework JWT, issuer `api.botframework.com`,
   audience = the bot's `MicrosoftAppId`). The adapter **must reject any activity
   that fails this signature check before reading a single field.** A user cannot
   forge the tenant claim or the conversation id; they are stamped by the service,
   not editable from the message body.

2. **Server-side resolution.** From the verified activity:
   - **Default:** `bot_tag = config_map[channelData.tenant.id]` (tenant
     granularity). One entry for single-workspace deployments.
   - **Multi-workspace:** `bot_tag = binding_store[(tenant_id,
     channel/conversation_id)]` (channel granularity), **fail-closed** — an unbound
     conversation is rejected with a clear Teams message and is **never** served a
     default `bot_tag`.

3. **Format validation before the filter.** The resolved value is validated against
   the decision record's regex `^[A-Za-z0-9_-]{1,128}$` (§ Validation rules, lines
   156-172) — rejecting quotes, spaces, semicolons, OData operators, path-traversal,
   and over-long values — **before** it reaches the OData filter. This closes a real
   gap: `search_service.py:110` only escapes single quotes (`'` → `''`) and applies
   **no** format/length check today, so a craft or oversized `bot_tag` silently
   returns 0 results instead of a clean 400.

The granularity choice (tenant-level config map vs channel-level binding store vs
AAD-group binding) is the architect's call and is surfaced in Open Questions; it
maps onto decision-record OQ#1 and OQ#2. What is **not** negotiable is that the
value is server-derived from a signed, service-stamped field — that property is what
makes it unspoofable regardless of which granularity is chosen.

## Auth / token flow to the QnA API

Two distinct tokens, never conflated.

- **Inbound (Bot Connector → adapter).** A Bot Framework JWT, issuer
  `api.botframework.com`. Validating it is what makes `channelData.tenant.id` and
  the conversation reference trustworthy. **This token is never forwarded to /qna.**

- **Outbound (adapter → /qna).** A genuine **Azure AD user token** obtained via
  **Teams SSO + On-Behalf-Of**:
  - The Teams client SSO returns the user's AAD token to the adapter.
  - The adapter performs an OBO exchange (`grant_type=on-behalf-of`) requesting a
    token whose **`aud == AUDIENCE_ID`** (the **QnA API** app registration), scope
    **`api://<qna-app-id>/.default`** — **not** the bot's own app id. Targeting the
    bot app id yields a token that silently 401s on an audience mismatch that looks
    like a generic auth failure.
  - The resulting token's `iss` is the customer tenant's v1 or v2 issuer, both of
    which `token_validator.py:182-190` already handles.
  - The adapter forwards it as `Authorization: Bearer`. The existing middleware does
    the rest unchanged: RS256/JWKS verification, audience + issuer check, 10s leeway,
    email extraction, `request.state.email`.

OBO preserves the per-user `upn`/`preferred_username` claim, which keeps
`request.state.email` meaningful for audit/billing/rate-limiting and keeps the
issuer pin doing real isolation work. **No P0-1 code changes are required for the
happy path** — the adapter only changes how the bearer token is obtained, not how it
is verified.

## Hosting & deployment (Bot Framework adapter in the client subscription)

A **new standalone service** — a Python Bot Framework adapter (aiohttp
`CloudAdapter` / `BotFrameworkHttpAdapter` messaging endpoint) — deployed as its own
**Container App into the same client Azure subscription / resource group** as QnA.
It is **not** folded into the QnA FastAPI app: the adapter owns the Bot Connector
JWT verification, while `/qna` keeps its Azure AD JWT contract untouched.

- It calls `/qna` via the typed SDK (`tocdoc_sdk.TocDocClient`, which already sets
  the bearer header, parses the success envelope, and raises `ApiError` from the
  P0-6 envelope) or via raw HTTPS.
- It holds **no** Azure OpenAI / Search secrets. Its only secrets are its bot app
  registration plus the OBO client credentials.
- Config: `AZURE_TENANT_ID`, `AUDIENCE_ID`, the QnA base URL, `fr_tag` default, and
  the tenant→bot_tag map (or binding-store connection) — mirroring the config
  discipline in `services/qna/src/config/config.py`.
- **`/qna` is kept network-private within the subscription**, reachable only by the
  adapter, so no external caller can hand-craft a `Payload` with an arbitrary
  `bot_tag`. This is defense-in-depth on top of the auth layer, not a substitute for
  it.
- **Startup assertion:** the adapter (and ideally QnA) must assert at startup that
  `AZURE_TENANT_ID` is a concrete tenant GUID and **never `common`/`organizations`**.
  The entire cross-tenant guarantee rests on issuer equality; a placeholder fails
  closed (denies all) rather than leaking, and this check makes the misconfiguration
  loud.
- Observability: read `X-Request-ID` from QnA responses into adapter logs so
  correlation spans Teams turn → /qna → search.

For the default config-map deployment, remapping a tenant requires a redeploy —
acceptable for single-tenant clients. The binding-store evolution removes that
constraint (rebind without redeploy), which is its main operational payoff.

## Citation rendering (adaptive cards)

The success body is `{answer, citation}` with `citation` a flat `CitationMap =
{filename: filepath}` (`responses.py`; `response_model_exclude_none=True` keeps it
byte-identical). The adapter renders `answer` as the card body and iterates the
citation map generically, one entry per cited document.

- **`filepath` is an internal blob/source path, not a user-clickable URL.** Render
  `filename` as **plain text** (or a non-navigating chip). Do **not** emit a blind
  `Action.OpenUrl` to `filepath` — that produces either broken links or an
  over-permissive leak. A clickable link is gated on a future permission-aware
  resolver (`filepath` → an authorized SAS / SharePoint sharing URL honoring the
  user's permissions); until that exists, text only.
- **Forward-compatible with `page_number` (P2-1, not implemented).** Iterate the
  citation entries generically rather than hard-coding the flat shape; the SDK's
  models are tolerant of unknown keys, but card rendering must show page info only
  when present so old (`{filename: filepath}`) and new (with page) shapes both render
  during a rolling upgrade.
- Preserve the QnA `request_id` in the turn (e.g. an Activity custom field) for
  end-to-end tracing.

## Preserving tenant isolation (P0-2)

Isolation is layered, and the strongest layer is **existing code, not the adapter**:

1. **Cross-tenant (the paramount bar) — already enforced.** In the per-deployment
   single-tenant model, `AZURE_TENANT_ID` is the client's concrete tenant GUID, so
   `token_validator.py:182-190` pins the issuer to that tenant. A token minted by
   any other tenant fails the issuer-equality check and is rejected **401 before any
   search runs.** Tenant B's documents are not even in tenant A's index. This holds
   for all candidate mappings; it is the shared spine. **Precondition:**
   `AZURE_TENANT_ID` must be a concrete GUID (enforced by the startup assertion
   above).

2. **No user-supplied scope.** Because `bot_tag` is server-derived from the signed
   activity, a user has no code path to inject another scope by editing a payload.
   Combined with `/qna` being network-private, there is no external path to a
   hand-crafted `Payload` either.

3. **Within-tenant (workspace) separation.** This is the honest limit: QnA does
   **not** today bind the request-body `bot_tag` to the token's `tid`. In the
   config-map default, within-tenant separation rests on adapter-map correctness; in
   the binding-store evolution it rests on the integrity of the operator-owned store
   (operator-only writes, audit-logged changes, no wildcard per decision-record line
   181, regex-validated values, fail-closed on unbound). The **defense-in-depth
   fix** — new behavior, sequenced below — is a QnA-side check that the verified
   `tid` is authorized for a **permitted set** of `bot_tags`, returning 403
   otherwise.

4. **OData hardening.** Regex-validate `bot_tag` before it reaches the filter
   (`search_service.py:110`), complementing the existing single-quote escaping.

## Rejected alternatives

- **App-only / client-credentials service token to /qna.** Rejected as a default.
  It carries no `upn`/`preferred_username`/`email`, so `auth.py:119-120` returns 401
  — it cannot reuse P0-1 unchanged, and it collapses `request.state.email` to the
  service principal, destroying per-user audit/billing/rate-limiting. This is the
  sharpest discriminator in favor of OBO. Acceptable only as an explicitly opted-in,
  documented fallback where the operator accepts losing per-user attribution.

- **Trusting a client-supplied `bot_tag` from the message body / payload.** Rejected.
  Today `bot_tag` flows from the body untouched into the OData filter; trusting it
  from the user would let any authenticated user in a tenant query any `bot_tag`.
  The whole design hinges on server-side derivation from a signed field.

- **`tid == bot_tag` strict equality (Proposal 3 as written).** Rejected as the
  general rule. All three judges flagged that one `tid` legitimately owns many
  `bot_tags` (decision record §3), so strict equality would reject legitimate
  multi-workspace requests. **Corrected and grafted** as `tid → permitted set →
  403`.

- **Inferring workspace purely from the AAD tenant claim (one bot per tenant).**
  Rejected as the *only* model. It cannot express the documented HR/legal/finance
  split within one tenant. Retained as the **default single-workspace** case and
  extended (not replaced) by channel- or group-level binding.

- **Folding the bot into the QnA FastAPI app.** Rejected. The adapter speaks a
  different protocol (Bot Framework, Connector JWT) and must not entangle the clean
  Azure AD JWT contract of `/qna`.

- **Multi-tenant deployment with `AZURE_TENANT_ID=common`/`organizations` or a
  shared audience across tenants.** Rejected as the operating model. It breaks the
  issuer pin and reopens cross-tenant replay. Single-tenant deployment with a
  concrete GUID is the guardrail.

## Sequenced delivery plan (PR-sized increments)

1. **Config + guardrails.** Define the tenant→bot_tag config contract (one entry for
   single-workspace). Add the startup assertion that `AZURE_TENANT_ID` is a concrete
   GUID (reject `common`/`organizations`). Confirm `AUDIENCE_ID` == the QnA API app
   registration.
2. **Adapter skeleton.** Stand up the Bot Framework adapter Container App in the
   client subscription with **inbound Bot Connector JWT verification** that rejects
   unsigned/invalid activities before reading any field.
3. **Identity resolution.** Read the verified `channelData.tenant.id` (default) /
   conversation reference (multi-workspace), resolve `bot_tag`, **fail-closed** on no
   mapping, and validate against `^[A-Za-z0-9_-]{1,128}$`.
4. **OBO token flow.** Implement Teams SSO + OBO to mint a user token with
   `aud == AUDIENCE_ID`, scope `api://<qna-app-id>/.default`; forward as Bearer;
   build the `Payload` per `util.Payload`; prove the **unchanged** P0-1 middleware
   accepts it (email present on `request.state`).
5. **Rendering + resilience.** Adaptive-card rendering of `{answer, citation}`
   (filename as text), map P0-6 `ApiError` envelopes to friendly Teams messages
   surfacing `request_id`, wrap **every turn** so it always replies, and add
   **adapter-side 429 backoff / circuit-breaking** (the SDK fails fast on 429) plus
   graceful handling of 503 from JWKS outages and OBO consent/refresh.
6. **OData hardening (QnA-side).** Add `bot_tag` format/length validation before the
   filter, returning a clean 400 — independent of the adapter.
7. **Defense-in-depth (QnA-side, NEW behavior).** Add the `tid → permitted set of
   bot_tags → 403` cross-check. Ship with tests for issuer-mismatch denial,
   tid/bot_tag mismatch 403, and empty/oversized bot_tag rejection.
8. **Multi-workspace evolution (only if a client requires it).** Replace the config
   map with the operator-owned, fail-closed, no-wildcard, audit-logged binding store
   (DB-backed per OQ#4), enabling rebind without redeploy.

## Open questions for the architect

1. **Granularity / binding model** (decision-record OQ#1, OQ#2): tenant-level config
   map vs **channel-level** binding (location-scoped) vs **AAD-group** binding
   (identity-scoped). These answer different product questions and are genuinely the
   architect's call. Which is the launch target, and is multi-workspace a hard
   requirement for the first client?
2. **Permitted-set source for the 403 check:** where does `tid → {bot_tags}` live —
   adapter config, the binding store, an AAD group/app-role claim, or QnA config?
   This determines whether the defense-in-depth check is self-contained in QnA or
   depends on the adapter's store.
3. **Binding store backing** (OQ#4): config-baked (redeploy to change) vs managed
   store (Cosmos/table) for hot rebinding and audit. Trade simplicity now against
   operability at multi-workspace scale.
4. **`filepath` → authorized URL resolver:** is there appetite to build the
   permission-aware SAS/SharePoint resolver so citations become clickable, or do we
   ship text-only citations for v1?
5. **Shared-QnA fan-out:** if one QnA instance backs many bot instances, do we need
   adapter-side rate-limit governance beyond per-turn backoff, given the SDK does not
   retry 429?
6. **`page_number` (P2-1) versioning:** when typed/page citations land, how do we
   version the SDK models and gate card rendering so old and new shapes coexist
   during rolling upgrades?
