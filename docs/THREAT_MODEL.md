# Threat Model

This document describes the **security architecture** of the system: where the
trust boundaries are, what we are protecting, the threats we have reasoned
about, the mitigations that exist **in code today**, and the residual risks /
hardening work that is **not** yet done.

It complements [`SECURITY.md`](../SECURITY.md), which is the *policy* (how to
report a vulnerability, supported versions, secret-handling rules). This is the
*architecture* — grounded in the actual code, with file/line citations so the
claims can be checked.

A deliberate discipline runs through this document: **enforced-in-code** is kept
strictly separate from **designed-but-not-shipped**. Where a control is planned
but not implemented, it is labelled as such and lives in
[Planned controls](#planned-controls-not-yet-shipped) or
[Residual risks](#residual-risks--hardening-backlog), never in
[Mitigations in place](#threats-mitigations-and-residual-risk).

---

## Deployment model

The product is two services — **ingestion** and **QnA** — deployed **into each
client's own Azure subscription / resource group**. The client owns all data,
compute, and Azure resources (Azure OpenAI, Cognitive Search, Document
Intelligence, Key Vault). There is no shared multi-tenant control plane operated
by the vendor.

A single deployment is **single Azure-AD-tenant**: it is configured with one
concrete `AZURE_TENANT_ID`, and the whole cross-tenant guarantee rests on that
(see [T2](#t2-cross-tenant-data-access) and its precondition). Within one
deployment, the index can hold several logical **workspaces** distinguished by a
`bot_tag` value (e.g. an HR workspace and a legal workspace for the same client).

---

## Trust boundaries

```mermaid
flowchart LR
    subgraph Internet["Untrusted"]
        U[End user / API caller]
        AAD[Azure AD / JWKS\nlogin.microsoftonline.com]
    end

    subgraph Sub["Client Azure subscription (trusted infra, client-owned)"]
        subgraph QnA["QnA service (FastAPI)"]
            MW[Auth middleware\nRS256 JWT verify]
            TB[Tenant-binding guard\ntid -> bot_tag allowlist]
            PIPE[QnA pipeline]
            SS[Search service\nbot_tag OData filter]
        end
        subgraph ING["Ingestion service (FastAPI, ingress internal by default)"]
            UP[/upload\nX-Admin-Token guard/]
            HP[/health\nunauthenticated probe/]
            ADMIN[/admin/*\nX-Admin-Token guard/]
            SAS[Search admin service]
        end
        KV[(Azure Key Vault)]
        SEARCH[(Azure Cognitive Search index)]
        AOAI[(Azure OpenAI)]
    end

    U -->|Bearer JWT| MW
    MW -->|JWKS fetch HTTPS| AAD
    MW --> TB --> PIPE --> SS --> SEARCH
    U -.->|file upload + X-Admin-Token\n(only if ingress externalized)| UP
    U -.->|X-Admin-Token\n(only if ingress externalized)| ADMIN
    ADMIN --> SAS --> SEARCH
    QnA -.secrets at startup.-> KV
    ING -.secrets at startup.-> KV
    PIPE --> AOAI
```

Boundaries that matter for this model:

1. **Internet → QnA service.** Every authenticated request crosses the Azure AD
   JWT boundary. This is the primary authentication boundary.
2. **Internet → ingestion service.** `/admin/*` **and** `POST /upload` cross the
   interim admin-token boundary — the upload route carries the same
   `require_admin_token` dependency as the admin routes
   (`services/ingestion/app.py:219`). Only `/health` (the liveness probe) is
   unauthenticated (`services/ingestion/admin/auth.py:15`). In the shipped
   infra the ingestion Container App's ingress is **internal by default**
   (`infra/main.bicep:74`, applied at `infra/main.bicep:276`; mirrored in
   `infra/terraform/variables.tf:96-105`), so this boundary is not even
   internet-reachable unless an operator opts in (see
   [T1](#t1-spoofing--unauthenticated-access)).
3. **QnA service → Azure AD.** Outbound HTTPS to the JWKS endpoint to fetch
   signing keys. Enforced HTTPS-only (`token_validator.py:92-93`).
4. **Service → Azure Key Vault.** Secrets are pulled at startup, never stored in
   the repo.
5. **Within a deployment: workspace ↔ workspace (`bot_tag`).** This is a *logical*
   boundary inside one tenant's index, and it is **enforced in code** on the QnA
   request path: a default-ON guard binds the validated token's `tid` to an
   operator-configured allowlist of `bot_tag` values and fails closed with a 403
   before any retrieval (`services/qna/src/core/tenant_binding.py:104-163`; see
   [T2b](#t2b-cross-workspace-within-tenant-access) and
   [R1 — resolved](#r1-within-tenant-workspace-separation-resolved)).

---

## Assets

| Asset | Why it matters |
| --- | --- |
| Client document content & embeddings (in Cognitive Search) | The core confidential data. Cross-tenant or cross-workspace leakage is the worst case. |
| Azure AD identity / the user email claim | Drives audit, billing, and authorization decisions; spoofing it is a foothold. |
| Service secrets (Azure OpenAI key, Search key, SP client secret, admin token) | Compromise gives direct access to the data plane. |
| Request integrity (the `bot_tag` scope) | The scope that decides which workspace's documents a query can read. |
| Audit / correlation trail (`X-Request-ID`, `request.state.email`) | Needed to investigate incidents; must not itself leak sensitive content. |

---

## Threats, mitigations, and residual risk

The threats below are organised roughly along STRIDE lines: **S**poofing,
cross-tenant / cross-workspace access (Elevation / Information disclosure),
**I**njection (Tampering), secret leakage (Information disclosure), and token
replay (Spoofing). Each lists the mitigation **in code** and, where relevant,
the honest gap.

### T1. Spoofing / unauthenticated access

**Threat.** An attacker calls an endpoint without (or with a forged) identity.

**Mitigations in place.**

- **QnA: RS256 JWT validation on every authenticated request.** The auth
  middleware (`services/qna/src/core/auth.py`) requires
  `Authorization: Bearer <token>`, then verifies the token cryptographically.
  `validate_token` (`services/qna/src/core/token_validator.py:169`) reads the
  `kid` from the header only to *select* a public key (never to trust it),
  fetches the matching JWKS key, and decodes with **full signature verification
  on** using `algorithms=["RS256"]` (`token_validator.py:282-294`). There is no
  `verify=False` path.
- **Email-claim required.** Even a validly-signed token is rejected **401** if it
  carries no `upn` / `preferred_username` / `email` claim
  (`auth.py:130-144`). This is what forces a *user* (delegated) token rather than
  an app-only token, keeping `request.state.email` meaningful for audit.
- **Fail-closed.** Any validation failure — malformed header, missing `kid`,
  unknown key, bad signature, wrong issuer/audience, expiry — raises
  `TokenValidationError` and returns an error envelope; nothing falls through to
  the handler (`auth.py:159-193`, `token_validator.py:198-302`). JWKS-unavailable
  is a **503**, not a bypass (`token_validator.py:100-109`).
- **Public routes are an explicit, narrow allowlist:** CORS preflight, the
  exact-match (root-path-aware) `/health` probe, and the Swagger asset paths
  only (`auth.py:93-98`).
- **Ingestion `/admin/*` AND `POST /upload`: shared-secret guard on every write
  path.** `require_admin_token` (`services/ingestion/admin/auth.py`) guards every
  `/admin/*` route and is **also applied to `POST /upload`**
  (`services/ingestion/app.py:219`), so the expensive DI + embedding + index
  path requires a valid `X-Admin-Token`. The guard compares the header to
  `ADMIN_API_TOKEN` using **constant-time** `secrets.compare_digest`
  (`auth.py:52-54`), refuses with **503** if no token is configured rather than
  bypassing auth (`auth.py:46-50`), and returns an identical **401** for both
  "missing" and "wrong" to avoid token enumeration (`auth.py:41-43, 52-58`).
- **Abuse / pre-auth DoS throttling.** `/qna` and `/qna/stream` carry a per-key
  sliding-window rate limit plus a global in-flight concurrency cap, both
  returning **429 + `Retry-After`** (`services/qna/app.py:162-202`, wired as
  route dependencies at `app.py:350` and `app.py:525`). Ingestion `/upload` has
  an in-process concurrency cap that likewise sheds load with 429 +
  `Retry-After` (`services/ingestion/app.py:253-260`). The JWKS path is hardened
  against the pre-auth bogus-`kid` fetch flood: unknown-`kid` refetches are
  throttled to one per 60s window and recently-unresolved `kid`s are held in a
  bounded negative cache (`token_validator.py:44-78`, `token_validator.py:217-255`).
  The `/qna/stream` SSE body closes the inner pipeline stream **promptly on
  client disconnect** (cooperative stop in an inner `finally`,
  `services/qna/app.py:613-647`), so an aborted stream cannot leak its
  concurrency slot or keep burning tokens, and the stream's concurrency slot is
  released only after the response body is fully torn down
  (`app.py:519-525`).

**Residual.** Only `/health` on the ingestion service remains unauthenticated —
it is the liveness probe (`admin/auth.py:15`). The admin token is still an
**interim** static shared secret; the intended end state is the same Azure AD
JWT mechanism as QnA (`admin/auth.py:9-10`). See
[R2 — resolved](#r2-ingestion-upload-authentication-resolved), which tracks the
remaining JWT migration under
[Planned controls](#planned-controls-not-yet-shipped).

### T2. Cross-tenant data access

**Threat.** A user from tenant B retrieves tenant A's documents.

**Mitigation in place — the issuer pin (the cross-tenant spine).**
`token_validator.py:270-279` derives the *expected* issuer from the configured
`settings.AZURE_TENANT_ID` — accepting only the v1 (`https://sts.windows.net/{tenant}/`)
or v2 (`https://login.microsoftonline.com/{tenant}/v2.0`) form for **that**
tenant — and rejects any other issuer with `TokenValidationError` → **401**. A
token minted by any other tenant fails issuer equality and is rejected **before
any search runs**. Combined with the per-deployment model, tenant B's documents
are not even present in tenant A's index. This control **fails closed**.

**Precondition (honest caveat).** This guarantee holds **only if
`AZURE_TENANT_ID` is configured to a concrete tenant GUID.** If it were ever set
to `common` or `organizations`, the issuer pin would no longer bind to one
tenant. **The QnA service still has no startup assertion for this** (no such
check exists in `services/qna/src/config/config.py`). The pattern now exists
in-repo: the Teams-bot adapter asserts a concrete GUID at startup and rejects
`common`/`organizations`/`consumers` for its own config
(`services/teams-bot/teams_bot/config.py:51-66`, invoked from
`config.py:47-48`) — but QnA has not yet adopted it. So the accurate statement
is: *cross-tenant isolation is enforced in code, provided the operator
configures a concrete GUID — QnA itself still has no guard that makes a
misconfiguration loud.* See
[R3](#r3-no-startup-assertion-that-azure_tenant_id-is-a-concrete-guid).

### T2b. Cross-workspace (within-tenant) access

**Threat.** An authenticated user within tenant A requests workspace
`client_a_legal` while only entitled to `client_a_hr`.

**Mitigations in place.**

- **Default-ON `tid` → `bot_tag` binding guard (fails closed).** The auth
  middleware attaches the **validated** token's `tid` claim to `request.state`
  (`services/qna/src/core/auth.py:157`) — the trusted tenant id never comes from
  the request body. `enforce_tenant_bot_tag_binding`
  (`services/qna/src/core/tenant_binding.py:104-163`) then requires the
  requested `bot_tag` to appear in a config-driven allowlist for that `tid`
  (`QNA_TENANT_BOT_TAG_MAP`, a JSON `{tid: [bot_tag, ...]}` map,
  `tenant_binding.py:67-101`). Enforcement is **ON by default** —
  `QNA_ENFORCE_TENANT_BINDING` unset means enforced; only an explicit falsy
  literal disables it (`services/qna/src/config/config.py:402-430`). Every
  failure mode **fails closed with a 403 envelope and no search**: missing
  `tid`, missing/unparseable map, unmapped `tid`, or a `bot_tag` outside the
  tenant's allowlist (`tenant_binding.py:141-163`, rejection at
  `tenant_binding.py:166-175`). The guard runs in both the `/qna` and
  `/qna/stream` handlers *before* the pipeline / retrieval fork
  (`services/qna/app.py:440`, `app.py:590`).
- **Scoped retrieval filter.** `bot_tag` is threaded explicitly through the
  pipeline and becomes part of the OData filter
  `fr_tag eq '<fr>' and bot_tag eq '<bot_tag>'`
  (`services/qna/src/services/search_service.py:131-133`). An **empty / blank
  `bot_tag` is rejected** with `ValueError` *before any search runs*
  (`search_service.py:54-55`). The `bot_tag` is **state-only and never made
  LLM-visible** (`services/qna/src/agents/state.py:36`), so it cannot be
  exfiltrated through model output.

**Honest caveat.** The allowlist is operator-configured environment state, and a
deployment may *explicitly* opt out (`QNA_ENFORCE_TENANT_BINDING=false`) — the
documented intent is single-workspace deployments that derive `bot_tag`
elsewhere (e.g. the Teams adapter derives it server-side,
`services/teams-bot/teams_bot/identity.py:65-97`). An opted-out deployment
reverts to caller-supplied `bot_tag` scoping. See
[R1 — resolved](#r1-within-tenant-workspace-separation-resolved).

### T3. Injection (OData filter injection)

**Threat.** A crafted `bot_tag` / `document_id` / `fr_mode` breaks out of the
filter literal and reads or alters the query scope.

**Mitigations in place.**

- **Admin layer: regex validation + escaping (defense in depth).** Admin routes
  validate path/query parameters against strict patterns *before* the service
  layer runs — `BOT_TAG_PATTERN = ^[A-Za-z0-9_-]{1,128}$`,
  `DOCUMENT_ID_PATTERN`, `RUN_ID_PATTERN`
  (`services/ingestion/admin/routes.py:64-68`, applied at lines 101, 132, 136,
  174, 208, 212, 247, 295, 299). These reject quotes, spaces, semicolons, OData
  operators, path traversal, and over-long values, returning a clean **422/400**.
  The service layer then *additionally* escapes single quotes via `_escape_odata`
  (`'` → `''`, `search_admin_service.py:88-95`) as a second line of defence "in
  case a future caller bypasses validation."
- **Upload route: the same pattern route-side AND escaping at the sink.**
  `POST /upload` validates `bot_tag` against the same
  `^[A-Za-z0-9_-]{1,128}$` pattern at the route, rejecting a quote/space-bearing
  payload with a 422 before any pipeline call
  (`services/ingestion/app.py:210`, applied at `app.py:223`), and `fr_mode` is
  allowlisted to `^(read|layout)$` at the same route (`app.py:225-229`). The
  ingestion data layer *additionally* OData-escapes both values at a single
  filter-building chokepoint (`services/ingestion/custom_rag.py:49-69`; the
  not-regex-validated `source_path` is likewise escaped,
  `custom_rag.py:787-799`).
- **QnA search layer: quote escaping.** `search_service.py:131-133` escapes single
  quotes in both `bot_tag` and `fr_mode` before building the filter literal.
- **QnA `fr_tag` is allowlisted at the request boundary** — unknown modes are
  rejected with a 400 before any retrieval or agent fork
  (`services/qna/app.py:425-430`); QnA uses an internal `fr_mode` → `fr_tag`
  mapping rather than a free-form value.

**Residual — validation asymmetry (narrowed).** The QnA `/qna` path still
applies **no format/length regex** to `bot_tag` (the model field is a plain
string, `services/qna/src/utils/util.py:42`). The exposure is materially
narrower than when this model was first written: with tenant binding ON (the
default), a `bot_tag` outside the tenant's allowlist is rejected **403 before
any search** (`tenant_binding.py:159-163`), so a crafted or oversized value no
longer reaches the filter in a default deployment; quote-escaping
(`search_service.py:131-133`) remains the backstop. The regex remains backlog
hygiene for explicitly opted-out deployments. See
[R4](#r4-qna-bot_tag-is-quote-escaped-but-not-format-validated).

### T4. Secret leakage

**Threat.** Secrets end up in the repo, in logs, or in error responses.

**Mitigations in place.**

- **No secrets in the repo.** Secrets are loaded at runtime from **environment
  variables** and **Azure Key Vault** (`services/qna/src/config/config.py`, which
  pulls KV secrets via `DefaultAzureCredential` + `SecretClient` and rewrites them
  into `os.environ` under canonical names). `.env*` files are git-ignored
  (per `SECURITY.md`).
- **Structured error envelope never leaks exception text (P0-6).** Every 4xx/5xx
  is the `ErrorEnvelope` shape (`code` / `message` / `request_id`) built by
  `build_error_response` (`services/qna/src/core/errors.py`). The catch-all
  `unhandled_exception_handler` logs the full trace **server-side** but returns
  only a generic `"Internal server error"` — `str(exc)` is **never** sent to the
  client (`errors.py:311-345`). Auth middleware likewise never returns `str(e)`
  and never logs the token value, only a coarse `failure_type` label
  (`auth.py:27-47, 159-193`).
- **Logs avoid sensitive payloads.** The token value is never logged
  (`auth.py:122`); `_materialize` "never logs chunk content"
  (`search_service.py:190`); validation errors are truncated and the user
  `input` field is not echoed back (`errors.py:281-308`).
- **Admin select-lists are deliberately narrow** — they never select `content`
  or vector fields (`search_admin_service.py:38-62`), so admin responses cannot
  spill document text.
- **Dependency CVEs gate CI.** The security job runs `bandit` (gating,
  `.github/workflows/ci.yml:98-99`) and `pip-audit` as a **hard gate** — no
  `continue-on-error` — over both services' requirements
  (`ci.yml:104`, steps at `ci.yml:124-135`), so a vulnerable transitive package
  fails the build instead of merely printing a report.

**Residual.** None specific to this row beyond the platform-inherent ones; the
former pip-audit gap is closed — see
[R5 — resolved](#r5-pip-audit-ci-gating-resolved).

### T5. Token replay / forgery

**Threat.** A captured or forged token is replayed.

**Mitigations in place.**

- **Expiry is enforced** with a tight **10-second** clock-skew leeway
  (`token_validator.py:293`); expired tokens map to a distinct `expired_token`
  failure label and a 401 (`token_validator.py:295-296`, `auth.py:41-42`).
- **Audience pin — and the `aud` claim is mandatory.**
  `jwt.decode(..., audience=settings.AUDIENCE_ID)` (`auth.py:123-127`,
  `token_validator.py:286`) rejects tokens minted for a different audience, and
  `require_aud: True` (`token_validator.py:293`) rejects a signed token that
  *omits* the `aud` claim rather than skipping the check.
- **Signature + issuer + JWKS rotation (DoS-bounded).** Forgery requires the
  tenant's private signing key. Key rotation is handled gracefully *without*
  opening a pre-auth fetch amplifier: on a `kid` cache miss the JWKS set is
  refreshed **in place** (a fetch failure keeps serving the existing keys,
  `token_validator.py:147-161`), the refresh is throttled to once per 60s
  window, and recently-unresolved `kid`s are short-circuited from a bounded
  FIFO negative cache (`token_validator.py:44-78`, miss-path logic at
  `token_validator.py:217-255`). The positive cache has a 1h TTL
  (`token_validator.py:42`, `token_validator.py:130-133`).

**Residual.** There is no token denylist / `jti` tracking — a stolen, still-valid
token can be replayed within its lifetime. This is standard for stateless JWT
auth and is mitigated by short token lifetimes (an Azure AD configuration
concern, outside this code).

---

## Planned controls (not yet shipped)

This section keeps the enforced-in-code vs designed-but-not-shipped split
honest in **both** directions: the second list below is what remains designed
but not implemented (documented so the intended end state is clear and nobody
mistakes it for current behaviour), and the first list records which
first-draft entries have **since shipped** — now described as
enforced-in-code behaviour in
[Threats, mitigations, and residual risk](#threats-mitigations-and-residual-risk).

Shipped since the first draft of this model:

- **Teams bot unspoofable identity → `bot_tag` — IMPLEMENTED**
  (`services/teams-bot/`). The inbound `Activity`'s Bot Framework JWT is
  validated by `adapter.process_activity` **before any activity handling**
  (`services/teams-bot/teams_bot/app.py:60-73`); `bot_tag` is **derived
  server-side** from the verified, service-stamped tenant id through an
  admin-configured map — fail-closed on an unmapped tenant, and the resolved
  value is format-validated against `^[A-Za-z0-9_-]{1,128}$`
  (`services/teams-bot/teams_bot/identity.py:40`, `identity.py:65-97`). The
  user never types, names, or supplies a `bot_tag`. The adapter's config
  asserts a concrete tenant GUID at startup, rejecting
  `common`/`organizations`/`consumers` (`teams_bot/config.py:51-66`). Note:
  ADR 10's status header (line 1) still reads "DRAFT … Not yet implemented" —
  that line is stale relative to the code.
- **QnA-side `tid` → permitted-set-of-`bot_tags` → 403 check — IMPLEMENTED**,
  exactly in the permitted-*set* shape the ADR called for (not strict
  `tid == bot_tag`): the default-ON tenant-binding guard described in
  [T2b](#t2b-cross-workspace-within-tenant-access)
  (`services/qna/src/core/tenant_binding.py:104-163`).

Still planned, not yet shipped:

- **`AZURE_TENANT_ID` concrete-GUID startup assertion in QnA (ADR 10, lines
  197-201).** Proposed to make a `common`/`organizations` misconfiguration fail
  loudly at startup. Not implemented in
  `services/qna/src/config/config.py`; the Teams adapter's
  `assert_concrete_tenant` (`teams_bot/config.py:51-66`) is the in-repo
  precedent to adopt (see
  [R3](#r3-no-startup-assertion-that-azure_tenant_id-is-a-concrete-guid)).
- **QnA-side `bot_tag` format/length validation (ADR 10, lines 135-141).**
  Proposed to apply the admin routes' `^[A-Za-z0-9_-]{1,128}$` pattern on the QnA
  path too. Not implemented (see [R4](#r4-qna-bot_tag-is-quote-escaped-but-not-format-validated));
  the exposure is narrowed by the default-ON tenant-binding guard.
- **Azure AD JWT auth for ingestion `/admin/*` and `/upload`.** The interim
  `X-Admin-Token` shared secret (now covering the upload path too) is still the
  mechanism; the documented end state is the same RS256 JWT contract as QnA
  (`services/ingestion/admin/auth.py:9-10`).
- **Live On-Behalf-Of wiring for the Teams adapter.** The outbound
  Teams SSO → OBO exchange is a deliberate deployment seam:
  `OnBehalfOfTokenProvider` **raises until wired** so a half-configured
  deployment fails loudly rather than silently sending no/invalid tokens
  (`services/teams-bot/teams_bot/tokens.py:72-110`).
- **Network-private `/qna` within the subscription (ADR 10, lines 193-196).** A
  deployment-level control to ensure only the adapter can reach `/qna`. The
  shipped infra now defaults the *ingestion* app's ingress to internal
  (`infra/main.bicep:74`), while `/qna` remains public by design with auth at
  the application layer (`infra/main.bicep:344-346`); flipping `/qna` private is
  still an infrastructure-configuration concern, not enforced by application
  code.

---

## Residual risks & hardening backlog

Ordered as in the first draft of this model. **Risk IDs are stable** — code
comments reference them (e.g. `services/qna/src/core/tenant_binding.py:1` cites
R1) — so resolved entries are retained here, each stating the control now in
place rather than re-narrating the old gap. **Still open: R3, R4 (narrowed),
R6.** Resolved: R1, R2, R5.

### R1. Within-tenant workspace separation (resolved)

**Resolved — enforced in code.** QnA binds the request's `bot_tag` to the
validated token's `tid` via a **default-ON, fail-closed** allowlist guard:
`enforce_tenant_bot_tag_binding`
(`services/qna/src/core/tenant_binding.py:104-163`) rejects with a 403 envelope
**before any retrieval** when the `(tid, bot_tag)` pair is not in the
operator-configured `QNA_TENANT_BOT_TAG_MAP` — and equally when the map is
missing/unparseable or the token carries no `tid`
(`tenant_binding.py:141-163`, `tenant_binding.py:166-175`). Enforcement
defaults ON (`services/qna/src/config/config.py:402-430`) and the guard covers
both `/qna` and `/qna/stream` (`services/qna/app.py:440`, `app.py:590`); the
trusted `tid` comes from the verified JWT
(`services/qna/src/core/auth.py:157`), never the request body. Remaining
caveat: a deployment may explicitly opt out
(`QNA_ENFORCE_TENANT_BINDING=false`) — intended only for single-workspace
deployments — see the honest caveat under
[T2b](#t2b-cross-workspace-within-tenant-access).

### R2. Ingestion upload authentication (resolved)

**Resolved — `/upload` is authenticated and the service is network-internal by
default.** `POST /upload` carries the same `require_admin_token` dependency as
`/admin/*` (`services/ingestion/app.py:219`; constant-time compare
`admin/auth.py:52-54`, fail-closed 503 when the token is unset
`admin/auth.py:46-50`). Its `bot_tag` is pattern-validated route-side
(`app.py:210`, `app.py:223`) and OData-escaped again at the sink
(`services/ingestion/custom_rag.py:49-69`). In the shipped infra the ingestion
Container App's ingress defaults to **internal-only**
(`infra/main.bicep:74`, `infra/main.bicep:276`; Terraform mirror
`infra/terraform/variables.tf:96-105`, `infra/terraform/main.tf:242`). Only
`/health` (liveness) remains unauthenticated (`admin/auth.py:15`). Remaining
item, tracked under [Planned controls](#planned-controls-not-yet-shipped): the
admin token is still an interim static shared secret; the end state is the same
Azure AD RS256 JWT mechanism as QnA (`admin/auth.py:9-10`).

### R3. No startup assertion that `AZURE_TENANT_ID` is a concrete GUID

**Still open (QnA).** The cross-tenant guarantee
([T2](#t2-cross-tenant-data-access)) depends on the operator configuring a
concrete tenant GUID. A `common`/`organizations` misconfiguration would
silently weaken the issuer pin, and QnA has no guard to make this loud (no such
check exists in `services/qna/src/config/config.py`). The Teams adapter now
implements exactly this assertion for its own config — rejecting
`common`/`organizations`/`consumers` and non-GUID values at startup
(`services/teams-bot/teams_bot/config.py:51-66`) — so the fix is to adopt that
in-repo pattern in QnA.

### R4. QnA `bot_tag` is quote-escaped but not format-validated

**Still open, narrowed.** Unlike the admin and upload routes, the QnA path
applies no length/format regex to `bot_tag`
(`services/qna/src/utils/util.py:42`;
[T3](#t3-injection-odata-filter-injection)). The single-quote escaping
(`search_service.py:131-133`) prevents filter break-out, and — new since the
first draft — the default-ON tenant-binding guard rejects any `bot_tag` outside
the tenant's allowlist with a clean 403 before search
(`tenant_binding.py:159-163`), so a crafted/oversized value no longer reaches
the filter in a default deployment. What remains is **input hygiene for
explicitly opted-out deployments**: there, a crafted value is escaped, used,
and returns a confusing empty result instead of a clean 400. Fix: apply
`^[A-Za-z0-9_-]{1,128}$` on the QnA path.

### R5. `pip-audit` CI gating (resolved)

**Resolved — hard gate.** The CI security job runs `bandit` (gating,
`.github/workflows/ci.yml:98-99`) and `pip-audit` over both services'
requirements as a **hard gate with no `continue-on-error`**
(`ci.yml:104`, steps at `ci.yml:124-135`). A dependency CVE without a vetted
ignore now fails the build.

### R6. No token revocation / replay window

**Still open (inherent).** Stateless JWT auth has an inherent replay window
until expiry ([T5](#t5-token-replay--forgery)); expiry is enforced with a
10-second leeway (`token_validator.py:293`). Mitigated by short token lifetimes
configured in Azure AD; no application-side `jti` denylist exists.

---

## Summary

The strongest controls are **existing code**: RS256 JWT verification with a
fail-closed issuer pin (cross-tenant isolation), the **default-ON
`tid` → `bot_tag` tenant-binding guard** (within-tenant workspace isolation —
R1, resolved), admin-token auth on **every ingestion write path** with
internal-by-default ingress (R2, resolved), the email-claim requirement, the
P0-6 error envelope that never leaks exception text, constant-time admin-token
comparison, layered OData-injection defence on both services, throttled
JWKS refetch with a bounded negative cache, app-level rate limiting with
429 + `Retry-After`, and a **gating** bandit + pip-audit CI job (R5, resolved).
The Teams-bot identity spine — verify the Bot Framework signature first, derive
`bot_tag` server-side, fail closed — is **implemented** in
`services/teams-bot/`, with the live On-Behalf-Of exchange remaining a
loud-failing deployment seam. The remaining honest gaps are the missing QnA
startup assertion on `AZURE_TENANT_ID` (R3), QnA-side `bot_tag` format
validation (R4, narrowed), the interim admin shared secret pending Azure AD
JWT, and the inherent stateless-JWT replay window (R6) — documented as such so
this model stays honest.
