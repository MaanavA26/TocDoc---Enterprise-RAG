using '../main.bicep'

param prefix = 'tocdocdev'
param environment = 'dev'
param location = 'eastus'
param tenantId = '<your-azure-tenant-id>'
param audienceClientId = '<your-app-registration-client-id>'
param searchSku = 'basic'
