#!/bin/bash
# Start the PoliceTracker Web Server using gunicorn (production-friendly)

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

export WEB_PORT=${WEB_PORT:-8892}
export WEB_HOST=${WEB_HOST:-127.0.0.1}
export WEB_WORKERS=${WEB_WORKERS:-1}

echo "Starting PoliceTracker Web Server (gunicorn) on $WEB_HOST:$WEB_PORT..."
echo "Workers: $WEB_WORKERS"
echo ""

exec gunicorn \
    --workers "$WEB_WORKERS" \
    --bind "$WEB_HOST:$WEB_PORT" \
    --access-logfile "-" \
    --error-logfile "-" \
    wsgi:app
