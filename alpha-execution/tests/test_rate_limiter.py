"""
tests/test_rate_limiter.py — GAP-5: Token Bucket Rate Limiter (Alpha Execution)

Tests for the _TokenBucket class added to main.py.

GENESIS 2026-04-27
"""

import sys
import os
import time
import threading
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Extract _TokenBucket class without loading full FastAPI app
_src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "main.py")).read()
_start = _src.index("class _TokenBucket:")
_end   = _src.index("\n_execute_rate_limiter", _start)
_class_src = "import threading\nimport time as _time\n" + _src[_start:_end]
_mod = types.ModuleType("_tb_test_alpha")
exec(_class_src, _mod.__dict__)
_TokenBucket = _mod._TokenBucket


def test_allows_requests_under_capacity():
    bucket = _TokenBucket(capacity=10, refill_rate=1.0 / 6.0)
    results = [bucket.consume() for _ in range(5)]
    assert all(results)


def test_blocks_when_capacity_exceeded():
    bucket = _TokenBucket(capacity=10, refill_rate=1.0 / 6.0)
    results = [bucket.consume() for _ in range(11)]
    assert results[10] is False
    assert sum(results) == 10


def test_refills_over_time():
    bucket = _TokenBucket(capacity=3, refill_rate=10.0)
    for _ in range(3):
        bucket.consume()
    assert bucket.consume() is False
    time.sleep(0.2)
    assert bucket.consume() is True


def test_thread_safety():
    bucket = _TokenBucket(capacity=5, refill_rate=0.0)
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

    assert sum(1 for r in results if r) == 5


def test_capacity_never_exceeds_max():
    bucket = _TokenBucket(capacity=10, refill_rate=1000.0)
    time.sleep(0.05)
    drained = 0
    for _ in range(100):
        if bucket.consume():
            drained += 1
        else:
            break
    assert drained <= 10
