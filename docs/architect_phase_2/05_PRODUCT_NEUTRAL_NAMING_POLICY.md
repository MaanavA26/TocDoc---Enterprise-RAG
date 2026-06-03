# Product-Neutral Naming Policy

## Status

**Legacy client-name removal: COMPLETE.** A case-insensitive repository search returns **zero** occurrences of the legacy client name across code, comments, configuration, sample payloads, tests, docs, diagrams, prompts, Docker/compose files, `.env.example` files, deployment assets, and file/folder names. This document supersedes the prior P0 removal-priority spec (that spec named the client and so was itself the final reference; it has been removed and replaced by this policy).

## Why

TocDoc is a reusable enterprise product, not a client-specific deployment. Leftover client-specific references would:

1. Make the product look bespoke rather than reusable.
2. Risk exposing prior engagement context.
3. Weaken sales credibility with other clients.
4. Create confidentiality, brand, and legal concerns.
5. Embed obsolete domain assumptions in code and docs.

This is product-readiness and confidentiality hardening, mandatory before any client-facing demo, handover, public repository sharing, or productization milestone.

## Policy (ongoing)

- **Never** introduce a real client or company name anywhere in the repository — code, comments, config, tests, sample data, docs, diagrams, prompts, deployment assets, file/folder names, or commit messages.
- Use product-neutral placeholders instead:

  | Reference type | Use |
  |---|---|
  | a client / tenant | `client`, `client_a` |
  | a specific company | `enterprise_client` |
  | a tenant / bot example | `demo_workspace`, `client_a_workspace` |
  | a document example | `sample_document` |
  | a domain example | `enterprise_documents` |
  | file / folder name | neutral equivalent, no client reference |

- Never substitute one real client name for another.
- `bot_tag` values in examples and tests must be neutral (`client_a_hr`, `demo_workspace`, …).

## Verification (run before any public share)

```bash
grep -Rni --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ "<legacy-client-name>" .
find . -iname "*<legacy-client-name>*" -not -path "*/.git/*"
```

Expected result: **no matches.** Re-run as part of the public-readiness checklist; any new match is a release blocker.

## Non-goals

- Do not rename the product (TocDoc).
- Do not rewrite unrelated architecture.
- Git history rewriting is out of scope; this policy governs the working tree and all new commits.
