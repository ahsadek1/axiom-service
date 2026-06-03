"""
scorer.py — Wrapper for compute_score_from_context
===================================================
Provides compute_score_from_context by importing from nexus_v2_rebuild.scorer
"""

from __future__ import annotations
import sys
import os
import importlib.util as _ilu

# Load nexus_v2_rebuild/scorer.py directly (avoid circular import)
_NEXUS_ROOT = os.path.dirname(os.path.abspath(__file__))
_SCORER_PATH = os.path.join(_NEXUS_ROOT, "nexus_v2_rebuild", "scorer.py")

_spec = _ilu.spec_from_file_location("_v2_rebuild_scorer", _SCORER_PATH)
_v2_scorer = _ilu.module_from_spec(_spec)
sys.modules["_v2_rebuild_scorer"] = _v2_scorer
_spec.loader.exec_module(_v2_scorer)

# Re-export
compute_score_from_context = _v2_scorer.compute_score_from_context
ScoreResult = _v2_scorer.ScoreResult

__all__ = ["compute_score_from_context", "ScoreResult"]
