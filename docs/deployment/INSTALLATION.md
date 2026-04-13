# TocDoc — Client Installation Guide

This guide covers deploying TocDoc into a new client Azure resource group.

## Prerequisites
- Azure subscription with Contributor access on the target resource group
- Azure CLI logged in (`az login`)
- Docker (for building images locally if not using CI/CD)
- GitHub access to `MaanavA26/TocDoc---Enterprise-RAG`

## Step 1: Provision Azure resources

```bash
az group create --name rg-tocdoc-<client-name> --location <region>

az deployment group create \
  --resource-group rg-tocdoc-<client-name> \
  --template-file infra/main.bicep \
  --parameters infra/parameters/prod.bicepparam \
  --parameters tenantId=<tenant-id> audienceClientId=<audience-id>
```

Save the deployment outputs — you will need the endpoints for Step 2.

## Step 2: Configure Key Vault secrets

Store the following in the provisioned Key Vault:

| Secret name | Value |
|------------|-------|
| `AzureOpenaiAccountEndpoint` | From deployment output |
| `TocdocOpenAIKey` | From Azure OpenAI resource |
| `AzureOpenaiApiVersion` | `2024-02-01` |
| `AzureOpenaiLlmModel` | `gpt-4o-mini` |
| `AzureSearchEndpoint` | From deployment output |
| `AzureSearchKey` | From Azure Search resource |
| `TocdocSPTenantID` | Azure AD tenant ID |
| `TocdocSPClientID` | Service principal client ID |
| `TocdocSPSecretValue` | Service principal secret |

## Step 3: Deploy container images

Images are published to GitHub Container Registry on every merge to main.

```bash
# Pull latest images
docker pull ghcr.io/maanava26/tocdoc-ingestion:latest
docker pull ghcr.io/maanava26/tocdoc-qna:latest

# Deploy to Azure Container Apps
az containerapp update \
  --name tocdoc-ingestion-prod \
  --resource-group rg-tocdoc-<client-name> \
  --image ghcr.io/maanava26/tocdoc-ingestion:latest

az containerapp update \
  --name tocdoc-qna-prod \
  --resource-group rg-tocdoc-<client-name> \
  --image ghcr.io/maanava26/tocdoc-qna:latest
```

## Step 4: Smoke test

```bash
# Health checks
curl https://<ingestion-app-fqdn>/upload_pipeline/health
curl https://<qna-app-fqdn>/qna/health

# Expected response: {"status": "ok"}
```

## Estimated installation time
- Infrastructure provisioning: 10–15 minutes
- Secret configuration: 10 minutes
- Container deployment: 5 minutes
- Smoke testing: 5 minutes
- **Total: ~30–45 minutes**
