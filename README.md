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
                  └─ calls Crafty REST API to restart the server
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
| `POLL_SECONDS` | `5` | How often to check `PORT_FILE` for changes |

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

## Docker image

Pre-built multi-arch images (linux/amd64 + linux/arm64) are published to GHCR
on every push to `master` and on version tags:

```
ghcr.io/alpasyon007/crafty-proton-port-updater:latest
```

## License

MIT — see [LICENSE](LICENSE).
