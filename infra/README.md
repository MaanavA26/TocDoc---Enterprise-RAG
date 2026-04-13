# TocDoc Infrastructure as Code

Azure Bicep templates for deploying TocDoc into a client Azure resource group.

## Prerequisites
- Azure CLI 2.50+
- A target Azure resource group
- An Azure AD app registration (for JWT audience and Key Vault access)

## Deploy

```bash
# Create resource group
az group create --name rg-tocdoc-client --location eastus

# Deploy infrastructure
az deployment group create \
  --resource-group rg-tocdoc-client \
  --template-file infra/main.bicep \
  --parameters infra/parameters/prod.bicepparam \
  --parameters tenantId=<your-tenant-id> audienceClientId=<your-client-id>
```

## Resources provisioned
| Resource | Purpose | SKU (prod) |
|----------|---------|-----------|
| Azure OpenAI | LLM + embeddings | S0 |
| Azure Cognitive Search | Vector + keyword index | S1 |
| Azure Document Intelligence | PDF extraction | S0 |
| Azure Key Vault | Secret storage | Standard |
| Log Analytics | Centralized logging | PerGB2018 |
| Application Insights | Telemetry | Linked to Log Analytics |
| Container Apps Environment | Container hosting | Consumption |

## After deployment
1. Copy output values (endpoints, Key Vault name) into your `.env` files
2. Store secrets in Key Vault
3. Grant the QnA service principal `Key Vault Secrets User` role
4. Deploy container images using the CD pipeline or `az containerapp update`
