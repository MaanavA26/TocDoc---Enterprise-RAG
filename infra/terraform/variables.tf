# Input variables. These mirror the parameters in infra/main.bicep one-for-one
# (Bicep param name -> Terraform variable name in snake_case), plus a small
# number of inputs that the Bicep derived from deployment context
# (resourceGroup().location / .name) which Terraform must take explicitly.

# ── Deployment target (Bicep got these from the resource group context) ───────

variable "resource_group_name" {
  description = "Name of the (pre-existing) resource group to deploy into. The Bicep used targetScope='resourceGroup'; Terraform takes it explicitly."
  type        = string
}

variable "location" {
  description = "Azure region for all resources. Mirrors the Bicep 'location' param (which defaulted to resourceGroup().location)."
  type        = string
  default     = "eastus"
}

# ── Non-secret parameters (mirror of the Bicep non-secret params) ─────────────

variable "prefix" {
  description = "Short name prefix for all resource names. Keep under 8 chars."
  type        = string
  default     = "tocdoc"

  validation {
    condition     = length(var.prefix) > 0 && length(var.prefix) <= 8
    error_message = "prefix must be 1-8 characters."
  }
}

variable "environment" {
  description = "Environment tag: dev, staging, or prod."
  type        = string
  default     = "prod"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "tenant_id" {
  description = "Azure AD tenant ID (used for JWT validation and SP auth)."
  type        = string
}

variable "audience_client_id" {
  description = "App registration client ID (audience for JWT / AUDIENCE_ID)."
  type        = string
}

variable "openai_api_version" {
  description = "Azure OpenAI API version."
  type        = string
  default     = "2024-02-01"
}

variable "openai_llm_model" {
  description = "Azure OpenAI LLM deployment/model name."
  type        = string
  default     = "gpt-4o-mini"
}

variable "openai_embedding_model" {
  description = "Azure OpenAI embedding deployment/model name."
  type        = string
  default     = "text-embedding-3-small"
}

variable "search_index_name" {
  description = "Azure Cognitive Search index name."
  type        = string
  default     = "tocdoc-index"
}

variable "openai_sku" {
  description = "SKU for Azure OpenAI."
  type        = string
  default     = "S0"
}

variable "search_sku" {
  description = "Search service SKU."
  type        = string
  default     = "standard" # azurerm uses 'standard' for what the portal/Bicep call 'S1'

  validation {
    condition     = contains(["basic", "standard", "standard2", "standard3"], var.search_sku)
    error_message = "search_sku must be one of: basic, standard, standard2, standard3 (azurerm naming for basic/S1/S2/S3)."
  }
}

# ── Network / auth hardening parameters (mirror of the Bicep hardening params) ─

variable "ingestion_ingress_external" {
  description = <<-EOT
    Expose the ingestion Container App on the public internet. Defaults to false
    (internal-only): the /upload endpoint drives metered Document Intelligence +
    OpenAI + Search writes and is admin-token-authed but still sensitive, so it
    should not be reachable from the public internet by default. Set true only
    when ingress-level auth/rate-limiting (APIM/Front Door) fronts it.
  EOT
  type        = bool
  default     = false
}

variable "disable_local_auth" {
  description = <<-EOT
    Disable shared-key (local) auth on Azure OpenAI, Cognitive Search, and
    Document Intelligence, forcing Entra ID / managed-identity auth. Defaults to
    false because the services CURRENTLY authenticate with API keys. Flip to true
    only AFTER migrating the app code off keys to ManagedIdentityCredential.
  EOT
  type        = bool
  default     = false
}

variable "enable_app_insights_tracing" {
  description = <<-EOT
    Inject APPLICATIONINSIGHTS_CONNECTION_STRING (from the provisioned App
    Insights resource) into both Container Apps, enabling the Azure Monitor
    trace exporter in the service code. Defaults to false, mirroring the Bicep
    'enableAppInsightsTracing' param default. App Insights is always provisioned;
    this only controls whether the apps are wired to emit traces to it.
  EOT
  type        = bool
  default     = false
}

variable "allowed_ip_ranges" {
  description = <<-EOT
    Optional list of public IP ranges (CIDR) permitted to reach the data-plane
    services. Empty (default) preserves current behavior (public network access
    enabled, no IP filter). When non-empty, an IP allow-list is applied to
    Cognitive Search only.
  EOT
  type        = list(string)
  default     = []
}

# ── Secret parameters (mirror of the Bicep @secure() params) ──────────────────
# Unlike the Bicep (which injected these directly as Container App secrets),
# these values are written into Key Vault and the Container Apps reference them
# from there via the user-assigned identity. Values are never inlined into the
# container configuration. See README.md.

variable "openai_api_key" {
  description = "Azure OpenAI API key. Stored in Key Vault; referenced by both apps."
  type        = string
  sensitive   = true
}

variable "search_api_key" {
  description = "Azure Cognitive Search admin key. Stored in Key Vault; referenced by both apps."
  type        = string
  sensitive   = true
}

variable "doc_intel_api_key" {
  description = "Azure Document Intelligence key. Stored in Key Vault; referenced by the ingestion app only."
  type        = string
  sensitive   = true
}

variable "sp_client_id" {
  description = "Service principal client ID for QnA Key Vault access (current auth path)."
  type        = string
  sensitive   = true
}

variable "sp_client_secret" {
  description = "Service principal client secret for QnA Key Vault access (current auth path)."
  type        = string
  sensitive   = true
}

variable "admin_api_token" {
  description = <<-EOT
    Static shared-secret token guarding /admin/* routes on the ingestion service.
    Interim auth. Generate with:
      python -c "import secrets; print(secrets.token_urlsafe(48))"
  EOT
  type        = string
  sensitive   = true
}

variable "container_image" {
  description = "Container image for both apps. Defaults to the public hello-world placeholder (matching the Bicep); replace after pushing your own images."
  type        = string
  default     = "mcr.microsoft.com/azuredocs/containerapps-helloworld:latest"
}
