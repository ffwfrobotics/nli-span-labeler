# Deploying the E13 labeler for external labelers

M2 lets people other than the owner label. They reach the app over the
internet or a LAN, so it needs HTTPS (NFR-5, §11 Q5). The app itself speaks
plain HTTP and stays behind a reverse proxy.

> These files were written and checked without a container runtime: the
> install steps were rehearsed outside a container, but the image itself has
> not been built yet. Build it once and walk through the checklist below
> before inviting anyone.

## Option A: containers (Caddy, automatic certificates)

Needs a host with a public DNS name pointing at it, ports 80 and 443 open,
and Podman or Docker with compose.

```bash
git clone <this repo> && cd nli-span-labeler
export E13_DOMAIN=labels.example.org
export E13_APP_COMMIT=$(git rev-parse --short=12 HEAD)   # recorded on every annotation (NFR-7)
podman compose -f deploy/compose.yaml up -d --build
podman compose -f deploy/compose.yaml exec app python -m e13_labeler create-owner
```

Then open `https://labels.example.org`, log in as the owner, import a pool
(Admin → Batches → Import, or `exec app python -m e13_labeler import ...`),
and send invite links.

What the compose file sets up:
- **Caddy** terminates HTTPS, renews the certificate and adds HSTS. It is the
  only container with published ports.
- **The app** is reachable only from Caddy. `TRUSTED_PROXIES` names Caddy's
  fixed address, so the login rate limit sees real client addresses;
  `ALLOWED_HOSTS` is your domain; cookies are `Secure`.
- **Data** (database, exports, nightly backups) lives in the `e13-data`
  volume, mounted at `/data`. The container runs as an unprivileged user.

## Option B: no containers (an existing reverse proxy or a tunnel)

```bash
uv sync --frozen --no-dev
uv run python -m e13_labeler create-owner
ALLOWED_HOSTS=labels.example.org TRUSTED_PROXIES=127.0.0.1 ./run.sh   # binds 127.0.0.1:8000
```

Point the proxy (nginx, Caddy, or a tunnel such as Cloudflare Tunnel or
Tailscale Funnel) at `127.0.0.1:8000`. Set `TRUSTED_PROXIES` to the address
the proxy connects from, and nothing else. Keep `HOST` at its loopback default
unless the proxy runs on another machine.

## Checklist before the first invite

- [ ] `https://<domain>/api/auth/status` answers; `http://` redirects to https.
- [ ] Requests with another `Host` header get 400.
- [ ] The session cookie shows `Secure; HttpOnly; SameSite=Strict`.
- [ ] Gold: at least 12 items covering every reason plus 2 answerable (12 libre
      ones for public labelers). Opening a batch warns if not.
- [ ] The guideline (`e13_labeler/guideline.json`) is revised and its version bumped.
- [ ] The contributor agreement's licence line is settled (§11 Q4), and
      `contributor.VERSION` bumped.
- [ ] A backup appears in `/data/backups` (or `outputs/e13_labeler/backups`)
      after start-up; copy backups off the host on a schedule.

## Backups and upgrades

The server writes an online backup at start-up when the newest is older than
`BACKUP_INTERVAL_HOURS` (24), then checks hourly. `python -m e13_labeler backup`
or Admin → `POST /api/admin/backup` (owner) makes one at any time. Backups are
never deleted. The database migrates itself on start-up (`PRAGMA user_version`),
so upgrading is: take a backup, pull, rebuild, restart.
