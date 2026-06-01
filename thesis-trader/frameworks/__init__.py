"""
THESIS — Frameworks package.

Exports all five master trading framework classes used by the THESIS service.
Each framework analyzes market data and news via Claude Opus 4 (and Gemini 2.5 Pro
for Soros) and returns a FrameworkAnalysis result.
"""

from __future__ import annotations

from frameworks.druckenmiller import DruckenmillerFramework
from frameworks.jones import JonesFramework
from frameworks.soros import SorosFramework
from frameworks.cohen import CohenFramework
from frameworks.buffett import BuffettFramework

__all__ = [
    "DruckenmillerFramework",
    "JonesFramework",
    "SorosFramework",
    "CohenFramework",
    "BuffettFramework",
]
