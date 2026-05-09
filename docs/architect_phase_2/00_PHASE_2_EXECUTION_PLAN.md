# TocDoc Phase 2 Execution Plan — Operability, Control Plane, and Product Readiness

## Purpose

This document starts the next TocDoc productization phase. The previous phase hardened the core platform: authentication, tenant isolation, concurrency safety, ingestion lifecycle, runtime posture, and Azure deployment. Phase 2 turns TocDoc from a deployable backend into an operable product that can be installed, supported, and eventually sold repeatedly.

## Current product state

TocDoc is now a sellable guided-deployment MVP for Azure-based enterprise document Q&A. It is not yet a fully self-serve product. The next phase should focus on operator control, visibility, validation, and client-facing ingestion readiness.

## Phase 2 goal

Make TocDoc supportable after deployment.

A client or delivery engineer should be able to answer:
- What documents are indexed?
- Which tenant/bot owns them?
- Did ingestion succeed or fail?
- Can I delete or reindex a document?
- Why did a QnA request return a given answer?
- Which sources were retrieved?
- Is the deployment configured correctly?

## Workstreams to start now

### Workstream A — Admin APIs

Owner: Codex / Claude implementation agent

Goal: Add a secure control-plane API for managing indexed documents and tenant data.

Start from:
- `docs/architect_phase_2/01_ADMIN_API_SPEC.md`

Primary backlog mapping:
- `docs/productization_backlog/10_PRODUCT_Add_admin_APIs_for_index_management_and_tenant_operations.md`
- `docs/productization_backlog/04_INGESTION_Add_deterministic_chunk_ids_and_reindex_delete_lifecycle.md`

### Workstream B — Observability baseline

Owner: Codex / Claude implementation agent

Goal: Add structured telemetry, request IDs, ingestion diagnostics, QnA retrieval diagnostics, and audit-friendly logs.

Start from:
- `docs/architect_phase_2/02_OBSERVABILITY_SPEC.md`

Primary backlog mapping:
- `docs/productization_backlog/09_OBSERVABILITY_Add_telemetry_audit_logs_and_operational_metrics.md`

### Workstream C — Deployment validation

Owner: Codex / Claude implementation agent

Goal: Add a deployment validation script and runbook checks so client installations fail early instead of failing at runtime.

Start from:
- `docs/architect_phase_2/03_DEPLOYMENT_VALIDATION_SPEC.md`

Primary backlog mapping:
- `docs/productization_backlog/12_PLATFORM_Add_IaC_CI_CD_and_repeatable_client_installation_assets.md`
- `docs/productization_backlog/13_QUALITY_Expand_test_strategy_coverage_and_release_gates.md`

### Workstream D — `bot_tag` product decision

Owner: Architect + implementation agent

Goal: Decide and document whether `bot_tag` remains the product’s tenant/bot scoping primitive, how it should be named, and how it should be enforced.

Start from:
- `docs/architect_phase_2/04_BOT_TAG_DECISION_RECORD.md`

Primary backlog mapping:
- `docs/productization_backlog/02_ISOLATION_Enforce_bot_tag_tenant_scoping_in_retrieval.md`
- `docs/productization_backlog/10_PRODUCT_Add_admin_APIs_for_index_management_and_tenant_operations.md`

## Recommended development order

1. Admin API read-only endpoints first.
2. Observability request/correlation ID middleware.
3. Admin destructive actions: delete document, delete tenant/bot.
4. Deployment validation script.
5. Ingestion job/status model.
6. `bot_tag` naming/contract cleanup.

## Merge discipline

Each PR should target only one workstream. Do not combine admin APIs, observability, and deployment validation in one large PR.

Expected PR structure:
- PR 1: Admin read-only APIs and tests.
- PR 2: Admin delete/reindex APIs and tests.
- PR 3: Observability middleware/log schema and tests.
- PR 4: Deployment validation script and docs.
- PR 5: `bot_tag` decision implementation, if any naming/API changes are approved.

## Definition of done for Phase 2

Phase 2 is complete when:
- operators can list, inspect, delete, and reindex documents
- every request has a correlation/request ID
- QnA responses can be debugged through retrieval diagnostics
- ingestion failures are observable and classifiable
- deployments have automated validation checks
- `bot_tag` is either retained and documented as a product primitive or replaced with a clearer equivalent

## Architect rule

Do not remove `bot_tag` without a replacement isolation primitive. Tenant/bot scoping is not optional. If renamed, it must remain enforced in ingestion, retrieval, admin APIs, and any future UI or connector flow.

Co-Authored by Maanav's Mac-Air
