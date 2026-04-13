# TocDoc Productization Backlog

This folder is the working issue pack for turning TocDoc from a strong engineering build into a repeatable enterprise product that can be deployed inside a client Azure resource group.

## Why this backlog exists

The current codebase already proves the core value proposition: PDF ingestion, hybrid retrieval, conversation-aware rephrasing, and cited answers over enterprise documents. The next phase is productization. That means security hardening, tenant isolation, deployment repeatability, operational visibility, admin controls, and cleaner installation workflows.

These files are intentionally written as issue-ready work items for coding agents such as Codex and Claude. Each item includes:
- the problem being solved
- why it matters commercially and technically
- implementation guidance
- expected deliverables
- acceptance criteria
- non-goals where relevant

## Priority order

### P0 — must be fixed before claiming production readiness
1. `01_SECURITY_Enable_strict_Azure_AD_JWT_validation.md`
2. `02_ISOLATION_Enforce_bot_tag_tenant_scoping_in_retrieval.md`
3. `03_CONCURRENCY_Remove_global_request_state_from_qna_pipeline.md`
4. `04_INGESTION_Add_deterministic_chunk_ids_and_reindex_delete_lifecycle.md`
5. `05_RETRIEVAL_Implement_true_token_aware_chunking_and_eval.md`
6. `06_API_Harden_error_contracts_request_validation_and_response_schema.md`
7. `07_CONFIG_Normalize_env_secret_bootstrap_and_deployment_profiles.md`
8. `08_RUNTIME_Harden_containers_cors_logging_and_cloud_native_defaults.md`

### P1 — required for repeatable client delivery
9. `09_OBSERVABILITY_Add_telemetry_audit_logs_and_operational_metrics.md`
10. `10_PRODUCT_Add_admin_APIs_for_index_management_and_tenant_operations.md`
11. `11_CONNECTORS_Add_connector_based_ingestion_for_blob_sharepoint_upload.md`
12. `12_PLATFORM_Add_IaC_CI_CD_and_repeatable_client_installation_assets.md`
13. `13_QUALITY_Expand_test_strategy_coverage_and_release_gates.md`

### P2 — product differentiation and scale-up
14. `14_ROADMAP_Retrieval_quality_upgrades_reranking_metadata_page_citations.md`
15. `15_PRODUCT_Define_packaging_tiers_and_deployment_operating_model.md`

## Execution guidance for Codex and Claude

- Do not treat these as independent cosmetic tickets. Several are architectural dependencies for later work.
- Prefer small PRs that fully close one backlog file at a time.
- Preserve the product direction: dedicated client deployment in the client’s Azure environment is the primary commercialization path.
- Avoid introducing breaking API changes unless the issue explicitly calls for them and the README / docs / tests are updated in the same change.
- Every completed item should update the README, relevant `.env.example` files, and tests where applicable.

## Definition of done for the backlog

An item is not done until:
- code is implemented
- tests are added or updated
- deployment/configuration docs are updated
- operational behavior is documented where relevant
- any security-sensitive behavior has explicit validation coverage

## Review rhythm

Future PRs should be reviewed against this backlog. If a PR partially addresses an item, the PR description should reference the backlog file name and state exactly which acceptance criteria were completed.
