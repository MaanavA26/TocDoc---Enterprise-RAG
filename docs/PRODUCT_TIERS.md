# TocDoc — Packaging Tiers & Deployment Operating Model

> **Status:** DRAFT for review (P2-2). Maps product tiers to **shipped vs planned** capability so any backlog item traces to a tier, sales can explain the product without a demo, and procurement can understand the purchase from docs alone.

## Deployment posture (non-negotiable)

TocDoc deploys **into the client's own Azure resource group / subscription**. The client owns all data, Azure resources, and compute. This is a deployable product, **not** shared multi-tenant SaaS — `bot_tag` isolation (P0-2) is the within-deployment tenancy boundary, enforced at the search layer and on every admin/connector operation.

## Tiers

Each capability is tagged **[shipped]** (on `main` today) or **[planned]** (roadmap item, with its backlog ref).

| Tier | Included | Adds over previous | Hosting |
|------|----------|--------------------|---------|
| **Starter** | Manual PDF upload + QnA; hybrid retrieval; JWT/RS256 auth **[shipped P0-1]**; `bot_tag` tenant isolation **[shipped P0-2]**; structured error envelope + `X-Request-ID` **[shipped P0-6]**; request-ID + stage-level observability **[shipped P1-1]**; Bicep install + runbook **[shipped P1-4]**; post-deploy validation script **[shipped P1-4/Phase-2C]** | — | Azure Container Apps |
| **Standard** | Everything in Starter | Read-only + destructive admin API (list/inspect/delete/reindex-stub, `bot_tag`-scoped) **[shipped P1-2]**; connector ingestion — Blob **[shipped P1-3]**, SharePoint + operator sync trigger **[in review P1-3]**; audit-grade structured logs **[shipped P1-1]** | ACA or App Service |
| **Enterprise** | Everything in Standard + SLA / support | Semantic reranking **[in review P2-1]** + page-level citations **[planned P2-1]**; agentic layer (router / map-reduce / ReAct / self-critique / memory / SSE) **[planned P3]**; Microsoft Teams bot **[planned P4-1]**; RAGAS quality evaluation **[planned P4-2]**; AKS/Helm packaging **[planned P4-3]**; Python client SDK **[planned P4-4]** | AKS or ACA |

## Capability → tier traceability

- **Retrieval quality** (semantic rerank, page citations, P2-1) is an **Enterprise** differentiator; the typed `CitationMap` success contract it introduces is also the prerequisite for the SDK (P4-4) and clean RAGAS contexts (P4-2).
- **Connectors** (P1-3) move ingestion from manual upload to automated Blob/SharePoint sync — the **Standard**-tier operability line.
- **Agentic layer** (P3) ships dark-launched behind a default-OFF `QNA_AGENT_ENABLED` flag, so it can be enabled per-deployment for **Enterprise** without affecting Starter/Standard behavior.
- **Admin API** (P1-2) is what makes TocDoc *operable* (inspect/manage indexed data) rather than just deployable — the **Standard** baseline.

## Delivery operating model

1. **Install engagement** — provision into the client subscription via the Bicep template + installation runbook (P1-4); run `scripts/validate_deployment.sh` (Phase-2 C) to confirm resources, Container App health probes, and Key Vault wiring before handover.
2. **Configure** — set canonical UPPER_SNAKE env vars (P0-7); wire secrets through Key Vault; enable optional capabilities by config flag (e.g. `AZURE_SEARCH_SEMANTIC_CONFIG=mySemanticConfig` for semantic rerank; connector env config for Blob/SharePoint sync).
3. **Operate** — admin API for index/tenant management; structured logs queryable in Azure Container Apps / Application Insights (P1-1).
4. **Upgrade** — image redeploy (ACA) or `helm upgrade` (AKS, when P4-3 ships). The CI gate (P1-5) gates every change before release.
5. **Support** — managed-support add-on at the Enterprise tier.

## Per-tier Azure footprint (indicative)

| Resource | Starter | Standard | Enterprise |
|----------|---------|----------|------------|
| Azure AI Search | Basic | Standard **S1+** (semantic rerank requires S1+) | Standard S1+ / S2 |
| Azure OpenAI | chat + embeddings deployments | + reranker/verifier model deployments (P3) | + higher quota |
| Compute | Container Apps | ACA / App Service | AKS or ACA |
| Cosmos DB | — | — | conversation memory (P3-5) |
| Key Vault, Document Intelligence, App Insights | ✓ | ✓ | ✓ |

## Open questions for the architect

1. Confirm the Starter/Standard/Enterprise split and which capabilities are **contractual** vs **best-effort** per tier.
2. Is semantic reranking a paid **Enterprise** gate, or available to Standard where the client already runs an S1+ Search tier?
3. Connector ingestion at **Standard** vs **Enterprise** — does SharePoint (Graph app registration burden) belong only at Enterprise?
4. Support-tier boundaries (response times, on-call) for the Enterprise SLA.
