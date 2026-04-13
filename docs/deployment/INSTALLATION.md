# TocDoc — Client Installation Guide

This guide covers a complete, runnable TocDoc deployment into a new Azure
resource group. Following all steps in order results in both services starting
successfully after the container image swap.

## What Bicep wires automatically vs what you provide

| Value | How it is set |
|---|---|
| OpenAI / Search / Doc Intelligence endpoints | Computed from deployed resources → wired as plain env vars |
| API version, model names, index name | Parameters with sensible defaults → wired as plain env vars |
| `AUDIENCE_ID`, `TocdocSPTenantID`, `AZURE_KEY_VAULT` | Parameters → wired as plain env vars |
| API keys (`openAiApiKey`, `searchApiKey`, `docIntelApiKey`) | Passed as `@secure()` params at deploy time → stored as Container App secrets, never in plain config |
| SP credentials (`spClientId`, `spClientSecret`) | Passed as `@secure()` params → stored as Container App secrets (current QnA auth path for Key Vault) |

> **Auth model note**: The QnA service currently uses `ClientSecretCredential`
> (service principal) to load secrets from Key Vault at startup. System-assigned
> managed identities are provisioned and granted Key Vault Secrets User in this
> template as infrastructure preparation for a future migration to
> `ManagedIdentityCredential`. They do not affect the current startup path.

## Prerequisites

- Azure subscription with Contributor access on the target resource group
- Azure CLI ≥ 2.50 logged in (`az login`)
- Docker (for building container images)
- Service principal with Key Vault access created for the QnA service

## Step 1: Provision Azure resources

Gather these values before running the command:
- `<tenant-id>` — Azure AD tenant ID
- `<audience-client-id>` — App registration client ID for JWT audience
- `<openai-key>` — Azure OpenAI API key (from the resource after deploy, or use an existing one)
- `<search-key>` — Azure Cognitive Search admin key
- `<doc-intel-key>` — Document Intelligence API key
- `<sp-client-id>` — Service principal client ID for QnA Key Vault access
- `<sp-client-secret>` — Service principal client secret

```bash
az group create --name rg-tocdoc-<client-name> --location <region>

az deployment group create \
  --resource-group rg-tocdoc-<client-name> \
  --template-file infra/main.bicep \
  --parameters infra/parameters/prod.bicepparam \
  --parameters \
      tenantId=<tenant-id> \
      audienceClientId=<audience-client-id> \
      openAiApiKey=<openai-key> \
      searchApiKey=<search-key> \
      docIntelApiKey=<doc-intel-key> \
      spClientId=<sp-client-id> \
      spClientSecret=<sp-client-secret>
```

The `@secure()` parameters are not logged or shown in Azure deployment history.

Save the deployment outputs — you will need the FQDNs for smoke testing:

```bash
az deployment group show \
  --resource-group rg-tocdoc-<client-name> \
  --name main \
  --query "properties.outputs" -o json
```

## Step 2: Verify container app configuration

Confirm both apps have the expected env/secret names configured before swapping
the image. A missing env var here means the app will fail at startup.

```bash
# Check ingestion env vars
az containerapp show \
  --name tocdoc-ingestion-prod \
  --resource-group rg-tocdoc-<client-name> \
  --query "properties.template.containers[0].env[].name" -o tsv

# Expected output should include:
# AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_VERSION, AZURE_OPENAI_EMBEDDING_MODEL,
# AZURE_SEARCH_ENDPOINT, INDEX_NAME, DOC_INTELLIGENCE_ENDPOINT, LOG_LEVEL,
# AZURE_OPENAI_KEY, AZURE_SEARCH_KEY, DOC_INTELLIGENCE_KEY

# Check QnA env vars
az containerapp show \
  --name tocdoc-qna-prod \
  --resource-group rg-tocdoc-<client-name> \
  --query "properties.template.containers[0].env[].name" -o tsv

# Expected output should include:
# AzureOpenaiAccountEndpoint, AzureOpenaiApiVersion, AzureOpenaiLlmModel,
# AZURE_OPENAI_EMBEDDING_MODEL, AzureSearchEndpoint, INDEX_NAME,
# AUDIENCE_ID, AZURE_KEY_VAULT, TocdocSPTenantID, LOG_LEVEL,
# TocdocOpenAIKey, AzureSearchKey, TocdocSPClientID, TocdocSPSecretValue
```

## Step 3: Build and deploy container images

```bash
# Build and push (replace <your-registry> with your container registry)
docker build -t <your-registry>/tocdoc-ingestion:latest ./services/ingestion
docker push <your-registry>/tocdoc-ingestion:latest

docker build -t <your-registry>/tocdoc-qna:latest ./services/qna
docker push <your-registry>/tocdoc-qna:latest

# Swap the placeholder image — all env vars and secrets are already configured
az containerapp update \
  --name tocdoc-ingestion-prod \
  --resource-group rg-tocdoc-<client-name> \
  --image <your-registry>/tocdoc-ingestion:latest

az containerapp update \
  --name tocdoc-qna-prod \
  --resource-group rg-tocdoc-<client-name> \
  --image <your-registry>/tocdoc-qna:latest
```

After the image swap the real services start immediately — all required env vars
and secret values are already present in the container app configuration.

## Step 4: Smoke test

```bash
INGESTION_FQDN=$(az deployment group show \
  --resource-group rg-tocdoc-<client-name> --name main \
  --query "properties.outputs.ingestionAppFqdn.value" -o tsv)

QNA_FQDN=$(az deployment group show \
  --resource-group rg-tocdoc-<client-name> --name main \
  --query "properties.outputs.qnaAppFqdn.value" -o tsv)

curl https://${INGESTION_FQDN}/upload_pipeline/health
curl https://${QNA_FQDN}/qna/health

# Expected: {"status":"healthy"} and {"status":"ok","qna_module":"loaded",...}
```

## Estimated installation time

| Step | Time |
|---|---|
| Infrastructure provisioning (Step 1) | 10–15 min |
| Config verification (Step 2) | 2 min |
| Image build + push + update (Step 3) | 5–10 min |
| Smoke test (Step 4) | 2 min |
| **Total** | **~20–30 min** |
