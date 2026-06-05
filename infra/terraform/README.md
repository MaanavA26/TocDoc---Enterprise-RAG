# TocDoc Infrastructure — Terraform module

A Terraform (`azurerm`) alternative to [`infra/main.bicep`](../main.bicep), for
teams that self-host via Terraform. It provisions the same resources, into an
existing resource group:

- **Azure OpenAI**, **Azure AI Search**, **Document Intelligence**
  (`azurerm_cognitive_account` x2 + `azurerm_search_service`)
- **Key Vault** with `purge_protection_enabled = true` and RBAC authorization
- **Log Analytics** workspace + **Application Insights**
- **Container Apps Environment** wired to Log Analytics
- **Container Apps** for QnA (port 5500, external ingress) and ingestion
  (port 5501, internal ingress by default) with the same env/secret wiring,
  plus HTTP liveness/readiness probes on `/health`
- A **user-assigned managed identity** granted **Key Vault Secrets User** on the
  vault, used by both apps to resolve their secrets from Key Vault

Scope is limited to this directory; the Bicep template is unchanged. Pick one
IaC path — Bicep **or** Terraform — not both against the same resource group.

## Files

| File                       | Purpose                                                    |
| -------------------------- | ---------------------------------------------------------- |
| `versions.tf`              | Terraform + `azurerm` provider version pins                |
| `variables.tf`             | Inputs (one-to-one with the Bicep parameters)              |
| `main.tf`                  | All resources                                              |
| `outputs.tf`               | The same seven outputs the Bicep exposes                   |
| `terraform.tfvars.example` | Copy to `terraform.tfvars` and fill in (placeholders only) |

## Deviations from the Bicep

These are intentional and required by the brief; behavior is otherwise mirrored.

1. **Secrets are referenced from Key Vault, never inlined.** The Bicep injects
   the secret values straight into each Container App's `secret` blocks. Here,
   the secret values are written to Key Vault (`azurerm_key_vault_secret`) and
   each Container App `secret` references them by `key_vault_secret_id`. The
   container env vars then map to those secret references by name — values never
   appear in the container configuration.

2. **User-assigned managed identity instead of system-assigned.** Resolving a
   Key Vault secret reference at container create-time requires an identity that
   *already* holds **Key Vault Secrets User** on the vault. A system-assigned
   identity (as in the Bicep) cannot satisfy that ordering in one pass: the app
   must exist before its principal exists, but the secret reference needs the
   role granted to that principal first — a dependency cycle. A user-assigned
   identity is created and granted the role up front, then both apps reference
   it (for both the `identity` block and each secret's `identity`), with an
   explicit `depends_on` the role assignment. This is the data-plane role the
   Bicep granted per-app; here it is granted once to the shared identity.

3. **Probes added.** The Bicep declares none. HTTP liveness + readiness probes
   target `GET /health` on each app's port (both services expose it).

4. **Inputs the Bicep took from context.** The Bicep used
   `targetScope='resourceGroup'` and defaulted `location` to
   `resourceGroup().location`. Terraform takes `resource_group_name`
   (pre-existing) and `location` explicitly.

## Parameter / SKU naming

Variable names are the Bicep parameter names in `snake_case`. One value differs:
`search_sku` uses the `azurerm` SKU names (`basic`, `standard`, `standard2`,
`standard3`) rather than the portal/Bicep `basic`/`S1`/`S2`/`S3`. The default
`standard` equals the Bicep default `S1`.

The negative `disable_local_auth` is mapped to the positive `azurerm` arguments
(`local_auth_enabled` / `local_authentication_enabled`) internally.

## Usage

```sh
cp terraform.tfvars.example terraform.tfvars
# edit terraform.tfvars; prefer TF_VAR_* env vars for the secret values

terraform init
terraform plan  -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
```

Prefer supplying secrets via environment variables in CI rather than writing
them to `terraform.tfvars`:

```sh
export TF_VAR_openai_api_key=...
export TF_VAR_search_api_key=...
export TF_VAR_doc_intel_api_key=...
export TF_VAR_sp_client_id=...
export TF_VAR_sp_client_secret=...
export TF_VAR_admin_api_token=...
```

The placeholder container image (`mcr.microsoft.com/azuredocs/containerapps-helloworld`)
matches the Bicep. Replace it with your pushed images via the `container_image`
variable (or `az containerapp update --image ...` after the first apply).

### Deployment prerequisites

- The resource group named by `resource_group_name` must already exist.
- Because Key Vault uses RBAC authorization, the principal running
  `terraform apply` needs **Key Vault Secrets Officer** on the vault (or
  subscription/RG scope) to create the secrets. RBAC role propagation can lag
  the first apply by a minute or two; a re-run resolves transient
  `403`/`Forbidden` errors on the secret writes.

## Validation

This module is validated with:

```sh
terraform fmt -check
terraform init -backend=false
terraform validate
```
