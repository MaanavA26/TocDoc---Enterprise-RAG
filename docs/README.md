# TocDoc Documentation Index

This directory contains the design, reference, operations, and planning
documentation for TocDoc — a two-service enterprise RAG product (ingestion +
Q&A) deployed into a client's own Azure resource group.

For licensing terms, see the root [LICENSE](../LICENSE) (Business Source License
1.1). For shipped changes, see the root [CHANGELOG](../CHANGELOG.md).

## Architecture & product

- [ARCHITECTURE.md](ARCHITECTURE.md) — System overview: services, data flow, and
  the Azure components TocDoc builds on.
- [PRODUCT_TIERS.md](PRODUCT_TIERS.md) — Packaging tiers and the deployment
  operating model.
- [RESUME.md](RESUME.md) — Quick-start state snapshot for picking the project
  back up after a pause.

## Reference

- [API.md](API.md) — REST API reference for the Q&A and admin endpoints.
- [CONFIGURATION.md](CONFIGURATION.md) — Configuration and environment-variable
  reference.

## Operations & local development

- [OPERATIONS.md](OPERATIONS.md) — Operations runbook for running TocDoc in
  production.
- [LOCAL_DEV.md](LOCAL_DEV.md) — Local development quickstart.
- [deployment/INSTALLATION.md](deployment/INSTALLATION.md) — Client installation
  guide for deploying TocDoc into a client Azure resource group.
- [deployment/SMOKE_TEST.md](deployment/SMOKE_TEST.md) — End-to-end **live-Azure**
  deployment + smoke-test runbook (run this to validate a real deployment).
- [deployment/P3_ENABLEMENT.md](deployment/P3_ENABLEMENT.md) — Cutover guide for
  enabling the default-OFF P3 agentic layer.
- [deployment/CONTAINER_IMAGES.md](deployment/CONTAINER_IMAGES.md) — Pulling the
  published container images.

## Security, compliance & audit

- [../SECURITY.md](../SECURITY.md) — Security policy + the security-controls posture.
- [security/CODEQL_TRIAGE.md](security/CODEQL_TRIAGE.md) — CodeQL scan triage.
- [LICENSE_COMPLIANCE.md](LICENSE_COMPLIANCE.md) — Dependency license-compatibility
  audit for BSL-1.1 sellability (incl. the **PyMuPDF AGPL** blocker).
- [AUTONOMOUS_SESSION_LOG.md](AUTONOMOUS_SESSION_LOG.md) — Record of the autonomous
  audit/hardening run: every change with a one-line revert, council verdicts, and
  items held for the owner.

## Planning & tracker (`agent_plan/`)

The working plan and delivery tracker that drove the productization phases.

- [agent_plan/00_MASTER_TRACKER.md](agent_plan/00_MASTER_TRACKER.md) — Master plan
  and agent delivery tracker (merged-item discipline).
- [agent_plan/01_P0_HARDENING.md](agent_plan/01_P0_HARDENING.md) — Phase P0:
  security, correctness, and production hardening.
- [agent_plan/02_P1_ENTERPRISE.md](agent_plan/02_P1_ENTERPRISE.md) — Phase P1:
  enterprise feature completeness.
- [agent_plan/03_P2_DIFFERENTIATION.md](agent_plan/03_P2_DIFFERENTIATION.md) —
  Phase P2: product differentiation and commercial packaging.
- [agent_plan/04_AGENTIC_ROADMAP.md](agent_plan/04_AGENTIC_ROADMAP.md) — Phase P3:
  agentic AI layer (LangGraph).
- [agent_plan/05_CODEBASE_CONTEXT.md](agent_plan/05_CODEBASE_CONTEXT.md) —
  Codebase context for sub-agents.
- [agent_plan/06_TECH_LEAD_OPERATING_MODEL.md](agent_plan/06_TECH_LEAD_OPERATING_MODEL.md)
  — Tech lead operating model for the sub-agent workflow.
- [agent_plan/07_P2_P4_REFRESHED_PLAN.md](agent_plan/07_P2_P4_REFRESHED_PLAN.md) —
  Refreshed P2 & P4 specs and sequenced delivery plan.

## Phase 2 specs & decision records (`architect_phase_2/`)

Execution plans, workstream specifications, and architecture decision records
(ADRs) produced during the operability and control-plane phase.

- [architect_phase_2/00_PHASE_2_EXECUTION_PLAN.md](architect_phase_2/00_PHASE_2_EXECUTION_PLAN.md)
  — Phase 2 execution plan: operability, control plane, and product readiness.
- [architect_phase_2/01_ADMIN_API_SPEC.md](architect_phase_2/01_ADMIN_API_SPEC.md)
  — Workstream A: Admin API specification.
- [architect_phase_2/02_OBSERVABILITY_SPEC.md](architect_phase_2/02_OBSERVABILITY_SPEC.md)
  — Workstream B: observability specification.
- [architect_phase_2/03_DEPLOYMENT_VALIDATION_SPEC.md](architect_phase_2/03_DEPLOYMENT_VALIDATION_SPEC.md)
  — Workstream C: deployment validation specification.
- [architect_phase_2/04_BOT_TAG_DECISION_RECORD.md](architect_phase_2/04_BOT_TAG_DECISION_RECORD.md)
  — Decision record: `bot_tag` scope, naming, and product role.
- [architect_phase_2/05_PRODUCT_NEUTRAL_NAMING_POLICY.md](architect_phase_2/05_PRODUCT_NEUTRAL_NAMING_POLICY.md)
  — Product-neutral naming policy.
- [architect_phase_2/06_BACKLOG_HYGIENE_ARCHIVE_COMPLETED_ITEMS.md](architect_phase_2/06_BACKLOG_HYGIENE_ARCHIVE_COMPLETED_ITEMS.md)
  — Priority issue: clean completed productization backlog items.
- [architect_phase_2/07_P3_LANGGRAPH_ADR.md](architect_phase_2/07_P3_LANGGRAPH_ADR.md)
  — ADR: P3 LangGraph agentic layer.
- [architect_phase_2/08_P1_3_CONNECTORS_ADR.md](architect_phase_2/08_P1_3_CONNECTORS_ADR.md)
  — ADR: P1-3 connector ingestion.
- [architect_phase_2/09_P2_1_PAGE_CITATIONS_ADR.md](architect_phase_2/09_P2_1_PAGE_CITATIONS_ADR.md)
  — ADR: P2-1 page-level citations.
- [architect_phase_2/10_P4_1_TEAMS_BOT_ADR.md](architect_phase_2/10_P4_1_TEAMS_BOT_ADR.md)
  — ADR: P4-1 Microsoft Teams bot.

## Productization backlog (`productization_backlog/`)

The working issue pack that scoped TocDoc into a repeatable enterprise product.
See its own index for the full set of items.

- [productization_backlog/00_BACKLOG_INDEX.md](productization_backlog/00_BACKLOG_INDEX.md)
  — Backlog index and item descriptions (security, isolation, ingestion,
  retrieval, API, config, runtime, observability, admin, connectors, platform,
  quality, roadmap, and packaging).
