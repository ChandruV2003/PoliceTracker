#!/bin/bash
# Setup script for PoliceTracker

set -e

echo "Setting up PoliceTracker..."

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Copy example config if config doesn't exist
if [ ! -f "config.yaml" ]; then
    echo "Creating config.yaml from example..."
    cp config.yaml.example config.yaml
    echo "Please edit config.yaml with your channel URLs and settings"
fi

# Create audio cache directory
mkdir -p audio_cache

echo ""
echo "Setup complete!"
echo ""
echo "Next steps:"
echo "1. Edit config.yaml with your channel URLs and keywords"
echo "2. Run: source venv/bin/activate && python police_tracker.py"
echo ""
