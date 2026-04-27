#!/usr/bin/env python3
"""
Port-updater service for Crafty + ProtonVPN + Gluetun.

Watches PORT_FILE (written by Gluetun's VPN_PORT_FORWARDING_UP_COMMAND) for
the currently-forwarded ProtonVPN port.  On change it:

  1. Rewrites server.properties for each configured Crafty-managed server.
  2. Updates Cloudflare DNS:
       - SRV record (_minecraft._tcp.<domain>) pointing at CF_TARGET + new port.
       - A record (CF_A_NAME, optional) with the ProtonVPN exit IP.
  3. Restarts affected server(s) via Crafty REST API.

All configuration is via environment variables.  Python 3.12 stdlib only.
"""

import json
import logging
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Configuration — required vars raise KeyError if missing
# ---------------------------------------------------------------------------
PORT_FILE: str = os.environ.get("PORT_FILE", "/shared/forwarded_port")
CRAFTY_URL: str = os.environ.get("CRAFTY_URL", "https://127.0.0.1:8443")
CRAFTY_TOKEN: str = os.environ["CRAFTY_TOKEN"]
SERVER_IDS: list[str] = [
    s.strip() for s in os.environ["SERVER_IDS"].split(",") if s.strip()
]
SERVER_PROPS_PATHS: list[str] = [
    s.strip() for s in os.environ["SERVER_PROPS_PATHS"].split(",") if s.strip()
]

# Cloudflare — all optional; DNS update is skipped when CF_TOKEN / CF_ZONE_ID unset
CF_TOKEN: str = os.environ.get("CF_TOKEN", "")
CF_ZONE_ID: str = os.environ.get("CF_ZONE_ID", "")
CF_SRV_NAME: str = os.environ.get("CF_SRV_NAME", "")    # _minecraft._tcp.play.example.com
CF_A_NAME: str = os.environ.get("CF_A_NAME", "")        # play.example.com  (optional)
CF_TARGET: str = os.environ.get("CF_TARGET", "")        # play.example.com
CF_TTL: int = int(os.environ.get("CF_TTL", "60"))

# Gluetun control-server base URL (shares the network namespace, so localhost)
GLUETUN_API: str = os.environ.get("GLUETUN_API", "http://127.0.0.1:8000")
# Optional API key for gluetun 3.40+ authenticated control server (leave empty to skip)
GLUETUN_API_KEY: str = os.environ.get("GLUETUN_API_KEY", "")

POLL_SECONDS: int = int(os.environ.get("POLL_SECONDS", "5"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("port-updater")

# Crafty ships a self-signed TLS certificate by default — skip verification
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# Generic HTTP helper
# ---------------------------------------------------------------------------
def _http(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    body=None,
    timeout: int = 30,
    ctx: ssl.SSLContext | None = None,
) -> tuple[int, str]:
    """Send an HTTP request; return (status_code, response_text)."""
    data: bytes | None = None
    h = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


# ---------------------------------------------------------------------------
# Exit-IP detection via Gluetun's control API
# ---------------------------------------------------------------------------
def get_exit_ip() -> str | None:
    """Return the ProtonVPN exit IP from Gluetun's built-in control server."""
    try:
        headers: dict = {}
        if GLUETUN_API_KEY:
            headers["X-API-Key"] = GLUETUN_API_KEY
        code, body = _http("GET", f"{GLUETUN_API}/v1/publicip/ip", headers=headers, timeout=10)
        if code == 200:
            data = json.loads(body)
            return data.get("public_ip") or data.get("ip")
        log.warning("Gluetun IP endpoint returned HTTP %s", code)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not fetch exit IP from Gluetun: %s", exc)
    return None


# ---------------------------------------------------------------------------
# server.properties
# ---------------------------------------------------------------------------
def update_server_properties(path: str, port: str) -> None:
    """Set server-port in *path* to *port*, creating the file if absent."""
    if not os.path.exists(path):
        log.warning("server.properties not found at %s — creating it", path)
        with open(path, "w") as fh:
            fh.write(f"server-port={port}\n")
        return

    with open(path) as fh:
        lines = fh.readlines()

    found = False
    for i, line in enumerate(lines):
        if line.startswith("server-port="):
            lines[i] = f"server-port={port}\n"
            found = True
            break

    if not found:
        lines.append(f"server-port={port}\n")

    with open(path, "w") as fh:
        fh.writelines(lines)

    log.info("Updated %s → server-port=%s", path, port)


# ---------------------------------------------------------------------------
# Crafty REST API
# ---------------------------------------------------------------------------
def crafty_restart(server_id: str) -> bool:
    """Ask Crafty to restart *server_id*.  Returns True on success."""
    url = f"{CRAFTY_URL}/api/v2/servers/{server_id}/action/restart_server"
    headers = {"Authorization": f"Bearer {CRAFTY_TOKEN}"}
    code, body = _http("POST", url, headers=headers, ctx=_SSL_CTX)
    if 200 <= code < 300:
        log.info("Crafty restart %s → HTTP %s", server_id, code)
        return True
    log.error("Crafty restart %s failed → HTTP %s: %s", server_id, code, body[:300])
    return False


# ---------------------------------------------------------------------------
# Cloudflare DNS
# ---------------------------------------------------------------------------
def _cf_headers() -> dict:
    return {"Authorization": f"Bearer {CF_TOKEN}"}


def _cf_find_record(rtype: str, name: str) -> str | None:
    """Return the record ID of the first matching Cloudflare DNS record."""
    q = urllib.parse.urlencode({"type": rtype, "name": name})
    code, body = _http(
        "GET",
        f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records?{q}",
        headers=_cf_headers(),
    )
    if not (200 <= code < 300):
        log.error("Cloudflare record lookup failed: %s %s", code, body[:300])
        return None
    results = json.loads(body).get("result", [])
    return results[0]["id"] if results else None


def _cf_upsert(rtype: str, name: str, payload: dict) -> None:
    """Create or update a Cloudflare DNS record."""
    base = f"https://api.cloudflare.com/client/v4/zones/{CF_ZONE_ID}/dns_records"
    record_id = _cf_find_record(rtype, name)
    if record_id:
        code, body = _http("PUT", f"{base}/{record_id}", headers=_cf_headers(), body=payload)
    else:
        code, body = _http("POST", base, headers=_cf_headers(), body=payload)
    if 200 <= code < 300:
        log.info("Cloudflare %s %s updated", rtype, name)
    else:
        log.error("Cloudflare %s %s failed: %s %s", rtype, name, code, body[:300])


def cloudflare_update(port: str, exit_ip: str | None) -> None:
    """Update the Cloudflare SRV (and optionally A) record for the new port/IP."""
    if not (CF_TOKEN and CF_ZONE_ID):
        log.info("Cloudflare not configured — skipping DNS update")
        return

    # SRV record — _minecraft._tcp.play.example.com
    if CF_SRV_NAME and CF_TARGET:
        # Extract the bare hostname from the underscore labels:
        # "_minecraft._tcp.play.example.com" → "play.example.com"
        parts = CF_SRV_NAME.split(".", 2)
        record_name = parts[2] if len(parts) == 3 else CF_SRV_NAME
        _cf_upsert(
            "SRV",
            CF_SRV_NAME,
            {
                "type": "SRV",
                "name": CF_SRV_NAME,
                "ttl": CF_TTL,
                "data": {
                    "service": "_minecraft",
                    "proto": "_tcp",
                    "name": record_name,
                    "priority": 0,
                    "weight": 5,
                    "port": int(port),
                    "target": CF_TARGET,
                },
            },
        )

    # A record — optional, tracks the ProtonVPN exit IP
    if CF_A_NAME:
        if exit_ip:
            _cf_upsert(
                "A",
                CF_A_NAME,
                {
                    "type": "A",
                    "name": CF_A_NAME,
                    "content": exit_ip,
                    "ttl": CF_TTL,
                    "proxied": False,
                },
            )
        else:
            log.warning("CF_A_NAME set but exit IP unknown — skipping A record update")


def _cf_upsert_a_only(exit_ip: str) -> None:
    """Update only the Cloudflare A record with a new exit IP (no SRV change)."""
    if not (CF_TOKEN and CF_ZONE_ID and CF_A_NAME):
        return
    _cf_upsert(
        "A",
        CF_A_NAME,
        {
            "type": "A",
            "name": CF_A_NAME,
            "content": exit_ip,
            "ttl": CF_TTL,
            "proxied": False,
        },
    )


# ---------------------------------------------------------------------------
# Port-file reader
# ---------------------------------------------------------------------------
def _read_port_file() -> str | None:
    """Read and validate the forwarded port from PORT_FILE; return None if absent/invalid."""
    if not os.path.exists(PORT_FILE):
        return None
    with open(PORT_FILE) as fh:
        raw = fh.read().strip()
    if raw and raw.isdigit() and 1024 <= int(raw) <= 65535:
        return raw
    return None


# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------
def main() -> None:
    log.info("port-updater starting")
    log.info("PORT_FILE=%s  poll_interval=%ss", PORT_FILE, POLL_SECONDS)
    log.info("CRAFTY_URL=%s  servers=%s", CRAFTY_URL, SERVER_IDS)
    log.info("Cloudflare SRV=%s  A=%s  target=%s", CF_SRV_NAME or "(off)", CF_A_NAME or "(off)", CF_TARGET or "(off)")

    last_port: str | None = None
    last_exit_ip: str | None = None

    while True:
        try:
            current_port = _read_port_file()
            current_ip = get_exit_ip()  # always poll — cheap, local

            port_changed = current_port is not None and current_port != last_port
            ip_changed = current_ip is not None and current_ip != last_exit_ip

            if port_changed:
                log.info("Port change detected: %s → %s", last_port, current_port)

                # 1. Update server.properties for every managed server
                for path in SERVER_PROPS_PATHS:
                    try:
                        update_server_properties(path, current_port)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("server.properties update failed (%s): %s", path, exc)

                # Retry exit-IP fetch a few times if it failed (e.g. startup race
                # where gluetun's HTTP control server hasn't bound :8000 yet).
                if not current_ip:
                    for _attempt in range(5):
                        time.sleep(5)
                        current_ip = get_exit_ip()
                        if current_ip:
                            break

                # 2. Update Cloudflare DNS
                if current_ip:
                    log.info("ProtonVPN exit IP: %s", current_ip)
                cloudflare_update(current_port, current_ip)

                # 3. Restart servers via Crafty
                for sid in SERVER_IDS:
                    try:
                        crafty_restart(sid)
                    except Exception as exc:  # noqa: BLE001
                        log.exception("Crafty restart failed (%s): %s", sid, exc)

                last_port = current_port
                last_exit_ip = current_ip

            elif ip_changed and CF_A_NAME:
                # Exit IP rotated without a port change — update A record only.
                # This also fixes the startup race: if get_exit_ip() failed on the
                # first cycle (gluetun not yet ready), it will succeed here on the
                # next cycle and the A record will converge within POLL_SECONDS.
                log.info("Exit IP change detected: %s → %s", last_exit_ip, current_ip)
                _cf_upsert_a_only(current_ip)
                last_exit_ip = current_ip

        except Exception as exc:  # noqa: BLE001
            log.exception("Unexpected loop error: %s", exc)

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
