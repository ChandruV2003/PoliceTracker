# Public Deployment (HTTPS)

This project can be exposed publicly in two ways:

1. Quick/temporary (HTTP + port forward `8892`) — simplest, not recommended for long-term public use.
2. Recommended (HTTPS + reverse proxy) — uses a real web server and TLS.

## Recommended: HTTPS With Caddy

### Requirements

- A domain name you control (Let's Encrypt cannot issue certs for a raw IP address).
- Router port forwards:
  - External `80` -> this Mac `80`
  - External `443` -> this Mac `443`
- DNS `A` record pointing your domain to your public IP.

### 1) Run PoliceTracker Web Server Locally (Gunicorn)

```bash
cd /Users/c.s.d.v.r.s./Developer/PoliceTracker
source venv/bin/activate
pip install -r requirements.txt

# Recommended: protect event ingestion (POST /api/events)
export API_TOKEN="set-a-long-random-token"

# Keep the app bound to localhost; Caddy will be the public entrypoint.
export WEB_HOST=127.0.0.1
export WEB_PORT=8892

./start_web_server_prod.sh
```

### 2) Install And Run Caddy

If you use Homebrew:

```bash
brew install caddy
```

Create a `Caddyfile` from `Caddyfile.example` and replace `yourdomain.com` with your real domain.

Then run:

```bash
caddy run --config ./Caddyfile
```

Once your ports/DNS are correct, Caddy will automatically get and renew HTTPS certificates.

### 3) Point The Listener At The Public URL (Optional)

If your listener (`police_tracker.py`) runs on another machine, set:

- `alerts.api_endpoint` to `https://yourdomain.com/api/events`
- `alerts.api_token` to the same token you used in `API_TOKEN`

## Quick Option: HTTP + Port Forward 8892

This keeps things simple, but it’s not encrypted (anyone can see traffic), and the Flask dev server is not meant for production.

```bash
./start_web_server.sh
```

Then forward external port `8892` -> this Mac `8892`.

## Notes / Gotchas

- If your ISP uses CGNAT, router port forwarding won’t work. In that case, use a tunnel (Cloudflare Tunnel, Tailscale Funnel, ngrok, etc.).
- If you enable `API_TOKEN`, only event ingestion (`POST /api/events`) is protected.
- To protect the dashboard + read APIs, set `DASHBOARD_PIN` (PIN mode) or `DASHBOARD_USER` + `DASHBOARD_PASS` (HTTP Basic Auth).
