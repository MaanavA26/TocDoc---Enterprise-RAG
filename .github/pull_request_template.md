<!-- Keep this concise. One logical change per PR. -->

## Summary

<!-- What does this change do, and why? -->

## Type of change

- [ ] feat — new functionality
- [ ] fix — bug fix
- [ ] docs — documentation only
- [ ] chore — tooling / housekeeping
- [ ] deps — dependency change

## How verified

- [ ] `ruff check` + `ruff format --check` pass
- [ ] `pytest` passes for the affected service(s)
- [ ] CI gate is green

## Backward-compatible?

- [ ] Yes — no breaking changes
- [ ] No — breaking change (explain below)

<!--
For API changes, the expectation is a byte-identical response contract:
existing request/response shapes must serialize exactly as before unless the
change is an explicitly versioned, documented break. Note any deviation here.
-->

## Linked issue / ADR

<!-- e.g. Closes #123, or a link to the relevant ADR. -->
