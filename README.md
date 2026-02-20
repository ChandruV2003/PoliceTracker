# PoliceTracker

Automatic listener for police radio channels that monitors Broadcastify (and similar) streams, extracts important information, and sends alerts to a tracking application.

## Features

- Stream audio from multiple police radio channels simultaneously
- Keyword detection from transcribed audio
- Optional speech-to-text transcription (Whisper)
- Webhook/API integration for notifications
- Utilitarian, mobile-friendly public dashboard (PIN-protected option)
- Live map view (v1 coarse channel centroids, v2 optional address geocoding)
- Lightweight resource usage (<5% CPU per channel)

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Ensure FFmpeg is installed and on your PATH

3. Configure channels in `config.local.yaml` (recommended; ignored by git). `config.yaml` can hold safe defaults.

4. Run:
```bash
python police_tracker.py
```

## Architecture

- Audio streaming via FFmpeg or direct HTTP streams
- Keyword matching on audio chunks
- Optional Whisper transcription
- REST API for status and alerts
- Webhook notifications for important events

## Resource Usage

- CPU: <5% per channel (without transcription)
- RAM: ~100MB per channel
- Network: ~32-64 kbps per stream

## Public Dashboard

Events are persisted to SQLite by default at `data/policetracker.db` (override with `DATABASE_PATH`).

1. Start the web server (dev):
```bash
./start_web_server.sh
```

Or run production-friendly (gunicorn):
```bash
./start_web_server_prod.sh
```

2. Open the dashboard locally:
```bash
http://localhost:8892
```

3. To make it public-facing, forward port `8892` on your router to this Mac.
See `PORT_FORWARDING.md` for step-by-step instructions.

For a recommended HTTPS setup, see `PUBLIC_DEPLOYMENT.md`.

4. (Optional but recommended) Set an API token so only your listener can post events:
```bash
export API_TOKEN="your-secret-token"
```

Then add the same token to `config.local.yaml` under `alerts.api_token` so the listener can authenticate.

5. (Optional) Protect the dashboard + read APIs with a PIN (Basic Auth prompt):
```bash
export DASHBOARD_PIN="84346"
```

Or use a full username/password:
```bash
export DASHBOARD_USER="admin"
export DASHBOARD_PASS="set-a-strong-password"
```

## Map + Geocoding (v1/v2)

- **v1 (coarse map):** the listener attaches approximate `lat/lon` per channel/region so events plot immediately (county/city centroid style).
- **v2 (refine):** the web server can optionally extract an address-like hint from the transcript and geocode it in the background.

By default, v2 uses the **US Census Geocoder** (`GEOCODER_PROVIDER=census`) and only attempts geocoding when the hint contains digits (street-address style). Results are cached in SQLite (`geocode_cache`) and written back to the event’s `lat/lon`.

Environment variables:
```bash
export ENABLE_GEOCODING="1"            # default: 1
export GEOCODER_PROVIDER="census"      # census|nominatim
export GEOCODER_TIMEOUT_SECONDS="3"
export GEOCODER_MIN_INTERVAL_SECONDS="1"
```

## Transcription

Keyword detection requires transcription. Enable it by setting:
```yaml
processing:
  use_transcription: true
  transcription_model: "base"
```

If you want transcription, install a Whisper backend:
```bash
pip install openai-whisper
```
