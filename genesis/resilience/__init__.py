"""
GENESIS 30% Resilience Layer — agent-specific hardening.
Spec: genesis-resilience-v1.md v1.2 (Cipher-reviewed, Ahmed-approved 2026-05-03)

Wires pre-deploy contract gate, stale deploy detection, automated diagnosis,
atomic rollback, and Iron Triad coordination into GENESIS's build pipeline.
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from .deploy_gate import load_circuit_registry, refresh_registry_from_chronicle, wal_write, rotate_wal_if_needed, enter_standby
from .stale_monitor import check_stale_deploys, auto_restart_stale
from .diagnosis import load_diagnosis_tree, diagnose
from .rollback import tag_pre_deploy, check_post_deploy_health, auto_rollback
from .coordinator import propose_fix, register_api_key, check_api_key_expiry, run_chaos_test, CHAOS_SAFE_TO_KILL

__all__ = [
    # deploy_gate
    "load_circuit_registry",
    "refresh_registry_from_chronicle",
    "wal_write",
    "rotate_wal_if_needed",
    "enter_standby",
    # stale_monitor
    "check_stale_deploys",
    "auto_restart_stale",
    # diagnosis
    "load_diagnosis_tree",
    "diagnose",
    # rollback
    "tag_pre_deploy",
    "check_post_deploy_health",
    "auto_rollback",
    # coordinator
    "propose_fix",
    "register_api_key",
    "check_api_key_expiry",
    "run_chaos_test",
    "CHAOS_SAFE_TO_KILL",
]
