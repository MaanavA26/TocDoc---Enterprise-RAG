using '../main.bicep'

param prefix = 'tocdoc'
param environment = 'prod'
param location = 'eastus'
param tenantId = '<your-azure-tenant-id>'
param audienceClientId = '<your-app-registration-client-id>'
param searchSku = 'S1'
param openAiApiVersion = '2024-02-01'
param openAiLlmModel = 'gpt-4o-mini'
param openAiEmbeddingModel = 'text-embedding-3-small'
param searchIndexName = 'tocdoc-index'
// Secret params (openAiApiKey, searchApiKey, docIntelApiKey, spClientId, spClientSecret)
// must be passed at deploy time — do NOT commit secret values to source control.
// See INSTALLATION.md Step 1 for the --parameters flags to use.
