#!/usr/bin/env python3
"""
Test script to verify PoliceTracker web API integration
"""
import requests
import time
import json

WEB_API_URL = "http://localhost:8892/api/events"

def test_api_connection():
    """Test if web API is running"""
    try:
        response = requests.get("http://localhost:8892/api/health", timeout=2)
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

def send_test_event():
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
        response = requests.post(WEB_API_URL, json=test_event, timeout=5)
        if response.status_code == 201:
            data = response.json()
            print(f"✅ Test event sent successfully (ID: {data.get('event_id')})")
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
    
    # Test 2: Send test event
    print("Sending test event...")
    if not send_test_event():
        exit(1)
    
    print()
    
    # Test 3: Retrieve events
    print("Retrieving events...")
    if not get_events():
        exit(1)
    
    print()
    print("=" * 50)
    print("✅ All tests passed!")
    print()
    print("Next steps:")
    print("1. Open http://localhost:8892 in your browser to see the dashboard")
    print("2. Configure PoliceTracker to send events to: http://localhost:8892/api/events")
    print("3. To expose to internet: Forward port 8892 on your router")
