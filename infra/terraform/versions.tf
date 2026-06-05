# Provider and Terraform version pinning.
# azurerm is pinned to a 3.x line so the resource schemas used in main.tf
# (azurerm_container_app, azurerm_cognitive_account, azurerm_search_service,
# azurerm_key_vault*) stay stable across applies.

terraform {
  required_version = ">= 1.5.0, < 2.0.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.116"
    }
  }
}

provider "azurerm" {
  features {
    key_vault {
      # Mirror the Bicep posture: purge protection is enabled on the vault, so
      # never purge soft-deleted vaults on destroy.
      purge_soft_delete_on_destroy = false
    }
  }
}
