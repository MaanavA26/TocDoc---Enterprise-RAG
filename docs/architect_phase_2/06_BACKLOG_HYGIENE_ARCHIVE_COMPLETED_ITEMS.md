# Priority Issue — Clean Completed Productization Backlog Items

## Priority

P0 — Backlog hygiene and execution traceability.

This should be done before starting large new Phase 2 implementation PRs so developers, reviewers, and future agents can clearly distinguish active work from completed hardening work.

## Objective

Clean up `docs/productization_backlog/` so completed items are no longer mixed with active productization work.

The folder should remain easy to navigate, with clear traceability from:

```text
backlog item → implementing PR → current status → remaining follow-up work
```

## Why this matters

The productization backlog has already served its first purpose: guiding the initial hardening PRs. Several items are now completed or mostly completed. If completed items remain mixed with active issues, future agents may:

- rework already-completed items
- misunderstand current priorities
- duplicate PRs
- miss remaining active gaps
- confuse P0 historical blockers with current P0 work

Backlog cleanup is not cosmetic. It improves execution discipline and makes the repo easier for Codex, Claude, and human reviewers to operate from.

## Current completed or mostly completed backlog areas

The cleanup agent should review the merged PRs and update status accordingly.

Known completed/mostly completed areas include:

| Backlog item | Status | Evidence |
|---|---|---|
| `01_SECURITY_Enable_strict_Azure_AD_JWT_validation.md` | Completed | PR #4 merged |
| `02_ISOLATION_Enforce_bot_tag_tenant_scoping_in_retrieval.md` | Completed / keep follow-up notes for naming | PR #2 merged |
| `03_CONCURRENCY_Remove_global_request_state_from_qna_pipeline.md` | Completed | PR #2 merged |
| `04_INGESTION_Add_deterministic_chunk_ids_and_reindex_delete_lifecycle.md` | Mostly completed | PR #1 merged; admin lifecycle APIs still remain separately |
| `05_RETRIEVAL_Implement_true_token_aware_chunking_and_eval.md` | Partially completed | token-aware chunking completed; evaluation loop still open |
| `08_RUNTIME_Harden_containers_cors_logging_and_cloud_native_defaults.md` | Completed | PR #3 merged |
| `12_PLATFORM_Add_IaC_CI_CD_and_repeatable_client_installation_assets.md` | Partially completed | Bicep/install guide completed in PR #5; CI/CD and validation still open |

The agent must verify each item before moving or marking it.

## Required cleanup model

Use a traceable folder structure instead of deleting history.

Recommended structure:

```text
docs/productization_backlog/
  00_BACKLOG_INDEX.md
  active/
    ...active backlog files...
  completed/
    ...completed backlog files...
  partially_completed/
    ...items where some acceptance criteria remain...
```

If moving files is too invasive for one PR, acceptable first step:

```text
docs/productization_backlog/
  00_BACKLOG_INDEX.md
  COMPLETED_ITEMS.md
  ACTIVE_ITEMS.md
  PARTIALLY_COMPLETED_ITEMS.md
  <existing files retained temporarily>
```

But the preferred final state is a clean folder split by status.

## Required actions

### 1. Audit all backlog files

Review every file under:

```text
docs/productization_backlog/
```

Classify each item as:

- `completed`
- `partially_completed`
- `active`
- `superseded`

### 2. Preserve traceability

Every completed or partially completed item must include:

- status
- implementing PR number(s)
- short completion summary
- remaining follow-up, if any

Example:

```markdown
## Status

Completed by PR #4: `fix(auth): implement strict Azure AD RS256 JWT signature validation`.

## Remaining follow-up

None for this backlog item.
```

### 3. Update index

Update `00_BACKLOG_INDEX.md` so it reflects the new current state.

It should include:

- completed items
- partially completed items
- active Phase 2 items
- archived/superseded items if any
- clear next-priority order

### 4. Avoid deleting useful context

Do not delete backlog files unless they are moved or replaced with an equivalent archived version.

History matters because future developers need to know why decisions were made.

### 5. Align with Phase 2 architect docs

The active backlog should now point developers toward:

- `docs/architect_phase_2/00_PHASE_2_EXECUTION_PLAN.md`
- `docs/architect_phase_2/01_ADMIN_API_SPEC.md`
- `docs/architect_phase_2/02_OBSERVABILITY_SPEC.md`
- `docs/architect_phase_2/03_DEPLOYMENT_VALIDATION_SPEC.md`
- `docs/architect_phase_2/04_BOT_TAG_DECISION_RECORD.md`
- `docs/architect_phase_2/05_PRODUCT_NEUTRAL_NAMING_POLICY.md`

## Suggested status classification

Initial expected classification:

### Completed

- `01_SECURITY_Enable_strict_Azure_AD_JWT_validation.md`
- `02_ISOLATION_Enforce_bot_tag_tenant_scoping_in_retrieval.md`
- `03_CONCURRENCY_Remove_global_request_state_from_qna_pipeline.md`
- `08_RUNTIME_Harden_containers_cors_logging_and_cloud_native_defaults.md`

### Partially completed

- `04_INGESTION_Add_deterministic_chunk_ids_and_reindex_delete_lifecycle.md`
- `05_RETRIEVAL_Implement_true_token_aware_chunking_and_eval.md`
- `12_PLATFORM_Add_IaC_CI_CD_and_repeatable_client_installation_assets.md`
- `13_QUALITY_Expand_test_strategy_coverage_and_release_gates.md`

### Active

- `06_API_Harden_error_contracts_request_validation_and_response_schema.md`
- `07_CONFIG_Normalize_env_secret_bootstrap_and_deployment_profiles.md`
- `09_OBSERVABILITY_Add_telemetry_audit_logs_and_operational_metrics.md`
- `10_PRODUCT_Add_admin_APIs_for_index_management_and_tenant_operations.md`
- `11_CONNECTORS_Add_connector_based_ingestion_for_blob_sharepoint_upload.md`
- `14_ROADMAP_Retrieval_quality_upgrades_reranking_metadata_page_citations.md`
- `15_PRODUCT_Define_packaging_tiers_and_deployment_operating_model.md`

This classification must be verified against actual code before finalizing.

## Acceptance criteria

This issue is complete when:

- completed backlog items are clearly separated from active work
- partially completed items show exactly what remains
- `00_BACKLOG_INDEX.md` reflects the current true state
- no completed P0 item appears as if it is still untouched
- active Phase 2 priorities are easy to identify
- each completed item references its implementing PR
- no useful historical context is lost

## PR requirements

Recommended PR title:

```text
chore(backlog): archive completed productization backlog items
```

PR description should include:

```markdown
## Summary
- Reclassified productization backlog items into completed, partially completed, and active work.
- Added PR traceability for completed items.
- Updated backlog index to reflect current Phase 2 priorities.

## Completed items archived
- PR #1: ingestion deterministic IDs and token-aware chunking
- PR #2: tenant isolation and concurrency cleanup
- PR #3: runtime hardening
- PR #4: strict Azure AD JWT validation
- PR #5: Azure Bicep IaC and installation guide

## Verification
- Reviewed all files under `docs/productization_backlog/`.
- Confirmed active Phase 2 work points to `docs/architect_phase_2/`.
```

## Non-goals

- Do not implement admin APIs in this PR.
- Do not implement observability in this PR.
- Do not change runtime code.
- Do not remove backlog history.
- Do not close partially completed items unless all acceptance criteria are met.

## Architect note

The backlog folder should be treated as the product execution control center. If it is stale, coding agents will make stale decisions. Clean traceability now will prevent duplicate work and confusion during Phase 2.

Co-Authored by Maanav's Mac-Air
