#!/bin/bash
# Start the PoliceTracker listener (stream monitor + detection)

set -euo pipefail

cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
    echo "Virtual environment not found. Run ./setup.sh first."
    exit 1
fi

# Load environment variables from .env if present (not committed; safe for secrets)
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    source ".env"
    set +a
fi

source venv/bin/activate

LISTENER_PORT=${LISTENER_PORT:-8891}
if lsof -nP -iTCP:"$LISTENER_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $LISTENER_PORT is already in use; attempting to stop the existing listener..."
    pids="$(lsof -tiTCP:"$LISTENER_PORT" -sTCP:LISTEN 2>/dev/null || true)"
    if [ -n "$pids" ]; then
        kill -TERM $pids 2>/dev/null || true
        sleep 1
        pids2="$(lsof -tiTCP:"$LISTENER_PORT" -sTCP:LISTEN 2>/dev/null || true)"
        if [ -n "$pids2" ]; then
            kill -KILL $pids2 2>/dev/null || true
        fi
    fi
fi

echo "Starting PoliceTracker listener..."
echo "Config: config.local.yaml (overrides config.yaml if present)"
echo "Listener status: http://127.0.0.1:8891/status"
echo ""

exec python -u police_tracker.py
