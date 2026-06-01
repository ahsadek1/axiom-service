"""
error_taxonomy.py — Universal Alpaca Error Taxonomy
====================================================
Every possible Alpaca failure. Every error has:
  - A code (EAL001-EAL020)
  - A severity (P0-P3)
  - A detection method
  - A deterministic fix sequence
  - A can_auto_fix flag
  - An escalation target

Ahmed directive: "I want you to not rely much on what already exists
as it is inefficient at best. Comprehensive list of Alpaca errors/bugs
and failures with deterministic code that heals any error automatically."
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AlpacaErrorCode(str, Enum):
    # ── Connectivity ──────────────────────────────────────────────────────────
    EAL001 = "EAL001"   # API timeout
    EAL002 = "EAL002"   # Rate limited (429)
    EAL003 = "EAL003"   # Service down (5xx)
    EAL004 = "EAL004"   # SSL/TLS failure
    EAL005 = "EAL005"   # DNS failure

    # ── Authentication ────────────────────────────────────────────────────────
    EAL006 = "EAL006"   # Invalid credentials (401)
    EAL007 = "EAL007"   # Account blocked/restricted (403)
    EAL008 = "EAL008"   # Options not approved

    # ── Orders ────────────────────────────────────────────────────────────────
    EAL009 = "EAL009"   # Order rejected (422)
    EAL010 = "EAL010"   # Order fill timeout
    EAL011 = "EAL011"   # Partial fill — leg mismatch
    EAL012 = "EAL012"   # Order stuck pending
    EAL013 = "EAL013"   # Duplicate order detected

    # ── Positions ─────────────────────────────────────────────────────────────
    EAL014 = "EAL014"   # Ghost position (DB open, Alpaca absent)
    EAL015 = "EAL015"   # Orphan position (Alpaca has, DB missing)
    EAL016 = "EAL016"   # Position size mismatch
    EAL017 = "EAL017"   # Expired option still tracked as open

    # ── Capital ───────────────────────────────────────────────────────────────
    EAL018 = "EAL018"   # Buying power drift (allocated ≠ actual)
    EAL019 = "EAL019"   # Buying power critically low
    EAL020 = "EAL020"   # Margin call detected


class Severity(str, Enum):
    P0 = "P0"   # Critical — halt trading immediately
    P1 = "P1"   # High — fix within 10 min
    P2 = "P2"   # Medium — fix within 30 min
    P3 = "P3"   # Low — fix at next opportunity


@dataclass
class AlpacaErrorDefinition:
    code:            AlpacaErrorCode
    name:            str
    severity:        Severity
    description:     str
    detection:       str
    fix_sequence:    list[str]
    can_auto_fix:    bool = True
    max_attempts:    int  = 3
    backoff_seconds: list[int] = field(default_factory=lambda: [5, 15, 30])
    escalate_to:     str = "cipher"   # dispatch chain target


ALPACA_ERROR_REGISTRY: dict[AlpacaErrorCode, AlpacaErrorDefinition] = {

    # ── Connectivity ──────────────────────────────────────────────────────────

    AlpacaErrorCode.EAL001: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL001,
        name="API Timeout",
        severity=Severity.P1,
        description="Alpaca API call timed out — network or broker latency",
        detection="requests.Timeout or connection timeout > 10s",
        fix_sequence=[
            "Wait 5s then retry with fresh TCP connection",
            "Wait 15s then retry",
            "Wait 30s then probe /v2/account",
            "If still failing: set account state DEGRADED, pause trading",
            "Continue retrying every 60s until recovered",
        ],
        backoff_seconds=[5, 15, 30],
    ),

    AlpacaErrorCode.EAL002: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL002,
        name="Rate Limited",
        severity=Severity.P1,
        description="429 Too Many Requests — exceeding Alpaca API rate limits",
        detection="HTTP 429 response",
        fix_sequence=[
            "Read Retry-After header (default 60s if absent)",
            "Wait Retry-After seconds",
            "Implement request throttling for remainder of session",
            "Batch future requests to stay under limit",
            "Resume with reduced request frequency",
        ],
        backoff_seconds=[60, 120, 180],
        max_attempts=5,
    ),

    AlpacaErrorCode.EAL003: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL003,
        name="Service Down",
        severity=Severity.P0,
        description="Alpaca returning 5xx — broker infrastructure issue",
        detection="HTTP 500/502/503/504 response",
        fix_sequence=[
            "Wait 30s then probe /v2/account",
            "Wait 60s then probe again",
            "Wait 120s then probe again",
            "If still down: halt all trading, set account state DOWN",
            "Continue probing every 120s until recovered",
            "On recovery: reconcile all positions before resuming",
        ],
        backoff_seconds=[30, 60, 120],
        max_attempts=10,
    ),

    AlpacaErrorCode.EAL004: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL004,
        name="SSL/TLS Failure",
        severity=Severity.P1,
        description="SSL certificate or TLS handshake failure",
        detection="SSLError or certificate verification failure",
        fix_sequence=[
            "Create new requests.Session() with fresh SSL context",
            "Retry with new session",
            "If persists: check system time (TLS requires accurate clock)",
            "Escalate if cannot establish secure connection",
        ],
        backoff_seconds=[5, 15, 30],
    ),

    AlpacaErrorCode.EAL005: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL005,
        name="DNS Failure",
        severity=Severity.P1,
        description="Cannot resolve paper-api.alpaca.markets",
        detection="socket.gaierror or Name or service not known",
        fix_sequence=[
            "Wait 10s then retry DNS resolution",
            "Flush DNS cache: subprocess dscacheutil -flushcache",
            "Try alternate DNS (8.8.8.8)",
            "Alert if network partition detected",
        ],
        backoff_seconds=[10, 30, 60],
    ),

    # ── Authentication ────────────────────────────────────────────────────────

    AlpacaErrorCode.EAL006: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL006,
        name="Invalid Credentials",
        severity=Severity.P0,
        description="401 Unauthorized — API key invalid or expired",
        detection="HTTP 401 response",
        fix_sequence=[
            "Probe /v2/account to confirm 401",
            "Log credentials fingerprint (first 8 chars only)",
            "Alert Ahmed — credentials require manual rotation",
            "Halt all trading immediately",
        ],
        can_auto_fix=False,
        max_attempts=1,
    ),

    AlpacaErrorCode.EAL007: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL007,
        name="Account Blocked",
        severity=Severity.P0,
        description="403 — account restricted or suspended",
        detection="HTTP 403 + account status != ACTIVE",
        fix_sequence=[
            "Fetch account status from /v2/account",
            "Wait 60s (paper account throttle may self-clear)",
            "Probe again — if ACTIVE: resume",
            "If still blocked: halt trading, alert Ahmed immediately",
        ],
        backoff_seconds=[60, 120],
        max_attempts=2,
    ),

    AlpacaErrorCode.EAL008: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL008,
        name="Options Not Approved",
        severity=Severity.P0,
        description="Account does not have options trading level 3",
        detection="options_approved_level < 3 or options_trading_level < 3",
        fix_sequence=[
            "Check account options levels",
            "Alert Ahmed — requires Alpaca account upgrade",
            "Halt options trading immediately",
        ],
        can_auto_fix=False,
        max_attempts=1,
    ),

    # ── Orders ────────────────────────────────────────────────────────────────

    AlpacaErrorCode.EAL009: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL009,
        name="Order Rejected",
        severity=Severity.P2,
        description="422 — order rejected by Alpaca (invalid params, buying power, etc)",
        detection="HTTP 422 on order submission",
        fix_sequence=[
            "Parse rejection reason from response body",
            "If buying power: check current BP and reduce position size",
            "If invalid symbol: verify contract symbol, try alternate expiry",
            "If market closed: queue for next market open",
            "Skip ticker for this session if unresolvable",
            "Log rejection reason for learning loop",
        ],
        backoff_seconds=[0],
        max_attempts=2,
    ),

    AlpacaErrorCode.EAL010: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL010,
        name="Order Fill Timeout",
        severity=Severity.P1,
        description="Order submitted but not filled within timeout window",
        detection="Order status != filled after 30s (market) or 120s (limit)",
        fix_sequence=[
            "Check current order status via /v2/orders/{id}",
            "If partially_filled: decide to accept partial or cancel",
            "If still pending: cancel and resubmit as market order",
            "If market order also times out: cancel both legs, alert",
            "If spread — ensure both legs are synchronized (cancel unfilled if other filled)",
        ],
        backoff_seconds=[5, 10],
        max_attempts=3,
    ),

    AlpacaErrorCode.EAL011: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL011,
        name="Partial Fill Leg Mismatch",
        severity=Severity.P0,
        description="One leg of spread filled, other leg failed — naked position risk",
        detection="short leg filled AND long leg not filled (or vice versa)",
        fix_sequence=[
            "IMMEDIATELY identify which leg is filled and which failed",
            "If short filled, long failed: BUY long leg at market (emergency hedge)",
            "If long filled, short failed: SELL short leg at market",
            "Do NOT leave naked options position under any circumstance",
            "Alert Ahmed after legs are balanced",
            "Block this ticker for remainder of session",
        ],
        can_auto_fix=True,
        max_attempts=1,  # Act immediately, no retry delay
        backoff_seconds=[0],
    ),

    AlpacaErrorCode.EAL012: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL012,
        name="Order Stuck Pending",
        severity=Severity.P1,
        description="Order in pending_new or accepted state for >60s",
        detection="order status in (pending_new, accepted) for >60s",
        fix_sequence=[
            "Attempt cancel via DELETE /v2/orders/{id}",
            "If cancel succeeds: resubmit as market order",
            "If cancel fails (already filled): record fill, update DB",
            "If cancel fails (unknown): check order status, wait 30s, retry",
        ],
        backoff_seconds=[10, 30],
        max_attempts=3,
    ),

    AlpacaErrorCode.EAL013: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL013,
        name="Duplicate Order Detected",
        severity=Severity.P1,
        description="Same order submitted twice — idempotency violation",
        detection="client_order_id already exists on Alpaca (422 with existing order)",
        fix_sequence=[
            "Fetch existing order by client_order_id",
            "If existing order is filled: record fill, do not resubmit",
            "If existing order is pending: monitor for fill",
            "If existing order is canceled: safe to resubmit with new COID",
            "Never submit duplicate — always check COID first",
        ],
        backoff_seconds=[0],
        max_attempts=1,
    ),

    # ── Positions ─────────────────────────────────────────────────────────────

    AlpacaErrorCode.EAL014: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL014,
        name="Ghost Position",
        severity=Severity.P1,
        description="Position open in DB but absent from Alpaca for >5 minutes",
        detection="DB status=OPEN AND symbol not in Alpaca positions AND no pending order",
        fix_sequence=[
            "Verify position is genuinely absent (not in fill window)",
            "Check order status by order_id — if filled: Alpaca propagation lag",
            "Wait 5 minutes: if still absent, confirm ghost",
            "Mark position as GHOST in DB",
            "Release allocated capital back to buying power",
            "Log ghost with full context for investigation",
            "Alert via Telegram with position details",
        ],
        backoff_seconds=[30, 60, 300],
        max_attempts=3,
    ),

    AlpacaErrorCode.EAL015: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL015,
        name="Orphan Position",
        severity=Severity.P1,
        description="Position in Alpaca but not tracked in DB — unmanaged risk",
        detection="symbol in Alpaca positions AND not in DB as OPEN",
        fix_sequence=[
            "Fetch full position details from Alpaca",
            "Check if this is a THESIS, Alpha, or Prime position by symbol pattern",
            "Attempt to match to a closed DB record (may be timing issue)",
            "If unmatched: create DB record as ORPHAN status",
            "Alert Ahmed — orphan positions represent unmanaged risk",
            "Do NOT close automatically — could be legitimate",
        ],
        can_auto_fix=False,
        max_attempts=1,
    ),

    AlpacaErrorCode.EAL016: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL016,
        name="Position Size Mismatch",
        severity=Severity.P1,
        description="DB quantity differs from Alpaca quantity for same position",
        detection="DB qty != Alpaca qty for matching position",
        fix_sequence=[
            "Fetch Alpaca position qty as ground truth",
            "Update DB to match Alpaca qty",
            "Recalculate P&L and exit targets based on actual qty",
            "Log mismatch with old and new values",
        ],
        backoff_seconds=[0],
        max_attempts=1,
    ),

    AlpacaErrorCode.EAL017: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL017,
        name="Expired Option Tracked Open",
        severity=Severity.P0,
        description="Option contract past expiration still tracked as OPEN in DB",
        detection="DB status=OPEN AND expiration_date < today",
        fix_sequence=[
            "Verify expiration date from contract symbol",
            "Check Alpaca for any residual position",
            "If Alpaca shows zero: close DB record with EXPIRED status",
            "Calculate final P&L (expired worthless or assigned)",
            "Release allocated capital",
            "Alert Ahmed if assignment detected (requires manual review)",
        ],
        backoff_seconds=[0],
        max_attempts=1,
    ),

    # ── Capital ───────────────────────────────────────────────────────────────

    AlpacaErrorCode.EAL018: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL018,
        name="Capital Drift",
        severity=Severity.P1,
        description="Allocated capital in DB diverges from Alpaca buying power by >$500",
        detection="abs(db_allocated - alpaca_buying_power) > 500",
        fix_sequence=[
            "Fetch current Alpaca buying power",
            "Calculate total DB allocated capital from open positions",
            "Compute drift = abs(db_allocated - alpaca_bp)",
            "If drift > $500: trigger full position reconciliation",
            "Sync DB allocated to match Alpaca as source of truth",
            "Log drift amount and root cause",
        ],
        backoff_seconds=[0],
        max_attempts=1,
    ),

    AlpacaErrorCode.EAL019: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL019,
        name="Buying Power Critical",
        severity=Severity.P0,
        description="Options buying power below $10,000 — insufficient for new trades",
        detection="options_buying_power < 10000",
        fix_sequence=[
            "Halt new position entries immediately",
            "Check for ghost positions consuming allocated capital",
            "Run ghost hunter — may free up capital",
            "Alert Ahmed if buying power still low after ghost cleanup",
        ],
        backoff_seconds=[0],
        max_attempts=1,
    ),

    AlpacaErrorCode.EAL020: AlpacaErrorDefinition(
        code=AlpacaErrorCode.EAL020,
        name="Margin Call",
        severity=Severity.P0,
        description="Account equity below margin requirement",
        detection="account equity < maintenance_margin",
        fix_sequence=[
            "HALT ALL TRADING IMMEDIATELY",
            "Alert Ahmed with full account state",
            "Do NOT auto-close positions (could worsen situation)",
            "Wait for Ahmed's explicit instruction",
        ],
        can_auto_fix=False,
        max_attempts=1,
    ),
}


class AlpacaError(Exception):
    """Raised when Alpaca returns a non-2xx response."""
    def __init__(
        self,
        status_code: int,
        body: str,
        error_code: Optional[AlpacaErrorCode] = None,
        retry_after: Optional[int] = None,
    ):
        self.status_code  = status_code
        self.body         = body
        self.error_code   = error_code
        self.retry_after  = retry_after
        super().__init__(f"Alpaca {status_code}: {body[:200]}")


def classify_http_error(status_code: int, body: str) -> AlpacaErrorCode:
    """Map HTTP status code + body to AlpacaErrorCode."""
    body_lower = body.lower()
    if status_code == 401:
        return AlpacaErrorCode.EAL006
    if status_code == 403:
        if any(w in body_lower for w in ("account", "restricted", "blocked", "suspended")):
            return AlpacaErrorCode.EAL007
        if any(w in body_lower for w in ("options", "level", "approved")):
            return AlpacaErrorCode.EAL008
        return AlpacaErrorCode.EAL006
    if status_code == 422:
        return AlpacaErrorCode.EAL009
    if status_code == 429:
        return AlpacaErrorCode.EAL002
    if status_code >= 500:
        return AlpacaErrorCode.EAL003
    return AlpacaErrorCode.EAL001


def classify_exception(exc: Exception) -> AlpacaErrorCode:
    """Map Python exception to AlpacaErrorCode."""
    name = type(exc).__name__
    msg  = str(exc).lower()
    if "timeout" in name.lower() or "timeout" in msg:
        return AlpacaErrorCode.EAL001
    if "ssl" in name.lower() or "ssl" in msg:
        return AlpacaErrorCode.EAL004
    if "gaierror" in name.lower() or "name or service" in msg or "dns" in msg:
        return AlpacaErrorCode.EAL005
    if "connection" in name.lower() or "connection" in msg:
        return AlpacaErrorCode.EAL001
    return AlpacaErrorCode.EAL001
