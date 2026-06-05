# Security Policy

## Reporting a vulnerability

**Please do not open a public issue for security vulnerabilities.**

Report suspected vulnerabilities privately via GitHub's [private vulnerability
reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
(the **"Report a vulnerability"** button under the repository's **Security** tab),
or by contacting the maintainers directly.

Please include:

- a description of the issue and its potential impact,
- steps to reproduce (a minimal proof-of-concept if possible),
- affected component(s) and version/commit.

We aim to acknowledge reports within a few business days and will keep you
updated on remediation progress. Please give us reasonable time to ship a fix
before any public disclosure.

## Scope

TocDoc is deployed **into each client's own Azure subscription**; the client owns
all data, compute, and Azure resources. Tenant isolation within a deployment is
enforced by the `bot_tag` filter at the search layer. Reports most relevant to
this project include:

- authentication / authorization bypass (JWT validation, the admin-token guard),
- cross-tenant data exposure (`bot_tag` isolation),
- injection (e.g. OData filter injection in the admin/search layers),
- secret handling / leakage in logs, errors, or artifacts.

## Security architecture (controls in place)

These are the controls implemented on `main`. They describe the security posture
at a high level; they are not an exhaustive design document.

- **Authentication.** The Q&A API validates Azure AD **RS256 JWTs** against the
  tenant JWKS with the issuer and audience pinned, audience verification
  required, and expiry checked — failing **closed** on any error. JWKS lookups
  are negatively cached / throttled so unknown-key requests cannot drive
  repeated outbound refetches.
- **Multi-tenant isolation.** Retrieval is scoped to a `bot_tag` filter at the
  search layer. Within a tenant, workspace binding is enforced **by default**:
  the request's `bot_tag` must be allowed for the caller's token tenant
  (`tid`), and the service fails closed otherwise.
- **Administrative & ingestion access.** Admin endpoints and the ingestion
  upload endpoint require an admin token, and the ingestion service's ingress is
  **internal by default**. The Q&A API is the only externally exposed surface and
  is JWT-authenticated.
- **Injection defenses.** Tenant identifiers are format-validated and
  single-quote-escaped before reaching any OData filter, at every sink. The
  upload path resolves filesystem inputs against an allowed root (realpath
  containment) to prevent traversal.
- **Error & log hygiene.** All errors return a structured envelope with a
  correlation `X-Request-ID` and **never** leak exception text or stack traces.
  Logs carry metadata only — user queries, model answers, conversation history,
  tokens, and document content are not logged.
- **Abuse & availability controls.** Public endpoints enforce application-level
  rate limiting (HTTP 429), outbound Azure/LLM calls have timeouts, and upload
  size / batch limits are enforced.
- **Supply chain & SDLC.** CI hard-gates `pip-audit` and `bandit`, runs **CodeQL**
  code scanning and a test-coverage floor, and Dependabot tracks dependency CVEs.
  Containers run as a non-root user with a hardened security context.
- **Deployment model.** Deployed into the client's own Azure subscription using
  managed identity and **Key Vault** references for secrets, with parameters to
  restrict data-plane network access and disable local (key) auth where the
  environment supports it.

## Secret handling

This repository contains **no real credentials** — secrets are loaded at runtime
from environment variables and **Azure Key Vault**, and `.env*` files are
git-ignored. If you believe a secret has been committed, report it privately as
above so it can be rotated.

## Supported versions

The project is under active development; security fixes are applied to the
`main` branch. Dependency CVEs are tracked automatically via Dependabot and the
CI security checks (`bandit`, `pip-audit`).
