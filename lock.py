#!/usr/bin/env python3
"""
Yale lock status check for the Ptown house.

The Yale lock is paired to SmartThings (Yale Access app → Works With →
SmartThings). We share the same OAuth-In SmartApp credentials with the
garage door and Caseta lights — see smartthings_oauth.py. The lock
exposes the SmartThings `lock` capability; attribute `lock` has values
locked / unlocked / unknown / not fully locked.

Credentials in `.env`:
    SMARTTHINGS_CLIENT_ID / SMARTTHINGS_CLIENT_SECRET / SMARTTHINGS_REFRESH_TOKEN
    SMARTTHINGS_LOCK_DEVICE_ID=<UUID>  # optional; auto-discovered if absent

Why this isn't yalexs: Yale forced all individual logins onto OAuth in late
2024 (their WAF returns 403 on the legacy /session endpoint). The
community yalexs library only implements password auth and has no OAuth
support. SmartThings sidesteps the whole mess — Yale's SmartThings
integration handles the OAuth dance under the hood and we just read the
device state via the SmartThings REST API.

Usage:
    python3 lock.py              # pretty-printed status
    python3 lock.py --raw        # dump raw /devices/{id}/status JSON
    python3 lock.py --json       # emit normalized JSON for dashboard.py
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import smartthings_oauth

API_BASE = "https://api.smartthings.com/v1"
UA = "ptown-monitor/0.1 (+local)"

LOCK_CAPABILITY = "lock"


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


# ---------- SmartThings API ----------
def list_devices(token: str) -> list[dict]:
    return _get(f"{API_BASE}/devices", token).get("items", [])


def device_has_lock_capability(dev: dict) -> bool:
    for component in dev.get("components", []):
        for cap in component.get("capabilities", []):
            if cap.get("id") == LOCK_CAPABILITY:
                return True
    return False


def find_lock_devices(devices: list[dict]) -> list[dict]:
    return [d for d in devices if device_has_lock_capability(d)]


def get_device_status(token: str, device_id: str) -> dict:
    return _get(f"{API_BASE}/devices/{device_id}/status", token)


def get_device_health(token: str, device_id: str) -> dict:
    try:
        return _get(f"{API_BASE}/devices/{device_id}/health", token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


# ---------- status extraction ----------
def extract_lock_state(status: dict) -> tuple[str | None, int | None, str | None]:
    """Pull (lock_state, battery_level_0_to_100, last_changed) from /status.

    SmartThings shape:
      {"components": {"main": {
          "lock": {"lock": {"value": "locked", "timestamp": "..."}},
          "battery": {"battery": {"value": 80}},
      }}}
    """
    components = status.get("components", {}) or {}
    main = components.get("main", {}) or {}
    lock_attr = (main.get(LOCK_CAPABILITY) or {}).get("lock") or {}
    value = lock_attr.get("value")
    timestamp = lock_attr.get("timestamp")

    battery = None
    bat_attr = (main.get("battery") or {}).get("battery") or {}
    if isinstance(bat_attr.get("value"), (int, float)):
        battery = int(bat_attr["value"])

    return value, battery, timestamp


def is_online(health: dict) -> bool:
    state = (health.get("state") or "").upper()
    if not state:
        return True
    return state == "ONLINE"


# ---------- normalize for dashboard ----------
def to_normalized_device(dev: dict, lock_state: str | None, battery,
                         online: bool, last_changed: str | None) -> dict:
    """Match the no-temperature shape consumed by dashboard.py."""
    # SmartThings reports "not fully locked" with a space; normalize to a
    # hyphenated form so it survives JSON/email rendering without quoting.
    mode = (lock_state or "unknown").lower().replace(" ", "_")
    extra: dict = {"lock_state": mode}
    if battery is not None:
        extra["battery_level"] = battery
    if last_changed:
        extra["last_changed"] = last_changed
    return {
        "name": dev.get("label") or dev.get("name") or "(unnamed lock)",
        "current_f": None,
        "setpoint_f": None,
        "mode": mode,
        "online": online,
        "extra": extra,
    }


# ---------- entry point ----------
def _emit_json_error(msg: str) -> None:
    print(json.dumps({"system": "lock", "devices": [], "error": msg}))


def describe(dev: dict) -> str:
    name = dev["name"]
    state = (dev.get("mode") or "unknown").upper()
    online = "online" if dev.get("online") else "OFFLINE"
    extra = dev.get("extra", {}) or {}
    bits = [f"  {name:<28} {state:<14} {online}"]
    if (bat := extra.get("battery_level")) is not None:
        bits.append(f"battery {bat}%")
    if lc := extra.get("last_changed"):
        bits.append(f"since {lc}")
    return "  ".join(bits)


def main() -> int:
    parser = argparse.ArgumentParser(description="Yale lock status (via SmartThings)")
    parser.add_argument("--raw", action="store_true",
                        help="dump raw /devices/{id}/status JSON")
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

    device_id = (os.environ.get("SMARTTHINGS_LOCK_DEVICE_ID") or "").strip()

    if not device_id:
        try:
            devices = list_devices(token)
        except Exception as e:
            if args.emit_json:
                _emit_json_error(f"Device list failed: {e}")
                return 1
            print(f"Device list failed: {e}", file=sys.stderr)
            return 1
        lock_devices = find_lock_devices(devices)
        if not lock_devices:
            msg = "No lock-capable devices visible. Re-check the Yale↔SmartThings pairing."
            if args.emit_json:
                _emit_json_error(msg)
                return 1
            print(msg, file=sys.stderr)
            return 1
        if len(lock_devices) > 1:
            names = ", ".join(
                f"{d.get('label') or d.get('name')} ({d.get('deviceId')})"
                for d in lock_devices
            )
            msg = (f"Multiple lock-capable devices: {names}. "
                   "Set SMARTTHINGS_LOCK_DEVICE_ID in .env to pick one.")
            if args.emit_json:
                _emit_json_error(msg)
                return 1
            print(msg, file=sys.stderr)
            return 1
        device = lock_devices[0]
        device_id = device["deviceId"]
        if not args.emit_json:
            label = device.get("label") or device.get("name")
            print(f"(auto-discovered: {label} = {device_id})", file=sys.stderr)
            print("  Pin this by adding to .env:", file=sys.stderr)
            print(f"    SMARTTHINGS_LOCK_DEVICE_ID={device_id}", file=sys.stderr)
    else:
        try:
            device = _get(f"{API_BASE}/devices/{device_id}", token)
        except Exception as e:
            if args.emit_json:
                _emit_json_error(f"Device lookup failed: {e}")
                return 1
            print(f"Device lookup failed: {e}", file=sys.stderr)
            return 1

    try:
        status = get_device_status(token, device_id)
    except Exception as e:
        if args.emit_json:
            _emit_json_error(f"Status fetch failed: {e}")
            return 1
        print(f"Status fetch failed: {e}", file=sys.stderr)
        return 1

    if args.raw:
        print(json.dumps(status, indent=2))
        return 0

    lock_state, battery, last_changed = extract_lock_state(status)
    try:
        health = get_device_health(token, device_id)
    except Exception:
        health = {}
    online = is_online(health)

    if args.emit_json:
        normalized = to_normalized_device(device, lock_state, battery, online, last_changed)
        print(json.dumps({"system": "lock", "devices": [normalized], "error": None}))
        return 0

    normalized = to_normalized_device(device, lock_state, battery, online, last_changed)
    print(f"Yale lock — 1 device:")
    print(describe(normalized))
    return 0


if __name__ == "__main__":
    sys.exit(main())
