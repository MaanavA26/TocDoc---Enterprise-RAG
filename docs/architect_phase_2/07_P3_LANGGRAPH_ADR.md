> **Status:** DRAFT — produced by a multi-agent design council (3 independent architecture proposals, 3 judges scoring fit/deliverability/isolation-safety; risk-first won unanimously). Pending architect review. Not yet implemented.

# P3 LangGraph Agentic Layer — Architecture Decision Record

## Context & constraints (from the existing P0/P1 codebase)

The QnA service today answers via a single naked call: `app.py:210` does `await src.pipeline.qna_pipeline.generate_answer(...)` and returns its `{answer, citation}` dict (`qna_pipeline.py:249`) verbatim. P3 inserts a LangGraph `StateGraph` between that handler and the work without disturbing the operational invariants the P0/P1 work established. Every design choice below is justified by a constraint observed in the source, not by agentic novelty for its own sake. Where the brief's casual phrasing collides with the code, the code wins.

The binding constraints, verified against the repo:

- **Sync SDK + tiny executor (the fan-out footgun).** `azure.openai_client` is the synchronous `openai.AzureOpenAI` client (`azure_clients.py:4,43`), invoked via sync `.chat.completions.create()` (`openai_service.py:100,180`) offloaded onto a **2-worker** pool, `openai_executor = ThreadPoolExecutor(max_workers=2)` (`openai_service.py:15`). Search has its own 2-worker pool (`search_service.py:16`). Consequence: bare `asyncio.gather()` over the sync client parallelizes **nothing**, and an unbounded executor melts the Azure quota. Any P3 fan-out must use `run_in_executor`/`to_thread` on a config-sized executor bounded by a semaphore.
- **Tenant isolation lives at the search layer.** `perform_search()` rejects empty/whitespace `bot_tag` before any call (`search_service.py:46-47`) and builds the filter from an escaped `safe_bot_tag` (`search_service.py:100-102`). The agentic layer's only job is to **never let an LLM reach that parameter**.
- **Retrieval is hard-capped.** `_search_sync` is invoked with `top=localconfig.TOP_K` (`search_service.py:62`), `TOP_K=20`. "Retrieve all chunks" for map-reduce is therefore a **real signature change**, not a pass-through.
- **Request-scoped state only.** `generate_answer(query, fr_mode, bot_tag, history, azure)` (`qna_pipeline.py:34`) takes everything explicitly; `azure` is injected from `request.app.state.azure` (`app.py:172`). No module-level mutable globals. LangGraph state must preserve this.
- **Structured error envelope (P0-6).** The handler stays naked — no try/except around the call — so node exceptions bubble to the global handlers in `core/errors.py` for envelope production with `X-Request-ID`. The pipeline re-raises after logging (`qna_pipeline.py:254`); we never return a 200-with-error-field.
- **Best-effort non-critical steps.** Rephrasal is wrapped catch-and-warn-and-proceed (`qna_pipeline.py:135-137`). New non-critical node steps follow this exact pattern.
- **request_id correlation across threads.** `RequestIDMiddleware` mints the UUID and stores it on `request.state.request_id` and a `ContextVar`. Because search/LLM calls offload to executor threads, the `ContextVar` is **not reliably visible** there; nodes must extract `request_id` from state and pass it explicitly to `log_event(request_id=...)`.
- **No structured-output capability exists yet.** A `grep` for `response_format`/`json_schema` across `src/` returns nothing; today the code only regex-parses rephrasal. The classifier and verifier introduce a genuinely **new** capability (a `response_format`/JSON-schema helper), not a reuse of an existing path.

## Decision (the recommended architecture, with state schema + node graph)

Introduce a new `services/qna/src/agents/` package that wraps the existing pipeline in a LangGraph `StateGraph`. The package is **risk-first** in design (every node serves a verified invariant), **converged** in topology (one verifier downstream of all answer strategies, so self-critique is written once), and **dark-launched** in delivery (a default-OFF feature flag preserves byte-for-byte parity and gives an instant kill-switch with no redeploy).

The handler's only change at `app.py:210` is a flag-gated swap from the naked `generate_answer(...)` call to a naked `agents.router.agentic_generate_answer(...)` call that returns the **same** `{answer, citation}` shape (plus optional additive fields). With `QNA_AGENT_ENABLED=false`, the legacy direct call is retained unchanged — this is the merge gate for every increment.

**Why these grafts.** From P1 (Minimal-Incremental) we take the default-OFF flag discipline and the hard PR0 dependency-resolution gate. From P2 (Full-Supervisor) we take the converged `→ verifier → END` topology as the *target* shape, and the explicit `perform_search` signature change. The base structure, code-grounded concurrency, and honesty about the v1 verifier gap are P3's.

### State schema

A single request-scoped `TypedDict` in `agents/state.py`. It is constructed fresh per request inside `agentic_generate_answer()` and never stored at module level — request-scoping is structural, mirroring the `generate_answer` contract (`qna_pipeline.py:34`). `total=False` keeps every output optional so partial graphs are valid and the schema does not balloon. The documented invariant is **each node reads its declared inputs and writes only its own output keys** — no two nodes write the same key, which directly answers the overlapping-mutation risk.

```python
from typing import Any, Literal, TypedDict

class AgentState(TypedDict, total=False):
    # --- Required on entry (set by the wrapper; mirror generate_answer's contract) ---
    query: str                       # latest user utterance
    fr_mode: str                     # 'read' | 'layout'
    bot_tag: str                     # tenant key — flows state->node->perform_search; NEVER LLM-visible
    history: list[dict]              # conversation turns (windowed by the handler)
    azure: Any                       # the live AzureOpenAIHandler from request.app.state.azure
    request_id: str                  # from request.state.request_id; passed explicitly to log_event()

    # --- Router output (written only by classifier) ---
    route: Literal["standard", "map_reduce", "react"]
    effective_query: str             # warm-start rephrase; set ONLY on non-self-rephrasing routes
    is_followup: bool

    # --- Answer-node outputs (each key written by exactly one node) ---
    retrieved_chunks: list[dict]     # map_reduce / react; ABSENT on standard route in v1 (see below)
    partial_answers: list[str]       # map_reduce map step only
    final_answer: str                # whichever answer node ran
    citations: dict[str, str]        # whichever answer node ran
    reasoning_trace: list[dict]      # react only (thought/action/observation per iter)

    # --- Verifier output ---
    verified: bool
    unsupported_claims: list[str]

    # --- Soft logical-error sentinel (checked by the wrapper post-invoke) ---
    error: dict | None
```

Two deliberate consequences. (1) `azure` and `request_id` live **in state** — nodes never construct clients and never trust the `ContextVar`. (2) Because state carries the live `azure` client, `AgentState` is **non-JSON-serializable**; this intentionally rules out the LangGraph checkpointer and is precisely why conversation memory (P3-5) is route-handler-owned rather than a graph persistence layer. `bot_tag`/`fr_mode` are state-only and are bound into tool closures, never surfaced as LLM-visible tool parameters.

### Node graph

The dispatcher is a **classifier node with conditional edges** (idiomatic LangGraph, no extra agent round-trip), not an agent-as-supervisor. All three answer strategies converge on a single verifier before `END`, so self-critique is authored once and composes across strategies as the target shape.

```
            START
              │
              ▼
        ┌───────────┐   (best-effort; defaults route="standard" on classifier failure)
        │ classifier│   agents/router.py  — sets state["route"], emits route_decision
        └─────┬─────┘
              │  add_conditional_edges(state["route"])
   ┌──────────┼───────────────┐
   ▼          ▼               ▼
┌────────┐ ┌──────────┐ ┌──────────────┐
│standard│ │map_reduce│ │ react_agent  │
│ _route │ │ (P3-2)   │ │   (P3-3)     │
└───┬────┘ └────┬─────┘ └──────┬───────┘
    │           │              │
    └───────────┴──────┬───────┘
                       ▼
                 ┌──────────┐    verifier (P3-4) reads final_answer + retrieved_chunks
                 │ verifier │    NO-OP on standard route in v1 (no chunks exposed)
                 └────┬─────┘
                      ▼
                     END  ──►  wrapper returns {answer, citation [, verified, ...]}
```

- `classifier` (`agents/router.py`) — calls `azure.openai_client` with a **new** structured-output classifier helper to set `state["route"]`; may compute `effective_query` for warm-start, but **only on routes that do not self-rephrase** (avoids double-rephrasal with `standard_route`'s internal `rephrase_queries()`). Best-effort: on exception, log a warning and default `route="standard"`.
- `standard_route` (`agents/standard_route.py`) — thin wrapper that calls the **unchanged** `qna_pipeline.generate_answer(...)` and unpacks the result into `state["final_answer"]`/`state["citations"]`. No reimplementation of any P0 guarantee.
- `map_reduce` (`agents/map_reduce.py`) and `react_agent` (`agents/react_agent.py`) — alternative answer strategies (detailed below).
- `verifier` (`agents/verifier.py`) — the single convergence node. In **v1 it is a no-op on the standard route**, because `generate_answer` returns only `{answer, citation}` and does not expose `retrieved_chunks` (`qna_pipeline.py:249`); a verifier with no evidence cannot judge. The honest, converged target is reached in a scoped follow-up (see plan, item 9) that threads `retrieved_chunks` out of the pipeline.

**Error flow.** Hard exceptions from any node are **not** caught in the graph; they bubble through `ainvoke()` → `agentic_generate_answer` (no try/except) → the naked `await` in `app.py` → the global handler in `core/errors.py` → a 500 envelope with `X-Request-ID`. Soft/logical errors set `state["error"]`; the wrapper checks it post-invoke and calls `raise_api_error(...)` so they still flow through the P0-6 envelope. Best-effort steps (classifier, map fan-out, verifier) catch-and-warn-and-proceed, mirroring `qna_pipeline.py:135-137`.

## How each P3 feature maps (P3-1 router, P3-2 map-reduce summarizer, P3-3 ReAct multi-hop, P3-4 self-critique, P3-5 conversation memory, P3-6 SSE streaming)

- **P3-1 Router — `agents/router.py` classifier node + conditional edges. Flag: `QNA_AGENT_ROUTER`.** Receives `AgentState`; calls `azure.openai_client` with a new `response_format`/JSON-schema helper (structured output does not exist today — `openai_service.py` only regex-parses) to set `state["route"]` ∈ {standard, map_reduce, react}. May reuse `rephrase_queries()` for follow-up warm-start, computing `effective_query` only on non-self-rephrasing routes. Best-effort: defaults to `standard` on any failure. Emits a `route_decision` `log_event` with `request_id` pulled from state.
- **P3-2 Map-Reduce — `agents/map_reduce.py`. Flag: `QNA_AGENT_MAP_REDUCE`.** Retrieves **all** chunks for `(bot_tag, fr_mode)` — this requires the real `perform_search`/`_search_sync` signature change (today `top=localconfig.TOP_K=20` at `search_service.py:62` silently caps it). Chunks are batched by `MAP_REDUCE_BATCH_SIZE` (default 20). **Concurrency, code-grounded:** since `azure.openai_client` is synchronous (`azure_clients.py:4`) behind a 2-worker pool, bare `asyncio.gather()` parallelizes nothing — each batch LLM call is wrapped in `run_in_executor`/`to_thread` on a **dedicated, config-sized executor** bounded by `asyncio.Semaphore(MAP_REDUCE_CONCURRENCY)` with exponential backoff. Map (extract) uses GPT-4o-mini; Reduce uses a separate `AZURE_OPENAI_REDUCE_MODEL` (GPT-4o). Citations resolved with the existing `_norm_name`/`_stem` (`util.py:123,149`). Logs `chunk_count` + `batch_count` (asserting chunk_count proves the cap was lifted). Best-effort: on map failure, log a warning and fall back to standard retrieval rather than fail the request.
- **P3-3 ReAct — `agents/react_agent.py` wrapping `create_react_agent()`. Flag: `QNA_AGENT_REACT`.** Two tools, `search_documents()` and `extract_entities()`, built by a factory whose **closure** captures `bot_tag`/`fr_mode`/`azure`/`request_id` from state. The LLM-visible tool schema exposes **only query-shaped args** — never a tenant or filter parameter. The closure re-asserts the state `bot_tag` before each search, and `perform_search` independently rejects empty `bot_tag` (`search_service.py:46-47`), so a prompt-injected filter cannot reach the search layer. Max 5 iterations; `(thought, action, observation)` logged per iteration into `reasoning_trace` with `request_id`.
- **P3-4 Self-Critique — `agents/verifier.py` convergence node. Flag: `QNA_AGENT_VERIFY`.** Calls a verifier LLM (new `AZURE_OPENAI_VERIFIER_MODEL`) with structured output `{verdict, unsupported_claims, confidence}` over `final_answer` vs `retrieved_chunks`. **v1 = Option A (non-destructive):** on `HALLUCINATION_DETECTED`, log the claims and set `state["verified"]=False`; do not rewrite the answer (cheap, ties to cost control). Response gains an optional additive `verified: bool`. No-op on the standard route (no chunks exposed in v1) and skipped by streaming. Verifier exceptions are best-effort: log and leave `verified` unset; never fail the request.
- **P3-5 Conversation Memory — `agents/memory.py`, owned by the route handler not the graph. Flag: `QNA_MEMORY_ENABLED`.** Justified by the non-serializable state (the live `azure` client in state rules out the graph checkpointer). `Payload.session_id` already exists (`util.py:39`). Before the graph: if `session_id` present and memory enabled, load history from Cosmos keyed by `bot_tag`; else generate a UUID. The LLM sees the last `MAX_HISTORY_TURNS` (default 6) truncated from the full Cosmos history, **always read fresh per request** (mitigates the stale-tab window). After the graph: save with TTL 86400s. Container schema `{id, bot_tag, user/email, history, ttl}`; reads/writes filtered by `bot_tag` so memory respects tenant isolation. Cosmos client initialized in `core/lifecycle.py` `startup_event()` alongside `app.state.azure`, released in `shutdown_event()`; absence of Cosmos config degrades gracefully to stateless.
- **P3-6 SSE Streaming — new `/answer/stream` endpoint in `app.py`, built outside the graph. Endpoint-gated (exists only when shipped).** Does **not** modify `/qna`. Code-grounded: `azure.openai_client.chat.completions.create(stream=True)` returns a **synchronous iterator** (the brief's `stream_chat()` does not exist), so the endpoint pumps it from a thread into an async generator yielding `data: {json}\n\n` for EventSource clients. Reuses the standard retrieval + prompt build and the same `bot_tag` validation; once streaming has begun, mid-stream failures emit a terminal error event. v1 **skips the verifier** (documented; recommend `/qna` for high-stakes queries). `request_id` from `request.state` flows into `log_event` explicitly.

## Preserving P0 guarantees (bot_tag isolation, request-scoping, structured errors)

- **Tenant isolation.** `bot_tag` flows state → node → `perform_search`, whose empty-`bot_tag` rejection (`search_service.py:46-47`) and `safe_bot_tag` filter (`search_service.py:100-102`) are reused, **not reimplemented**. The LLM never sees `bot_tag`/`fr_mode`: they are bound in tool closures and absent from every LLM-visible tool schema; the ReAct tool wrapper re-asserts the state `bot_tag` before each search. This neutralizes the prompt-injection-sets-foreign-tenant risk.
- **Request-scoping.** `AgentState` is constructed fresh per request inside `agentic_generate_answer()`; `azure` is injected from `request.app.state.azure` (`app.py:172`) and never created in a node. The only module-level objects added are the bounded executor and the compiled, stateless graph — both immutable. This matches the request-scoped `generate_answer` contract.
- **Structured errors (P0-6).** Two explicitly separated paths. **Hard failure:** a node raises, no node swallows, the exception bubbles to the global handler in `core/errors.py` → 500 envelope + `X-Request-ID`, exactly as `qna_pipeline.py:254` re-raises and `app.py:210` makes a naked call. **Soft/logical:** a node sets `state["error"]`; the wrapper calls `raise_api_error(...)` post-invoke so it too flows through the envelope. Best-effort steps catch-warn-proceed per `qna_pipeline.py:135-137`. We never return a 200-with-error-field.
- **request_id correlation.** Every node calls `log_event()` with `request_id` pulled from state — not the `ContextVar`, which does not cross the executor threads that search and LLM calls already use. **Disclosed gap:** `generate_answer` mints its own `request_id = f"gen_{...}"` (`qna_pipeline.py:64`) and takes no `request_id` parameter, so the standard route's inner-pipeline logs will not share the middleware/graph correlation ID until the scoped follow-up (plan item 9) threads `request_id` into its signature. This is a tracked decision, not a silent regression.

## Rejected alternatives (the other two philosophies — one paragraph each, why not)

**P1 — Minimal-Incremental (LangGraph wrapped around, never threaded through; one idea kept).** Its blast-radius story is the strongest of the three and we adopt one piece outright: the **default-OFF dark-launch flag** retaining the legacy direct call as a no-redeploy kill-switch. Rejected as the primary shape, however, because (a) it is wrong on two verified facts — it states map-reduce fans out "via `asyncio.gather` inside the node" (gather over the sync client at `azure_clients.py:4` parallelizes nothing) and "no top-k change to the function; pass-through" (which silently caps retrieval at `TOP_K=20`, `search_service.py:62`), so a "small PR of map-reduce" would ship a non-functional feature; and (b) its philosophy makes the verifier a bolt-on post-graph step and every feature an independent flag branch, which produces a combinatorial flag-matrix testing cost and forecloses the converged single-verifier topology we want long-term.

**P2 — Full-Supervisor (supervisor node as the new single entry point; one idea kept).** It is correct on the sync-SDK and top-k facts, and we adopt two pieces: the **converged `→ verifier → END` topology** as the target shape, and the **explicit `perform_search` `top`/`fetch_all` signature change** with `chunk_count` asserted in logs. Rejected as the base, however, because (a) by its own admission the first increment is "deliberately larger" (full scaffold + supervisor + convergence at once), which is weakest on the explicit ship-in-small-PRs criterion; and (b) its headline claim that "self-critique composes uniformly across every route" is partly unrealizable on its own terms — it wraps the **unchanged** pipeline as the standard node, yet that pipeline does not expose `retrieved_chunks` (`qna_pipeline.py:249`), so the verifier has no evidence on the standard route — and P2 does not acknowledge this, whereas our design ships the honest v1 no-op and converges only after a scoped follow-up surfaces the chunks. P2 also silently inherits the `gen_{ts}` correlation gap it never discloses.

## Sequenced delivery plan (ordered, independently-shippable PR-sized increments, each tied to a P3 item)

Each increment is independently mergeable and leaves the default `/qna` path byte-for-byte identical until its flag is enabled. PR0 is the only hard prerequisite for the agentic branches.

1. **PR0 — Scaffold + dependency gate + dark seam (keystone, behavior-preserving).** Add `langgraph>=0.2.0,<0.3.0` to requirements; **hard CI acceptance gate: pip must resolve it against the pinned `langchain-core==0.3.60` — do not merge if resolution fails** (unverifiable in our review-only environment, so the gate is the only safe mechanism). Add `agents/state.py` (`AgentState`), `agents/router.py` (`agentic_generate_answer` + a classifier stub that always routes `standard`), `agents/standard_route.py` (wraps the unchanged `generate_answer`), wire `START → classifier → standard_route → verifier(no-op) → END`, and swap the `app.py:210` call behind `QNA_AGENT_ENABLED` **defaulting OFF**. Flag-off path is provably the legacy direct call. Tests: graph returns the same `{answer, citation}` shape; `request_id` passed explicitly to `log_event`.
2. **PR1 — Config (no behavior change).** Add `MAP_REDUCE_BATCH_SIZE`, `MAP_REDUCE_CONCURRENCY`, `AZURE_OPENAI_REDUCE_MODEL`, `AZURE_OPENAI_VERIFIER_MODEL`, `MAX_HISTORY_TURNS`, and `COSMOS_*` to `LocalConfig` via the existing canonical-aware `_get_env` resolver, all **UPPER_SNAKE per the P0-7 commit**. None exist today.
3. **PR2 — P3-1 Router (real classifier). Flag `QNA_AGENT_ROUTER`.** Replace the routing stub with the new structured-output classifier helper; add `add_conditional_edges`; emit `route_decision`; double-rephrasal guard in place. With only `standard` wired downstream it still behaves identically but routes for real.
4. **PR3 — P3-2 Map-Reduce. Flag `QNA_AGENT_MAP_REDUCE`.** Includes the `perform_search` `top`/`fetch_all` signature edit, the bounded executor + `Semaphore` + backoff (no gather-over-sync), and the standard-retrieval fallback. Wire the `map_reduce` edge live.
5. **PR4 — P3-3 ReAct. Flag `QNA_AGENT_REACT`.** `create_react_agent` with closure-bound tools, `reasoning_trace`, max 5 iterations, wrapper re-asserts `bot_tag`. Wire the `react` edge live.
6. **PR5 — P3-4 Self-Critique. Flag `QNA_AGENT_VERIFY`.** Verifier node, Option A (flag, non-destructive). Active on `map_reduce`/`react`; explicit no-op on `standard` (no chunks in v1). Adds optional `verified: bool`.
7. **PR6 — P3-5 Conversation Memory. Flag `QNA_MEMORY_ENABLED`.** `agents/memory.py` + Cosmos init in `lifecycle.py`; route-handler-owned load/save around the graph, keyed by `bot_tag`. Orthogonal — touches neither router nor nodes; degrades gracefully without Cosmos config.
8. **PR7 — P3-6 SSE Streaming.** New `/answer/stream` endpoint pumping the sync `create(stream=True)` iterator from a thread into an async generator; skips verifier; `/qna` untouched. Orthogonal; sequence by priority.
9. **PR8 — Verifier convergence + correlation closure (scoped follow-up).** Thread `retrieved_chunks` **and** the middleware `request_id` out of `generate_answer` (changing its signature, closing the `gen_{ts}` gap at `qna_pipeline.py:64`) so the standard-route verifier stops being a no-op and self-critique composes uniformly across all three routes. This is the deliberate, reviewed change that makes P2's converged topology real on our terms.

## Open questions for the architect

1. **Verifier action — Option A vs B.** Ship A (flag `verified=False`, surface to client) as v1; is B (re-run generation with a stricter prompt on `HALLUCINATION_DETECTED`) ever in scope, given its extra LLM cost against the token/cost pillar?
2. **Streaming + verification.** Confirm v1 skips the verifier outright, or do we want the post-stream async-verifier-with-disclaimer-final-chunk variant despite its added complexity?
3. **Map-reduce parallelism mechanism.** Dedicated config-sized executor + `Semaphore` (reuses the established `run_in_executor` precedent, zero client change) **vs** adding an `AsyncAzureOpenAI` to `AzureOpenAIHandler` (cleaner long-term, but a new client on the holder and a broader blast radius). Recommendation: executor+semaphore for v1.
4. **`generate_answer` signature change timing (PR8).** Thread `request_id`/`retrieved_chunks` into the pipeline now to get uniform verification + correlation, or defer? This is the one change that touches the protected pipeline; it needs an explicit green-flag.
5. **Response-schema strategy.** Keep the core `{answer, citation}` contract frozen and put `verified`/`reasoning_trace`/`chunk_count` behind a single optional `response_metadata` object, or add them as additive top-level optional fields? Affects existing clients.
6. **Cosmos isolation semantics.** Confirm memory is per-session (not per-user) and that keying every read/write by `bot_tag` is sufficient tenant isolation for the memory store, mirroring the search-layer guarantee.
7. **LangGraph supervisor primitive.** We chose a classifier-node-with-conditional-edges over `create_react_agent`-as-supervisor for the top level; confirm this stays the convention as more strategies are added, to keep the top-level graph a shallow star.
