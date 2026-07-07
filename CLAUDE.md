# CLAUDE.md — Agent Operating Guide for TocDoc

Operational knowledge for AI coding sessions working in this repo. Everything here
was ground-truthed against the code on 2026-07-07. When this file and a doc under
`docs/` disagree, check the Docs trust map below — several docs are known-stale.

## What this repo is

TocDoc is a **BSL-1.1 source-available, multi-tenant, document-grounded RAG
platform** (enterprise Q&A with citations), deployed **into each client's own
Azure subscription** (Azure OpenAI + AI Search + Document Intelligence + Key
Vault). Two stateless FastAPI services — **ingestion** (parse → chunk → embed →
index) and **QnA** (authenticate → retrieve → answer with citations) — plus a
Python SDK, a Teams bot, an admin web SPA, a RAGAS eval harness, and IaC
(Bicep/Terraform/Helm). Solo-owner project (GitHub `MaanavA26`), built and
maintained largely by AI agents under a documented operating model.

**Status snapshot:** P0 + P1 fully shipped · P2 partial (semantic rerank shipped;
page-level citations are a wire-contract only — see Half-wired below) · **P3
agentic layer built but DARK** behind default-OFF flags, pending architect ADR
sign-off, not validated end-to-end · P4 shipped (SDK, Teams bot, Helm, Terraform,
admin SPA, eval harness). Runtime **Python 3.12**, langchain 1.x line. Everything
is hermetically tested (~740 tests across the 7 CI suites); **nothing has ever
been validated against live Azure** — `docs/deployment/SMOKE_TEST.md` is the gate
before any client traffic. No release tag has ever been cut (`release.yml` and
GHCR images are an unexercised path).

## Read-first pointers

1. `docs/RESUME.md` — the canonical resume-from-cold doc (most current).
2. `docs/agent_plan/00_MASTER_TRACKER.md` — **trust only the top section**; the
   lower per-phase tables are stale and contradict it.
3. `docs/ARCHITECTURE.md` — canonical system tour (current).
4. `docs/agent_plan/06_TECH_LEAD_OPERATING_MODEL.md` — how work is organized
   (personas, per-PR workflow, defensive coding bar).

## Docs trust map

**Current / authoritative:** RESUME.md · ARCHITECTURE.md · API.md ·
OPERATIONS.md · SECURITY.md · CHANGELOG.md ·
deployment/{SMOKE_TEST,P3_ENABLEMENT,INSTALLATION,CONTAINER_IMAGES}.md ·
security/CODEQL_TRIAGE.md · MASTER_TRACKER **top section only**.

**Known-stale (verify before citing; fix opportunistically):**
- `docs/CONFIGURATION.md` — lags the code by ~14 live env vars, including the
  security-critical `QNA_ENFORCE_TENANT_BINDING` / `QNA_TENANT_BOT_TAG_MAP`
  pair, all four `QNA_AGENT_*` flags, the cache/rate-limit/concurrency vars,
  and `APPLICATIONINSIGHTS_CONNECTION_STRING`. The fail-closed pair is
  documented in RESUME.md; the flag table in this file is code-verified.
  (`services/qna/.env.example` has the same gap.)
- `docs/PRODUCT_TIERS.md` — draft from P2-2; marks shipped things (SharePoint
  connector, semantic rerank, Teams bot, RAGAS, Helm, SDK) as planned/in-review
  AND overstates "audit-grade structured logs" (there is no audit store; App
  Insights isn't wired by default). Needs regeneration before any sales use.
- `docs/LICENSE_COMPLIANCE.md` — verdict still correct (PyMuPDF AGPL blocker
  stands) but §2 claims "no web/ directory / no Node surface"; `web/` exists now
  and has never been license-audited.
- `docs/agent_plan/04_AGENTIC_ROADMAP.md`, `05_CODEBASE_CONTEXT.md`, ADR
  `07_P3_LANGGRAPH_ADR.md` — cite langchain 0.3.x / langgraph 0.2.x era pins;
  actual: langchain 1.3.11 / langgraph 1.2.7.
- ADR headers `07`, `08`, `10` still say "DRAFT … not yet implemented" — 08
  (connectors) and 10 (Teams bot) are SHIPPED; 07 (P3) is built-dark pending
  enable sign-off.
- `services/qna/requirements.txt` header comment cites old versions — trust the
  pins, not the comment.
- The "65 findings, 5 HIGH" figure in RESUME.md is an inferred aggregate: a
  36-finding security audit + a 29-finding autonomous hardening run
  (`docs/AUTONOMOUS_SESSION_LOG.md`). No single audit found 65.

## Repo map

```
services/qna/          QnA service (port 5500, root_path /qna) — 260 tests
services/ingestion/    Ingestion service (port 5501, root_path /upload_pipeline) — 247 tests
services/teams-bot/    Bot Framework adapter (aiohttp, port 3978) — 35 tests
clients/python/        tocdoc-sdk: sync/async clients, admin client, CLI, LangChain retriever — 86 tests
web/                   Admin dashboard SPA (React 18, Vite 8, Vitest 4, Node 22.13 in CI) — 10 tests
eval/                  RAGAS eval harness — DELIBERATELY FROZEN on langchain 0.3.x (see below) — 49 tests
tests/integration/     Hermetic e2e: real apps via TestClient, Azure mocked at the seam — 35 tests
infra/                 main.bicep + terraform/ mirror (Container Apps, Key Vault w/ purge protection)
charts/tocdoc/         Helm chart (AKS alternative to Container Apps)
loadtest/              Locust scenarios + helper tests
docs/                  MkDocs site (deployed to GitHub Pages by docs.yml)
scripts/               validate_deployment.sh etc. (shellcheck-gated)
```

## Architecture facts agents get wrong

- **Wire paths include the root_path**: `POST /qna/qna`, `POST /qna/qna/stream`
  (SSE), `GET /qna/health` · ingestion `POST /upload_pipeline/upload`, admin API
  under `/upload_pipeline/admin/*`, `GET /upload_pipeline/health`.
- **QnA pipeline**: optional answer cache (default OFF, keyed per bot_tag,
  history NOT in key) → LLM rephrase (best-effort; greeting/followup detection)
  → embed → hybrid search (BM25 + HNSW vector, TOP_K=20, `fr_{mode}` filter,
  optional L2 semantic rerank via `AZURE_SEARCH_SEMANTIC_CONFIG`) → grounded
  answer → citation resolution to `{filename: filepath}`.
- **Ingestion**: `document_id = sha256(bytes)[:16]`; chunk id
  `{bot_tag}_{document_id}_{fr_mode}_{index:05d}`; `read` mode = 500-token/50-
  overlap sliding window (tiktoken cl100k), `layout` = MarkdownHeaderTextSplitter
  then the same token splitter per section; upsert-then-prune keeps the index
  consistent on re-upload. PDF parsing is **Azure Document Intelligence**
  (`prebuilt-read`/`prebuilt-layout`); PyMuPDF (`fitz`) is used ONLY for
  `doc.page_count` (this one call is the AGPL blocker). Non-PDF formats (docx,
  pptx, html, md, txt) via `loaders.py` registry.
- **Auth**: QnA = Azure AD JWT middleware, **RS256-only** against tenant JWKS
  (`algorithms=["RS256"]` hardcoded in `token_validator.py`; `require_aud`;
  fail-closed). Tenant binding (`QNA_ENFORCE_TENANT_BINDING`, default ON,
  fail-closed 403-before-search) maps `tid → allowed bot_tags` via
  `QNA_TENANT_BOT_TAG_MAP`. Ingestion `/upload` + all `/admin/*` require
  `X-Admin-Token` (constant-time compare; unset token env → 503 refuse).
- **P3 agentic seam**: `app.py` checks `is_agent_enabled()` per request →
  `src/agents/router.py` LangGraph graph (classifier → standard|map_reduce|react
  → verifier). Flags: `QNA_AGENT_ENABLED` master + `QNA_AGENT_MAP_REDUCE`,
  `QNA_AGENT_REACT`, `QNA_AGENT_VERIFY` (all default OFF, read live
  per-request). All nodes are best-effort → collapse to the standard route.
  Flag OFF ⇒ byte-identical legacy path.
- **Observability**: structured `log_event` JSON + `X-Request-ID` threading in
  both services. Ingestion has OpenTelemetry wired but **inert** unless
  `APPLICATIONINSIGHTS_CONNECTION_STRING` is set (spans-only, query strings
  scrubbed). **QnA has no OTel at all.** Don't claim tracing exists by default.

## Half-wired — do NOT claim these work

1. **`page_citations` is dead on the wire.** The response contract shipped
   (PR #76) and search selects `page_number`, but ingestion never writes
   `page_number` on any chunk — so the field is always absent. Populating it
   needs the page-citation reindex (ADR 09, pending two spikes + sign-off).
2. **Agentic rich outputs are discarded.** `agentic_generate_answer` maps final
   state to `{answer, citation}` only; `reasoning_trace`, `verified`,
   `unsupported_claims`, `retrieved_chunks` are computed then dropped. The
   verifier's groundedness verdict never reaches the client.
3. **`/qna/qna/stream` always bypasses the agentic seam** (deliberate; documented
   in code) — SSE is legacy-pipeline-only regardless of flags.
4. **`POST …/reindex` is a documented 501 stub** (no source persistence).
5. **QnA root `GET /qna/` advertises `"docs": "/api/v1/docs"`** — wrong; real
   docs URL is `/qna/docs`.
6. **`pypdf`, `pytesseract`, `Pillow` are pinned in ingestion but imported
   nowhere** — there is no OCR path and no pypdf parsing (pypdf is the intended
   AGPL-swap target, staged but unwired).
7. Rate limiting and concurrency caps are **per-process**; connector run-status
   and single-flight locks are **in-process and lost on restart**. Multi-replica
   deployments need ingress-level limits.

## Conventions (mandatory)

- **Branch-first, always.** Never commit to `main` directly, even for docs.
  One workstream = one branch = one PR. Branch prefixes: `feat/ fix/ chore/ docs/ ci/`.
- **Conventional Commits** (`feat(scope): …`, `fix(deps): …`, etc.).
- **Dual-persona commit footer** (repo convention, see operating-model doc):
  implementation/tech-lead work ends with `Co-Authored by Maanav's Mac-Pro`;
  architect/spec work ends with `Co-Authored by Maanav's Mac-Air`. Add your own
  `Co-Authored-By: Claude …` line after it. Mac-Air (the architect persona,
  ChatGPT) reviews staged PRs; Mac-Pro is the dev side.
- **Merge gate:** CODEOWNERS (`@MaanavA26`) + GitHub's self-approval block means
  owner-authored PRs merge with `gh pr merge --squash --admin`. `--admin`
  bypasses required checks, so the binding rule is procedural:
  **`--admin` only when every required check is green on the PR's CURRENT head
  SHA — never past a red, missing, or stale-refuted check.**
- **A recorded green is not a current green.** If a pip-audit-relevant advisory
  landed after a PR's last CI run, re-run (`@dependabot rebase` / empty push)
  before merging. Watch main's own post-merge run to completion — ci.yml's
  `cancel-in-progress` cancels intermediate main runs during merge trains.
- **Claude self-merge policy** (owner instruction): self-merge only when
  really important, 100%-certain, or many follow-ups depend on it; otherwise
  stage the PR for Mac-Air review. Routine green Dependabot merges are fine
  once explicitly asked to clear the queue.
- **Council review**: substantive diffs get independent fresh-context agent
  review (red-team) before merging. The operating-model doc details the
  tech-lead → dev-agent → reviewer-agent workflow and the defensive coding bar
  (no bare except, never leak exception text, never log secrets/JWTs/answers,
  truncate logged strings ≤200 chars, escape OData, paginate Search via
  `.by_page()`, wrap sync Azure SDK calls in executors).

## CI: 13 required checks + traps

Required on `main` (branch protection, hardened 2026-06-12): the 12 `ci.yml`
contexts — `lint (ruff)`, `security (bandit + pip-audit)`, `bicep build`,
`helm lint`, `shellcheck`, `test (qna|ingestion|sdk|eval|teams-bot|integration|loadtest)`
— plus `analyze (python)` from `codeql.yml`. Advisory (NOT required, step-skip
via dorny/paths-filter): `docker build (qna|ingestion)`, `web (build/test)`.

Traps:
- **Never rename a `ci.yml` job or add workflow-level path filters** without
  updating branch protection in the same change — PRs strand on
  "Expected — waiting" forever (warning banner at top of ci.yml).
- **`security` is a HARD gate**: bandit + `pip-audit` over the two SERVICE
  requirements files only (not eval/docs/sdk/teams-bot). Allowlist is empty; a
  new advisory reds every open PR simultaneously (and main latently). Fix =
  a lockstep bump PR first, then rebase everything else.
- **`test (integration)` installs BOTH services' requirements into ONE env** —
  see Dependabot rule 1 below. Also true locally.
- Coverage floor: `--cov-fail-under=60` on qna + ingestion (ratchet, not target).
- GitHub's *default* CodeQL setup may also run and fail in ~3s (conflicts with
  the advanced setup); it is not a required check — the real one is
  `analyze (python)`. Disable-in-settings is the standing fix (UI-only).

## Dependabot (two hard rules + config facts)

1. **Shared pins move in lockstep or not at all.** A dep pinned in both services
   bumped in only one file ⇒ `ResolutionImpossible` in `test (integration)` by
   construction. Per-service Dependabot PRs for shared deps can NEVER pass;
   supersede them with one coordinated both-services PR (#229/#260/#263/#267
   precedent), then close them. eval/, web/, actions, teams-bot surfaces are
   exempt (own closures): green CI ⇒ merge.
2. **Stale-green rule** (see Merge gate above).

Config (`.github/dependabot.yml`): weekly Monday; grouped families (langchain,
fastapi-stack, azure, minor-patch). Deliberate ignores: `pydantic_core`
(lockstep with pydantic), `websockets` major (langgraph-sdk cap), the whole
OpenTelemetry family in ingestion (pinned by azure-monitor-opentelemetry), and
in eval/ the entire langchain/langsmith/openai/tiktoken/ragas line (frozen).
**Unmanaged surfaces (known gap):** no npm entry for `web/`, no pip entry for
`services/teams-bot`, `loadtest/`, `tests/integration/` — security-only PRs
appear for web, nothing for the others. Structural fix candidate: a single
multi-directory pip entry (`directories: [/services/qna, /services/ingestion]`)
would make Dependabot itself emit lockstep PRs — owner call.

## Testing (Python 3.12 everywhere)

```bash
pip install -r services/qna/requirements.txt pytest pytest-asyncio \
  && pytest services/qna/test   # 260 tests — test deps are NOT in qna's requirements.txt (CI installs them)
pip install -r services/ingestion/requirements.txt -r services/ingestion/requirements-dev.txt \
  && pytest services/ingestion/test                                               # 247 tests
pip install -e "clients/python[dev]" && pytest clients/python/tests               # 86 tests
(cd services/teams-bot && pip install -r requirements-dev.txt && pytest)          # 35 tests
pip install -r eval/requirements.txt && pytest eval  # own venv! frozen 0.3.x     # 49 tests
pip install -r services/qna/requirements.txt -r services/ingestion/requirements.txt \
  -r tests/integration/requirements.txt && pytest tests/integration               # 35 tests
cd web && npm ci && npm run build && npm run typecheck && npm run lint && npm test
```

- **eval/ is deliberately decoupled**: ragas 0.4.3 imports a class removed in
  langchain-community 0.4+, so eval pins langchain 0.3.x + langsmith 0.3.x in
  its own venv. NEVER re-add `-r ../services/qna/requirements.txt` there; never
  "fix" its old pins. Unfreeze condition: ragas supports langchain-core ≥ 1.
  Its open security alerts are proposed for dismissal (CI-only, vulnerable
  paths unreachable, no secrets in eval CI) — the actual ledger dismissal is a
  pending owner call, not yet recorded anywhere in-repo.
- The dev pip proxy can lag public PyPI — **CI (public PyPI) is the source of
  truth, not a local venv resolve.**
- Intel-Mac: `cryptography` 49.x has no x86_64 macOS wheels (Rust + OpenSSL
  needed to build). This dev box is arm64; CI/containers unaffected.

## Security posture facts (save yourself the re-derivation)

- ecdsa alert #226 (GHSA-wj6h-64fc-37mp, HIGH, no fix exists): **benign here** —
  qna verifies RS256 only; ecdsa is an unconditional python-jose transitive; the
  GHSA itself says verification is unaffected. Dismissal-ready ("vulnerable code
  not in use"); real remediation = python-jose → PyJWT migration (scheduled
  hardening, not urgent). pip-audit does NOT fire on it (GitHub-alert-only).
- langsmith advisories (June 2026 family): the vulnerable `TracingMiddleware` is
  mounted nowhere in this repo — deployed images were never exploitable; those
  bumps were gate hygiene.
- `requirements-docs.txt` alerts on the Security tab are mis-attributed noise
  (mkdocs-only manifest) — pre-known, ignore.
- Never log: secrets, JWTs, raw queries/answers/document text (>200-char
  truncation is enforced in `log_event`).

## Open decisions / owner-only calls (do not action unilaterally)

1. **PyMuPDF AGPL** — sellability blocker. Fix paths: buy Artifex license, or
   swap the single `fitz` `page_count` call to the already-pinned `pypdf` and
   validate. (`docs/LICENSE_COMPLIANCE.md`)
2. **P3 enablement** — needs architect sign-off on ADR 07; enable via
   P3_ENABLEMENT.md staging sequence only. P3 is unvalidated end-to-end.
3. **Page-citation reindex** — ADR 09, two spikes pending.
4. **BSL placeholders** — LICENSE Licensor = `TocDoc` (needs legal entity) and
   Change Date = `2030-06-05` (needs confirmation) before going public.
5. **Live-Azure smoke** — SMOKE_TEST.md has never been run; required before any
   client traffic.
6. Alert-ledger dismissals (ecdsa #226, eval family), teams-bot pip-audit/
   Dependabot gate gap, Dependabot multi-directory restructuring, go-public
   scrub decisions — all flagged, all owner calls.

## Session hygiene

- `.claude/` shows untracked at repo root — expected, leave it.
- Worktrees live under `Documents/Project/TocDoc-worktrees/` (max two parallel).
- No live Azure calls, no production execution from sessions; tests run in CI.
- Update `docs/RESUME.md` when you change operator-relevant behavior — it is
  the canonical hand-off document and MUST stay accurate (docs-outrun-code has
  been called out as this repo's biggest credibility risk).
