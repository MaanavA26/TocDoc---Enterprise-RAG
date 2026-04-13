# 10 — Product: Add admin APIs for index management and tenant operations

**Priority:** P1  
**Type:** Product operations / Admin tooling / Commercial readiness

## Problem

TocDoc currently focuses on ingestion and QnA, but it lacks an explicit administrative control surface for managing indexed content and tenant datasets. In a real client deployment, operators will need safe ways to inspect, reindex, delete, and monitor content without directly manipulating Azure Search by hand.

## Why this matters

- Operational teams need supported workflows, not hidden scripts.
- Managed-service delivery becomes harder if every maintenance action requires engineering intervention.
- Clients will expect controlled lifecycle operations once content changes or needs to be revoked.
- Admin tooling helps transform TocDoc from a backend demo into an operable product.

## Desired outcome

Provide a secure admin surface for managing document and tenant data lifecycle, with clear authorization boundaries and auditability.

## Scope

- Add endpoints or internal service operations for:
  - list indexed documents for a tenant
  - get document/index stats
  - delete by document
  - delete by tenant / bot
  - trigger reindex or refresh flows
- Design auth and authorization expectations for admin-only operations.
- Ensure these operations integrate with the deterministic document lifecycle model from the ingestion backlog.

## Implementation guidance

- Do not expose destructive operations without explicit auth and audit considerations.
- Keep API naming consistent with the product’s future admin model.
- Consider whether some operations belong in the ingestion service, a new admin service, or a shared platform layer.

## Deliverables

- admin-oriented API design
- implementation for core lifecycle operations
- authorization and audit hooks
- documentation and examples

## Acceptance criteria

- Operators can inspect and manage indexed content safely.
- Delete and reindex flows operate predictably.
- Admin operations are restricted and audited.
- Documentation explains intended use and security posture.

## Non-goals

- A full graphical admin portal. This issue is about backend admin capabilities first.

## Notes for Codex / Claude

Design this with future platform operations in mind. Avoid one-off helper endpoints that will not scale into a real product admin model.