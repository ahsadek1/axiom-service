"""
ghost_hunter.py — Continuous Ghost Position & Capital Drift Detector
=====================================================================
Runs every 5 minutes. Compares DB positions to Alpaca ground truth.
Automatically fixes every discrepancy it can. Alerts on what it cannot.

Ghost position:   DB says OPEN — Alpaca has nothing — capital locked for no reason
Orphan position:  Alpaca has position — DB has no record — unmanaged risk
Size mismatch:    DB qty != Alpaca qty — wrong P&L, wrong exit targets
Capital drift:    Allocated capital in DB != Alpaca buying power
Expired option:   Option past expiration still tracked as OPEN

This runs for EVERY system — THESIS, Alpha, Prime, all SQS accounts.
Each system passes its own DB path and executor instance.
"""
from __future__ import annotations
import logging
import os
import sqlite3
import time
from datetime import datetime, date, timezone, timedelta
from typing import Optional

from .request_executor import RequestExecutor
from .error_taxonomy import AlpacaErrorCode

log = logging.getLogger("nexus.alpaca.ghost_hunter")

# Ghost confirmation window — position must be absent for this long before cleanup
GHOST_CONFIRM_MINUTES  = 5
# Capital drift threshold — alert if drift exceeds this
DRIFT_THRESHOLD_DOLLARS = 500.0
# Check interval
CHECK_INTERVAL_S        = 300   # 5 minutes


class GhostHunter:
    """
    Continuous ghost position detector and capital drift monitor.

    One instance per system (per DB + per Alpaca account).
    Runs in background thread.

    Usage:
        hunter = GhostHunter(
            system_id="THESIS",
            db_path="/Users/ahmedsadek/nexus/data/thesis_positions.db",
            executor=executor,
            notify_fn=_notify,
        )
        hunter.start()
    """

    def __init__(
        self,
        system_id:    str,
        db_path:      str,
        executor:     RequestExecutor,
        notify_fn:    Optional[callable] = None,
        positions_table: str = "positions",
        status_column:   str = "status",
        ticker_column:   str = "ticker",
        open_value:      str = "OPEN",
    ):
        self.system_id       = system_id
        self.db_path         = db_path
        self.executor        = executor
        self.notify_fn       = notify_fn
        self.positions_table = positions_table
        self.status_column   = status_column
        self.ticker_column   = ticker_column
        self.open_value      = open_value

        self._running        = False
        self._suspect_ghosts: dict[str, float] = {}  # position_id -> first_seen_absent

    def start(self) -> None:
        """Start ghost hunter in background thread."""
        import threading
        self._running = True
        t = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"ghost-hunter-{self.system_id.lower()}",
        )
        t.start()
        log.info("[%s] Ghost hunter started (interval=%ds)", self.system_id, CHECK_INTERVAL_S)

    def stop(self) -> None:
        self._running = False

    def _run_loop(self) -> None:
        while self._running:
            try:
                self.run_scan()
            except Exception as exc:
                log.error("[%s] Ghost hunter scan error: %s", self.system_id, exc)
            time.sleep(CHECK_INTERVAL_S)

    def run_scan(self) -> dict:
        """
        Run one complete scan. Returns summary dict.
        Called by background thread and can also be called manually.
        """
        log.debug("[%s] Ghost hunter scan starting", self.system_id)

        results = {
            "system_id":      self.system_id,
            "timestamp":      datetime.now(timezone.utc).isoformat(),
            "ghosts_found":   0,
            "ghosts_fixed":   0,
            "orphans_found":  0,
            "size_mismatches": 0,
            "drift_dollars":  0.0,
            "expired_found":  0,
        }

        # Get DB positions
        db_positions = self._get_db_positions()
        if db_positions is None:
            log.error("[%s] Cannot read DB positions", self.system_id)
            return results

        # Get Alpaca positions
        try:
            alpaca_positions = self.executor.get_positions()
            alpaca_orders    = self.executor.get_orders(status="open")
        except Exception as exc:
            log.error("[%s] Cannot fetch Alpaca data: %s", self.system_id, exc)
            return results

        # Build lookup sets
        alpaca_symbols  = {p.get("symbol","").upper() for p in alpaca_positions}
        pending_symbols = {o.get("symbol","").upper() for o in alpaca_orders}
        alpaca_by_sym   = {p.get("symbol","").upper(): p for p in alpaca_positions}

        # ── Check 1: Expired options ──────────────────────────────────────────
        expired = self._check_expired_options(db_positions)
        results["expired_found"] = len(expired)

        # ── Check 2: Ghost positions ──────────────────────────────────────────
        ghosts = self._detect_ghosts(
            db_positions, alpaca_symbols, pending_symbols
        )
        results["ghosts_found"] = len(ghosts)
        fixed = self._fix_ghosts(ghosts)
        results["ghosts_fixed"] = fixed

        # ── Check 3: Size mismatches ──────────────────────────────────────────
        mismatches = self._check_size_mismatches(db_positions, alpaca_by_sym)
        results["size_mismatches"] = len(mismatches)
        if mismatches:
            self._fix_size_mismatches(mismatches, alpaca_by_sym)

        # ── Check 4: Orphan positions ─────────────────────────────────────────
        orphans = self._detect_orphans(db_positions, alpaca_symbols)
        results["orphans_found"] = len(orphans)
        if orphans:
            self._alert_orphans(orphans, alpaca_by_sym)

        # ── Check 5: Capital drift ────────────────────────────────────────────
        drift = self._check_capital_drift(db_positions, alpaca_positions)
        results["drift_dollars"] = drift

        if (results["ghosts_fixed"] > 0 or results["expired_found"] > 0 or
                results["orphans_found"] > 0 or abs(drift) > DRIFT_THRESHOLD_DOLLARS):
            self._notify(
                f"GHOST HUNTER: {self.system_id}\n"
                f"Ghosts fixed: {results['ghosts_fixed']}\n"
                f"Expired options: {results['expired_found']}\n"
                f"Orphans: {results['orphans_found']}\n"
                f"Capital drift: ${drift:+,.0f}\n"
                f"Size mismatches: {results['size_mismatches']}"
            )

        log.info("[%s] Scan complete: %s", self.system_id, results)
        return results

    def _get_db_positions(self) -> Optional[list[dict]]:
        """Load open positions from DB."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM {self.positions_table} "
                f"WHERE {self.status_column}=?",
                (self.open_value,)
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows]
        except Exception as exc:
            log.error("[%s] DB read failed: %s", self.system_id, exc)
            return None

    def _check_expired_options(self, db_positions: list[dict]) -> list[dict]:
        """Find options past their expiration date still tracked as open."""
        expired = []
        today   = date.today()

        for p in db_positions:
            exp = p.get("expiration") or p.get("expiration_date") or ""
            if not exp:
                continue
            try:
                exp_date = date.fromisoformat(exp[:10])
                if exp_date < today:
                    expired.append(p)
                    log.warning(
                        "[%s] Expired option: %s exp=%s still OPEN in DB",
                        self.system_id, p.get(self.ticker_column), exp
                    )
                    self._close_position_in_db(
                        p, "EXPIRED",
                        f"Option expired {exp} — closed automatically"
                    )
            except (ValueError, TypeError):
                pass

        return expired

    def _detect_ghosts(
        self,
        db_positions:   list[dict],
        alpaca_symbols: set,
        pending_symbols: set,
    ) -> list[dict]:
        """
        Detect ghost positions.
        A position is a ghost if:
        - DB says OPEN
        - Not in Alpaca positions
        - Not in pending orders (fill window)
        - Absent for GHOST_CONFIRM_MINUTES
        """
        ghosts = []
        now    = time.time()

        for p in db_positions:
            ticker    = p.get(self.ticker_column,"").upper()
            short_sym = p.get("short_symbol","").upper()
            long_sym  = p.get("long_symbol","").upper()
            pos_id    = str(p.get("position_id") or p.get("id",""))

            # Check if any relevant symbol is in Alpaca
            symbols_to_check = {s for s in [ticker, short_sym, long_sym] if s}
            in_alpaca  = bool(symbols_to_check & alpaca_symbols)
            in_pending = bool(symbols_to_check & pending_symbols)

            if in_alpaca or in_pending:
                # Position is accounted for — clear from suspect list
                self._suspect_ghosts.pop(pos_id, None)
                continue

            # Not in Alpaca — start or update suspect timer
            if pos_id not in self._suspect_ghosts:
                self._suspect_ghosts[pos_id] = now
                log.info(
                    "[%s] Suspect ghost: %s (pos_id=%s) — monitoring for %d min",
                    self.system_id, ticker, pos_id[:8], GHOST_CONFIRM_MINUTES
                )
                continue

            # Check if confirmed ghost (absent long enough)
            first_absent = self._suspect_ghosts[pos_id]
            absent_minutes = (now - first_absent) / 60

            if absent_minutes >= GHOST_CONFIRM_MINUTES:
                ghosts.append(p)
                log.error(
                    "[%s] CONFIRMED GHOST: %s (pos_id=%s) absent %.1f min",
                    self.system_id, ticker, pos_id[:8], absent_minutes
                )

        return ghosts

    def _fix_ghosts(self, ghosts: list[dict]) -> int:
        """Mark confirmed ghosts as GHOST status. Release allocated capital."""
        fixed = 0
        for p in ghosts:
            pos_id = str(p.get("position_id") or p.get("id",""))
            ticker = p.get(self.ticker_column,"")
            success = self._close_position_in_db(
                p, "GHOST",
                f"Ghost position confirmed — absent from Alpaca for {GHOST_CONFIRM_MINUTES}+ min"
            )
            if success:
                fixed += 1
                self._suspect_ghosts.pop(pos_id, None)
                log.info("[%s] Ghost fixed: %s marked GHOST, capital released", self.system_id, ticker)
        return fixed

    def _check_size_mismatches(
        self,
        db_positions: list[dict],
        alpaca_by_sym: dict,
    ) -> list[dict]:
        """Find positions where DB qty != Alpaca qty."""
        mismatches = []
        for p in db_positions:
            ticker = p.get(self.ticker_column,"").upper()
            if ticker not in alpaca_by_sym:
                continue
            alpaca_qty = float(alpaca_by_sym[ticker].get("qty", 0))
            db_qty     = float(p.get("contracts") or p.get("qty") or 0)
            if db_qty > 0 and abs(alpaca_qty - db_qty) > 0.01:
                mismatches.append({
                    "position": p,
                    "db_qty":   db_qty,
                    "alpaca_qty": alpaca_qty,
                })
                log.warning(
                    "[%s] Size mismatch: %s DB=%s Alpaca=%s",
                    self.system_id, ticker, db_qty, alpaca_qty
                )
        return mismatches

    def _fix_size_mismatches(
        self,
        mismatches: list[dict],
        alpaca_by_sym: dict,
    ) -> None:
        """Update DB qty to match Alpaca (Alpaca is ground truth)."""
        try:
            conn = sqlite3.connect(self.db_path, timeout=10)
            for m in mismatches:
                p         = m["position"]
                pos_id    = str(p.get("position_id") or p.get("id",""))
                alpaca_qty = m["alpaca_qty"]
                ticker     = p.get(self.ticker_column,"")

                # Try to update contracts or qty column
                for col in ["contracts", "qty"]:
                    try:
                        conn.execute(
                            f"UPDATE {self.positions_table} SET {col}=? "
                            f"WHERE position_id=? OR id=?",
                            (alpaca_qty, pos_id, pos_id)
                        )
                        log.info("[%s] Size fixed: %s qty updated to %s",
                                self.system_id, ticker, alpaca_qty)
                        break
                    except Exception:
                        pass

            conn.commit()
            conn.close()
        except Exception as exc:
            log.error("[%s] Size mismatch fix failed: %s", self.system_id, exc)

    def _detect_orphans(
        self,
        db_positions: list[dict],
        alpaca_symbols: set,
    ) -> list[str]:
        """Find symbols in Alpaca not tracked in DB."""
        db_tickers = {p.get(self.ticker_column,"").upper() for p in db_positions}
        db_shorts  = {p.get("short_symbol","").upper() for p in db_positions if p.get("short_symbol")}
        db_longs   = {p.get("long_symbol","").upper() for p in db_positions if p.get("long_symbol")}
        db_all     = db_tickers | db_shorts | db_longs

        orphans = [sym for sym in alpaca_symbols if sym and sym not in db_all]
        for sym in orphans:
            log.error(
                "[%s] ORPHAN POSITION: %s in Alpaca but not in DB",
                self.system_id, sym
            )
        return orphans

    def _alert_orphans(self, orphans: list[str], alpaca_by_sym: dict) -> None:
        """Alert on orphan positions — do not auto-close."""
        details = []
        for sym in orphans[:5]:
            pos = alpaca_by_sym.get(sym, {})
            details.append(
                f"{sym}: qty={pos.get('qty',0)} "
                f"value=${float(pos.get('market_value',0)):,.0f}"
            )
        self._notify(
            f"ORPHAN POSITIONS DETECTED: {self.system_id}\n"
            f"These positions are in Alpaca but NOT tracked in DB.\n"
            f"Manual review required.\n"
            + "\n".join(details)
        )

    def _check_capital_drift(
        self,
        db_positions: list[dict],
        alpaca_positions: list[dict],
    ) -> float:
        """
        Calculate capital drift: DB allocated vs Alpaca market value.
        Returns drift in dollars (positive = DB over-allocated).
        """
        try:
            account = self.executor.get_account()
            alpaca_bp = float(account.get("options_buying_power") or
                             account.get("buying_power") or 0)
            total_cap = float(account.get("portfolio_value") or
                             account.get("equity") or 200000)

            # DB allocated = sum of max_loss on open positions
            db_allocated = sum(
                float(p.get("max_loss") or
                      (float(p.get("spread_width",5)) *
                       float(p.get("contracts",1)) * 100))
                for p in db_positions
            )

            # Alpaca available = portfolio_value - market_value_of_positions
            alpaca_allocated = sum(
                abs(float(p.get("cost_basis") or
                         float(p.get("market_value",0))))
                for p in alpaca_positions
            )

            drift = db_allocated - alpaca_allocated

            if abs(drift) > DRIFT_THRESHOLD_DOLLARS:
                log.warning(
                    "[%s] Capital drift: $%.0f (DB=$%.0f Alpaca=$%.0f)",
                    self.system_id, drift, db_allocated, alpaca_allocated
                )

            return round(drift, 2)

        except Exception as exc:
            log.warning("[%s] Capital drift check failed: %s", self.system_id, exc)
            return 0.0

    def _close_position_in_db(
        self,
        position: dict,
        status:   str,
        reason:   str,
    ) -> bool:
        """Update position status in DB."""
        try:
            conn  = sqlite3.connect(self.db_path, timeout=10)
            pos_id = str(position.get("position_id") or position.get("id",""))
            now    = datetime.now(timezone.utc).isoformat()

            # Try options-style positions table first
            for query in [
                f"""UPDATE {self.positions_table}
                    SET status=?, exit_time=?, exit_reason=?, exit_pnl=0
                    WHERE position_id=?""",
                f"""UPDATE {self.positions_table}
                    SET status=?, closed_at=?, close_reason=?, pnl_usd=0
                    WHERE id=?""",
            ]:
                try:
                    cursor = conn.execute(query, (status, now, reason, pos_id))
                    if cursor.rowcount > 0:
                        conn.commit()
                        conn.close()
                        return True
                except Exception:
                    pass

            conn.close()
            return False

        except Exception as exc:
            log.error("[%s] DB update failed: %s", self.system_id, exc)
            return False

    def _notify(self, msg: str) -> None:
        if self.notify_fn:
            try:
                self.notify_fn(msg)
            except Exception:
                pass
