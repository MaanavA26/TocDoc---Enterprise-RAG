// TocDoc Enterprise RAG — Infrastructure Prerequisites
// Provisions: Azure OpenAI, Cognitive Search, Document Intelligence,
// Key Vault, Log Analytics, App Insights, Container Apps Environment,
// and Container Apps (ingestion + QnA) with all required env vars wired
// using the exact names the current service code reads at startup.
//
// Authentication model (current):
//   - Ingestion: reads API keys directly from environment variables.
//   - QnA: reads bootstrap SP credentials from env, then uses
//     ClientSecretCredential to load additional secrets from Key Vault.
//   - System-assigned managed identities are provisioned and granted
//     Key Vault Secrets User for future ManagedIdentityCredential migration.
//
// Secrets are passed as @secure() parameters so they are stored as
// Container App secrets and never appear in plain deployment logs.
//
// Usage:
//   az deployment group create \
//     --resource-group rg-tocdoc-<client> \
//     --template-file infra/main.bicep \
//     --parameters infra/parameters/prod.bicepparam \
//     --parameters \
//         openAiApiKey=<key> \
//         searchApiKey=<key> \
//         docIntelApiKey=<key> \
//         spClientId=<id> \
//         spClientSecret=<secret>

targetScope = 'resourceGroup'

// ── Non-secret parameters ─────────────────────────────────────────────────────

@description('Short name prefix for all resource names. Keep under 8 chars.')
param prefix string = 'tocdoc'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Environment tag: dev, staging, or prod.')
@allowed(['dev', 'staging', 'prod'])
param environment string = 'prod'

@description('Azure AD tenant ID (used for JWT validation and SP auth).')
param tenantId string

@description('App registration client ID (audience for JWT / AUDIENCE_ID).')
param audienceClientId string

@description('Azure OpenAI API version.')
param openAiApiVersion string = '2024-02-01'

@description('Azure OpenAI LLM deployment/model name.')
param openAiLlmModel string = 'gpt-4o-mini'

@description('Azure OpenAI embedding deployment/model name.')
param openAiEmbeddingModel string = 'text-embedding-3-small'

@description('Azure Cognitive Search index name.')
param searchIndexName string = 'tocdoc-index'

@description('SKU for Azure OpenAI.')
param openAiSku string = 'S0'

@description('Search service SKU.')
@allowed(['basic', 'S1', 'S2', 'S3'])
param searchSku string = 'S1'

// ── Secret parameters (@secure prevents values appearing in deployment logs) ──

@secure()
@description('Azure OpenAI API key. Injected as a container app secret.')
param openAiApiKey string

@secure()
@description('Azure Cognitive Search admin key. Injected as a container app secret.')
param searchApiKey string

@secure()
@description('Azure Document Intelligence key. Injected into the ingestion app only.')
param docIntelApiKey string

@secure()
@description('Service principal client ID for QnA Key Vault access (current auth path).')
param spClientId string

@secure()
@description('Service principal client secret for QnA Key Vault access (current auth path).')
param spClientSecret string

@secure()
@description('Static shared-secret token guarding /admin/* routes on the ingestion service. Interim auth — to be replaced with Azure AD JWT in a follow-up. Generate with: python -c "import secrets; print(secrets.token_urlsafe(48))"')
param adminApiToken string

// ── Resource names ────────────────────────────────────────────────────────────
var openAiName         = '${prefix}-openai-${environment}'
var searchName         = '${prefix}-search-${environment}'
var docIntelName       = '${prefix}-docintel-${environment}'
var kvName             = '${prefix}-kv-${environment}'
var logAnalyticsName   = '${prefix}-logs-${environment}'
var appInsightsName    = '${prefix}-appinsights-${environment}'
var containerEnvName   = '${prefix}-containerenv-${environment}'
var ingestionAppName   = '${prefix}-ingestion-${environment}'
var qnaAppName         = '${prefix}-qna-${environment}'

// Key Vault Secrets User (read secret values; no management operations)
var kvSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e0'

// ── Log Analytics workspace ───────────────────────────────────────────────────
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
  tags: { environment: environment, product: 'tocdoc' }
}

// ── Application Insights ──────────────────────────────────────────────────────
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
  tags: { environment: environment, product: 'tocdoc' }
}

// ── Azure OpenAI ──────────────────────────────────────────────────────────────
resource openAi 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: openAiName
  location: location
  kind: 'OpenAI'
  sku: { name: openAiSku }
  properties: {
    publicNetworkAccess: 'Enabled'
    customSubDomainName: openAiName
  }
  tags: { environment: environment, product: 'tocdoc' }
}

// ── Azure Cognitive Search ────────────────────────────────────────────────────
resource search 'Microsoft.Search/searchServices@2023-11-01' = {
  name: searchName
  location: location
  sku: { name: searchSku }
  properties: {
    replicaCount: 1
    partitionCount: 1
    publicNetworkAccess: 'enabled'
    semanticSearch: 'free'
  }
  tags: { environment: environment, product: 'tocdoc' }
}

// ── Document Intelligence ─────────────────────────────────────────────────────
resource docIntel 'Microsoft.CognitiveServices/accounts@2023-05-01' = {
  name: docIntelName
  location: location
  kind: 'FormRecognizer'
  sku: { name: 'S0' }
  properties: {
    publicNetworkAccess: 'Enabled'
    customSubDomainName: docIntelName
  }
  tags: { environment: environment, product: 'tocdoc' }
}

// ── Key Vault ─────────────────────────────────────────────────────────────────
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: kvName
  location: location
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: tenantId
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 90
  }
  tags: { environment: environment, product: 'tocdoc' }
}

// ── Container Apps Environment ────────────────────────────────────────────────
resource containerEnv 'Microsoft.App/managedEnvironments@2023-11-02-preview' = {
  name: containerEnvName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
  tags: { environment: environment, product: 'tocdoc' }
}

// ── Ingestion Container App ───────────────────────────────────────────────────
// Env var names match exactly what ingestion/custom_rag.py reads via os.getenv().
// API keys are stored as Container App secrets (not plain env vars).
resource ingestionApp 'Microsoft.App/containerApps@2023-11-02-preview' = {
  name: ingestionAppName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      secrets: [
        // Injected as env vars via secretRef below; never appear in plain config.
        { name: 'azure-openai-key',  value: openAiApiKey   }
        { name: 'azure-search-key',  value: searchApiKey   }
        { name: 'doc-intel-key',     value: docIntelApiKey }
        { name: 'admin-api-token',   value: adminApiToken  }
      ]
      ingress: {
        external: true
        targetPort: 5501
      }
    }
    template: {
      containers: [
        {
          name: 'ingestion'
          // Replace after pushing your image: az containerapp update --image ...
          image: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            // ── Plain env vars (computed from deployed resources) ──
            { name: 'AZURE_OPENAI_ENDPOINT',         value: openAi.properties.endpoint }
            { name: 'AZURE_OPENAI_VERSION',           value: openAiApiVersion }
            { name: 'AZURE_OPENAI_EMBEDDING_MODEL',   value: openAiEmbeddingModel }
            { name: 'AZURE_SEARCH_ENDPOINT',          value: 'https://${search.name}.search.windows.net' }
            { name: 'INDEX_NAME',                     value: searchIndexName }
            { name: 'DOC_INTELLIGENCE_ENDPOINT',      value: docIntel.properties.endpoint }
            { name: 'LOG_LEVEL',                      value: 'INFO' }
            // ── Secret env vars (values stored as Container App secrets above) ──
            { name: 'AZURE_OPENAI_KEY',   secretRef: 'azure-openai-key' }
            { name: 'AZURE_SEARCH_KEY',   secretRef: 'azure-search-key' }
            { name: 'DOC_INTELLIGENCE_KEY', secretRef: 'doc-intel-key'  }
            { name: 'ADMIN_API_TOKEN',    secretRef: 'admin-api-token' }
          ]
        }
      ]
      scale: { minReplicas: 0, maxReplicas: 3 }
    }
  }
  tags: { environment: environment, product: 'tocdoc' }
}

// ── QnA Container App ─────────────────────────────────────────────────────────
// Env var names are canonical UPPER_SNAKE — matches services/qna/src/config/
// config.py and the ingestion service. Pre-P0-7 deployments using PascalCase
// (AzureOpenaiAccountEndpoint, TocdocOpenAIKey, etc.) still work because the
// config module's _get_env helper accepts the legacy names with a one-shot
// deprecation warning. New deployments should use the canonical names only.
//
// AZURE_CLIENT_ID / AZURE_CLIENT_SECRET / AZURE_TENANT_ID match the Azure SDK
// DefaultAzureCredential conventions, so a future switch from
// ClientSecretCredential to DefaultAzureCredential requires no rename.
resource qnaApp 'Microsoft.App/containerApps@2023-11-02-preview' = {
  name: qnaAppName
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      secrets: [
        { name: 'azure-openai-key',    value: openAiApiKey    }
        { name: 'azure-search-key',    value: searchApiKey    }
        { name: 'azure-client-id',     value: spClientId      }
        { name: 'azure-client-secret', value: spClientSecret  }
      ]
      ingress: {
        external: true
        targetPort: 5500
      }
    }
    template: {
      containers: [
        {
          name: 'qna'
          image: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            // ── Plain env vars (canonical UPPER_SNAKE) ──
            { name: 'AZURE_OPENAI_ENDPOINT',         value: openAi.properties.endpoint }
            { name: 'AZURE_OPENAI_VERSION',          value: openAiApiVersion }
            { name: 'AZURE_OPENAI_LLM_MODEL',        value: openAiLlmModel }
            { name: 'AZURE_OPENAI_EMBEDDING_MODEL',  value: openAiEmbeddingModel }
            { name: 'AZURE_SEARCH_ENDPOINT',         value: 'https://${search.name}.search.windows.net' }
            { name: 'INDEX_NAME',                    value: searchIndexName }
            { name: 'AUDIENCE_ID',                   value: audienceClientId }
            { name: 'AZURE_KEY_VAULT',               value: keyVault.name }
            { name: 'AZURE_TENANT_ID',               value: tenantId }
            { name: 'LOG_LEVEL',                     value: 'INFO' }
            // ── Secret env vars (canonical UPPER_SNAKE) ──
            { name: 'AZURE_OPENAI_KEY',     secretRef: 'azure-openai-key'    }
            { name: 'AZURE_SEARCH_KEY',     secretRef: 'azure-search-key'    }
            { name: 'AZURE_CLIENT_ID',      secretRef: 'azure-client-id'     }
            { name: 'AZURE_CLIENT_SECRET',  secretRef: 'azure-client-secret' }
          ]
        }
      ]
      scale: { minReplicas: 0, maxReplicas: 3 }
    }
  }
  tags: { environment: environment, product: 'tocdoc' }
}

// ── Key Vault role assignments (future ManagedIdentityCredential migration) ───
// Grants each app's system-assigned identity the ability to read secret values.
// The current QnA code path still uses ClientSecretCredential (wired above as
// AZURE_CLIENT_ID / AZURE_CLIENT_SECRET / AZURE_TENANT_ID). These assignments
// are infrastructure preparation for a future code migration — they do not
// affect current behavior.

resource kvRoleIngestion 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, ingestionApp.id, kvSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
    principalId: ingestionApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

resource kvRoleQna 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, qnaApp.id, kvSecretsUserRoleId)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsUserRoleId)
    principalId: qnaApp.identity.principalId
    principalType: 'ServicePrincipal'
  }
}

// ── Outputs ───────────────────────────────────────────────────────────────────
output openAiEndpoint              string = openAi.properties.endpoint
output searchEndpoint              string = 'https://${search.name}.search.windows.net'
output docIntelEndpoint            string = docIntel.properties.endpoint
output keyVaultName                string = keyVault.name
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output ingestionAppFqdn            string = ingestionApp.properties.configuration.ingress.fqdn
output qnaAppFqdn                  string = qnaApp.properties.configuration.ingress.fqdn
