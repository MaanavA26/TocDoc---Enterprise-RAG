// TocDoc Enterprise RAG — Infrastructure Prerequisites
// Provisions: Azure OpenAI, Cognitive Search, Document Intelligence,
// Key Vault, Log Analytics, App Insights, Container Apps Environment,
// and placeholder Container Apps (ingestion + QnA) with system-assigned
// managed identities and Key Vault Secrets User role assignments.
//
// After deployment the container apps have all required env vars wired.
// Swap the placeholder image with the real one via:
//   az containerapp update --name <app> --resource-group <rg> --image <image>
//
// Usage:
//   az deployment group create \
//     --resource-group rg-tocdoc-client \
//     --template-file infra/main.bicep \
//     --parameters infra/parameters/prod.bicepparam

targetScope = 'resourceGroup'

@description('Short name prefix used for all resource names. Keep under 8 chars.')
param prefix string = 'tocdoc'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Environment tag: dev, staging, or prod.')
@allowed(['dev', 'staging', 'prod'])
param environment string = 'prod'

@description('Azure AD tenant ID for JWT validation.')
param tenantId string

@description('App registration client ID (audience for JWT).')
param audienceClientId string

@description('SKU for Azure OpenAI. Use S0 for standard.')
param openAiSku string = 'S0'

@description('Search service SKU. Use basic for small deployments, S1 for production.')
@allowed(['basic', 'S1', 'S2', 'S3'])
param searchSku string = 'S1'

@description('Azure OpenAI API version used by both services.')
param openAiApiVersion string = '2024-02-01'

@description('Azure OpenAI chat deployment/model name.')
param openAiLlmModel string = 'gpt-4o-mini'

@description('Azure OpenAI embedding deployment/model name.')
param openAiEmbeddingModel string = 'text-embedding-3-small'

@description('Azure Cognitive Search index name.')
param searchIndexName string = 'tocdoc-index'

// ── Resource names (deterministic from prefix) ───────────────────────────────
var openAiName = '${prefix}-openai-${environment}'
var searchName = '${prefix}-search-${environment}'
var docIntelName = '${prefix}-docintel-${environment}'
var kvName = '${prefix}-kv-${environment}'
var logAnalyticsName = '${prefix}-logs-${environment}'
var appInsightsName = '${prefix}-appinsights-${environment}'
var containerEnvName = '${prefix}-containerenv-${environment}'
var ingestionAppName = '${prefix}-ingestion-${environment}'
var qnaAppName = '${prefix}-qna-${environment}'

// Key Vault Secrets User built-in role ID (read secret values, no management)
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

// ── Azure OpenAI ─────────────────────────────────────────────────────────────
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
// System-assigned identity lets the app authenticate to Key Vault at startup
// to load API keys. All non-secret config is wired directly as env vars below.
resource ingestionApp 'Microsoft.App/containerApps@2023-11-02-preview' = {
  name: ingestionAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 5501
      }
    }
    template: {
      containers: [
        {
          name: 'ingestion'
          // Replace with the real image after pushing: az containerapp update --image ...
          image: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            // Auth
            { name: 'AUDIENCE_ID', value: audienceClientId }
            { name: 'AZURE_TENANT_ID', value: tenantId }
            // Key Vault — the app loads secret values (API keys) from KV at startup
            { name: 'AZURE_KEY_VAULT', value: keyVault.name }
            // Azure OpenAI
            { name: 'AZURE_OPENAI_ENDPOINT', value: openAi.properties.endpoint }
            { name: 'AZURE_OPENAI_VERSION', value: openAiApiVersion }
            { name: 'AZURE_OPENAI_LLM_MODEL', value: openAiLlmModel }
            { name: 'AZURE_OPENAI_EMBEDDING_MODEL', value: openAiEmbeddingModel }
            // Azure Cognitive Search
            { name: 'AZURE_SEARCH_ENDPOINT', value: 'https://${search.name}.search.windows.net' }
            { name: 'INDEX_NAME', value: searchIndexName }
            // Document Intelligence
            { name: 'DOC_INTELLIGENCE_ENDPOINT', value: docIntel.properties.endpoint }
            // Observability
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
            { name: 'LOG_LEVEL', value: 'INFO' }
          ]
        }
      ]
      scale: { minReplicas: 0, maxReplicas: 3 }
    }
  }
  tags: { environment: environment, product: 'tocdoc' }
}

// ── QnA Container App ─────────────────────────────────────────────────────────
resource qnaApp 'Microsoft.App/containerApps@2023-11-02-preview' = {
  name: qnaAppName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: containerEnv.id
    configuration: {
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
            // Auth
            { name: 'AUDIENCE_ID', value: audienceClientId }
            { name: 'AZURE_TENANT_ID', value: tenantId }
            // Key Vault — the app loads secret values (API keys) from KV at startup
            { name: 'AZURE_KEY_VAULT', value: keyVault.name }
            // Azure OpenAI
            { name: 'AZURE_OPENAI_ENDPOINT', value: openAi.properties.endpoint }
            { name: 'AZURE_OPENAI_VERSION', value: openAiApiVersion }
            { name: 'AZURE_OPENAI_LLM_MODEL', value: openAiLlmModel }
            { name: 'AZURE_OPENAI_EMBEDDING_MODEL', value: openAiEmbeddingModel }
            // Azure Cognitive Search
            { name: 'AZURE_SEARCH_ENDPOINT', value: 'https://${search.name}.search.windows.net' }
            { name: 'INDEX_NAME', value: searchIndexName }
            // Observability
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: appInsights.properties.ConnectionString }
            { name: 'LOG_LEVEL', value: 'INFO' }
          ]
        }
      ]
      scale: { minReplicas: 0, maxReplicas: 3 }
    }
  }
  tags: { environment: environment, product: 'tocdoc' }
}

// ── Key Vault access: Key Vault Secrets User role for both container apps ─────
// Allows each app's managed identity to read secret *values* from Key Vault.
// The app uses this at startup to load API keys without storing them in env vars.

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
output openAiEndpoint string = openAi.properties.endpoint
output searchEndpoint string = 'https://${search.name}.search.windows.net'
output docIntelEndpoint string = docIntel.properties.endpoint
output keyVaultName string = keyVault.name
output containerEnvId string = containerEnv.id
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output ingestionAppFqdn string = ingestionApp.properties.configuration.ingress.fqdn
output qnaAppFqdn string = qnaApp.properties.configuration.ingress.fqdn
