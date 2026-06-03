# conftest.py - Pytest configuration and fixtures
import sys
import os
from pathlib import Path

# Add nexus root to path for imports (VERY EARLY)
nexus_root = Path(__file__).parent
if str(nexus_root) not in sys.path:
    sys.path.insert(0, str(nexus_root))

# Also remove any tests directory from path to avoid namespace conflicts
sys.path[:] = [p for p in sys.path if 'tests' not in p or str(nexus_root) in p]

# Ensure CWD
os.chdir(nexus_root)
