# 12 — Platform: Add IaC, CI/CD, and repeatable client installation assets

**Priority:** P1  
**Type:** Platform engineering / Delivery acceleration / Commercial readiness

## Problem

The current repo describes how TocDoc can run, but it does not yet provide a full delivery accelerator for repeatable installations into client Azure environments. Without IaC and deployment automation, every client setup risks becoming a bespoke engineering exercise.

## Why this matters

- Your business model depends on repeatable deployment into client resource groups.
- Installation speed directly affects delivery cost, margin, and client confidence.
- Infrastructure consistency improves security, operability, and support.
- CI/CD and release assets reduce regression risk as the product evolves.

## Desired outcome

TocDoc should ship with deployment assets that let an engineer or delivery lead stand up the product in a new Azure environment with minimal custom effort.

## Scope

- Create infrastructure-as-code for the recommended hosting model.
- Define resource expectations for Azure OpenAI, AI Search, Document Intelligence, Key Vault, monitoring, and compute.
- Add CI/CD pipelines for validation, image build, and deployment workflows.
- Document a reference deployment architecture for client environments.

## Implementation guidance

- Prioritize the simplest sellable hosting target first, such as Azure Container Apps or App Service, before more complex AKS footprints unless required.
- Keep deployment variables aligned with the normalized configuration model.
- Include environment separation guidance for dev, test, and prod.
- Treat deployment assets as product artifacts, not one-off infra scripts.

## Deliverables

- IaC templates or modules
- CI/CD workflow definitions
- deployment runbook / operator guide
- reference architecture documentation

## Acceptance criteria

- A new client environment can be provisioned and configured using documented assets.
- CI validates core code quality before release.
- Container images and deployment artifacts are produced consistently.
- Deployment docs are usable by a technical delivery resource without tribal knowledge.

## Non-goals

- Supporting every cloud provider. This item is about making Azure deployment repeatable first.

## Notes for Codex / Claude

This issue is central to commercialization. The code alone is not the product; the installation system is part of the product too.