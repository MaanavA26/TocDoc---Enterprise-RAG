# 02 — Isolation: Enforce `bot_tag` tenant scoping in retrieval

**Priority:** P0  
**Type:** Multi-tenancy / Data isolation / Production blocker

## Problem

The ingestion flow stores `bot_tag` on indexed chunks, but the QnA retrieval path currently filters only on `fr_tag`. In a shared index, this creates a cross-tenant leakage risk: one tenant’s retrieval request can potentially surface another tenant’s indexed content.

## Why this matters

- This is a confidentiality and trust issue, not just a search-quality issue.
- It weakens TocDoc’s core commercial promise of isolated client deployments and bot-level separation.
- Even if the first commercialization path is dedicated deployment per client, the product still needs internal isolation correctness.
- A single leakage incident would be far more damaging than a typical bug.

## Desired outcome

Every retrieval request must enforce both retrieval mode and tenant/bot scope. The search layer should make it impossible to query outside the intended `bot_tag` boundary unless an explicitly privileged admin flow is being used.

## Scope

- Pass `bot_tag` from the API layer into the search layer.
- Enforce `fr_tag` + `bot_tag` filter composition in the search client.
- Review whether index design should also include `client_id`, `dataset_id`, or `environment` as future-proof fields.
- Decide whether dedicated indexes per client remain the default commercial posture or whether shared-index isolation is expected to be fully supported.

## Implementation guidance

- Treat the filter as mandatory, not optional.
- Validate empty or missing `bot_tag` as a request error before search execution.
- Keep the search abstraction generic enough to add more filter fields later.
- Update tests so retrieval fails or returns no results when `bot_tag` does not match indexed data.

## Deliverables

- updated API-to-pipeline propagation of `bot_tag`
- updated search service filter logic
- tests covering correct isolation and negative cases
- documentation stating the isolation model clearly

## Acceptance criteria

- QnA requests only retrieve documents for the provided `bot_tag`.
- Requests with the wrong `bot_tag` do not surface other tenants’ data.
- Search logic remains compatible with both `read` and `layout` modes.
- README and deployment guidance explain whether TocDoc recommends one index per client or a safely shared index.

## Non-goals

- Full RBAC for administrators across multiple tenants. This item is specifically about retrieval-side data isolation.

## Notes for Codex / Claude

Do not stop at adding one filter string. Trace the request end to end and make sure the isolation contract is reflected in API validation, search logic, tests, and documentation.