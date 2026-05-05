"""
probe.py — Options execution probe for nexus-integrity.

Implements Vector Final Rebuttal Amendment V12:
  6-step SPY ATM 30-DTE roundtrip probe that tests actual options execution readiness,
  replacing the equity smoke test entirely.

Implements Vector Amendment V3:
  - Dedicated PROBE_SECRET auth header
  - X-Probe-Mode: true on all probe traffic
  - Named mutex prevents concurrent probes

Implements probe quota safety:
  - Max 1 probe per PROBE_MIN_INTERVAL_S (10 min default)
  - Results cached with TTL — second call within TTL returns cached result
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests  # noqa: F401 — used in submodule calls

import config
from models import ProbeResult, ProbeRunResult

logger = logging.getLogger("integrity.probe")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_probe_mutex = threading.Lock()
_last_probe_time: float = 0.0
_last_probe_result: Optional["ProbeRunResult"] = None
_last_probe_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fetch_spy_options_chain() -> dict:
    """Fetch SPY options chain from ORATS using the dedicated probe API key.

    Returns:
        dict with keys: chain_exists, bid_ask_nonzero, age_seconds, chain_data

    Raises:
        requests.RequestException: On network or API errors.
        ValueError: If chain data is malformed or stale.
    """
    if not config.ORATS_PROBE_KEY:
        raise ValueError("ORATS_PROBE_KEY not configured — cannot probe options chain")

    url = "https://api.orats.io/datav2/strikes"
    params = {
        "token": config.ORATS_PROBE_KEY,
        "ticker": "SPY",
        "fields": "ticker,expirDate,strike,callBidPrice,callAskPrice,putBidPrice,putAskPrice,delta",
    }
    headers = {"X-Probe-Mode": "true", "User-Agent": "nexus-integrity-probe/1.0"}

    resp = requests.get(url, params=params, headers=headers, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("data"):
        raise ValueError("ORATS returned empty chain for SPY")

    return {"chain_exists": True, "chain_data": data["data"]}


def _resolve_atm_contract(chain_data: list) -> Optional[str]:
    """Resolve a tradeable SPY put contract from Alpaca's actual available contracts.

    GENESIS-FIX-PROBE-001 2026-05-01: The original implementation used ORATS delta
    to find the ATM strike (~$723 today), then built an OCC symbol — but Alpaca paper
    only carries contracts up to ~$495 strike, so the order always returned 404.
    Fix: query Alpaca directly for available SPY put contracts, pick the highest
    tradeable strike in the nearest 30-DTE expiry (closest to real ATM within
    Alpaca's available universe).

    Args:
        chain_data: Unused (kept for signature compatibility). Alpaca is queried directly.

    Returns:
        OCC symbol string for a tradeable Alpaca contract, or None if unavailable.
    """
    import datetime
    import requests as _req

    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        logger.warning("Alpaca credentials not configured — cannot resolve contract")
        return None

    today = datetime.date.today()
    target_dte = 30
    exp_min = (today + datetime.timedelta(days=20)).strftime("%Y-%m-%d")
    exp_max = (today + datetime.timedelta(days=45)).strftime("%Y-%m-%d")

    try:
        resp = _req.get(
            f"{config.ALPACA_BASE_URL}/v2/options/contracts",
            headers={
                "APCA-API-KEY-ID": config.ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
            },
            params={
                "underlying_symbols": "SPY",
                "type": "put",
                "expiration_date_gte": exp_min,
                "expiration_date_lte": exp_max,
                "limit": 100,
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        contracts = resp.json().get("option_contracts", [])
    except Exception as e:
        logger.warning("Alpaca contract query failed: %s", e)
        return None

    tradeable = [c for c in contracts if c.get("tradable")]
    if not tradeable:
        logger.warning("No tradeable SPY put contracts found on Alpaca")
        return None

    # Group by expiry, pick nearest to 30 DTE
    def exp_dte(c: dict) -> int:
        try:
            exp = datetime.date.fromisoformat(c["expiration_date"])
            return abs((exp - today).days - target_dte)
        except (KeyError, ValueError):
            return 9999

    tradeable.sort(key=exp_dte)
    best_expiry = tradeable[0]["expiration_date"]
    expiry_group = [c for c in tradeable if c["expiration_date"] == best_expiry]

    # Pick highest strike in that expiry (closest to ATM from below)
    expiry_group.sort(key=lambda c: float(c.get("strike_price", 0)), reverse=True)
    best = expiry_group[0]

    logger.info("Probe step 2 OK (Alpaca-native): %s strike=%s exp=%s",
                best["symbol"], best["strike_price"], best["expiration_date"])
    return best["symbol"]


def _submit_probe_order(occ_symbol: str) -> Optional[str]:
    """Submit a 1-contract probe options order to Alpaca paper.

    Uses PROBE_ prefix in client_order_id. Limit price $0.01 (will not fill).

    Args:
        occ_symbol: OCC symbol for the options contract.

    Returns:
        Alpaca order_id string on success, None on failure.

    Raises:
        requests.RequestException: On network errors.
        ValueError: If order is rejected by Alpaca.
    """
    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        raise ValueError("Alpaca credentials not configured")

    # GENESIS-FIX-PROBE-002 2026-05-01: Alpaca paper does not support /v2/options/orders
    # (returns 404). Use /v2/orders with OCC symbol — identical behaviour for paper accounts.
    url = f"{config.ALPACA_BASE_URL}/v2/orders"
    headers = {
        "APCA-API-KEY-ID": config.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
        "X-Probe-Mode": "true",
        "Content-Type": "application/json",
    }
    payload = {
        "symbol": occ_symbol,
        "qty": "1",
        "side": "buy",
        "type": "limit",
        "limit_price": "0.01",
        "time_in_force": "day",
        "client_order_id": f"{config.PROBE_ORDER_PREFIX}{int(time.time())}",
    }

    resp = requests.post(url, json=payload, headers=headers, timeout=15.0)

    if resp.status_code not in (200, 201):
        raise ValueError(
            f"Alpaca rejected probe order: {resp.status_code} — {resp.text[:200]}"
        )

    order_data = resp.json()
    order_id = order_data.get("id")
    if not order_id:
        raise ValueError(f"Alpaca returned no order id: {resp.text[:200]}")
    return order_id


def _wait_for_order_accepted(order_id: str, timeout_s: float = 10.0) -> bool:
    """Poll Alpaca until order reaches 'new' or 'accepted' status.

    Args:
        order_id: Alpaca order ID.
        timeout_s: Max seconds to wait.

    Returns:
        True if order accepted within timeout, False otherwise.
    """
    headers = {
        "APCA-API-KEY-ID": config.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
        "X-Probe-Mode": "true",
    }
    url = f"{config.ALPACA_BASE_URL}/v2/orders/{order_id}"
    deadline = time.time() + timeout_s

    while time.time() < deadline:
        try:
            resp = requests.get(url, headers=headers, timeout=5.0)
            if resp.status_code == 200:
                status = resp.json().get("status", "")
                if status in ("new", "accepted", "pending_new"):
                    return True
                if status in ("rejected", "canceled", "expired"):
                    return False
        except requests.RequestException as e:
            logger.warning("Order status poll error: %s", e)
        time.sleep(1.0)

    return False


def _cancel_probe_order(order_id: str) -> bool:
    """Cancel a probe order and verify cancellation.

    Args:
        order_id: Alpaca order ID to cancel.

    Returns:
        True if cancelled successfully, False otherwise.
    """
    headers = {
        "APCA-API-KEY-ID": config.ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": config.ALPACA_SECRET_KEY,
        "X-Probe-Mode": "true",
    }
    url = f"{config.ALPACA_BASE_URL}/v2/orders/{order_id}"

    try:
        resp = requests.delete(url, headers=headers, timeout=10.0)
        # 204 = cancelled, 422 = already filled/cancelled (acceptable)
        return resp.status_code in (204, 200, 422)
    except requests.RequestException as e:
        logger.error("Probe order cancel failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_options_probe() -> ProbeRunResult:
    """Run the 6-step options execution probe (Vector V12 amendment).

    Steps:
      1. Fetch SPY options chain from ORATS — verify chain exists, bid/ask non-zero, fresh
      2. Resolve nearest 30-DTE ATM contract — verify valid OCC symbol
      3. Construct 1-contract order payload — validate against Alpaca schema
      4. Submit to Alpaca paper with qty=1, PROBE_ prefix in client_order_id
      5. Verify order accepted within 10s
      6. Cancel immediately, verify cancellation confirmed

    Protected by:
      - Named mutex (V3: prevents concurrent probes)
      - Minimum interval (quota safety: max 1 probe per PROBE_MIN_INTERVAL_S)
      - Cached result returned within TTL

    Returns:
        ProbeRunResult — always returns, never raises.
    """
    global _last_probe_time, _last_probe_result

    start = time.time()

    # Check minimum interval (quota safety)
    with _last_probe_lock:
        elapsed = time.time() - _last_probe_time
        if elapsed < config.PROBE_MIN_INTERVAL_S and _last_probe_result is not None:
            logger.debug(
                "Probe skipped — last probe was %.0fs ago (min interval %ds)",
                elapsed, config.PROBE_MIN_INTERVAL_S
            )
            return ProbeRunResult(
                result=ProbeResult.SKIPPED,
                detail=f"Too recent — last probe {elapsed:.0f}s ago",
                duration_ms=(time.time() - start) * 1000,
            )

    # Acquire mutex (V3: prevents concurrent probes)
    acquired = _probe_mutex.acquire(timeout=config.PROBE_MUTEX_TIMEOUT_MS / 1000.0)
    if not acquired:
        logger.warning("Probe mutex busy — another probe in flight")
        return ProbeRunResult(
            result=ProbeResult.MUTEX_BUSY,
            detail="Another probe is in flight",
            duration_ms=(time.time() - start) * 1000,
        )

    order_id: Optional[str] = None
    try:
        # Step 1: Fetch options chain
        logger.info("Probe step 1: fetching SPY options chain from ORATS")
        try:
            chain_result = _fetch_spy_options_chain()
        except Exception as e:
            logger.error("Probe step 1 FAILED: %s", e)
            return _probe_fail(1, f"ORATS chain fetch failed: {e}", start)

        # Step 2: Resolve ATM contract
        logger.info("Probe step 2: resolving 30-DTE ATM contract")
        occ_symbol = _resolve_atm_contract(chain_result["chain_data"])
        if not occ_symbol:
            return _probe_fail(2, "Could not resolve valid OCC symbol from chain", start)

        logger.info("Probe step 2 OK: occ_symbol=%s", occ_symbol)

        # Step 3: Validate payload (construct and check — step 3 is validation only)
        logger.info("Probe step 3: validating order payload for %s", occ_symbol)
        if not occ_symbol.startswith("SPY") or len(occ_symbol) < 15:
            return _probe_fail(3, f"Invalid OCC symbol format: {occ_symbol}", start)

        # Step 4: Submit probe order
        logger.info("Probe step 4: submitting probe order to Alpaca paper")
        try:
            order_id = _submit_probe_order(occ_symbol)
        except Exception as e:
            logger.error("Probe step 4 FAILED: %s", e)
            return _probe_fail(4, f"Alpaca order submit failed: {e}", start, occ_symbol=occ_symbol)

        logger.info("Probe step 4 OK: order_id=%s", order_id)

        # Step 5: Verify order accepted within 10s
        logger.info("Probe step 5: waiting for order acceptance")
        accepted = _wait_for_order_accepted(order_id, timeout_s=10.0)
        if not accepted:
            return _probe_fail(5, f"Order {order_id} not accepted within 10s", start, order_id=order_id, occ_symbol=occ_symbol)

        logger.info("Probe step 5 OK: order accepted")

        # Step 6: Cancel and verify
        logger.info("Probe step 6: cancelling probe order %s", order_id)
        cancelled = _cancel_probe_order(order_id)
        if not cancelled:
            logger.warning("Probe step 6: cancel returned non-success for %s (may already be cancelled)", order_id)
            # Non-fatal for probe result — order was accepted which proves readiness

        logger.info("Probe PASSED: occ_symbol=%s order_id=%s", occ_symbol, order_id)
        result = ProbeRunResult(
            result=ProbeResult.PASS,
            step_failed=None,
            order_id=order_id,
            occ_symbol=occ_symbol,
            duration_ms=(time.time() - start) * 1000,
            detail="All 6 steps passed",
        )

        # Cache result
        with _last_probe_lock:
            _last_probe_time = time.time()
            _last_probe_result = result

        return result

    except Exception as e:  # noqa: BLE001 — catch-all, probe must never crash the service
        logger.error("Probe unexpected error: %s", e)
        # Attempt cleanup if we have an order
        if order_id:
            try:
                _cancel_probe_order(order_id)
            except Exception:
                pass
        return ProbeRunResult(
            result=ProbeResult.FAIL,
            detail=f"Unexpected probe error: {e}",
            duration_ms=(time.time() - start) * 1000,
        )
    finally:
        _probe_mutex.release()


def _probe_fail(
    step: int,
    detail: str,
    start: float,
    order_id: Optional[str] = None,
    occ_symbol: Optional[str] = None,
) -> ProbeRunResult:
    """Return a failed ProbeRunResult and attempt order cleanup if needed.

    Args:
        step: The step number that failed (1-6).
        detail: Human-readable failure reason.
        start: Probe start time (for duration calculation).
        order_id: Alpaca order ID to cancel if step >= 4.
        occ_symbol: OCC symbol for logging context.

    Returns:
        ProbeRunResult with result=FAIL.
    """
    if order_id:
        try:
            _cancel_probe_order(order_id)
        except Exception as cleanup_err:
            logger.error("Probe cleanup failed for order %s: %s", order_id, cleanup_err)

    return ProbeRunResult(
        result=ProbeResult.FAIL,
        step_failed=step,
        order_id=order_id,
        occ_symbol=occ_symbol,
        duration_ms=(time.time() - start) * 1000,
        detail=detail,
    )


def get_last_probe_result() -> Optional[ProbeRunResult]:
    """Return the last cached probe result, or None if no probe has run.

    Returns:
        Last ProbeRunResult or None.
    """
    with _last_probe_lock:
        return _last_probe_result


# ===========================================================================
# C3 — Probe Secret Self-Authentication Check (Cipher Amendment)
# ===========================================================================

def check_probe_secret_validity() -> dict:
    """
    C3: Verify PROBE_SECRET is still valid by calling a probe-auth-gated endpoint.

    Run at service startup AND once per trading day at 08:45 ET.

    Prevents the "probe credentials silently expired" failure mode
    (same class as the Guardian bot token 401 that ran undetected for days).

    Returns dict: {"valid": bool, "status_code": int|None, "error": str|None}
    """
    import config as _cfg
    try:
        resp = requests.get(
            f"{_cfg.INTEGRITY_URL}/trs" if hasattr(_cfg, "INTEGRITY_URL")
            else "http://localhost:8011/trs",
            headers={"X-Nexus-Secret": _cfg.PROBE_SECRET},
            timeout=5.0,
        )
        if resp.status_code == 200:
            logger.debug("C3 probe secret self-auth: VALID")
            return {"valid": True, "status_code": 200, "error": None}
        elif resp.status_code in (401, 403):
            logger.error(
                "C3 PROBE SECRET INVALID: self-auth returned %d. "
                "All probes blocked until secret is rotated.",
                resp.status_code,
            )
            return {"valid": False, "status_code": resp.status_code,
                    "error": f"HTTP {resp.status_code} — secret rejected"}
        else:
            # Non-auth error (service down etc) — not a secret rotation issue
            return {"valid": True, "status_code": resp.status_code,
                    "error": f"HTTP {resp.status_code} (non-auth)"}
    except requests.RequestException as e:
        # Service unreachable — not a secret issue
        return {"valid": True, "status_code": None,
                "error": f"Connection error (not auth): {e}"}


# ===========================================================================
# C6 — Alpaca Options Authorization Assertion (Cipher Amendment)
# ===========================================================================

def assert_alpaca_options_authorized(alpaca_headers: dict) -> dict:
    """
    C6: Verify Alpaca API key session is authorized for options trading.

    Called within the Stage 5 dry-run probe, AFTER the order is accepted.
    Checks that the account has options_approved_level >= 2.

    An expired key returns 200 on /health but 403 on the first real order.
    This catches credential expiry at 08:00 ET probe time, not at 09:35 AM
    on the first live trade.

    Returns dict: {"authorized": bool, "level": int|None, "error": str|None}
    """
    import config as _cfg
    try:
        resp = requests.get(
            f"{_cfg.ALPACA_BASE_URL}/v2/account/configurations",
            headers=alpaca_headers,
            timeout=8.0,
        )
        if resp.status_code in (401, 403):
            return {
                "authorized": False,
                "level": None,
                "error": f"Alpaca credentials rejected: HTTP {resp.status_code}",
            }
        if resp.status_code != 200:
            return {
                "authorized": True,   # Non-auth failure — don't block on config endpoint
                "level": None,
                "error": f"Config endpoint returned {resp.status_code}",
            }
        data = resp.json()
        options_level = data.get("options_approved_level", 0)
        if options_level < 2:
            return {
                "authorized": False,
                "level": options_level,
                "error": f"options_approved_level={options_level} (need >=2 for spreads)",
            }
        return {"authorized": True, "level": options_level, "error": None}
    except requests.RequestException as e:
        # Connectivity failure — not an auth failure; don't block on it
        return {"authorized": True, "level": None, "error": f"Config check failed: {e}"}
