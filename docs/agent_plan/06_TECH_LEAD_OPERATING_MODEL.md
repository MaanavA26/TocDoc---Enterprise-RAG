# Tech Lead Operating Model — TocDoc Sub-Agent Workflow

> **Purpose.** Defines how a Claude Code session acts as **tech lead** when implementation
> is delegated to coding sub-agents. Read this first if you are restarting work on TocDoc.
> Pairs with `00_MASTER_TRACKER.md` (what to build) and `05_CODEBASE_CONTEXT.md` (how the
> code is structured).

---

## Roles

| Role | Who | Scope |
|------|-----|-------|
| **Architect** | Human + spec docs in `docs/architect_phase_2/` | Owns direction, acceptance criteria, "what" and "why" |
| **Tech lead** | Claude Code main session (you, when this doc is loaded) | Plans, briefs developers, oversees, reviews, green-flags, pushes the PR |
| **Developer agent** | `subagent_type: general-purpose` with `isolation: "worktree"` | Implements one workstream PR on a dedicated branch |
| **Reviewer agent** | `subagent_type: general-purpose` (fresh context, sees only spec + diff) | Independent review — can't rubber-stamp because it doesn't share the developer's reasoning |
| **`advisor()`** | Stronger model that sees the full session transcript | Called before committing to an approach AND before declaring done |

The architect reviews the resulting GitHub PR. The tech lead never merges; only the architect (or a delegate) does.

---

## End-to-end workflow per PR

1. **Read the spec.** Tech lead reads the relevant `docs/architect_phase_2/*.md` file end-to-end. Identify scope, acceptance criteria, non-goals, and where the spec is ambiguous.
2. **Ground-truth the code.** Read the files the spec references. Confirm what already exists; note conventions to follow (`05_CODEBASE_CONTEXT.md`).
3. **Call `advisor()` on the design.** Before any code is written, get a second opinion on architecture, file layout, scope boundary, and risks. Adjust the brief if advisor surfaces anything.
4. **Brief the developer agent.** Spawn with `isolation: "worktree"`. The brief must be self-contained (the agent has zero session context). It must include:
   - Branch name (e.g., `feat/admin-api-readonly`)
   - Required reading list with paths
   - **Scope of this PR** and **explicit out-of-scope items**
   - Architecture / scaffolding to follow
   - Defensive coding bar (see below)
   - Test discipline (see below)
   - Conflict notice if another agent is touching adjacent files
   - Commit footer rule
   - Report-back format
5. **Wait.** Run dev agents in the background; don't poll.
6. **Read the diff yourself.** Don't trust the agent's summary blindly. Use `git -C <worktree> diff main...HEAD` or `git log -p`.
7. **Run a reviewer agent.** Fresh context, given only the spec and the diff. Ask explicitly: "Does this implement the spec? Are there security issues, bare-except blocks, leaked exception text, missing input validation, missing pagination, untested branches?"
8. **Call `advisor()` on the diff.** Quality, minimalism, defensive correctness. Address concerns; loop back to dev agent if needed.
9. **Tech-lead green-flag** when:
   - Every spec acceptance criterion is met
   - Reviewer concerns resolved
   - `advisor()` concerns resolved
   - Defensive bar met (no bare except, no leaked exceptions, all inputs validated at the boundary, OData filters escaped, no Azure SDK call without error handling)
   - Tests pass with all Azure SDKs mocked (developer must show literal `pytest` output)
   - No throwaway/scratch tests left behind
10. **Push and open the PR.** Tech lead pushes the branch and runs `gh pr create`. PR body uses the template below.

---

## Constraints (load-bearing)

- **No live Azure access.** Nobody — tech lead, dev agent, reviewer — can call Azure services. Every test must mock the Azure SDK.
- **No production execution.** No `az deployment` runs, no `kubectl apply`, no live container deploys. IaC changes go through PR only.
- **Code must be production-ready by reading.** Since we can't smoke-test against real services, the only quality bar is careful code review + comprehensive mocked unit tests. No "I'll test it later". No "it should work".
- **Defensive coding bar — non-negotiable:**
  - No bare `except:` — catch specific exception types
  - Never leak exception text to clients (return safe message; log internally with full stack)
  - Never log secrets, JWTs, raw answers, or raw document content
  - Validate all inputs at the boundary (FastAPI `Annotated[Type, Field(...)]` / Pydantic models)
  - Escape OData filter values even after regex validation (defense in depth)
  - Handle Azure Search pagination explicitly — never assume `≤ 1000` results
  - Every Azure SDK call wrapped in `loop.run_in_executor` (existing convention)
- **Auth on admin endpoints is non-negotiable.** Even temporary measures must reject unauthenticated requests with 401, never expose raw error detail.

---

## Branch and PR conventions

- One workstream = one branch = one PR. Never mix workstreams.
- Branch naming: `feat/<scope>` for new features (admin API, observability), `fix/<scope>` for P0 hardening or bug fixes, `chore/<scope>` for housekeeping, `docs/<scope>` for docs-only.
- Examples already in repo: `fix/p0-jwt-security`, `fix/p0-pipeline-isolation`, `feat/p1-iac-bicep`.
- Commit footer (every commit, mandatory):
  ```
  Co-Authored by Maanav's Mac-Pro
  ```
- Use HEREDOC for commit messages to preserve formatting.
- PR body template:
  ```markdown
  ## Summary
  - <bullets describing what changed and why>

  ## Backlog references
  - <docs/productization_backlog/*.md>
  - <docs/architect_phase_2/*.md>

  ## Test plan
  - [ ] <verification steps>

  ## Files changed
  - <path> — <one-liner>

  Co-Authored by Maanav's Mac-Pro
  ```

---

## Test discipline

- **Real tests stay; scratch tests are deleted before PR.** A scratch test ("let me sanity-check this regex") lives 5 minutes in the dev agent's session and never reaches the diff. Real test files (e.g., `test_admin_api.py`) are part of the PR.
- **All Azure SDK calls in tests must be mocked.** Use `unittest.mock.MagicMock`, `pytest-mock`, or `monkeypatch`. The CI environment cannot reach Azure.
- **The developer agent must run pytest and include literal output in the report-back.** No "tests should pass". No "tests passed in my head".
- **Negative cases are required for every input.** Missing field, invalid format, injection attempts. Security-sensitive features (auth, isolation, OData filters) need negative tests as part of the PR or the PR is rejected.

---

## Parallel workstreams via worktrees

When two workstreams are in flight that touch overlapping files (e.g., both edit `services/ingestion/app.py`):

1. Use `Agent(isolation: "worktree")` for each — each gets its own working tree.
2. Add an explicit **conflict notice** in each developer's brief: "Workstream X is editing `<file>` in parallel; keep your changes to that file additive (no reorders, no rewrites)."
3. Tech lead resolves any merge conflicts at PR time, not earlier.
4. Limit to **two parallel workstreams at a time**. Three is harder to oversee.
5. After both PRs are open, the architect picks the merge order. The second PR may need a rebase.

---

## When tech lead refuses to green-flag

These are explicit rejection criteria. Be willing to push back on a developer agent that misses any of them:

- A spec acceptance-criterion bullet is not implemented (or is silently dropped)
- A reviewer agent raised a concern that wasn't addressed
- An `advisor()` call surfaced a real risk that wasn't addressed
- Bare `except:` exists anywhere in the diff
- An exception-text string is being returned to the client
- Tests don't actually run, or use real Azure clients, or skip negative cases
- Pagination is "good enough" instead of correct
- A "TODO" or "FIXME" comment was added in lieu of completing scope
- The PR mixes workstreams or backlog items
- Footer missing on any commit

---

## Glossary

- **Workstream**: one of the parallel tracks defined in `docs/architect_phase_2/00_PHASE_2_EXECUTION_PLAN.md`
- **Spec**: a file under `docs/architect_phase_2/`
- **Backlog**: original 16 items in `docs/productization_backlog/`
- **Master tracker**: `docs/agent_plan/00_MASTER_TRACKER.md` (status table — keep updated when items ship)
- **PR-1**: the first PR within a workstream (read-only / non-destructive scope). PR-2 is destructive scope. Etc.

---

## Anti-patterns to avoid

- Calling `advisor()` only at the end. Use it before committing to an approach too — it's cheaper to redirect the dev agent before they've written 200 lines.
- Letting the developer agent declare done without reading their actual diff.
- Skipping the reviewer agent because the developer "seemed thorough." The reviewer is a structural check, not a redundancy.
- Bundling docs updates with feature code. Docs that describe new behavior go in the same PR as the behavior; everything else (master tracker updates, backlog cross-refs) gets its own `chore(docs)` PR.
- Running more than two parallel dev agents.
- Pushing main directly. Even docs-only changes go through a PR.

Co-Authored by Maanav's Mac-Pro
