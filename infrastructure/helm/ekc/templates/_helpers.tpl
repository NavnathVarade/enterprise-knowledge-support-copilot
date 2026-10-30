{{- define "ekc.fullname" -}}
{{- default .Release.Name .Values.nameOverride | trunc 40 | trimSuffix "-" -}}
{{- end -}}

{{- define "ekc.labels" -}}
app.kubernetes.io/part-of: enterprise-knowledge-copilot
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/instance: {{ .Release.Name }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "ekc.image" -}}
{{- $tag := required "image.tag is required" .Values.image.tag -}}
{{- if eq $tag "latest" }}{{ fail "image.tag must be immutable, not 'latest'" }}{{ end -}}
{{- printf "%s:%s" (required "image.repository is required" .Values.image.repository) $tag -}}
{{- end -}}

{{- define "ekc.webImage" -}}
{{- $tag := required "web.image.tag is required" .Values.web.image.tag -}}
{{- if eq $tag "latest" }}{{ fail "web.image.tag must be immutable, not 'latest'" }}{{ end -}}
{{- printf "%s:%s" (required "web.image.repository is required" .Values.web.image.repository) $tag -}}
{{- end -}}

{{/* Pod-level hardening shared by every workload (matches the Kustomize base). */}}
{{- define "ekc.podSecurity" -}}
serviceAccountName: {{ include "ekc.fullname" . }}
automountServiceAccountToken: false
{{- with .Values.image.pullSecrets }}
imagePullSecrets: {{ toYaml . | nindent 2 }}
{{- end }}
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  fsGroup: 10001
  seccompProfile: {type: RuntimeDefault}
{{- end -}}

{{- define "ekc.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities: {drop: [ALL]}
{{- end -}}

{{- define "ekc.envFrom" -}}
envFrom:
  - configMapRef: {name: {{ include "ekc.fullname" . }}-config}
  - secretRef: {name: {{ .Values.secrets.appSecretName }}}
{{- end -}}

{{- define "ekc.volumes" -}}
- name: tmp
  emptyDir: {sizeLimit: 128Mi}
- name: kafka-ca
  secret: {secretName: {{ .Values.secrets.kafkaCaSecretName }}, optional: false}
{{- end -}}

{{- define "ekc.antiAffinity" -}}
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          topologyKey: kubernetes.io/hostname
          labelSelector:
            matchLabels: {app.kubernetes.io/name: {{ .name }}}
{{- end -}}

{{- define "ekc.workerProbes" -}}
startupProbe:
  exec: {command: ["python", "-m", "packages.workers.health", "--max-age", "900"]}
  periodSeconds: 5
  timeoutSeconds: 10
  failureThreshold: 36
livenessProbe:
  exec: {command: ["python", "-m", "packages.workers.health", "--max-age", "900"]}
  periodSeconds: 30
  timeoutSeconds: 10
  failureThreshold: 3
readinessProbe:
  exec: {command: ["python", "-m", "packages.workers.health", "--max-age", "900"]}
  periodSeconds: 15
  timeoutSeconds: 10
  failureThreshold: 3
{{- end -}}
