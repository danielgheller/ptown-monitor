#!/usr/bin/env python3
"""
Nuheat heated-floor status check for the Ptown house.

Reads MyNuheat credentials from `.env` in the same folder:
    NUHEAT_EMAIL=you@example.com
    NUHEAT_PASSWORD=your-password

Usage:
    python3 nuheat.py            # pretty-printed status
    python3 nuheat.py --raw      # dump raw /thermostats JSON (for debugging)
    python3 nuheat.py --json     # emit normalized JSON for dashboard consumption

No third-party dependencies — uses only the Python stdlib.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://www.mynuheat.com/api"
UA = "ptown-monitor/0.1 (+local)"


# ---------- tiny .env loader (no dotenv dependency) ----------
def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v)


# ---------- thin HTTP helpers ----------
def _post(url: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": UA,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _get(url: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


# ---------- Nuheat API ----------
def authenticate(email: str, password: str) -> str:
    """Log into MyNuheat and return a session id."""
    resp = _post(
        f"{API_BASE}/authenticate/user",
        {"Email": email, "Password": password, "Application": "0"},
    )
    err = resp.get("ErrorCode", 0)
    if err not in (0, None):
        raise SystemExit(f"Nuheat login failed (ErrorCode={err}): {resp}")
    sid = resp.get("SessionId")
    if not sid:
        raise SystemExit(f"Nuheat login returned no SessionId: {resp}")
    return sid


def list_thermostats(sid: str) -> list[dict]:
    """Return a flat list of thermostat dicts from the /thermostats endpoint."""
    resp = _get(f"{API_BASE}/thermostats?sessionid={urllib.parse.quote(sid)}")
    stats: list[dict] = []
    for group in resp.get("Groups", []):
        stats.extend(group.get("Thermostats", []))
    # fallback shape some accounts return
    stats.extend(resp.get("Thermostats", []))
    return stats


def fetch_thermostat(sid: str, serial: str) -> dict:
    """Fetch a single thermostat's detail (used when /thermostats is thin)."""
    url = (
        f"{API_BASE}/thermostat?sessionid={urllib.parse.quote(sid)}"
        f"&serialnumber={urllib.parse.quote(serial)}"
    )
    return _get(url)


# ---------- formatting ----------
def format_temp(hundredths_c) -> str:
    """
    Nuheat reports temperatures in hundredths of °C
    (e.g. 1810 -> 18.10°C -> 64.6°F). Convert to °F for display.

    Verified 2026-04-17: raw API returned values like 1810, 1450, 500, which
    only make sense as hundredths of °C given the house's known state.
    """
    if hundredths_c is None:
        return "   ? °F"
    celsius = hundredths_c / 100
    fahrenheit = celsius * 9 / 5 + 32
    return f"{fahrenheit:5.1f}°F"


def describe(t: dict) -> str:
    name = t.get("Room") or t.get("Name") or t.get("SerialNumber") or "(unnamed)"
    cur = t.get("Temperature")
    setp = t.get("SetPointTemp") or t.get("SetpointTemp")
    state = "heating" if t.get("Heating") else "idle"
    online = "online" if t.get("Online", True) else "OFFLINE"
    return (
        f"  {name:<28} now {format_temp(cur)}   "
        f"set {format_temp(setp)}   {state:<8} {online}"
    )


def _hundredths_c_to_f(val) -> float | None:
    """Nuheat temps are hundredths of °C. Convert to °F, preserve None."""
    if val is None:
        return None
    return round((val / 100) * 9 / 5 + 32, 1)


def to_normalized_device(t: dict) -> dict:
    """Return the normalized shape consumed by dashboard.py."""
    return {
        "name": t.get("Room") or t.get("Name") or t.get("SerialNumber") or "(unnamed)",
        "current_f": _hundredths_c_to_f(t.get("Temperature")),
        "setpoint_f": _hundredths_c_to_f(
            t.get("SetPointTemp") or t.get("SetpointTemp")
        ),
        "mode": "heating" if t.get("Heating") else "idle",
        "online": bool(t.get("Online", True)),
        "extra": {},
    }


# ---------- entry point ----------
def main() -> int:
    parser = argparse.ArgumentParser(description="Nuheat Ptown status check")
    parser.add_argument(
        "--raw", action="store_true",
        help="dump raw /thermostats JSON response and exit",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="emit normalized JSON (consumed by dashboard.py)",
    )
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    load_env(here / ".env")

    email = (os.environ.get("NUHEAT_EMAIL") or "").strip()
    password = (os.environ.get("NUHEAT_PASSWORD") or "").strip()
    if not email or not password:
        msg = "Missing NUHEAT_EMAIL / NUHEAT_PASSWORD"
        if args.json:
            print(json.dumps({"system": "nuheat", "devices": [], "error": msg}))
            return 2
        print(f"{msg}. Fill them into:", file=sys.stderr)
        print(f"  {here / '.env'}", file=sys.stderr)
        return 2

    try:
        sid = authenticate(email, password)
    except Exception as e:
        if args.json:
            print(json.dumps({"system": "nuheat", "devices": [],
                              "error": f"Login failed: {e}"}))
            return 1
        print(f"Login failed: {e}", file=sys.stderr)
        return 1

    try:
        if args.raw:
            resp = _get(
                f"{API_BASE}/thermostats?sessionid={urllib.parse.quote(sid)}"
            )
            print(json.dumps(resp, indent=2))
            return 0

        thermostats = list_thermostats(sid)
        if not thermostats:
            if args.json:
                print(json.dumps({"system": "nuheat", "devices": [], "error": None}))
            else:
                print("No Nuheat thermostats found on this account.")
            return 0

        # If the list response is missing temperature data on some thermostats,
        # fetch the detail endpoint for those.
        for i, t in enumerate(thermostats):
            if t.get("Temperature") is None:
                serial = t.get("SerialNumber") or t.get("Serial")
                if serial:
                    thermostats[i] = fetch_thermostat(sid, serial)

        if args.json:
            print(json.dumps({
                "system": "nuheat",
                "devices": [to_normalized_device(t) for t in thermostats],
                "error": None,
            }))
            return 0

        print(
            f"Nuheat heated floors — {len(thermostats)} "
            f"thermostat{'s' if len(thermostats) != 1 else ''}:"
        )
        for t in thermostats:
            print(describe(t))
    except Exception as e:
        if args.json:
            print(json.dumps({"system": "nuheat", "devices": [],
                              "error": f"Fetch failed: {e}"}))
            return 1
        print(f"Fetch failed: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
