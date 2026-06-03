"""
legend_screener.py — Five Legend Pool Screener
5 LLM calls per cycle. One brief in. One shortlist out.
Tickers with 3+ legend endorsements (score >= 60) → shortlist.
"""
from __future__ import annotations
import json, logging, os, sqlite3, time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
import anthropic
from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)
ENDORSEMENT_THRESHOLD = 55  # lowered from 60 — more candidates qualify
MIN_ENDORSEMENTS = 2  # lowered from 3 — 2/5 legends sufficient for entry
LEGEND_MODEL = "claude-sonnet-4-20250514"

@dataclass
class LegendResult:
    legend_name: str
    scores: Dict[str, int]
    top_picks: List[str]
    cycle_id: str
    elapsed_ms: int
    error: Optional[str] = None

@dataclass
class ScreeningResult:
    cycle_id: str
    shortlist: List[str]
    endorsement_counts: Dict[str, int]
    composite_scores: Dict[str, float]
    legend_results: Dict[str, LegendResult]
    regime: str
    generated_at: str
    universe_size: int
    shortlist_size: int

def _get_backtest_table(tickers: List[str], regime: str, db_path: str) -> str:
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        placeholders = ",".join("?"*len(tickers))
        rows = conn.execute(f"""SELECT ticker, strategy, win_rate FROM historical_win_rates WHERE ticker IN ({placeholders}) AND regime=? AND direction='bullish' AND sample_count>=5 ORDER BY ticker, strategy""", tickers+[regime]).fetchall()
        conn.close()
        data: Dict[str, Dict] = {}
        for ticker, strategy, wr in rows:
            if ticker not in data: data[ticker] = {}
            data[ticker][strategy] = round(wr, 4)
        sorted_t = sorted(data.items(), key=lambda x: x[1].get("bull_put_spread",0), reverse=True)[:30]
        lines = ["TICKER | BULL_PUT | SWING_5D | INTRADAY", "-------|---------|---------|--------"]
        for ticker, rates in sorted_t:
            bp = rates.get("bull_put_spread",0); s5 = rates.get("swing_5day",0); id2 = rates.get("intraday_2day",0)
            lines.append(f"{ticker:<6} | {bp:.2f}    | {s5:.2f}    | {id2:.2f}")
        return "\n".join(lines)
    except Exception:
        return "Backtest data unavailable."

def _build_prompt(legend: str, brief: Dict, backtest_table: str, universe: List[str]) -> str:
    roles = {
        "Druckenmiller": "macro regime alignment, sector rotation, liquidity flow. Score highest when macro tailwind + high historical win rate.",
        "Jones": "R:R potential, technical structure, entry quality. VIX 14-22 = IDEAL for credit spreads. Score high when structure is clean and win rate >= 0.95.",
        "Soros": "reflexivity, narrative momentum, inflection points. Score high when narrative is building and volume confirms.",
        "Cohen": "institutional flow, unusual volume (2x+), smart money signals. Score high on accumulation + strong backtest.",
        "Buffett": "fundamental quality, earnings stability, moat. Score high for quality names at value with strong backtest support.",
    }
    return f"""You are {legend}'s screening engine in the NEXUS trading system.
Your domain: {roles[legend]}

Market intelligence:
{json.dumps(brief, indent=2)}

Historical win rates (current regime: {brief['vix']['regime']}):
{backtest_table}

Universe ({len(universe)} stocks): {', '.join(universe)}

Score each stock 0-100. Stocks with score >= 60 will be endorsed for trading.
80-100: strong signal | 60-79: acceptable | 40-59: neutral | 0-39: avoid

Respond ONLY with valid JSON:
{{"legend":"{legend}","regime_read":"<1 sentence>","scores":{{"TICKER":{{"score":<0-100>,"rationale":"<1 sentence>"}}}}}}
Include ALL {len(universe)} tickers."""

class LegendScreener:
    def __init__(self, backtest_db_path=None, anthropic_api_key=None):
        self._backtest_db = backtest_db_path or os.getenv("BACKTEST_DB_PATH","/Users/ahmedsadek/nexus/data/backtest.db")
        self._client = anthropic.AsyncAnthropic(api_key=anthropic_api_key or os.getenv("ANTHROPIC_API_KEY",""))

    async def screen(self, brief: Dict, universe: List[str]) -> ScreeningResult:
        import asyncio
        regime = brief.get("vix",{}).get("regime","NORMAL")
        cycle_id = brief.get("cycle_id","unknown")
        backtest_table = _get_backtest_table(universe, regime, self._backtest_db)
        legends = ["Druckenmiller","Jones","Soros","Cohen","Buffett"]
        tasks = [self._run_legend(name, brief, backtest_table, universe) for name in legends]
        results_list = await asyncio.gather(*tasks, return_exceptions=True)
        results = {}
        for name, result in zip(legends, results_list):
            if isinstance(result, Exception):
                results[name] = LegendResult(legend_name=name, scores={}, top_picks=[], cycle_id=cycle_id, elapsed_ms=0, error=str(result))
            else:
                results[name] = result
        shortlist, endorsement_counts, composite_scores = self._aggregate(results, universe)
        return ScreeningResult(cycle_id=cycle_id, shortlist=shortlist, endorsement_counts=endorsement_counts, composite_scores=composite_scores, legend_results=results, regime=regime, generated_at=datetime.now(timezone.utc).isoformat(), universe_size=len(universe), shortlist_size=len(shortlist))

    async def _run_legend(self, name: str, brief: Dict, backtest_table: str, universe: List[str]) -> LegendResult:
        start = time.monotonic()
        cycle_id = brief.get("cycle_id","unknown")
        prompt = _build_prompt(name, brief, backtest_table, universe)
        response = await self._client.messages.create(model=LEGEND_MODEL, max_tokens=4096, messages=[{"role":"user","content":prompt}])
        raw = response.content[0].text
        elapsed_ms = int((time.monotonic()-start)*1000)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                start_i = raw.index("{"); end_i = raw.rindex("}")+1
                data = json.loads(raw[start_i:end_i])
            except Exception:
                return LegendResult(legend_name=name, scores={}, top_picks=[], cycle_id=cycle_id, elapsed_ms=elapsed_ms, error="Parse error")
        scores_raw = data.get("scores",{})
        scores = {}
        for ticker, info in scores_raw.items():
            if isinstance(info, dict): scores[ticker] = max(0,min(100,int(info.get("score",0))))
            elif isinstance(info,(int,float)): scores[ticker] = max(0,min(100,int(info)))
        top_picks = [t for t,s in scores.items() if s >= ENDORSEMENT_THRESHOLD]
        logger.info("Legend %s: %d endorsements in %dms", name, len(top_picks), elapsed_ms)
        return LegendResult(legend_name=name, scores=scores, top_picks=top_picks, cycle_id=cycle_id, elapsed_ms=elapsed_ms)

    def _aggregate(self, results, universe):
        endorsement_counts = {t:0 for t in universe}
        score_totals: Dict[str, List[int]] = {t:[] for t in universe}
        for lr in results.values():
            if lr.error: continue
            for ticker in lr.top_picks:
                if ticker in endorsement_counts: endorsement_counts[ticker] += 1
            for ticker, score in lr.scores.items():
                if ticker in score_totals: score_totals[ticker].append(score)
        composite_scores = {t: round(sum(s)/len(s),1) for t,s in score_totals.items() if s}
        shortlist = [t for t,c in endorsement_counts.items() if c >= MIN_ENDORSEMENTS]
        shortlist.sort(key=lambda t: composite_scores.get(t,0), reverse=True)
        return shortlist, endorsement_counts, composite_scores
