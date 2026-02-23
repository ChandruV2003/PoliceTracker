import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path


# Set env vars before importing the app module
_tmp_dir = tempfile.TemporaryDirectory()
_db_path = Path(_tmp_dir.name) / "policetracker_test.db"
os.environ["DATABASE_PATH"] = str(_db_path)
os.environ["API_TOKEN"] = "testtoken"
# Disable dashboard/read API auth for this unit test run; we only validate API_TOKEN on POST here.
# (web_server.py may load a local .env which sets DASHBOARD_PIN for real deployments.)
os.environ["DASHBOARD_PIN"] = ""
os.environ["DASHBOARD_USER"] = ""
os.environ["DASHBOARD_PASS"] = ""
# Avoid network access during unit tests (geocoding worker).
os.environ["ENABLE_GEOCODING"] = "0"

import importlib
import web_server  # noqa: E402

importlib.reload(web_server)


class TestWebAPI(unittest.TestCase):
    def setUp(self):
        self.client = web_server.app.test_client()

    def test_api_token_required_for_post(self):
        payload = {
            "channel": "Test Channel",
            "timestamp": time.time(),
            "keywords": ["accident"],
            "transcript": "Test transcript",
            "priority": 5,
            "location": "Test Location",
        }

        # No auth header -> 401
        resp = self.client.post("/api/events", json=payload)
        self.assertEqual(resp.status_code, 401)

        # Correct auth header -> 201
        resp = self.client.post("/api/events", json=payload, headers={"Authorization": "Bearer testtoken"})
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertIn("event_id", data)

        # Event is persisted
        conn = sqlite3.connect(str(_db_path))
        count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1)

    def test_get_events_returns_events(self):
        # Insert one event via authenticated POST
        payload = {"channel": "Chan", "timestamp": time.time()}
        resp = self.client.post("/api/events", json=payload, headers={"Authorization": "Bearer testtoken"})
        self.assertEqual(resp.status_code, 201)

        resp = self.client.get("/api/events?limit=10")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertGreaterEqual(data.get("count", 0), 1)
        self.assertIn("stats", data)

    def test_event_feedback_endpoint(self):
        payload = {"channel": "Chan", "timestamp": time.time(), "keywords": ["accident"], "transcript": "Test transcript"}
        resp = self.client.post("/api/events", json=payload, headers={"Authorization": "Bearer testtoken"})
        self.assertEqual(resp.status_code, 201)
        event_id = resp.get_json().get("event_id")
        self.assertTrue(event_id)

        resp = self.client.post(f"/api/events/{event_id}/feedback", json={"label": "up"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data.get("event_id"), event_id)
        self.assertEqual(data.get("up"), 1)
        self.assertEqual(data.get("down"), 0)

        resp = self.client.post(f"/api/events/{event_id}/feedback", json={"label": "down"})
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data.get("up"), 1)
        self.assertEqual(data.get("down"), 1)


if __name__ == "__main__":
    unittest.main()
