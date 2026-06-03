#!/usr/bin/env bash
#
# validate_deployment.sh — TocDoc post-deploy validation (Phase 2 Workstream C)
#
# Runs a series of read-only Azure CLI checks against a TocDoc deployment to
# catch configuration drift, missing env vars, broken revisions, and stale
# Bicep outputs BEFORE a demo or production traffic exposes the failure.
#
# Read-only: the script never calls `az ... create`, `update`, or `set`.
# Secret-safe: validates env var NAMES only, never prints values.
#
# Compatibility: bash 3.2+ (macOS default). No associative arrays, no mapfile,
# no jq dependency. Intended to be shellcheck-clean (enforced by the CI lint gate once it lands on main).
#
# Exit codes:
#   0 — all required checks passed (warnings allowed)
#   1 — one or more required checks failed
#   2 — script usage / preflight error (az missing, not logged in, etc.)
#
# Usage:
#   scripts/validate_deployment.sh \
#     --resource-group rg-tocdoc-client \
#     --ingestion-app tocdoc-ingestion-prod \
#     --qna-app tocdoc-qna-prod \
#     [--environment prod] \
#     [--deployment-name main] \
#     [--expected-index-name tocdoc-index] \
#     [--skip-health-checks] \
#     [--output text|json]
#
# See docs/architect_phase_2/03_DEPLOYMENT_VALIDATION_SPEC.md for the
# acceptance criteria this script implements.

set -uo pipefail
IFS=$'\n\t'

# ---------------------------------------------------------------------------
# Defaults and globals
# ---------------------------------------------------------------------------

RG=""
INGESTION_APP=""
QNA_APP=""
ENVIRONMENT=""   # empty = infer the env suffix from the app-name's last segment
DEPLOYMENT_NAME="main"
EXPECTED_INDEX_NAME="tocdoc-index"
SKIP_HEALTH=""
OUTPUT="text"

# Result aggregator — four parallel arrays (bash 3.2 friendly).
CHECK_NAMES=()
CHECK_STATUSES=()  # PASS | FAIL | WARN
CHECK_MESSAGES=()
CHECK_REMEDIES=()

# Expected env vars (post-P0-7 canonical UPPER_SNAKE; see PR #11).
# Ingestion service:
INGESTION_REQUIRED_ENV=(
    "AZURE_OPENAI_ENDPOINT"
    "AZURE_OPENAI_VERSION"
    "AZURE_OPENAI_EMBEDDING_MODEL"
    "AZURE_SEARCH_ENDPOINT"
    "INDEX_NAME"
    "DOC_INTELLIGENCE_ENDPOINT"
    "LOG_LEVEL"
    "AZURE_OPENAI_KEY"
    "AZURE_SEARCH_KEY"
    "DOC_INTELLIGENCE_KEY"
    "ADMIN_API_TOKEN"
)

# QnA service (post-P0-7 canonical names):
QNA_REQUIRED_ENV=(
    "AZURE_OPENAI_ENDPOINT"
    "AZURE_OPENAI_VERSION"
    "AZURE_OPENAI_LLM_MODEL"
    "AZURE_OPENAI_EMBEDDING_MODEL"
    "AZURE_SEARCH_ENDPOINT"
    "INDEX_NAME"
    "AUDIENCE_ID"
    "AZURE_KEY_VAULT"
    "AZURE_TENANT_ID"
    "LOG_LEVEL"
    "AZURE_OPENAI_KEY"
    "AZURE_SEARCH_KEY"
    "AZURE_CLIENT_ID"
    "AZURE_CLIENT_SECRET"
)

# Legacy PascalCase names (from P0-7 dual-read fallback) — presence is a WARN,
# not a FAIL; the service still works during the deprecation window.
LEGACY_ENV_NAMES=(
    "AzureOpenaiAccountEndpoint"
    "TocdocOpenAIKey"
    "AzureOpenaiApiVersion"
    "AzureOpenaiLlmModel"
    "AzureSearchEndpoint"
    "AzureSearchKey"
    "TocdocSPClientID"
    "TocdocSPSecretValue"
    "TocdocSPTenantID"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

usage() {
    cat <<EOF
Usage: $(basename "$0") --resource-group RG --ingestion-app NAME --qna-app NAME [options]

Required:
  --resource-group RG          Azure resource group containing the deployment
  --ingestion-app NAME         Ingestion Container App name (e.g., tocdoc-ingestion-prod)
  --qna-app NAME               QnA Container App name (e.g., tocdoc-qna-prod)

Optional:
  --environment ENV            Env suffix override for resource-name inference (default: inferred from app name)
  --deployment-name NAME       Bicep deployment name (default: main)
  --expected-index-name NAME   Cognitive Search index name (default: tocdoc-index)
  --skip-health-checks         Skip the HTTP /health probes (useful for cold-start
                               scenarios or when the operator is on a network without
                               egress to the Container App FQDNs)
  --output FORMAT              Output format: text (default) or json
  --help                       Show this message

Exit codes:
  0 — all required checks passed
  1 — one or more required checks failed
  2 — script usage error or az preflight failed
EOF
}

# Append a result to all four parallel arrays.
record_result() {
    local name="$1"
    local status="$2"
    local message="$3"
    local remedy="${4:-}"
    CHECK_NAMES+=("$name")
    CHECK_STATUSES+=("$status")
    CHECK_MESSAGES+=("$message")
    CHECK_REMEDIES+=("$remedy")
}

# JSON-escape a string. No jq dependency.
json_escape() {
    local s="$1"
    # Order matters: backslashes first, then double quotes, then control chars.
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    # Strip embedded newlines / carriage returns / tabs (replace with space).
    s=$(printf '%s' "$s" | tr '\n\r\t' '   ')
    printf '%s' "$s"
}

# Bash 3.2-friendly "is X in array Y" — pass the search value then the array
# elements as remaining args.
contains_element() {
    local needle="$1"
    shift
    local item
    for item in "$@"; do
        if [ "$item" = "$needle" ]; then
            return 0
        fi
    done
    return 1
}

# Parse a comma-or-newline-separated string into the script's own value.
# Caller must use `IFS=$'\n'` around the assignment when feeding tsv output.

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --resource-group)        RG="$2"; shift 2 ;;
            --ingestion-app)         INGESTION_APP="$2"; shift 2 ;;
            --qna-app)               QNA_APP="$2"; shift 2 ;;
            --environment)           ENVIRONMENT="$2"; shift 2 ;;
            --deployment-name)       DEPLOYMENT_NAME="$2"; shift 2 ;;
            --expected-index-name)   EXPECTED_INDEX_NAME="$2"; shift 2 ;;
            --skip-health-checks)    SKIP_HEALTH="true"; shift ;;
            --output)                OUTPUT="$2"; shift 2 ;;
            --help|-h)               usage; exit 0 ;;
            *)
                echo "Unknown argument: $1" >&2
                usage >&2
                exit 2
                ;;
        esac
    done

    if [ -z "$RG" ] || [ -z "$INGESTION_APP" ] || [ -z "$QNA_APP" ]; then
        echo "Missing required argument(s)." >&2
        usage >&2
        exit 2
    fi

    case "$OUTPUT" in
        text|json) ;;
        *)
            echo "Invalid --output value: $OUTPUT (expected text or json)" >&2
            exit 2
            ;;
    esac
}

# ---------------------------------------------------------------------------
# Preflight — must run before any other check
# ---------------------------------------------------------------------------

preflight_az_cli() {
    if ! command -v az >/dev/null 2>&1; then
        echo "ERROR: Azure CLI ('az') not found on PATH." >&2
        echo "Install per https://learn.microsoft.com/cli/azure/install-azure-cli then re-run." >&2
        exit 2
    fi

    local account_id
    account_id=$(az account show --query id -o tsv 2>/dev/null || true)
    if [ -z "$account_id" ]; then
        echo "ERROR: Azure CLI is not logged in or no subscription is selected." >&2
        echo "Run: az login && az account set --subscription <subscription-id>" >&2
        exit 2
    fi

    record_result "az_cli_authenticated" "PASS" "Azure CLI authenticated (subscription: $account_id)" ""
}

# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

check_resource_group() {
    local found
    found=$(az group show -n "$RG" --query name -o tsv 2>/dev/null || true)
    if [ "$found" = "$RG" ]; then
        record_result "resource_group_exists" "PASS" "Resource group exists: $RG" ""
    else
        record_result "resource_group_exists" "FAIL" "Resource group not found: $RG" "az group create --name $RG --location <region>"
    fi
}

check_expected_resources() {
    # Resource name prefix is derived from the Bicep `prefix` parameter (default "tocdoc"),
    # combined with the environment tag. We can't fetch the prefix without parsing the
    # Bicep deployment outputs, so we infer it from the container app name suffix.
    # If the operator used a non-default prefix, the resource name check may misfire —
    # this is reported as a WARN, not a FAIL.

    local prefix env_suffix
    # Derive: INGESTION_APP="tocdoc-ingestion-prod" → prefix="tocdoc", env_suffix="prod"
    prefix=$(printf '%s' "$INGESTION_APP" | awk -F- '{print $1}')
    # Use the explicit --environment override when given; otherwise infer the
    # suffix from the app name's last segment (the robust default).
    env_suffix="${ENVIRONMENT:-$(printf '%s' "$INGESTION_APP" | awk -F- '{print $NF}')}"

    # Check container apps exist (these are the resources that matter most for runtime).
    local apps=("$INGESTION_APP" "$QNA_APP")
    local app
    for app in "${apps[@]}"; do
        local found
        found=$(az containerapp show --name "$app" -g "$RG" --query name -o tsv 2>/dev/null || true)
        if [ "$found" = "$app" ]; then
            record_result "container_app_${app}_exists" "PASS" "Container App exists: $app" ""
        else
            record_result "container_app_${app}_exists" "FAIL" "Container App not found: $app" "az deployment group create --template-file infra/main.bicep ..."
        fi
    done

    # Check the other expected resources by naming convention.
    # Names: ${prefix}-{openai,search,docintel,kv,logs,appinsights,containerenv}-${env_suffix}
    local short_names=(
        "openai"
        "search"
        "docintel"
        "kv"
        "logs"
        "appinsights"
        "containerenv"
    )
    local short_name fullname
    for short_name in "${short_names[@]}"; do
        fullname="${prefix}-${short_name}-${env_suffix}"
        local exists
        # `az resource show` requires a concrete --resource-type; "*" is not a
        # reliable wildcard and can miss resources that exist. List by group and
        # filter by name instead (returns the name or empty).
        exists=$(az resource list -g "$RG" --query "[?name=='$fullname'].name | [0]" -o tsv 2>/dev/null || true)
        if [ -n "$exists" ]; then
            record_result "resource_${short_name}_exists" "PASS" "Found expected resource: $fullname" ""
        else
            # Most operators run with the default prefix; non-default prefixes (e.g.,
            # tocdocdev for dev environments) will trigger this WARN. The operator
            # can ignore it if they intentionally used a different prefix.
            record_result "resource_${short_name}_exists" "WARN" "Expected resource not found by naming convention: $fullname (may be normal if --environment used a non-default prefix)" ""
        fi
    done
}

check_container_app_revisions() {
    local apps=("$INGESTION_APP" "$QNA_APP")
    local app
    for app in "${apps[@]}"; do
        local active_count
        active_count=$(az containerapp revision list --name "$app" -g "$RG" \
            --query "length([?properties.active])" -o tsv 2>/dev/null || echo "0")

        if [ "$active_count" -ge 1 ] 2>/dev/null; then
            record_result "revision_${app}_active" "PASS" "Container App has $active_count active revision(s): $app" ""
        else
            record_result "revision_${app}_active" "FAIL" "Container App has no active revisions: $app" "az containerapp revision restart --name $app -g $RG"
            continue
        fi

        # Inspect the latest active revision's running state.
        # `runningState` may be Running, RunningAtMaxScale (scale-to-zero default = Idle when no traffic).
        # Idle / Provisioned is acceptable when minReplicas=0; Failed / Stopped is not.
        local running_state
        running_state=$(az containerapp revision list --name "$app" -g "$RG" \
            --query "[?properties.active] | [0].properties.runningState" -o tsv 2>/dev/null || true)
        case "$running_state" in
            Running|RunningAtMaxScale|Provisioned|Idle|"")
                record_result "revision_${app}_running_state" "PASS" "Latest revision running state: ${running_state:-unknown (Bicep template scales to zero by default)}" ""
                ;;
            Failed|Stopped|Degraded)
                record_result "revision_${app}_running_state" "FAIL" "Latest revision is in state: $running_state" "az containerapp logs show --name $app -g $RG --container <name>"
                ;;
            *)
                record_result "revision_${app}_running_state" "WARN" "Latest revision running state: $running_state" ""
                ;;
        esac
    done
}

check_env_vars() {
    local app_name="$1"
    shift
    # Remaining args are the required env var names.

    local required=("$@")

    # Fetch env var names — newline-separated TSV is the bash-3.2-safe form.
    local env_names_raw
    env_names_raw=$(az containerapp show --name "$app_name" -g "$RG" \
        --query "properties.template.containers[0].env[].name" -o tsv 2>/dev/null || true)

    if [ -z "$env_names_raw" ]; then
        record_result "env_${app_name}" "FAIL" "Could not fetch env var names for Container App: $app_name" ""
        return
    fi

    # Convert newline-separated names into a bash array.
    local env_names=()
    local line
    while IFS= read -r line; do
        [ -n "$line" ] && env_names+=("$line")
    done <<EOF
$env_names_raw
EOF

    # Check each required canonical name.
    local var
    local missing=()
    for var in "${required[@]}"; do
        if ! contains_element "$var" "${env_names[@]}"; then
            missing+=("$var")
        fi
    done

    if [ "${#missing[@]}" -eq 0 ]; then
        record_result "env_${app_name}_canonical" "PASS" "All required env vars present on $app_name (${#required[@]} checked)" ""
    else
        local missing_list
        missing_list=$(printf '%s,' "${missing[@]}")
        missing_list=${missing_list%,}
        record_result "env_${app_name}_canonical" "FAIL" "Missing required env vars on $app_name: $missing_list" "az containerapp update --name $app_name -g $RG --set-env-vars <NAME>=<VALUE>"
    fi

    # Check for legacy PascalCase names (P0-7 deprecation period) — only relevant for QnA.
    if [ "$app_name" = "$QNA_APP" ]; then
        local legacy_present=()
        local legacy
        for legacy in "${LEGACY_ENV_NAMES[@]}"; do
            if contains_element "$legacy" "${env_names[@]}"; then
                legacy_present+=("$legacy")
            fi
        done
        if [ "${#legacy_present[@]}" -gt 0 ]; then
            local legacy_list
            legacy_list=$(printf '%s,' "${legacy_present[@]}")
            legacy_list=${legacy_list%,}
            record_result "env_${app_name}_legacy" "WARN" "Deprecated PascalCase env vars present (still accepted via dual-read; rename to canonical UPPER_SNAKE): $legacy_list" "See docs/deployment/INSTALLATION.md 'Migrating from a pre-P0-7 deployment' for the rename table"
        else
            record_result "env_${app_name}_legacy" "PASS" "No deprecated PascalCase env vars present on $app_name" ""
        fi
    fi
}

check_health_endpoints() {
    if [ -n "$SKIP_HEALTH" ]; then
        record_result "health_endpoints" "WARN" "Skipped (--skip-health-checks)" ""
        return
    fi

    local apps_paths=("$INGESTION_APP /upload_pipeline/health" "$QNA_APP /qna/health")
    local entry app path fqdn http_code curl_status
    for entry in "${apps_paths[@]}"; do
        app=$(printf '%s' "$entry" | awk '{print $1}')
        path=$(printf '%s' "$entry" | awk '{print $2}')

        fqdn=$(az containerapp show --name "$app" -g "$RG" \
            --query properties.configuration.ingress.fqdn -o tsv 2>/dev/null || true)
        if [ -z "$fqdn" ]; then
            record_result "health_${app}" "FAIL" "Could not resolve FQDN for Container App: $app" ""
            continue
        fi

        # --max-time accommodates cold-start when minReplicas=0.
        # Capture curl's exit status separately. Appending `|| echo "000"` to the
        # `%{http_code}` output risks concatenating to "000000" on failure, which
        # would fall through to the generic-HTTP-failure branch instead of the
        # intended unreachable branch.
        http_code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time 45 --retry 1 --retry-delay 5 \
            "https://${fqdn}${path}" 2>/dev/null)
        curl_status=$?
        if [ "$curl_status" -ne 0 ] || [ -z "$http_code" ]; then
            http_code="000"
        fi

        if [ "$http_code" = "200" ]; then
            record_result "health_${app}" "PASS" "Health endpoint OK: https://${fqdn}${path}" ""
        elif [ "$http_code" = "000" ]; then
            record_result "health_${app}" "FAIL" "Health endpoint unreachable: https://${fqdn}${path}" "Check Container App revision status; cold start may take up to 60s with minReplicas=0"
        else
            record_result "health_${app}" "FAIL" "Health endpoint returned HTTP $http_code: https://${fqdn}${path}" "Check Container App logs"
        fi
    done
}

check_key_vault() {
    # Infer the Key Vault name from the QnA app's AZURE_KEY_VAULT env var.
    local kv_name
    kv_name=$(az containerapp show --name "$QNA_APP" -g "$RG" \
        --query "properties.template.containers[0].env[?name=='AZURE_KEY_VAULT'].value | [0]" -o tsv 2>/dev/null || true)

    if [ -z "$kv_name" ]; then
        record_result "key_vault_configured" "WARN" "AZURE_KEY_VAULT not set on $QNA_APP — Key Vault loading is disabled" ""
        return
    fi

    # Check the vault exists.
    local found
    found=$(az keyvault show --name "$kv_name" --query name -o tsv 2>/dev/null || true)
    if [ "$found" != "$kv_name" ]; then
        record_result "key_vault_exists" "FAIL" "Key Vault not found: $kv_name" ""
        return
    fi
    record_result "key_vault_exists" "PASS" "Key Vault exists: $kv_name" ""

    # Attempt to list secret names (NAMES ONLY — values never fetched).
    # Listing may 403 if the current principal lacks Key Vault Secrets User; that's a WARN.
    local secret_names_raw
    secret_names_raw=$(az keyvault secret list --vault-name "$kv_name" --query "[].name" -o tsv 2>/dev/null || true)
    if [ -z "$secret_names_raw" ]; then
        record_result "key_vault_secrets_listable" "WARN" "Cannot list secrets in $kv_name (current principal may lack Key Vault Secrets User role, or vault is empty)" "az role assignment create --role 'Key Vault Secrets User' --assignee <principal> --scope <vault-resource-id>"
        return
    fi

    # Look for canonical hyphenated names (per P0-7 KV naming convention).
    local canonical_secrets=(
        "azure-openai-endpoint"
        "azure-openai-key"
        "azure-search-endpoint"
        "azure-search-key"
        "azure-client-id"
        "azure-client-secret"
    )
    local secret_names=()
    local line
    while IFS= read -r line; do
        [ -n "$line" ] && secret_names+=("$line")
    done <<EOF
$secret_names_raw
EOF

    local secret found_canonical=0
    for secret in "${canonical_secrets[@]}"; do
        if contains_element "$secret" "${secret_names[@]}"; then
            found_canonical=$((found_canonical + 1))
        fi
    done

    if [ "$found_canonical" -gt 0 ]; then
        record_result "key_vault_canonical_secrets" "PASS" "Found $found_canonical canonical hyphenated KV secret(s) in $kv_name" ""
    else
        record_result "key_vault_canonical_secrets" "WARN" "No canonical hyphenated secrets found in $kv_name — QnA reads env vars directly (not from KV). Operator should populate $kv_name with hyphenated-lowercase secret names for the dual-read fallback to upgrade legacy values." "See docs/deployment/INSTALLATION.md 'Migrating from a pre-P0-7 deployment'"
    fi
}

check_search_service() {
    # Infer the Search service name from the QnA app's AZURE_SEARCH_ENDPOINT env var.
    local search_endpoint
    search_endpoint=$(az containerapp show --name "$QNA_APP" -g "$RG" \
        --query "properties.template.containers[0].env[?name=='AZURE_SEARCH_ENDPOINT'].value | [0]" -o tsv 2>/dev/null || true)

    if [ -z "$search_endpoint" ]; then
        record_result "search_service_configured" "FAIL" "AZURE_SEARCH_ENDPOINT not set on $QNA_APP" ""
        return
    fi

    # Extract the service name from the endpoint URL: https://<name>.search.windows.net
    local search_name
    search_name=$(printf '%s' "$search_endpoint" | sed -E 's|^https?://([^.]+)\..*|\1|')

    local found
    found=$(az search service show --name "$search_name" -g "$RG" --query name -o tsv 2>/dev/null || true)
    if [ "$found" = "$search_name" ]; then
        record_result "search_service_exists" "PASS" "Cognitive Search service exists: $search_name" ""
    else
        record_result "search_service_exists" "FAIL" "Cognitive Search service not found: $search_name (derived from $search_endpoint)" ""
    fi

    # Data-plane index check (admin-key required) is deferred to a follow-up — the script
    # would need the admin key value, which contradicts the never-print-secrets stance.
    record_result "search_index_check" "WARN" "Index ($EXPECTED_INDEX_NAME) existence not verified — admin-key data-plane check deferred to a follow-up validation. The index is created lazily on first ingestion if absent." ""
}

check_deployment_outputs() {
    # Confirm Bicep deployment outputs are present so the runbook's
    # `az deployment group show --query 'properties.outputs.<X>'` commands succeed.
    local outputs_raw
    outputs_raw=$(az deployment group show -g "$RG" --name "$DEPLOYMENT_NAME" \
        --query "properties.outputs" -o json 2>/dev/null || true)
    if [ -z "$outputs_raw" ] || [ "$outputs_raw" = "null" ]; then
        record_result "deployment_outputs" "WARN" "Deployment '$DEPLOYMENT_NAME' not found or has no outputs (operator may have used a different --deployment-name)" "az deployment group list -g $RG"
        return
    fi

    local expected_outputs=(
        "ingestionAppFqdn"
        "qnaAppFqdn"
        "keyVaultName"
        "openAiEndpoint"
        "searchEndpoint"
        "docIntelEndpoint"
        "appInsightsConnectionString"
    )
    local out missing=()
    for out in "${expected_outputs[@]}"; do
        # Check if the output key exists in the JSON. Crude but jq-free.
        if ! printf '%s' "$outputs_raw" | grep -q "\"$out\""; then
            missing+=("$out")
        fi
    done

    if [ "${#missing[@]}" -eq 0 ]; then
        record_result "deployment_outputs_present" "PASS" "All ${#expected_outputs[@]} expected Bicep outputs present in deployment '$DEPLOYMENT_NAME'" ""
    else
        local missing_list
        missing_list=$(printf '%s,' "${missing[@]}")
        missing_list=${missing_list%,}
        record_result "deployment_outputs_present" "FAIL" "Missing Bicep outputs in deployment '$DEPLOYMENT_NAME': $missing_list" "Re-run Bicep deployment per docs/deployment/INSTALLATION.md Step 1"
    fi
}

# ---------------------------------------------------------------------------
# Output renderers
# ---------------------------------------------------------------------------

render_text() {
    local i symbol
    for ((i = 0; i < ${#CHECK_NAMES[@]}; i++)); do
        case "${CHECK_STATUSES[$i]}" in
            PASS) symbol="[PASS]" ;;
            FAIL) symbol="[FAIL]" ;;
            WARN) symbol="[WARN]" ;;
            *)    symbol="[????]" ;;
        esac
        printf '%-7s %s — %s\n' "$symbol" "${CHECK_NAMES[$i]}" "${CHECK_MESSAGES[$i]}"
        if [ -n "${CHECK_REMEDIES[$i]}" ]; then
            printf '        Remedy: %s\n' "${CHECK_REMEDIES[$i]}"
        fi
    done
}

render_json() {
    local overall="passed"
    local i status_lc esc_name esc_message esc_remedy
    for ((i = 0; i < ${#CHECK_STATUSES[@]}; i++)); do
        if [ "${CHECK_STATUSES[$i]}" = "FAIL" ]; then
            overall="failed"
            break
        fi
    done

    printf '{\n  "status": "%s",\n  "resource_group": "%s",\n  "checks": [\n' "$overall" "$(json_escape "$RG")"
    for ((i = 0; i < ${#CHECK_NAMES[@]}; i++)); do
        status_lc=$(printf '%s' "${CHECK_STATUSES[$i]}" | tr '[:upper:]' '[:lower:]')
        esc_name=$(json_escape "${CHECK_NAMES[$i]}")
        esc_message=$(json_escape "${CHECK_MESSAGES[$i]}")
        esc_remedy=$(json_escape "${CHECK_REMEDIES[$i]}")
        if [ "$i" -gt 0 ]; then
            printf ',\n'
        fi
        printf '    {"name": "%s", "status": "%s", "message": "%s", "remedy": "%s"}' \
            "$esc_name" "$status_lc" "$esc_message" "$esc_remedy"
    done
    printf '\n  ]\n}\n'
}

# ---------------------------------------------------------------------------
# Exit-code logic
# ---------------------------------------------------------------------------

compute_exit_code() {
    local i
    for ((i = 0; i < ${#CHECK_STATUSES[@]}; i++)); do
        if [ "${CHECK_STATUSES[$i]}" = "FAIL" ]; then
            return 1
        fi
    done
    return 0
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    parse_args "$@"

    # Preflight short-circuits with exit 2 on failure — no other check runs.
    preflight_az_cli

    check_resource_group
    check_expected_resources
    check_container_app_revisions
    check_env_vars "$INGESTION_APP" "${INGESTION_REQUIRED_ENV[@]}"
    check_env_vars "$QNA_APP" "${QNA_REQUIRED_ENV[@]}"
    check_key_vault
    check_search_service
    check_health_endpoints
    check_deployment_outputs

    case "$OUTPUT" in
        text) render_text ;;
        json) render_json ;;
    esac

    compute_exit_code
}

main "$@"
