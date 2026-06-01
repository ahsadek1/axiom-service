#!/usr/bin/env python3
"""
Memory Profiling Tool for OMNI Synthesis Pipeline

Quick analysis focusing on identified bottlenecks.
"""

import json
import sys
import time
import tracemalloc
from typing import Dict
import os

sys.path.insert(0, "/Users/ahmedsadek/nexus")
sys.path.insert(0, "/Users/ahmedsadek/nexus/omni")

def format_bytes(b: int) -> str:
    """Format bytes to human readable."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if b < 1024:
            return f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}TB"

def profile_step(name: str, fn, iterations: int = 1) -> Dict[str, any]:
    """Generic profiling function."""
    tracemalloc.start()
    
    # Run iterations
    for i in range(iterations):
        fn()
    
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    
    return {
        "step": name,
        "iterations": iterations,
        "current_mb": current / (1024 * 1024),
        "peak_mb": peak / (1024 * 1024),
        "per_iteration_kb": (peak / iterations) / 1024,
    }

def test_context_building():
    """Test context building memory."""
    from synthesis import build_context
    
    concordance = {
        "ticker": "AAPL",
        "direction": "bullish",
        "system": "alpha",
        "pathway": "P3",
        "weighted_score": 72.5,
        "agents_involved": ["bull_put_spread", "call_ratio_spread", "iron_butterfly"],
        "scores": {"bull_put_spread": 75.2, "call_ratio_spread": 68.3},
        "verdict": "GO",
        "echo_chamber": False,
        "notes": ["High confidence signal"],
        "window_id": "win_12345",
    }
    
    axiom_result = {
        "risk_score": 4,
        "sizing_mult": 0.85,
        "in_pool": True,
        "concern_1": "Elevated IV",
        "concern_2": None,
        "hard_stops": [],
    }
    
    regime = {
        "classification": "Risk-On",
        "vix": 14.2,
        "strategy_bias": "bullish",
        "alpha_debit_allowed": True,
        "alpha_credit_allowed": True,
        "prime_allowed": True,
    }
    
    oracle_ctx = {
        "ticker": "AAPL",
        "flow_score": 0.68,
        "gamma_exposure": "long",
        "dealer_delta": -0.15,
    }
    
    return build_context(concordance, axiom_result, regime, oracle_ctx)

def test_verdict_computation():
    """Test verdict computation memory."""
    from synthesis import compute_verdict
    
    brain_results = {
        "claude": {"vote": "GO", "echo_chamber": False, "error": None},
        "o3mini": {"vote": "GO", "echo_chamber": False, "error": None},
        "gemini": {"vote": "NO_GO", "echo_chamber": False, "error": None},
        "deepseek": {"vote": "GO", "echo_chamber": False, "error": None},
    }
    
    return compute_verdict(
        brain_results=brain_results,
        pathway="P3",
        concordance_sizing=0.75,
        axiom_result={"hard_stops": [], "sizing_mult": 0.85},
        thesis_ctx={"sizing_multiplier": 1.0},
    )

def test_json_serialization():
    """Test JSON serialization overhead."""
    context = {
        "ticker": "AAPL",
        "direction": "bullish",
        "system": "alpha",
        "pathway": "P3",
        "agent_weighted_score": 72.5,
        "agents_involved": ["bull_put_spread", "call_ratio_spread", "iron_butterfly"],
        "agent_scores": {"bull_put_spread": 75.2, "call_ratio_spread": 68.3},
        "axiom": {
            "risk_score": 4,
            "sizing_mult": 0.85,
            "concern_1": "Elevated IV",
            "concern_2": None,
            "hard_stops": [],
        },
        "regime": {
            "classification": "Risk-On",
            "vix": 14.2,
            "strategy_bias": "bullish",
            "alpha_debit_allowed": True,
        },
    }
    
    # Serialize to JSON (what goes to brains)
    json_str = json.dumps(context, indent=2, default=str)
    return json_str

def main():
    """Run profiling."""
    print("=" * 80)
    print("OMNI SYNTHESIS MEMORY ANALYSIS")
    print("=" * 80)
    
    print("\n[1/3] Context Building (100 iterations)...")
    r1 = profile_step("context_building", test_context_building, iterations=100)
    print(f"  Peak memory: {format_bytes(int(r1['peak_mb'] * 1024 * 1024))}")
    print(f"  Per iteration: {format_bytes(int(r1['per_iteration_kb'] * 1024))}")
    
    print("\n[2/3] Verdict Computation (100 iterations)...")
    r2 = profile_step("verdict_computation", test_verdict_computation, iterations=100)
    print(f"  Peak memory: {format_bytes(int(r2['peak_mb'] * 1024 * 1024))}")
    print(f"  Per iteration: {format_bytes(int(r2['per_iteration_kb'] * 1024))}")
    
    print("\n[3/3] JSON Serialization (1000 iterations)...")
    r3 = profile_step("json_serialization", test_json_serialization, iterations=1000)
    print(f"  Peak memory: {format_bytes(int(r3['peak_mb'] * 1024 * 1024))}")
    print(f"  Per iteration: {format_bytes(int(r3['per_iteration_kb'] * 1024))}")
    
    print("\n" + "=" * 80)
    
    # Analysis
    context_size = r1['peak_mb']
    verdict_size = r2['peak_mb']
    json_size = r3['peak_mb'] / 10  # Normalized to 100 iterations
    
    print("\n=== MEMORY ANALYSIS ===")
    print(f"Context building baseline: {context_size:.1f}MB for 100 iterations")
    print(f"Verdict computation baseline: {verdict_size:.1f}MB for 100 iterations")
    print(f"JSON serialization baseline: {json_size:.1f}MB for 100 iterations")
    
    concurrent_verdicts = 3  # synthesis pool size
    total_per_concurrent_set = (context_size + verdict_size + json_size) / 100
    total_3_concurrent = total_per_concurrent_set * concurrent_verdicts
    
    print(f"\nPer verdict cycle (estimated): {format_bytes(int(total_per_concurrent_set * 1024 * 1024))}")
    print(f"3 concurrent verdicts: {format_bytes(int(total_3_concurrent * 1024 * 1024))}")
    
    print("\n=== KEY FINDINGS ===")
    print("1. Context building creates complete dict with oracle/regime data")
    print("   → Potential optimization: lazy-load only needed fields")
    print("")
    print("2. JSON serialization happens per brain call (4 calls per verdict)")
    print("   → Potential optimization: serialize once, reuse for all brains")
    print("")
    print("3. Brain results accumulate in memory during pool processing")
    print("   → Potential optimization: stream results, release after use")

if __name__ == "__main__":
    main()
