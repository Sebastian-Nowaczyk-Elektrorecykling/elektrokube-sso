#!/usr/bin/env python3
"""Exercise the shipped rules with real Heimdall and synthetic identity responses.

No test server is deployed. Docker is the default; an optional argument selects a
local Heimdall binary. Cilium's live network-policy datapath is outside this test.
"""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import grpc
import yaml
from envoy.service.auth.v3 import external_auth_pb2, external_auth_pb2_grpc

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "infrastructure/heimdall"
container = next(doc for doc in yaml.safe_load_all((SOURCE / "workload.yaml").read_text())
                 if doc["kind"] == "Deployment")["spec"]["template"]["spec"]["containers"][0]
binary = str(Path(sys.argv[1]).resolve()) if len(sys.argv) == 2 else None
if binary:
    assert container["image"].rsplit(":", 1)[1] in subprocess.check_output(
        [binary, "--version"], text=True)


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


class Identity(BaseHTTPRequestHandler):
    def do_GET(self):
        name = self.headers.get("Cookie", "").removeprefix("__Host-elektrokube_sso=")
        if self.headers.get("Authorization"):
            name = self.headers["Authorization"].removeprefix("Bearer ")
        groups = {"ordinary": ["sso-users"], "admin": ["cluster-admins"],
                  "bootstrap": ["authentik Admins"], "unrelated": ["other-service"],
                  "empty": [], "malformed": "cluster-admins"}
        self.send_response(200 if name in groups else 401)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"user": name, "groups": groups.get(name, [])}).encode())

    def log_message(self, *args):
        pass


# These endpoints must be available before any external authorization succeeds.
login = next(doc for doc in yaml.safe_load_all(
    (ROOT / "infrastructure/gateway/login-routes.yaml").read_text())
    if doc["metadata"]["name"] == "sso-login")
for path, target in [("/", None), ("/if/flow/", "authentik-server"),
                     ("/sso/access-denied", "sso-access-pages"),
                     ("/application/o/", "authentik-server"), ("/oauth2/", "oauth2-proxy")]:
    rule = next(rule for rule in login["spec"]["rules"]
                if any(match["path"]["value"] == path for match in rule.get("matches", [])))
    assert all(f["type"] != "ExternalAuth" for f in rule.get("filters", [])), path
    if target:
        assert rule["backendRefs"][0]["name"] == target, path
    else:
        redirect = rule["filters"][0]["requestRedirect"]
        assert redirect["path"]["replaceFullPath"] == "/if/flow/default-authentication-flow/"

server = ThreadingHTTPServer(("127.0.0.1", 0), Identity)
threading.Thread(target=server.serve_forever, daemon=True).start()
try:
    with tempfile.TemporaryDirectory() as workspace:
        root = Path(workspace)
        root.chmod(0o755)
        config = yaml.safe_load((SOURCE / "config.yaml").read_text())
        for section in ("serve", "management"):
            config[section].update(host="127.0.0.1", port=free_port())
        for mechanism in config["mechanisms"]["authenticators"]:
            if mechanism["id"] == "session":
                mechanism["config"]["identity_info_endpoint"]["url"] = (
                    f"http://127.0.0.1:{server.server_port}/oauth2/userinfo")
        config["providers"]["file_system"]["src"] = str(root / "rules.yaml")
        (root / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
        (root / "rules.yaml").write_text((SOURCE / "rules.yaml").read_text())
        arguments = [f"--config={root / 'config.yaml'}" if arg.startswith("--config=") else arg
                     for arg in container["args"]]
        name = f"heimdall-browser-{os.getpid()}"
        command = [binary] if binary else [
            "docker", "run", "--rm", "--name", name, "--network=host", "--read-only",
            "--user=65532:65532", "--cap-drop=ALL", "--security-opt=no-new-privileges",
            "-v", f"{root}:{root}:ro", container["image"]]
        with tempfile.TemporaryFile(mode="w+") as logs:
            environment = {k: v for k, v in os.environ.items() if not k.startswith("HEIMDALL_")}
            process = subprocess.Popen(command + arguments, env=environment, stdout=logs,
                                       stderr=subprocess.STDOUT)
            channel = grpc.insecure_channel(f"127.0.0.1:{config['serve']['port']}")
            try:
                grpc.channel_ready_future(channel).result(timeout=25)
                client = external_auth_pb2_grpc.AuthorizationStub(channel)

                def request(host, cookie="", accept="text/html", authorization="", extra=None):
                    headers = {"accept": accept}
                    if cookie:
                        headers["cookie"] = "__Host-elektrokube_sso=" + cookie
                    if authorization:
                        headers["authorization"] = authorization
                    headers.update(extra or {})
                    response = client.Check(external_auth_pb2.CheckRequest(attributes={
                        "request": {"http": {"method": "GET", "host": host,
                                             "path": "/if/admin/", "scheme": "https",
                                             "headers": headers}}}), timeout=5)
                    if response.status.code == 0:
                        return 200, ""
                    headers = {h.header.key.lower(): h.header.value
                               for h in response.denied_response.headers}
                    return response.denied_response.status.code, headers.get("location", "")

                for host in ("auth.internal", "authentik.admin.internal"):
                    for cookie in ("", "expired"):
                        code, location = request(host, cookie)
                        assert code == 302 and location.startswith(f"https://{host}/oauth2/start?rd="), (code, location)
                    assert request(host, accept="application/json") == (401, "")
                    assert request(host, authorization="Bearer invalid") == (401, "")
                    for cookie in ("admin", "bootstrap"):
                        assert request(host, cookie) == (200, "")
                    for cookie in ("unrelated", "empty", "malformed"):
                        assert request(host, cookie) == (302, "https://auth.internal/sso/access-denied")
                        assert request(host, cookie, accept="application/json") == (403, "")
                    assert request(host, authorization="Bearer unrelated") == (403, "")
                assert request("auth.internal", "ordinary") == (200, "")
                assert request("authentik.admin.internal", "ordinary") == (
                    302, "https://auth.internal/sso/access-denied")
                assert request("authentik.admin.internal", "ordinary", accept="application/json",
                               extra={"x-auth-request-groups": "cluster-admins"}) == (403, "")
                assert request("unknown.internal", "admin")[0] != 200
                # An identity-service outage is not a missing-group decision.
                server.shutdown()
                server.server_close()
                code, location = request("auth.internal", "ordinary")
                assert code >= 500 and not location, (code, location)
            except Exception:
                logs.seek(0)
                print(logs.read(), file=sys.stderr)
                raise
            finally:
                channel.close()
                if not binary:
                    subprocess.run(["docker", "stop", "--time=5", name], check=False,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)
finally:
    server.shutdown()
    server.server_close()

print("Browser login, bootstrap routes, permission page, API denials and outage separation pass.")
