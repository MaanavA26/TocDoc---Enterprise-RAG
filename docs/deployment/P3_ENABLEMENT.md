# Enabling the P3 Agentic Layer — Cutover Guide

> **Status: the layer is built and merged but ships DARK (all flags default-OFF).**
> This is the go/no-go + step-by-step for turning it on safely. It was deliberately
> **not** enabled autonomously — flipping the default answer path is outward-facing
> and the agentic path has never run against live Azure (see the autonomous-session
> council verdict: stage, don't flip blind).

## What the layer is

A LangGraph `StateGraph` wraps the QnA pipeline behind a master flag plus per-node
sub-flags. With the master flag off, `/qna` and `/qna/stream` are byte-for-byte the
existing standard pipeline.

| Flag (env) | Default | Effect when on |
|---|---|---|
| `QNA_AGENT_ENABLED` | **off** | Master switch — routes requests through the agentic graph. Read live per-request, so it's an instant kill-switch. |
| `QNA_AGENT_MAP_REDUCE` | off | Enables the map-reduce summarizer route (fan-out over chunks; tuned by `MAP_REDUCE_BATCH_SIZE`/`_CONCURRENCY`/`_MAX_CHUNKS`). |
| `QNA_AGENT_REACT` | off | Enables the ReAct multi-hop route (bounded iterations; falls back to standard on failure). |
| `QNA_AGENT_VERIFY` | off | Enables the self-critique verifier (a real groundedness/citation grader; on the `react` route it can trigger one bounded refine, keeping the original answer unless the refine clears the bar). |

## Why it's not on yet (council go/no-go = STAGE)

- The agentic routes have **never executed against live Azure** — classifier-route
  accuracy, latency, and cost are unobserved. Mocked tests pass, but that's not the
  same as live behavior.
- With the master flag on but sub-flags off, every request still resolves to the
  standard route **plus** a mandatory classifier LLM hop — added latency/cost for
  zero functional gain. Enable sub-flags deliberately, not implicitly.
- Flipping the default is outward-facing for a product that deploys into client Azure
  tenants; wrong/degraded answers served are not recallable. The flag is reversible;
  the answers are not.

## Prerequisites before enabling

1. A live deployment with the **smoke test green** (`docs/deployment/SMOKE_TEST.md`).
2. Eyes on live telemetry (enable App Insights tracing — `enableAppInsightsTracing`
   in Bicep / `enable_app_insights_tracing` in Terraform).
3. Confirm current node behavior against the code you're shipping (verifier grading
   thresholds via `VERIFY_MIN_SCORE`; ReAct caps via `REACT_*`).

## Cutover steps (do these awake, watching live)

1. Keep `QNA_AGENT_ENABLED=false` as the documented instant revert.
2. Enable on a **staging / non-client** config first, not a live client default.
3. Turn on the **master flag only** and run a live smoke: confirm the classifier
   routes sensibly and latency/cost are acceptable on the standard route.
4. Turn on **one sub-flag at a time** (`QNA_AGENT_MAP_REDUCE`, then `QNA_AGENT_REACT`,
   then `QNA_AGENT_VERIFY`), running a real query through each and watching App
   Insights spans + answer quality + cost per turn.
5. Only after each is validated live, promote the flags to the shipped default for a
   client — one client/canary first if possible.

## Instant revert

Set `QNA_AGENT_ENABLED=false` (or the offending sub-flag). It's read per-request, so
the next request reverts to the standard pipeline. No redeploy required.

## What to watch

- p50/p95/p99 latency vs the standard route (the classifier hop adds a round-trip).
- Per-turn token cost (map-reduce fans out over chunks).
- Classifier route distribution (is it picking sensible routes?).
- Verifier accept/refine/reject rates and whether refines actually improve answers.
- Tenant isolation holds on every agentic path (the default-ON tenant binding applies
  before retrieval regardless of route).
