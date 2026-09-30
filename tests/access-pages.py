#!/usr/bin/env python3
"""Verify the actual Nginx configuration returns a readable page with HTTP 403.

Docker is the default; an optional argument selects a local Nginx binary.
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
SOURCE = ROOT / "infrastructure/gateway"
container = next(doc for doc in yaml.safe_load_all((SOURCE / "access-pages.yaml").read_text())
                 if doc["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]
binary = str(Path(sys.argv[1]).resolve()) if len(sys.argv) == 2 else None
if binary:
    version = subprocess.check_output([binary, "-v"], stderr=subprocess.STDOUT, text=True)
    assert container["image"].rsplit(":", 1)[1].split("-", 1)[0] in version

with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
with tempfile.TemporaryDirectory() as workspace:
    root = Path(workspace)
    root.chmod(0o755)
    config = (SOURCE / "pages/nginx.conf").read_text().replace("listen 8080;", f"listen 127.0.0.1:{port};")
    config = config.replace("/etc/sso-pages", str(root))
    if binary:
        config = config.replace("/tmp/", str(root) + "/")
        config = config.replace("/dev/stderr", "stderr").replace("/dev/stdout", str(root / "access.log"))
    (root / "nginx.conf").write_text(config)
    (root / "403.html").write_text((SOURCE / "pages/403.html").read_text())
    name = f"sso-pages-{os.getpid()}"
    command = [binary, "-e", "stderr", "-p", str(root)] if binary else [
        "docker", "run", "--rm", "--name", name, "--network=host", "--read-only",
        "--user=101:101", "--cap-drop=ALL", "--security-opt=no-new-privileges", "--tmpfs=/tmp",
        "-v", f"{root}:{root}:ro", "--entrypoint=nginx", container["image"]]
    # Root in a restricted local workspace may not be able to switch to nobody.
    # CI still verifies the actual container as the deployment's unprivileged UID.
    global_options = "daemon off;"
    if binary and os.geteuid() == 0:
        global_options += " user root;"
    command += ["-c", str(root / "nginx.conf"), "-g", global_options]
    with tempfile.TemporaryFile(mode="w+") as logs:
        process = subprocess.Popen(command, stdout=logs, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while True:
                try:
                    with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=0.5) as response:
                        assert response.status == 200
                    break
                except urllib.error.URLError:
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise
                    time.sleep(0.1)
            for path, expected in [("/sso/access-denied", 403), ("/403.html", 404),
                                   ("/nginx.conf", 404), ("/if/admin/", 404)]:
                try:
                    response = opener.open(f"http://127.0.0.1:{port}{path}", timeout=2)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    body = response.read().decode()
                    assert response.status == expected, (path, response.status)
                    if expected == 403:
                        assert "missing a required access group" in body
                        assert "not a connection failure" in body
                        assert "text/html" in response.headers["Content-Type"]
                        assert response.headers["Cache-Control"] == "no-store"
                        assert "default-src 'none'" in response.headers["Content-Security-Policy"]
        except Exception:
            logs.seek(0)
            print(logs.read(), file=sys.stderr)
            raise
        finally:
            if not binary:
                subprocess.run(["docker", "stop", "--time=5", name], check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)

print("Nginx serves the permission page as HTTP 403, with no application proxy or config exposure.")
