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
import queue
import re
from base64 import b64decode
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import List, Dict, Optional
from flask import Flask, render_template_string, jsonify, request, Response
from flask_cors import CORS
from collections import deque
import threading
from dotenv import load_dotenv
import requests

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

load_dotenv()  # Allow configuring via .env in production

app = Flask(__name__)
CORS(app)  # Allow cross-origin requests for public access
API_TOKEN = os.environ.get("API_TOKEN")
DASHBOARD_USER = os.environ.get("DASHBOARD_USER")
DASHBOARD_PASS = os.environ.get("DASHBOARD_PASS")
DASHBOARD_PIN = os.environ.get("DASHBOARD_PIN")

# Optional: background geocoding (v2). Uses a public service by default, so keep requests low.
ENABLE_GEOCODING = os.environ.get("ENABLE_GEOCODING", "1") == "1"
GEOCODER_PROVIDER = os.environ.get("GEOCODER_PROVIDER", "census")
GEOCODER_TIMEOUT_SECONDS = float(os.environ.get("GEOCODER_TIMEOUT_SECONDS", "3"))
GEOCODER_MIN_INTERVAL_SECONDS = float(os.environ.get("GEOCODER_MIN_INTERVAL_SECONDS", "1"))
GEOCODER_USER_AGENT = os.environ.get(
    "GEOCODER_USER_AGENT",
    "PoliceTracker/1.0 (public dashboard; contact: admin@example.com)",
)

# Optional: proxy listener status into the dashboard (client devices can't reach server-local 127.0.0.1)
LISTENER_STATUS_URL = os.environ.get("LISTENER_STATUS_URL", "http://127.0.0.1:8891/status")

AUTH_WINDOW_SECONDS = int(os.environ.get("AUTH_WINDOW_SECONDS", "300"))
AUTH_MAX_FAILURES = int(os.environ.get("AUTH_MAX_FAILURES", "8"))
AUTH_LOCKOUT_SECONDS = int(os.environ.get("AUTH_LOCKOUT_SECONDS", "600"))
auth_lock = threading.Lock()
auth_failures: Dict[str, List[float]] = {}
auth_locked_until: Dict[str, float] = {}


def require_dashboard_auth(handler):
    """Optional HTTP Basic Auth for the dashboard and read APIs."""

    @wraps(handler)
    def wrapper(*args, **kwargs):
        pin_mode = bool(DASHBOARD_PIN)
        userpass_mode = bool(DASHBOARD_USER and DASHBOARD_PASS)
        if not (pin_mode or userpass_mode):
            return handler(*args, **kwargs)

        client_ip = request.remote_addr or "unknown"
        now = time.time()

        with auth_lock:
            locked_until = auth_locked_until.get(client_ip, 0)
            if now < locked_until:
                retry_after = max(1, int(locked_until - now))
                return Response(
                    "Too Many Requests",
                    429,
                    {"Retry-After": str(retry_after)},
                )

        auth = request.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                decoded = b64decode(auth.split(" ", 1)[1]).decode("utf-8")
                username, password = decoded.split(":", 1)
                if pin_mode and password == DASHBOARD_PIN:
                    with auth_lock:
                        auth_failures.pop(client_ip, None)
                        auth_locked_until.pop(client_ip, None)
                    return handler(*args, **kwargs)
                if userpass_mode and username == DASHBOARD_USER and password == DASHBOARD_PASS:
                    with auth_lock:
                        auth_failures.pop(client_ip, None)
                        auth_locked_until.pop(client_ip, None)
                    return handler(*args, **kwargs)
            except Exception:
                pass

        # Track failures and lock out obvious brute-force attempts.
        with auth_lock:
            failures = auth_failures.get(client_ip, [])
            failures = [t for t in failures if now - t <= AUTH_WINDOW_SECONDS]
            failures.append(now)
            auth_failures[client_ip] = failures
            if len(failures) >= AUTH_MAX_FAILURES:
                auth_failures.pop(client_ip, None)
                auth_locked_until[client_ip] = now + AUTH_LOCKOUT_SECONDS

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
    """Initialize SQLite schema + light migrations."""
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
                units TEXT,
                lat REAL,
                lon REAL,
                geo_source TEXT,
                geo_confidence REAL,
                geo_query TEXT,
                geo_updated_at REAL,
                raw_data TEXT
            )
        """)

        # Add columns for older DBs (SQLite has no IF NOT EXISTS for columns).
        existing_cols = {
            row["name"]
            for row in db_conn.execute("PRAGMA table_info(events)").fetchall()
        }
        migrations = {
            "units": "ALTER TABLE events ADD COLUMN units TEXT",
            "lat": "ALTER TABLE events ADD COLUMN lat REAL",
            "lon": "ALTER TABLE events ADD COLUMN lon REAL",
            "geo_source": "ALTER TABLE events ADD COLUMN geo_source TEXT",
            "geo_confidence": "ALTER TABLE events ADD COLUMN geo_confidence REAL",
            "geo_query": "ALTER TABLE events ADD COLUMN geo_query TEXT",
            "geo_updated_at": "ALTER TABLE events ADD COLUMN geo_updated_at REAL",
        }
        for col, ddl in migrations.items():
            if col not in existing_cols:
                db_conn.execute(ddl)

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

        db_conn.execute("""
            CREATE TABLE IF NOT EXISTS event_units (
                event_id INTEGER NOT NULL,
                unit TEXT NOT NULL,
                FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
            )
        """)
        db_conn.execute("CREATE INDEX IF NOT EXISTS idx_event_units_unit ON event_units(unit)")
        db_conn.execute("CREATE INDEX IF NOT EXISTS idx_event_units_event_id ON event_units(event_id)")

        db_conn.execute("""
            CREATE TABLE IF NOT EXISTS geocode_cache (
                query TEXT PRIMARY KEY,
                lat REAL NOT NULL,
                lon REAL NOT NULL,
                confidence REAL,
                provider TEXT,
                created_at REAL NOT NULL
            )
        """)

        db_conn.execute("""
            CREATE TABLE IF NOT EXISTS event_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE
            )
        """)
        db_conn.execute("CREATE INDEX IF NOT EXISTS idx_event_feedback_event_id ON event_feedback(event_id)")
        db_conn.execute("CREATE INDEX IF NOT EXISTS idx_event_feedback_label ON event_feedback(label)")

        db_conn.commit()


def load_recent_events_into_memory():
    """Warm in-memory cache with recent events from SQLite."""
    with db_lock:
        rows = db_conn.execute(
            "SELECT id, timestamp, channel, keywords, transcript, priority, location, units, lat, lon, geo_source, geo_confidence, geo_query FROM events ORDER BY timestamp DESC LIMIT ?",
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
                "location": row["location"] or "",
                "units": json.loads(row["units"]) if row["units"] else [],
                "lat": row["lat"],
                "lon": row["lon"],
                "geo_source": row["geo_source"],
                "geo_confidence": row["geo_confidence"],
                "geo_query": row["geo_query"],
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
    units_list = event.get("units", []) or []
    units_json = json.dumps(units_list)
    raw_data_json = json.dumps(event.get("raw_data", {}))

    with db_lock:
        cur = db_conn.execute(
            """
            INSERT INTO events (timestamp, channel, keywords, transcript, priority, location, units, lat, lon, geo_source, geo_confidence, geo_query, geo_updated_at, raw_data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                float(event.get("timestamp", time.time())),
                event.get("channel", ""),
                keywords_json,
                event.get("transcript", ""),
                int(event.get("priority", 5)),
                event.get("location", ""),
                units_json,
                event.get("lat"),
                event.get("lon"),
                event.get("geo_source"),
                event.get("geo_confidence"),
                event.get("geo_query"),
                event.get("geo_updated_at"),
                raw_data_json
            )
        )
        event_id = int(cur.lastrowid)

        for kw in keywords_list:
            db_conn.execute(
                "INSERT INTO event_keywords (event_id, keyword) VALUES (?, ?)",
                (event_id, str(kw))
            )

        for u in units_list:
            db_conn.execute(
                "INSERT INTO event_units (event_id, unit) VALUES (?, ?)",
                (event_id, str(u)),
            )

        db_conn.commit()

    return event_id


def query_events(limit: int, channel: Optional[str] = None, keyword: Optional[str] = None, since: Optional[float] = None) -> List[Dict]:
    """Query events from SQLite with optional filters."""
    sql = """
        SELECT
            e.id,
            e.timestamp,
            e.channel,
            e.keywords,
            e.transcript,
            e.priority,
            e.location,
            e.units,
            e.lat,
            e.lon,
            e.geo_source,
            e.geo_confidence,
            e.geo_query,
            COALESCE(fb.up, 0) AS feedback_up,
            COALESCE(fb.down, 0) AS feedback_down
        FROM events e
        LEFT JOIN (
            SELECT
                event_id,
                SUM(CASE WHEN label = 'up' THEN 1 ELSE 0 END) AS up,
                SUM(CASE WHEN label = 'down' THEN 1 ELSE 0 END) AS down
            FROM event_feedback
            GROUP BY event_id
        ) fb ON fb.event_id = e.id
        WHERE 1=1
    """
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
                OR EXISTS (
                    SELECT 1 FROM event_units eu
                    WHERE eu.event_id = e.id AND LOWER(eu.unit) LIKE ?
                )
            )
        """
        params.extend([kw_like, kw_like, kw_like])

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
            "location": row["location"] or "",
            "units": json.loads(row["units"]) if row["units"] else [],
            "lat": row["lat"],
            "lon": row["lon"],
            "geo_source": row["geo_source"],
            "geo_confidence": row["geo_confidence"],
            "geo_query": row["geo_query"],
            "feedback_up": int(row["feedback_up"] or 0),
            "feedback_down": int(row["feedback_down"] or 0),
        })
    return results


def extract_unit_mentions(transcript: str) -> List[str]:
    """Extract likely unit identifiers from transcripts (best-effort).

    This is heuristic and intentionally conservative to avoid noise.
    """
    t = (transcript or "").strip()
    if not t:
        return []

    # Examples:
    # - "engine 391", "unit 12", "car 5", "medic 2", "ladder 1"
    # - "Unit five, zero, three" -> "unit 503" (whisper tiny often spells digits)
    # - "EMS-6A", "LF-109", "W815"
    unit_num_re = re.compile(
        r"\b(?P<kind>unit|car|adam|engine|medic|rescue|ladder|truck|squad|chief|station)\s*#?-?\s*(?P<num>\d{1,4}[a-z]?)\b",
        re.I,
    )

    digit_words = {
        "zero": "0",
        "oh": "0",
        "o": "0",
        "one": "1",
        "two": "2",
        "three": "3",
        "four": "4",
        "five": "5",
        "six": "6",
        "seven": "7",
        "eight": "8",
        "nine": "9",
    }
    digit_word_re = re.compile(
        r"\b(?P<kind>unit|car|adam|engine|medic|ladder|truck|station)\s+(?P<digits>(?:zero|oh|o|one|two|three|four|five|six|seven|eight|nine)(?:[\s,.-]+(?:zero|oh|o|one|two|three|four|five|six|seven|eight|nine)){1,6})\b",
        re.I,
    )

    hyphen_code_re = re.compile(r"\b([A-Za-z]{1,6}-\d{1,4}[A-Za-z]?)\b")
    alnum_code_re = re.compile(r"\b([A-Za-z]{1,3}\d{2,4}[A-Za-z]?)\b")

    out: List[str] = []
    seen = set()

    def add(val: str) -> None:
        key = (val or "").strip().lower()
        if not key or key in seen:
            return
        seen.add(key)
        out.append(val.strip())

    for m in unit_num_re.finditer(t):
        kind = m.group("kind").lower()
        num = m.group("num").lower()
        add(f"{kind} {num}")

    for m in digit_word_re.finditer(t):
        kind = m.group("kind").lower()
        digits_raw = m.group("digits").lower()
        parts = re.split(r"[\s,.-]+", digits_raw)
        digits = "".join(digit_words[p] for p in parts if p in digit_words)
        if len(digits) >= 2:
            add(f"{kind} {digits}")

    for m in hyphen_code_re.finditer(t):
        code = m.group(1)
        prefix = code.split("-", 1)[0].lower()
        if prefix in {"i", "us", "rt", "route"}:
            continue
        add(code.upper())

    for m in alnum_code_re.finditer(t):
        code = m.group(1)
        prefix = re.match(r"[A-Za-z]+", code)
        if prefix and prefix.group(0).lower() in {"i", "us", "rt"}:
            continue
        add(code.upper())

    return out


def extract_location_hint(transcript: str, context: str = "") -> Optional[str]:
    """Extract a geocodeable location hint from a transcript (v2).

    We try to build a query like: "Blackhorse Pike & Farewell Dr, Washington Township, NJ".
    If we can't find something strong, return None.
    """
    t = " ".join((transcript or "").strip().split())
    if len(t) < 18:
        return None

    t_lc = t.lower()
    ctx = " ".join((context or "").strip().split())

    # Municipalities (limited but useful). We'll still geocode without this.
    muni_re = re.compile(r"\b([A-Za-z][A-Za-z .'-]{2,}\s+(?:township|city|borough|boro|village))\b", re.I)
    muni = None
    m = muni_re.search(t)
    if m:
        muni = m.group(1).strip()

    # Road name: require a road suffix to avoid matching random "x at y" phrases.
    # Limit the number of tokens before the suffix to reduce junk matches from noisy transcripts.
    road_suffix = r"(?:st(?:reet)?|rd|road|ave(?:nue)?|blvd|boulevard|pike|hwy|highway|turnpike|parkway|dr(?:ive)?|ln|lane|ct|court|ter(?:race)?|pl(?:ace)?|cir(?:cle)?|way)"
    token = r"[A-Za-z0-9][A-Za-z0-9.'-]*"
    road = rf"(?:\d{{1,5}}\s+)?{token}(?:\s+{token}){{0,3}}\s+{road_suffix}"
    road_re = re.compile(rf"\b({road})\b", re.I)

    # Intersection: "<road> at <road>" or "<road> and <road>"
    inter_re = re.compile(rf"\b(?P<r1>{road})\s+(?:and|&|at)\s+(?P<r2>{road})\b", re.I)

    def is_plausible_road(name: str) -> bool:
        s = re.sub(r"\s+", " ", (name or "").strip().lower())
        if not s:
            return False
        if s in {"the road", "off the road", "on the road", "in the road", "no way", "this way", "that way"}:
            return False
        words = [w for w in s.split(" ") if w]
        if len(words) < 2:
            return False
        core = words[:-1]  # drop suffix word (road/st/ave/etc.)
        stop = {"the", "a", "an", "of", "to", "from", "at", "in", "on", "off", "near", "around", "up", "down", "left", "right", "straight", "by", "for", "with", "and"}
        meaningful = [w for w in core if w not in stop and len(w) >= 4]
        return bool(meaningful)

    def join_with_nj(parts: List[str]) -> str:
        joined = " ".join(parts)
        if re.search(r"\b(nj|new jersey)\b", joined, re.I):
            return ", ".join(parts)
        return ", ".join(parts + ["NJ"])

    m = inter_re.search(t)
    if m:
        r1 = " ".join(m.group("r1").split())
        r2 = " ".join(m.group("r2").split())
        if is_plausible_road(r1) and is_plausible_road(r2):
            parts = [f"{r1} & {r2}"]
            if muni:
                parts.append(muni)
            elif ctx:
                parts.append(ctx)
            return join_with_nj(parts)

    # Single road + muni/county context
    m = road_re.search(t)
    if m:
        road_name = " ".join(m.group(1).split())
        if is_plausible_road(road_name):
            parts = [road_name]
            if muni:
                parts.append(muni)
            elif ctx:
                parts.append(ctx)
            return join_with_nj(parts)

    # Exit / route patterns
    exit_m = re.search(r"\bexit\s*(\d{1,3}[a-z]?)\b", t_lc)
    route_m = re.search(r"\b(?:route|rt\.?|i-|us)\s*-?\s*(\d{1,3})\b", t_lc)
    if exit_m and route_m:
        parts = [f"Exit {exit_m.group(1).upper()} on Route {route_m.group(1)}"]
        if ctx:
            parts.append(ctx)
        return join_with_nj(parts)

    return None


def approximate_channel_center(channel_name: str, location: str) -> Optional[tuple]:
    """Return an approximate (lat, lon) for a channel to power a coarse map view (v1)."""
    channel_lc = (channel_name or "").lower()
    loc_lc = (location or "").lower()

    lookup = {
        # Cities
        "newark": (40.7357, -74.1724),
        "jersey city": (40.7178, -74.0431),
        "trenton": (40.2204, -74.7643),
        # Counties (approx centroids)
        "essex county": (40.7876, -74.2452),
        "somerset county": (40.5606, -74.6400),
        "ocean county": (39.9153, -74.2846),
        "camden county": (39.8023, -74.9517),
        "burlington county": (39.8395, -74.7173),
        "gloucester county": (39.7176, -75.1410),
        "atlantic county": (39.4778, -74.6338),
        "mercer county": (40.2829, -74.7027),
        "hudson county": (40.7453, -74.0535),
        # Regions
        "north nj": (40.8850, -74.2700),
        "central nj": (40.3573, -74.5082),
        "south nj": (39.8200, -74.9900),
        "statewide nj": (40.0583, -74.4057),
    }

    for key in ("newark", "jersey city", "trenton"):
        if key in channel_lc:
            return lookup[key]

    for key, coords in lookup.items():
        if key in loc_lc:
            return coords

    if "troop a" in channel_lc:
        return lookup["north nj"]
    if "troop b" in channel_lc:
        return lookup["central nj"]
    if "troop c" in channel_lc:
        return lookup["south nj"]

    if "nj" in channel_lc or "new jersey" in channel_lc or "nj" in loc_lc:
        return lookup["statewide nj"]

    return None


def backfill_missing_geo(max_rows: int = 20000) -> int:
    """Best-effort backfill for older DB rows that predate geo fields."""
    with db_lock:
        rows = db_conn.execute(
            "SELECT id, channel, location FROM events WHERE (lat IS NULL OR lon IS NULL) AND (geo_source IS NULL OR geo_source = '') LIMIT ?",
            (int(max_rows),),
        ).fetchall()

    updates = []
    for row in rows:
        center = approximate_channel_center(row["channel"], row["location"] or "")
        if not center:
            continue
        updates.append(
            (
                float(center[0]),
                float(center[1]),
                "channel",
                0.25,
                (row["location"] or row["channel"]),
                time.time(),
                int(row["id"]),
            )
        )

    if not updates:
        return 0

    with db_lock:
        db_conn.executemany(
            "UPDATE events SET lat = ?, lon = ?, geo_source = ?, geo_confidence = ?, geo_query = ?, geo_updated_at = ? WHERE id = ?",
            updates,
        )
        db_conn.commit()

    logger.info(f"Backfilled geo for {len(updates)} event(s)")
    return len(updates)


def normalize_geocode_query(query: str) -> str:
    return " ".join((query or "").strip().lower().split())


def get_geocode_cache(query: str) -> Optional[Dict]:
    key = normalize_geocode_query(query)
    if not key:
        return None
    with db_lock:
        row = db_conn.execute(
            "SELECT query, lat, lon, confidence, provider, created_at FROM geocode_cache WHERE query = ?",
            (key,),
        ).fetchone()
    if not row:
        return None
    return {
        "query": row["query"],
        "lat": float(row["lat"]),
        "lon": float(row["lon"]),
        "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
        "provider": row["provider"],
        "created_at": float(row["created_at"]),
    }


def put_geocode_cache(query: str, lat: float, lon: float, confidence: Optional[float], provider: str) -> None:
    key = normalize_geocode_query(query)
    if not key:
        return
    with db_lock:
        db_conn.execute(
            "INSERT OR REPLACE INTO geocode_cache (query, lat, lon, confidence, provider, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (key, float(lat), float(lon), float(confidence) if confidence is not None else None, provider, time.time()),
        )
        db_conn.commit()


def update_event_geo(event_id: int, lat: float, lon: float, source: str, confidence: Optional[float], query: str) -> None:
    with db_lock:
        db_conn.execute(
            "UPDATE events SET lat = ?, lon = ?, geo_source = ?, geo_confidence = ?, geo_query = ?, geo_updated_at = ? WHERE id = ?",
            (float(lat), float(lon), str(source), float(confidence) if confidence is not None else None, query, time.time(), int(event_id)),
        )
        db_conn.commit()


def geocode_nominatim(query: str) -> Optional[Dict]:
    params = {
        "q": query,
        "format": "jsonv2",
        "addressdetails": 1,
        "limit": 1,
    }
    headers = {"User-Agent": GEOCODER_USER_AGENT}
    resp = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params=params,
        headers=headers,
        timeout=GEOCODER_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        return None
    data = resp.json() or []
    if not data:
        return None
    item = data[0]
    try:
        lat = float(item.get("lat"))
        lon = float(item.get("lon"))
    except Exception:
        return None

    addr = item.get("address") or {}
    state = str(addr.get("state") or "").lower()
    if state and "new jersey" not in state and state != "nj":
        return None

    confidence = None
    try:
        # importance is 0..1-ish; treat it as a soft confidence
        if item.get("importance") is not None:
            confidence = float(item.get("importance"))
    except Exception:
        confidence = None

    return {"lat": lat, "lon": lon, "confidence": confidence, "provider": "nominatim"}


def geocode_census(query: str) -> Optional[Dict]:
    """Geocode using the US Census Geocoder (best for full US street addresses)."""
    params = {
        "address": query,
        "benchmark": "2020",
        "format": "json",
    }
    headers = {"User-Agent": GEOCODER_USER_AGENT}
    resp = requests.get(
        "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress",
        params=params,
        headers=headers,
        timeout=GEOCODER_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        return None
    data = resp.json() or {}
    matches = ((data.get("result") or {}).get("addressMatches") or [])
    if not matches:
        return None
    m = matches[0] or {}
    coords = m.get("coordinates") or {}
    try:
        lon = float(coords.get("x"))
        lat = float(coords.get("y"))
    except Exception:
        return None

    addr = m.get("addressComponents") or {}
    state = str(addr.get("state") or "").strip().lower()
    if state and state != "nj":
        return None

    return {"lat": lat, "lon": lon, "confidence": 0.75, "provider": "census", "matched": m.get("matchedAddress")}


# Background geocoding worker (v2)
geocode_queue: "queue.Queue[Dict]" = queue.Queue(maxsize=500)
_geocode_thread_started = False
_geocode_last_request_at = 0.0
_geocode_rate_lock = threading.Lock()


def enqueue_geocode(event_id: int, transcript: str, context: str) -> None:
    if not ENABLE_GEOCODING:
        return
    try:
        geocode_queue.put_nowait({"event_id": int(event_id), "transcript": transcript or "", "context": context or ""})
    except queue.Full:
        logger.warning("Geocode queue is full; dropping task")


def geocode_worker() -> None:
    global _geocode_last_request_at
    logger.info("Geocode worker started")
    while True:
        task = geocode_queue.get()
        try:
            event_id = int(task.get("event_id"))
            transcript = str(task.get("transcript") or "")
            context = str(task.get("context") or "")

            hint = extract_location_hint(transcript, context=context)
            if not hint:
                continue

            cached = get_geocode_cache(hint)
            if cached:
                update_event_geo(
                    event_id,
                    cached["lat"],
                    cached["lon"],
                    cached.get("provider") or "cache",
                    cached.get("confidence"),
                    cached.get("query") or hint,
                )
                continue

            provider = (GEOCODER_PROVIDER or "").strip().lower()
            if provider == "census" and not re.search(r"\d", hint):
                # Census geocoder needs a proper street address.
                continue
            if provider not in {"nominatim", "census"}:
                continue

            # Simple global rate limit.
            with _geocode_rate_lock:
                now = time.time()
                sleep_for = (GEOCODER_MIN_INTERVAL_SECONDS - (now - _geocode_last_request_at))
                if sleep_for > 0:
                    time.sleep(sleep_for)
                _geocode_last_request_at = time.time()

            if provider == "nominatim":
                result = geocode_nominatim(hint)
            else:
                result = geocode_census(hint)
            if not result:
                continue

            put_geocode_cache(
                hint,
                result["lat"],
                result["lon"],
                result.get("confidence"),
                result.get("provider") or provider,
            )
            update_event_geo(
                event_id,
                result["lat"],
                result["lon"],
                result.get("provider") or provider,
                result.get("confidence"),
                hint,
            )
        except Exception as e:
            logger.warning(f"Geocode worker error: {e}")
        finally:
            try:
                geocode_queue.task_done()
            except Exception:
                pass


def start_geocode_worker_once() -> None:
    global _geocode_thread_started
    if not ENABLE_GEOCODING:
        return
    if _geocode_thread_started:
        return
    _geocode_thread_started = True
    t = threading.Thread(target=geocode_worker, name="GeocodeWorker", daemon=True)
    t.start()


# Initialize persistence + caches on import (works for both direct run and gunicorn)
init_db()
backfill_missing_geo()
load_recent_events_into_memory()
rebuild_stats_from_db()
logger.info(f"Using SQLite database at: {DB_PATH}")
start_geocode_worker_once()


# Utilitarian (mobile-first) dashboard HTML template
DASHBOARD_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
	  <meta charset="UTF-8" />
	  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
	  <title>PoliceTracker Dashboard</title>
	  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
	  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css" />
	  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css" />
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

	    :root[data-theme="dark"] {
	      --bg: #0b1020;
	      --panel: #0f172a;
	      --text: #e5e7eb;
	      --muted: #94a3b8;
	      --border: rgba(148,163,184,.22);
	      --accent: #3b82f6;
	      --danger: #f87171;
	      --warn: #fbbf24;
	      --ok: #34d399;
	      --shadow: 0 1px 2px rgba(0,0,0,.35), 0 10px 24px rgba(0,0,0,.35);
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

	    .top-actions {
	      display: flex;
	      align-items: flex-start;
	      gap: 10px;
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

    .dot.warn {
      background: var(--warn);
      box-shadow: 0 0 0 2px rgba(217,119,6,.15);
    }

    .dot.disabled {
      background: rgba(148,163,184,.45);
      box-shadow: 0 0 0 2px rgba(148,163,184,.12);
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
      grid-template-columns: 1fr 220px 160px 160px 160px auto;
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
	      background: var(--panel);
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
	      background: var(--panel);
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
	      color: var(--text);
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

	    .details {
	      margin-top: 8px;
	      display: none;
	      font-size: 12px;
	      color: var(--muted);
	      font-family: var(--mono);
	      line-height: 1.5;
	      word-break: break-word;
	    }

	    .event.expanded .details { display: block; }

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

	    :root[data-theme="dark"] .badge {
	      background: rgba(148,163,184,.12);
	      border-color: rgba(148,163,184,.22);
	      color: var(--text);
	    }

	    .actions {
	      margin-top: 10px;
	      display: flex;
	      gap: 8px;
      align-items: center;
    }

	    .linkbtn {
	      border: 1px solid var(--border);
	      background: var(--panel);
	      color: var(--text);
	      padding: 6px 10px;
	      border-radius: 10px;
	      cursor: pointer;
	      font-size: 13px;
	      font-weight: 700;
	      text-decoration: none;
	      display: inline-flex;
	      align-items: center;
	    }

	    .linkbtn.active {
	      border-color: rgba(37, 99, 235, .45);
	      color: var(--accent);
	      background: rgba(37, 99, 235, .08);
	    }

	    .linkbtn:disabled {
	      opacity: .55;
	      cursor: not-allowed;
	    }

	    .map-panel { overflow: hidden; }
	    .map-header {
	      padding: 10px 12px;
	      border-bottom: 1px solid var(--border);
	      display: flex;
	      justify-content: space-between;
	      gap: 10px;
	      flex-wrap: wrap;
	      align-items: center;
	      color: var(--muted);
	      font-size: 12px;
	    }
		    .map-title { font-family: var(--mono); color: var(--text); font-weight: 800; }
		    #map { height: 360px; width: 100%; }
		    @media (max-width: 640px) { #map { height: 280px; } }

		    /* Match markercluster styling to the utilitarian theme */
		    .marker-cluster-small { background-color: rgba(37, 99, 235, .18); }
		    .marker-cluster-small div { background-color: rgba(37, 99, 235, .35); color: #fff; font-family: var(--mono); }
		    .marker-cluster-medium { background-color: rgba(217, 119, 6, .18); }
		    .marker-cluster-medium div { background-color: rgba(217, 119, 6, .35); color: #fff; font-family: var(--mono); }
		    .marker-cluster-large { background-color: rgba(220, 38, 38, .18); }
		    .marker-cluster-large div { background-color: rgba(220, 38, 38, .35); color: #fff; font-family: var(--mono); }

		    .feeds-panel { overflow: hidden; }
		    #feeds-list { padding: 10px 12px; display: grid; gap: 8px; }
		    .feed {
		      border: 1px solid var(--border);
		      border-radius: 10px;
		      padding: 8px 10px;
		      display: grid;
		      gap: 6px;
		      background: var(--panel);
		    }
		    .feed-top { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
		    .feed-main { display: flex; align-items: center; gap: 8px; min-width: 0; }
		    .feed-name { font-weight: 800; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
		    .feed-meta { display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }
		    .feed-error { font-family: var(--mono); font-size: 12px; color: var(--muted); }
		    .feed-url { font-family: var(--mono); font-size: 11px; color: var(--muted); opacity: .85; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
		    .feed-loc { font-family: var(--mono); font-size: 11px; color: var(--muted); opacity: .85; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

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
	          <span>•</span>
	          <span id="coverage">Coverage --</span>
	        </div>
	      </div>
	      <div class="top-actions">
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
	        <button id="theme-toggle" class="linkbtn" type="button" title="Toggle theme">Theme</button>
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
      <select id="group">
        <option value="off">No grouping</option>
        <option value="120">Group 2m</option>
        <option value="300">Group 5m</option>
      </select>
	      <button id="refresh" type="button">Refresh</button>
	    </section>

		    <section class="panel map-panel">
		      <div class="map-header">
		        <div class="map-title">Map</div>
		        <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
		          <span><span id="map-count">0</span> plotted</span>
		          <div style="display:flex; gap:6px; align-items:center;">
		            <button id="map-mode-markers" class="linkbtn" type="button">Markers</button>
		            <button id="map-mode-cluster" class="linkbtn" type="button">Cluster</button>
		            <button id="map-mode-heat" class="linkbtn" type="button">Heat</button>
		          </div>
		          <button id="fit-map" class="linkbtn" type="button">Fit</button>
		        </div>
		      </div>
		      <div id="map"></div>
		    </section>

		    <section class="panel feeds-panel">
		      <div class="map-header">
		        <div class="map-title">Feeds</div>
		        <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
		          <span id="feeds-summary">--</span>
		          <button id="feeds-toggle" class="linkbtn" type="button">Only problems</button>
		        </div>
		      </div>
		      <div id="feeds-list">
		        <div class="empty">Loading...</div>
		      </div>
		    </section>

		    <section class="panel events">
		      <div class="events-header">
		        <div><span id="result-count">0</span> shown <span id="raw-count"></span></div>
		        <div style="font-family: var(--mono);">GET /api/events</div>
		      </div>
      <div id="events-list">
        <div class="empty">Loading...</div>
      </div>
	    </section>
	  </div>

	  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
	  <script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
	  <script src="https://unpkg.com/leaflet.heat@0.2.0/dist/leaflet-heat.js"></script>
	  <script>
	    const state = {
	      search: "",
	      channel: "all",
      priority: "all",
      range: "all",
      group: "off"
    };

	    let allEvents = [];
	    let lastStats = {};
	    let lastRefreshOk = true;
	    let lastFeeds = [];
	    let feedsOnlyProblems = false;

    // Theme + map state
    const THEME_KEY = "pt_theme";
    const MAP_MODE_KEY = "pt_map_mode";
    let map = null;
    let markerLayer = null;
    let clusterLayer = null;
    let heatLayer = null;
    let tileLayers = null;
    let didAutoFit = false;
    let mapMode = "cluster";

    function currentTheme() {
      const saved = localStorage.getItem(THEME_KEY);
      if (saved === "dark" || saved === "light") return saved;
      const prefersDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
      return prefersDark ? "dark" : "light";
    }

    function applyTheme(mode) {
      const root = document.documentElement;
      if (mode === "dark") root.dataset.theme = "dark";
      else delete root.dataset.theme;
      localStorage.setItem(THEME_KEY, mode);
      const btn = document.getElementById("theme-toggle");
      if (btn) btn.textContent = (mode === "dark") ? "Light" : "Dark";
      if (map && tileLayers) {
        try {
          if (mode === "dark") {
            if (tileLayers.light) map.removeLayer(tileLayers.light);
            if (tileLayers.dark) tileLayers.dark.addTo(map);
          } else {
            if (tileLayers.dark) map.removeLayer(tileLayers.dark);
            if (tileLayers.light) tileLayers.light.addTo(map);
          }
        } catch (e) {}
      }
    }

    function toggleTheme() {
      const mode = (document.documentElement.dataset && document.documentElement.dataset.theme === "dark") ? "dark" : "light";
      applyTheme(mode === "dark" ? "light" : "dark");
    }

    function currentMapMode() {
      const saved = localStorage.getItem(MAP_MODE_KEY);
      if (saved === "markers") return "markers";
      if (saved === "cluster") return (window.L && L.markerClusterGroup) ? "cluster" : "markers";
      if (saved === "heat") return (window.L && L.heatLayer) ? "heat" : "markers";
      return (window.L && L.markerClusterGroup) ? "cluster" : "markers";
    }

    function updateMapModeButtons() {
      const btnMarkers = document.getElementById("map-mode-markers");
      const btnCluster = document.getElementById("map-mode-cluster");
      const btnHeat = document.getElementById("map-mode-heat");
      if (btnMarkers) btnMarkers.classList.toggle("active", mapMode === "markers");
      if (btnCluster) {
        btnCluster.disabled = !(window.L && L.markerClusterGroup);
        btnCluster.classList.toggle("active", mapMode === "cluster");
      }
      if (btnHeat) {
        btnHeat.disabled = !(window.L && L.heatLayer);
        btnHeat.classList.toggle("active", mapMode === "heat");
      }
    }

    function setMapMode(mode) {
      const m = (mode === "markers" || mode === "cluster" || mode === "heat") ? mode : "markers";
      mapMode = m;
      localStorage.setItem(MAP_MODE_KEY, m);
      updateMapModeButtons();
      didAutoFit = false;
      updateMap(getDisplayEvents(), true);
    }

    function attachActiveMapLayer() {
      if (!map) return;
      const want = (mapMode === "heat" && heatLayer) ? heatLayer : (mapMode === "cluster" && clusterLayer) ? clusterLayer : markerLayer;
      for (const layer of [markerLayer, clusterLayer, heatLayer]) {
        if (!layer) continue;
        if (layer !== want && map.hasLayer(layer)) {
          try {
            map.removeLayer(layer);
          } catch (e) {}
        }
      }
      if (want && !map.hasLayer(want)) {
        try {
          want.addTo(map);
        } catch (e) {}
      }
    }

    function initMapOnce() {
      if (map || !window.L) return;
      map = L.map("map", { zoomControl: true });
      map.setView([40.12, -74.67], 8);

      tileLayers = {
        light: L.tileLayer("https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", { maxZoom: 19, attribution: "&copy; OpenStreetMap contributors &copy; CARTO" }),
        dark: L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png", { maxZoom: 19, attribution: "&copy; OpenStreetMap contributors &copy; CARTO" })
      };

      mapMode = currentMapMode();

      markerLayer = L.layerGroup();
      clusterLayer = (L.markerClusterGroup ? L.markerClusterGroup({ showCoverageOnHover: false, maxClusterRadius: 55 }) : null);
      heatLayer = (L.heatLayer ? L.heatLayer([], { radius: 20, blur: 16, maxZoom: 12 }) : null);
      attachActiveMapLayer();
      applyTheme(currentTheme());
      updateMapModeButtons();

      const btnMarkers = document.getElementById("map-mode-markers");
      if (btnMarkers) btnMarkers.addEventListener("click", () => setMapMode("markers"));
      const btnCluster = document.getElementById("map-mode-cluster");
      if (btnCluster) btnCluster.addEventListener("click", () => setMapMode("cluster"));
      const btnHeat = document.getElementById("map-mode-heat");
      if (btnHeat) btnHeat.addEventListener("click", () => setMapMode("heat"));

      const fitBtn = document.getElementById("fit-map");
      if (fitBtn) {
        fitBtn.addEventListener("click", () => {
          didAutoFit = false;
          updateMap(getDisplayEvents(), true);
        });
      }
    }

    function markerColor(pr) {
      const p = Number(pr ?? 5);
      if (p >= 8) return "#dc2626";
      if (p >= 5) return "#d97706";
      return "#475569";
    }

    function dotIcon(color) {
      return L.divIcon({
        className: "",
        html: `<div style="width:10px;height:10px;border-radius:999px;background:${color};border:2px solid rgba(255,255,255,.9);"></div>`,
        iconSize: [10, 10],
        iconAnchor: [5, 5],
      });
    }

    function updateMap(events, forceFit = false) {
      initMapOnce();
      if (!map || !markerLayer) return;
      try { markerLayer.clearLayers(); } catch (e) {}
      try { if (clusterLayer) clusterLayer.clearLayers(); } catch (e) {}
      try { if (heatLayer) heatLayer.setLatLngs([]); } catch (e) {}
      attachActiveMapLayer();

      const points = [];
      const heatPoints = [];
      let plotted = 0;

      for (const e of (events || [])) {
        const lat = Number(e.lat);
        const lon = Number(e.lon);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;
        plotted += 1;
        points.push([lat, lon]);

        const pr = Number(e.priority ?? 5);
        const color = markerColor(pr);
        const when = e.timestamp ? new Date(Number(e.timestamp) * 1000).toLocaleString() : "";
        const kw = Array.isArray(e.keywords) ? e.keywords : [];
        const src = e.geo_source ? String(e.geo_source) : "unknown";
        const conf = (e.geo_confidence !== null && e.geo_confidence !== undefined) ? Number(e.geo_confidence) : null;

        let popup = "<div style='font-family: var(--mono); font-size: 12px; line-height: 1.35;'>";
        popup += "<div><b>" + escapeHtml(e.channel || "Unknown") + "</b></div>";
        if (when) popup += "<div>" + escapeHtml(when) + "</div>";
        if (e.location) popup += "<div>" + escapeHtml(e.location) + "</div>";
        popup += "<div>geo: " + escapeHtml(src) + (conf !== null ? " (" + escapeHtml(conf.toFixed(2)) + ")" : "") + "</div>";
        if (kw.length) popup += "<div style='margin-top:4px;'>" + kw.map(k => "<span style='display:inline-block; margin:2px 4px 0 0; padding:2px 6px; border:1px solid rgba(148,163,184,.25); border-radius:999px;'>" + escapeHtml(k) + "</span>").join("") + "</div>";
        popup += "</div>";

        if (mapMode === "heat" && heatLayer) {
          const intensity = pr >= 8 ? 1.0 : (pr >= 5 ? 0.7 : 0.45);
          heatPoints.push([lat, lon, intensity]);
          continue;
        }

        if (mapMode === "cluster" && clusterLayer) {
          const marker = L.marker([lat, lon], { icon: dotIcon(color), keyboard: false });
          marker.bindPopup(popup);
          clusterLayer.addLayer(marker);
          continue;
        }

        const marker = L.circleMarker([lat, lon], {
          radius: pr >= 8 ? 7 : (pr >= 5 ? 6 : 5),
          color: color,
          fillColor: color,
          fillOpacity: 0.65,
          weight: 2,
        });
        marker.bindPopup(popup);
        marker.addTo(markerLayer);
      }

      if (mapMode === "heat" && heatLayer) {
        try {
          heatLayer.setLatLngs(heatPoints);
        } catch (e) {}
      }

      const countEl = document.getElementById("map-count");
      if (countEl) countEl.textContent = String(plotted);

      if ((forceFit || !didAutoFit) && points.length) {
        try {
          const bounds = L.latLngBounds(points);
          map.fitBounds(bounds.pad(0.15));
          didAutoFit = true;
        } catch (e) {}
      }
    }

    async function fetchCoverage() {
      try {
        const resp = await fetch("/api/listener/status");
        if (!resp.ok) throw new Error("HTTP " + resp.status);
        const data = await resp.json();
        const channels = Array.isArray(data.channels) ? data.channels : [];

        lastFeeds = channels;

        const enabled = channels.filter(c => c && c.enabled);
        const running = enabled.filter(c => c && c.running);
        const cov = document.getElementById("coverage");
        if (cov) cov.textContent = "Coverage " + running.length + "/" + enabled.length + " feeds";

        const summaryEl = document.getElementById("feeds-summary");
        if (summaryEl) {
          const bad = enabled.filter(c => feedState(c).kind === "down").length;
          const warn = enabled.filter(c => feedState(c).kind === "warn").length;
          summaryEl.textContent = `${running.length}/${enabled.length} running • ${bad} down • ${warn} warn`;
        }

        renderFeeds(channels);
      } catch (e) {
        const cov = document.getElementById("coverage");
        if (cov) cov.textContent = "Coverage unavailable";

        const summaryEl = document.getElementById("feeds-summary");
        if (summaryEl) summaryEl.textContent = "Unavailable";

        const container = document.getElementById("feeds-list");
        if (container) container.innerHTML = `<div class="empty">Listener unavailable.</div>`;
      }
    }

    function formatAge(seconds) {
      if (seconds === null || seconds === undefined) return "--";
      const s = Number(seconds);
      if (!Number.isFinite(s)) return "--";
      if (s < 60) return Math.round(s) + "s";
      if (s < 3600) return Math.round(s / 60) + "m";
      return Math.round(s / 3600) + "h";
    }

    function feedState(c) {
      if (!c || !c.enabled) {
        return { kind: "disabled", dotClass: "disabled", label: "DISABLED", pillClass: "" };
      }

      const running = Boolean(c.running);
      const age = (c.audio_age_s === null || c.audio_age_s === undefined) ? NaN : Number(c.audio_age_s);
      const hasAge = Number.isFinite(age);
      const hasErr = Boolean(c.last_error);

      if (!running) return { kind: "down", dotClass: "offline", label: "DOWN", pillClass: "prio-high" };
      if (!hasAge) return { kind: "down", dotClass: "offline", label: "NO AUDIO", pillClass: "prio-high" };

      if (age > 45 || (hasErr && age > 20)) return { kind: "down", dotClass: "offline", label: "STALE", pillClass: "prio-high" };
      if (age > 15 || hasErr) return { kind: "warn", dotClass: "warn", label: "WARN", pillClass: "prio-med" };
      return { kind: "ok", dotClass: "", label: "OK", pillClass: "" };
    }

    function renderFeeds(channels) {
      const container = document.getElementById("feeds-list");
      if (!container) return;

      const list = Array.isArray(channels) ? channels.slice() : [];
      if (!list.length) {
        container.innerHTML = `<div class="empty">No feeds configured.</div>`;
        return;
      }

      function severityRank(kind) {
        if (kind === "down") return 3;
        if (kind === "warn") return 2;
        if (kind === "ok") return 1;
        return 0; // disabled
      }

      list.sort((a, b) => {
        const sa = feedState(a);
        const sb = feedState(b);
        const ra = severityRank(sa.kind);
        const rb = severityRank(sb.kind);
        if (ra !== rb) return rb - ra;

        const aa = (a && a.audio_age_s !== null && a.audio_age_s !== undefined) ? Number(a.audio_age_s) : -1;
        const ab = (b && b.audio_age_s !== null && b.audio_age_s !== undefined) ? Number(b.audio_age_s) : -1;
        if (Number.isFinite(aa) && Number.isFinite(ab) && aa !== ab) return ab - aa;

        const na = String((a && a.name) || "");
        const nb = String((b && b.name) || "");
        return na.localeCompare(nb);
      });

      const show = feedsOnlyProblems
        ? list.filter(c => {
            const st = feedState(c);
            return st.kind === "down" || st.kind === "warn";
          })
        : list;

      if (!show.length) {
        container.innerHTML = `<div class="empty">No problem feeds right now.</div>`;
        return;
      }

      container.innerHTML = show.map(c => {
        const st = feedState(c);
        const name = escapeHtml(c && c.name ? c.name : "Unnamed");
        const url = c && c.url ? String(c.url) : "";
        const err = c && c.last_error ? String(c.last_error) : "";
        const loc = c && c.location ? String(c.location) : "";

        const age = (c && c.audio_age_s !== null && c.audio_age_s !== undefined) ? Number(c.audio_age_s) : NaN;
        const ageText = Number.isFinite(age) ? ("audio " + formatAge(age)) : "audio --";
        const restarts = Number((c && c.restart_count) || 0);

        const pills = [];
        pills.push(`<span class="pill ${st.pillClass}">${escapeHtml(st.label)}</span>`);
        if (c && c.enabled) pills.push(`<span class="pill">${escapeHtml(ageText)}</span>`);
        if (restarts > 0) pills.push(`<span class="pill prio-med">restarts ${escapeHtml(String(restarts))}</span>`);

        const dotCls = st.dotClass ? `dot ${st.dotClass}` : "dot";

        return `
          <div class="feed">
            <div class="feed-top">
              <div class="feed-main">
                <span class="${dotCls}"></span>
                <div style="min-width:0;">
                  <div class="feed-name">${name}</div>
                  ${loc ? `<div class="feed-loc">${escapeHtml(loc)}</div>` : ``}
                </div>
              </div>
              <div class="feed-meta">${pills.join("")}</div>
            </div>
            ${url ? `<div class="feed-url">${escapeHtml(url)}</div>` : ``}
            ${err ? `<div class="feed-error">${escapeHtml(err)}</div>` : ``}
          </div>
        `;
      }).join("");
    }


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

    function groupKey(e) {
      const ch = String((e && e.channel) || "");
      const kws = Array.isArray(e && e.keywords) ? e.keywords.slice().map(x => String(x)).sort().join(",") : "";
      const hint = String((e && e.geo_query) || "").toLowerCase().trim();
      return ch + "|" + kws + "|" + hint;
    }

    function groupEvents(events, windowSeconds) {
      const w = Number(windowSeconds);
      if (!Number.isFinite(w) || w <= 0) return events || [];
      const list = Array.isArray(events) ? events : [];
      const out = [];
      const lastByKey = new Map();

      for (const e of list) {
        const ts = Number((e && e.timestamp) || 0);
        const key = groupKey(e);
        const g = lastByKey.get(key);
        if (g && (Number(g.group_last_ts || 0) - ts) <= w) {
          g.group_count = Number(g.group_count || 1) + 1;
          g.group_first_ts = Math.min(Number(g.group_first_ts || ts), ts);
          g.group_ids.push(e.id);
          g.feedback_up = Number(g.feedback_up || 0) + Number(e.feedback_up || 0);
          g.feedback_down = Number(g.feedback_down || 0) + Number(e.feedback_down || 0);
          g.priority = Math.max(Number(g.priority ?? 0), Number(e.priority ?? 0));
          if (e && e.transcript && g.group_samples && g.group_samples.length < 3) {
            g.group_samples.push(String(e.transcript));
          }
          continue;
        }

        const base = Object.assign({}, e);
        base.group_count = 1;
        base.group_first_ts = ts;
        base.group_last_ts = ts;
        base.group_ids = [e.id];
        base.group_samples = (e && e.transcript) ? [String(e.transcript)] : [];
        base.feedback_up = Number(e.feedback_up || 0);
        base.feedback_down = Number(e.feedback_down || 0);
        lastByKey.set(key, base);
        out.push(base);
      }

      return out;
    }

    function getDisplayEvents() {
      const raw = applyPriorityFilter(allEvents);
      if (!state.group || state.group === "off") return raw;
      return groupEvents(raw, Number(state.group));
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
      const raw = applyPriorityFilter(allEvents);
      let eventsToShow = raw;
      if (state.group && state.group !== "off") {
        eventsToShow = groupEvents(raw, Number(state.group));
      }

      document.getElementById("result-count").textContent = String(eventsToShow.length);
      const rawCountEl = document.getElementById("raw-count");
      if (rawCountEl) rawCountEl.textContent = (state.group && state.group !== "off") ? `/ ${raw.length} raw` : "";

      updateMap(eventsToShow);

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
        const units = Array.isArray(e.units) ? e.units : [];
        const geoQuery = e.geo_query ? String(e.geo_query) : "";
        const geoSrc = e.geo_source ? String(e.geo_source) : "";
        const geoConf = (e.geo_confidence !== null && e.geo_confidence !== undefined) ? Number(e.geo_confidence) : null;
        const lat = (e.lat !== null && e.lat !== undefined) ? Number(e.lat) : null;
        const lon = (e.lon !== null && e.lon !== undefined) ? Number(e.lon) : null;

        const groupCount = Number(e.group_count || 1);
        const groupFirst = Number(e.group_first_ts || ts);
        const groupLast = Number(e.group_last_ts || ts);
        const groupIds = Array.isArray(e.group_ids) ? e.group_ids : [];
        const groupSamples = Array.isArray(e.group_samples) ? e.group_samples : [];

        const fbUp = Number(e.feedback_up || 0);
        const fbDown = Number(e.feedback_down || 0);

        const idStr = String(e.id || "");
        const voted = idStr ? localStorage.getItem("pt_vote_" + idStr) : null;
        const voteDisabled = Boolean(voted);
        const upActive = voted === "up";
        const downActive = voted === "down";

        return `
          <div class="event" data-id="${escapeHtml(e.id || "")}">
            <div>
              <div class="when" title="${escapeHtml(abs)}">${escapeHtml(formatRel(ts))}<br/><span style="color: var(--muted);">${escapeHtml(abs)}</span></div>
              <div class="meta">
                <span class="pill ${priorityClass(pr)}">P${escapeHtml(pr)}</span>
                <span class="pill">#${escapeHtml(e.id || "-")}</span>
                ${groupCount > 1 ? `<span class="pill">x${escapeHtml(String(groupCount))}</span>` : ``}
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
              <div class="details">
                ${groupCount > 1 ? `<div>group: x${escapeHtml(String(groupCount))} over ${escapeHtml(String(Math.max(0, Math.round(groupLast - groupFirst))))}s (${escapeHtml(formatRel(groupFirst))} → ${escapeHtml(formatRel(groupLast))})</div>` : ``}
                ${groupSamples.length > 1 ? `<div>samples: ${groupSamples.slice(0, 3).map(s => escapeHtml(String(s))).join(" | ")}</div>` : ``}
                ${units.length ? `<div>units: ${escapeHtml(units.join(", "))}</div>` : ``}
                ${geoQuery ? `<div>hint: ${escapeHtml(geoQuery)}</div>` : ``}
                <div>geo: ${escapeHtml(geoSrc || "--")}${geoConf !== null ? ` (${escapeHtml(geoConf.toFixed(2))})` : ``}${(lat !== null && lon !== null && Number.isFinite(lat) && Number.isFinite(lon)) ? ` @ ${escapeHtml(lat.toFixed(5))}, ${escapeHtml(lon.toFixed(5))}` : ``}</div>
                <div>feedback: good ${escapeHtml(String(fbUp))} • bad ${escapeHtml(String(fbDown))}</div>
                ${groupIds.length > 1 ? `<div>ids: ${escapeHtml(groupIds.slice(0, 8).join(", "))}${groupIds.length > 8 ? "…" : ""}</div>` : ``}
              </div>
              <div class="actions">
                <button class="linkbtn" type="button" data-action="toggle">Details</button>
                <button class="linkbtn ${upActive ? "active" : ""}" type="button" data-action="fb-up" ${voteDisabled ? "disabled" : ""} title="Mark as accurate">${escapeHtml(`Good (${fbUp})`)}</button>
                <button class="linkbtn ${downActive ? "active" : ""}" type="button" data-action="fb-down" ${voteDisabled ? "disabled" : ""} title="Mark as noise/false positive">${escapeHtml(`Bad (${fbDown})`)}</button>
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

      container.querySelectorAll("button[data-action='fb-up'], button[data-action='fb-down']").forEach(btn => {
        btn.addEventListener("click", async (ev) => {
          const b = ev.target;
          const card = b.closest(".event");
          if (!card) return;
          const eventId = card.dataset.id;
          if (!eventId) return;

          const action = b.getAttribute("data-action");
          const label = (action === "fb-up") ? "up" : "down";
          const key = "pt_vote_" + String(eventId);
          if (localStorage.getItem(key)) return;

          // Optimistic disable while we POST.
          card.querySelectorAll("button[data-action='fb-up'], button[data-action='fb-down']").forEach(x => x.disabled = true);

          try {
            const resp = await fetch(`/api/events/${encodeURIComponent(eventId)}/feedback`, {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ label })
            });
            if (!resp.ok) throw new Error("HTTP " + resp.status);
            const data = await resp.json();

            localStorage.setItem(key, label);

            const up = Number(data.up || 0);
            const down = Number(data.down || 0);
            const btnUp = card.querySelector("button[data-action='fb-up']");
            const btnDown = card.querySelector("button[data-action='fb-down']");
            if (btnUp) {
              btnUp.textContent = `Good (${up})`;
              btnUp.classList.toggle("active", label === "up");
            }
            if (btnDown) {
              btnDown.textContent = `Bad (${down})`;
              btnDown.classList.toggle("active", label === "down");
            }
          } catch (e) {
            // Re-enable to allow retry.
            card.querySelectorAll("button[data-action='fb-up'], button[data-action='fb-down']").forEach(x => x.disabled = false);
          }
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

    document.getElementById("group").addEventListener("change", (ev) => {
      state.group = ev.target.value;
      render();
    });

    document.getElementById("refresh").addEventListener("click", () => fetchEvents());

    // Boot helpers
    applyTheme(currentTheme());
    const themeBtn = document.getElementById("theme-toggle");
    if (themeBtn) themeBtn.addEventListener("click", toggleTheme);
    initMapOnce();
    fetchCoverage();
    setInterval(fetchCoverage, 30000);
    const feedsBtn = document.getElementById("feeds-toggle");
    if (feedsBtn) {
      feedsBtn.addEventListener("click", () => {
        feedsOnlyProblems = !feedsOnlyProblems;
        feedsBtn.textContent = feedsOnlyProblems ? "Show all" : "Only problems";
        renderFeeds(lastFeeds);
      });
    }


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


@app.route("/api/events/<int:event_id>/feedback", methods=["POST"])
@require_dashboard_auth
def create_feedback(event_id: int):
    """Record simple dashboard feedback on an event (up/down)."""
    try:
        data = request.get_json(silent=True) or {}
        label = str(data.get("label") or "").strip().lower()
        if label not in {"up", "down"}:
            return jsonify({"error": "Invalid label; expected 'up' or 'down'"}), 400

        now = time.time()
        with db_lock:
            db_conn.execute(
                "INSERT INTO event_feedback (event_id, label, created_at) VALUES (?, ?, ?)",
                (int(event_id), label, float(now)),
            )
            db_conn.commit()

            row = db_conn.execute(
                """
                SELECT
                    SUM(CASE WHEN label = 'up' THEN 1 ELSE 0 END) AS up,
                    SUM(CASE WHEN label = 'down' THEN 1 ELSE 0 END) AS down
                FROM event_feedback
                WHERE event_id = ?
                """,
                (int(event_id),),
            ).fetchone()

        up = int(row["up"] or 0) if row else 0
        down = int(row["down"] or 0) if row else 0
        return jsonify({"event_id": int(event_id), "up": up, "down": down, "label": label})
    except Exception as e:
        logger.error(f"Feedback error for event {event_id}: {e}")
        return jsonify({"error": "feedback_failed"}), 500


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
        keywords = data.get("keywords", [])
        if isinstance(keywords, str):
            keywords = [keywords]
        if not isinstance(keywords, list):
            keywords = []

        event = {
            "timestamp": float(data.get('timestamp', time.time())),
            "channel": data.get('channel'),
            "keywords": keywords,
            "transcript": data.get('transcript', ''),
            "priority": data.get('priority', 5),
            "location": data.get('location', ''),
            "raw_data": data,
        }

        # Best-effort unit extraction (v2-ish). If the listener supplies units, prefer those.
        units = data.get("units", [])
        units_list: List[str] = []
        if isinstance(units, list):
            for u in units:
                s = str(u).strip()
                if s:
                    units_list.append(s)
        if not units_list:
            units_list = extract_unit_mentions(event.get("transcript", ""))
        event["units"] = units_list

        # Coarse geo (v1) from listener or refined geo (v2).
        lat = data.get("lat")
        lon = data.get("lon")
        try:
            lat_f = float(lat) if lat is not None else None
        except Exception:
            lat_f = None
        try:
            lon_f = float(lon) if lon is not None else None
        except Exception:
            lon_f = None

        geo_source = data.get("geo_source")
        if (lat_f is not None and lon_f is not None) and not geo_source:
            geo_source = "channel"
        geo_confidence = data.get("geo_confidence")
        try:
            geo_confidence_f = float(geo_confidence) if geo_confidence is not None else None
        except Exception:
            geo_confidence_f = None

        event["lat"] = lat_f
        event["lon"] = lon_f
        event["geo_source"] = geo_source
        event["geo_confidence"] = geo_confidence_f
        incoming_geo_query = data.get("geo_query")
        event["geo_query"] = incoming_geo_query or event.get("location", "")
        # Best-effort: store a per-event location hint even if we can't geocode it.
        # This helps grouping/dedup and gives the UI a more informative context string.
        try:
            hint = extract_location_hint(
                str(event.get("transcript") or ""),
                context=str(event.get("location") or ""),
            )
            if hint and (not incoming_geo_query or incoming_geo_query == event.get("location", "")):
                event["geo_query"] = hint
        except Exception:
            pass
        if lat_f is not None and lon_f is not None:
            event["geo_updated_at"] = float(data.get("geo_updated_at", time.time()))

        event_id = persist_event(event)
        event["id"] = event_id

        # Optional: refine approximate locations in the background (v2).
        # Only enqueue if there's enough transcript to parse.
        if ENABLE_GEOCODING and event.get("transcript") and len(str(event.get("transcript")).strip()) >= 18:
            enqueue_geocode(
                event_id=event_id,
                transcript=str(event.get("transcript") or ""),
                context=str(event.get("location") or event.get("channel") or ""),
            )
        
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


@app.route("/api/listener/status")
@require_dashboard_auth
def get_listener_status():
    """Proxy the listener /status so dashboard clients can see coverage (they can't reach server-local 127.0.0.1)."""
    try:
        resp = requests.get(LISTENER_STATUS_URL, timeout=2)
        if resp.status_code != 200:
            return jsonify({"error": "listener_unavailable", "status_code": resp.status_code}), 502
        return jsonify(resp.json())
    except Exception as e:
        return jsonify({"error": "listener_unavailable", "detail": str(e)}), 502


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
