{{- define "ionshield.name" -}}
{{- .Chart.Name -}}
{{- end -}}

{{- define "ionshield.labels" -}}
app.kubernetes.io/name: {{ include "ionshield.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "ionshield.selectorLabels" -}}
app: {{ include "ionshield.name" . }}
{{- end -}}
