"""
conftest.py — Axiom resilience test suite.
Sets paths and required env vars BEFORE any test module is imported.
"""
import os
import sys

# Add the axiom directory to sys.path so resilience/ modules can import
# from shared/ using the sys.path.insert("../..") pattern in each module.
# Also allows: from resilience.contracts import ... (flat import in tests)
_AXIOM_DIR = os.path.join(os.path.dirname(__file__), "../..")
_NEXUS_DIR = os.path.join(_AXIOM_DIR, "..")
sys.path.insert(0, os.path.abspath(_AXIOM_DIR))
sys.path.insert(0, os.path.abspath(_NEXUS_DIR))

# Minimal env vars required by resilience modules
os.environ.setdefault("AXIOM_SECRET",      "test-secret-12345")
os.environ.setdefault("POLYGON_API_KEY",   "test-polygon-key")
os.environ.setdefault("ORATS_TOKEN",       "test-orats-token")
os.environ.setdefault("ALPACA_KEY",        "test-alpaca-key")
os.environ.setdefault("ALPACA_SECRET",     "test-alpaca-secret")
os.environ.setdefault("DEEPSEEK_KEY",      "test-deepseek-key")
os.environ.setdefault("DEEPSEEK_API_KEY",  "test-deepseek-key")
