# 08 — Runtime: Harden containers, CORS, logging, and cloud-native defaults

**Priority:** P0  
**Type:** Runtime operations / Deployment hygiene / Production blocker

## Problem

The current runtime configuration still carries several development-oriented defaults, including permissive CORS and dev-like container startup behavior. Logging is serviceable, but it is not yet aligned to a cloud-native production operating model.

## Why this matters

- Runtime defaults become product behavior in client environments.
- Overly permissive defaults increase security risk.
- Development flags or local-file logging patterns are not ideal for managed cloud operations.
- Operators need clean stdout/stderr logs, health behavior, and predictable runtime settings.

## Desired outcome

TocDoc should ship with production-safe runtime defaults, environment-driven overrides, and deployment guidance for cloud-native hosting platforms such as Azure Container Apps, App Service, or AKS.

## Scope

- Remove development-only runtime flags from production container defaults.
- Make CORS configurable and default-safe.
- Review log formatting, correlation IDs, and whether local file logging should remain enabled in containers.
- Revisit worker counts, startup behavior, and health/readiness semantics.

## Implementation guidance

- Prefer stdout/stderr structured logging for container platforms.
- Keep developer convenience possible through explicit local profiles, not by default in production images.
- Ensure health endpoints remain lightweight and reliable.
- Document recommended runtime settings per hosting target.

## Deliverables

- updated Dockerfiles and runtime commands
- configurable CORS model
- cloud-native logging defaults
- deployment notes for recommended hosting targets

## Acceptance criteria

- Production container images do not run with development-only settings.
- CORS is controlled through configuration and is not globally open by default.
- Logs are suitable for Azure-native ingestion into monitoring platforms.
- Runtime docs clearly distinguish local dev from production behavior.

## Non-goals

- Building a full frontend gateway or WAF solution. This item is about service runtime posture.

## Notes for Codex / Claude

Think like an operator and security reviewer here. The right defaults matter as much as the code path itself.