{{- define "tirkit.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "tirkit.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "tirkit.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
app.kubernetes.io/name: {{ include "tirkit.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/* Selector labels for one component: include "tirkit.selectorLabels" (list . "server") */}}
{{- define "tirkit.selectorLabels" -}}
{{- $ctx := index . 0 -}}
app.kubernetes.io/name: {{ include "tirkit.name" $ctx }}
app.kubernetes.io/instance: {{ $ctx.Release.Name }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end }}

{{- define "tirkit.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) }}
{{- end }}

{{- define "tirkit.secretName" -}}
{{- default (include "tirkit.fullname" .) .Values.secrets.existingSecret }}
{{- end }}

{{- define "tirkit.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "tirkit.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "tirkit.mediaClaim" -}}
{{- default (printf "%s-media" (include "tirkit.fullname" .)) .Values.media.persistence.existingClaim }}
{{- end }}

{{/* The Postgres password: the value given, else the one already stored, else a new one. */}}
{{- define "tirkit.postgresPassword" -}}
{{- if .Values.postgresql.auth.password }}
{{- .Values.postgresql.auth.password }}
{{- else }}
{{- $existing := lookup "v1" "Secret" .Release.Namespace (include "tirkit.fullname" .) }}
{{- if and $existing $existing.data (hasKey $existing.data "POSTGRES_PASSWORD") }}
{{- index $existing.data "POSTGRES_PASSWORD" | b64dec }}
{{- else }}
{{- randAlphaNum 32 }}
{{- end }}
{{- end }}
{{- end }}

{{/* Environment shared by the server and the worker. */}}
{{- define "tirkit.env" -}}
- name: TIRKIT_DATABASE_URL
  valueFrom:
    secretKeyRef: {name: {{ include "tirkit.secretName" . }}, key: DATABASE_URL}
- name: TIRKIT_MEDIA_DIR
  value: /data/media
- name: TIRKIT_EMBEDDED_WORKER
  value: "0"
- name: PROMETHEUS_MULTIPROC_DIR
  value: /tmp/tirkit-metrics
{{- if not .Values.metrics.enabled }}
- name: TIRKIT_METRICS
  value: "0"
{{- end }}
- name: TIRKIT_METRICS_TOKEN
  valueFrom:
    secretKeyRef: {name: {{ include "tirkit.secretName" . }}, key: METRICS_TOKEN, optional: true}
{{- with .Values.config.adminEmail }}
- name: TIRKIT_ADMIN_EMAIL
  value: {{ . | quote }}
{{- end }}
- name: TIRKIT_ADMIN_PASSWORD
  valueFrom:
    secretKeyRef: {name: {{ include "tirkit.secretName" . }}, key: ADMIN_PASSWORD, optional: true}
{{- with .Values.config.llmBaseUrl }}
- name: LLM_BASE_URL
  value: {{ . | quote }}
{{- end }}
{{- with .Values.config.judgeModel }}
- name: JUDGE_MODEL
  value: {{ . | quote }}
{{- end }}
- name: LLM_API_KEY
  valueFrom:
    secretKeyRef: {name: {{ include "tirkit.secretName" . }}, key: LLM_API_KEY, optional: true}
- name: ANTHROPIC_API_KEY
  valueFrom:
    secretKeyRef: {name: {{ include "tirkit.secretName" . }}, key: ANTHROPIC_API_KEY, optional: true}
{{- end }}

{{- define "tirkit.volumes" -}}
- name: media
{{- if .Values.media.persistence.enabled }}
  persistentVolumeClaim:
    claimName: {{ include "tirkit.mediaClaim" . }}
{{- else }}
  emptyDir: {}
{{- end }}
- name: tmp
  emptyDir: {}
{{- end }}

{{- define "tirkit.volumeMounts" -}}
- name: media
  mountPath: /data/media
- name: tmp
  mountPath: /tmp
{{- end }}
