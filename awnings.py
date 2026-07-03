#!/usr/bin/env python3
"""
Somfy awning control for the Ptown house — via TaHoma → SmartThings.

Why SmartThings and not Somfy's own cloud: Somfy killed third-party
password login for migrated North America accounts (Dec 2024) and the
app-generated Developer Mode token only works on the hub's LOCAL API —
unreachable from GitHub Actions. But TaHoma has a "Works with SmartThings"
certified integration ("Somfy Window Treatment"), so we route through the
SmartThings cloud exactly like the garage (OHD) and lock (Yale). Same
OAuth-In SmartApp, same scopes (x:devices:* for commands), zero new
credentials. See tahoma.py for the direct-API attempt and its post-mortem.

The awnings are RTS motors — one-way radio. SmartThings tracks an ASSUMED
state (last command it sent), not ground truth, so this module is
CONTROL-ONLY and is not part of the hourly monitoring run.

One-time setup:
    SmartThings app → Add device → Partner devices → "Somfy Window
    Treatment" (TaHoma) → sign in with the TaHoma account → authorize.
    The awnings then appear as windowShade devices on the account.

Usage:
    python3 awnings.py --discover      # list windowShade-capable devices
    python3 awnings.py close           # retract ALL awnings
    python3 awnings.py open            # extend ALL awnings
    python3 awnings.py pause           # stop mid-travel
    python3 awnings.py close --match deck   # subset by label

No third-party dependencies — reuses garage.py's SmartThings helpers.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import garage  # SmartThings HTTP helpers + .env loader (same account/token)
import smartthings_oauth

HERE = Path(__file__).resolve().parent

COMMANDS = ("open", "close", "pause")

# Capabilities Somfy's linked service may expose, in preference order.
# Stateful shades get `windowShade` (direct open/close/pause commands);
# one-way RTS motors are often exposed with the STATELESS capability
# `statelessCurtainPowerButton`, whose single `setButton` command takes the
# verb as an argument. `windowShadeLevel` is a last-resort position setter.
SHADE_CAPABILITIES = ("windowShade", "statelessCurtainPowerButton",
                      "windowShadeLevel")


def _shade_capability(dev: dict) -> str | None:
    """Return the best-matching shade capability id for this device."""
    caps: set[str] = set()
    for component in dev.get("components", []):
        for cap in component.get("capabilities", []):
            caps.add(cap.get("id"))
    for wanted in SHADE_CAPABILITIES:
        if wanted in caps:
            return wanted
    return None


def find_shade_devices(devices: list[dict]) -> list[dict]:
    return [d for d in devices if _shade_capability(d) is not None]


def _device_caps(dev: dict) -> list[str]:
    out: list[str] = []
    for component in dev.get("components", []):
        for cap in component.get("capabilities", []):
            if cap.get("id"):
                out.append(cap["id"])
    return out


def _inventory(devices: list[dict]) -> str:
    """Compact device inventory for diagnostics — skips the ~40 Caseta
    switches (pure `switch` devices) so the interesting ones stand out."""
    lines: list[str] = []
    skipped = 0
    for d in devices:
        caps = _device_caps(d)
        if set(caps) <= {"switch", "switchLevel", "refresh", "healthCheck"}:
            skipped += 1
            continue
        label = d.get("label") or d.get("name") or d.get("deviceId", "?")
        lines.append(f"{label}[{','.join(sorted(set(caps)))}]")
    return "; ".join(lines) + f" (+{skipped} plain switches hidden)"


def _command_payload(capability: str, verb: str) -> dict:
    """Map a verb to the right command shape for the capability."""
    if capability == "statelessCurtainPowerButton":
        return {"component": "main", "capability": capability,
                "command": "setButton", "arguments": [verb]}
    if capability == "windowShadeLevel":
        level = {"open": 100, "close": 0}.get(verb)
        if level is None:
            raise ValueError(f"windowShadeLevel can't express '{verb}'")
        return {"component": "main", "capability": capability,
                "command": "setShadeLevel", "arguments": [level]}
    return {"component": "main", "capability": capability, "command": verb}


def send_shade_command(token: str, dev: dict, verb: str) -> str:
    """Send `verb` using the device's best capability. Needs x:devices:*.

    Returns the capability used (for result details)."""
    capability = _shade_capability(dev)
    if capability is None:
        raise ValueError("device has no shade capability")
    payload = {"commands": [_command_payload(capability, verb)]}
    garage._post(f"{garage.API_BASE}/devices/{dev['deviceId']}/commands",
                 token, payload)
    return capability


def run_command(command: str, match: str | None = None) -> list[dict]:
    """Send `command` to every awning (optionally label-filtered).

    Returns control.py-shaped result dicts. 'ok' means SmartThings accepted
    the command — RTS gives no confirmation the motor moved, by design.
    """
    garage.load_env(HERE / ".env")
    try:
        token = smartthings_oauth.get_access_token()
    except Exception as e:
        return [{"device": "awnings:auth", "ok": False, "detail": str(e)}]

    try:
        devices = garage.list_devices(token)
        shades = find_shade_devices(devices)
    except Exception as e:
        return [{"device": "awnings:discover", "ok": False, "detail": str(e)}]

    if match:
        shades = [d for d in shades
                  if match.lower() in (d.get("label") or d.get("name") or "").lower()]
    if not shades:
        return [{"device": "awnings:discover", "ok": False,
                 "detail": "no shade-capable devices found"
                           + (f" matching '{match}'" if match else "")
                           + " — is the Somfy Window Treatment linked service "
                             "connected in SmartThings? Inventory: "
                           + _inventory(devices)}]

    out: list[dict] = []
    for d in shades:
        name = d.get("label") or d.get("name") or d.get("deviceId", "?")
        try:
            cap = send_shade_command(token, d, command)
            out.append({"device": f"awnings:{name}", "ok": True,
                        "detail": f"command → {command} (via {cap})"})
        except Exception as e:
            out.append({"device": f"awnings:{name}", "ok": False, "detail": str(e)})
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Somfy awning control (TaHoma via SmartThings)")
    parser.add_argument("command", nargs="?", choices=COMMANDS,
                        help="windowShade command to send to all awnings")
    parser.add_argument("--match", help="only awnings whose label contains this")
    parser.add_argument("--discover", action="store_true",
                        help="list windowShade-capable devices on the account")
    parser.add_argument("--json", dest="emit_json", action="store_true")
    args = parser.parse_args()

    if not args.discover and not args.command:
        parser.error("need a command (open/close/pause) or --discover")

    if args.discover:
        garage.load_env(HERE / ".env")
        try:
            token = smartthings_oauth.get_access_token()
            devices = garage.list_devices(token)
        except Exception as e:
            print(f"awnings: ERROR — {e}", file=sys.stderr)
            return 1
        shades = find_shade_devices(devices)
        print(f"{len(shades)} windowShade device(s) "
              f"(of {len(devices)} total on the SmartThings account):")
        for d in shades:
            label = d.get("label") or d.get("name") or "(unnamed)"
            print(f"  {label}   ({d.get('deviceId')})")
        if not shades:
            print("  — none. Link 'Somfy Window Treatment' in the "
                  "SmartThings app first.")
        return 0 if shades else 1

    results = run_command(args.command, args.match)
    if args.emit_json:
        print(json.dumps({"system": "awnings", "results": results}, indent=2))
    else:
        for r in results:
            mark = "✓" if r["ok"] else "✗"
            print(f"  {mark} {r['device']}: {r['detail']}")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
