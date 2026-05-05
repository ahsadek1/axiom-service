"""
tests/test_rate_limiter.py — GAP-5: Token Bucket Rate Limiter (Prime Execution)

Tests for the _TokenBucket class added to main.py.
Imported directly to avoid FastAPI startup cost.

GENESIS 2026-04-27
"""

import sys
import os
import time
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Import just the class without importing the full FastAPI app
import importlib.util, types

_src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "main.py")).read()
# Extract the _TokenBucket class definition only
_start = _src.index("class _TokenBucket:")
_end   = _src.index("\n_execute_rate_limiter", _start)
_class_src = "import threading\nimport time as _time\n" + _src[_start:_end]
_mod = types.ModuleType("_tb_test")
exec(_class_src, _mod.__dict__)
_TokenBucket = _mod._TokenBucket


# ── Tests ─────────────────────────────────────────────────────────────────────

def test_allows_requests_under_capacity():
    """T1: Requests under burst capacity are all allowed."""
    bucket = _TokenBucket(capacity=10, refill_rate=1.0 / 6.0)
    results = [bucket.consume() for _ in range(5)]
    assert all(results), "All 5 requests should be allowed"


def test_blocks_when_capacity_exceeded():
    """T2: 11th request is blocked when capacity=10."""
    bucket = _TokenBucket(capacity=10, refill_rate=1.0 / 6.0)
    results = [bucket.consume() for _ in range(11)]
    assert results[10] is False, "11th request must be rate-limited"
    assert sum(results) == 10, "Exactly 10 requests should be allowed"


def test_refills_over_time():
    """T3: After exhaustion, tokens refill and allow new requests."""
    bucket = _TokenBucket(capacity=3, refill_rate=10.0)  # refill 10/s = fast for test
    # Drain
    for _ in range(3):
        bucket.consume()
    # Next immediate call should be blocked
    assert bucket.consume() is False
    # Wait 0.2s — should get ~2 tokens back (10/s * 0.2s = 2)
    time.sleep(0.2)
    assert bucket.consume() is True, "Should be allowed after refill"


def test_thread_safety():
    """T4: Concurrent requests don't exceed capacity under race conditions."""
    bucket = _TokenBucket(capacity=5, refill_rate=0.0)  # no refill
    results = []
    lock = threading.Lock()

    def worker():
        r = bucket.consume()
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = sum(1 for r in results if r)
    assert allowed == 5, f"Exactly 5 should be allowed, got {allowed}"


def test_capacity_never_exceeds_max():
    """T5: Token count never exceeds capacity even after long idle."""
    bucket = _TokenBucket(capacity=10, refill_rate=1000.0)  # extreme refill rate
    time.sleep(0.05)
    # Drain all
    drained = 0
    for _ in range(100):
        if bucket.consume():
            drained += 1
        else:
            break
    assert drained <= 10, f"Should never exceed capacity=10, got {drained}"
