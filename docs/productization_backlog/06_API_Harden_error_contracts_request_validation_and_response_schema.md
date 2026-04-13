# 06 — API: Harden error contracts, request validation, and response schema

**Priority:** P0  
**Type:** API design / Client integration / Production blocker

## Problem

The current API layer mixes framework-level HTTP errors with pipeline-level fallback payloads. In some failure scenarios the system can return an answer-like object with embedded error details instead of surfacing a clearly modeled API failure. The contract is functional for experimentation but too ambiguous for client applications and enterprise integrations.

## Why this matters

- Frontend and orchestration layers need predictable status codes and response shapes.
- Operational monitoring is much easier when error conditions are explicit.
- Ambiguous response behavior complicates debugging, support, and SLAs.
- Sellable products need stable contracts, not just working handlers.

## Desired outcome

Both services should expose explicit request and response contracts with stable validation rules, structured success responses, and structured failure responses.

## Scope

- Review request validation behavior for ingestion and QnA.
- Review HTTP status code mapping for user errors, auth errors, upstream dependency failures, and internal processing failures.
- Define a standard error payload shape.
- Review whether response models should include request IDs and optional diagnostics in a safe, controlled way.

## Implementation guidance

- Avoid leaking raw exception text in client-facing responses.
- Preserve enough server-side logging context for support.
- Ensure response models are documented in FastAPI OpenAPI output.
- Decide whether the product should version its APIs now or after the next major contract change.

## Deliverables

- explicit Pydantic response models for success and error cases
- standardized error handling strategy
- updated API docs and examples
- tests for representative validation and failure scenarios

## Acceptance criteria

- Success and error responses follow documented schemas.
- 4xx and 5xx behavior is intentional and consistent.
- Client-facing responses do not leak sensitive internal exception details.
- API docs reflect actual behavior.

## Non-goals

- Full frontend redesign. This item is about backend contract quality.

## Notes for Codex / Claude

This is a product API hardening task, not a cosmetic cleanup. Think like an external integrator consuming TocDoc as a stable service.