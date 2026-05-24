# Phase 2 Workstream C — Deployment Validation Specification

## Objective

Add automated deployment validation so TocDoc client installations fail early with actionable errors instead of failing later during demos or production traffic.

The previous phase added Bicep infrastructure and installation documentation. This workstream adds the practical validation layer around that deployment path.

## Backlog mapping

- `docs/productization_backlog/12_PLATFORM_Add_IaC_CI_CD_and_repeatable_client_installation_assets.md`
- `docs/productization_backlog/13_QUALITY_Expand_test_strategy_coverage_and_release_gates.md`
- `docs/productization_backlog/07_CONFIG_Normalize_env_secret_bootstrap_and_deployment_profiles.md`

## Required artifact

Create a script:

```text
scripts/validate_deployment.sh
```

Optional later Python version:

```text
scripts/validate_deployment.py
```

Start with Bash because the install guide already uses Azure CLI commands.

## Inputs

The script should accept:

```bash
scripts/validate_deployment.sh \
  --resource-group rg-tocdoc-client \
  --environment prod \
  --ingestion-app tocdoc-ingestion-prod \
  --qna-app tocdoc-qna-prod
```

Optional inputs:
- `--deployment-name main`
- `--expected-index-name tocdoc-index`
- `--skip-health-checks`
- `--output json`

## Validation checks

### 1. Azure CLI login/context

Check:
- Azure CLI is installed.
- User is logged in.
- Subscription is selected.

Failure should explain the command to fix it:

```text
az login
az account set --subscription <subscription-id>
```

### 2. Resource group exists

Check:
- target resource group exists

### 3. Expected resources exist

Check for:
- Azure OpenAI resource
- Azure AI Search resource
- Document Intelligence resource
- Key Vault
- Log Analytics workspace
- Application Insights
- Container Apps Environment
- Ingestion Container App
- QnA Container App

### 4. Container App env vars

Validate ingestion env vars include:
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_VERSION`
- `AZURE_OPENAI_EMBEDDING_MODEL`
- `AZURE_SEARCH_ENDPOINT`
- `INDEX_NAME`
- `DOC_INTELLIGENCE_ENDPOINT`
- `LOG_LEVEL`
- `AZURE_OPENAI_KEY`
- `AZURE_SEARCH_KEY`
- `DOC_INTELLIGENCE_KEY`

Validate QnA env vars include (canonical UPPER_SNAKE names, per P0-7):
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_VERSION`
- `AZURE_OPENAI_LLM_MODEL`
- `AZURE_OPENAI_EMBEDDING_MODEL`
- `AZURE_SEARCH_ENDPOINT`
- `INDEX_NAME`
- `AUDIENCE_ID`
- `AZURE_KEY_VAULT`
- `AZURE_TENANT_ID`
- `LOG_LEVEL`
- `AZURE_OPENAI_KEY`
- `AZURE_SEARCH_KEY`
- `AZURE_CLIENT_ID`
- `AZURE_CLIENT_SECRET`

Important:
- The script should validate names only, not print secret values.
- For backward compatibility with deployments that haven't migrated yet, the
  script may also recognize the pre-P0-7 PascalCase legacy aliases
  (`AzureOpenaiAccountEndpoint` → `AZURE_OPENAI_ENDPOINT`, etc. — see the
  full table in `docs/deployment/INSTALLATION.md`) and emit a warning
  rather than failing. New deployments should match the canonical list above.

### 5. Container App revision status

Check:
- each app has at least one active revision
- latest revision is running or ready

### 6. Health endpoint checks

Use deployment outputs or app FQDNs to call:
- ingestion health endpoint
- QnA health endpoint

Expected:
- ingestion returns healthy status
- QnA returns healthy/ok status

The current install guide references:
- `/upload_pipeline/health`
- `/qna/health`

The implementation agent should verify the actual routes and update docs if route names differ.

### 7. Key Vault readiness

Check:
- Key Vault exists
- required access path is configured for current QnA startup model
- if secrets are expected in Key Vault, verify secret names exist without printing values

Current architecture note:
- QnA still uses service-principal bootstrap for Key Vault loading.
- Managed identity exists as future direction but should not be assumed as active startup auth unless code is changed.

### 8. Azure Search readiness

Check:
- search service exists
- configured index exists, if expected after deployment
- if index is not created until first ingestion, return warning, not hard failure

### 9. Deployment outputs

Check:
- Bicep deployment outputs include FQDNs or resource names required by the install guide

## Output format

Default human-readable output:

```text
[PASS] Azure CLI authenticated
[PASS] Resource group exists: rg-tocdoc-client
[PASS] Container App exists: tocdoc-ingestion-prod
[FAIL] Missing QnA env var: AUDIENCE_ID
[WARN] Search index not found. This may be expected before first ingestion.
```

JSON output option:

```json
{
  "status": "failed",
  "checks": [
    {
      "name": "qna_env_vars",
      "status": "failed",
      "message": "Missing AUDIENCE_ID"
    }
  ]
}
```

Exit codes:
- `0`: all required checks passed
- `1`: one or more required checks failed
- `2`: script usage/config error

Warnings should not fail the script unless `--strict` is added later.

## Documentation updates required

Update:
- `docs/deployment/INSTALLATION.md`
- `infra/README.md`

Add a step after deployment and before smoke testing:

```bash
scripts/validate_deployment.sh \
  --resource-group rg-tocdoc-<client-name> \
  --environment prod \
  --ingestion-app tocdoc-ingestion-prod \
  --qna-app tocdoc-qna-prod
```

## CI/CD follow-up

This script should later be reused in CI/CD.

Future PR can add:
- Bicep build validation
- what-if validation
- Docker image build validation
- unit test gate
- integration smoke test gate

Do not combine full CI/CD rollout in the first validation PR.

## Testing requirements

Add shellcheck-compatible script style if shellcheck is available.

At minimum, test manually against:
- missing resource group
- missing container app
- missing env var
- healthy deployment

If adding Python wrapper/tests, include unit tests for:
- argument parsing
- check result aggregation
- exit code behavior

## Acceptance criteria

This workstream is accepted when:
- `scripts/validate_deployment.sh` exists
- script checks resource presence, env vars, revision state, and health endpoints
- script does not print secrets
- docs include validation command
- failures are actionable
- script exits non-zero for required failed checks

## Non-goals

- Full CI/CD pipeline implementation
- Full synthetic ingestion/QnA integration test
- Managed identity migration
- Replacing existing Bicep template

## Architect note

A deployment runbook without validation is still risky. This script is the bridge between IaC existing in the repo and delivery engineers trusting it in a real client environment.

Co-Authored by Maanav's Mac-Air
