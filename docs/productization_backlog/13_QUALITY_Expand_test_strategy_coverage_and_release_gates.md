# 13 — Quality: Expand test strategy, coverage, and release gates

**Priority:** P1  
**Type:** Quality engineering / Release confidence / Enterprise readiness

## Problem

The project has some test coverage in the QnA service, but the overall quality strategy is still incomplete for a sellable platform. Coverage is uneven, edge cases are not exhaustively modeled, and release gating is not yet formalized.

## Why this matters

- Enterprise product confidence comes from predictable behavior, not just successful demos.
- Security, concurrency, and lifecycle fixes need regression protection.
- CI/CD is only as useful as the checks it enforces.
- Strong tests reduce support effort and make coding agents safer to use on the repo.

## Desired outcome

TocDoc should have a layered quality strategy spanning unit tests, service-level tests, contract tests, negative-path tests, and release gates that prevent obvious regressions from shipping.

## Scope

- Expand tests for ingestion flows, retrieval behavior, auth, lifecycle operations, and config bootstrap.
- Add regression coverage for the newly identified P0 issues.
- Define minimum automated checks for PRs and releases.
- Consider smoke tests for deployment validation.

## Implementation guidance

- Focus on high-risk behavior first: security, isolation, concurrency, data lifecycle, and API contract integrity.
- Keep tests fast where possible, but include a small set of realistic integration-style checks.
- Use fixtures and mocks consistently to avoid fragile cloud-dependent tests.
- Document what each layer of testing is intended to catch.

## Deliverables

- expanded automated tests
- CI quality gates
- documented test strategy
- optional smoke or post-deploy validation script

## Acceptance criteria

- The most critical production risks have automated regression coverage.
- PR validation includes meaningful quality checks.
- Test expectations are documented for contributors and coding agents.
- Releases are gated by more than a basic syntax or import check.

## Non-goals

- Achieving a vanity coverage percentage with low-value tests. Focus on risk reduction and contract protection.

## Notes for Codex / Claude

Think in terms of product confidence. The best tests are the ones that lock down the failures most likely to hurt customers or break delivery.