#!/usr/bin/env python3
"""
PoliceTracker Web Server - Public dashboard and API for live incident tracking
"""
import os
import json
import logging
import signal
import sqlite3
import sys
import time
from base64 import b64decode
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import List, Dict, Optional
from flask import Flask, render_template_string, jsonify, request, Response
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
API_TOKEN = os.environ.get("API_TOKEN")
DASHBOARD_USER = os.environ.get("DASHBOARD_USER")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS")


def require_dashboard_auth(handler):
    """Optional HTTP Basic Auth for the dashboard and read APIs."""

    @wraps(handler)
    def wrapper(*args, **kwargs):
        if not (DASHBOARD_USER and DASHBOARD_PASS):
            return handler(*args, **kwargs)

        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = b64decode(auth.split(" ", 1)[1]).decode("utf-8")
                username, password = decoded.split(":", 1)
                if username == DASHBOARD_USER and password == DASHBOARD_PASS:
                    return handler(*args, **kwargs)
            except Exception:
                pass

        return Response(
            "Unauthorized",
            401,
            {"WWW-Authenticate": 'Basic realm="PoliceTracker"'},
        )

    return wrapper

# Persistence
DB_PATH = Path(os.environ.get("DATABASE_PATH", "data/policetracker.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
db_lock = threading.Lock()
db_conn: sqlite3.Connection = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
db_conn.row_factory = sqlite3.Row
db_conn.execute("PRAGMA journal_mode=WAL;")
db_conn.execute("PRAGMA foreign_keys=ON;")

# In-memory event cache (dashboard queries still hit SQLite; this is for quick stats + sanity)
events: deque = deque(maxlen=10000)  # Keep last 10k events in memory
stats_lock = threading.Lock()

# Statistics
stats = {
    "total_events": 0,
    "events_by_channel": {},
    "events_by_keyword": {},
    "last_event_time": None,
    "start_time": time.time()
}


def init_db():
    """Initialize SQLite schema."""
    with db_lock:
        db_conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                channel TEXT NOT NULL,
                keywords TEXT,
                transcript TEXT,
                priority INTEGER,
                location TEXT,
                raw_data TEXT
            )
        """)
        db_conn.execute("CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events(timestamp)")
        db_conn.execute("CREATE INDEX IF NOT EXISTS idx_events_channel ON events(channel)")
        db_conn.execute("""
            CREATE TABLE IF NOT EXISTS event_keywords (
                event_id INTEGER NOT NULL,
                keyword TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
            )
        """)
        db_conn.execute("CREATE INDEX IF NOT EXISTS idx_event_keywords_keyword ON event_keywords(keyword)")
        db_conn.execute("CREATE INDEX IF NOT EXISTS idx_event_keywords_event_id ON event_keywords(event_id)")
        db_conn.commit()


def load_recent_events_into_memory():
    """Warm in-memory cache with recent events from SQLite."""
    with db_lock:
        rows = db_conn.execute(
            "SELECT id, timestamp, channel, keywords, transcript, priority, location FROM events ORDER BY timestamp DESC LIMIT ?",
            (events.maxlen,)
        ).fetchall()

    # Reverse: oldest first, so deque ends with newest
    with stats_lock:
        events.clear()
        for row in reversed(rows):
            events.append({
                "id": row["id"],
                "timestamp": row["timestamp"],
                "channel": row["channel"],
                "keywords": json.loads(row["keywords"]) if row["keywords"] else [],
                "transcript": row["transcript"] or "",
                "priority": row["priority"] if row["priority"] is not None else 5,
                "location": row["location"] or ""
            })


def rebuild_stats_from_db():
    """Rebuild aggregate stats from SQLite (used on startup)."""
    with db_lock:
        total_events_row = db_conn.execute("SELECT COUNT(*) AS c FROM events").fetchone()
        last_event_row = db_conn.execute("SELECT MAX(timestamp) AS t FROM events").fetchone()
        channel_rows = db_conn.execute("SELECT channel, COUNT(*) AS c FROM events GROUP BY channel").fetchall()
        keyword_rows = db_conn.execute("SELECT keyword, COUNT(*) AS c FROM event_keywords GROUP BY keyword").fetchall()

    total_events = int(total_events_row["c"]) if total_events_row else 0
    last_event_time = float(last_event_row["t"]) if last_event_row and last_event_row["t"] is not None else None
    events_by_channel = {row["channel"]: int(row["c"]) for row in channel_rows}
    events_by_keyword = {row["keyword"]: int(row["c"]) for row in keyword_rows}

    with stats_lock:
        stats["total_events"] = total_events
        stats["last_event_time"] = last_event_time
        stats["events_by_channel"] = events_by_channel
        stats["events_by_keyword"] = events_by_keyword


def persist_event(event: Dict) -> int:
    """Insert an event into SQLite. Returns the new event ID."""
    keywords_list = event.get("keywords", []) or []
    keywords_json = json.dumps(keywords_list)
    raw_data_json = json.dumps(event.get("raw_data", {}))

    with db_lock:
        cur = db_conn.execute(
            """
            INSERT INTO events (timestamp, channel, keywords, transcript, priority, location, raw_data)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                float(event.get("timestamp", time.time())),
                event.get("channel", ""),
                keywords_json,
                event.get("transcript", ""),
                int(event.get("priority", 5)),
                event.get("location", ""),
                raw_data_json
            )
        )
        event_id = int(cur.lastrowid)

        for kw in keywords_list:
            db_conn.execute(
                "INSERT INTO event_keywords (event_id, keyword) VALUES (?, ?)",
                (event_id, str(kw))
            )

        db_conn.commit()

    return event_id


def query_events(limit: int, channel: Optional[str] = None, keyword: Optional[str] = None, since: Optional[float] = None) -> List[Dict]:
    """Query events from SQLite with optional filters."""
    sql = "SELECT id, timestamp, channel, keywords, transcript, priority, location FROM events e WHERE 1=1"
    params: List[object] = []

    if channel:
        sql += " AND e.channel = ?"
        params.append(channel)

    if since is not None:
        sql += " AND e.timestamp >= ?"
        params.append(float(since))

    if keyword:
        kw = keyword.lower()
        kw_like = f"%{kw}%"
        sql += """
            AND (
                LOWER(COALESCE(e.transcript, '')) LIKE ?
                OR EXISTS (
                    SELECT 1 FROM event_keywords ek
                    WHERE ek.event_id = e.id AND LOWER(ek.keyword) LIKE ?
                )
            )
        """
        params.extend([kw_like, kw_like])

    sql += " ORDER BY e.timestamp DESC LIMIT ?"
    params.append(int(limit))

    with db_lock:
        rows = db_conn.execute(sql, params).fetchall()

    results: List[Dict] = []
    for row in rows:
        results.append({
            "id": row["id"],
            "timestamp": row["timestamp"],
            "channel": row["channel"],
            "keywords": json.loads(row["keywords"]) if row["keywords"] else [],
            "transcript": row["transcript"] or "",
            "priority": row["priority"] if row["priority"] is not None else 5,
            "location": row["location"] or ""
        })
    return results


# Initialize persistence + caches on import (works for both direct run and gunicorn)
init_db()
load_recent_events_into_memory()
rebuild_stats_from_db()
logger.info(f"Using SQLite database at: {DB_PATH}")


# Utilitarian (mobile-first) dashboard HTML template
DASHBOARD_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>PoliceTracker Dashboard</title>
  <style>
    :root {
      --bg: #f5f7fb;
      --panel: #ffffff;
      --text: #111827;
      --muted: #6b7280;
      --border: #e5e7eb;
      --accent: #2563eb;
      --danger: #dc2626;
      --warn: #d97706;
      --ok: #16a34a;
      --shadow: 0 1px 2px rgba(0,0,0,.06), 0 10px 24px rgba(0,0,0,.05);
      --radius: 12px;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
    }

    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      padding: 14px;
    }

    .container {
      max-width: 1100px;
      margin: 0 auto;
      display: grid;
      gap: 12px;
    }

    .panel {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
    }

    .topbar {
      padding: 14px 14px;
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      flex-wrap: wrap;
    }

    .brand h1 {
      margin: 0;
      font-size: 18px;
      line-height: 1.2;
      letter-spacing: 0.2px;
    }

    .subline {
      margin-top: 6px;
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: 13px;
    }

    .dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--ok);
      box-shadow: 0 0 0 2px rgba(22,163,74,.15);
      display: inline-block;
    }

    .dot.offline {
      background: var(--danger);
      box-shadow: 0 0 0 2px rgba(220,38,38,.15);
    }

    .kpis {
      display: grid;
      grid-auto-flow: column;
      gap: 10px;
      align-items: start;
    }

    .kpi {
      padding: 8px 10px;
      border: 1px solid var(--border);
      border-radius: 10px;
      min-width: 112px;
    }

    .kpi .label {
      font-size: 11px;
      color: var(--muted);
      text-transform: uppercase;
      letter-spacing: 0.6px;
    }

    .kpi .value {
      margin-top: 4px;
      font-family: var(--mono);
      font-size: 14px;
      font-weight: 700;
    }

    .controls {
      padding: 12px;
      display: grid;
      grid-template-columns: 1fr 220px 160px 160px auto;
      gap: 10px;
      position: sticky;
      top: 0;
      z-index: 10;
    }

    .controls input,
    .controls select {
      width: 100%;
      padding: 10px 10px;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: #fff;
      color: var(--text);
      font-size: 14px;
      outline: none;
    }

    .controls input:focus,
    .controls select:focus {
      border-color: rgba(37, 99, 235, .7);
      box-shadow: 0 0 0 4px rgba(37, 99, 235, .12);
    }

    .controls button {
      padding: 10px 12px;
      border-radius: 10px;
      border: 1px solid rgba(37, 99, 235, .35);
      background: var(--accent);
      color: #fff;
      font-weight: 700;
      font-size: 14px;
      cursor: pointer;
      white-space: nowrap;
    }

    .controls button:active {
      transform: translateY(1px);
    }

    .events {
      overflow: hidden;
    }

    .events-header {
      padding: 10px 12px;
      border-bottom: 1px solid var(--border);
      color: var(--muted);
      font-size: 12px;
      display: flex;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
    }

    .event {
      padding: 12px;
      border-bottom: 1px solid var(--border);
      display: grid;
      grid-template-columns: 180px 1fr;
      gap: 12px;
      align-items: start;
    }

    .event:last-child { border-bottom: none; }

    .when {
      font-family: var(--mono);
      font-size: 13px;
      color: var(--muted);
      line-height: 1.4;
    }

    .meta {
      margin-top: 8px;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 8px;
      border-radius: 999px;
      border: 1px solid var(--border);
      font-size: 12px;
      font-family: var(--mono);
      color: var(--muted);
      background: #fff;
    }

    .pill.prio-high { border-color: rgba(220,38,38,.35); color: var(--danger); background: rgba(220,38,38,.06); }
    .pill.prio-med  { border-color: rgba(217,119,6,.35); color: var(--warn); background: rgba(217,119,6,.08); }
    .pill.prio-low  { border-color: rgba(107,114,128,.35); color: #374151; background: rgba(107,114,128,.06); }

    .mainline {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
      align-items: baseline;
    }

    .channel {
      font-weight: 800;
      font-size: 14px;
      letter-spacing: 0.2px;
    }

    .location {
      color: var(--muted);
      font-size: 13px;
    }

    .transcript {
      margin-top: 6px;
      color: #1f2937;
      font-size: 13px;
      line-height: 1.5;
      display: -webkit-box;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
      overflow: hidden;
    }

    .event.expanded .transcript {
      -webkit-line-clamp: unset;
    }

    .badges {
      margin-top: 8px;
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      align-items: center;
    }

    .badge {
      background: rgba(17,24,39,.06);
      border: 1px solid rgba(17,24,39,.10);
      color: #374151;
      font-size: 12px;
      padding: 4px 8px;
      border-radius: 999px;
      font-family: var(--mono);
    }

    .actions {
      margin-top: 10px;
      display: flex;
      gap: 8px;
      align-items: center;
    }

    .linkbtn {
      border: 1px solid var(--border);
      background: #fff;
      color: var(--text);
      padding: 6px 10px;
      border-radius: 10px;
      cursor: pointer;
      font-size: 13px;
      font-weight: 700;
    }

    .empty {
      padding: 40px 12px;
      text-align: center;
      color: var(--muted);
    }

    @media (max-width: 900px) {
      .controls { grid-template-columns: 1fr 1fr; }
      .controls button { grid-column: 1 / -1; }
      .kpis { grid-auto-flow: row; width: 100%; }
    }

    @media (max-width: 640px) {
      body { padding: 10px; }
      .event { grid-template-columns: 1fr; }
      .controls { grid-template-columns: 1fr; position: static; }
      .kpi { min-width: unset; }
    }
  </style>
</head>
<body>
  <div class="container">
    <header class="panel topbar">
      <div class="brand">
        <h1>PoliceTracker</h1>
        <div class="subline">
          <span id="status-dot" class="dot"></span>
          <span id="status-text">Live</span>
          <span>•</span>
          <span>Updated <span id="last-update">--</span></span>
        </div>
      </div>
      <div class="kpis">
        <div class="kpi">
          <div class="label">Total Events</div>
          <div class="value" id="total-events">0</div>
        </div>
        <div class="kpi">
          <div class="label">Last Event</div>
          <div class="value" id="last-event">--</div>
        </div>
      </div>
    </header>

    <section class="panel controls">
      <input id="search" type="search" placeholder="Search keyword or transcript..." autocomplete="off" />
      <select id="channel">
        <option value="all">All channels</option>
      </select>
      <select id="priority">
        <option value="all">All priorities</option>
        <option value="high">High (8-10)</option>
        <option value="med">Medium (5-7)</option>
        <option value="low">Low (0-4)</option>
      </select>
      <select id="range">
        <option value="all">All time</option>
        <option value="1h">Last hour</option>
        <option value="24h">Last 24 hours</option>
      </select>
      <button id="refresh" type="button">Refresh</button>
    </section>

    <section class="panel events">
      <div class="events-header">
        <div><span id="result-count">0</span> events loaded</div>
        <div style="font-family: var(--mono);">GET /api/events</div>
      </div>
      <div id="events-list">
        <div class="empty">Loading...</div>
      </div>
    </section>
  </div>

  <script>
    const state = {
      search: "",
      channel: "all",
      priority: "all",
      range: "all"
    };

    let allEvents = [];
    let lastStats = {};
    let lastRefreshOk = true;

    function escapeHtml(text) {
      const div = document.createElement("div");
      div.textContent = String(text || "");
      return div.innerHTML;
    }

    function formatRel(timestamp) {
      const date = new Date(timestamp * 1000);
      const now = new Date();
      const diffMs = now - date;
      if (diffMs < 10 * 1000) return "just now";
      if (diffMs < 60 * 1000) return Math.floor(diffMs / 1000) + "s ago";
      if (diffMs < 60 * 60 * 1000) return Math.floor(diffMs / (60 * 1000)) + "m ago";
      if (diffMs < 24 * 60 * 60 * 1000) return Math.floor(diffMs / (60 * 60 * 1000)) + "h ago";
      return date.toLocaleString();
    }

    function priorityClass(p) {
      const pr = Number(p ?? 5);
      if (pr >= 8) return "prio-high";
      if (pr >= 5) return "prio-med";
      return "prio-low";
    }

    function applyPriorityFilter(events) {
      const f = state.priority;
      if (f === "high") return events.filter(e => (e.priority ?? 5) >= 8);
      if (f === "med") return events.filter(e => (e.priority ?? 5) >= 5 && (e.priority ?? 5) < 8);
      if (f === "low") return events.filter(e => (e.priority ?? 5) < 5);
      return events;
    }

    function rangeSince() {
      const now = Date.now() / 1000;
      if (state.range === "1h") return now - 3600;
      if (state.range === "24h") return now - 86400;
      return null;
    }

    function updateStatus(ok) {
      const dot = document.getElementById("status-dot");
      const text = document.getElementById("status-text");
      if (ok) {
        dot.classList.remove("offline");
        text.textContent = "Live";
      } else {
        dot.classList.add("offline");
        text.textContent = "Offline";
      }
    }

    function populateChannels(stats) {
      const select = document.getElementById("channel");
      const current = select.value || state.channel;
      const channels = Object.keys((stats && stats.events_by_channel) || {}).sort((a, b) => a.localeCompare(b));

      const options = ["<option value=\\"all\\">All channels</option>"];
      for (const ch of channels) {
        options.push(`<option value="${escapeHtml(ch)}">${escapeHtml(ch)}</option>`);
      }
      select.innerHTML = options.join("");
      select.value = current;
      state.channel = select.value;
    }

    function render() {
      const container = document.getElementById("events-list");
      let eventsToShow = applyPriorityFilter(allEvents);

      document.getElementById("result-count").textContent = String(eventsToShow.length);

      if (!eventsToShow.length) {
        container.innerHTML = `<div class="empty">No events match the current filters.</div>`;
        return;
      }

      container.innerHTML = eventsToShow.map(e => {
        const ts = Number(e.timestamp || 0);
        const abs = new Date(ts * 1000).toLocaleString();
        const pr = Number(e.priority ?? 5);
        const keywords = Array.isArray(e.keywords) ? e.keywords : [];
        const loc = e.location ? String(e.location) : "";
        const transcript = e.transcript ? String(e.transcript) : "";

        return `
          <div class="event" data-id="${escapeHtml(e.id || "")}">
            <div>
              <div class="when" title="${escapeHtml(abs)}">${escapeHtml(formatRel(ts))}<br/><span style="color: var(--muted);">${escapeHtml(abs)}</span></div>
              <div class="meta">
                <span class="pill ${priorityClass(pr)}">P${escapeHtml(pr)}</span>
                <span class="pill">#${escapeHtml(e.id || "-")}</span>
              </div>
            </div>
            <div>
              <div class="mainline">
                <div class="channel">${escapeHtml(e.channel || "Unknown")}</div>
                ${loc ? `<div class="location">${escapeHtml(loc)}</div>` : `<div class="location"></div>`}
              </div>
              ${transcript ? `<div class="transcript">"${escapeHtml(transcript)}"</div>` : `<div class="transcript" style="color: var(--muted);">No transcript</div>`}
              ${keywords.length ? `
                <div class="badges">
                  ${keywords.map(k => `<span class="badge">${escapeHtml(k)}</span>`).join("")}
                </div>
              ` : ``}
              <div class="actions">
                <button class="linkbtn" type="button" data-action="toggle">Details</button>
              </div>
            </div>
          </div>
        `;
      }).join("");

      container.querySelectorAll("button[data-action='toggle']").forEach(btn => {
        btn.addEventListener("click", (ev) => {
          const card = ev.target.closest(".event");
          if (!card) return;
          card.classList.toggle("expanded");
          btn.textContent = card.classList.contains("expanded") ? "Collapse" : "Details";
        });
      });
    }

    function updateKpis(stats) {
      document.getElementById("total-events").textContent = String((stats && stats.total_events) || 0);
      document.getElementById("last-update").textContent = formatRel(Date.now() / 1000);
      const lastEvent = (stats && stats.last_event_time) ? formatRel(stats.last_event_time) : "--";
      document.getElementById("last-event").textContent = lastEvent;
    }

    function debounce(fn, ms) {
      let t = null;
      return (...args) => {
        if (t) clearTimeout(t);
        t = setTimeout(() => fn(...args), ms);
      };
    }

    async function fetchEvents() {
      try {
        const params = new URLSearchParams();
        params.set("limit", "100");

        if (state.channel && state.channel !== "all") params.set("channel", state.channel);
        if (state.search) params.set("keyword", state.search);
        const since = rangeSince();
        if (since) params.set("since", String(since));

        const response = await fetch("/api/events?" + params.toString());
        if (!response.ok) throw new Error("HTTP " + response.status);
        const data = await response.json();

        allEvents = (data.events || []).slice().sort((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
        lastStats = data.stats || {};

        updateKpis(lastStats);
        populateChannels(lastStats);
        render();
        lastRefreshOk = true;
        updateStatus(true);
      } catch (e) {
        lastRefreshOk = false;
        updateStatus(false);
        document.getElementById("events-list").innerHTML = `<div class="empty">Error loading events. Check server logs.</div>`;
      }
    }

    document.getElementById("search").addEventListener("input", debounce((ev) => {
      state.search = ev.target.value.trim();
      fetchEvents();
    }, 250));

    document.getElementById("channel").addEventListener("change", (ev) => {
      state.channel = ev.target.value;
      fetchEvents();
    });

    document.getElementById("priority").addEventListener("change", (ev) => {
      state.priority = ev.target.value;
      render();
    });

    document.getElementById("range").addEventListener("change", (ev) => {
      state.range = ev.target.value;
      fetchEvents();
    });

    document.getElementById("refresh").addEventListener("click", () => fetchEvents());

    // Initial load + auto-refresh
    fetchEvents();
    setInterval(fetchEvents, 5000);
  </script>
</body>
</html>
"""


@app.route('/')
@require_dashboard_auth
def dashboard():
    """Main dashboard page"""
    return render_template_string(DASHBOARD_TEMPLATE)


@app.route('/api/events', methods=['GET'])
@require_dashboard_auth
def get_events():
    """Get events with optional filtering"""
    rebuild_stats_from_db()
    limit = int(request.args.get('limit', 100))
    channel = request.args.get('channel')
    keyword = request.args.get('keyword')
    since = request.args.get('since')  # Unix timestamp

    since_ts: Optional[float] = None
    if since:
        try:
            since_ts = float(since)
        except ValueError:
            return jsonify({"error": "Invalid 'since' timestamp"}), 400

    filtered_events = query_events(limit=limit, channel=channel, keyword=keyword, since=since_ts)

    with stats_lock:
        stats_snapshot = dict(stats)
    
    return jsonify({
        "events": filtered_events,
        "count": len(filtered_events),
        "stats": stats_snapshot
    })


@app.route('/api/events', methods=['POST'])
def create_event():
    """Receive new event from PoliceTracker"""
    try:
        if API_TOKEN:
            auth_header = request.headers.get("Authorization", "")
            api_key_header = request.headers.get("X-API-Key", "")
            if auth_header != f"Bearer {API_TOKEN}" and api_key_header != API_TOKEN:
                return jsonify({"error": "Unauthorized"}), 401

        data = request.get_json()
        
        if not data:
            return jsonify({"error": "No data provided"}), 400
        
        # Validate required fields
        if 'channel' not in data or 'timestamp' not in data:
            return jsonify({"error": "Missing required fields: channel, timestamp"}), 400
        
        # Build event payload
        event = {
            "timestamp": float(data.get('timestamp', time.time())),
            "channel": data.get('channel'),
            "keywords": data.get('keywords', []),
            "transcript": data.get('transcript', ''),
            "priority": data.get('priority', 5),
            "location": data.get('location', ''),
            "raw_data": data
        }

        event_id = persist_event(event)
        event["id"] = event_id
        
        # Add to in-memory cache + update stats
        with stats_lock:
            events.append(event)
            stats["total_events"] = int(stats.get("total_events", 0)) + 1
            
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
@require_dashboard_auth
def get_stats():
    """Get statistics"""
    rebuild_stats_from_db()
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
    try:
        with db_lock:
            db_conn.close()
    except Exception:
        pass
    sys.exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


if __name__ == "__main__":
    port = int(os.environ.get("WEB_PORT", 8892))
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    
    logger.info(f"Starting PoliceTracker Web Server on {host}:{port}")
    logger.info(f"Dashboard available at: http://{host}:{port}")
    logger.info(f"API available at: http://{host}:{port}/api/events")
    if API_TOKEN:
        logger.info("API token protection enabled for POST /api/events")
    
    app.run(host=host, port=port, debug=False, threaded=True)
