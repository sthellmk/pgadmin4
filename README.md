# pgAdmin auf OpenShift

Eigenes Helm-Chart ohne Abhängigkeiten in `charts/pgadmin`. Zielbasis: pgAdmin
**9.18.0**, OpenShift **4.20**, Standard-HAProxy-Ingress-Controller und `restricted-v2`.
Die tatsächliche Cluster-Version wurde nicht ermittelt. Der bereitgestellte
Arbeitsordner war leer, einschließlich Git-Metadaten und Pipeline-Dateien.
Eine bestehende GitLab-Pipeline wurde deshalb nicht verändert.

## Architektur und TLS

Browser → HTTPS/443 → Router → HTTPS/8443 → pgAdmin. Der ClusterIP-Service
veröffentlicht 443 und verweist über `https` auf den Container-Port 8443.
Die Route verwendet `reencrypt` und leitet HTTP auf HTTPS um. Der Service
fordert das TLS-Secret beim Service-CA-Controller an; Helm erstellt keine Secrets.
Das Secret wird als ganzes Verzeichnis read-only nach `/certs` projiziert:
`tls.crt` → `server.cert`, `tls.key` → `server.key`, ohne `subPath`.

`destinationCACertificate` bleibt absichtlich ungesetzt: Der Standard-Router muss
die vom Cluster verwaltete Service CA als Destination-CA verwenden und den
Service-Namen `<service>.<namespace>.svc` prüfen. Das ist die in der
[OpenShift-4.20-Route-API](https://github.com/openshift/api/blob/release-4.20/route/v1/types.go)
vorgesehene Vertrauensstellung. Bei angepassten Routern muss der Clusterbetrieb
diese Voraussetzung bestätigen. Keine abgeschaltete Prüfung, statische CA oder
Helm-`lookup`-Abhängigkeit.

Außen verwendet die Route das Standardzertifikat des Ingress Controllers.
DNS/Route-Hostname müssen zu dessen SAN/Wildcard passen; Browser müssen dessen
Aussteller vertrauen. Die interne Service CA ersetzt dieses Browservertrauen
nicht. Sie gehört ausschließlich zur Verbindung Router → Pod.

Zertifikatsrotation: Kubernetes aktualisiert das projizierte Verzeichnis mit
Verzögerung. Im geprüften Gunicorn-23.0.0-Code erzeugt der
[gthread-Worker](https://github.com/benoitc/gunicorn/blob/23.0.0/gunicorn/workers/gthread.py)
für neue TLS-Verbindungen über
[`ssl_wrap_socket`/`ssl_context`](https://github.com/benoitc/gunicorn/blob/23.0.0/gunicorn/sock.py)
einen Kontext und liest dabei Zertifikat und Schlüssel erneut. Das spricht für
Übernahme nach Volume-Aktualisierung bei neuen Verbindungen; vorhandene
Verbindungen behalten ihren Kontext. Ein praktischer Rotationstest im Cluster
steht aus. Bei abweichendem internem Image/Gunicorn oder ausbleibender Übernahme
ist ein kontrollierter `oc rollout restart` erforderlich. Secret-Änderungen lösen
im Chart keinen automatischen Pod-Neustart aus. Entra- und Bootstrap-Secrets in
Umgebungsvariablen benötigen nach Änderung immer einen Neustart.

## Schreibpfade und restricted-v2

Eine Instanz, `Recreate`, SQLite und persistente Daten unter `/var/lib/pgadmin`.
Nur dieser Pfad (PVC), `/tmp` und `/run/pgadmin` (begrenzte emptyDirs) sind
beschreibbar. HOME liegt auf dem PVC, Bytecode-Schreiben ist deaktiviert,
Dateilogs gehen nach `/dev/null`, Prozesslogs auf stdout/stderr. Postfix ist aus.

Die nicht leere ConfigMap-Datei wird über
`PGADMIN_CUSTOM_CONFIG_DISTRO_FILE=/etc/pgadmin/config_distro.py` eingebunden;
der Entrypoint muss daher keine Konfiguration ins Root-Dateisystem schreiben.
Keine zusätzlichen `PGADMIN_CONFIG_*`-Overrides verwenden: Sie werden bei dieser
Konfigurationsform nicht eingelesen. Grundlage sind die
[Container-Dokumentation 9.18](https://www.pgadmin.org/docs/pgadmin4/9.18/container_deployment.html),
der [Entrypoint 9.18](https://github.com/pgadmin-org/pgadmin4/blob/REL-9_18/pkg/docker/entrypoint.sh)
und das [Dockerfile 9.18](https://github.com/pgadmin-org/pgadmin4/blob/REL-9_18/Dockerfile).
Die Version unterstützt den Interpreter-Wechsel bei entfernten Capabilities.
Kein älteres oder intern verändertes Image ohne erneute Prüfung einsetzen.

Gesetzt sind `runAsNonRoot`, `allowPrivilegeEscalation: false`, Drop ALL,
RuntimeDefault-seccomp, read-only Root und kein ServiceAccount-Token.
UID/GID und fsGroup werden nicht festgelegt. OpenShift muss über restricted-v2
die zulässige UID und Volume-fsGroup zuweisen. Secret und ConfigMap verwenden
0440; die zugewiesene fsGroup muss Gruppen-Lesezugriff vermitteln. Der CSI-Treiber
muss fsGroup und Schreibzugriff auf PVCs unterstützen. Bestehende Daten mit
inkompatiblen Besitzrechten sind vor Nutzung durch den Storagebetrieb zu
migrieren; das Chart verändert keine Besitzrechte mit Root-Containern.

Die Proxy-Einstellungen vertrauen einem Router für X-Forwarded-For und -Proto.
Die Route setzt Forwarded-Header mit `replace`; Host bleibt der originale
Request-Host, Port/Prefix-Header werden nicht vertraut. Direkten Pod-/Servicezugriff
organisatorisch oder durch eine zur Cluster-Netzwerktopologie passende
NetworkPolicy auf vertrauenswürdige Clients begrenzen. Zusätzliche Proxies
erfordern eine Prüfung dieser Vertrauensgrenze. Sichere Session- und Remember-
Cookies sind eingeschaltet; der OIDC-Callback nutzt HTTPS.

## Entra ID und Benutzer

Single-Tenant-App-Registrierung als **Webanwendung**, Authorization Code Flow
mit serverseitigem Client-Secret. Keine SPA-/Public-Client-Konfiguration nötig.
Exakt diese Redirect-URI registrieren:

```text
https://<route-host>/oauth2/authorize
```

Tenantgebundene Discovery, `openid profile email`, vollständige Providerliste
`OAUTH2_CONFIG`; Secret-Laufzeitwert aus `ENTRA_CLIENT_SECRET` per `secretKeyRef`.
TLS-, ID-Token-/Issuer-, Signatur-, Audience- und Nonce-Prüfung erfolgen durch
die native pgAdmin/Authlib-OIDC-Integration und werden nicht überschrieben.
Siehe [pgAdmin OIDC 9.18](https://www.pgadmin.org/docs/pgadmin4/9.18/oauth2.html)
und [Microsoft OIDC](https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc).
Ausgehendes HTTPS zu Entra-Discovery, Token-Endpunkt und JWKS sowie funktionierende
DNS-Auflösung und Uhrzeitsynchronisierung sind erforderlich.

In der Enterprise Application **Zuweisung erforderlich** aktivieren und nur
berechtigte Benutzer/Gruppen zuweisen; Gruppen-Zuweisung kann eine passende
Entra-Lizenz erfordern. Erforderliche Benutzer-/Adminzustimmung gemäß
Tenant-Richtlinie erteilen. Claims (insbesondere stabile Identität und E-Mail)
im Login-Test prüfen. Das Chart führt keine eigene Gruppensynchronisierung aus.

Standard in Test und Prod: `oidc.enabled: false`, ausschließlich interner Login
mit Bootstrap-Admin. Dabei werden kein OIDC-Provider und keine Entra-Secret-Referenz
erzeugt. Zur späteren Aktivierung `oidc.enabled: true` setzen, Entra-Werte und
Secret-Referenz ergänzen und ein Helm-Upgrade ausführen. Mit aktivem OIDC bleibt
der interne Login aus, sofern `oidc.enableInternalLogin` nicht explizit aktiv ist.

`oidc.autoCreateUser` erstellt normale Benutzer,
keine Administratoren. Bei `false` müssen passende OAuth-Benutzer vorher
administrativ angelegt sein. Bootstrap-E-Mail und Passwort-Secret werden trotzdem
für die erste Datenbankinitialisierung benötigt. Der Bootstrap-Admin ist ein
interner Benutzer; `oidc.enableInternalLogin: true` aktiviert den internen
Notfallzugang ausdrücklich. Danach wieder deaktivieren. Bestehende Datenbanken
erhalten durch ein geändertes Bootstrap-Secret kein neues Admin-Passwort.

Bei OIDC kennt pgAdmin kein Login-Passwort zur Verschlüsselung gespeicherter
Datenbankpasswörter. Benutzer müssen deshalb ein separates Master-Passwort setzen
und bei Bedarf eingeben. `MASTER_PASSWORD_REQUIRED` bleibt aktiviert.

## Werte und externe Voraussetzungen

Die zentrale `charts/pgadmin/values.yaml` enthält nur `{}` und keine Vorgaben.
Jede Umgebung besitzt eine vollständige Datei:

- `environments/test/values.yaml`
- `environments/prod/values.yaml`

Immer genau die passende Datei mit `-f` übergeben. Ohne Umgebungswerte schlägt
die Schema-Prüfung absichtlich fehl. Test und Prod in getrennten Namespaces
installieren. Namen, Route und Secret-Referenzen sind getrennt vorbereitet;
Infrastrukturwerte bleiben Platzhalter. Kapazitäten sind Startwerte, keine
abgeleiteten Produktionsanforderungen. Die vollständige
`charts/pgadmin/values.example.yaml` dient nur als zusätzliche Vorlage.
Die gewählte Umgebungsdatei lokal ergänzen und alle
`REPLACE-*` ersetzen. Es sind keine Zugangsdaten enthalten. Vorhandene Secrets
über den etablierten Secret-Prozess bereitstellen; keine Secret-Werte in Helm,
Shell-History oder Logs kopieren.

| Ergänzen/prüfen | Einstellung bzw. Voraussetzung |
| --- | --- |
| Namespace | `--namespace REPLACE-test-namespace`; Berechtigung für Deployment, Service, Route, PVC, ConfigMap |
| Registry/Image-Tag | `image.repository`, `image.tag` (Vorgabe `dpage/pgadmin4:9.18.0` prüfen/freigeben) |
| Image-Pull-Secret | `imagePullSecrets`, vorhandenes Secret im Namespace, bei Bedarf |
| Route und Browservertrauen | `route.host`, DNS, passendes Ingress-Standardzertifikat, vertrauenswürdige CA |
| Backend-TLS | aktiver Service-CA-Controller und Standard-Router-Vertrauensstellung; `tls.servingCertSecretName` frei/stabil halten |
| Storage | `persistence.storageClass`, `size` oder `existingClaim`; fsGroup-fähiger CSI-Treiber, Backup |
| Bootstrap | `bootstrap.email`, `existingSecret`, `passwordKey` |
| Entra-Tenant | `oidc.tenantId`, Single-Tenant-UUID |
| Entra-App | `oidc.clientId`, Web-Redirect-URI wie oben |
| Client-Secret-Referenz | `oidc.existingSecret`, `clientSecretKey`; Secret-Wert statt Secret-ID hinterlegen |
| Entra-Zugriff | Benutzer-/Gruppenzuweisungen, Zustimmung, ggf. Lizenz, ausgehendes HTTPS |

Release-Name und `fullnameOverride` nach Installation stabil halten. Bei
vorhandenen Daten `existingClaim` explizit setzen; nie blind einen neuen Claim
anstelle des alten verwenden. Neu erzeugte PVCs haben `helm.sh/resource-policy:
keep` und bleiben bei Deinstallation erhalten. Wiederverwendung/Helm-Eigentum
nach Deinstallation bewusst über `existingClaim` regeln. StorageClass-Wechsel
und PVC-Verkleinerung sind keine normalen Helm-Upgrades. Backups vor Upgrades;
SQLite-Datenmigrationen sind nicht automatisch durch Helm-Rollback umkehrbar.

## Lokale Prüfung und Installation

Vom Repository-Stamm, mit Helm 3/4 und Python 3. Die Prüfwerte sind ausschließlich
fiktiv. Chart-Rendering braucht weder Netzwerk noch Cluster; nur die erstmalige
Installation des Python-YAML-Parsers benötigt einen Paketindex oder lokale Wheels.

```powershell
python -m pip install -r requirements-validation.txt
python scripts/validate_chart.py
helm lint --strict charts/pgadmin -f environments/test/values.yaml
helm lint --strict charts/pgadmin -f environments/prod/values.yaml
helm template pgadmin-test charts/pgadmin --namespace REPLACE-test-namespace -f environments/test/values.yaml
helm template pgadmin-prod charts/pgadmin --namespace REPLACE-prod-namespace -f environments/prod/values.yaml
# Erst nach Ergänzung der Werte und Review selbst ausführen:
helm upgrade --install pgadmin-test charts/pgadmin --namespace REPLACE-test-namespace -f environments/test/values.yaml --wait --timeout 10m
helm upgrade --install pgadmin-prod charts/pgadmin --namespace REPLACE-prod-namespace -f environments/prod/values.yaml --wait --timeout 10m
```

Nach Installation (Beispiel für Test; für Prod Namespace und Ressourcennamen entsprechend anpassen):

```powershell
oc version
oc -n REPLACE-test-namespace rollout status deployment/pgadmin-test
oc -n REPLACE-test-namespace get pods,pvc,service,route
oc -n REPLACE-test-namespace get route pgadmin-test -o yaml
oc -n REPLACE-test-namespace get pod -l app.kubernetes.io/name=pgadmin -o jsonpath='{.items[0].metadata.annotations.openshift\.io/scc}'
oc -n REPLACE-test-namespace exec deployment/pgadmin-test -- id
oc -n REPLACE-test-namespace exec deployment/pgadmin-test -- ls -ld /var/lib/pgadmin /tmp /run/pgadmin /certs /etc/pgadmin
oc -n REPLACE-test-namespace exec deployment/pgadmin-test -- ls -lL /certs/server.cert /certs/server.key /etc/pgadmin/config_distro.py
curl.exe --fail --show-error https://REPLACE-pgadmin-test.example.invalid/misc/ping
# Nach Secret-Rotation oder nötigem TLS-Neustart:
oc -n REPLACE-test-namespace rollout restart deployment/pgadmin-test
oc -n REPLACE-test-namespace rollout status deployment/pgadmin-test
```

Kein `curl -k`: bei privater Ingress-CA stattdessen `--cacert <CA-Datei>` verwenden.
Keine Secrets mit `oc get secret -o yaml` oder kompletten Prozessumgebungen loggen.

## Monitoring

Da pgAdmin 9.18 keinen dokumentierten nativen Prometheus-Endpunkt bietet, läuft
der [offizielle Blackbox Exporter](https://github.com/prometheus/blackbox_exporter)
als Sidecar (`v0.28.0`, Registry konfigurierbar). Keine Subcharts.
`monitoring.enabled: false` entfernt Exporter, dessen ConfigMaps, Metrics-Service
und ServiceMonitor vollständig. `monitoring.serviceMonitor.enabled: false`
entfernt nur den ServiceMonitor. In Test und Prod sind beide Schalter `true`.

Der ServiceMonitor sammelt `/probe` und `/metrics` am internen ClusterIP-Service
auf Port 9115. Die Probe prüft `https://<service>.<namespace>.svc/misc/ping`
mit Zertifikats- und Hostname-Prüfung gegen die von OpenShift injizierte Service CA.
Anfangs kann sie bis zur CA-/Zertifikatsbereitstellung fehlschlagen. Rotation im
Cluster testen und bei ausbleibender Übernahme das Deployment neu starten.
Siehe [Exporter-Konfiguration](https://github.com/prometheus/blackbox_exporter/blob/v0.28.0/CONFIGURATION.md).

Metriken: `probe_success`, `probe_duration_seconds`,
`probe_ssl_earliest_cert_expiry` sowie Exporter-Prozessmetriken. `up == 1` zeigt
nur die Erreichbarkeit des Exporters; für pgAdmin `probe_success == 1` prüfen.
Keine pgAdmin-Benutzer-/SQL-Metriken oder PostgreSQL-Servermetriken. CPU/RAM
kommen aus dem Cluster-Monitoring. Router, externes DNS, Browserzertifikat und
Login werden von dieser Backend-Probe nicht erfasst.

Voraussetzungen: ServiceMonitor-CRD, aktiviertes OpenShift User-Workload-Monitoring
und Auswahl von Namespace/ServiceMonitor durch Prometheus.
`monitoring.serviceMonitor.labels` bei Bedarf an dessen Selector anpassen.
Siehe [ServiceMonitor-API](https://prometheus-operator.dev/docs/api-reference/api/).
Das Chart aktiviert kein clusterweites Monitoring und erzeugt keine Alerts.
Scrape-Timeout muss über der 5-Sekunden-Probe und höchstens beim Intervall liegen.

Scrapes erfolgen clusterintern über HTTP ohne Zugangsdaten; keine Metrics-Route.
Port 9115 mit einer zur Clusterumgebung passenden NetworkPolicy auf
vertrauenswürdiges Monitoring begrenzen, weil `/probe` weitere Zielparameter
annimmt. Der Exporter benötigt DNS und HTTPS-Zugriff zum pgAdmin-Service.
Registry-Freigabe und Ressourcenbedarf des Exporters berücksichtigen.

```powershell
oc get crd servicemonitors.monitoring.coreos.com
oc -n REPLACE-test-namespace get servicemonitor pgadmin-test -o yaml
oc -n REPLACE-test-namespace logs deployment/pgadmin-test -c blackbox-exporter
# In Prometheus probe_success und probe_ssl_earliest_cert_expiry prüfen.
```

## Prüfstand und konkrete Grenzen

Lokal: striktes Helm-Lint, sieben Render-Varianten (Test, Prod, Beispiel, OIDC aktiv, ServiceMonitor aus,
Monitoring aus, vorhandener PVC mit OIDC/Notfall-Login und Pull-Secret), Prüfung von Portkette,
HTTPS-Probes, TLS-Mount/Rechten, Secret-Referenzen, SecurityContext, OIDC,
PVC-Auswahl und Python-Syntax. `latest` wird abgelehnt.

Noch offen: API-/SCC-Admission im echten Cluster, Image-Pull und Containerstart
mit zufälliger UID und read-only Root, Storage-Rechte, Router-Zertifikatsprüfung,
DNS/Browservertrauen, Zertifikatsrotation, Entra-Login einschließlich Ablehnung
nicht zugewiesener Nutzer, Benutzerrollen, Master-Passwort und Persistenz nach
Neustart. Ohne Docker/Cluster wurde kein Container-Laufzeittest durchgeführt.
HTTPS-Kubelet-Probes prüfen Erreichbarkeit, aber validieren selbst kein
Backend-Zertifikat; der Router muss diese Prüfung leisten.

ServiceMonitor-Discovery, Exporter-Containerstart und echte Scrapes sind ebenfalls
noch im Cluster zu prüfen. Es wurde nichts deployt. Die Einzelinstanz hat bei Neustarts/Upgrades Ausfallzeit.
Ressourcen und Speicher sind Startwerte und müssen anhand realer Last angepasst
werden. Ein späterer Versions-/Imagewechsel verlangt erneute Entrypoint-,
Schreibpfad-, OIDC- und Rotationstests.
