#!/usr/bin/env python3
"""One-way sync of a primary Pi-hole (v6) to a secondary, built for a pair
behind a virtual address (keepalived) or handed out together by DHCP.

What is copied from the primary to the secondary:
  - adlists, allow/deny entries, groups, clients (through Teleporter, gravity
    tables only), followed by a gravity update on the secondary
  - a fixed slice of the dns settings: upstreams, local records (hosts),
    CNAME records, conditional forwarding, domain and a few flags

What is never copied: web server ports, passwords, interface and listening
settings, DHCP, the blocking on/off state.

The primary is only ever read: every request that is not a GET goes to the
secondary, and the script refuses to send anything else to the primary.

Settings come from the environment (see pihole-sync.env.example):
  PRIMARY_URL, SECONDARY_URL        base URLs ending in /api
  PRIMARY_PASSWORD_FILE, SECONDARY_PASSWORD_FILE   optional, only if a
                                    password is set on that Pi-hole; the file
                                    must be readable by the user the script runs
                                    as (see README.md)
Use --dry-run to print the differences and change nothing.
Exit codes: 0 in sync (or synced), 1 primary or secondary unreachable,
2 refused or failed.
"""
import json
import os
import sys
import time
import http.client
import urllib.error
import urllib.request
import uuid

DNS_KEYS = ["upstreams", "hosts", "cnameRecords", "revServers", "domain",
            "dnssec", "domainNeeded", "expandHosts", "bogusPriv", "rateLimit"]
GRAVITY_IMPORT = {
    "config": False, "dhcp_leases": False,
    "gravity": {"group": True, "adlist": True, "adlist_by_group": True,
                "domainlist": True, "domainlist_by_group": True,
                "client": True, "client_by_group": True},
}

PRIMARY = os.environ.get("PRIMARY_URL", "").rstrip("/")
SECONDARY = os.environ.get("SECONDARY_URL", "").rstrip("/")
DRY = "--dry-run" in sys.argv


def log(msg):
    print(msg, flush=True)


class SecretError(Exception):
    pass


def read_secret(var):
    path = os.environ.get(var)
    if not path:
        return None
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError as e:
        raise SecretError("cannot read the password file named in %s (%s): %s" % (var, path, e.strerror))


def call(base, method, path, body=None, sid=None, raw=None, content_type=None, timeout=30):
    # Logging in and out is a session, not a change: the only non-GET requests
    # the primary may receive.
    if base == PRIMARY and method != "GET" and not (path == "/auth" and method in ("POST", "DELETE")):
        raise SystemExit("refusing to send %s %s to the primary" % (method, path))
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if raw is not None:
        data = raw
        headers["Content-Type"] = content_type
    if sid:
        headers["sid"] = sid
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = r.read()
        ctype = r.headers.get("Content-Type", "")
    if "json" in ctype and payload:
        return json.loads(payload)
    return payload


def login(base, pw_var):
    pw = read_secret(pw_var)
    if pw is None:
        return None
    r = call(base, "POST", "/auth", {"password": pw})
    return r["session"]["sid"]


def logout(base, sid):
    if sid:
        try:
            call(base, "DELETE", "/auth", sid=sid)
        except Exception:
            pass


def wait_api(base, seconds=240):
    """A Teleporter import and a gravity update make FTL restart; wait until
    the API answers again."""
    end = time.time() + seconds
    while time.time() < end:
        try:
            call(base, "GET", "/auth", timeout=5)
            return True
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            time.sleep(3)
    return False


def run_gravity(sid):
    """Run the gravity update on the secondary. The import restarts FTL a
    moment after it returns, which can cut the first request off, so the
    update counts as done only when its output ends with 'Done.'."""
    for attempt in range(1, 4):
        try:
            out = call(SECONDARY, "POST", "/action/gravity", sid=sid, timeout=900)
            if b"Done." in (out if isinstance(out, bytes) else json.dumps(out).encode()):
                return True
            log("gravity output did not end with Done. (attempt %d)" % attempt)
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            log("gravity request ended early (%s), attempt %d" % (type(e).__name__, attempt))
        if not wait_api(SECONDARY, 600):
            return False
        time.sleep(5)
    return False


def snapshot(base, sid):
    groups = call(base, "GET", "/groups", sid=sid)["groups"]
    gname = {g["id"]: g["name"] for g in groups}

    def names(ids):
        return sorted(gname.get(i, "?%s" % i) for i in ids)

    lists = call(base, "GET", "/lists", sid=sid)["lists"]
    domains = call(base, "GET", "/domains", sid=sid)["domains"]
    clients = call(base, "GET", "/clients", sid=sid)["clients"]
    cfg = call(base, "GET", "/config", sid=sid)["config"]["dns"]
    return {
        "groups": sorted((g["name"], bool(g["enabled"])) for g in groups),
        "lists": sorted((x["address"], x["type"], bool(x["enabled"]), tuple(names(x["groups"]))) for x in lists),
        "domains": sorted((x["domain"], x["type"], x["kind"], bool(x["enabled"]), tuple(names(x["groups"]))) for x in domains),
        "clients": sorted((x["client"], tuple(names(x["groups"]))) for x in clients),
        "dns": {k: (sorted(cfg[k]) if isinstance(cfg[k], list) else cfg[k]) for k in DNS_KEYS if k in cfg},
    }


def multipart(file_bytes, import_json):
    boundary = uuid.uuid4().hex
    parts = []
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"teleporter.zip\"\r\n"
                  "Content-Type: application/zip\r\n\r\n" % boundary).encode() + file_bytes + b"\r\n")
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"import\"\r\n\r\n%s\r\n"
                  % (boundary, json.dumps(import_json))).encode())
    parts.append(("--%s--\r\n" % boundary).encode())
    return b"".join(parts), "multipart/form-data; boundary=%s" % boundary


def main():
    if not PRIMARY or not SECONDARY:
        log("PRIMARY_URL and SECONDARY_URL must be set")
        return 2
    if PRIMARY == SECONDARY:
        log("primary and secondary are the same address, refusing")
        return 2
    psid = ssid = None
    try:
        for name, base, var in (("primary", PRIMARY, "PRIMARY_PASSWORD_FILE"), ("secondary", SECONDARY, "SECONDARY_PASSWORD_FILE")):
            try:
                sid = login(base, var)
                if name == "primary":
                    psid = sid
                    src = snapshot(base, sid)
                else:
                    ssid = sid
                    dst = snapshot(base, sid)
            except SecretError as e:
                log(str(e))
                return 2
            except urllib.error.HTTPError as e:
                hint = " (wrong or missing password?)" if e.code in (401, 403) else ""
                log("%s answered HTTP %d on %s%s, nothing changed" % (name, e.code, e.url, hint))
                return 2
            except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
                log("%s unreachable, nothing changed: %s" % (name, e))
                return 1

        if not src["lists"] or not src["groups"]:
            log("the primary shows no adlists or no groups; refusing to copy an empty-looking source")
            return 2

        gravity_diff = [k for k in ("groups", "lists", "domains", "clients") if src[k] != dst[k]]
        dns_diff = [k for k in DNS_KEYS if src["dns"].get(k) != dst["dns"].get(k)]
        if not gravity_diff and not dns_diff:
            log("in sync")
            return 0
        log("differences: gravity tables %s, dns settings %s" % (gravity_diff or "none", dns_diff or "none"))
        if DRY:
            for k in gravity_diff:
                log("  %s only on primary: %s" % (k, [x for x in src[k] if x not in dst[k]][:5]))
                log("  %s only on secondary: %s" % (k, [x for x in dst[k] if x not in src[k]][:5]))
            for k in dns_diff:
                log("  dns.%s primary=%s secondary=%s" % (k, json.dumps(src["dns"].get(k))[:100], json.dumps(dst["dns"].get(k))[:100]))
            return 0

        if gravity_diff:
            zipped = call(PRIMARY, "GET", "/teleporter", sid=psid, timeout=120)
            body, ctype = multipart(zipped, GRAVITY_IMPORT)
            call(SECONDARY, "POST", "/teleporter", sid=ssid, raw=body, content_type=ctype, timeout=120)
            log("imported adlists, allow/deny entries, groups and clients into the secondary")
            if not wait_api(SECONDARY):
                log("the secondary did not come back after the import")
                return 2
            ssid = login(SECONDARY, "SECONDARY_PASSWORD_FILE")
        if dns_diff:
            patch = {"config": {"dns": {k: src["dns"][k] for k in dns_diff}}}
            call(SECONDARY, "PATCH", "/config", patch, sid=ssid, timeout=60)
            log("dns settings copied: %s" % dns_diff)
            if not wait_api(SECONDARY):
                log("the secondary did not come back after the dns change")
                return 2
        # Allow/deny entries and clients work without a gravity run; new or
        # changed adlists and groups need one.
        if "lists" in gravity_diff or "groups" in gravity_diff:
            if not run_gravity(ssid):
                log("the gravity update on the secondary did not finish")
                return 2
            log("gravity updated on the secondary")

        ssid = login(SECONDARY, "SECONDARY_PASSWORD_FILE")
        after = snapshot(SECONDARY, ssid)
        left = [k for k in ("groups", "lists", "domains", "clients") if src[k] != after[k]] + \
               [k for k in DNS_KEYS if src["dns"].get(k) != after["dns"].get(k)]
        if left:
            log("still different after the sync: %s" % left)
            return 2
        log("synced")
        return 0
    except urllib.error.HTTPError as e:
        log("API error %s on %s: %s" % (e.code, e.url, e.read()[:200]))
        return 2
    finally:
        logout(PRIMARY, psid)
        logout(SECONDARY, ssid)


if __name__ == "__main__":
    sys.exit(main())
