# PoliceTracker

Automatic listener for police radio channels that monitors Broadcastify (and similar) streams, extracts important information, and sends alerts to a tracking application.

## Features

- Stream audio from multiple police radio channels simultaneously
- Keyword detection from transcribed audio
- Optional speech-to-text transcription (Whisper)
- Webhook/API integration for notifications
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

5. (Optional) Protect the dashboard + read APIs with Basic Auth:
```bash
export DASHBOARD_USER="admin"
export DASHBOARD_PASS="set-a-strong-password"
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
