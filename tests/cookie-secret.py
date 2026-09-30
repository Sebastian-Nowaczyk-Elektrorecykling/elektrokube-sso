#!/usr/bin/env python3
"""Check the manifest's generated key format with the pinned upstream validator.

Uses Docker by default. Pass an oauth2-proxy binary path for an offline local check.
Only synthetic test values are used; no cluster credentials are read.
"""

import base64
import os
from pathlib import Path
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
PREFIX = "secret-generator.v1.mittwald.de/"


def documents(path):
    return list(yaml.safe_load_all((ROOT / path).read_text()))


deployment = next(
    item for item in documents("infrastructure/oauth2-proxy/workload.yaml")
    if item["kind"] == "Deployment"
)
container = deployment["spec"]["template"]["spec"]["containers"][0]
reference = next(
    item["valueFrom"]["secretKeyRef"] for item in container["env"]
    if item["name"] == "OAUTH2_PROXY_COOKIE_SECRET"
)
secret = next(
    item for item in documents("infrastructure/credentials/secrets.yaml")
    if item["metadata"]["name"] == reference["name"]
)
annotations = secret["metadata"]["annotations"]
assert reference["key"] in annotations[PREFIX + "autogenerate"].split(",")
assert annotations[PREFIX + "length"] == "32B", "Cookie key must have 32 random bytes"
encoder = {"base64": base64.b64encode, "base64url": base64.urlsafe_b64encode}[
    annotations[PREFIX + "encoding"]
]

image = container["image"]
if len(sys.argv) == 2:
    command = [str(Path(sys.argv[1]).resolve())]
    version = subprocess.check_output(command + ["--version"], text=True)
    assert image.rsplit(":", 1)[1] in version, "Use the manifest's oauth2-proxy version"
else:
    command = ["docker", "run", "--rm", "--network=none",
               "-e", "OAUTH2_PROXY_COOKIE_SECRET", image]

# Isolate cookie validation from OIDC discovery and the live cluster.
command += ["--config-test", "--provider=google", "--client-id=validation",
            "--client-secret=validation", "--email-domain=*", "--upstream=static://202",
            "--redirect-url=https://validation.invalid/oauth2/callback"]


def validate(value):
    environment = {k: v for k, v in os.environ.items() if not k.startswith("OAUTH2_PROXY_")}
    environment["OAUTH2_PROXY_COOKIE_SECRET"] = value.decode("ascii")
    return subprocess.run(command, env=environment, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=120)


# Force both '+' and '/' in standard Base64: random tests can miss this regression.
raw = bytes([251, 255]) * 16
broken = validate(base64.b64encode(raw))
assert broken.returncode != 0 and "but is 44 bytes" in broken.stdout, broken.stdout
result = validate(encoder(raw))
assert result.returncode == 0, result.stdout
print("Cookie key accepted by pinned oauth2-proxy; standard-Base64 regression reproduced.")
