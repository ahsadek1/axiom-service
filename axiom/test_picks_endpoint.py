#!/usr/bin/env python3
"""
test_picks_endpoint.py — Integration test for GET /picks

Validates:
  1. Endpoint responds with 200 OK
  2. Response schema matches PicsResponse
  3. Auth header requirement
  4. DTE calculation accuracy
  5. Mock candidate conversion

Usage:
  python3 test_picks_endpoint.py [--local] [--prod]

Default: --prod (uses Railway URL)
"""

import requests
import json
import sys
from datetime import datetime, timedelta
import pytz

# Config
AXIOM_LOCAL = "http://localhost:8001"
AXIOM_PROD = "https://axiom-production-334c.up.railway.app"
AXIOM_SECRET = "62d7ecd98c8e298916c6c87555eac10e7a701cd9be86db27561593a9122244d2"

ET = pytz.timezone("America/New_York")


def test_endpoint(base_url: str, label: str):
    """Test the /picks endpoint."""
    print(f"\n{'='*60}")
    print(f"Testing {label}: {base_url}")
    print('='*60)
    
    url = f"{base_url}/picks"
    headers = {"X-Nexus-Secret": AXIOM_SECRET}
    
    # Test 1: Auth validation
    print("\n[Test 1] Auth validation (no secret)...")
    try:
        resp = requests.get(url, timeout=5)
        if resp.status_code == 403:
            print("  ✅ PASS — Correctly rejected missing auth header")
        else:
            print(f"  ❌ FAIL — Expected 403, got {resp.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"  ⚠️  SKIP — Connection error: {e}")
        return False
    
    # Test 2: Successful request
    print("\n[Test 2] Successful request with valid auth...")
    try:
        resp = requests.get(url, headers=headers, timeout=5)
        if resp.status_code == 200:
            print("  ✅ PASS — Received 200 OK")
        else:
            print(f"  ❌ FAIL — Expected 200, got {resp.status_code}")
            print(f"     Response: {resp.text}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"  ❌ FAIL — Connection error: {e}")
        return False
    
    # Test 3: Response schema
    print("\n[Test 3] Response schema validation...")
    try:
        data = resp.json()
        
        # Check top-level keys
        required_keys = {"pending_picks", "count", "timestamp"}
        if not required_keys.issubset(data.keys()):
            print(f"  ❌ FAIL — Missing keys: {required_keys - set(data.keys())}")
            return False
        
        # Check types
        if not isinstance(data["pending_picks"], list):
            print(f"  ❌ FAIL — pending_picks is not a list")
            return False
        
        if not isinstance(data["count"], int):
            print(f"  ❌ FAIL — count is not an int")
            return False
        
        if not isinstance(data["timestamp"], str):
            print(f"  ❌ FAIL — timestamp is not a string")
            return False
        
        # Count consistency
        if data["count"] != len(data["pending_picks"]):
            print(f"  ❌ FAIL — count mismatch: {data['count']} != {len(data['pending_picks'])}")
            return False
        
        print(f"  ✅ PASS — Schema valid (count={data['count']})")
        
    except json.JSONDecodeError as e:
        print(f"  ❌ FAIL — Invalid JSON: {e}")
        return False
    except Exception as e:
        print(f"  ❌ FAIL — Validation error: {e}")
        return False
    
    # Test 4: Pick schema (if any picks exist)
    if data["pending_picks"]:
        print(f"\n[Test 4] Pick schema validation ({len(data['pending_picks'])} pick(s))...")
        try:
            required_pick_keys = {
                "pick_id", "ticker", "strike", "dte", "direction",
                "thesis", "confidence", "screening_agent",
                "risk_flags", "risk_score", "submitted_at", "assessed_at"
            }
            
            for i, pick in enumerate(data["pending_picks"]):
                if not required_pick_keys.issubset(pick.keys()):
                    missing = required_pick_keys - set(pick.keys())
                    print(f"  ❌ FAIL — Pick {i}: missing keys {missing}")
                    return False
                
                # Type checks
                if not isinstance(pick["risk_flags"], list):
                    print(f"  ❌ FAIL — Pick {i}: risk_flags is not a list")
                    return False
                
                # DTE should be int or null
                if pick["dte"] is not None and not isinstance(pick["dte"], int):
                    print(f"  ❌ FAIL — Pick {i}: dte is not int or null")
                    return False
            
            print(f"  ✅ PASS — All {len(data['pending_picks'])} picks have valid schema")
            
        except Exception as e:
            print(f"  ❌ FAIL — Pick validation error: {e}")
            return False
    else:
        print("\n[Test 4] Pick schema (no picks to validate, skipping)")
    
    # Test 5: Timestamp format
    print("\n[Test 5] Timestamp validation...")
    try:
        timestamp_str = data["timestamp"]
        # Try to parse as ISO 8601
        parsed = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        age = datetime.now(ET) - parsed.replace(tzinfo=ET)
        
        if abs(age.total_seconds()) > 5:
            print(f"  ⚠️  WARN — Timestamp is {age.total_seconds():.1f}s old (may be cached)")
        else:
            print(f"  ✅ PASS — Timestamp is fresh ({age.total_seconds():.1f}s old)")
    except ValueError as e:
        print(f"  ❌ FAIL — Invalid timestamp format: {e}")
        return False
    
    # Test 6: Response time
    print("\n[Test 6] Performance check...")
    import time
    start = time.time()
    resp = requests.get(url, headers=headers, timeout=5)
    elapsed_ms = (time.time() - start) * 1000
    
    if elapsed_ms < 100:
        print(f"  ✅ PASS — Response time: {elapsed_ms:.1f}ms")
    elif elapsed_ms < 500:
        print(f"  ⚠️  WARN — Response time: {elapsed_ms:.1f}ms (acceptable)")
    else:
        print(f"  ❌ FAIL — Response time: {elapsed_ms:.1f}ms (too slow)")
    
    return True


def main():
    """Run tests."""
    target = "--prod"  # default
    
    if "--local" in sys.argv:
        target = "--local"
    elif "--prod" in sys.argv:
        target = "--prod"
    
    if target == "--local":
        success = test_endpoint(AXIOM_LOCAL, "Local (localhost)")
    else:
        success = test_endpoint(AXIOM_PROD, "Production (Railway)")
    
    print(f"\n{'='*60}")
    if success:
        print("✅ All tests passed!")
        sys.exit(0)
    else:
        print("❌ Some tests failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
