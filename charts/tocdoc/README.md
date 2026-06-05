# TocDoc Helm chart

Packages the two TocDoc Enterprise RAG services — **ingestion** and **QnA** — for
deployment to **Azure Kubernetes Service (AKS)**.

This is an **alternative to the Bicep + Azure Container Apps path** under
[`infra/main.bicep`](../../infra/main.bicep). It targets the Enterprise tier,
where customers run their own AKS cluster and want Kubernetes-native packaging,
autoscaling, pod disruption budgets, and Key Vault CSI secret injection. The
env-var contract, ports, and secret names mirror the Bicep template so the two
paths stay interchangeable.

| | Ingestion | QnA |
|---|---|---|
| Container port | 5501 | 5500 |
| Health probe | `GET /health` | `GET /health` |
| Image (default) | `tocdoc/ingestion` | `tocdoc/qna` |

Each enabled service gets a Deployment, Service (ClusterIP), optional Ingress,
HorizontalPodAutoscaler, and PodDisruptionBudget. Optional production-hardening
resources — NetworkPolicy, a Prometheus ServiceMonitor (or scrape annotations),
pod anti-affinity and topology spread — are all off by default and toggled in
`values.yaml`. A ready-to-edit [`values-production.yaml`](values-production.yaml)
turns the recommended set on.

## Requirements

- Kubernetes 1.23+ (AKS) and Helm 3.8+ (validated with Helm v4).
- Service container images pushed to a registry the cluster can pull (e.g. ACR).
- For production secret injection: the
  [Azure Key Vault provider for Secrets Store CSI Driver](https://learn.microsoft.com/azure/aks/csi-secrets-store-driver)
  (or the External Secrets Operator).

## Install

```bash
# From the repo root. Override image tags/registry and point at your secret.
helm upgrade --install tocdoc charts/tocdoc \
  --namespace tocdoc --create-namespace \
  --set global.imageRegistry=myregistry.azurecr.io \
  --set services.ingestion.image.tag=1.4.0 \
  --set services.qna.image.tag=1.4.0 \
  --set secret.externalSecretName=tocdoc-secrets
```

Render without installing (useful for review / CI):

```bash
helm template tocdoc charts/tocdoc | less
```

## Secrets — never inline them

**No secret values live in this chart.** Every secret env var is pulled from a
Kubernetes `Secret` via `secretKeyRef`. The Secret keys are the canonical
`UPPER_SNAKE` env-var names the services read at startup:

| Secret key | Ingestion | QnA |
|---|:---:|:---:|
| `AZURE_OPENAI_KEY` | yes | yes |
| `AZURE_SEARCH_KEY` | yes | yes |
| `DOC_INTELLIGENCE_KEY` | yes | — |
| `ADMIN_API_TOKEN` | yes | — |
| `AZURE_CLIENT_ID` | — | yes |
| `AZURE_CLIENT_SECRET` | — | yes |

You have three ways to supply that Secret.

### Option A (recommended): Azure Key Vault via the Secrets Store CSI driver

1. Install the Secrets Store CSI driver and the Azure provider on the cluster,
   and grant the cluster's workload identity / managed identity
   **Key Vault Secrets User** on the vault (the same role the Bicep template
   assigns).
2. Enable the bundled `SecretProviderClass` scaffold:

   ```bash
   helm upgrade --install tocdoc charts/tocdoc \
     --set keyVaultCSI.enabled=true \
     --set keyVaultCSI.keyvaultName=tocdoc-kv-prod \
     --set keyVaultCSI.tenantId=<tenant-id> \
     --set keyVaultCSI.userAssignedIdentityID=<uami-client-id> \
     --set keyVaultCSI.syncedSecretName=tocdoc-secrets \
     --set secret.externalSecretName=tocdoc-secrets
   ```

   The `SecretProviderClass` maps Key Vault objects (named as in the Bicep
   template: `azure-openai-key`, `azure-search-key`, `doc-intel-key`,
   `admin-api-token`, `azure-client-id`, `azure-client-secret`) to the canonical
   `UPPER_SNAKE` Secret keys above.

   When `keyVaultCSI.enabled=true`, each Deployment automatically mounts the CSI
   volume (`secrets-store.csi.k8s.io`) referencing this `SecretProviderClass`.
   That mount is what triggers the driver to materialize the synced Secret, so
   the env `secretKeyRef` lookups resolve. You still need to grant the cluster's
   identity access to the vault and set `secret.externalSecretName` to the
   synced Secret name (step 2 above).

### Option B: External Secrets Operator (or any pre-existing Secret)

Create the Secret out-of-band with the keys above, then reference it:

```bash
--set secret.externalSecretName=tocdoc-secrets
```

### Option C: chart-rendered placeholder Secret (non-production only)

For local/dev bootstrap you can have the chart render a Secret from values.
**Never commit real secret values** — pass them at install time and keep any
override file out of version control:

```bash
helm upgrade --install tocdoc charts/tocdoc \
  --set secret.create=true \
  --set secret.data.AZURE_OPENAI_KEY=$AZURE_OPENAI_KEY \
  --set secret.data.AZURE_SEARCH_KEY=$AZURE_SEARCH_KEY \
  --set secret.data.DOC_INTELLIGENCE_KEY=$DOC_INTELLIGENCE_KEY \
  --set secret.data.ADMIN_API_TOKEN=$ADMIN_API_TOKEN \
  --set secret.data.AZURE_CLIENT_ID=$AZURE_CLIENT_ID \
  --set secret.data.AZURE_CLIENT_SECRET=$AZURE_CLIENT_SECRET
```

## Values

### Global

| Key | Default | Description |
|---|---|---|
| `global.environment` | `prod` | `dev` / `staging` / `prod`; tags resources. |
| `global.imageRegistry` | `""` | Registry prefix prepended to each image repository. |
| `global.imagePullSecrets` | `[]` | Image pull secrets for all Deployments. |
| `global.config.*` | see `values.yaml` | Non-secret env vars shared by both services (Azure OpenAI / Search endpoints, `INDEX_NAME`, `LOG_LEVEL`, optional `EMBEDDING_DIMENSIONS`, `AZURE_SEARCH_SEMANTIC_CONFIG`, `CORS_ALLOWED_ORIGINS`). |

### Secret / Key Vault CSI

| Key | Default | Description |
|---|---|---|
| `secret.create` | `false` | Render a placeholder Secret from `secret.data` (non-prod only). |
| `secret.externalSecretName` | `""` | Name of a pre-existing Secret to source env vars from. Defaults to `<release>-tocdoc` when empty. |
| `secret.data.*` | `""` | Placeholder secret values used only when `create=true`. |
| `keyVaultCSI.enabled` | `false` | Render an Azure `SecretProviderClass`. |
| `keyVaultCSI.keyvaultName` | `tocdoc-kv-prod` | Target Key Vault name. |
| `keyVaultCSI.tenantId` | `<your-azure-tenant-id>` | Azure AD tenant. |
| `keyVaultCSI.userAssignedIdentityID` | `""` | UAMI client ID used by the CSI driver. |
| `keyVaultCSI.syncedSecretName` | `tocdoc-secrets` | Name of the synced Secret produced by the CSI driver. |
| `keyVaultCSI.objects` | see `values.yaml` | Key Vault object name → Secret key mappings. |

### Per service (`services.ingestion.*`, `services.qna.*`)

| Key | Default | Description |
|---|---|---|
| `enabled` | `true` | Render this service's resources. |
| `replicaCount` | `1` | Replicas when autoscaling is disabled. |
| `image.repository` | `tocdoc/<svc>` | Image repository. |
| `image.tag` | `""` | Image tag; defaults to `.Chart.AppVersion` when empty. |
| `image.pullPolicy` | `IfNotPresent` | Image pull policy. |
| `containerPort` | `5501` / `5500` | Container port (matches the Dockerfile `EXPOSE`). |
| `config.*` | per service | Service-specific plain env vars (merged over `global.config`). |
| `secretEnv` | per service | Secret keys injected as env vars via `secretKeyRef`. |
| `service.type` / `service.port` | `ClusterIP` / `80` | Service exposure. |
| `resources` | 0.5–1 CPU / 1–2Gi | Requests/limits. |
| `livenessProbe` / `readinessProbe` | `/health` | HTTP probes against the container port. |
| `autoscaling.enabled` | `true` | Render an HPA. |
| `autoscaling.minReplicas` | `1` | HPA floor (clamped to ≥ 1 — stock HPA cannot scale to zero; the ACA path's scale-to-zero needs KEDA). |
| `autoscaling.maxReplicas` | `3` | HPA ceiling (matches the Bicep `maxReplicas`). |
| `autoscaling.target{CPU,Memory}UtilizationPercentage` | `70` / `80` | HPA targets. |
| `autoscaling.extraMetrics` | `[]` | Extra HPA metrics (custom/external/pods) appended under `spec.metrics`. |
| `autoscaling.behavior` | `{}` | Optional HPA `spec.behavior` (scale-up/down stabilization & policies). |
| `podDisruptionBudget.enabled` | `true` | Render a PDB. |
| `podDisruptionBudget.minAvailable` | `1` | PDB floor. |
| `ingress.enabled` | `false` | Render an Ingress for this service. |
| `ingress.className` / `annotations` / `host` / `path` / `pathType` / `tls` | see `values.yaml` | Ingress routing. |
| `podAnnotations` / `nodeSelector` / `tolerations` / `affinity` | empty | Standard pod scheduling knobs. |
| `topologySpreadConstraints` | `[]` | Explicit spread constraints; overrides the global `topologySpread` toggle for this service. |

### Production hardening (all optional, default off)

These chart-wide toggles add production resources. Every one defaults to **off**
so a default `helm template` / install on a plain or single-node cluster still
renders and schedules. Enable them in `values.yaml` or via
[`values-production.yaml`](values-production.yaml).

#### NetworkPolicy (`networkPolicy.*`)

Renders a `NetworkPolicy` per enabled service, **scoped to this chart's own pods**
(never the whole namespace, so co-tenant workloads are untouched). Default-deny
ingress with explicit allows, plus egress limited to `egressPorts`. Only enforced
on clusters whose CNI supports NetworkPolicy (Azure CNI / Calico).

| Key | Default | Description |
|---|---|---|
| `networkPolicy.enabled` | `false` | Render NetworkPolicies. |
| `networkPolicy.allowFromLabels` | `[]` | Sources allowed to reach the services on their container port. Each entry is either a plain `matchLabels` map (same-namespace pods) or a map with `podLabels` and/or `namespaceLabels`. **Cross-namespace sources (Prometheus, an ingress controller in its own namespace) must set `namespaceLabels`** or they are silently denied. Empty = no extra ingress sources. |
| `networkPolicy.egressPorts` | `[53, 443]` | Allowed egress ports. **Must include `53`** or DNS breaks (pods can't resolve Azure OpenAI / Search / Key Vault); `53` emits a UDP+TCP rule, others emit TCP. `443` covers HTTPS to Azure. |
| `networkPolicy.extraIngress` / `extraEgress` | `[]` | Raw additional rule objects appended verbatim (CIDR allowlists, namespace selectors, etc.). |

#### Metrics / Prometheus (`metrics.*`)

> **The application must expose Prometheus metrics on `metrics.path` (default
> `/metrics`) on its HTTP container port** for either option below to collect
> anything. The chart only wires up scraping; it does not add a metrics endpoint.

| Key | Default | Description |
|---|---|---|
| `metrics.path` | `/metrics` | HTTP path the app serves Prometheus metrics on. |
| `metrics.serviceMonitor.enabled` | `false` | Render a Prometheus Operator `ServiceMonitor` (CRD `monitoring.coreos.com/v1`). Requires the operator + CRD on the cluster. |
| `metrics.serviceMonitor.interval` / `scrapeTimeout` | `30s` / `10s` | Scrape cadence and per-scrape timeout. |
| `metrics.serviceMonitor.labels` | `{}` | Extra labels so the operator's `serviceMonitorSelector` picks it up (e.g. `release: kube-prometheus-stack`). |
| `metrics.podAnnotations.enabled` | `false` | Alternative for plain Prometheus (no operator): emit `prometheus.io/scrape`, `/path`, `/port` annotations on each pod. |

#### High-availability scheduling (`podAntiAffinity.*`, `topologySpread.*`)

| Key | Default | Description |
|---|---|---|
| `podAntiAffinity.enabled` | `false` | Generate a soft (preferred) pod anti-affinity spreading a service's replicas across nodes. Skipped for any service that sets its own `affinity` (explicit wins; the `affinity` key is emitted at most once). |
| `podAntiAffinity.topologyKey` | `kubernetes.io/hostname` | Topology key for the anti-affinity term. |
| `topologySpread.enabled` | `false` | Generate `topologySpreadConstraints` spreading replicas across a topology domain. Skipped for any service that sets its own `topologySpreadConstraints`. |
| `topologySpread.maxSkew` | `1` | Max skew between domains. |
| `topologySpread.topologyKey` | `topology.kubernetes.io/zone` | Domain to spread across. |
| `topologySpread.whenUnsatisfiable` | `ScheduleAnyway` | `DoNotSchedule` (hard) or `ScheduleAnyway` (soft). |

## Differences from the Bicep / ACA path

- **Scale-to-zero:** the Bicep template sets `minReplicas: 0` (an Azure
  Container Apps feature). A stock Kubernetes HPA cannot scale to zero, so this
  chart's HPA floor is clamped to ≥ 1. Use KEDA for scale-to-zero on AKS.
- **Managed identity:** the Bicep grants each app a system-assigned identity the
  **Key Vault Secrets User** role. On AKS the equivalent is AKS workload identity
  / a user-assigned identity wired to the Secrets Store CSI driver
  (`keyVaultCSI.*`).
- **Ingress:** ACA provides built-in external ingress; on AKS you bring your own
  ingress controller and enable `services.<svc>.ingress`.

## Validation

```bash
helm lint --strict charts/tocdoc
helm template tocdoc charts/tocdoc            # default render

# All toggles on
helm template tocdoc charts/tocdoc \
  --set secret.create=true \
  --set keyVaultCSI.enabled=true \
  --set services.ingestion.ingress.enabled=true \
  --set services.qna.ingress.enabled=true \
  --set networkPolicy.enabled=true \
  --set metrics.serviceMonitor.enabled=true \
  --set metrics.podAnnotations.enabled=true \
  --set podAntiAffinity.enabled=true \
  --set topologySpread.enabled=true

# Example production overrides
helm template tocdoc charts/tocdoc -f charts/tocdoc/values-production.yaml
```

Each render can be piped through a YAML parser to confirm valid output, e.g.:

```bash
helm template tocdoc charts/tocdoc \
  | python3 -c "import sys,yaml; list(yaml.safe_load_all(sys.stdin))"
```
