# Priority Issue — Remove Every Mention of GMR From TocDoc

## Priority

P0 — Immediate cleanup required before any client-facing demo, handover, sales discussion, public repository sharing, deployment package sharing, or productization milestone.

## Objective

Remove every possible mention of `GMR` from the complete TocDoc project.

This includes code, comments, configuration files, sample payloads, test data, documentation, diagrams, scripts, notebooks, Docker files, environment examples, prompts, logs, markdown files, deployment assets, and any generated artifacts committed to the repository.

## Why this is urgent

Any leftover reference to GMR creates a serious productization risk:

1. It makes TocDoc look like a client-specific project instead of a reusable enterprise product.
2. It can expose prior client or engagement context accidentally.
3. It weakens sales credibility when demonstrating the product to a different client.
4. It creates legal, confidentiality, and brand-risk concerns.
5. It can confuse developers by preserving obsolete domain assumptions in code or docs.

This is not cosmetic cleanup. It is a product-readiness and confidentiality hardening task.

## Required action

Search the entire repository for every case-insensitive occurrence of:

```text
GMR
gmr
Gmr
```

Also search for likely variants if they exist in the project:

```text
GMR Group
GMR Airports
GMR airport
GMR domain
GMR data
GMR docs
GMR use case
```

If any spelling variants, abbreviations, file names, folder names, comments, or sample values are discovered, remove or replace them as well.

## Replacement guidance

Use neutral product-safe names.

Preferred replacements:

| Old reference type | Replacement |
|---|---|
| client name | `client` |
| specific company name | `enterprise_client` |
| tenant/bot example | `demo_workspace` or `client_a_workspace` |
| document example | `sample_document` |
| domain example | `enterprise_documents` |
| file/folder name | neutral equivalent with no client reference |

Do not replace GMR with another real client name.

## Scope of cleanup

The cleanup must cover at least:

- repository root files
- `README.md`
- `docs/`
- `services/ingestion/`
- `services/qna/`
- `infra/`
- `scripts/`
- tests
- sample payloads
- `.env.example` files
- Docker and compose files
- comments/docstrings
- diagrams and Mermaid blocks
- prompt templates
- generated documentation
- deployment runbooks

## Files and folders to inspect manually

Developers must inspect these locations even if search returns no result:

```text
README.md
docs/
docs/productization_backlog/
docs/architect_phase_2/
docs/deployment/
infra/
services/ingestion/
services/qna/
docker-compose.yml
.env.example files
```

## Required commands

Run these or equivalent commands locally from repository root:

```bash
grep -Rni --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ "GMR" .
grep -Rni --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ "gmr" .
find . -iname "*gmr*" -not -path "*/.git/*"
```

If using ripgrep:

```bash
rg -n -i "gmr|gmr group|gmr airports" .
find . -iname "*gmr*" -not -path "*/.git/*"
```

## Required implementation behavior

For every occurrence found:

1. Determine whether it is a client-specific reference.
2. Replace it with a neutral product-safe term.
3. If it appears in a file name or folder name, rename the file/folder.
4. If it appears in test expectations, update the test and expected output.
5. If it appears in documentation, rewrite the sentence so it reads as product-generic.
6. If it appears in examples, use `client_a`, `demo_workspace`, or `enterprise_client`.
7. Re-run the search until zero occurrences remain.

## Acceptance criteria

This issue is complete only when:

- case-insensitive repository search returns zero occurrences for `gmr`
- no file or folder names contain `gmr`
- no docs, comments, examples, tests, prompts, or configuration files contain `GMR`
- the cleanup PR clearly lists every file changed
- the PR includes the final search command output showing zero matches
- no replacement introduces another real client name

## PR requirements

The PR title should be:

```text
chore(product): remove all GMR references from repository
```

The PR description must include:

```markdown
## Summary
- Removed all GMR/client-specific references from code, docs, tests, examples, and deployment assets.
- Replaced references with product-neutral terms.
- Verified no remaining case-insensitive `gmr` matches exist.

## Verification
```bash
rg -n -i "gmr|gmr group|gmr airports" .
find . -iname "*gmr*" -not -path "*/.git/*"
```

Expected result: no matches.
```

## Non-goals

- Do not rewrite unrelated architecture.
- Do not rename TocDoc.
- Do not replace GMR with another real customer or company name.
- Do not change runtime behavior unless a hardcoded GMR value is part of runtime configuration.

## Architect note

This must be treated as an immediate P0 cleanup before any further product packaging. A reusable enterprise product must not carry old client-specific references anywhere in the repository.

Co-Authored by Maanav's Mac-Air
