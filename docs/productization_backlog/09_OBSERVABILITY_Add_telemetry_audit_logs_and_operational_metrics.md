# 09 — Observability: Add telemetry, audit logs, and operational metrics

**Priority:** P1  
**Type:** Operations / Supportability / Enterprise readiness

## Problem

The project has basic logging, but it does not yet present a full operational observability model. There is limited structured telemetry around request traces, ingestion outcomes, retrieval behavior, latency breakdowns, and audit-sensitive actions.

## Why this matters

- Enterprise deployments need visibility for support, incident response, and service improvement.
- Without operational metrics, it is hard to diagnose issues like poor retrieval, ingestion duplication, auth failures, or upstream Azure latency spikes.
- Auditability becomes important as soon as clients rely on document-grounded answers for internal decision support.

## Desired outcome

TocDoc should expose an observability layer that makes the system operable in real client environments. At minimum, operators should be able to answer: who called the service, what happened, how long it took, whether retrieval worked, and which source documents were involved.

## Scope

- Add structured request telemetry for ingestion and QnA.
- Add latency and outcome metrics for key stages.
- Add audit-relevant logs for admin operations, ingestion lifecycle actions, and auth events.
- Define guidance for Azure Monitor / Application Insights / Log Analytics integration.

## Implementation guidance

- Prefer correlation IDs across services.
- Separate user-facing IDs from internal diagnostic IDs when appropriate.
- Avoid logging sensitive raw content unnecessarily.
- Consider metrics such as request count, error rate, latency percentiles, chunk counts, and retrieval result counts.

## Deliverables

- structured telemetry model
- log field conventions
- instrumentation for key paths
- observability documentation and dashboard guidance

## Acceptance criteria

- Operators can trace an ingestion or QnA request through logs and metrics.
- Audit-sensitive actions are captured cleanly.
- Sensitive content exposure in logs is minimized.
- Documentation explains how to wire TocDoc into Azure-native monitoring.

## Non-goals

- A full custom observability UI. This item is about telemetry readiness and supportability.

## Notes for Codex / Claude

Build the foundations first. Good observability will make every later backlog item easier to verify and support.