# CodeQL Alert Triage

Triage of the CodeQL code-scanning alerts surfaced after scanning was enabled
(workflow: `.github/workflows/codeql.yml`). This is a **read-only triage** to
drive a follow-up; no source was modified. Each assessment is grounded in the
cited file/line on `origin/main`.

**Scan status:** completed (CodeQL workflow runs green on recent PRs).
**Alerts fetched:** 2026-06-05.

Re-run to refresh:

```
gh api repos/MaanavA26/TocDoc---Enterprise-RAG/code-scanning/alerts --paginate --method GET -f state=open
gh run list --workflow=codeql.yml
```

---

## Summary

| Severity | Count |
|----------|-------|
| High (`error` / security-severity high) | 5 |
| Medium (`error` / security-severity medium) | 2 |
| **Total open** | **7** |

| Disposition | Count |
|-------------|-------|
| True positive — worth fixing | 5 |
| False positive / acceptable | 2 |

| Rule | Count | Disposition |
|------|-------|-------------|
| `py/path-injection` | 3 | True positive |
| `py/stack-trace-exposure` | 2 | True positive |
| `py/clear-text-logging-sensitive-data` | 2 | False positive / acceptable |

---

## High severity

### 1. `py/path-injection` — Uncontrolled data used in path expression (×3)

| # | Location | Sink |
|---|----------|------|
| 7 | `services/ingestion/app.py:137` | `os.walk(filepath)` |
| 6 | `services/ingestion/app.py:133` | `os.path.isdir(filepath)` |
| 5 | `services/ingestion/app.py:156` | `open(self.file_path, "rb")` |

**Assessment — TRUE positive.** The `/upload` endpoint takes a
user-controlled `filepath` query parameter (`filepath: str = Query(...)`,
`app.py:109`) and flows it directly into `os.path.isdir`, `os.walk`, and
`open` with no containment check against a safe root. An external caller can
therefore steer file-system traversal to arbitrary server directories and read
any `.pdf` found there. CodeQL's taint trace (query param → path sink) is
accurate.

**Recommended action:** Validate the resolved path is contained within a
configured upload root before any path operation (e.g. `os.path.realpath` then
verify it starts with the allowed root; reject otherwise). Pair with
authentication on the endpoint (see cross-reference below). Triaged as a single
follow-up since all three sinks share one tainted source.

### 2. `py/clear-text-logging-sensitive-data` — clears-text logging (×2)

| # | Location | Disposition |
|---|----------|-------------|
| 4 | `services/qna/src/core/observability.py:166` | False positive |
| 3 | `services/qna/src/config/config.py:139` | False positive |

**Alert 4 — `observability.py:166` (`logger.log(level, line)`).**
**Assessment — FALSE positive / acceptable.** This is the generic structured
log sink inside `log_event()`; the flagged line emits whatever fields a caller
passed. Reviewing every caller of `log_event(` in `services/qna`
(`src/core/auth.py`, `src/core/errors.py`, `src/pipeline/qna_pipeline.py`,
`src/agents/*`), none forward a secret-bearing field: the auth middleware
explicitly logs only coarse `failure_type` labels and never the bearer token
(`auth.py:93–179`, with in-code notes that the token value is never logged).
The function also truncates string field values by default (200 chars) as a
defence-in-depth measure. No secret reaches this sink in the current tree.

**Alert 3 — `config.py:139` (`canonical`).**
**Assessment — FALSE positive.** The flagged `warning(...)` call logs the
*names* of environment variables / Key Vault secrets (`legacy`, `canonical`)
for a deprecation notice — not their values. Logging a configuration key's name
is not disclosure of the secret material itself.

**Recommended action:** Dismiss both in the CodeQL UI as *Won't fix / false
positive* with the rationale above. If stronger assurance is desired for the
generic sink, an allow/deny field-name filter in `log_event()` would make the
guarantee explicit and silence the alert at the source — optional hardening,
not a defect fix.

---

## Medium severity

### 3. `py/stack-trace-exposure` — Stack-trace / exception text may reach an external user (×2)

| # | Location | Returned value |
|---|----------|----------------|
| 1 | `services/ingestion/app.py:168` | folder-mode `results` list containing `error: str(e)` (set at `app.py:165`) |
| 2 | `services/qna/app.py:142` | `{"status": "error", "qna_module": str(e)}` |

**Assessment — TRUE positive (minor).** Both paths convert a caught exception
to a string and return it in the HTTP response body, so internal error detail
can surface to the caller. Alert 1 is the higher-priority of the two because it
sits on the `/upload` endpoint (see cross-reference). Alert 2 is on the QnA
health/status branch and is lower impact, but the pattern is the same.

**Recommended action:** Return a generic client-facing message and log the full
detail server-side instead of echoing `str(e)`. The codebase already has the
right pattern elsewhere (the QnA auth middleware logs `type(e).__name__` and
returns a fixed message, `auth.py:157–171`) — apply the same shape here.

---

## Security-posture cross-reference

CodeQL coverage of the known residual items is **asymmetric** — stated plainly
so this report is not read as implying full coverage:

- **Unauthenticated `/upload` (residual item): SURFACED.** The path-injection
  trio (alerts 5–7) and the stack-trace exposure on `services/ingestion/app.py:168`
  (alert 1) all sit on `/upload`. With no authentication on the endpoint, an
  unauthenticated caller can both traverse arbitrary server paths and receive
  raw exception text. These alerts reinforce the existing residual finding and
  raise its priority — fix path containment, exception handling, and auth
  together.

- **Within-tenant `bot_tag` ↔ `tid` isolation gap (residual item): NOT
  SURFACED.** This is an authorization-semantics gap that CodeQL does not model,
  and no alert in this set covers it. It remains tracked independently of CodeQL;
  do not infer coverage from a clean scan here.

No new residual items beyond what CodeQL already surfaces are disclosed in this
document.

---

## Follow-up actions (no code changed here)

1. Path containment + auth on `/upload` (alerts 5–7, 1) — highest priority.
2. Replace `str(e)` in response bodies with generic messages (alerts 1, 2).
3. Dismiss the two `py/clear-text-logging-sensitive-data` alerts (3, 4) as false
   positives in the CodeQL UI, optionally adding a field-name filter to
   `log_event()` as explicit hardening.
