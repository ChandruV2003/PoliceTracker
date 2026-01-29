#!/usr/bin/env python3
"""
PoliceTracker Web Server - Public dashboard and API for live incident tracking
"""
import os
import json
import logging
import signal
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional
from flask import Flask, render_template_string, jsonify, request
from flask_cors import CORS
from collections import deque
import threading

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)  # Allow cross-origin requests for public access

# In-memory event storage (can be upgraded to SQLite/Postgres later)
events: deque = deque(maxlen=10000)  # Keep last 10k events
stats_lock = threading.Lock()

# Statistics
stats = {
    "total_events": 0,
    "events_by_channel": {},
    "events_by_keyword": {},
    "last_event_time": None,
    "start_time": time.time()
}


# Modern dashboard HTML template
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>PoliceTracker - Live Incident Dashboard</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: #333;
            min-height: 100vh;
            padding: 20px;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
        }
        
        .header {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 16px;
            padding: 24px 32px;
            margin-bottom: 24px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 16px;
        }
        
        .header h1 {
            font-size: 2em;
            font-weight: 700;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            background-clip: text;
        }
        
        .stats {
            display: flex;
            gap: 16px;
            flex-wrap: wrap;
        }
        
        .stat-card {
            background: rgba(255, 255, 255, 0.9);
            padding: 12px 20px;
            border-radius: 12px;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.1);
        }
        
        .stat-label {
            font-size: 0.85em;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }
        
        .stat-value {
            font-size: 1.5em;
            font-weight: 700;
            color: #667eea;
            margin-top: 4px;
        }
        
        .status-indicator {
            display: inline-block;
            width: 12px;
            height: 12px;
            border-radius: 50%;
            background: #10b981;
            box-shadow: 0 0 10px #10b981;
            animation: pulse 2s infinite;
            margin-right: 8px;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        
        .controls {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 16px;
            padding: 20px;
            margin-bottom: 24px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
            align-items: center;
        }
        
        .filter-btn {
            padding: 8px 16px;
            border: 2px solid #667eea;
            background: white;
            color: #667eea;
            border-radius: 8px;
            cursor: pointer;
            font-weight: 600;
            transition: all 0.2s;
        }
        
        .filter-btn:hover {
            background: #667eea;
            color: white;
        }
        
        .filter-btn.active {
            background: #667eea;
            color: white;
        }
        
        .events-container {
            background: rgba(255, 255, 255, 0.95);
            backdrop-filter: blur(10px);
            border-radius: 16px;
            padding: 24px;
            box-shadow: 0 8px 32px rgba(0, 0, 0, 0.1);
            max-height: 70vh;
            overflow-y: auto;
        }
        
        .event-card {
            background: white;
            border-left: 4px solid #667eea;
            border-radius: 12px;
            padding: 20px;
            margin-bottom: 16px;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
            transition: transform 0.2s, box-shadow 0.2s;
            animation: slideIn 0.3s ease-out;
        }
        
        .event-card:hover {
            transform: translateY(-2px);
            box-shadow: 0 6px 24px rgba(0, 0, 0, 0.12);
        }
        
        .event-card.priority-high {
            border-left-color: #ef4444;
            background: linear-gradient(to right, #fef2f2 0%, white 10%);
        }
        
        .event-card.priority-medium {
            border-left-color: #f59e0b;
            background: linear-gradient(to right, #fffbeb 0%, white 10%);
        }
        
        @keyframes slideIn {
            from {
                opacity: 0;
                transform: translateX(-20px);
            }
            to {
                opacity: 1;
                transform: translateX(0);
            }
        }
        
        .event-header {
            display: flex;
            justify-content: space-between;
            align-items: start;
            margin-bottom: 12px;
            flex-wrap: wrap;
            gap: 12px;
        }
        
        .event-channel {
            font-weight: 700;
            color: #667eea;
            font-size: 1.1em;
        }
        
        .event-time {
            color: #666;
            font-size: 0.9em;
        }
        
        .event-keywords {
            display: flex;
            gap: 8px;
            flex-wrap: wrap;
            margin-top: 12px;
        }
        
        .keyword-badge {
            background: #667eea;
            color: white;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 0.85em;
            font-weight: 600;
        }
        
        .event-transcript {
            margin-top: 12px;
            color: #555;
            line-height: 1.6;
            font-style: italic;
        }
        
        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: #999;
        }
        
        .empty-state h2 {
            font-size: 1.5em;
            margin-bottom: 12px;
        }
        
        .loading {
            text-align: center;
            padding: 40px;
            color: #667eea;
        }
        
        ::-webkit-scrollbar {
            width: 8px;
        }
        
        ::-webkit-scrollbar-track {
            background: #f1f1f1;
            border-radius: 10px;
        }
        
        ::-webkit-scrollbar-thumb {
            background: #667eea;
            border-radius: 10px;
        }
        
        ::-webkit-scrollbar-thumb:hover {
            background: #5568d3;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1>🚔 PoliceTracker</h1>
                <p style="color: #666; margin-top: 8px;">Live Incident Monitoring Dashboard</p>
            </div>
            <div class="stats">
                <div class="stat-card">
                    <div class="stat-label">Status</div>
                    <div class="stat-value">
                        <span class="status-indicator"></span>
                        <span id="status-text">Live</span>
                    </div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Total Events</div>
                    <div class="stat-value" id="total-events">0</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">Last Update</div>
                    <div class="stat-value" id="last-update" style="font-size: 1em;">--</div>
                </div>
            </div>
        </div>
        
        <div class="controls">
            <button class="filter-btn active" onclick="filterEvents('all')">All Events</button>
            <button class="filter-btn" onclick="filterEvents('high')">High Priority</button>
            <button class="filter-btn" onclick="filterEvents('medium')">Medium Priority</button>
            <button class="filter-btn" onclick="filterEvents('low')">Low Priority</button>
            <button class="filter-btn" onclick="filterEvents('recent')">Last Hour</button>
        </div>
        
        <div class="events-container">
            <div id="events-list">
                <div class="loading">Loading events...</div>
            </div>
        </div>
    </div>
    
    <script>
        let allEvents = [];
        let currentFilter = 'all';
        
        function formatTime(timestamp) {
            const date = new Date(timestamp * 1000);
            const now = new Date();
            const diff = now - date;
            
            if (diff < 60000) return 'Just now';
            if (diff < 3600000) return Math.floor(diff / 60000) + ' min ago';
            if (diff < 86400000) return Math.floor(diff / 3600000) + ' hr ago';
            return date.toLocaleString();
        }
        
        function getPriorityClass(priority) {
            if (priority >= 8) return 'priority-high';
            if (priority >= 5) return 'priority-medium';
            return '';
        }
        
        function renderEvents(eventsToShow) {
            const container = document.getElementById('events-list');
            
            if (eventsToShow.length === 0) {
                container.innerHTML = `
                    <div class="empty-state">
                        <h2>No events found</h2>
                        <p>Events will appear here as they are detected</p>
                    </div>
                `;
                return;
            }
            
            container.innerHTML = eventsToShow.map(event => `
                <div class="event-card ${getPriorityClass(event.priority || 5)}">
                    <div class="event-header">
                        <div>
                            <div class="event-channel">${escapeHtml(event.channel || 'Unknown')}</div>
                            <div class="event-time">${formatTime(event.timestamp)}</div>
                        </div>
                    </div>
                    ${event.transcript ? `<div class="event-transcript">"${escapeHtml(event.transcript)}"</div>` : ''}
                    ${event.keywords && event.keywords.length > 0 ? `
                        <div class="event-keywords">
                            ${event.keywords.map(kw => `<span class="keyword-badge">${escapeHtml(kw)}</span>`).join('')}
                        </div>
                    ` : ''}
                </div>
            `).join('');
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        function filterEvents(filter) {
            currentFilter = filter;
            
            // Update button states
            document.querySelectorAll('.filter-btn').forEach(btn => {
                btn.classList.remove('active');
            });
            event.target.classList.add('active');
            
            let filtered = [...allEvents];
            
            if (filter === 'high') {
                filtered = filtered.filter(e => (e.priority || 5) >= 8);
            } else if (filter === 'medium') {
                filtered = filtered.filter(e => (e.priority || 5) >= 5 && (e.priority || 5) < 8);
            } else if (filter === 'low') {
                filtered = filtered.filter(e => (e.priority || 5) < 5);
            } else if (filter === 'recent') {
                const oneHourAgo = Date.now() / 1000 - 3600;
                filtered = filtered.filter(e => e.timestamp >= oneHourAgo);
            }
            
            renderEvents(filtered);
        }
        
        async function fetchEvents() {
            try {
                const response = await fetch('/api/events?limit=100');
                const data = await response.json();
                
                allEvents = data.events || [];
                allEvents.sort((a, b) => b.timestamp - a.timestamp);
                
                document.getElementById('total-events').textContent = data.stats.total_events || 0;
                document.getElementById('last-update').textContent = formatTime(Date.now() / 1000);
                
                // Re-apply current filter
                if (currentFilter === 'all') {
                    renderEvents(allEvents);
                } else {
                    filterEvents(currentFilter);
                }
            } catch (error) {
                console.error('Error fetching events:', error);
            }
        }
        
        // Initial load
        fetchEvents();
        
        // Auto-refresh every 5 seconds
        setInterval(fetchEvents, 5000);
    </script>
</body>
</html>
"""


@app.route('/')
def dashboard():
    """Main dashboard page"""
    return render_template_string(DASHBOARD_TEMPLATE)


@app.route('/api/events', methods=['GET'])
def get_events():
    """Get events with optional filtering"""
    limit = int(request.args.get('limit', 100))
    channel = request.args.get('channel')
    keyword = request.args.get('keyword')
    since = request.args.get('since')  # Unix timestamp
    
    with stats_lock:
        filtered_events = list(events)
    
    # Apply filters
    if channel:
        filtered_events = [e for e in filtered_events if e.get('channel') == channel]
    
    if keyword:
        filtered_events = [e for e in filtered_events 
                          if keyword.lower() in str(e.get('keywords', [])).lower() or
                          keyword.lower() in str(e.get('transcript', '')).lower()]
    
    if since:
        since_ts = float(since)
        filtered_events = [e for e in filtered_events if e.get('timestamp', 0) >= since_ts]
    
    # Sort by timestamp (newest first) and limit
    filtered_events.sort(key=lambda x: x.get('timestamp', 0), reverse=True)
    filtered_events = filtered_events[:limit]
    
    return jsonify({
        "events": filtered_events,
        "count": len(filtered_events),
        "stats": stats
    })


@app.route('/api/events', methods=['POST'])
def create_event():
    """Receive new event from PoliceTracker"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        # Validate required fields
        if 'channel' not in data or 'timestamp' not in data:
            return jsonify({"error": "Missing required fields: channel, timestamp"}), 400
        
        # Add event ID and ensure timestamp
        event = {
            "id": len(events) + 1,
            "timestamp": data.get('timestamp', time.time()),
            "channel": data.get('channel'),
            "keywords": data.get('keywords', []),
            "transcript": data.get('transcript', ''),
            "priority": data.get('priority', 5),
            "location": data.get('location', ''),
            "raw_data": data
        }
        
        # Add to events list
        with stats_lock:
            events.append(event)
            stats["total_events"] += 1
            
            # Update channel stats
            channel = event['channel']
            if channel not in stats["events_by_channel"]:
                stats["events_by_channel"][channel] = 0
            stats["events_by_channel"][channel] += 1
            
            # Update keyword stats
            for keyword in event.get('keywords', []):
                if keyword not in stats["events_by_keyword"]:
                    stats["events_by_keyword"][keyword] = 0
                stats["events_by_keyword"][keyword] += 1
            
            stats["last_event_time"] = event['timestamp']
        
        logger.info(f"Received event from {event['channel']}: {event.get('keywords', [])}")
        
        return jsonify({"status": "success", "event_id": event["id"]}), 201
        
    except Exception as e:
        logger.error(f"Error processing event: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/stats')
def get_stats():
    """Get statistics"""
    with stats_lock:
        uptime = time.time() - stats["start_time"]
        return jsonify({
            **stats,
            "uptime_seconds": uptime,
            "uptime_formatted": str(timedelta(seconds=int(uptime))),
            "events_in_memory": len(events)
        })


@app.route('/api/health')
def health():
    """Health check"""
    return jsonify({"status": "healthy", "timestamp": time.time()})


def signal_handler(sig, frame):
    """Handle shutdown signals"""
    logger.info("Shutting down web server...")
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


if __name__ == "__main__":
    port = int(os.environ.get("WEB_PORT", 8892))
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    
    logger.info(f"Starting PoliceTracker Web Server on {host}:{port}")
    logger.info(f"Dashboard available at: http://{host}:{port}")
    logger.info(f"API available at: http://{host}:{port}/api/events")
    
    app.run(host=host, port=port, debug=False, threaded=True)
