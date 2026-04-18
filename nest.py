#!/usr/bin/env python3
"""
Google Nest thermostat status check for the Ptown house.

Uses the Smart Device Management (SDM) API. Reads credentials from `.env`:

    NEST_PROJECT_ID=...   # Device Access Project ID (UUID)
    NEST_CLIENT_ID=...    # OAuth 2.0 Client ID from Google Cloud
    NEST_CLIENT_SECRET=...
    NEST_REFRESH_TOKEN=...  # From the one-time auth code exchange

Usage:
    ./ptown nest            # pretty-printed status
    ./ptown nest --raw      # dump raw SDM device list JSON
    ./ptown nest --json     # emit normalized JSON for dashboard consumption

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

TOKEN_URL = "https://oauth2.googleapis.com/token"
SDM_BASE = "https://smartdevicemanagement.googleapis.com/v1"
UA = "ptown-monitor/0.1 (+local)"


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v)


def _http(url: str, *, data=None, headers=None, method=None) -> dict:
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Accept": "application/json", "User-Agent": UA, **(headers or {})},
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit(f"HTTP {e.code} from {url}:\n{body}")


def refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    resp = _http(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    if "access_token" not in resp:
        raise SystemExit(f"Token refresh failed: {resp}")
    return resp["access_token"]


def list_devices(project_id: str, access_token: str) -> list[dict]:
    url = f"{SDM_BASE}/enterprises/{urllib.parse.quote(project_id)}/devices"
    resp = _http(url, headers={"Authorization": f"Bearer {access_token}"})
    return resp.get("devices", [])


def _c_to_f(c):
    if c is None:
        return None
    return c * 9 / 5 + 32


def _fmt_f(f_val) -> str:
    if f_val is None:
        return "   ?°F"
    return f"{f_val:5.1f}°F"


def _short_name(device: dict) -> str:
    """Prefer the user-set custom name; else parse the structure/room from parentRelations."""
    traits = device.get("traits", {}) or {}
    info = traits.get("sdm.devices.traits.Info", {}) or {}
    if info.get("customName"):
        return info["customName"]
    # parentRelations includes the room name, e.g. "Living Room"
    for rel in device.get("parentRelations", []) or []:
        if rel.get("displayName"):
            return rel["displayName"]
    # Fall back to the last segment of the resource name
    name = device.get("name", "")
    return name.rsplit("/", 1)[-1] if name else "(unnamed)"


def to_normalized_device(device: dict) -> dict:
    """Return the normalized shape consumed by dashboard.py."""
    traits = device.get("traits", {}) or {}
    temp = traits.get("sdm.devices.traits.Temperature", {}) or {}
    humidity = traits.get("sdm.devices.traits.Humidity", {}) or {}
    hvac = traits.get("sdm.devices.traits.ThermostatHvac", {}) or {}
    mode = traits.get("sdm.devices.traits.ThermostatMode", {}) or {}
    setpoint = traits.get("sdm.devices.traits.ThermostatTemperatureSetpoint", {}) or {}
    eco = traits.get("sdm.devices.traits.ThermostatEco", {}) or {}
    connectivity = traits.get("sdm.devices.traits.Connectivity", {}) or {}

    current_f = _c_to_f(temp.get("ambientTemperatureCelsius"))

    if eco.get("mode") and eco["mode"] != "OFF":
        heat_f = _c_to_f(eco.get("heatCelsius"))
        cool_f = _c_to_f(eco.get("coolCelsius"))
        mode_label = f"Eco {eco['mode'].lower()}"
    else:
        heat_f = _c_to_f(setpoint.get("heatCelsius"))
        cool_f = _c_to_f(setpoint.get("coolCelsius"))
        mode_label = (mode.get("mode") or "").title() or "—"

    # Pick the "primary" setpoint — whichever applies for the mode.
    mode_val = (mode.get("mode") or "").upper()
    if mode_val == "COOL":
        primary_setpoint_f = cool_f
    else:  # HEAT, HEATCOOL, OFF, or Eco fallback
        primary_setpoint_f = heat_f

    hvac_status = (hvac.get("status") or "off").lower()
    online = connectivity.get("status") == "ONLINE" if connectivity else True

    return {
        "name": _short_name(device),
        "current_f": round(current_f, 1) if current_f is not None else None,
        "setpoint_f": round(primary_setpoint_f, 1) if primary_setpoint_f is not None else None,
        "mode": f"{mode_label}, HVAC {hvac_status}",
        "online": bool(online),
        "extra": {
            "humidity": humidity.get("ambientHumidityPercent"),
            "hvac_status": hvac_status,
            "thermostat_mode": (mode.get("mode") or "").lower(),
            "eco_mode": (eco.get("mode") or "").lower(),
            "heat_f": round(heat_f, 1) if heat_f is not None else None,
            "cool_f": round(cool_f, 1) if cool_f is not None else None,
        },
    }


def describe(device: dict) -> str:
    traits = device.get("traits", {}) or {}
    temp = traits.get("sdm.devices.traits.Temperature", {}) or {}
    humidity = traits.get("sdm.devices.traits.Humidity", {}) or {}
    hvac = traits.get("sdm.devices.traits.ThermostatHvac", {}) or {}
    mode = traits.get("sdm.devices.traits.ThermostatMode", {}) or {}
    setpoint = traits.get("sdm.devices.traits.ThermostatTemperatureSetpoint", {}) or {}
    eco = traits.get("sdm.devices.traits.ThermostatEco", {}) or {}
    connectivity = traits.get("sdm.devices.traits.Connectivity", {}) or {}

    current_f = _c_to_f(temp.get("ambientTemperatureCelsius"))
    humidity_pct = humidity.get("ambientHumidityPercent")

    # If Eco mode is active, the effective setpoints come from the Eco trait.
    if eco.get("mode") and eco["mode"] != "OFF":
        heat_f = _c_to_f(eco.get("heatCelsius"))
        cool_f = _c_to_f(eco.get("coolCelsius"))
        mode_label = f"eco:{eco['mode'].lower()}"
    else:
        heat_f = _c_to_f(setpoint.get("heatCelsius"))
        cool_f = _c_to_f(setpoint.get("coolCelsius"))
        mode_label = f"mode:{(mode.get('mode') or '?').lower()}"

    hvac_state = (hvac.get("status") or "?").lower()
    online = connectivity.get("status") == "ONLINE" if connectivity else True

    # Setpoint display: just heat for HEAT mode, just cool for COOL, both for HEATCOOL
    mode_val = (mode.get("mode") or "").upper()
    if mode_val == "HEAT" or (eco.get("mode") and eco["mode"] != "OFF" and heat_f is not None and cool_f is None):
        setpoint_str = f"set {_fmt_f(heat_f)}"
    elif mode_val == "COOL":
        setpoint_str = f"set {_fmt_f(cool_f)}"
    elif mode_val == "HEATCOOL":
        setpoint_str = f"set {_fmt_f(heat_f)}–{_fmt_f(cool_f)}"
    elif mode_val == "OFF":
        setpoint_str = "set ——"
    else:
        setpoint_str = f"set {_fmt_f(heat_f)}"

    name = _short_name(device)
    bits = [
        f"now {_fmt_f(current_f)}",
        setpoint_str,
        f"hvac:{hvac_state}",
        mode_label,
    ]
    if humidity_pct is not None:
        bits.append(f"rh:{humidity_pct}%")
    bits.append("online" if online else "OFFLINE")
    return f"  {name:<22}  " + "   ".join(bits)


def main() -> int:
    parser = argparse.ArgumentParser(description="Nest Ptown status check")
    parser.add_argument("--raw", action="store_true",
                        help="dump raw SDM device list and exit")
    parser.add_argument("--json", dest="emit_json", action="store_true",
                        help="emit normalized JSON (consumed by dashboard.py)")
    args = parser.parse_args()

    here = Path(__file__).resolve().parent
    _load_env(here / ".env")

    missing = [k for k in ("NEST_PROJECT_ID", "NEST_CLIENT_ID",
                           "NEST_CLIENT_SECRET", "NEST_REFRESH_TOKEN")
               if not (os.environ.get(k) or "").strip()]
    if missing:
        msg = f"Missing in .env: {', '.join(missing)}"
        if args.emit_json:
            print(json.dumps({"system": "nest", "devices": [], "error": msg}))
            return 2
        print(msg, file=sys.stderr)
        print(f"  {here / '.env'}", file=sys.stderr)
        return 2

    try:
        access_token = refresh_access_token(
            os.environ["NEST_CLIENT_ID"].strip(),
            os.environ["NEST_CLIENT_SECRET"].strip(),
            os.environ["NEST_REFRESH_TOKEN"].strip(),
        )
    except SystemExit:
        raise
    except Exception as e:
        if args.emit_json:
            print(json.dumps({"system": "nest", "devices": [],
                              "error": f"Access token refresh failed: {e}"}))
            return 1
        print(f"Access token refresh failed: {e}", file=sys.stderr)
        return 1

    try:
        devices = list_devices(os.environ["NEST_PROJECT_ID"].strip(), access_token)
    except Exception as e:
        if args.emit_json:
            print(json.dumps({"system": "nest", "devices": [],
                              "error": f"Device list failed: {e}"}))
            return 1
        print(f"Device list failed: {e}", file=sys.stderr)
        return 1

    # Filter to thermostats only (ignore cameras, doorbells, etc. if any)
    thermostats = [d for d in devices
                   if d.get("type") == "sdm.devices.types.THERMOSTAT"]

    if args.raw:
        print(json.dumps({"devices": devices, "thermostats": thermostats}, indent=2))
        return 0

    if args.emit_json:
        print(json.dumps({
            "system": "nest",
            "devices": [to_normalized_device(d) for d in thermostats],
            "error": None,
        }))
        return 0

    if not thermostats:
        print("No Nest thermostats found on this SDM project.")
        if devices:
            other_types = sorted({d.get("type", "?") for d in devices})
            print(f"(Project does have {len(devices)} non-thermostat device(s): "
                  f"{', '.join(other_types)})")
        return 0

    print(f"Google Nest — {len(thermostats)} thermostat"
          f"{'s' if len(thermostats) != 1 else ''}:")
    for d in thermostats:
        print(describe(d))
    return 0


if __name__ == "__main__":
    sys.exit(main())
