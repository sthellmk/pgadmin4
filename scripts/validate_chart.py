"""Offline checks after installing requirements-validation.txt; no cluster access."""
import ast
import os
from pathlib import Path
import subprocess
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / 'charts' / 'pgadmin'

def render(*args):
    result = subprocess.run(
        ['helm', 'template', 'fictitious-test', str(CHART), '--namespace',
         'fictitious-test', *args], check=True, capture_output=True, text=True)
    return {(d['kind'], d['metadata']['name']): d
            for d in yaml.safe_load_all(result.stdout) if d}

TEST = ROOT / 'environments/test/values.yaml'
PROD = ROOT / 'environments/prod/values.yaml'
assert yaml.safe_load((CHART / 'values.yaml').read_text()) == {}
for values in [TEST, PROD, CHART / 'values.example.yaml']:
    subprocess.run(['helm', 'lint', '--strict', str(CHART), '-f', str(values)], check=True)
variants = [(['-f', str(TEST)], 'pgadmin-test'),
            (['-f', str(PROD)], 'pgadmin-prod'),
            (['-f', str(CHART / 'values.example.yaml')], 'pgadmin'),
            (['-f', str(TEST), '--set', 'oidc.enabled=true'], 'pgadmin-test'),
            (['-f', str(TEST), '--set', 'monitoring.serviceMonitor.enabled=false'], 'pgadmin-test'),
            (['-f', str(TEST), '--set', 'monitoring.enabled=false'], 'pgadmin-test'),
            (['-f', str(TEST), '--set', 'oidc.enabled=true',
              '--set', 'persistence.existingClaim=fictitious-existing-pvc',
              '--set', 'oidc.enableInternalLogin=true',
              '--set', 'oidc.autoCreateUser=false',
              '--set', 'imagePullSecrets[0].name=fictitious-pull-secret'], 'pgadmin-test')]
for args, name in variants:
    docs = render(*args)
    assert not any(kind == 'Secret' for kind, _ in docs)
    dep = docs['Deployment', name]['spec']
    assert dep['replicas'] == 1 and dep['strategy']['type'] == 'Recreate'
    pod = dep['template']['spec']
    assert pod['automountServiceAccountToken'] is False
    assert pod['securityContext'] == {
        'runAsNonRoot': True, 'seccompProfile': {'type': 'RuntimeDefault'}}
    container = next(c for c in pod['containers'] if c['name'] == 'pgadmin')
    assert container['securityContext'] == {
        'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
        'capabilities': {'drop': ['ALL']}}
    assert container['ports'] == [{'name': 'https', 'containerPort': 8443}]
    for probe in ['startupProbe', 'readinessProbe', 'livenessProbe']:
        assert container[probe]['httpGet'] == {
            'scheme': 'HTTPS', 'path': '/misc/ping', 'port': 'https'}
    env = {e['name']: e for e in container['env']}
    assert env['PGADMIN_LISTEN_PORT']['value'] == '8443'
    assert env['PGADMIN_ENABLE_TLS']['value'] == 'True'
    assert env['PGADMIN_DISABLE_POSTFIX']['value'] == 'True'
    oidc_enabled = 'oidc.enabled=true' in args
    assert ('ENTRA_CLIENT_SECRET' in env) == oidc_enabled
    for secret_env in ['PGADMIN_DEFAULT_PASSWORD'] + (['ENTRA_CLIENT_SECRET'] if oidc_enabled else []):
        assert set(env[secret_env]) == {'name', 'valueFrom'}
        assert set(env[secret_env]['valueFrom']['secretKeyRef']) == {'name', 'key'}
    volumes = {v['name']: v for v in pod['volumes']}
    assert volumes['tls']['secret']['defaultMode'] == 0o440
    assert volumes['config']['configMap']['defaultMode'] == 0o440
    assert volumes['tls']['secret']['items'] == [
        {'key': 'tls.crt', 'path': 'server.cert'},
        {'key': 'tls.key', 'path': 'server.key'}]
    mount = next(m for m in container['volumeMounts'] if m['name'] == 'tls')
    assert mount == {'name': 'tls', 'mountPath': '/certs', 'readOnly': True}
    service = docs['Service', name]
    assert service['spec']['ports'] == [
        {'name': 'https', 'port': 443, 'targetPort': 'https'}]
    assert service['metadata']['annotations'][
        'service.beta.openshift.io/serving-cert-secret-name'] == volumes['tls']['secret']['secretName']
    route = docs['Route', name]['spec']
    assert route['tls'] == {
        'termination': 'reencrypt', 'insecureEdgeTerminationPolicy': 'Redirect'}
    assert route['to']['name'] == service['metadata']['name']
    assert route['port']['targetPort'] == 'https'
    source = docs['ConfigMap', name]['data']['config_distro.py']
    ast.parse(source)
    os.environ.pop('ENTRA_CLIENT_SECRET', None)
    if oidc_enabled:
        os.environ['ENTRA_CLIENT_SECRET'] = 'FICTITIOUS-VALIDATION-ONLY'
    config = {}
    exec(compile(source, '<rendered-config>', 'exec'), config)
    if oidc_enabled:
        provider, = config['OAUTH2_CONFIG']
        assert provider['OAUTH2_CLIENT_SECRET'] == os.environ['ENTRA_CLIENT_SECRET']
        assert provider['OAUTH2_SSL_CERT_VERIFICATION'] is True
        assert provider['OAUTH2_SCOPE'] == 'openid profile email'
        assert provider['OAUTH2_RESPONSE_TYPE'] == 'code'
    else:
        assert config['OAUTH2_CONFIG'] == []
        assert config['AUTHENTICATION_SOURCES'] == ['internal']
    if args and 'persistence.existingClaim=fictitious-existing-pvc' in args:
        assert ('PersistentVolumeClaim', name) not in docs
        assert config['AUTHENTICATION_SOURCES'] == ['oauth2', 'internal']
        assert config['OAUTH2_AUTO_CREATE_USER'] is False
        assert volumes['data']['persistentVolumeClaim']['claimName'] == 'fictitious-existing-pvc'
        assert pod['imagePullSecrets'] == [{'name': 'fictitious-pull-secret'}]
    else:
        assert config['AUTHENTICATION_SOURCES'] == (['oauth2'] if oidc_enabled else ['internal'])
        assert docs['PersistentVolumeClaim', name]['metadata']['annotations']['helm.sh/resource-policy'] == 'keep'

    monitoring = 'monitoring.enabled=false' not in args
    assert (('Service', name + '-metrics') in docs) == monitoring
    assert (len(pod['containers']) == 2) == monitoring
    monitor_enabled = monitoring and 'monitoring.serviceMonitor.enabled=false' not in args
    assert (('ServiceMonitor', name) in docs) == monitor_enabled
    if monitoring:
        exporter = next(c for c in pod['containers'] if c['name'] == 'blackbox-exporter')
        assert exporter['securityContext'] == container['securityContext']
        assert docs['Service', name + '-metrics']['spec']['selector'] == service['spec']['selector']
        module = yaml.safe_load(docs['ConfigMap', name + '-exporter']['data']['blackbox.yaml'])['modules']['pgadmin_https']
        assert module['http']['tls_config'] == {
            'ca_file': '/etc/service-ca/service-ca.crt', 'insecure_skip_verify': False}
        assert docs['ConfigMap', name + '-service-ca']['metadata']['annotations'][
            'service.beta.openshift.io/inject-cabundle'] == 'true'
    if monitor_enabled:
        monitor = docs['ServiceMonitor', name]['spec']
        labels = docs['Service', name + '-metrics']['metadata']['labels']
        assert all(labels[k] == v for k, v in monitor['selector']['matchLabels'].items())
        probe, metrics = monitor['endpoints']
        assert probe['params']['target'] == [f'https://{name}.fictitious-test.svc/misc/ping']
        assert probe['path'] == '/probe' and metrics['path'] == '/metrics'
        assert probe['port'] == metrics['port'] == 'metrics'

bad = subprocess.run(['helm', 'template', 'test', str(CHART), '-f', str(TEST), '--set',
                      'image.tag=latest'], capture_output=True)
assert bad.returncode != 0
missing = subprocess.run(['helm', 'template', 'test', str(CHART)], capture_output=True)
assert missing.returncode != 0
print('PASS: seven variants; Test/Prod, OIDC on/off, monitoring on/off, TLS, secrets, security, PVC, Python syntax')
