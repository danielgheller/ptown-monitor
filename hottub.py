#!/usr/bin/env python3
"""
Jacuzzi SmartTub status check for the Ptown house.

NOTE ON THE FILENAME: this file is called `hottub.py` rather than
`smarttub.py` because the installed library is named `smarttub` — a local
file named `smarttub.py` shadows the package and breaks `from smarttub...`
imports. Leaving it as `hottub.py` keeps things simple.

Uses the `python-smarttub` community library (installed via requirements.txt
when you run through the `./ptown` wrapper). Reads credentials from `.env`:

    SMARTTUB_EMAIL=you@example.com
    SMARTTUB_PASSWORD=your-smarttub-password

Usage (via wrapper):
    ./ptown hottub           # pretty-printed status
    ./ptown hottub --raw     # dump raw Spa + Status objects for debugging
    ./ptown hottub --json    # emit normalized JSON for dashboard consumption
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import aiohttp
# Import SmartTub from its submodule to be version-independent — some
# versions of python-smarttub don't re-export it at the package top level.
from smarttub.api import SmartTub

_HERE = Path(__file__).resolve().parent


# ---------- tiny .env loader (duplicated from nuheat.py on purpose) ----------
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


# ---------- helpers ----------
def _attr(obj, *names, default=None):
    """Grab the first attribute that exists on obj (names tried in order)."""
    for n in names:
        if hasattr(obj, n):
            val = getattr(obj, n)
            if val is not None:
                return val
    return default


def _c_to_f(celsius) -> float | None:
    """Convert °C to °F, or pass through None."""
    if celsius is None:
        return None
    return celsius * 9 / 5 + 32


def _fmt_f(fahrenheit) -> str:
    """Format a °F temperature for display."""
    if fahrenheit is None:
        return "   ?°F"
    return f"{fahrenheit:5.1f}°F"


def _water_temp_c(status) -> float | None:
    """Dig out the current water temperature (°C) from the Status object."""
    water = getattr(status, "water", None)
    if water is not None:
        t = getattr(water, "temperature", None)
        if t is not None:
            return t
    # Fallback to the properties dict (older library versions)
    props = getattr(status, "properties", None) or {}
    water = props.get("water") if isinstance(props, dict) else None
    if isinstance(water, dict):
        return water.get("temperature")
    return None


async def _gather_spa_info(spa) -> dict:
    """Return the normalized per-device shape used by dashboard.py."""
    name = _attr(spa, "name", default=spa.id)
    status = await spa.get_status()

    water_f = _c_to_f(_water_temp_c(status))
    set_f = _c_to_f(_attr(status, "set_temperature", "target_temperature", "setpoint"))
    heater = _attr(status, "heater", default=None)
    state = _attr(status, "state", default=None)
    watercare = _attr(status, "watercare", default=None)
    online = _attr(status, "online", default=True)

    err_obj = _attr(status, "error")
    err_code = _attr(status, "error_code", default=0) or 0
    err_title = err_obj.get("title") if isinstance(err_obj, dict) else None

    mode_bits = []
    if heater:
        mode_bits.append(f"heater {str(heater).lower()}")
    if watercare:
        mode_bits.append(f"{str(watercare).lower()}")
    mode = ", ".join(mode_bits) if mode_bits else (str(state).lower() if state else "")

    return {
        "name": name,
        "current_f": round(water_f, 1) if water_f is not None else None,
        "setpoint_f": round(set_f, 1) if set_f is not None else None,
        "mode": mode,
        "online": bool(online),
        "extra": {
            "heater": str(heater).lower() if heater else None,
            "state": str(state).lower() if state else None,
            "watercare": str(watercare).lower() if watercare else None,
            "error_code": int(err_code) if err_code else 0,
            "error_title": err_title,
        },
    }


# ---------- main ----------
async def _async_main(raw: bool, emit_json: bool) -> int:
    _load_env(_HERE / ".env")

    email = (os.environ.get("SMARTTUB_EMAIL") or "").strip()
    password = (os.environ.get("SMARTTUB_PASSWORD") or "").strip()
    if not email or not password:
        msg = "Missing SMARTTUB_EMAIL / SMARTTUB_PASSWORD"
        if emit_json:
            print(json.dumps({"system": "hottub", "devices": [], "error": msg}))
            return 2
        print(f"{msg}. Fill them into:", file=sys.stderr)
        print(f"  {_HERE / '.env'}", file=sys.stderr)
        return 2

    async with aiohttp.ClientSession() as session:
        st = SmartTub(session)
        try:
            await st.login(email, password)
        except Exception as e:
            if emit_json:
                print(json.dumps({"system": "hottub", "devices": [],
                                  "error": f"SmartTub login failed: {e}"}))
                return 1
            print(f"SmartTub login failed: {e}", file=sys.stderr)
            return 1

        account = await st.get_account()
        spas = await account.get_spas()

        if not spas:
            if emit_json:
                print(json.dumps({"system": "hottub", "devices": [], "error": None}))
            else:
                print("No spas found on this SmartTub account.")
            return 0

        if raw:
            import pprint
            for i, spa in enumerate(spas):
                print(f"=== Spa {i}: {getattr(spa, 'name', spa.id)} ===")
                status = await spa.get_status()
                pprint.pprint(vars(spa) if hasattr(spa, "__dict__") else spa)
                print("--- status ---")
                pprint.pprint(vars(status) if hasattr(status, "__dict__") else status)
                print()
            return 0

        if emit_json:
            devices = [await _gather_spa_info(spa) for spa in spas]
            print(json.dumps({"system": "hottub", "devices": devices, "error": None}))
            return 0

        print(f"Jacuzzi SmartTub — {len(spas)} spa{'s' if len(spas) != 1 else ''}:")
        for spa in spas:
            name = _attr(spa, "name", default=spa.id)
            status = await spa.get_status()

            water_f = _c_to_f(_water_temp_c(status))
            set_f = _c_to_f(_attr(status, "set_temperature", "target_temperature", "setpoint"))
            heater = _attr(status, "heater", default="?")
            state = _attr(status, "state", default="?")
            watercare = _attr(status, "watercare")
            online = _attr(status, "online", default=True)

            err_obj = _attr(status, "error")
            err_code = _attr(status, "error_code", default=0)
            err_title = None
            if isinstance(err_obj, dict):
                err_title = err_obj.get("title")

            bits = [
                f"water {_fmt_f(water_f)}",
                f"set {_fmt_f(set_f)}",
                f"heater:{str(heater).lower()}",
                f"state:{str(state).lower()}",
            ]
            if watercare:
                bits.append(f"care:{str(watercare).lower()}")
            if err_code and err_code != 0:
                bits.append(f"ERR {err_code} {err_title or ''}".strip())
            bits.append("online" if online else "OFFLINE")

            print(f"  {name:<22}  " + "   ".join(bits))

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="SmartTub Ptown status check")
    parser.add_argument(
        "--raw", action="store_true",
        help="dump raw Spa + Status objects for debugging",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="emit normalized JSON (consumed by dashboard.py)",
    )
    args = parser.parse_args()
    return asyncio.run(_async_main(raw=args.raw, emit_json=args.json))


if __name__ == "__main__":
    sys.exit(main())
