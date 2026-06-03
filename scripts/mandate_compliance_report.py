#!/usr/bin/env python3
"""
MANDATE COMPLIANCE REPORT GENERATOR
Issued by: Ahmed Sadek — Apr 28, 2026
Generates 3x daily compliance reports for the Immediate Intervention Mandate.

Schedule: 8:00 AM / 12:00 PM / 4:30 PM ET (Mon-Fri)

Reads from CHRONICLE DB: mandate_compliance table
Posts to: NEXUS HEALTH & INTEGRITY GROUP + SOVEREIGN
"""

import sqlite3
import json
import datetime
import pytz
import sys
import os
import requests

# ── Config ──────────────────────────────────────────────────────────────────
CHRONICLE_DB   = "/Users/ahmedsadek/nexus/data/chronicle.db"
TG_BOT_TOKEN   = "8747601602:AAGTzRd3NJWq44Bvbzd5JvhtnO2edBUvjbc"
HEALTH_GROUP   = "-1003954790884"
AHMED_DM       = "8573754783"
ET             = pytz.timezone("America/New_York")

# Ideal response time thresholds (minutes)
IDEAL_RESPONSE = {
    "execution_error":    2,
    "service_down":       1,
    "pipeline_blocked":   5,
    "data_source":        3,
    "ghost_position":     5,
    "health_parse":      10,
    "credential":         5,
    "db_inconsistency":  10,
    "default":            5,
}

MAX_RESPONSE = {
    "execution_error":    5,
    "service_down":       3,
    "pipeline_blocked":  10,
    "data_source":        7,
    "ghost_position":    15,
    "health_parse":      30,
    "credential":        10,
    "db_inconsistency":  20,
    "default":           15,
}

def ensure_schema(conn):
    """Create mandate_compliance table if not exists."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS mandate_compliance (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id      TEXT UNIQUE NOT NULL,
            agent            TEXT NOT NULL,
            component        TEXT NOT NULL,
            failure_type     TEXT NOT NULL,
            detected_at      TEXT NOT NULL,
            intervention_at  TEXT,
            resolved_at      TEXT,
            ideal_response_min INTEGER,
            actual_response_min INTEGER,
            response_status  TEXT,
            fix_type         TEXT,
            outcome          TEXT,
            damage           TEXT,
            root_cause       TEXT,
            fix_applied      TEXT,
            recurring_risk   TEXT,
            reported_by      TEXT,
            created_at       TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def get_incidents(conn, window_hours=24, report_type="eod"):
    """Fetch incidents for the reporting window."""
    now_utc = datetime.datetime.utcnow()

    if report_type == "premarket":
        # Last 12 hours (overnight)
        since = (now_utc - datetime.timedelta(hours=12)).isoformat()
    elif report_type == "midday":
        # Since midnight ET today
        today_et = datetime.datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        since = today_et.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S")
    else:  # eod
        # Full trading day
        today_et = datetime.datetime.now(ET).replace(hour=9, minute=0, second=0, microsecond=0)
        since = today_et.astimezone(pytz.utc).strftime("%Y-%m-%dT%H:%M:%S")

    rows = conn.execute("""
        SELECT * FROM mandate_compliance
        WHERE detected_at >= ?
        ORDER BY detected_at ASC
    """, (since,)).fetchall()

    cols = [d[0] for d in conn.execute("SELECT * FROM mandate_compliance LIMIT 0").description]
    return [dict(zip(cols, r)) for r in rows]


def fmt_ts(ts_str):
    """Format UTC timestamp to ET display."""
    if not ts_str:
        return "—"
    try:
        dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=pytz.utc)
        return dt.astimezone(ET).strftime("%H:%M ET")
    except Exception:
        return ts_str[:16]


def response_lag_str(incident):
    mins = incident.get("actual_response_min")
    ideal = incident.get("ideal_response_min") or IDEAL_RESPONSE.get(incident.get("failure_type","default"), 5)
    if mins is None:
        return "⚠️ NO INTERVENTION RECORDED"
    lag_label = "✅ ON TIME" if mins <= ideal else f"🔴 DELAYED +{mins - ideal}m"
    return f"{mins}m [{lag_label} — ideal: {ideal}m]"


def outcome_emoji(outcome):
    mapping = {
        "RESOLVED":    "✅",
        "PARTIAL":     "⚠️",
        "ESCALATED":   "🔼",
        "UNRESOLVED":  "❌",
        "OPEN":        "🔄",
    }
    return mapping.get(outcome or "OPEN", "❓")


def fix_type_label(fix_type):
    mapping = {
        "ROOT_CAUSE":   "🟢 ROOT CAUSE",
        "SYMPTOMATIC":  "🟡 SYMPTOMATIC",
        "PARTIAL":      "🟠 PARTIAL",
        "UNKNOWN":      "⚫ UNKNOWN",
    }
    return mapping.get(fix_type or "UNKNOWN", fix_type or "—")


def build_report(incidents, report_type, now_et):
    time_label = now_et.strftime("%I:%M %p ET")
    date_label = now_et.strftime("%b %d, %Y")

    type_headers = {
        "premarket": "🌅 PRE-MARKET COMPLIANCE BRIEF",
        "midday":    "☀️ MID-DAY COMPLIANCE CHECK",
        "eod":       "🌆 END-OF-DAY COMPLIANCE REPORT",
    }
    header = type_headers.get(report_type, "📋 COMPLIANCE REPORT")

    if not incidents:
        return (
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ {header}\n"
            f"{time_label} | {date_label}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"FAILURES LOGGED: 0\n"
            f"All agents operating under mandate. No incidents.\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )

    # Compute summary stats
    total        = len(incidents)
    resolved_root = sum(1 for i in incidents if i.get("outcome") == "RESOLVED" and i.get("fix_type") == "ROOT_CAUSE")
    resolved_sym  = sum(1 for i in incidents if i.get("outcome") == "RESOLVED" and i.get("fix_type") == "SYMPTOMATIC")
    escalated     = sum(1 for i in incidents if i.get("outcome") == "ESCALATED")
    unresolved    = sum(1 for i in incidents if i.get("outcome") in ("UNRESOLVED", "OPEN", None))
    no_intervention = sum(1 for i in incidents if not i.get("intervention_at"))

    lags = [i["actual_response_min"] for i in incidents if i.get("actual_response_min") is not None]
    avg_lag = round(sum(lags) / len(lags), 1) if lags else None

    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🔍 {header}",
        f"{time_label} | {date_label}",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"FAILURES LOGGED: {total}",
        "",
    ]

    for inc in incidents:
        eid = inc.get("incident_id", "?")
        agent = inc.get("agent", "?")
        component = inc.get("component", "?")
        ftype = inc.get("failure_type", "?")
        detected = fmt_ts(inc.get("detected_at"))
        intervention = fmt_ts(inc.get("intervention_at")) if inc.get("intervention_at") else "🚨 NONE"
        resolved = fmt_ts(inc.get("resolved_at")) if inc.get("resolved_at") else "OPEN"
        lag = response_lag_str(inc)
        fix = fix_type_label(inc.get("fix_type"))
        outcome = f"{outcome_emoji(inc.get('outcome'))} {inc.get('outcome') or 'OPEN'}"
        damage = inc.get("damage") or "None recorded"
        root_cause = inc.get("root_cause") or "Not analyzed"
        fix_applied = inc.get("fix_applied") or "None"
        recurring = inc.get("recurring_risk") or "Unknown"

        lines += [
            f"─────────────────────────────────────",
            f"INCIDENT: {eid} | {agent} → {component}",
            f"Type:          {ftype}",
            f"Detected:      {detected}",
            f"Intervention:  {intervention}",
            f"Response lag:  {lag}",
            f"Resolved:      {resolved}",
            f"Fix type:      {fix}",
            f"Outcome:       {outcome}",
            f"Damage:        {damage}",
            f"Root cause:    {root_cause}",
            f"Fix applied:   {fix_applied}",
            f"Recurring risk:{recurring}",
        ]

    lines += [
        f"─────────────────────────────────────",
        f"SUMMARY",
        f"Total incidents:        {total}",
        f"Resolved (root cause):  {resolved_root}",
        f"Resolved (symptomatic): {resolved_sym}",
        f"Escalated:              {escalated}",
        f"Unresolved/Open:        {unresolved}",
        f"No intervention logged: {no_intervention}{'  ⚠️ MANDATE VIOLATION' if no_intervention else ''}",
        f"Avg response lag:       {avg_lag}m" if avg_lag else "Avg response lag:       N/A",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    return "\n".join(lines)


def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": ""}
    resp = requests.post(url, json=payload, timeout=10)
    return resp.ok, resp.status_code


def main():
    # Determine report type from arg or time
    now_et = datetime.datetime.now(ET)
    hour = now_et.hour

    if len(sys.argv) > 1:
        report_type = sys.argv[1]
    elif hour < 10:
        report_type = "premarket"
    elif hour < 14:
        report_type = "midday"
    else:
        report_type = "eod"

    print(f"[{now_et.strftime('%H:%M ET')}] Generating {report_type} compliance report...")

    conn = sqlite3.connect(CHRONICLE_DB)
    ensure_schema(conn)

    incidents = get_incidents(conn, report_type=report_type)
    report = build_report(incidents, report_type, now_et)

    print(report)
    print()

    # Post to Health & Integrity Group
    ok, code = send_telegram(TG_BOT_TOKEN, HEALTH_GROUP, report)
    print(f"Health group: {'✅' if ok else '❌'} (HTTP {code})")

    # Post to Ahmed DM if EOD or if there are violations
    no_intervention = sum(1 for i in incidents if not i.get("intervention_at"))
    if report_type == "eod" or no_intervention > 0:
        ok2, code2 = send_telegram(TG_BOT_TOKEN, AHMED_DM, report)
        print(f"Ahmed DM: {'✅' if ok2 else '❌'} (HTTP {code2})")

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
