#!/usr/bin/env python3
"""Render pinned charts and validate every manifest against Kubernetes/CRD schemas.

Requires Python/PyYAML, Helm, Kustomize and kubeconform. No cluster is contacted.
Cache contents contain synthetic chart-generated credentials, never live secrets.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import yaml

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / '.cache/validation'
SCHEMAS = CACHE / 'schemas'
KUBE_VERSION = '1.36.4'  # Also exercises the oldest pinned bootstrap patch in use.


def docs(text):
    return [d for d in yaml.safe_load_all(text) if d]


def run(*args):
    return subprocess.check_output(args, text=True, cwd=ROOT)


def fetch(url, filename, digest=None):
    path = CACHE / filename
    if not path.exists():
        with urllib.request.urlopen(url, timeout=60) as response:
            path.write_bytes(response.read())
    data = path.read_bytes()
    if digest:
        assert hashlib.sha256(data).hexdigest() == digest, f'Checksum mismatch: {url}'
    return data.decode()


def schemas(resources):
    for doc in resources:
        if doc['kind'] != 'CustomResourceDefinition':
            continue
        spec = doc['spec']
        for version in spec['versions']:
            schema = version.get('schema', {}).get('openAPIV3Schema')
            if schema:
                name = f"{spec['group']}_{spec['names']['kind'].lower()}_{version['name']}.json"
                (SCHEMAS / name).write_text(json.dumps(schema))


def main():
    os.umask(0o077)
    SCHEMAS.mkdir(parents=True, exist_ok=True)
    resources = {}
    for path in sorted(ROOT.rglob('kustomization.yaml')):
        if any(p.startswith('.') for p in path.relative_to(ROOT).parts):
            continue
        rendered = run('kustomize', 'build', str(path.parent)).replace('${API_IP}', '192.0.2.10')
        assert '${' not in rendered, 'Unexpected unresolved substitution'
        for d in docs(rendered):
            key = (d['apiVersion'], d['kind'], d['metadata'].get('namespace'), d['metadata']['name'])
            assert key not in resources or resources[key] == d, f'Conflicting resource {key}'
            resources[key] = d
    manifests = list(resources.values())
    sources = {d['metadata']['name']: d['spec']['url'] for d in manifests if d['kind'] == 'HelmRepository'}
    for release in [d for d in manifests if d['kind'] == 'HelmRelease']:
        spec = release['spec']; chart = spec['chart']['spec']; name = release['metadata']['name']
        archive = CACHE / f"{chart['chart']}-{chart['version']}.tgz"
        if not archive.exists():
            run('helm', 'pull', chart['chart'], '--version', chart['version'], '--repo',
                sources[chart['sourceRef']['name']], '--destination', str(CACHE))
        values = CACHE / f'{name}-values.yaml'
        values.write_text(yaml.safe_dump(spec.get('values', {})))
        rendered = docs(run('helm', 'template', spec['releaseName'], str(archive), '--namespace',
                            release['metadata']['namespace'], '--kube-version', KUBE_VERSION,
                            '--include-crds', '--values', str(values)))
        if name == 'sso-secret-generator':
            assert not any(d['kind'] in ('ClusterRole', 'ClusterRoleBinding') for d in rendered), \
                'Secret generator must remain scoped to sso'
        if name == 'authentik':
            workloads = [d for d in rendered if d['kind'] == 'Deployment']
            assert len(workloads) == 2
            assert all(d['spec']['template']['spec'].get('automountServiceAccountToken') is False for d in workloads)
        manifests.extend(rendered)
        print(f'Rendered {name}: {len(rendered)} resources', flush=True)
    schemas(manifests)
    schemas(docs(fetch('https://github.com/fluxcd/flux2/releases/download/v2.9.6/install.yaml', 'flux-v2.9.6.yaml',
                       '9c1fda7e401429531ed1478f67ba06b3edff6513b75da7ee47bde9f1c4d4251c')))
    schemas(docs(fetch('https://github.com/kubernetes-sigs/gateway-api/releases/download/v1.6.2/experimental-install.yaml',
                       'gateway-v1.6.2.yaml', 'fb03263a4a720cc7abe9b36ede6e308b558d2d725a39595d664f34fe37f9c503')))
    schemas(docs(fetch('https://raw.githubusercontent.com/cilium/cilium/v1.20.2/pkg/k8s/apis/cilium.io/client/crds/v2/ciliumnetworkpolicies.yaml',
                       'cilium-v1.20.2.yaml')))
    cnpg = CACHE / 'cloudnative-pg-0.29.1.tgz'
    if not cnpg.exists():
        run('helm', 'pull', 'cloudnative-pg', '--repo', 'https://cloudnative-pg.github.io/charts',
            '--version', '0.29.1', '--destination', str(CACHE))
    schemas(docs(run('helm', 'template', 'schema-only', str(cnpg), '--include-crds', '--kube-version', KUBE_VERSION)))
    api = json.loads(fetch(f'https://raw.githubusercontent.com/kubernetes/kubernetes/v{KUBE_VERSION}/api/openapi-spec/v3/apis__apiextensions.k8s.io__v1_openapi.json',
                           f'crd-openapi-{KUBE_VERSION}.json'))
    definitions = json.loads(json.dumps(api['components']['schemas']).replace('#/components/schemas/', '#/definitions/'))
    (SCHEMAS / 'apiextensions.k8s.io_customresourcedefinition_v1.json').write_text(json.dumps({
        '$ref': '#/definitions/io.k8s.apiextensions-apiserver.pkg.apis.apiextensions.v1.CustomResourceDefinition',
        'definitions': definitions}))
    target = CACHE / 'all.yaml'
    target.write_text(yaml.safe_dump_all(manifests, sort_keys=False))
    print(run('kubeconform', '-strict', '-summary', '-kubernetes-version', KUBE_VERSION,
              '-schema-location', str(SCHEMAS / '{{.Group}}_{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'),
              '-schema-location', 'default', str(target)), end='')


if __name__ == '__main__':
    main()
