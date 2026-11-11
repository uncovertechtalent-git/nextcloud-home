"""Tests for pihole-sync.py against two fake Pi-hole v6 APIs.

Run:  python3 -m unittest pihole-pair/test_pihole_sync.py
No network, no Pi-hole needed.
"""
import copy
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pihole-sync.py")

BASE_STATE = {
    "groups": [{"id": 0, "name": "Default", "enabled": True, "comment": None}],
    "lists": [{"address": "https://lists.example/hosts", "type": "block", "enabled": True, "groups": [0], "comment": None}],
    "domains": [{"domain": "ok.example", "type": "allow", "kind": "exact", "enabled": True, "groups": [0]}],
    "clients": [],
    "dns": {"upstreams": ["192.0.2.1"], "hosts": ["192.0.2.10 a.lan.example"], "cnameRecords": [],
            "revServers": [], "domain": {"name": "lan", "local": True}, "dnssec": False,
            "domainNeeded": False, "expandHosts": False, "bogusPriv": True,
            "rateLimit": {"count": 1000, "interval": 60}},
}


class Fake:
    """A tiny Pi-hole API. Records every request as (method, path)."""

    def __init__(self, password=None, state=None):
        self.password = password
        self.state = copy.deepcopy(state or BASE_STATE)
        self.seen = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="application/json"):
                if not isinstance(body, bytes):
                    body = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                path = self.path.split("?")[0]
                outer.seen.append((method, path))
                if path == "/api/auth":
                    if method == "POST":
                        pw = json.loads(raw or b"{}").get("password")
                        if outer.password is None or pw == outer.password:
                            return self._send(200, {"session": {"valid": True, "sid": "SID"}})
                        return self._send(401, {"error": {"key": "unauthorized"}})
                    if method == "DELETE":
                        return self._send(204)
                    return self._send(200, {"session": {"valid": outer.password is None}})
                if outer.password is not None and self.headers.get("sid") != "SID":
                    return self._send(401, {"error": {"key": "unauthorized"}})
                s = outer.state
                if method == "GET":
                    if path == "/api/groups": return self._send(200, {"groups": s["groups"]})
                    if path == "/api/lists": return self._send(200, {"lists": s["lists"]})
                    if path == "/api/domains": return self._send(200, {"domains": s["domains"]})
                    if path == "/api/clients": return self._send(200, {"clients": s["clients"]})
                    if path == "/api/config": return self._send(200, {"config": {"dns": s["dns"]}})
                    if path == "/api/teleporter": return self._send(200, b"PK-fake-zip", "application/zip")
                if method == "POST" and path == "/api/teleporter":
                    outer.imported = raw
                    for k in ("groups", "lists", "domains", "clients"):
                        s[k] = copy.deepcopy(outer.source_state[k])
                    return self._send(200, {"files": []})
                if method == "PATCH" and path == "/api/config":
                    s["dns"].update(json.loads(raw)["config"]["dns"])
                    return self._send(200, {"config": {}})
                if method == "POST" and path == "/api/action/gravity":
                    return self._send(200, b"  [i] Gravity\n  [\xe2\x9c\x93] Done.\n", "text/plain")
                return self._send(404, {"error": {"key": "not_found"}})

            def do_GET(self): self._handle("GET")
            def do_POST(self): self._handle("POST")
            def do_PATCH(self): self._handle("PATCH")
            def do_DELETE(self): self._handle("DELETE")

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/api" % self.server.server_port
        self.source_state = None
        self.imported = None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def run(primary_url, secondary_url, *extra, env_extra=None):
    env = dict(os.environ, PRIMARY_URL=primary_url, SECONDARY_URL=secondary_url)
    env.update(env_extra or {})
    p = subprocess.run([sys.executable, SCRIPT, *extra], env=env, capture_output=True, text=True, timeout=60)
    return p.returncode, p.stdout + p.stderr


def free_port_url():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return "http://127.0.0.1:%d/api" % port


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pw_primary = os.path.join(self.tmp.name, "pp")
        self.pw_secondary = os.path.join(self.tmp.name, "sp")
        for path, pw in ((self.pw_primary, "primary-pw"), (self.pw_secondary, "secondary-pw")):
            with open(path, "w") as f:
                f.write(pw + "\n")
        self.fakes = []

    def tearDown(self):
        for f in self.fakes:
            f.close()
        self.tmp.cleanup()

    def fake(self, **kw):
        f = Fake(**kw)
        self.fakes.append(f)
        return f

    def test_in_sync_changes_nothing(self):
        a, b = self.fake(), self.fake()
        rc, out = run(a.url, b.url)
        self.assertEqual(rc, 0, out)
        self.assertIn("in sync", out)
        self.assertFalse([r for r in b.seen if r[0] != "GET"])

    def test_drift_is_repaired_and_primary_only_read(self):
        # Both Pi-holes have a password; the secondary is missing an adlist and a local record.
        a = self.fake(password="primary-pw")
        a.state["lists"].append({"address": "https://more.example/hosts", "type": "block", "enabled": True, "groups": [0], "comment": None})
        a.state["dns"]["hosts"].append("192.0.2.11 b.lan.example")
        b = self.fake(password="secondary-pw")
        b.source_state = a.state
        rc, out = run(a.url, b.url, env_extra={"PRIMARY_PASSWORD_FILE": self.pw_primary,
                                               "SECONDARY_PASSWORD_FILE": self.pw_secondary})
        self.assertEqual(rc, 0, out)
        self.assertIn("synced", out)
        # The primary saw reads and the login/logout of the session, nothing else.
        writes = [r for r in a.seen if r[0] != "GET" and r[1] != "/api/auth"]
        self.assertEqual(writes, [])
        self.assertIn(("DELETE", "/api/auth"), a.seen)
        # The secondary got the import, the dns change and a gravity run.
        self.assertIn(("POST", "/api/teleporter"), b.seen)
        self.assertIn(("PATCH", "/api/config"), b.seen)
        self.assertIn(("POST", "/api/action/gravity"), b.seen)
        self.assertIn("192.0.2.11 b.lan.example", b.state["dns"]["hosts"])
        self.assertIn(b'"adlist": true', b.imported)   # the import selection travels with the file
        self.assertIn(b'"config": false', b.imported)  # and it excludes the config

    def test_empty_primary_is_refused(self):
        a = self.fake()
        a.state["lists"] = []
        b = self.fake()
        rc, out = run(a.url, b.url)
        self.assertEqual(rc, 2, out)
        self.assertFalse([r for r in b.seen if r[0] != "GET"])

    def test_wrong_password_reads_as_authentication_failure(self):
        a = self.fake(password="something-else")
        b = self.fake()
        rc, out = run(a.url, b.url, env_extra={"PRIMARY_PASSWORD_FILE": self.pw_primary})
        self.assertEqual(rc, 2, out)
        self.assertIn("password", out.lower())
        self.assertNotIn("unreachable", out)

    def test_unreadable_password_file_is_named_as_such(self):
        a = self.fake(password="primary-pw")
        b = self.fake()
        rc, out = run(a.url, b.url, env_extra={"PRIMARY_PASSWORD_FILE": os.path.join(self.tmp.name, "missing")})
        self.assertEqual(rc, 2, out)
        self.assertIn("cannot read the password file", out)

    def test_unreachable_primary_exits_1_and_changes_nothing(self):
        b = self.fake()
        rc, out = run(free_port_url(), b.url)
        self.assertEqual(rc, 1, out)
        self.assertIn("unreachable", out)
        self.assertFalse([r for r in b.seen if r[0] != "GET"])

    def test_same_address_is_refused(self):
        a = self.fake()
        rc, out = run(a.url, a.url)
        self.assertEqual(rc, 2, out)

    def test_same_machine_under_another_spelling_is_refused(self):
        a = self.fake()
        other_spelling = a.url.replace("127.0.0.1", "localhost")
        rc, out = run(a.url, other_spelling)
        self.assertEqual(rc, 2, out)
        self.assertIn("same machine", out)
        self.assertEqual([r for r in a.seen if r[0] != "GET"], [])

    def test_session_is_renewed_after_a_dns_only_change(self):
        # Only the dns settings differ; the secondary forgets its sessions when it
        # applies them, as FTL can. The run must still end cleanly.
        a = self.fake(password="primary-pw")
        a.state["dns"]["hosts"].append("192.0.2.11 b.lan.example")
        b = self.fake(password="secondary-pw")
        b.source_state = a.state
        rc, out = run(a.url, b.url, env_extra={"PRIMARY_PASSWORD_FILE": self.pw_primary,
                                               "SECONDARY_PASSWORD_FILE": self.pw_secondary})
        self.assertEqual(rc, 0, out)
        self.assertEqual(b.state["dns"]["hosts"], a.state["dns"]["hosts"])
        self.assertEqual(b.seen.count(("POST", "/api/auth")), 3)  # login, again after the change, again for the final check


if __name__ == "__main__":
    unittest.main()
