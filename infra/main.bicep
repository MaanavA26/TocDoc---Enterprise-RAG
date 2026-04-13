// TocDoc Enterprise RAG — Infrastructure Prerequisites
// Provisions: Azure OpenAI, Cognitive Search, Document Intelligence,
// Key Vault, Log Analytics, App Insights, Container Apps Environment,
// and placeholder Container Apps (ingestion + QnA).
//
// NOTE: Container images must be built and pushed separately.
// The container apps are initialized with a placeholder image.
// Update them after pushing your images: az containerapp update --image ...
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
    semanticSearch: 'free'  // Enable semantic ranker
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
resource ingestionApp 'Microsoft.App/containerApps@2023-11-02-preview' = {
  name: ingestionAppName
  location: location
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
          image: 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'
          resources: { cpu: json('0.5'), memory: '1Gi' }
          env: [
            { name: 'AUDIENCE_ID', value: audienceClientId }
            { name: 'AZURE_TENANT_ID', value: tenantId }
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
            { name: 'AUDIENCE_ID', value: audienceClientId }
            { name: 'AZURE_TENANT_ID', value: tenantId }
          ]
        }
      ]
      scale: { minReplicas: 0, maxReplicas: 3 }
    }
  }
  tags: { environment: environment, product: 'tocdoc' }
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
