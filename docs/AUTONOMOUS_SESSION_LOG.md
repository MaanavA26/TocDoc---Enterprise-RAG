# Autonomous Session Log

> A running, undo-friendly record of everything done **autonomously while the owner
> was asleep** (~5-hour window starting 2026-06-06 ~01:30 IST). Every autonomous
> merge is listed with a one-line revert. Council verdicts and anything held for
> the owner are recorded too. Read this first on wake-up.

## Operating rules for this run (self-imposed, advisor-validated)

- **Council decides engineering questions only.** Irreversible / outward-facing /
  real-world-fact decisions are **staged for the owner**, never executed by a council.
- **Protect green `main` over merge velocity.** Only *adversarially-confirmed* bugs are
  fixed; if `main` goes red or a fix won't converge in ~2 CI cycles, the PR is
  reverted/closed to restore green and the issue is logged — never thrashed or built upon.
- **No faking.** Anything this environment cannot build/validate (no `npm`, no `docker`,
  no live Azure) or any real-world fact (the BSL legal entity) is **held, not fabricated.**
- `--admin` merges are used per the owner's standing authorization, but only on green CI.

## ⚠️ TOP PRIORITY — sellability blocker found (license audit)

**PyMuPDF (`fitz`) is AGPL-3.0** and is actively used for PDF parsing (`services/ingestion/custom_rag.py` `import fitz`, also pinned in `eval/`). **AGPL is incompatible with selling a proprietary, self-hosted product under BSL 1.1** — this blocks the stated commercial goal. Not fixed autonomously (it's a business + quality decision needing live validation). Your options:
1. **Buy the Artifex commercial PyMuPDF license** (keeps fitz's high-quality extraction), or
2. **Swap to a permissive lib** — BSD-3 `pypdf` is *already pinned but unused* in ingestion; or `pdfplumber`/`pdfminer.six` (MIT). A swap changes extraction quality, so validate PDF output before committing.
Full analysis: `docs/LICENSE_COMPLIANCE.md` (#180). Everything else is permissive (MIT/Apache/BSD); 3 MPL deps (`certifi`/`tqdm`/`orjson`) just need a NOTICE file. No LGPL/GPL/unknown.

## Held for the owner (NOT done autonomously — by design)

| Item | Why held | What you do |
|---|---|---|
| **PyMuPDF AGPL** (see TOP PRIORITY above) | Copyleft incompatible with selling under BSL 1.1; swap needs PDF-quality validation, or buy the commercial license — a business call | Buy Artifex license, or swap to `pypdf`/`pdfplumber` + validate extraction |
| **P3 enable** (flip `QNA_AGENT_ENABLED` default-ON) | Agentic paths never ran against live Azure; flipping prod while you sleep is the #1 bad-surprise risk | After a live smoke, set the env flag; cutover steps in `docs/deployment/P3_ENABLEMENT.md` |
| **#90 THREAT_MODEL merge** | Publicly enumerates live residual risks (attacker checklist); the merged `SECURITY.md` controls doc is the safe public version | Decide: keep internal / trim / merge |
| **#142 Dockerfile hardening** | No `docker` in this env — cannot build; won't merge unbuilt runtime images | `docker build` both services, then merge |
| **#140 web admin SPA** | No `npm` in this env (registry blocked) — cannot build/test | `npm install && build && test` in `web/`, then merge |
| **BSL Licensor entity + Change Date** | Real-world legal fact only you have | Fill the placeholders in `LICENSE` |

## In-flight at start of the autonomous run

- **Re-audit engine** (workflow) — deep audit of the never-audited new strides (streaming,
  cache, multi-format, P3 nodes, OTel) + cross-feature integration coherence, adversarially
  verified. Confirmed findings → fixed in subsequent waves.
- **Decisions council** (workflow) — documented recommendation per gated item above.
- **Smoke runbook** (agent) — `docs/deployment/SMOKE_TEST.md` for live-Azure validation.
- **Heartbeat monitor** — keeps the loop alive across lulls.

## Autonomous merges this run

All CI-gated on green `main`. Revert any one with `git revert <sha>` (squash commits).

| PR | What | Revert (sha) |
|---|---|---|
| #167 | Live-Azure deploy + smoke-test runbook (docs) | `git revert 9de4d54` |
| #168 | mkdocs `--strict` fix — unbreak docs build (`nav.omitted_files: info`) | `git revert 2004011` |
| #169 | **Audit fix** — Terraform App Insights wiring + accurate IP-range var doc | `git revert 7f94adb` |
| #170 | **Audit fix (HIGH)** — SDK SSE parser honors event types (no citation-in-answer, surfaces errors) | `git revert b6af9d3` |
| #171 | **Audit fix (2×HIGH)** — qna SSE backpressure + cooperative cancel + verifier/cache/logging/doc | `git revert 9b37c7b` |
| #172 | **Audit fix (HIGH)** — ingestion OTel span scrub + zip-bomb/malformed/layout/empty + 404 envelope | `git revert 6a616cd` |
| #173 | P3 agentic-layer enablement cutover guide (docs) | `git revert 5029cda` |

**Re-audit of the new strides → 20 confirmed bugs (4 HIGH) → all fixed above.** The 4 HIGH were:
SSE no-backpressure (token-drop/deadlock/slot-leak DoS) + non-cooperative cancel (executor-pool exhaustion/denial-of-wallet) in qna #171; SDK SSE parser corrupting answers + swallowing errors #170; OTel server span leaking the `/upload` query (filepath + bot_tag) #172. A **confirmatory re-audit then found 9 more (1 HIGH + 2 MED + 6 LOW)** — including a HIGH the wave-1 SSE fix had left (a `yield` inside `finally` that breaks on client disconnect + defers worker cleanup to GC) — all fixed in wave 2 (#175–177). A 3rd focused SSE review then confirmed the path CLEAN, and #178 added the missing gate-release-on-disconnect regression test.

**Dependabot declined this run (re-raised, eval-only):** #150–155 — eval's langchain stack is pinned ragas-compatible (langchain 0.3.x); 1.x breaks ragas. Same call as #121–126. No revert needed (closed, not merged).

## Council verdicts (as the owner, adversarially checked) — all STAGE/HOLD, none executed

| Decision | Verdict | Wake-blind risk | Why (short) |
|---|---|---|---|
| **P3 enable** (default-ON) | STAGE for owner | high | Agentic paths never ran against live Azure; the **verifier node is a confirmed pass-through no-op** and ReAct silently collapses to standard — no safety net, no upside tonight, only added classifier-hop cost on 100% of traffic. Flip is reversible; the *answers served* are not. |
| **#90 threat-model merge** | HOLD / internal | high | Public merge = first public exposure of live residual-risk recipes (R1 cross-workspace read, R2 unauth `/upload`) **before** fixes ship; irreversible (forks/cache). Keep as internal backlog. |
| **#142 docker** | HOLD | medium | CI never builds images, so merging adds zero validation; the only real gate (`docker build`+run) is impossible here. |
| **#140 web** | HOLD | medium | npm registry unreachable here; can't build the first admin surface (destructive Danger Zone) before merge. |
| **BSL Licensor + Change Date** | HOLD | high | Owner-only legal facts; guessing = a defective public legal instrument. Placeholders left intact. |

**SECURITY.md item — RESOLVED:** the re-audit found NO real tenant-binding gap (only stale code comments, fixed in #171). The default-ON `tenant_binding` guard enforces `bot_tag`→`tid` *before* search on every path, so `SECURITY.md`'s isolation claim holds. The council's overclaim worry is closed.

---

## Wave 2 (confirmatory-re-audit fixes) + loop closure

| PR | What | Revert (sha) |
|---|---|---|
| #175 | **re-audit** — Bicep health probes + `@maxLength(8)` prefix parity + coherent single-replica PDB | `git revert b47e1bc` |
| #176 | **re-audit** — ingestion OTel exports redacted spans only (no raw logs) + `url.query` scrub | `git revert 88035d5` |
| #177 | **re-audit (HIGH)** — qna SSE disconnect cleanup (no yield-in-finally) + dedicated stream executor + verifier/react robustness | `git revert 81c154f` |
| #178 | test — end-to-end SSE concurrency-gate release on client disconnect (mutation-verified) | `git revert 0c81446` |

**Audit loop CLOSED.** Cycle 0 (audit → 20) → wave 1 (#169–172) → confirmatory re-audit (9, incl. a wave-1-missed HIGH) → wave 2 (#175–177) → focused SSE review = CLEAN + regression test (#178). `main` green throughout. The SSE streaming path (the recurring hotspot) took 3 passes and is now sound + tested.

## Wake-up summary

- **Done autonomously:** deep re-audit of the never-audited new strides → **29 findings (5 HIGH) across two waves, all fixed** on green `main`, every one reversible (tables above); a live-Azure **smoke runbook** (#167); a **P3 enablement cutover guide** (#173); the **decisions council** (all 5 gated items staged/held, recorded above); Dependabot kept clean.
- **The 5 HIGH were all in the brand-new strides** (SSE streaming, OTel log/span leaks, SDK SSE parser) — the code per-PR CI couldn't vet. `main` is green and materially hardened.
- **Held for you (unchanged, by design):** #90 threat-model exposure; #140 web (needs `npm` build); #142 docker (needs `docker build`); BSL Licensor/Change-Date; and **P3 enable** (live-Azure smoke first — see `docs/deployment/P3_ENABLEMENT.md`).
- **Recommended first move on waking:** run `docs/deployment/SMOKE_TEST.md` against a real Azure deployment — that's the one thing this environment couldn't do and the gate to "validated product."

_Last updated: audit/fix loop closed. Run continues in maintenance (heartbeat + Dependabot triage) until you're back._
