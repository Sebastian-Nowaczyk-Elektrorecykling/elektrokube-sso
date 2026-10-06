#!/usr/bin/env python3
"""Check the declarative authentication boundary without accessing a cluster."""
from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[1]


def docs(path):
    return [d for d in yaml.safe_load_all((ROOT / path).read_text()) if d]


def check():
    stages = {d['metadata']['name']: d['spec'] for d in docs('clusters/elektrokube/infrastructure.yaml')}
    assert stages['sso-foundation']['prune'] is True, 'Deleted allow policies must be revoked'
    assert stages['sso-foundation']['deletionPolicy'] == 'Orphan'
    policies = docs('infrastructure/foundation/network-policies.yaml')
    baseline = next(d for d in policies if d['metadata']['name'] == 'sso-default-deny-ingress')
    assert baseline['kind'] == 'NetworkPolicy' and baseline['metadata']['namespace'] == 'sso'
    assert baseline['spec'] == {'podSelector': {}, 'policyTypes': ['Ingress'], 'ingress': []}, \
        'Revoking an allow must leave the default-deny baseline in force'
    for namespace in docs('infrastructure/foundation/namespaces.yaml'):
        assert namespace['metadata']['annotations']['kustomize.toolkit.fluxcd.io/prune'] == 'disabled', \
            'Policy pruning must never cascade through a Namespace deletion'
    for secret in docs('infrastructure/credentials/secrets.yaml'):
        annotations = secret['metadata']['annotations']
        assert annotations['kustomize.toolkit.fluxcd.io/ssa'] == 'IfNotPresent'
        assert annotations['kustomize.toolkit.fluxcd.io/prune'] == 'disabled'
        assert not secret.get('data') and not secret.get('stringData'), 'Credentials must be generated'

    routes = docs('infrastructure/routes/routes.yaml')
    protected_hosts = {host for r in routes for host in r['spec']['hostnames']}
    certificate = next(d for d in docs('infrastructure/certificates/certificates.yaml')
                       if d['metadata']['name'] == 'sso-gateway')
    assert protected_hosts <= set(certificate['spec']['dnsNames'])
    blueprint = yaml.load((ROOT / 'infrastructure/authentik/blueprint.yaml').read_text(), Loader=yaml.BaseLoader)
    provider = next(e['attrs'] for e in blueprint['entries'] if e['model'] == 'authentik_providers_oauth2.oauth2provider')
    callbacks = {r['url'] for r in provider['redirect_uris'] if r['matching_mode'] == 'strict'}
    assert {f'https://{h}/oauth2/callback' for h in protected_hosts} <= callbacks
    deployment = next(d for d in docs('infrastructure/oauth2-proxy/workload.yaml') if d['kind'] == 'Deployment')
    args = deployment['spec']['template']['spec']['containers'][0]['args']
    for flag in ('--cookie-name=__Host-elektrokube_sso', '--cookie-path=/', '--cookie-secure=true',
                 '--cookie-httponly=true', '--code-challenge-method=S256',
                 '--bearer-token-login-fallback=false', '--skip-auth-strip-headers=false'):
        assert flag in args, flag
    assert not any(a.startswith(('--cookie-domain=', '--insecure-oidc-', '--ssl-insecure-skip-verify=true',
                                 '--skip-auth-route=', '--skip-auth-regex=', '--trusted-ip=')) for a in args)
    assert all(f'--whitelist-domain={host}' in args for host in protected_hosts)

    rules = yaml.safe_load((ROOT / 'infrastructure/heimdall/rules.yaml').read_text())['rules']
    rule_hosts = set()
    for rule in rules:
        assert rule['match']['scheme'] == 'https'
        for host in rule['match']['hosts']:
            assert host['type'] == 'exact'
            rule_hosts.add(host['value'])
            if host['value'].endswith('.admin.internal'):
                authz = next(s for s in rule['execute'] if s.get('authorizer') == 'service-group')
                assert authz['config']['values']['group'] == '', 'Admin hosts cannot admit an ordinary group'
    assert protected_hosts <= rule_hosts

    for route in routes:
        assert route['spec']['parentRefs'] == [{'name': 'lan-gateway', 'sectionName': 'https'}]
        for rule in route['spec']['rules']:
            if not rule.get('backendRefs'):
                continue
            filters = rule.get('filters', [])
            if all(b['name'] == 'oauth2-proxy' for b in rule['backendRefs']):
                assert rule['matches'] == [{'path': {'type': 'PathPrefix', 'value': '/oauth2/'}}]
                continue
            auth = next((f['externalAuth'] for f in filters if f['type'] == 'ExternalAuth'), None)
            assert auth and auth['protocol'] == 'GRPC' and auth['backendRef'] == {'name': 'heimdall-authz', 'port': 4456}
            assert not auth.get('forwardBody'), 'Authentication must not receive application bodies'
            headers = next(f['requestHeaderModifier'] for f in filters if f['type'] == 'RequestHeaderModifier')
            assert {'X-Auth-Request-Groups', 'X-Forwarded-Groups', 'Remote-Groups', 'Forwarded'} <= set(headers['remove'])

    # Review every deliberate public IdP path explicitly. A new broad exemption
    # must not silently expose administration APIs or an application backend.
    login = next(d for d in docs('infrastructure/gateway/login-routes.yaml') if d['metadata']['name'] == 'sso-login')
    expected = {
        ('Exact', '/'), ('Exact', '/sso/access-denied'), ('PathPrefix', '/application/o/'),
        ('PathPrefix', '/if/flow/'), ('PathPrefix', '/api/v3/flows/executor/'),
        ('PathPrefix', '/static/'), ('PathPrefix', '/media/'), ('Exact', '/api/v3/root/config/'),
        ('Exact', '/api/v3/core/brands/current/'), ('Exact', '/api/v3/core/users/me/'),
        ('PathPrefix', '/oauth2/'),
    }
    actual = {(m['path']['type'], m['path']['value']) for r in login['spec']['rules'] for m in r.get('matches', [])}
    assert actual == expected, 'Review changes to the public authentication surface'
    assert login['spec']['hostnames'] == ['auth.internal']
    gateway = docs('infrastructure/gateway/gateway.yaml')[0]
    assert all(l['allowedRoutes']['namespaces']['from'] == 'Same' for l in gateway['spec']['listeners'])
    print('Credential retention, policy revocation, routes, callbacks, TLS names and authorization boundaries pass.')


if __name__ == '__main__':
    check()
