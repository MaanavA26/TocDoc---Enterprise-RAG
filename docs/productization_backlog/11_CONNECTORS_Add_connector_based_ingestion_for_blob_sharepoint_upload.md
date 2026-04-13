# 11 — Connectors: Add connector-based ingestion for Blob, SharePoint, and managed upload flows

**Priority:** P1  
**Type:** Product capability / Ingestion UX / Commercial readiness

## Problem

The current ingestion API supports uploaded files and server-side file paths. That is useful for controlled development, but it is not yet the right operating model for a repeatable enterprise product. Client installations will need safer, more natural source integrations.

## Why this matters

- Enterprises commonly store documents in SharePoint, OneDrive, Blob Storage, network shares, or controlled upload locations.
- Asking operators to rely on raw server file paths creates deployment friction and weakens the product experience.
- Connector-based ingestion makes TocDoc easier to sell, easier to implement, and easier to operate.

## Desired outcome

TocDoc should support connector-oriented ingestion patterns where documents are pulled from governed source systems or uploaded into a managed landing zone, rather than relying on ad hoc local server paths.

## Scope

- Define ingestion source types such as manual upload, Azure Blob Storage, and SharePoint.
- Standardize source metadata captured for each document.
- Design a connector abstraction that can be extended later.
- Decide how ingestion jobs, polling, or event-driven triggers will work for each source type.

## Implementation guidance

- Start with the most commercially useful connectors first.
- Keep source identity canonical so lifecycle operations can work consistently across connectors.
- Separate source acquisition from chunking/indexing logic where possible.
- Document authentication expectations clearly for each connector type.

## Deliverables

- connector abstraction or ingestion source model
- at least one or two practical connector implementations
- metadata model for source tracking
- documentation for setup and operational behavior

## Acceptance criteria

- TocDoc can ingest from at least one enterprise-native source without relying on a raw local path.
- Source metadata is captured consistently.
- The architecture supports adding more connectors later.
- Documentation makes the ingestion model understandable to operators and client teams.

## Non-goals

- Supporting every enterprise content system in one release. Focus on the highest-value connectors first.

## Notes for Codex / Claude

This is a strategic product feature. It improves sellability directly because clients care how quickly their real documents can be connected with minimal custom work.