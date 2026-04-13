# TocDoc — Client Installation Guide

This guide covers deploying TocDoc into a new client Azure resource group.

## Prerequisites
- Azure subscription with Contributor access on the target resource group
- Azure CLI logged in (`az login`)
- Docker (for building images locally)
- GitHub access to `MaanavA26/TocDoc---Enterprise-RAG`

## Step 1: Provision Azure resources

```bash
az group create --name rg-tocdoc-<client-name> --location <region>

az deployment group create \
  --resource-group rg-tocdoc-<client-name> \
  --template-file infra/main.bicep \
  --parameters infra/parameters/prod.bicepparam \
  --parameters tenantId=<tenant-id> audienceClientId=<audience-client-id>
```

The deployment provisions:
- Azure OpenAI (S0) with endpoint wired into both apps
- Azure Cognitive Search (S1 + semantic ranker) with endpoint wired into both apps
- Azure Document Intelligence with endpoint wired into the ingestion app
- Key Vault (soft-delete 90 days, RBAC-enabled)
- Log Analytics workspace + Application Insights (connection string wired into both apps)
- Container Apps Environment with log forwarding
- Ingestion + QnA container apps with system-assigned managed identities
- Key Vault Secrets User role assignments so apps load API keys at startup

Save the deployment outputs — you will need the FQDNs for smoke testing:

```bash
az deployment group show \
  --resource-group rg-tocdoc-<client-name> \
  --name main \
  --query properties.outputs
```

## Step 2: Store API keys in Key Vault

The Bicep template wires all endpoint URLs automatically. You only need to
store the API keys (secrets) that cannot be computed from resource properties:

```bash
KV=<key-vault-name-from-deployment-output>

# Azure OpenAI key
az keyvault secret set --vault-name $KV --name TocdocOpenAIKey \
  --value "<azure-openai-api-key>"

# Azure Cognitive Search admin key
az keyvault secret set --vault-name $KV --name AzureSearchKey \
  --value "<azure-search-admin-key>"

# Document Intelligence key (for ingestion service)
az keyvault secret set --vault-name $KV --name DocIntelligenceKey \
  --value "<document-intelligence-api-key>"

# Service principal credentials (if used for additional auth)
az keyvault secret set --vault-name $KV --name TocdocSPClientID \
  --value "<service-principal-client-id>"
az keyvault secret set --vault-name $KV --name TocdocSPSecretValue \
  --value "<service-principal-secret>"
```

The container apps' system-assigned managed identities have been granted
**Key Vault Secrets User** by Bicep. No additional access policy setup is needed.

## Step 3: Build and deploy container images

Build the real images and swap out the placeholder:

```bash
# Build and push (replace <your-registry> with your container registry)
docker build -t <your-registry>/tocdoc-ingestion:latest ./services/ingestion
docker push <your-registry>/tocdoc-ingestion:latest

docker build -t <your-registry>/tocdoc-qna:latest ./services/qna
docker push <your-registry>/tocdoc-qna:latest

# Swap the placeholder image — env vars and KV access are already configured
az containerapp update \
  --name tocdoc-ingestion-<environment> \
  --resource-group rg-tocdoc-<client-name> \
  --image <your-registry>/tocdoc-ingestion:latest

az containerapp update \
  --name tocdoc-qna-<environment> \
  --resource-group rg-tocdoc-<client-name> \
  --image <your-registry>/tocdoc-qna:latest
```

The apps will start, read their endpoint URLs from the pre-configured env vars,
and load API keys from Key Vault using their managed identities automatically.

## Step 4: Smoke test

Use the FQDNs from the deployment outputs:

```bash
INGESTION_FQDN=<ingestionAppFqdn from deployment output>
QNA_FQDN=<qnaAppFqdn from deployment output>

curl https://${INGESTION_FQDN}/upload_pipeline/health
curl https://${QNA_FQDN}/qna/health

# Expected response: {"status": "healthy"} / {"status": "ok"}
```

## Estimated installation time
- Infrastructure provisioning: 10–15 minutes
- Secret configuration: 5 minutes
- Container image build + push: 5–10 minutes
- Container app update + startup: 3–5 minutes
- Smoke testing: 2 minutes
- **Total: ~25–40 minutes**
