# crafty-proton-port-updater

A lightweight, zero-dependency Python service that keeps a
[Crafty Controller](https://gitlab.com/crafty-controller/crafty-4)‑managed
Minecraft server reachable after every ProtonVPN port rotation.

## Architecture

```
ProtonVPN (random forwarded port, rotates every few hours)
        │
        ▼
┌─────────────────────────────┐
│ gluetun-crafty (WireGuard)  │  ← writes new port to /shared/forwarded_port
└──────────┬──────────────────┘
           │  shared network namespace
           ├──► crafty            (Minecraft server process)
           └──► port-updater      (this service)
                  │
                  ├─ rewrites server.properties  (new port)
                  ├─ updates Cloudflare SRV record
                  ├─ updates Cloudflare A record  (ProtonVPN exit IP)
                  └─ calls Crafty REST API: stop_server → wait → start_server
```

## Environment Variables

### Required

| Variable | Description |
|---|---|
| `CRAFTY_TOKEN` | Crafty API bearer token (Settings → API Keys) |
| `SERVER_IDS` | Comma-separated Crafty server UUIDs |
| `SERVER_PROPS_PATHS` | Comma-separated paths to `server.properties` files (must align with `SERVER_IDS`) |

### Optional — general

| Variable | Default | Description |
|---|---|---|
| `PORT_FILE` | `/shared/forwarded_port` | File written by Gluetun's `VPN_PORT_FORWARDING_UP_COMMAND` |
| `CRAFTY_URL` | `https://127.0.0.1:8443` | Crafty base URL (self-signed cert is accepted automatically) |
| `GLUETUN_API` | `http://127.0.0.1:8000` | Gluetun control-server base URL (used to read the exit IP) |
| `GLUETUN_API_KEY` | *(empty)* | API key for gluetun 3.40+ authenticated control server; leave empty when using `auth = "none"` |
| `POLL_SECONDS` | `5` | How often to check `PORT_FILE` for changes |
| `CRAFTY_RESTART_MAX_ATTEMPTS` | `6` | Total attempts for each Crafty API call on connection refused / timeout — handles Crafty's slow Tornado-server startup race on cold boot |
| `CRAFTY_RESTART_RETRY_DELAY` | `5` | Seconds between retry attempts for each Crafty API call |
| `CRAFTY_STOP_TIMEOUT` | `60` | Seconds to wait for Crafty to confirm the server has stopped before sending `start_server` anyway |

### Optional — Cloudflare DNS

Leave `CF_TOKEN` / `CF_ZONE_ID` unset to skip all DNS updates.

| Variable | Description |
|---|---|
| `CF_TOKEN` | Cloudflare API token with *Edit zone DNS* permission |
| `CF_ZONE_ID` | Cloudflare zone ID (right sidebar of your domain's overview page) |
| `CF_SRV_NAME` | Full SRV record name, e.g. `_minecraft._tcp.play.example.com` |
| `CF_TARGET` | SRV target hostname, e.g. `play.example.com` |
| `CF_A_NAME` | *(optional)* A record to keep pointing at the ProtonVPN exit IP, e.g. `play.example.com` |
| `CF_TTL` | DNS TTL in seconds (default `60`) |

## Quick Start

### 1 — Prerequisites on TrueNAS (or any Docker host)

```bash
mkdir -p /mnt/Apps/app-configs/{gluetun-crafty,shared,crafty/{backups,logs,servers,config,import}}
chown -R 1000:1000 /mnt/Apps/app-configs/crafty
chown -R 1000:1000 /mnt/Apps/app-configs/shared
```

### 2 — Deploy the stack

Copy `compose.example.yaml` to `compose.yaml`, fill in the placeholders marked
`# CHANGE`, then:

```bash
docker compose up -d
```

### 3 — Get the Crafty API token

On first boot Crafty generates a random admin password — find it in its logs:

```bash
docker compose logs crafty | grep -i password
```

Log in → user icon (top-right) → **Panel Config** → **API Keys** → **+ New** →
enable all server permissions → **Save** → copy the token into `CRAFTY_TOKEN`.

### 4 — Create your Minecraft server in Crafty

1. Drop your CurseForge server-pack ZIP into the `import` volume path.
2. **Servers → Create New Server → Import Server → Import a Zip**.
3. The server UUID appears in the URL when you open the server detail page:
   `/panel/server_detail?id=<UUID>`.
4. Set `SERVER_IDS=<UUID>` and
   `SERVER_PROPS_PATHS=<path-to-server.properties>` in your compose file and
   restart the stack.

For multiple servers use comma-separated values (they all receive the same port
— only run one at a time):

```
SERVER_IDS=uuid1,uuid2
SERVER_PROPS_PATHS=/crafty/servers/uuid1/server.properties,/crafty/servers/uuid2/server.properties
```

### 5 — Cloudflare DNS

Create these records in Cloudflare **once** (the service will keep them
updated):

```
play.example.com        A     <any-placeholder-IP>
_minecraft._tcp         SRV   0 5 <placeholder-port> play.example.com
```

Players connect to `play.example.com` — no port needed.

## Deploying on TrueNAS SCALE

### App installation

Use the **Custom App** flow: **Apps → Discover Apps → Install via YAML** (not the catalog). Paste your `compose.yaml` directly into the YAML editor.

### Dataset layout

Create the following datasets under `/mnt/<pool>/Apps/app-configs/` before deploying:

```
gluetun-crafty/          → /gluetun       (gluetun state, VPN keys, auth config)
shared/                  → /shared        (forwarded_port handoff file)
crafty/config/           → /crafty/app/config
crafty/servers/          → /crafty/servers
crafty/backups/          → /crafty/backups
crafty/logs/             → /crafty/logs
crafty/import/           → /crafty/import
```

```bash
mkdir -p /mnt/<pool>/Apps/app-configs/{gluetun-crafty,shared}
mkdir -p /mnt/<pool>/Apps/app-configs/crafty/{config,servers,backups,logs,import}
# TrueNAS apps run as UID 568 by default; use 1000 if your images expect it
chown -R 568:568 /mnt/<pool>/Apps/app-configs/
```

### Cloudflare DNS records to pre-create

Create these records once — the service will keep them updated:

| Type | Name | Content | Notes |
|------|------|---------|-------|
| `A` | `play.example.com` | any placeholder IP | **DNS-only (grey cloud)** — proxying breaks Minecraft |
| `SRV` | `_minecraft._tcp` | service `_minecraft`, proto `_tcp`, target `play.example.com`, port `25565` | players connect without a port number |

### Crafty API token

Log in → user icon (top-right) → **Panel Config** → **API Keys** → **+ New** →
enable all server permissions → **Save** → copy the token into `CRAFTY_TOKEN`.

### Gluetun control-server authentication (3.40+)

Recent gluetun versions require authentication for the control API. The simplest
workaround is to add a config file that allows unauthenticated access to the routes
port-updater uses:

```bash
mkdir -p /mnt/<pool>/Apps/app-configs/gluetun-crafty/auth
cat > /mnt/<pool>/Apps/app-configs/gluetun-crafty/auth/config.toml <<'EOF'
[[roles]]
name = "port-updater"
routes = [
  "GET /v1/publicip/ip",
  "GET /v1/openvpn/portforwarded",
]
auth = "none"
EOF
chmod 644 /mnt/<pool>/Apps/app-configs/gluetun-crafty/auth/config.toml
```

Alternatively, use API-key auth and pass the key via `GLUETUN_API_KEY` in the
port-updater environment.

### Crafty bind address

If `CRAFTY_URL=https://127.0.0.1:8443` returns connection errors, Crafty may be
binding only to a specific bridge IP. Edit `/crafty/app/config/config.json` (inside
the `crafty/config/` dataset) and set:

```json
"https_bind": "0.0.0.0"
```

Then restart the Crafty container.

### Common pitfalls

| Symptom | Cause | Fix |
|---------|-------|-----|
| "Secure Connection Failed" / `SSL_ERROR_RX_RECORD_TOO_LONG` | Browsing `http://host:30026` — that's HTTP, not HTTPS | Use `http://` for the gluetun control port (8000) |
| port-updater logs `WARNING Could not fetch exit IP from Gluetun` | gluetun control API requires authentication | Create `auth/config.toml` (see above) or set `GLUETUN_API_KEY` |
| Crafty or port-updater can't reach each other | `network_mode: container:<name>` requires gluetun running first | The restart policy usually recovers; check `docker ps` for gluetun status |
| NAT-PMP port forwarding fails with `i/o timeout` | `FIREWALL_OUTBOUND_SUBNETS` includes `10.0.0.0/8`, overlapping Proton's WireGuard gateway | Use LAN-specific CIDR only (e.g. `192.168.1.0/24`) |
| Crafty WebUI unreachable on port 30025 | `FIREWALL_INPUT_PORTS` not set — gluetun blocks inbound 8443 | Add `FIREWALL_INPUT_PORTS=8443,8000` to gluetun environment |
| port-updater logs `Crafty restart … failed after 6 attempts` on first deploy | Crafty's Tornado HTTPS server takes 30–60 s to bind `:8443` on cold boot | The updater now retries automatically (6 × 5 s). No manual WebUI restart needed. Increase `CRAFTY_RESTART_MAX_ATTEMPTS` if your hardware is slower. |
| After changing `CRAFTY_TOKEN`, the next port rotation fails with `HTTP 401` | The old token was revoked or a new one was generated in Crafty | Regenerate the token in Crafty UI (Panel Config → API Keys) and update `CRAFTY_TOKEN` in your env |

## Docker image

Pre-built multi-arch images (linux/amd64 + linux/arm64) are published to GHCR
on every push to `master` and on version tags:

```
ghcr.io/alpasyon007/crafty-proton-port-updater:latest
```

## License

MIT — see [LICENSE](LICENSE).
