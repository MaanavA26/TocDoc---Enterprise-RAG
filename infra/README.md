# TocDoc Infrastructure as Code

Azure Bicep templates for deploying TocDoc into a client Azure resource group.

## Prerequisites
- Azure CLI 2.50+
- A target Azure resource group
- An Azure AD app registration (for JWT audience)
- A service principal with Key Vault access for the QnA service

## What Bicep wires automatically vs what you provide

| Value | How it is set |
|---|---|
| OpenAI / Search / Doc Intelligence endpoints | Computed from deployed resources → wired as plain env vars |
| API version, model names, index name | Parameters with sensible defaults → wired as plain env vars |
| `AUDIENCE_ID`, `TocdocSPTenantID`, `AZURE_KEY_VAULT` | Parameters → wired as plain env vars |
| API keys (`openAiApiKey`, `searchApiKey`, `docIntelApiKey`) | Passed as `@secure()` params → stored as Container App secrets, never in plain config |
| SP credentials (`spClientId`, `spClientSecret`) | Passed as `@secure()` params → stored as Container App secrets (current QnA auth path for Key Vault) |

> **Auth model note**: The QnA service currently uses `ClientSecretCredential`
> (service principal) to load secrets from Key Vault at startup. System-assigned
> managed identities are provisioned and granted Key Vault Secrets User in this
> template as infrastructure preparation for a future migration to
> `ManagedIdentityCredential`. They do not affect the current startup path.

## Deploy

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

## Resources provisioned

| Resource | Purpose | SKU (prod) |
|----------|---------|-----------|
| Azure OpenAI | LLM + embeddings | S0 |
| Azure Cognitive Search | Vector + keyword index | S1 |
| Azure Document Intelligence | PDF extraction | S0 |
| Azure Key Vault | SP credentials at startup (future: managed identity) | Standard |
| Log Analytics | Centralized logging | PerGB2018 |
| Application Insights | Telemetry | Linked to Log Analytics |
| Container Apps Environment | Container hosting | Consumption |
| Ingestion Container App | Document ingestion service (port 5501) | — |
| QnA Container App | Question-answering service (port 5500) | — |

## After deployment

All environment variables and secrets are wired by the Bicep template. No manual
Key Vault secret population or role assignment steps are required.

1. **Verify configuration** — confirm expected env var names are present before swapping the image:
   ```bash
   az containerapp show \
     --name tocdoc-ingestion-prod \
     --resource-group rg-tocdoc-<client-name> \
     --query "properties.template.containers[0].env[].name" -o tsv
   ```

2. **Swap the placeholder image** — push your built image and update the container app:
   ```bash
   az containerapp update \
     --name tocdoc-ingestion-prod \
     --resource-group rg-tocdoc-<client-name> \
     --image <your-registry>/tocdoc-ingestion:latest

   az containerapp update \
     --name tocdoc-qna-prod \
     --resource-group rg-tocdoc-<client-name> \
     --image <your-registry>/tocdoc-qna:latest
   ```

3. **Smoke test**:
   ```bash
   curl https://<ingestion-fqdn>/upload_pipeline/health
   curl https://<qna-fqdn>/qna/health
   ```

See `docs/deployment/INSTALLATION.md` for the complete step-by-step runbook.
