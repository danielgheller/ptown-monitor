#!/usr/bin/env python3
"""
Overhead Door garage-door status check for the Ptown house.

The OHD Anywhere app (which is what Daniel uses) is rebranded Genie Aladdin
Connect. As of Jan 2024 Genie cut off third-party direct API access, so we
route through Samsung SmartThings instead:

    OHD Anywhere app  →  SmartThings (paired via Works With) → SmartThings REST API

SmartThings reads the standard `doorControl` capability and exposes the
door's state as one of: open, closed, opening, closing, unknown.

Credentials in `.env`:
    SMARTTHINGS_TOKEN=<Personal Access Token, read-only scopes>
    SMARTTHINGS_DEVICE_ID=<UUID of the garage door device>   # optional; auto-discovered if absent

Usage:
    python3 garage.py              # pretty-printed status
    python3 garage.py --raw        # dump raw /devices/{id}/status JSON
    python3 garage.py --json       # emit normalized JSON for dashboard.py
    python3 garage.py --discover   # list all SmartThings devices (for setup)

No third-party dependencies — uses only the Python stdlib.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://api.smartthings.com/v1"
UA = "ptown-monitor/0.1 (+local)"

# Capabilities the garage-door bridge might expose. Standard SmartThings is
# `doorControl` (attribute `door`), but some integrations register
# `garageDoorControl` instead. Try both.
DOOR_CAPABILITIES = ("doorControl", "garageDoorControl")


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
    """Return the full device list for this account."""
    return _get(f"{API_BASE}/devices", token).get("items", [])


def device_has_door_capability(dev: dict) -> str | None:
    """Return the matching door-capability id if the device exposes one, else None."""
    for component in dev.get("components", []):
        for cap in component.get("capabilities", []):
            cid = cap.get("id")
            if cid in DOOR_CAPABILITIES:
                return cid
    return None


def find_door_devices(devices: list[dict]) -> list[dict]:
    """Filter to devices that expose a door-control capability."""
    return [d for d in devices if device_has_door_capability(d) is not None]


def get_device_status(token: str, device_id: str) -> dict:
    """Fetch the device's full status dict."""
    return _get(f"{API_BASE}/devices/{device_id}/status", token)


def get_device_health(token: str, device_id: str) -> dict:
    """Fetch the device's health (online/offline). Tolerant of 404 on legacy devices."""
    try:
        return _get(f"{API_BASE}/devices/{device_id}/health", token)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        raise


# ---------- status extraction ----------
def extract_door_state(status: dict) -> tuple[str | None, str | None, str | None]:
    """Pull (door_state, battery_level, last_changed_iso) from a /devices/{id}/status payload.

    SmartThings shape:
      {"components": {"main": {"doorControl": {"door": {"value": "closed", "timestamp": "..."}}}}}
    """
    components = status.get("components", {}) or {}
    for comp_name, comp in components.items():
        for cap_id in DOOR_CAPABILITIES:
            cap = comp.get(cap_id)
            if not cap:
                continue
            door = cap.get("door") or {}
            value = door.get("value")
            timestamp = door.get("timestamp")

            # battery is reported on the `battery` capability if present
            battery = None
            bat_cap = comp.get("battery") or {}
            bat_attr = bat_cap.get("battery") or {}
            if isinstance(bat_attr.get("value"), (int, float)):
                battery = bat_attr["value"]

            return value, battery, timestamp
    return None, None, None


def is_online(health: dict) -> bool:
    """Translate the /health response into a boolean. Default to True if unknown."""
    state = (health.get("state") or "").upper()
    if not state:
        return True
    return state == "ONLINE"


# ---------- formatting ----------
def to_normalized_device(dev: dict, door_state: str | None, battery, online: bool,
                         last_changed: str | None) -> dict:
    """Return the normalized shape consumed by dashboard.py.

    Garage doors don't have temperatures, so current_f / setpoint_f are None
    and the door state lives in `mode` + `extra.door_state`. dashboard.py's
    garage evaluator and renderer both key off `mode` / `extra.door_state`.
    """
    extra: dict = {"door_state": door_state}
    if battery is not None:
        extra["battery_level"] = battery
    if last_changed:
        extra["last_changed"] = last_changed
    return {
        "name": dev.get("label") or dev.get("name") or "(unnamed garage)",
        "current_f": None,
        "setpoint_f": None,
        "mode": door_state or "unknown",
        "online": online,
        "extra": extra,
    }


def describe(name: str, door_state: str | None, online: bool, battery,
             last_changed: str | None) -> str:
    state_str = (door_state or "unknown").upper()
    online_str = "online" if online else "OFFLINE"
    bits = [f"  {name:<28} {state_str:<8} {online_str}"]
    if battery is not None:
        bits.append(f"battery {battery}%")
    if last_changed:
        bits.append(f"since {last_changed}")
    return "  ".join(bits)


# ---------- discovery helpers ----------
def _discover_print(devices: list[dict]) -> None:
    """Print a human-readable list of all devices, highlighting door-capable ones."""
    if not devices:
        print("(no devices visible to this token)")
        return
    print(f"{len(devices)} device(s) visible to this token:\n")
    for d in devices:
        cap = device_has_door_capability(d)
        marker = "  [DOOR] " if cap else "          "
        label = d.get("label") or d.get("name") or "(unnamed)"
        did = d.get("deviceId", "?")
        room = d.get("roomId", "")
        line = f"{marker}{label}   ({did})"
        if cap:
            line += f"   cap={cap}"
        print(line)
    print()
    print("Set SMARTTHINGS_DEVICE_ID=<deviceId> in .env to lock garage.py to one device.")


# ---------- entry point ----------
def _emit_json_error(msg: str) -> None:
    print(json.dumps({"system": "garage", "devices": [], "error": msg}))


def main() -> int:
    parser = argparse.ArgumentParser(description="Overhead Door garage status (via SmartThings)")
    parser.add_argument("--raw", action="store_true",
                        help="dump raw /devices/{id}/status JSON and exit")
    parser.add_argument("--json", dest="emit_json", action="store_true",
                        help="emit normalized JSON (consumed by dashboard.py)")
    parser.add_argument("--discover", action="store_true",
                        help="list all SmartThings devices (for first-time setup)")
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    load_env(here / ".env")

    token = (os.environ.get("SMARTTHINGS_TOKEN") or "").strip()
    if not token:
        msg = "Missing SMARTTHINGS_TOKEN"
        if args.emit_json:
            _emit_json_error(msg)
            return 2
        print(f"{msg}. Fill it into:", file=sys.stderr)
        print(f"  {here / '.env'}", file=sys.stderr)
        return 2

    device_id = (os.environ.get("SMARTTHINGS_DEVICE_ID") or "").strip()

    # --- discovery branch ---
    if args.discover:
        try:
            devices = list_devices(token)
        except Exception as e:
            print(f"Device list failed: {e}", file=sys.stderr)
            return 1
        _discover_print(devices)
        return 0

    # --- normal path: need a device_id, auto-discover if absent ---
    if not device_id:
        try:
            devices = list_devices(token)
        except Exception as e:
            if args.emit_json:
                _emit_json_error(f"Device list failed: {e}")
                return 1
            print(f"Device list failed: {e}", file=sys.stderr)
            return 1
        door_devices = find_door_devices(devices)
        if not door_devices:
            msg = "No door-capable devices visible. Re-check the OHD↔SmartThings pairing."
            if args.emit_json:
                _emit_json_error(msg)
                return 1
            print(msg, file=sys.stderr)
            print("  Try: ./ptown garage --discover", file=sys.stderr)
            return 1
        if len(door_devices) > 1:
            names = ", ".join(
                f"{d.get('label') or d.get('name')} ({d.get('deviceId')})"
                for d in door_devices
            )
            msg = (f"Multiple door-capable devices found: {names}. "
                   "Set SMARTTHINGS_DEVICE_ID in .env to pick one.")
            if args.emit_json:
                _emit_json_error(msg)
                return 1
            print(msg, file=sys.stderr)
            return 1
        device = door_devices[0]
        device_id = device["deviceId"]
        if not args.emit_json:
            label = device.get("label") or device.get("name")
            print(f"(auto-discovered: {label} = {device_id})", file=sys.stderr)
            print("  Pin this by adding to .env:", file=sys.stderr)
            print(f"    SMARTTHINGS_DEVICE_ID={device_id}", file=sys.stderr)
    else:
        # Even with device_id pinned we still need device metadata for the label.
        try:
            device = _get(f"{API_BASE}/devices/{device_id}", token)
        except Exception as e:
            if args.emit_json:
                _emit_json_error(f"Device lookup failed: {e}")
                return 1
            print(f"Device lookup failed: {e}", file=sys.stderr)
            return 1

    # --- fetch status + health in sequence ---
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

    door_state, battery, last_changed = extract_door_state(status)
    try:
        health = get_device_health(token, device_id)
    except Exception:
        health = {}
    online = is_online(health)

    if args.emit_json:
        normalized = to_normalized_device(device, door_state, battery, online, last_changed)
        print(json.dumps({"system": "garage", "devices": [normalized], "error": None}))
        return 0

    name = device.get("label") or device.get("name") or "(unnamed garage)"
    print(f"Overhead Door garage — 1 device:")
    print(describe(name, door_state, online, battery, last_changed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
