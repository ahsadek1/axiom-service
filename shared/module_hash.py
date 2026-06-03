"""
module_hash.py — Shared service module fingerprinting utility.

G7 FIX (2026-05-03): _compute_module_hash() was copy-pasted into 4 services
(alpha-execution, prime-execution, alpha-buffer, prime-buffer). Extracted here
to eliminate drift and ensure a single authoritative implementation.

Usage in each service main.py:
    from shared.module_hash import compute_module_hash
    _CODE_HASH = compute_module_hash(__file__)
"""

from __future__ import annotations

import glob
import hashlib
import os


def compute_module_hash(service_file: str) -> str:
    """Hash all *.py files in the service directory (excluding __pycache__).

    Returns an 8-char hex digest that changes whenever any source file in
    the service changes. Used to detect stale deploys at runtime.

    Args:
        service_file: Path to the service's main.py (pass __file__).

    Returns:
        8-character lowercase hex string.
    """
    svc_dir = os.path.dirname(os.path.abspath(service_file))
    files = sorted(
        f for f in glob.glob(os.path.join(svc_dir, "*.py"))
        if "__pycache__" not in f
    )
    h = hashlib.md5()
    for f in files:
        try:
            with open(f, "rb") as fh:
                h.update(fh.read())
        except Exception:
            pass
    return h.hexdigest()[:8]
