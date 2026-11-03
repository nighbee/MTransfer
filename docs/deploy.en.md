# MTransfer SaaS — deploy guide (EN)

Two supported targets: a cheap **VPS** (public) or a **home server** (LAN / Tailscale).

## What you get

- One `docker compose up -d` — app + Caddy (auto HTTPS).
- No accounts: each visitor gets an isolated session cookie (`mtsession`).
- Tokens live in server memory only; per-session data under `data/sessions/<id>/`.
- Idle sessions drop from memory after 24h (`SessionStore.cleanup`); prune files with `docker volume prune` or the snippet below.

## VPS (Hetzner / DigitalOcean / etc.)

1. Point your domain's A record at the server IP (e.g. `mtransfer.example.com`).
2. Edit `Caddyfile`: replace `mtransfer.example.com` with your domain.
3. On the server:
   ```bash
   git clone <repo> && cd MTransfer
   git checkout saas
   docker compose up -d --build
   docker compose logs -f
   ```
4. Open `https://your-domain` — the wizard guides each visitor.

Minimum box: 1 vCPU / 1 GB RAM. Matching is I/O-bound (~3s/track via iTunes API).

## Home server (LAN or Tailscale)

1. Same `docker compose up -d --build` on the home box.
2. For LAN-only: change `Caddyfile` first line to `http://<lan-ip-or-hostname>` (no public TLS inside LAN).
3. For remote access: use a Tailscale hostname (`http://box.tailnet:80` via Caddy, or expose app port directly):
   ```yaml
   # quick alternative: expose app without Caddy
   # docker compose run --service-ports ...
   ```
   Simplest: uncomment the `ports: ["8000:8000"]` block in compose and reach `http://<tailscale-ip>:8000`.

## Operations

```bash
docker compose ps                    # status
docker compose logs -f app           # app logs (tokens are redacted)
docker compose down                  # stop
docker compose up -d --build         # redeploy after git pull

# prune old session files (older than 2 days), keeps .gitkeep:
find /var/lib/docker/volumes/mtransfer-data/_data/sessions -maxdepth 1 -mtime +2 -exec rm -rf {} +
```

## Security notes

- HTTPS terminates at Caddy (auto Let's Encrypt on VPS).
- Session cookie is `HttpOnly; SameSite=Lax`; use a trusted network or HTTPS.
- No tokens or exports are committed — `data/`, dot-token files stay gitignored.
- Rate story: iTunes Search ~20 req/min per IP — one server IP shared by visitors means matching queues naturally; the UI shows live progress per job.
