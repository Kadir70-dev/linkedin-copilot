# Hostinger VPS deployment (operator)

## Security model

- **Authentication:** every gateway route except `GET /health` requires `Authorization: Bearer <access key>`. Keys come from `GATEWAY_API_TOKENS` in the VPS `.env` (comma-separated, ≥32 chars, one per user so one can be revoked). The gateway refuses to start with no key unless `ALLOW_NO_AUTH=true`, which is for a laptop only — never set it on the VPS.
- **Only HTTPS is public.** Caddy (Compose profile `vps`) is the only service with public ports (80/443). It gets a TLS certificate for `DOMAIN` automatically and forwards to the gateway on the internal Docker network.
- **Redis, Ollama and the gateway's own port stay private:** their Compose ports bind to `127.0.0.1`. Keep 6379, 8080 and 11434 closed in the Hostinger firewall.
- **Rate limits:** 120 requests/min per IP overall and 8/min on the AI routes (`/ingest`, `/profile-ingest`, `/batch-ingest`); HTTP 429 + `Retry-After: 60` when exceeded. Tune with `RATE_LIMIT_RPM` / `RATE_LIMIT_GENERATION_RPM`. `TRUST_PROXY=true` is correct with the bundled Caddy (it sets `X-Real-IP`); don't enable it if port 8080 is reachable from the Internet.
- **CORS** is not authentication. Set `CORS_ALLOWED_ORIGINS=chrome-extension://<extension-id>` once the extension ID is known.
- **Human-only send** is unchanged: the extension fills the LinkedIn box after approval, and the user presses Send.

## Requirements

- Client-owned Hostinger VPS, Ubuntu 22.04/24.04, ≥8 GB RAM (16 GB recommended), ≥30 GB free disk.
- A hostname (for example `copilot.client-domain.com`) with a DNS **A record pointing to the VPS IP**.
- SSH access as a sudo user.

## Deploy

```bash
# 1. Docker + Compose plugin
curl -fsSL https://get.docker.com | sh

# 2. Code at the approved release commit
git clone https://github.com/Kadir70-dev/linkedin-copilot.git && cd linkedin-copilot
git checkout <approved-commit>

# 3. Private config (never commit .env)
cp .env.example .env && chmod 600 .env
openssl rand -hex 32          # run once per user; put the keys in GATEWAY_API_TOKENS
#   in .env set: GATEWAY_API_TOKENS=<key1>[,<key2>]  ALLOW_NO_AUTH=false
#                DOMAIN=copilot.client-domain.com   TRUST_PROXY=true
#                FAST_MODE=true on a CPU-only 8 GB VPS

# 4. Start (Caddy included) and pull models
docker compose --profile vps up -d --build
make pull-model
```

Hostinger firewall (hPanel → VPS → Firewall): allow inbound 22, 80 and 443 only.

## Verify before handing over

```bash
curl -s http://127.0.0.1:8080/health                         # {"ok":true}
curl -s -o /dev/null -w '%{http_code}\n' https://$DOMAIN/stats  # 401 — no key, rejected
make doctor                                                    # uses the first key from .env, never prints it
```

From another machine, confirm `nc -zv <vps-ip> 6379`, `8080` and `11434` all fail.

## Build the client extension

On the build machine (not the VPS is fine either):

```bash
python3 scripts/configure_extension.py https://copilot.client-domain.com
python3 scripts/prepare_cws.py      # safety checks + dist/linkedin-copilot-*.zip
```

This sets the gateway URL and the single matching host permission. Don't commit this change to `main` unless the repo should default to the client's host. Send the client the ZIP and their access key **separately** (key over a password manager or a secure channel, never in the repo or the same email as the ZIP). Then set `CORS_ALLOWED_ORIGINS` to the extension's `chrome-extension://<id>` shown in `chrome://extensions` and run `docker compose --profile vps up -d`.

## Operate

- Revoke a user: remove their key from `GATEWAY_API_TOKENS`, then `docker compose --profile vps up -d`.
- Back up the `gwdata` volume (SQLite) regularly; `ollama` holds re-downloadable models.
- Monitor RAM/disk, container health (`make doctor`) and 401/429 counts in `docker compose logs caddy gateway`.
