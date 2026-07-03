#!/usr/bin/env python3
"""
Samsung Frame TV status check + off control for the Ptown house.

Samsung TVs are SmartThings-native — once signed into the Samsung account
(TV → Settings → General → System Manager → Samsung Account, or SmartThings
app → Add device on the house WiFi), they appear as devices with the
`switch` capability. Same OAuth-In SmartApp as garage/lock/caseta.

Monitoring stance (Daniel, 2026-07-03): a TV on while AWAY is a WARN —
and **Art Mode counts as ON** (a Frame displaying art still burns power
and panel hours). When in Ptown, TVs on are fine.

Graceful degradation: if no TVs are linked to the account yet, this
reports OK with zero devices instead of nagging — the module lies dormant
until the TVs show up.

Frame quirk to watch (unverified until the TVs are linked): on some Frame
firmware, SmartThings `switch off` lands in Art Mode rather than standby.
If the tvs_off button "works" but the art stays on, that's why — revisit
with the device's real capability list (samsungvd.* commands).

Usage:
    python3 tv.py               # pretty-printed status
    python3 tv.py --raw         # dump raw /status JSON per TV
    python3 tv.py --json        # normalized JSON for dashboard.py
    python3 tv.py --discover    # list TV-ish devices on the account
    python3 tv.py --off         # turn every TV off

No third-party dependencies — reuses garage.py's SmartThings helpers.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import garage  # SmartThings HTTP helpers + .env loader
import smartthings_oauth

HERE = Path(__file__).resolve().parent

SWITCH_CAPABILITY = "switch"

# Signals that a switch-capable device is a TV rather than a light:
# tvChannel / mediaInputSource capabilities, any samsungvd.* vendor
# capability, or the OCF device type. Belt and suspenders because Samsung
# device profiles vary by model year.
TV_CAPABILITY_HINTS = ("tvChannel", "mediaInputSource", "audioVolume")
OCF_TV_TYPE = "oic.d.tv"


def _capabilities(dev: dict) -> set[str]:
    caps: set[str] = set()
    for component in dev.get("components", []):
        for cap in component.get("capabilities", []):
            if cap.get("id"):
                caps.add(cap["id"])
    return caps


def is_tv_device(dev: dict) -> bool:
    caps = _capabilities(dev)
    if any(c.startswith("samsungvd.") for c in caps):
        return True
    if any(hint in caps for hint in TV_CAPABILITY_HINTS) and SWITCH_CAPABILITY in caps:
        return True
    ocf_type = ((dev.get("ocf") or {}).get("ocfDeviceType") or "").lower()
    return ocf_type == OCF_TV_TYPE


def find_tv_devices(devices: list[dict]) -> list[dict]:
    return [d for d in devices if is_tv_device(d)]


def extract_tv_state(status: dict) -> tuple[str | None, str | None]:
    """Return (switch_value, art_mode_value) from /devices/{id}/status.

    Art-mode reporting varies by firmware (samsungvd.artMode,
    custom.artModeStatus, ...) — scan for any capability key containing
    'artmode' and take its first attribute value.
    """
    main = (status.get("components") or {}).get("main", {}) or {}
    sw = (main.get(SWITCH_CAPABILITY) or {}).get("switch", {}).get("value")
    art = None
    for cap_key, attrs in main.items():
        if "artmode" in cap_key.lower() and isinstance(attrs, dict):
            for attr in attrs.values():
                if isinstance(attr, dict) and attr.get("value") is not None:
                    art = str(attr["value"])
                    break
        if art is not None:
            break
    return sw, art


def to_normalized_device(dev: dict, sw: str | None, art: str | None,
                         online: bool) -> dict:
    """Normalized shape for dashboard.py. Art Mode counts as ON (Daniel's
    call) but we surface it in the mode string for the email."""
    mode = sw or "unknown"
    if sw == "on" and (art or "").lower() in ("on", "true", "artmode"):
        mode = "on (art mode)"
    extra: dict = {}
    if art is not None:
        extra["art_mode"] = art
    return {
        "name": dev.get("label") or dev.get("name") or "(unnamed TV)",
        "current_f": None,
        "setpoint_f": None,
        "mode": mode,
        "online": online,
        "extra": extra,
    }


def turn_off(token: str, device_id: str) -> dict:
    """Send switch.off. Needs x:devices:* scope."""
    payload = {"commands": [
        {"component": "main", "capability": SWITCH_CAPABILITY, "command": "off"}
    ]}
    return garage._post(f"{garage.API_BASE}/devices/{device_id}/commands",
                        token, payload)


def run_off(match: str | None = None) -> list[dict]:
    """Turn off every TV; control.py-shaped results."""
    garage.load_env(HERE / ".env")
    try:
        token = smartthings_oauth.get_access_token()
        tvs = find_tv_devices(garage.list_devices(token))
    except Exception as e:
        return [{"device": "tv:setup", "ok": False, "detail": str(e)}]
    if match:
        tvs = [d for d in tvs
               if match.lower() in (d.get("label") or d.get("name") or "").lower()]
    if not tvs:
        return [{"device": "tv:discover", "ok": False,
                 "detail": "no TVs found on the SmartThings account — are the "
                           "Frames signed into the Samsung account / added in "
                           "the SmartThings app?"}]
    out: list[dict] = []
    for d in tvs:
        name = d.get("label") or d.get("name") or d.get("deviceId", "?")
        try:
            turn_off(token, d["deviceId"])
            out.append({"device": f"tv:{name}", "ok": True, "detail": "switch → off"})
        except Exception as e:
            out.append({"device": f"tv:{name}", "ok": False, "detail": str(e)})
    return out


# ---------- entry point ----------
def _emit_json(devices: list[dict], error: str | None) -> None:
    print(json.dumps({"system": "tv", "devices": devices, "error": error}))


def main() -> int:
    parser = argparse.ArgumentParser(description="Samsung TV status (via SmartThings)")
    parser.add_argument("--raw", action="store_true")
    parser.add_argument("--json", dest="emit_json", action="store_true")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument("--off", action="store_true", help="turn every TV off")
    args = parser.parse_args()

    garage.load_env(HERE / ".env")
    try:
        token = smartthings_oauth.get_access_token()
    except Exception as e:
        msg = f"SmartThings auth failed: {e}"
        if args.emit_json:
            _emit_json([], msg)
            return 2
        print(msg, file=sys.stderr)
        return 2

    try:
        tvs = find_tv_devices(garage.list_devices(token))
    except Exception as e:
        if args.emit_json:
            _emit_json([], f"Device list failed: {e}")
            return 1
        print(f"Device list failed: {e}", file=sys.stderr)
        return 1

    if args.discover:
        print(f"{len(tvs)} TV device(s) on the SmartThings account:")
        for d in tvs:
            print(f"  {d.get('label') or d.get('name')}   ({d.get('deviceId')})")
        if not tvs:
            print("  — none. Sign the TVs into the Samsung account or add "
                  "them in the SmartThings app.")
        return 0

    if args.off:
        results = run_off()
        ok = True
        for r in results:
            mark = "✓" if r["ok"] else "✗"
            ok = ok and r["ok"]
            print(f"  {mark} {r['device']}: {r['detail']}")
        return 0 if ok else 1

    # --- status path. Zero TVs = quietly OK (module dormant until linked).
    normalized: list[dict] = []
    for d in tvs:
        try:
            status = garage.get_device_status(token, d["deviceId"])
        except Exception as e:
            if args.emit_json:
                continue  # skip broken device; better partial than CRIT
            print(f"Status fetch failed for {d.get('label')}: {e}", file=sys.stderr)
            continue
        if args.raw:
            print(json.dumps(status, indent=2))
            continue
        sw, art = extract_tv_state(status)
        try:
            health = garage.get_device_health(token, d["deviceId"])
        except Exception:
            health = {}
        normalized.append(to_normalized_device(d, sw, art,
                                               garage.is_online(health)))

    if args.raw:
        return 0
    if args.emit_json:
        _emit_json(normalized, None)
        return 0

    print(f"Samsung TVs — {len(normalized)} device(s):")
    if not normalized:
        print("  (none linked to SmartThings yet)")
    for dev in normalized:
        online = "online" if dev["online"] else "OFFLINE/standby"
        print(f"  {dev['name']:<28} {dev['mode'].upper():<16} {online}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
