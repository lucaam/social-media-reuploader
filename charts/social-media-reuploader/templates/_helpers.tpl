{{/* Chart helper templates */}}

{{- define "social-media-reuploader.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name: "<release>-<chart>", or just the release name when it
already contains the chart name (e.g. release "social-media-reuploader").
*/}}
{{- define "social-media-reuploader.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Name of the GUI Deployment and Service (a DNS label: max 63 chars). */}}
{{- define "social-media-reuploader.guiFullname" -}}
{{- printf "%s-gui" (include "social-media-reuploader.fullname" . | trunc 59 | trimSuffix "-") -}}
{{- end -}}

{{- define "social-media-reuploader.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Common labels. */}}
{{- define "social-media-reuploader.labels" -}}
helm.sh/chart: {{ include "social-media-reuploader.chart" . }}
app.kubernetes.io/name: {{ include "social-media-reuploader.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Selector labels of a component ("bot" or "gui"). Kept identical to the
previous chart versions: Deployment selectors are immutable.
Usage: include "social-media-reuploader.selectorLabels" (list $ "bot")
*/}}
{{- define "social-media-reuploader.selectorLabels" -}}
{{- $root := index . 0 -}}
app.kubernetes.io/instance: {{ $root.Release.Name }}
app.kubernetes.io/component: {{ index . 1 }}
{{- end -}}

{{- define "social-media-reuploader.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (printf "%s-sa" (include "social-media-reuploader.fullname" .)) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{/* Image of the bot and (unless overridden) of the GUI. */}}
{{- define "social-media-reuploader.image" -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion | toString) -}}
{{- end -}}

{{- define "social-media-reuploader.guiImage" -}}
{{- $repository := .Values.gui.image.repository | default .Values.image.repository -}}
{{- $tag := .Values.gui.image.tag | default .Values.image.tag | default .Chart.AppVersion -}}
{{- printf "%s:%s" $repository ($tag | toString) -}}
{{- end -}}

{{/*
Resource profile of the bot as YAML (use with fromYaml); fails on unknown names.
*/}}
{{- define "social-media-reuploader.profile" -}}
{{- if not (hasKey .Values.resourceProfiles .Values.resourceProfile) -}}
{{- fail (printf "unknown resourceProfile %q: use one of %s" (toString .Values.resourceProfile) (keys .Values.resourceProfiles | sortAlpha | join ", ")) -}}
{{- end -}}
{{- toYaml (index .Values.resourceProfiles .Values.resourceProfile) -}}
{{- end -}}

{{/* Grace period of the bot pod (seconds). */}}
{{- define "social-media-reuploader.gracePeriod" -}}
{{- $profile := include "social-media-reuploader.profile" . | fromYaml -}}
{{- .Values.terminationGracePeriodSeconds | default $profile.terminationGracePeriodSeconds | default 60 | int64 -}}
{{- end -}}

{{/*
String form of a value of the `env` maps. Helm reads numbers of values files
as float64 and would print 52428800 as "5.24288e+07": integral numbers are
rendered as integers, other numbers as they are, booleans as true/false and
lists joined with commas.
*/}}
{{- define "social-media-reuploader.envString" -}}
{{- $value := . -}}
{{- if kindIs "bool" $value -}}
{{- ternary "true" "false" $value -}}
{{- else if kindIs "float64" $value -}}
{{- if eq $value (floor $value) -}}
{{- $value | int64 | toString -}}
{{- else -}}
{{- $value | toString -}}
{{- end -}}
{{- else if kindIs "slice" $value -}}
{{- $items := list -}}
{{- range $value -}}
{{- $items = append $items (include "social-media-reuploader.envString" .) -}}
{{- end -}}
{{- join "," $items -}}
{{- else if kindIs "map" $value -}}
{{- fail "env values must be strings, numbers, booleans or lists" -}}
{{- else if kindIs "invalid" $value -}}
{{- "" -}}
{{- else -}}
{{- $value | toString -}}
{{- end -}}
{{- end -}}

{{/* Keys of the env maps that only the GUI reads. */}}
{{- define "social-media-reuploader.isGuiKey" -}}
{{- if or (hasPrefix "OAUTH_" .) (eq . "SESSION_COOKIE_SECURE") -}}true{{- end -}}
{{- end -}}

{{/*
Render an env map as EnvVar list entries (no leading newline).
Arguments (dict):
  values  the map
  source  its name, for error messages ("env" or "gui.env")
  target  "bot" or "gui"
  skip    keys to leave out
Rules: null values are skipped; PORT and REQUESTS_DB are set by the chart;
secrets never go into the pod spec in plain text (BOT_TOKEN / WEBHOOK_SECRET in
`env` are legacy inputs of the bot Secret); OAUTH_* and SESSION_COOKIE_SECURE
are passed to the GUI only (and skipped when empty, so they cannot shadow the
GUI Secret).
*/}}
{{- define "social-media-reuploader.renderEnv" -}}
{{- $managed := dict "PORT" "containerPort / gui.port" "REQUESTS_DB" "db.pvc.mountPath and db.fileName" -}}
{{- $secrets := dict "BOT_TOKEN" "secret.name or secret.botToken" "WEBHOOK_SECRET" "secret.webhookSecretKey or secret.webhookSecret" "ADMIN_TOKEN" "gui.secrets.adminToken or gui.existingSecret" "SECRET_KEY" "gui.secrets.secretKey or gui.existingSecret" "OAUTH_CLIENT_SECRET" "gui.secrets.oauthClientSecret or gui.existingSecret" -}}
{{- $legacyBotInputs := list "BOT_TOKEN" "WEBHOOK_SECRET" -}}
{{- $entries := list -}}
{{- range $key, $value := .values -}}
{{- if and (not (kindIs "invalid" $value)) (not (has $key $.skip)) -}}
{{- $string := include "social-media-reuploader.envString" $value -}}
{{- if hasKey $managed $key -}}
{{- fail (printf "%s.%s is set by the chart: configure it with %s" $.source $key (get $managed $key)) -}}
{{- else if hasKey $secrets $key -}}
{{- if and $string (not (and (eq $.source "env") (has $key $legacyBotInputs))) -}}
{{- fail (printf "%s.%s would put a secret in plain text into the pod spec: use %s" $.source $key (get $secrets $key)) -}}
{{- end -}}
{{- else if include "social-media-reuploader.isGuiKey" $key -}}
{{- if and (eq $.target "gui") (ne $string "") -}}
{{- $entries = append $entries (printf "- name: %s\n  value: %s" $key ($string | quote)) -}}
{{- end -}}
{{- else -}}
{{- $entries = append $entries (printf "- name: %s\n  value: %s" $key ($string | quote)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- join "\n" $entries -}}
{{- end -}}

{{/* Name of the Secret holding the bot token. */}}
{{- define "social-media-reuploader.botSecretName" -}}
{{- .Values.secret.name | default (printf "%s-secret" (include "social-media-reuploader.fullname" .)) -}}
{{- end -}}

{{/* Bot token given as a value (secret.botToken or the legacy env.BOT_TOKEN). */}}
{{- define "social-media-reuploader.botTokenValue" -}}
{{- $env := .Values.env | default dict -}}
{{- .Values.secret.botToken | default (get $env "BOT_TOKEN") | default "" | toString -}}
{{- end -}}

{{- define "social-media-reuploader.webhookSecretValue" -}}
{{- $env := .Values.env | default dict -}}
{{- .Values.secret.webhookSecret | default (get $env "WEBHOOK_SECRET") | default "" | toString -}}
{{- end -}}

{{/* "true" when the chart creates the bot Secret. */}}
{{- define "social-media-reuploader.createBotSecret" -}}
{{- if or .Values.secret.create (and (include "social-media-reuploader.botTokenValue" .) (not .Values.secret.name)) -}}true{{- end -}}
{{- end -}}

{{/* "true" when the bot reads BOT_TOKEN from a Secret. */}}
{{- define "social-media-reuploader.useBotSecret" -}}
{{- if or (include "social-media-reuploader.createBotSecret" .) .Values.secret.name -}}true{{- end -}}
{{- end -}}

{{/* Name of the Secret with the GUI credentials. */}}
{{- define "social-media-reuploader.guiSecretName" -}}
{{- .Values.gui.existingSecret | default .Values.gui.secretName | default (printf "%s-gui-secret" (include "social-media-reuploader.fullname" .)) -}}
{{- end -}}

{{/* "true" when the chart creates the GUI Secret. */}}
{{- define "social-media-reuploader.createGuiSecret" -}}
{{- if and .Values.gui.enabled (not (.Values.gui.existingSecret | default .Values.gui.secretName)) -}}true{{- end -}}
{{- end -}}

{{/* Path of the SQLite DB inside both containers. */}}
{{- define "social-media-reuploader.dbPath" -}}
{{- printf "%s/%s" (.Values.db.pvc.mountPath | default "/app/data" | trimSuffix "/") (.Values.db.fileName | default "requests.db") -}}
{{- end -}}

{{/* Claim name of the DB volume. */}}
{{- define "social-media-reuploader.dbClaimName" -}}
{{- .Values.db.pvc.existingClaim | default (printf "%s-data" (include "social-media-reuploader.fullname" .)) -}}
{{- end -}}

{{/* Effective TMP_DIR of the bot. */}}
{{- define "social-media-reuploader.tmpDir" -}}
{{- $env := .Values.env | default dict -}}
{{- include "social-media-reuploader.envString" (get $env "TMP_DIR" | default "/tmp/telegram_downloader") -}}
{{- end -}}

{{/*
Backend (Service name and port) of a route: "gui" (default) or "bot".
Usage: include "social-media-reuploader.backend" (list $ "gui") | fromYaml
*/}}
{{- define "social-media-reuploader.backend" -}}
{{- $root := index . 0 -}}
{{- $target := index . 1 | default "gui" -}}
{{- if eq $target "gui" -}}
{{- if not $root.Values.gui.enabled -}}
{{- fail "a route targets the GUI (service: gui) but gui.enabled is false" -}}
{{- end -}}
name: {{ include "social-media-reuploader.guiFullname" $root }}
port: {{ $root.Values.gui.port | int64 }}
{{- else if eq $target "bot" -}}
name: {{ include "social-media-reuploader.fullname" $root }}
port: {{ $root.Values.service.port | int64 }}
{{- else -}}
{{- fail (printf "unknown route service %q: use gui or bot" $target) -}}
{{- end -}}
{{- end -}}

{{/* Bot pod affinity: custom, or "prefer the GUI's node" when the GUI runs. */}}
{{- define "social-media-reuploader.botAffinity" -}}
{{- if .Values.affinity -}}
{{- toYaml .Values.affinity -}}
{{- else if and .Values.gui.enabled .Values.gui.colocateWithBot -}}
podAffinity:
  preferredDuringSchedulingIgnoredDuringExecution:
    - weight: 100
      podAffinityTerm:
        labelSelector:
          matchLabels:
            {{- include "social-media-reuploader.selectorLabels" (list . "gui") | nindent 12 }}
        topologyKey: kubernetes.io/hostname
{{- end -}}
{{- end -}}

{{/* GUI pod affinity: custom, or required co-location with the bot. */}}
{{- define "social-media-reuploader.guiAffinity" -}}
{{- if .Values.gui.affinity -}}
{{- toYaml .Values.gui.affinity -}}
{{- else if .Values.gui.colocateWithBot -}}
podAffinity:
  requiredDuringSchedulingIgnoredDuringExecution:
    - labelSelector:
        matchLabels:
          {{- include "social-media-reuploader.selectorLabels" (list . "bot") | nindent 10 }}
      topologyKey: kubernetes.io/hostname
{{- end -}}
{{- end -}}
