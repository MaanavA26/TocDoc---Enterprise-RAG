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

## Secret handling

This repository contains **no real credentials** — secrets are loaded at runtime
from environment variables and **Azure Key Vault**, and `.env*` files are
git-ignored. If you believe a secret has been committed, report it privately as
above so it can be rotated.

## Supported versions

The project is under active development; security fixes are applied to the
`main` branch. Dependency CVEs are tracked automatically via Dependabot and the
CI security checks (`bandit`, `pip-audit`).
