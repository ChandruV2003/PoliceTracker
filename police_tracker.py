#!/usr/bin/env python3
"""
PoliceTracker - Automatic police radio listener and alert system
"""
import os
import sys
import logging
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import List, Dict, Optional
import yaml
from flask import Flask, jsonify
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# Configuration
CONFIG_FILE = Path("config.yaml")
AUDIO_CACHE_DIR = Path("audio_cache")
AUDIO_CACHE_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
active_streams: Dict[str, subprocess.Popen] = {}
stream_status: Dict[str, Dict] = {}


class ChannelMonitor:
    """Monitors a single police radio channel"""
    
    def __init__(self, channel_config: Dict):
        self.name = channel_config.get("name", "Unknown")
        self.url = channel_config.get("url", "")
        self.keywords = channel_config.get("keywords", [])
        self.enabled = channel_config.get("enabled", True)
        self.process: Optional[subprocess.Popen] = None
        self.running = False
        
    def start(self):
        """Start monitoring this channel"""
        if not self.enabled or not self.url:
            logger.warning(f"Channel {self.name} is disabled or has no URL")
            return False
        
        logger.info(f"Starting monitor for channel: {self.name}")
        # TODO: Implement audio streaming and processing
        self.running = True
        return True
    
    def stop(self):
        """Stop monitoring this channel"""
        logger.info(f"Stopping monitor for channel: {self.name}")
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            except Exception as e:
                logger.error(f"Error stopping {self.name}: {e}")
        self.running = False
    
    def get_status(self) -> Dict:
        """Get current status of this channel"""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "running": self.running,
            "url": self.url,
            "keywords": self.keywords
        }


def load_config() -> Dict:
    """Load configuration from YAML file"""
    if not CONFIG_FILE.exists():
        logger.error(f"Config file not found: {CONFIG_FILE}")
        logger.info("Please copy config.yaml.example to config.yaml and configure it")
        sys.exit(1)
    
    with open(CONFIG_FILE, 'r') as f:
        return yaml.safe_load(f)


def start_monitors(config: Dict) -> List[ChannelMonitor]:
    """Start monitoring all enabled channels"""
    monitors = []
    channels = config.get("channels", [])
    
    for channel_config in channels:
        monitor = ChannelMonitor(channel_config)
        if monitor.start():
            monitors.append(monitor)
            stream_status[monitor.name] = monitor.get_status()
    
    return monitors


def stop_monitors(monitors: List[ChannelMonitor]):
    """Stop all monitors"""
    for monitor in monitors:
        monitor.stop()


@app.route('/status')
def status():
    """Get status of all channels"""
    return jsonify({
        "status": "running",
        "channels": [monitor.get_status() for monitor in active_monitors],
        "timestamp": time.time()
    })


@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy"})


def signal_handler(sig, frame):
    """Handle shutdown signals"""
    logger.info("Shutting down PoliceTracker...")
    stop_monitors(active_monitors)
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Global monitors list
active_monitors: List[ChannelMonitor] = []

if __name__ == "__main__":
    logger.info("Starting PoliceTracker...")
    
    # Load configuration
    config = load_config()
    
    # Start monitoring channels
    active_monitors = start_monitors(config)
    
    if not active_monitors:
        logger.warning("No channels enabled or configured")
    
    # Start Flask API server
    server_config = config.get("server", {})
    host = server_config.get("host", "0.0.0.0")
    port = server_config.get("port", 8891)
    
    logger.info(f"Starting API server on {host}:{port}")
    logger.info(f"Monitoring {len(active_monitors)} channel(s)")
    
    app.run(host=host, port=port, debug=server_config.get("debug", False), threaded=True)
