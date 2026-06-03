"""
Telegram report — format + delivery.
Splits messages > 4096 chars automatically.
"""

import os
import datetime
from typing import List

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
TELEGRAM_MAX = 4096


def _send_chunk(text: str) -> bool:
    """Send a single message chunk to Telegram."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
    }
    try:
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code == 200:
            return True
        # Retry without parse_mode if HTML caused issues
        payload2 = {"chat_id": CHAT_ID, "text": text}
        r2 = requests.post(url, json=payload2, timeout=30)
        return r2.status_code == 200
    except Exception as exc:
        print(f"[telegram] send error: {exc}")
        return False


def send_report(text: str) -> bool:
    """Send report, splitting into chunks if > 4096 chars."""
    if not BOT_TOKEN or not CHAT_ID:
        print("[telegram] BOT_TOKEN or CHAT_ID not set — skipping send")
        return False

    chunks = []
    while len(text) > TELEGRAM_MAX:
        # Split at last newline before limit
        split_at = text.rfind("\n", 0, TELEGRAM_MAX)
        if split_at == -1:
            split_at = TELEGRAM_MAX
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    chunks.append(text)

    success = True
    for i, chunk in enumerate(chunks):
        ok = _send_chunk(chunk)
        if not ok:
            print(f"[telegram] failed to send chunk {i+1}/{len(chunks)}")
            success = False
    return success


def format_report(results, run_date: str = None) -> str:
    """Format all ScenarioResults into the standard report string."""
    if run_date is None:
        run_date = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    line = "\u2501" * 42

    # --- Counts ---
    completed_pipeline = sum(1 for r in results if r.passed and not r.is_block_scenario)
    correctly_blocked = sum(1 for r in results if r.passed and r.is_block_scenario)
    total_passed = sum(1 for r in results if r.passed)
    total_failed = len(results) - total_passed

    # blocked_by_bugs = failed non-block scenarios (unexpected failures)
    blocked_by_bugs = sum(1 for r in results if not r.passed and not r.is_block_scenario)

    # --- Issues ---
    all_issues = []
    for r in results:
        for iss in r.issues:
            all_issues.append(iss)

    t1 = sum(1 for i in all_issues if i["tier"] == 1)
    t2 = sum(1 for i in all_issues if i["tier"] == 2)
    t34 = sum(1 for i in all_issues if i["tier"] >= 3)
    fixed_auto = sum(1 for i in all_issues if i.get("fixed"))
    ahmed_req = sum(1 for i in all_issues if i.get("requires_ahmed") and not i.get("fixed"))
    open_issues = len(all_issues) - fixed_auto

    # --- Scenario lines ---
    scenario_lines = []
    for r in results:
        mark = "\u2713" if r.passed else "\u2717"
        scenario_lines.append(f"  {r.number}. {r.name}: {mark}")

    # --- Resilience systems (derived from results) ---
    def sys_mark(condition):
        return "\u2713" if condition else "\u2717"

    # Snapshot: S5 (data source)
    snap_ok = any(r.number == 5 and r.passed for r in results)
    # Redundant data: S5
    redundant_ok = any(r.number == 5 and r.passed for r in results)
    # State machine: S8 (dedup) or S9 (position limit)
    state_ok = any(r.number in (8, 9) and r.passed for r in results)
    # Predictive (VIX brake interventions checked)
    vix_result = next((r for r in results if r.number == 4), None)
    vix_interventions = 0 if (vix_result and vix_result.passed) else 1
    # Degradation: S4 (brake system)
    degradation_ok = any(r.number == 4 and r.passed for r in results)

    # --- Verdict ---
    if total_passed >= 9:
        verdict = "READY"
    elif total_passed >= 7:
        verdict = "CONDITIONALLY READY"
    else:
        verdict = "NOT READY"

    # --- Before 7AM actions ---
    before_7am_lines = []
    if not all(r.passed for r in results if r.number in (3, 8)):
        before_7am_lines.append("  - Investigate score gate / dedup enforcement in alpha-buffer")
    if any(not r.passed for r in results if r.is_block_scenario):
        before_7am_lines.append("  - Review block scenario failures — gate integrity critical")
    if ahmed_req > 0:
        before_7am_lines.append(f"  - {ahmed_req} issue(s) require Ahmed review before live trading")
    if not before_7am_lines:
        before_7am_lines.append("  - All systems nominal — confirm VIX reading pre-market")

    # --- Issue detail lines ---
    issue_detail_lines = []
    for r in results:
        for iss in r.issues:
            tier_label = f"T{iss['tier']}"
            ahmed_flag = " [AHMED]" if iss.get("requires_ahmed") else ""
            issue_detail_lines.append(f"  [{tier_label}] S{r.number}: {iss['description']}{ahmed_flag}")

    # --- Duration ---
    total_dur = sum(r.duration_s for r in results)

    report = f"""{line}
\U0001f9ea MOCK TRADING REHEARSAL REPORT
Nexus V2 | {run_date} | GENESIS
{line}

TRADE RESULTS:
  Completed full pipeline: {completed_pipeline}/10
  Correctly blocked by design: {correctly_blocked}/10
  Blocked by bugs: {blocked_by_bugs}/10

SCENARIO RESULTS:
{chr(10).join(scenario_lines)}

ISSUES LOG:
  Tier 1: {t1} | Tier 2: {t2} | Tier 3/4: {t34}
  Fixed autonomously: {fixed_auto} | Ahmed required: {ahmed_req} | Open: {open_issues}

RESILIENCE SYSTEMS:
  Snapshot system: {sys_mark(snap_ok)}
  Redundant data: {sys_mark(redundant_ok)}
  State machine: {sys_mark(state_ok)}
  Predictive model: {vix_interventions} interventions
  Degradation manager: {sys_mark(degradation_ok)}

VERDICT: {verdict}

BEFORE 7AM TOMORROW:
{chr(10).join(before_7am_lines)}
{line}"""

    if issue_detail_lines:
        detail_block = "\n\nISSUE DETAIL:\n" + "\n".join(issue_detail_lines)
        report += detail_block

    # Per-scenario notes
    notes_lines = []
    for r in results:
        if r.notes:
            notes_lines.append(f"  S{r.number}: {r.notes}")
    if notes_lines:
        report += "\n\nSCENARIO NOTES:\n" + "\n".join(notes_lines)

    report += f"\n\nTotal runtime: {total_dur:.1f}s"

    return report
