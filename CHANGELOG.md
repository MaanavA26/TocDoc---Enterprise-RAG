# Changelog

All notable changes to TocDoc are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project aims to adhere to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

No versioned releases have been tagged yet; all shipped work to date is listed
under `[Unreleased]`.

## [Unreleased]

### Added

- **Authentication & security (P0).** Strict Azure AD RS256 JWT signature
  validation with dual issuer support (v1 + v2); `/health` liveness probe
  exempted from auth.
- **Tenant isolation (P0).** Enforced `bot_tag` tenant scoping throughout
  retrieval, with an OData injection guard and `bot_tag` validation in the
  pipeline.
- **Ingestion correctness (P0).** Deterministic, tenant-safe chunk IDs,
  token-aware chunking, expanded index schema fields, and a stale-document
  cleanup / reindex lifecycle.
- **API error contract (P0).** Structured error responses with an `X-Request-ID`
  on every response, plus request validation and a typed success-response schema.
- **Observability (P1).** Request-ID middleware, a structured logging helper, and
  pipeline stage-level events for both Q&A and ingestion.
- **Admin API (P1).** Read-only admin endpoints for index and tenant inspection,
  plus destructive document/tenant endpoints and a reindex entry point.
- **Connectors (P1).** A core ingestion abstraction with Blob Storage and
  SharePoint connectors, an operator sync-trigger endpoint, and persisted run
  status with `run_id` log correlation.
- **Infrastructure & delivery (P1).** Azure Bicep IaC templates, a client
  installation runbook, an automated post-deploy validation script, and GitHub
  Actions CI test gates (extended to cover the Python SDK and the evaluation
  harness).
- **Retrieval quality (P2).** Config-gated Azure semantic reranking with a hybrid
  retrieval fallback.
- **Page citations (P2).** A backward-compatible page-level citation contract
  (`CitationMap`) wired through retrieval and the API response.
- **Product packaging (P2).** Defined packaging tiers and a deployment operating
  model.
- **Agentic layer (P3, default-OFF / dark).** A LangGraph-based agentic layer
  shipped disabled by default behind `QNA_AGENT_ENABLED`: scaffold, a
  structured-output router/classifier, and a map-reduce summarizer node. The
  layer is dark — it does not affect the default request path unless explicitly
  enabled.
- **Python SDK (P4).** A Python client SDK for the Q&A API, later extended with an
  async client and admin-API methods.
- **Evaluation (P4).** A RAGAS evaluation harness for Q&A quality, with baseline
  comparison, threshold gating, and a richer report.
- **Microsoft Teams bot (P4).** A Bot Framework adapter with server-side
  `bot_tag` resolution and adaptive cards.
- **Helm chart (P4).** A Helm chart for AKS deployment, with chart-lint and
  Teams-bot test jobs in CI.
- **Documentation.** Architecture overview, REST API reference, configuration /
  environment-variable reference, operations runbook, local-development
  quickstart, per-service READMEs, packaging-tier docs, architecture decision
  records, and planning trackers.
- **Governance.** Public-repo governance files (CODEOWNERS, contributing guide,
  PR/issue templates), an automated review baseline, and CodeQL code scanning.
- **SDK depth.** A `tocdoc` command-line interface and connector sync-trigger /
  run-status methods on the admin client.
- **Terraform module.** An `infra/terraform/` (azurerm) deployment path mirroring
  the Bicep templates, for Terraform-based self-hosting.

### Changed

- **Configuration.** Normalized Q&A environment-variable naming to canonical
  `UPPER_SNAKE_CASE` and aligned the root `.env.example` to the canonical names.
- **Runtime hardening.** Non-root containers, configurable CORS, and
  env-controlled logging defaults suited to a cloud-native deployment.
- **Concurrency.** Removed global request state from the Q&A pipeline to make
  concurrent requests safe.
- **Naming.** Completed the product-neutral naming pass and adopted a
  product-neutral naming policy across code and docs.
- **Runtime → Python 3.12.** Bumped both services (CI + Dockerfiles) from Python
  3.10 to 3.12, unblocking the modern dependency stack.
- **LangChain 1.x.** Upgraded both services to the langchain 1.x line (with
  langgraph 1.x); the evaluation harness pins its own ragas-compatible stack
  independently.
- **Fail-closed defaults (multi-tenant safety).** Workspace tenant-binding
  enforcement now defaults ON (operators configure a tenant→`bot_tag` allow-list);
  the ingestion `/upload` endpoint now requires an admin token; and the ingestion
  service's ingress is internal by default.
- **Rate limiting.** Added application-level rate limiting to the `/qna` and
  `/upload` endpoints (HTTP 429 + `Retry-After`).

### Fixed

- Coordinated dependency upgrades to address known CVEs (including a
  FastAPI / Starlette / python-multipart bump and other low-risk fixes).
- A security-audit remediation pass: closed an unauthenticated injection vector
  and a path-traversal on the ingestion upload path, fixed a connector
  single-flight lock that did not serialize across runs, moved an
  event-loop-blocking call off the hot path, batched/validated the search upsert
  so partial failures are no longer reported as success, and restored `aiohttp`
  to a non-vulnerable pinned version.

### Security

- Source-available licensing under the Business Source License (BSL) 1.1, with
  public-readiness governance and documentation.
- Hardened the multi-tenant isolation model to fail closed by default, added
  JWKS negative-caching and outbound client timeouts, and removed user queries,
  answers, and conversation content from logs.
- Enabled CodeQL code scanning; CI now hard-gates `pip-audit` and enforces a
  test-coverage floor.
