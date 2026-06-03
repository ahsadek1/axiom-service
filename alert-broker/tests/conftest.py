"""
conftest.py — pytest configuration for alert-broker tests.

Forces broker's main.py to take priority over nexus/main.py in sys.path.
"""
import sys
from pathlib import Path

# The broker directory MUST be first — nexus root has a main.py that shadows ours
BROKER_DIR = str(Path(__file__).parent.parent)

# Remove any existing nexus root entries that could shadow broker modules
_nexus_root = str(Path(__file__).parent.parent.parent)
while _nexus_root in sys.path:
    sys.path.remove(_nexus_root)

# Ensure broker dir is at index 0
if BROKER_DIR in sys.path:
    sys.path.remove(BROKER_DIR)
sys.path.insert(0, BROKER_DIR)
