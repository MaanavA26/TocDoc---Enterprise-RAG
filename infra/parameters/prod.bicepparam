using '../main.bicep'

param prefix = 'tocdoc'
param environment = 'prod'
param location = 'eastus'
param tenantId = '<your-azure-tenant-id>'
param audienceClientId = '<your-app-registration-client-id>'
param searchSku = 'S1'
