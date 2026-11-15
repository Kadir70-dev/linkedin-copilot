# Hostinger VPS deployment notes

The gateway now has process-local per-IP throttling. The default limits are 120 requests/minute per IP for ordinary routes and 8 requests/minute for the AI-triggering routes `/ingest`, `/profile-ingest`, and `/batch-ingest`. Set `RATE_LIMIT_RPM` and `RATE_LIMIT_GENERATION_RPM` in the VPS `.env` to tune them. A rejected request returns HTTP 429 and `Retry-After: 60`.

## Before making it reachable

This app was designed to run locally. The gateway currently has no user login or API authentication. CORS and rate limiting do not authenticate callers; non-browser clients can ignore CORS. Keep the gateway private behind a VPN (recommended) or an authenticated access proxy until API authentication is added. Do not expose the gateway publicly as an unauthenticated API.

The Compose ports for the gateway, Redis, and Ollama are bound to `127.0.0.1`. Keep firewall rules closed for ports 8080, 6379, and 11434. Only the reverse proxy should be public, and only ports 80/443 should be open to the intended audience. Configure HTTPS before connecting the browser extension.

For rate limits behind a reverse proxy, set `TRUST_PROXY=true` only if the proxy is on the same VPS and overwrites `X-Real-IP` with the actual client address. Never enable it while port 8080 is directly reachable from the Internet. Set `CORS_ALLOWED_ORIGINS` to the exact extension origin, for example `chrome-extension://<extension-id>`; avoid `*` in production.

## VPS sizing and launch

Ollama model inference is CPU/RAM intensive. Use at least 8 GB RAM for the current Compose limits; 16 GB is preferable for smoother operation. Confirm available disk space for Docker images, the model files, and persistent volumes before downloading models.

1. Install Docker Engine and the Compose plugin on the client-owned Hostinger VPS.
2. Clone this repository and check out the approved release commit.
3. Create a private `.env` from `.env.example`; set the model profile, rate limits, proxy trust, and exact CORS origin.
4. Configure a TLS reverse proxy to forward to `127.0.0.1:8080`. Keep it private behind VPN/auth until the gateway implements caller authentication.
5. Run `docker compose up -d --build`, then pull the configured Ollama model and wait for health checks.
6. Verify locally on the VPS with `curl http://127.0.0.1:8080/health`. Confirm public scanning cannot reach ports 8080, 6379, or 11434.
7. Back up the Compose volumes, especially the SQLite gateway data and Ollama models. Monitor disk, RAM, container health, and 429 responses.

## Extension connection

The packaged extension currently points to `http://localhost:8080` and its manifest only grants that host. Before the client can use the VPS, configure the extension for the client’s final HTTPS hostname, add that hostname to the extension’s allowed hosts, package the updated extension, and configure its exact `chrome-extension://` origin in the gateway. This requires the client’s chosen domain and final extension ID.
