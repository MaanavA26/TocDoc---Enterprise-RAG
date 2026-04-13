# 07 — Config: Normalize environment naming, secret bootstrap, and deployment profiles

**Priority:** P0  
**Type:** Configuration / Deployment / Production blocker

## Problem

The current codebase uses inconsistent environment variable naming across services. Ingestion mainly uses uppercase names, while QnA uses several PascalCase or legacy names. Key Vault loading and required environment checks are also not aligned cleanly. This increases setup friction and creates avoidable bootstrap failures.

## Why this matters

- Product installations should be easy to configure repeatedly across different client Azure resource groups.
- Mixed naming conventions create support burden and onboarding mistakes.
- Secret handling and local development flows become harder to reason about.
- Clean configuration design is a major part of making TocDoc feel like a product rather than a custom project.

## Desired outcome

TocDoc should have a clear configuration model with standardized variable naming, deterministic secret loading behavior, and explicit deployment profiles such as local, test, and production.

## Scope

- Standardize environment variable names across both services.
- Define a clean precedence model: direct env vars, secret store injection, optional local `.env` for development.
- Rework startup/bootstrap assumptions so required config validation happens at the right time.
- Update all `.env.example` files and README configuration docs.

## Implementation guidance

- Prefer one canonical naming convention for new variables.
- If backward compatibility is needed, support legacy aliases temporarily but document deprecation.
- Keep configuration validation explicit and early, but not in a way that conflicts with secret bootstrapping.
- Consider centralizing shared configuration models where practical.

## Deliverables

- normalized configuration model
- updated secret bootstrap flow
- refreshed `.env.example` files
- migration notes for legacy env names
- tests for representative config/bootstrap scenarios

## Acceptance criteria

- Both services can be configured consistently using documented variables.
- Key Vault or other secret sources load cleanly without fragile import-time assumptions.
- Local development remains straightforward.
- Documentation reflects the real bootstrap order and required settings.

## Non-goals

- Replacing Azure Key Vault with another secret manager. This issue is about consistency and bootstrap correctness.

## Notes for Codex / Claude

Treat configuration as product UX for operators. A strong config model reduces deployment effort, support cost, and human error.