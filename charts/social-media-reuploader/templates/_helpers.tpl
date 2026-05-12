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
