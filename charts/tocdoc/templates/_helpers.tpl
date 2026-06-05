{{/*
Common helpers for the TocDoc chart.
*/}}

{{/* Chart name, sanitized. */}}
{{- define "tocdoc.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully qualified app name: "<release>-tocdoc" (truncated to 63 chars). */}}
{{- define "tocdoc.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "tocdoc.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* Chart label value "<name>-<version>". */}}
{{- define "tocdoc.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels applied to every resource.
Usage: {{- include "tocdoc.labels" . | nindent 4 }}
*/}}
{{- define "tocdoc.labels" -}}
helm.sh/chart: {{ include "tocdoc.chart" . }}
app.kubernetes.io/name: {{ include "tocdoc.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: tocdoc
tocdoc.io/environment: {{ .Values.global.environment | quote }}
{{- end -}}

{{/*
The name of the Secret env vars are sourced from. Precedence:
  1. secret.externalSecretName (explicit operator override)
  2. keyVaultCSI.syncedSecretName when keyVaultCSI.enabled (the CSI driver syncs
     Key Vault objects into that Secret; without this the Deployment would point
     at "<fullname>" while the driver writes to syncedSecretName -> pods fail).
  3. "<fullname>" (matches the chart-rendered placeholder Secret).
*/}}
{{- define "tocdoc.secretName" -}}
{{- if .Values.secret.externalSecretName -}}
{{- .Values.secret.externalSecretName -}}
{{- else if .Values.keyVaultCSI.enabled -}}
{{- .Values.keyVaultCSI.syncedSecretName -}}
{{- else -}}
{{- include "tocdoc.fullname" . -}}
{{- end -}}
{{- end -}}
