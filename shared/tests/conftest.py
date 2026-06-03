"""
conftest.py — Test path setup for shared/ tests.

Adds /Users/ahmedsadek/nexus/ to sys.path so that
'import shared.sovereign_comms' works when pytest is invoked
from within the shared/ directory (e.g. python -m pytest tests/).
"""
import sys
import os

# Add /Users/ahmedsadek/nexus/ (parent of shared/) to sys.path
_nexus_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _nexus_root not in sys.path:
    sys.path.insert(0, _nexus_root)
