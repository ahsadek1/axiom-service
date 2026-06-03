"""
market_brief_generator.py — One market brief per screening cycle.
All five legends consume this single document — not 250 individual calls.
"""
from __future__ import annotations
import json, logging, os, sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo
import httpx
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)
_ET = ZoneInfo("America/New_York")

SECTOR_ETFS = {
    "Technology":"XLK","Financials":"XLF","Energy":"XLE",
    "Healthcare":"XLV","Industrials":"XLI","Consumer_Staples":"XLP",
    "Utilities":"XLU","Materials":"XLB","Real_Estate":"XLRE",
    "Consumer_Discretionary":"XLY","Communication":"XLC",
}
REGIME_THRESHOLDS = {"LOW_VOL":12.0,"NORMAL":20.0,"ELEVATED":30.0,"STRESS":40.0,"HIGH_STRESS":55.0}

def classify_vix(vix: float) -> str:
    for regime, threshold in REGIME_THRESHOLDS.items():
        if vix <= threshold:
            return regime
    return "CRISIS"

class MarketBriefGenerator:
    def __init__(self, polygon_api_key=None, backtest_db_path=None, stock_universe=None):
        self._polygon_key = polygon_api_key or os.getenv("POLYGON_API_KEY","")
        self._backtest_db = backtest_db_path or os.getenv("BACKTEST_DB_PATH","/Users/ahmedsadek/nexus/data/backtest.db")
        if stock_universe is None:
            try:
                import sys; sys.path.insert(0,"/Users/ahmedsadek/nexus/ails")
                from universe_full import FULL_UNIVERSE
                self._universe = FULL_UNIVERSE
            except ImportError:
                self._universe = []
        else:
            self._universe = stock_universe

    async def generate(self) -> Dict[str, Any]:
        now_et = datetime.now(_ET)
        cycle_id = now_et.strftime("%Y%m%d_%H%M")
        async with httpx.AsyncClient(timeout=15.0) as client:
            vix_data = await self._fetch_vix(client)
            sector_data = await self._fetch_sectors(client)
            movers_data = await self._fetch_movers(client)
        backtest_context = self._load_backtest_context(vix_data["regime"])
        return {
            "cycle_id": cycle_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "market_time_et": now_et.strftime("%Y-%m-%d %H:%M ET"),
            "vix": vix_data,
            "sectors": sector_data,
            "movers": movers_data,
            "backtest_context": backtest_context,
            "screening_universe_size": len(self._universe),
        }

    async def _fetch_vix(self, client):
        try:
            resp = await client.get("https://api.polygon.io/v2/aggs/ticker/I:VIX/prev",params={"apiKey":self._polygon_key})
            if resp.status_code == 200:
                results = resp.json().get("results",[])
                if results:
                    vix = results[0].get("c",18.0)
                    if 9 <= vix <= 85:
                        return {"level":round(vix,2),"regime":classify_vix(vix),"source":"polygon"}
        except Exception as exc:
            logger.warning("VIX fetch error: %s", exc)
        return self._vix_from_db()

    def _vix_from_db(self):
        try:
            conn = sqlite3.connect(self._backtest_db)
            row = conn.execute("SELECT vix_close, regime FROM regime_history ORDER BY date DESC LIMIT 1").fetchone()
            conn.close()
            if row: return {"level":row[0],"regime":row[1],"source":"db_fallback"}
        except Exception: pass
        return {"level":18.0,"regime":"NORMAL","source":"default"}

    async def _fetch_sectors(self, client):
        sectors = {}
        tickers = ",".join(SECTOR_ETFS.values())
        try:
            resp = await client.get("https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers",params={"tickers":tickers,"apiKey":self._polygon_key})
            if resp.status_code == 200:
                ticker_map = {item["ticker"]:item for item in resp.json().get("tickers",[])}
                for sector_name, etf in SECTOR_ETFS.items():
                    item = ticker_map.get(etf,{})
                    day = item.get("day",{}); prev_day = item.get("prevDay",{})
                    close = day.get("c") or prev_day.get("c",0)
                    prev_close = prev_day.get("c",close)
                    pct = ((close-prev_close)/prev_close*100) if prev_close else 0
                    sectors[sector_name] = {"ticker":etf,"close":round(close,2),"pct_change":round(pct,2)}
        except Exception as exc:
            logger.warning("Sector fetch error: %s", exc)
        sorted_s = dict(sorted(sectors.items(),key=lambda x: x[1].get("pct_change",0),reverse=True))
        return {"by_sector":sorted_s,"leaders":[k for k,v in sorted_s.items() if v.get("pct_change",0)>0.5][:3],"laggards":[k for k,v in sorted_s.items() if v.get("pct_change",0)<-0.5][-3:]}

    async def _fetch_movers(self, client):
        if not self._universe: return {"up":[],"down":[],"unusual_volume":[]}
        chunks = [self._universe[i:i+250] for i in range(0,len(self._universe),250)]
        all_items = []
        try:
            for chunk in chunks:
                resp = await client.get("https://api.polygon.io/v2/snapshot/locale/us/markets/stocks/tickers",params={"tickers":",".join(chunk),"apiKey":self._polygon_key})
                if resp.status_code == 200: all_items.extend(resp.json().get("tickers",[]))
        except Exception as exc:
            logger.warning("Movers fetch error: %s", exc)
            return {"up":[],"down":[],"unusual_volume":[]}
        parsed = []; unusual = []
        for item in all_items:
            ticker = item.get("ticker",""); day = item.get("day",{}); prev_day = item.get("prevDay",{})
            close = day.get("c",0); prev_close = prev_day.get("c",close); volume = day.get("v",0)
            avg_volume = item.get("avgVolume",volume) or volume
            pct = ((close-prev_close)/prev_close*100) if prev_close else 0
            parsed.append({"ticker":ticker,"pct_change":round(pct,2),"close":round(close,2),"volume":int(volume)})
            if avg_volume > 0 and volume > avg_volume*2:
                unusual.append({"ticker":ticker,"volume_ratio":round(volume/avg_volume,1),"pct_change":round(pct,2)})
        parsed.sort(key=lambda x: x["pct_change"],reverse=True)
        return {"up":parsed[:10],"down":parsed[-10:][::-1],"unusual_volume":sorted(unusual,key=lambda x: x["volume_ratio"],reverse=True)[:15]}

    def _load_backtest_context(self, regime):
        try:
            conn = sqlite3.connect(self._backtest_db)
            rows = conn.execute("""SELECT strategy,direction,AVG(win_rate) as avg_win_rate,COUNT(DISTINCT ticker) as ticker_count FROM historical_win_rates WHERE regime=? GROUP BY strategy,direction ORDER BY strategy,direction""",(regime,)).fetchall()
            top_spread = conn.execute("""SELECT ticker,win_rate,sample_count FROM historical_win_rates WHERE strategy='bull_put_spread' AND regime=? AND direction='bullish' AND sample_count>=10 ORDER BY win_rate DESC LIMIT 20""",(regime,)).fetchall()
            conn.close()
            strategy_summary = {f"{r[0]}_{r[1]}":{"strategy":r[0],"direction":r[1],"avg_win_rate":round(r[2],4),"ticker_count":r[3]} for r in rows}
            return {"regime":regime,"strategy_averages":strategy_summary,"top_bull_put_spread":[{"ticker":r[0],"win_rate":round(r[1],4),"samples":r[2]} for r in top_spread],"note":f"Historical win rates for {regime} regime."}
        except Exception as exc:
            return {"regime":regime,"strategy_averages":{},"error":str(exc)}
