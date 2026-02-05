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

echo "Starting PoliceTracker listener..."
echo "Config: config.local.yaml (overrides config.yaml if present)"
echo "Listener status: http://127.0.0.1:8891/status"
echo ""

exec python -u police_tracker.py

