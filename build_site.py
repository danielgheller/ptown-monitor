#!/usr/bin/env python3
"""
Build the static data file for the GitHub Pages dashboard.

Reads a dashboard.py --json payload, rolls it into a 24-hour history window,
and writes docs/data.json. The HTML page (docs/index.html) is a static shell
that fetches data.json on load and renders from it client-side.

Storage layout:
    docs/
        index.html       # static — hand-written, rarely changes
        .nojekyll        # empty — disables GitHub Pages' Jekyll processor
        data.json        # machine-generated — overwritten every hour

data.json shape:
    {
        "current":      { ...raw dashboard.py --json output... },
        "history":      [ {"timestamp": "...", "readings": {"nuheat:guest floor": 60.4, ...}}, ... ],
        "generated_at": "2026-04-18T..."
    }

History keeps the last 24 entries (~24 hours at one-per-hour). On first-ever
run the file doesn't exist yet; we create it with a one-entry history.

Usage:
    ./ptown build                          # runs dashboard.py internally
    dashboard.py --json | ./build_site.py --stdin    # reuses caller's JSON
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOCS = HERE / "docs"
DATA_FILE = DOCS / "data.json"
HISTORY_LIMIT = 24
DASHBOARD_TIMEOUT = 90


def _fetch_dashboard() -> dict:
    result = subprocess.run(
        [sys.executable, str(HERE / "dashboard.py"), "--json"],
        capture_output=True, text=True, timeout=DASHBOARD_TIMEOUT,
    )
    if not result.stdout.strip():
        raise RuntimeError(f"dashboard.py produced no output; stderr={result.stderr[:500]}")
    return json.loads(result.stdout)


def _flatten_readings(dashboard: dict) -> dict:
    """Extract {'<system>:<device>' -> current_f} pairs for sparkline plotting.

    We key by "system:device" to avoid collisions if two systems ever used the
    same device name (e.g. both Nuheat and Nest having "Kitchen").
    """
    readings = {}
    for sys_result in dashboard.get("systems", []):
        system_name = sys_result.get("system", "?")
        for dev in sys_result.get("devices", []) or []:
            key = f"{system_name}:{dev.get('name', '?')}"
            readings[key] = dev.get("current_f")
    return readings


def _load_existing() -> dict:
    if not DATA_FILE.exists():
        return {}
    try:
        return json.loads(DATA_FILE.read_text())
    except Exception:
        # Corrupt file — start fresh rather than crashing. Worst case we lose
        # ~24h of sparkline history; the current reading is unaffected.
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
    history = existing.get("history", [])
    history.append({
        "timestamp": dashboard.get("timestamp") or time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "readings": _flatten_readings(dashboard),
    })
    # Keep only the most recent HISTORY_LIMIT entries (rolling window).
    history = history[-HISTORY_LIMIT:]

    out = {
        "current": dashboard,
        "history": history,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    DATA_FILE.write_text(json.dumps(out, indent=2) + "\n")
    print(f"Wrote {DATA_FILE} ({len(history)} history entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
