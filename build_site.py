#!/usr/bin/env python3
"""
Build the static data file for the GitHub Pages dashboard.

Reads a dashboard.py --json payload and writes docs/data.json, which the HTML
page (docs/index.html) fetches on load and renders from.

Two rolling history windows, both per-device:
  - history_hourly: last 24 hourly runs (for the "24h" sparkline)
  - history_daily:  last 7 Eastern calendar days (for the "7d" sparkline).
    One bucket per Eastern calendar date. Today's bucket is upserted on each
    run (so the current-day dot is live); older days are frozen at whatever
    their final pre-midnight-Eastern reading was.

Each reading records both setpoint (SET) and current temp (NOW), so the
frontend can draw one sparkline per value.

data.json shape:
    {
        "current":        { ...dashboard.py --json output... },
        "history_hourly": [
            {"timestamp": "...",
             "readings": {"nuheat:guest floor": {"set": 62.0, "now": 60.4}, ...}},
            ... (<=24)
        ],
        "history_daily":  [
            {"date": "2026-04-18", "timestamp": "...", "readings": {...}},
            ... (<=7)
        ],
        "generated_at":   "2026-04-18T..."
    }

Usage:
    ./ptown build                                   # runs dashboard.py internally
    dashboard.py --json | ./build_site.py --stdin   # reuses caller's JSON
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
DOCS = HERE / "docs"
DATA_FILE = DOCS / "data.json"
HISTORY_HOURLY_LIMIT = 24
HISTORY_DAILY_LIMIT = 7
DASHBOARD_TIMEOUT = 90
# Daniel's home timezone — daily buckets roll over at midnight here, not UTC.
# zoneinfo handles EDT/EST transitions automatically.
EASTERN = ZoneInfo("America/New_York")


def _fetch_dashboard() -> dict:
    result = subprocess.run(
        [sys.executable, str(HERE / "dashboard.py"), "--json"],
        capture_output=True, text=True, timeout=DASHBOARD_TIMEOUT,
    )
    if not result.stdout.strip():
        raise RuntimeError(f"dashboard.py produced no output; stderr={result.stderr[:500]}")
    return json.loads(result.stdout)


def _flatten_readings(dashboard: dict) -> dict:
    """Extract {'<system>:<device>' -> {'set': f, 'now': f}} pairs.

    We key by "system:device" to avoid collisions if two systems ever used
    the same device name (e.g. both Nuheat and Nest having "Kitchen"). Each
    value keeps BOTH setpoint and current temp so the frontend can plot
    either one — the SET sparkline and the NOW sparkline are drawn separately.
    """
    readings: dict = {}
    for sys_result in dashboard.get("systems", []):
        system_name = sys_result.get("system", "?")
        for dev in sys_result.get("devices", []) or []:
            key = f"{system_name}:{dev.get('name', '?')}"
            readings[key] = {
                "set": dev.get("setpoint_f"),
                "now": dev.get("current_f"),
            }
    return readings


def _is_new_schema(history: list) -> bool:
    """Check if a stored history entry uses the new {'set','now'} shape."""
    if not history:
        return True  # empty is fine — treat as new
    readings = history[0].get("readings", {})
    if not readings:
        return True
    sample = next(iter(readings.values()))
    return isinstance(sample, dict)


def _load_existing() -> dict:
    if not DATA_FILE.exists():
        return {}
    try:
        return json.loads(DATA_FILE.read_text())
    except Exception:
        # Corrupt file — start fresh rather than crashing. Worst case we lose
        # sparkline history; the current reading is unaffected.
        return {}


def main() -> int:
    parser = argparse.ArgumentParser(description="Build docs/data.json")
    parser.add_argument("--stdin", action="store_true",
                        help="read dashboard JSON from stdin instead of invoking dashboard.py")
    args = parser.parse_args()

    try:
        if args.stdin:
            dashboard = json.loads(sys.stdin.read())
        else:
            dashboard = _fetch_dashboard()
    except Exception as e:
        print(f"Could not obtain dashboard data: {e}", file=sys.stderr)
        return 1

    DOCS.mkdir(exist_ok=True)

    existing = _load_existing()
    hourly = existing.get("history_hourly", [])
    daily = existing.get("history_daily", [])

    # Schema migration: older builds stored readings as bare floats under a
    # single "history" key. Detect either legacy shape and reset — takes
    # ~24h to repopulate the hourly sparkline, ~7 days for daily.
    if existing.get("history") is not None:
        hourly, daily = [], []
    elif not _is_new_schema(hourly) or not _is_new_schema(daily):
        hourly, daily = [], []

    now_utc = dt.datetime.now(dt.timezone.utc)
    now_utc_iso = now_utc.strftime("%Y-%m-%dT%H:%M:%S%z")
    # Eastern calendar date drives the daily bucket. The first run that lands
    # AFTER midnight Eastern creates a new bucket; subsequent runs the same
    # day overwrite it, keeping "today" live.
    eastern_date = now_utc.astimezone(EASTERN).strftime("%Y-%m-%d")

    readings = _flatten_readings(dashboard)
    ts = dashboard.get("timestamp") or now_utc_iso

    # Append to hourly — rolling window, no dedupe.
    hourly.append({"timestamp": ts, "readings": readings})
    hourly = hourly[-HISTORY_HOURLY_LIMIT:]

    # Upsert daily: one bucket per Eastern calendar day. Today's bucket gets
    # overwritten on every run (latest wins); older days stay frozen.
    entry = {"date": eastern_date, "timestamp": ts, "readings": readings}
    if daily and daily[-1].get("date") == eastern_date:
        daily[-1] = entry
    else:
        daily.append(entry)
    daily = daily[-HISTORY_DAILY_LIMIT:]

    out = {
        "current": dashboard,
        "history_hourly": hourly,
        "history_daily": daily,
        "generated_at": now_utc_iso,
    }
    DATA_FILE.write_text(json.dumps(out, indent=2) + "\n")
    print(f"Wrote {DATA_FILE} (hourly={len(hourly)}, daily={len(daily)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
