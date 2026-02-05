#!/bin/bash
# Start the PoliceTracker Web Server

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

# Set port (default 8892, can override with WEB_PORT env var)
export WEB_PORT=${WEB_PORT:-8892}
export WEB_HOST=${WEB_HOST:-0.0.0.0}

echo "Starting PoliceTracker Web Server on port $WEB_PORT..."
echo "Dashboard: http://localhost:$WEB_PORT"
echo "API: http://localhost:$WEB_PORT/api/events"
echo ""
echo "To expose to internet:"
echo "1. Forward port $WEB_PORT on your router to this Mac's IP"
echo "2. Access via: http://YOUR_PUBLIC_IP:$WEB_PORT"
echo ""

python web_server.py
