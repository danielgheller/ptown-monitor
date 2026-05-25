#!/usr/bin/env python3
"""
Lutron Caseta lighting status check for the Ptown house.

The Caseta Smart Bridge is paired to SmartThings via the "Works With
SmartThings" integration; we share the same OAuth-In SmartApp credentials
with the garage door and lock — see smartthings_oauth.py. Each Caseta
dimmer/switch appears as a SmartThings device with the `switch`
capability (on/off) and, for dimmers, `switchLevel` (0-100 brightness).

Monitoring stance (Daniel, 2026-05-21): cost-protection. Every Caseta
device should be OFF when he's away from Ptown. Any light on while AWAY
trips a WARN with the offender list in the email.

Auto-discovers all `switch`-capable devices on the account each run; no
device-ID pinning needed (and it'd be tedious with 40+ devices). Fetches
each device's status in parallel via a ThreadPoolExecutor so the whole
sweep stays under ~2 seconds.

Usage:
    python3 caseta.py             # pretty-printed status
    python3 caseta.py --raw       # dump per-device raw /status JSON
    python3 caseta.py --json      # emit normalized JSON for dashboard.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import smartthings_oauth

API_BASE = "https://api.smartthings.com/v1"
UA = "ptown-monitor/0.1 (+local)"

# Capabilities that mark a device as a Caseta-style light/switch. Any device
# carrying `switch` is something we can ask on/off about. We exclude devices
# that ONLY have door-control (the garage), and we don't gate on `switchLevel`
# (it would exclude non-dimming switches like Caseta Pico-controlled ones).
SWITCH_CAPABILITY = "switch"
SWITCH_LEVEL_CAPABILITY = "switchLevel"
DOOR_CAPABILITIES = ("doorControl", "garageDoorControl")

# 8 parallel HTTPS calls is plenty for ~40 devices and well under SmartThings'
# unpublished rate limit (their docs suggest several-hundred reqs/min per token).
STATUS_FETCH_WORKERS = 8


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


# ---------- thin HTTP helper ----------
def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": UA,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


# ---------- SmartThings discovery + status ----------
def list_devices(token: str) -> list[dict]:
    return _get(f"{API_BASE}/devices", token).get("items", [])


def device_capabilities(dev: dict) -> set[str]:
    caps: set[str] = set()
    for component in dev.get("components", []):
        for cap in component.get("capabilities", []):
            cid = cap.get("id")
            if cid:
                caps.add(cid)
    return caps


def is_switch_device(dev: dict) -> bool:
    """Yes if it has `switch` and is NOT the garage door (which also exposes
    `switch` via the garageDoorControl module on some SmartThings drivers)."""
    caps = device_capabilities(dev)
    if SWITCH_CAPABILITY not in caps:
        return False
    if any(d in caps for d in DOOR_CAPABILITIES):
        return False
    return True


def extract_switch_state(status: dict) -> tuple[str | None, int | None]:
    """Return (switch_value, level) from a /devices/{id}/status payload.

    Shape:
      {"components": {"main": {
          "switch": {"switch": {"value": "on", "timestamp": "..."}},
          "switchLevel": {"level": {"value": 80}},
      }}}
    """
    components = status.get("components", {}) or {}
    main = components.get("main", {}) or {}
    sw_value = (main.get(SWITCH_CAPABILITY) or {}).get("switch", {}).get("value")
    level_raw = (main.get(SWITCH_LEVEL_CAPABILITY) or {}).get("level", {}).get("value")
    level = level_raw if isinstance(level_raw, (int, float)) else None
    return sw_value, level


def _fetch_one(dev: dict, token: str) -> dict:
    """Fetch + normalize a single device. Returns a dashboard-shaped dict.
    Tolerates per-device failures so one flaky bulb doesn't sink the run.
    """
    device_id = dev["deviceId"]
    name = dev.get("label") or dev.get("name") or "(unnamed)"
    try:
        status = _get(f"{API_BASE}/devices/{device_id}/status", token)
    except Exception as e:
        return {
            "name": name, "current_f": None, "setpoint_f": None,
            "mode": "unknown", "online": False,
            "extra": {"device_id": device_id, "error": str(e)[:120]},
        }
    sw, level = extract_switch_state(status)
    extra: dict = {"device_id": device_id}
    if level is not None:
        extra["level"] = int(level)
    return {
        "name": name,
        "current_f": None,
        "setpoint_f": None,
        "mode": (sw or "unknown"),
        "online": sw in ("on", "off"),  # if we got a real state, treat as online
        "extra": extra,
    }


def fetch_all_switches(token: str) -> list[dict]:
    devices = [d for d in list_devices(token) if is_switch_device(d)]
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=STATUS_FETCH_WORKERS) as ex:
        futures = {ex.submit(_fetch_one, d, token): d for d in devices}
        for fut in as_completed(futures):
            results.append(fut.result())
    # Stable ordering by name so the JSON output is reproducible run-to-run.
    results.sort(key=lambda r: r["name"].lower())
    return results


# ---------- entry point ----------
def _emit_json_error(msg: str) -> None:
    print(json.dumps({"system": "caseta", "devices": [], "error": msg}))


def main() -> int:
    parser = argparse.ArgumentParser(description="Lutron Caseta status (via SmartThings)")
    parser.add_argument("--raw", action="store_true",
                        help="dump per-device raw /status JSON")
    parser.add_argument("--json", dest="emit_json", action="store_true",
                        help="emit normalized JSON (consumed by dashboard.py)")
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    load_env(here / ".env")

    try:
        token = smartthings_oauth.get_access_token()
    except Exception as e:
        msg = f"SmartThings auth failed: {e}"
        if args.emit_json:
            _emit_json_error(msg)
            return 2
        print(msg, file=sys.stderr)
        print(f"  Check SMARTTHINGS_CLIENT_ID / SMARTTHINGS_CLIENT_SECRET / "
              f"SMARTTHINGS_REFRESH_TOKEN in {here / '.env'}", file=sys.stderr)
        return 2

    try:
        devices = fetch_all_switches(token)
    except Exception as e:
        if args.emit_json:
            _emit_json_error(f"Caseta fetch failed: {e}")
            return 1
        print(f"Caseta fetch failed: {e}", file=sys.stderr)
        return 1

    if args.raw:
        print(json.dumps(devices, indent=2))
        return 0

    if args.emit_json:
        print(json.dumps({"system": "caseta", "devices": devices, "error": None}))
        return 0

    on_devices = [d for d in devices if d["mode"] == "on"]
    print(f"Caseta — {len(devices)} switch(es), {len(on_devices)} on:")
    if on_devices:
        for d in on_devices:
            level = (d.get("extra") or {}).get("level")
            level_str = f"  ({level}%)" if level is not None else ""
            print(f"  ON  {d['name']}{level_str}")
    else:
        print("  All lights off.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
