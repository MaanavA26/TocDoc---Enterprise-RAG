# TocDoc Enterprise RAG — Infrastructure (Terraform / azurerm)
#
# Terraform mirror of infra/main.bicep. Provisions: Azure OpenAI, Cognitive
# Search, Document Intelligence, Key Vault, Log Analytics, App Insights,
# Container Apps Environment, and the ingestion + QnA Container Apps with all
# env vars wired using the exact names the service code reads at startup.
#
# Key deviation from the Bicep (documented in README.md): secrets are sourced
# from Key Vault by *reference* (key_vault_secret_id), never inlined into the
# container configuration. Resolving a Key Vault secret reference at container
# create-time requires an identity that already holds the Key Vault Secrets User
# role on the vault. A system-assigned identity (as in the Bicep) cannot satisfy
# that ordering in a single pass (the app must exist before its principal exists,
# but the secret reference needs the role granted to that principal first), so a
# USER-assigned identity is used instead — it can be created and granted the role
# before either app is created.

data "azurerm_resource_group" "rg" {
  name = var.resource_group_name
}

locals {
  # ── Resource names (identical formula to the Bicep var block) ──
  openai_name        = "${var.prefix}-openai-${var.environment}"
  search_name        = "${var.prefix}-search-${var.environment}"
  doc_intel_name     = "${var.prefix}-docintel-${var.environment}"
  kv_name            = "${var.prefix}-kv-${var.environment}"
  log_analytics_name = "${var.prefix}-logs-${var.environment}"
  app_insights_name  = "${var.prefix}-appinsights-${var.environment}"
  container_env_name = "${var.prefix}-containerenv-${var.environment}"
  ingestion_app_name = "${var.prefix}-ingestion-${var.environment}"
  qna_app_name       = "${var.prefix}-qna-${var.environment}"
  uami_name          = "${var.prefix}-id-${var.environment}"

  # azurerm exposes positive booleans; the Bicep param is phrased negatively.
  local_auth_enabled = !var.disable_local_auth

  search_endpoint = "https://${local.search_name}.search.windows.net"

  common_tags = {
    environment = var.environment
    product     = "tocdoc"
  }

  # Key Vault secret names (kebab-case, mirroring the Container App secret names
  # used in the Bicep). Values come from the sensitive *_key / *_token variables.
  kv_secrets = {
    "azure-openai-key"    = var.openai_api_key
    "azure-search-key"    = var.search_api_key
    "doc-intel-key"       = var.doc_intel_api_key
    "azure-client-id"     = var.sp_client_id
    "azure-client-secret" = var.sp_client_secret
    "admin-api-token"     = var.admin_api_token
  }
}

# ── Log Analytics workspace ───────────────────────────────────────────────────
resource "azurerm_log_analytics_workspace" "main" {
  name                = local.log_analytics_name
  location            = var.location
  resource_group_name = data.azurerm_resource_group.rg.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = local.common_tags
}

# ── Application Insights ──────────────────────────────────────────────────────
resource "azurerm_application_insights" "main" {
  name                = local.app_insights_name
  location            = var.location
  resource_group_name = data.azurerm_resource_group.rg.name
  application_type    = "web"
  workspace_id        = azurerm_log_analytics_workspace.main.id
  tags                = local.common_tags
}

# ── Azure OpenAI ──────────────────────────────────────────────────────────────
resource "azurerm_cognitive_account" "openai" {
  name                  = local.openai_name
  location              = var.location
  resource_group_name   = data.azurerm_resource_group.rg.name
  kind                  = "OpenAI"
  sku_name              = var.openai_sku
  custom_subdomain_name = local.openai_name

  # disableLocalAuth stays false (local_auth_enabled true) until the app migrates
  # off API keys to ManagedIdentityCredential. Mirror of the Bicep param.
  local_auth_enabled = local.local_auth_enabled

  public_network_access_enabled = true

  # NOTE: allowed_ip_ranges is applied to Cognitive Search ONLY (see below),
  # matching the Bicep. The Cognitive Services accounts deliberately keep no IP
  # firewall — the apps reach them by key/identity auth and Container Apps have
  # no stable egress IP to allow-list.

  tags = local.common_tags
}

# ── Azure Cognitive Search ────────────────────────────────────────────────────
resource "azurerm_search_service" "main" {
  name                = local.search_name
  location            = var.location
  resource_group_name = data.azurerm_resource_group.rg.name
  sku                 = var.search_sku
  replica_count       = 1
  partition_count     = 1

  public_network_access_enabled = true
  semantic_search_sku           = "free"

  # When disable_local_auth is true, force Entra ID (RBAC) auth and drop the
  # admin/query API keys. Defaults to keys-allowed to match current code.
  local_authentication_enabled = local.local_auth_enabled
  authentication_failure_mode  = local.local_auth_enabled ? "http401WithBearerChallenge" : null

  # Default-deny IP firewall, applied only when allowed_ip_ranges is supplied.
  # Empty (default) leaves the service open per current behavior.
  allowed_ips = var.allowed_ip_ranges

  tags = local.common_tags
}

# ── Document Intelligence ─────────────────────────────────────────────────────
resource "azurerm_cognitive_account" "doc_intel" {
  name                  = local.doc_intel_name
  location              = var.location
  resource_group_name   = data.azurerm_resource_group.rg.name
  kind                  = "FormRecognizer"
  sku_name              = "S0"
  custom_subdomain_name = local.doc_intel_name

  local_auth_enabled            = local.local_auth_enabled
  public_network_access_enabled = true

  # See the OpenAI account above: no IP firewall on the Cognitive Services
  # accounts — allowed_ip_ranges scopes to Cognitive Search only (Bicep parity).

  tags = local.common_tags
}

# ── Key Vault ─────────────────────────────────────────────────────────────────
resource "azurerm_key_vault" "main" {
  name                = local.kv_name
  location            = var.location
  resource_group_name = data.azurerm_resource_group.rg.name
  tenant_id           = var.tenant_id
  sku_name            = "standard"

  enable_rbac_authorization  = true
  soft_delete_retention_days = 90

  # Prevent permanent deletion of soft-deleted secrets/vault within the
  # retention window. Irreversible once enabled — the intended prod posture.
  purge_protection_enabled = true

  tags = local.common_tags
}

# ── User-assigned managed identity ────────────────────────────────────────────
# Created up front so the Key Vault Secrets User role can be granted to it
# BEFORE the Container Apps are created and reference Key Vault secrets.
resource "azurerm_user_assigned_identity" "apps" {
  name                = local.uami_name
  location            = var.location
  resource_group_name = data.azurerm_resource_group.rg.name
  tags                = local.common_tags
}

# Key Vault Secrets User (read secret values; no management operations). This is
# the data-plane role the Bicep granted to each app identity; here it is granted
# once to the shared user-assigned identity. It must exist before the apps
# reference secrets, so the apps depend_on it below.
resource "azurerm_role_assignment" "kv_secrets_user" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.apps.principal_id
}

# The deploying principal also needs to write the secrets below. With RBAC
# enabled, that requires "Key Vault Secrets Officer" on the vault (see README).
resource "azurerm_key_vault_secret" "secrets" {
  for_each     = local.kv_secrets
  name         = each.key
  value        = each.value
  key_vault_id = azurerm_key_vault.main.id
}

# ── Container Apps Environment ────────────────────────────────────────────────
# azurerm wires Log Analytics by workspace ID directly (no manual customerId /
# shared-key plumbing as in the Bicep).
resource "azurerm_container_app_environment" "main" {
  name                       = local.container_env_name
  location                   = var.location
  resource_group_name        = data.azurerm_resource_group.rg.name
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
  tags                       = local.common_tags
}

# ── Ingestion Container App ───────────────────────────────────────────────────
# Env var names match exactly what ingestion/custom_rag.py reads via os.getenv().
# Secrets are referenced from Key Vault (key_vault_secret_id), never inlined.
resource "azurerm_container_app" "ingestion" {
  name                         = local.ingestion_app_name
  resource_group_name          = data.azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  revision_mode                = "Single"
  tags                         = local.common_tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.apps.id]
  }

  # Secrets resolved from Key Vault at runtime via the user-assigned identity.
  secret {
    name                = "azure-openai-key"
    key_vault_secret_id = azurerm_key_vault_secret.secrets["azure-openai-key"].versionless_id
    identity            = azurerm_user_assigned_identity.apps.id
  }
  secret {
    name                = "azure-search-key"
    key_vault_secret_id = azurerm_key_vault_secret.secrets["azure-search-key"].versionless_id
    identity            = azurerm_user_assigned_identity.apps.id
  }
  secret {
    name                = "doc-intel-key"
    key_vault_secret_id = azurerm_key_vault_secret.secrets["doc-intel-key"].versionless_id
    identity            = azurerm_user_assigned_identity.apps.id
  }
  secret {
    name                = "admin-api-token"
    key_vault_secret_id = azurerm_key_vault_secret.secrets["admin-api-token"].versionless_id
    identity            = azurerm_user_assigned_identity.apps.id
  }

  # Ingestion ingress is INTERNAL by default. /upload is admin-token-authed but
  # still sensitive (drives metered DI/OpenAI/Search writes), so it must not be
  # publicly reachable unless an authenticating gateway fronts it. Set
  # ingestion_ingress_external = true to opt in to public exposure.
  ingress {
    external_enabled = var.ingestion_ingress_external
    target_port      = 5501
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 0
    max_replicas = 3

    container {
      name   = "ingestion"
      image  = var.container_image
      cpu    = 0.5
      memory = "1Gi"

      # ── Plain env vars (computed from deployed resources) ──
      env {
        name  = "AZURE_OPENAI_ENDPOINT"
        value = azurerm_cognitive_account.openai.endpoint
      }
      env {
        name  = "AZURE_OPENAI_VERSION"
        value = var.openai_api_version
      }
      env {
        name  = "AZURE_OPENAI_EMBEDDING_MODEL"
        value = var.openai_embedding_model
      }
      env {
        name  = "AZURE_SEARCH_ENDPOINT"
        value = local.search_endpoint
      }
      env {
        name  = "INDEX_NAME"
        value = var.search_index_name
      }
      env {
        name  = "DOC_INTELLIGENCE_ENDPOINT"
        value = azurerm_cognitive_account.doc_intel.endpoint
      }
      env {
        name  = "LOG_LEVEL"
        value = "INFO"
      }

      # ── Secret env vars (values resolved from Key Vault via secret refs) ──
      env {
        name        = "AZURE_OPENAI_KEY"
        secret_name = "azure-openai-key"
      }
      env {
        name        = "AZURE_SEARCH_KEY"
        secret_name = "azure-search-key"
      }
      env {
        name        = "DOC_INTELLIGENCE_KEY"
        secret_name = "doc-intel-key"
      }
      env {
        name        = "ADMIN_API_TOKEN"
        secret_name = "admin-api-token"
      }

      # Probes (not present in the Bicep; added per spec). The ingestion service
      # exposes GET /health (services/ingestion/app.py).
      liveness_probe {
        transport = "HTTP"
        path      = "/health"
        port      = 5501
      }
      readiness_probe {
        transport = "HTTP"
        path      = "/health"
        port      = 5501
      }
    }
  }

  # The secret references resolve only once the identity holds Key Vault Secrets
  # User, so make the dependency explicit.
  depends_on = [azurerm_role_assignment.kv_secrets_user]
}

# ── QnA Container App ─────────────────────────────────────────────────────────
# Env var names are canonical UPPER_SNAKE — matches services/qna/src/config/.
# AZURE_CLIENT_ID / AZURE_CLIENT_SECRET / AZURE_TENANT_ID follow the Azure SDK
# DefaultAzureCredential conventions.
resource "azurerm_container_app" "qna" {
  name                         = local.qna_app_name
  resource_group_name          = data.azurerm_resource_group.rg.name
  container_app_environment_id = azurerm_container_app_environment.main.id
  revision_mode                = "Single"
  tags                         = local.common_tags

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.apps.id]
  }

  secret {
    name                = "azure-openai-key"
    key_vault_secret_id = azurerm_key_vault_secret.secrets["azure-openai-key"].versionless_id
    identity            = azurerm_user_assigned_identity.apps.id
  }
  secret {
    name                = "azure-search-key"
    key_vault_secret_id = azurerm_key_vault_secret.secrets["azure-search-key"].versionless_id
    identity            = azurerm_user_assigned_identity.apps.id
  }
  secret {
    name                = "azure-client-id"
    key_vault_secret_id = azurerm_key_vault_secret.secrets["azure-client-id"].versionless_id
    identity            = azurerm_user_assigned_identity.apps.id
  }
  secret {
    name                = "azure-client-secret"
    key_vault_secret_id = azurerm_key_vault_secret.secrets["azure-client-secret"].versionless_id
    identity            = azurerm_user_assigned_identity.apps.id
  }

  # QnA stays external: every request is Azure AD JWT-authed at the app layer, so
  # public ingress is acceptable (unlike ingestion above).
  ingress {
    external_enabled = true
    target_port      = 5500
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 0
    max_replicas = 3

    container {
      name   = "qna"
      image  = var.container_image
      cpu    = 0.5
      memory = "1Gi"

      # ── Plain env vars (canonical UPPER_SNAKE) ──
      env {
        name  = "AZURE_OPENAI_ENDPOINT"
        value = azurerm_cognitive_account.openai.endpoint
      }
      env {
        name  = "AZURE_OPENAI_VERSION"
        value = var.openai_api_version
      }
      env {
        name  = "AZURE_OPENAI_LLM_MODEL"
        value = var.openai_llm_model
      }
      env {
        name  = "AZURE_OPENAI_EMBEDDING_MODEL"
        value = var.openai_embedding_model
      }
      env {
        name  = "AZURE_SEARCH_ENDPOINT"
        value = local.search_endpoint
      }
      env {
        name  = "INDEX_NAME"
        value = var.search_index_name
      }
      env {
        name  = "AUDIENCE_ID"
        value = var.audience_client_id
      }
      env {
        name  = "AZURE_KEY_VAULT"
        value = azurerm_key_vault.main.name
      }
      env {
        name  = "AZURE_TENANT_ID"
        value = var.tenant_id
      }
      env {
        name  = "LOG_LEVEL"
        value = "INFO"
      }

      # ── Secret env vars (values resolved from Key Vault via secret refs) ──
      env {
        name        = "AZURE_OPENAI_KEY"
        secret_name = "azure-openai-key"
      }
      env {
        name        = "AZURE_SEARCH_KEY"
        secret_name = "azure-search-key"
      }
      env {
        name        = "AZURE_CLIENT_ID"
        secret_name = "azure-client-id"
      }
      env {
        name        = "AZURE_CLIENT_SECRET"
        secret_name = "azure-client-secret"
      }

      # Probes (not present in the Bicep; added per spec). The QnA service
      # exposes GET /health (services/qna/src/core/auth.py treats it as public).
      liveness_probe {
        transport = "HTTP"
        path      = "/health"
        port      = 5500
      }
      readiness_probe {
        transport = "HTTP"
        path      = "/health"
        port      = 5500
      }
    }
  }

  depends_on = [azurerm_role_assignment.kv_secrets_user]
}
