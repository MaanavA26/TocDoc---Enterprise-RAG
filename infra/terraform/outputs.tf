# Outputs — mirror of the seven outputs declared in infra/main.bicep.

output "openai_endpoint" {
  description = "Azure OpenAI account endpoint."
  value       = azurerm_cognitive_account.openai.endpoint
}

output "search_endpoint" {
  description = "Azure Cognitive Search service endpoint."
  value       = local.search_endpoint
}

output "doc_intel_endpoint" {
  description = "Document Intelligence (Form Recognizer) endpoint."
  value       = azurerm_cognitive_account.doc_intel.endpoint
}

output "key_vault_name" {
  description = "Name of the provisioned Key Vault."
  value       = azurerm_key_vault.main.name
}

output "app_insights_connection_string" {
  description = "Application Insights connection string."
  value       = azurerm_application_insights.main.connection_string
  sensitive   = true
}

output "ingestion_app_fqdn" {
  description = "Ingestion Container App ingress FQDN."
  value       = azurerm_container_app.ingestion.ingress[0].fqdn
}

output "qna_app_fqdn" {
  description = "QnA Container App ingress FQDN."
  value       = azurerm_container_app.qna.ingress[0].fqdn
}
