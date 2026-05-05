#!/usr/bin/env python3
"""
Dress Rehearsal — Main Entrypoint
Runs all 10 mock trading scenarios and sends a Telegram report.
"""

import datetime
import sys
import traceback

from dotenv import load_dotenv
import os

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from scenarios import ALL_SCENARIOS, ScenarioResult
from telegram_report import format_report, send_report


def run_all_scenarios():
    results = []
    total = len(ALL_SCENARIOS)

    print(f"\n{'='*50}")
    print(f"  NEXUS DRESS REHEARSAL — {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*50}\n")

    for i, scenario_fn in enumerate(ALL_SCENARIOS, start=1):
        print(f"[{i:02d}/{total}] Running: {scenario_fn.__name__} ...", end=" ", flush=True)
        try:
            result = scenario_fn()
            results.append(result)
            status = "PASS" if result.passed else "FAIL"
            print(f"{status} ({result.duration_s:.1f}s)")
            if result.notes:
                print(f"         {result.notes}")
            if result.issues:
                for iss in result.issues:
                    print(f"         [T{iss['tier']}] {iss['description']}")
        except Exception as exc:
            print(f"ERROR — scenario raised unhandled exception")
            tb = traceback.format_exc()
            print(f"         {tb[:300]}")
            # Create a failed result so the report is complete
            failed = ScenarioResult(
                number=i,
                name=scenario_fn.__name__,
                passed=False,
                is_block_scenario=False,
                steps={},
                issues=[{"tier": 1, "description": f"Unhandled exception: {exc}", "requires_ahmed": True, "fixed": False}],
                notes=f"Exception: {exc}",
                duration_s=0.0,
            )
            results.append(failed)

    print(f"\n{'='*50}")
    passed = sum(1 for r in results if r.passed)
    print(f"  RESULTS: {passed}/{total} passed")
    print(f"{'='*50}\n")

    return results


def main():
    results = run_all_scenarios()

    run_date = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    report = format_report(results, run_date=run_date)

    print("\n" + "="*50)
    print("REPORT PREVIEW:")
    print("="*50)
    print(report)
    print("="*50 + "\n")

    print("Sending to Telegram...", end=" ", flush=True)
    ok = send_report(report)
    if ok:
        print("sent.")
    else:
        print("FAILED — check BOT_TOKEN and CHAT_ID.")
        sys.exit(1)


if __name__ == "__main__":
    main()
