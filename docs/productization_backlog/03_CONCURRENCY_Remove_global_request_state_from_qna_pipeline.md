# 03 — Concurrency: Remove global request state from the QnA pipeline

**Priority:** P0  
**Type:** Correctness / Concurrency / Production blocker

## Problem

The QnA flow uses a module-level global (`bot_queries`) to pass request history from the FastAPI layer into the pipeline. This is unsafe in any concurrent server environment. When multiple requests arrive around the same time, one request can overwrite another request’s conversation history.

## Why this matters

- This can create incorrect answers, broken follow-up behavior, and cross-user context bleed.
- It is especially dangerous in enterprise environments where users may query sensitive documents concurrently.
- The bug may be intermittent, which makes it difficult to diagnose once deployed.
- This is one of the most important architecture issues to fix before scaling traffic.

## Desired outcome

The QnA pipeline must become request-scoped and side-effect free. Conversation history should be passed explicitly through function arguments, not through mutable globals.

## Scope

- Remove any request-specific mutable module-level state from the QnA pipeline.
- Pass normalized history into `generate_answer()` directly.
- Ensure helper functions only operate on the history supplied for the current request.
- Audit for similar patterns in other modules.

## Implementation guidance

- Refactor `generate_answer()` to accept current query, mode, bot tag, and normalized history explicitly.
- Keep the request object out of deep business logic where possible.
- Preserve behavior while improving architecture; do not introduce unnecessary redesign if a smaller refactor can solve the issue safely.
- Add concurrency-oriented tests where two simultaneous requests with different histories cannot interfere with one another.

## Deliverables

- refactored pipeline interface with no request globals
- updated FastAPI endpoint integration
- tests covering concurrent or interleaved request scenarios
- documentation for the new function contract

## Acceptance criteria

- No request-specific global state remains in the QnA path.
- Two concurrent requests can execute without sharing conversation context.
- Follow-up rephrasing still works after refactor.
- Existing API behavior remains backward compatible unless a deliberate versioned change is introduced.

## Non-goals

- Full asynchronous redesign of all Azure clients. This issue is about state isolation and correctness.

## Notes for Codex / Claude

Treat this as an architecture hygiene fix with real privacy implications. Keep the final flow easy to reason about and easy to test.