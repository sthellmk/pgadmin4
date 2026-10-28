{{- define "pgadmin.name" -}}
{{- default (printf "%s-pgadmin" .Release.Name) .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- define "pgadmin.selector" -}}
app.kubernetes.io/name: pgadmin
app.kubernetes.io/instance: {{ .Release.Name | quote }}
{{- end -}}
