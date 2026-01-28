# PoliceTracker

Automatic listener for police radio channels that monitors Broadcastify (and similar) streams, extracts important information, and sends alerts to a tracking application.

## Features

- Stream audio from multiple police radio channels simultaneously
- Keyword detection and alerting
- Optional speech-to-text transcription
- Webhook/API integration for notifications
- Lightweight resource usage (<5% CPU per channel)

## Setup

1. Install dependencies:
```bash
pip install -r requirements.txt
```

2. Configure channels in `config.yaml` or via environment variables

3. Run:
```bash
python police_tracker.py
```

## Architecture

- Audio streaming via FFmpeg or direct HTTP streams
- Keyword matching on audio chunks
- Optional Whisper.cpp for transcription
- REST API for status and alerts
- Webhook notifications for important events

## Resource Usage

- CPU: <5% per channel (without transcription)
- RAM: ~100MB per channel
- Network: ~32-64 kbps per stream
