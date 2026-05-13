{{/* Chart helper templates */}}
{{- define "social-media-reuploader.name" -}}
{{- default .Chart.Name .Values.nameOverride -}}
{{- end -}}

{{- define "social-media-reuploader.fullname" -}}
{{- if .Values.fullnameOverride }}
  {{- printf "%s" .Values.fullnameOverride }}
{{- else }}
  {{- printf "%s" (include "social-media-reuploader.name" .) }}
{{- end }}
{{- end -}}

{{- /* Standard labels for de-facto Helm/Kubernetes conventions */ -}}
{{- define "social-media-reuploader.labels" -}}
{{- $name := include "social-media-reuploader.name" . -}}
{{- $labels := dict
    "app.kubernetes.io/name" $name
    "app.kubernetes.io/instance" .Release.Name
    "app.kubernetes.io/version" .Chart.AppVersion
    "app.kubernetes.io/managed-by" .Release.Service
    "helm.sh/chart" (printf "%s-%s" .Chart.Name .Chart.Version)
  -}}
{{ toYaml $labels }}
{{- end -}}
