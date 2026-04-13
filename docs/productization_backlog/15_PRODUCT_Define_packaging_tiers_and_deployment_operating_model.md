# 15 — Product: Define packaging tiers and the deployment operating model

**Priority:** P2  
**Type:** Product strategy / Commercialization / Operating model

## Problem

The repo now has a promising technical direction, but the commercial packaging and delivery model are not yet written down as product decisions. Without this, engineering can drift toward features that are technically interesting but commercially misaligned.

## Why this matters

- Product direction affects architecture decisions.
- Your stated go-to-market model is not “shared SaaS first”; it is a deployable product installed into client Azure environments.
- Engineering priorities should reflect how the product will actually be sold, delivered, secured, and supported.
- Clear packaging makes it easier to position TocDoc to potential clients.

## Desired outcome

Define the first commercial operating model for TocDoc and use it to guide roadmap decisions.

## Recommended base direction

The default sellable model should be:
- dedicated deployment per client Azure resource group
- client-owned data plane and cloud resources
- TocDoc deployed as a managed installable platform
- optional delivery / support services layered on top

Possible packaging tiers could include:
- Starter: manual upload + QnA + basic monitoring
- Enterprise: connector ingestion + admin APIs + auditability + stronger support
- Premium: advanced retrieval quality, governance features, branded deployment experience

## Scope

- Write down deployment archetypes and support boundaries.
- Define what is included in each packaging tier.
- Clarify recommended hosting targets for v1.
- Identify which backlog items are mandatory for the first sellable tier.

## Deliverables

- product packaging document
- deployment operating model guidance
- mapping from packaging tier to technical backlog requirements

## Acceptance criteria

- Engineering and delivery resources can explain how TocDoc will be sold and deployed.
- Product tiers align with the backlog and architecture roadmap.
- Future technical decisions can be evaluated against a written commercial model.

## Non-goals

- Final pricing strategy. This issue is about packaging and operating model clarity first.

## Notes for Codex / Claude

This is not a pure business document. It should actively influence roadmap sequencing and product architecture choices.