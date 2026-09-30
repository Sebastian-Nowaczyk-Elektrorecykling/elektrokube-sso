#!/usr/bin/env python3
"""Start the pinned Heimdall with Kubernetes-style projected ConfigMap volumes.

Uses Docker by default. Pass a Heimdall binary path for an offline local check.
The real deployment arguments and rules are used; test listeners bind to loopback.
"""

import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import yaml


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "infrastructure/heimdall"
deployment = next(item for item in yaml.safe_load_all((SOURCE / "workload.yaml").read_text())
                  if item["kind"] == "Deployment")
container = deployment["spec"]["template"]["spec"]["containers"][0]
mounts = {item["name"]: Path(item["mountPath"]) for item in container["volumeMounts"]}
image = container["image"]
binary = str(Path(sys.argv[1]).resolve()) if len(sys.argv) == 2 else None
if binary:
    version = subprocess.check_output([binary, "--version"], text=True)
    assert image.rsplit(":", 1)[1] in version, "Use the manifest's Heimdall version"


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def project(directory, name, contents):
    # Kubernetes publishes each key through ..data, which links to a directory.
    revision = directory / "..2026_09_30_00_00_00.000000000"
    revision.mkdir(parents=True)
    directory.chmod(0o755)
    revision.chmod(0o755)
    (revision / name).write_text(contents)
    (revision / name).chmod(0o644)
    (directory / "..data").symlink_to(revision.name, target_is_directory=True)
    (directory / name).symlink_to(Path("..data") / name)


with tempfile.TemporaryDirectory() as workspace:
    root = Path(workspace)
    root.chmod(0o755)
    config = yaml.safe_load((SOURCE / "config.yaml").read_text())
    probes = [container[kind]["httpGet"] for kind in ("readinessProbe", "livenessProbe")]
    ports = {item["name"]: item["containerPort"] for item in container["ports"]}
    for check in probes:
        assert ports.get(check["port"], check["port"]) == config["management"]["port"]
    for section in ("serve", "management"):
        config[section].update(host="127.0.0.1", port=free_port())

    arguments = container["args"].copy()
    name = f"heimdall-startup-{os.getpid()}"
    if binary:
        # Preserve the configured source's relative path, including a directory source.
        relative = Path(config["providers"]["file_system"]["src"]).relative_to(mounts["rules"])
        config["providers"]["file_system"]["src"] = str(root / "rules" / relative)
        arguments = [f"--config={root / 'config/config.yaml'}" if arg.startswith("--config=")
                     else arg for arg in arguments]
        command = [binary]
    else:
        command = ["docker", "run", "--rm", "--name", name, "--network=host",
                   "--read-only", "--user=65532:65532", "--cap-drop=ALL",
                   "--security-opt=no-new-privileges",
                   "-v", f"{root / 'config'}:{mounts['config']}:ro",
                   "-v", f"{root / 'rules'}:{mounts['rules']}:ro", image]

    project(root / "config", "config.yaml", yaml.safe_dump(config, sort_keys=False))
    project(root / "rules", "rules.yaml", (SOURCE / "rules.yaml").read_text())
    environment = {k: v for k, v in os.environ.items() if not k.startswith("HEIMDALL_")}
    health = [f"http://127.0.0.1:{config['management']['port']}{path}"
              for path in {check["path"] for check in probes}]
    probe = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with tempfile.TemporaryFile(mode="w+") as logs:
        process = subprocess.Popen(command + arguments, env=environment, stdout=logs,
                                   stderr=subprocess.STDOUT)
        ready = False
        failure = ""
        try:
            deadline = time.monotonic() + 30
            while process.poll() is None and time.monotonic() < deadline:
                try:
                    for endpoint in health:
                        with probe.open(endpoint, timeout=0.5) as response:
                            assert response.status == 200, response.status
                    ready = True
                    break
                except urllib.error.HTTPError as error:
                    failure = str(error)
                    if 400 <= error.code < 500:
                        break
                except (urllib.error.URLError, TimeoutError):
                    pass
                time.sleep(0.1)
        finally:
            if not binary:
                subprocess.run(["docker", "stop", "--time=5", name], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        logs.seek(0)
        output = logs.read()
        assert ready, f"Heimdall did not become healthy with a projected ConfigMap: {failure}\n{output}"
        assert "New rule set received" in output, output
        assert "failed to parse rule set" not in output, output

print("Pinned Heimdall loads the projected rules file and becomes healthy.")
