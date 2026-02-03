#!/usr/bin/env python3
"""
Test script to verify PoliceTracker web API integration
"""
import os
import requests
import time
import json

WEB_HOST = os.environ.get("WEB_HOST", "localhost")
WEB_PORT = int(os.environ.get("WEB_PORT", "8892"))
BASE_URL = f"http://{WEB_HOST}:{WEB_PORT}"
WEB_API_URL = f"{BASE_URL}/api/events"
HEALTH_URL = f"{BASE_URL}/api/health"
API_TOKEN = os.environ.get("API_TOKEN", "")

def test_api_connection():
    """Test if web API is running"""
    try:
        response = requests.get(HEALTH_URL, timeout=2)
        if response.status_code == 200:
            print("✅ Web API is running")
            return True
        else:
            print(f"❌ Web API returned status {response.status_code}")
            return False
    except requests.exceptions.ConnectionError:
        print("❌ Cannot connect to web API. Is web_server.py running?")
        print("   Start it with: ./start_web_server.sh")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

def send_test_event(use_auth: bool = True):
    """Send a test event to the API"""
    test_event = {
        "channel": "Somerset County NJ",
        "timestamp": time.time(),
        "keywords": ["accident", "traffic"],
        "transcript": "Test event: Vehicle accident reported on Route 22",
        "priority": 7,
        "location": "Route 22, Bridgewater"
    }
    
    try:
        headers = {}
        if use_auth and API_TOKEN:
            headers["Authorization"] = f"Bearer {API_TOKEN}"
        response = requests.post(WEB_API_URL, json=test_event, headers=headers, timeout=5)
        if response.status_code == 201:
            data = response.json()
            print(f"✅ Test event sent successfully (ID: {data.get('event_id')})")
            return True
        elif response.status_code == 401 and not use_auth and API_TOKEN:
            print("✅ Unauthorized POST correctly rejected (401)")
            return True
        else:
            print(f"❌ Failed to send event: {response.status_code}")
            print(f"   Response: {response.text}")
            return False
    except Exception as e:
        print(f"❌ Error sending test event: {e}")
        return False

def get_events():
    """Retrieve events from API"""
    try:
        response = requests.get(WEB_API_URL + "?limit=5", timeout=5)
        if response.status_code == 200:
            data = response.json()
            print(f"✅ Retrieved {len(data.get('events', []))} events")
            print(f"   Total events in system: {data.get('stats', {}).get('total_events', 0)}")
            return True
        else:
            print(f"❌ Failed to get events: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ Error getting events: {e}")
        return False

if __name__ == "__main__":
    print("Testing PoliceTracker Web API Integration")
    print("=" * 50)
    print()
    
    # Test 1: API connection
    if not test_api_connection():
        exit(1)
    
    print()

    # Test 2: Auth check (only if API_TOKEN is enabled)
    if API_TOKEN:
        print("Testing API token protection...")
        if not send_test_event(use_auth=False):
            exit(1)
        print()

    # Test 3: Send test event
    print("Sending test event...")
    if not send_test_event(use_auth=True):
        exit(1)
    
    print()
    
    # Test 4: Retrieve events
    print("Retrieving events...")
    if not get_events():
        exit(1)
    
    print()
    print("=" * 50)
    print("✅ All tests passed!")
    print()
    print("Next steps:")
    print(f"1. Open {BASE_URL} in your browser to see the dashboard")
    print(f"2. Configure PoliceTracker to send events to: {WEB_API_URL}")
    print("3. To expose to internet: Forward port 8892 on your router")
