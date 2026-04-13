# 01 — Security: Enable strict Azure AD JWT validation

**Priority:** P0  
**Type:** Security / Authentication / Production blocker

## Problem

The QnA service currently decodes JWTs with signature verification disabled. That means the middleware is only partially validating the token structure and claims, not the actual cryptographic authenticity of the token. In enterprise terms, this is a hard blocker because a well-formed token could be accepted even if it was not properly signed by Azure AD.

## Why this matters

- This breaks the trust boundary of the product.
- No security-conscious client will approve a system that accepts unsigned or unverified bearer tokens.
- It exposes TocDoc to impersonation risk, unauthorized access, and audit/compliance failure.
- From a sales perspective, this single issue can kill a proof of value during security review.

## Desired outcome

The QnA service must perform full RS256 signature validation against Azure AD-issued JWTs using the tenant JWKS / OpenID metadata flow, while continuing to validate issuer, audience, expiration, and relevant identity claims.

## Scope

- Replace the current decode flow with real signature verification.
- Resolve Azure AD signing keys dynamically from the tenant metadata endpoint.
- Cache signing keys safely to avoid fetching metadata on every request.
- Validate `iss`, `aud`, `exp`, `nbf`, and algorithm restrictions.
- Keep `/health` and OpenAPI assets public only if that is an explicit product decision.
- Fail closed on validation errors.

## Implementation guidance

- Introduce a dedicated token validator instead of keeping all logic inline in middleware.
- Support Azure AD token key rotation.
- Decide whether the product will support single-tenant only or configurable multi-tenant validation.
- Standardize which claim becomes the canonical user identity: `oid`, `upn`, `preferred_username`, or email.
- Produce structured auth failure logs without leaking token contents.

## Deliverables

- hardened auth validator module
- updated middleware integration
- configuration docs for tenant, audience, and metadata endpoint behavior
- tests for valid token, expired token, wrong audience, wrong issuer, invalid signature, and missing identity claim

## Acceptance criteria

- Tokens with invalid signatures are rejected.
- Tokens with wrong issuer or audience are rejected.
- Expired tokens are rejected.
- Valid Azure AD tokens are accepted and mapped to a stable user identity.
- Auth behavior is documented in README / deployment docs.

## Non-goals

- Full role-based authorization design. That can be a separate backlog item after identity validation is fixed.

## Notes for Codex / Claude

Do not patch this superficially. This is a security boundary and should be implemented as a first-class authentication component with tests.