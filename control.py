#!/usr/bin/env python3
"""
Apply a named "control action" to the Ptown house devices.

Invoked by the control.yml GitHub Actions workflow, which is itself triggered
either via `workflow_dispatch` from the Cloudflare Worker (one-tap email
button) or manually from the GH Actions UI for testing.

Actions
-------
  away_all          Tub setpoint → 65°F + heat mode REST.
                    Nest (all 3) → manual_eco. Floors (all) → 41°F.
                    Side effect: marks IN_PTOWN flag as away (caller commits).
  tub_104           Tub → 104°F, heat mode READY (active heating).
  nest_off_eco      All Nest thermostats: set ThermostatEco.SetMode OFF.
                    They revert to whatever HEAT setpoint was last saved.
  master_bath_72    Nuheat thermostat with "master" in its name → 72°F.
  garage_close      Send CLOSE to the Overhead Door garage (SmartThings).
                    Close-only by design — no remote open.
  awnings_close     Retract all Somfy awnings (TaHoma → SmartThings). RTS
                    motors are one-way, so success = "command accepted",
                    not "confirmed closed".
  awnings_open      Extend all Somfy awnings.

Why "exit eco only" instead of also setting a heat target: the user picked
that option explicitly in the design conversation. Nest's SDM API treats
eco-OFF as "go back to whatever you were doing before" — when the device
flipped INTO eco, its prior heat setpoint was preserved server-side, and
SetMode OFF restores it. Cleaner than asserting a setpoint we don't know.

Output
------
JSON to stdout with `{"action": ..., "results": [{device, ok, detail}, ...]}`
so the workflow can surface failures in its run log without us having to
parse human-readable text. Exit code 0 if every device call succeeded;
exit 1 if any failed (the workflow then surfaces it as a failed run, which
shows up in the GH Actions UI / mobile notifications).

Usage
-----
    python control.py away_all
    python control.py tub_104
    python control.py nest_off_eco
    python control.py master_bath_72

Reads the same `.env` / GH Actions secrets that nuheat.py + hottub.py +
nest.py read.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Reuse the auth + list functions from the read-only scripts so we don't
# duplicate login logic. Each script's module-level `load_env`/`_load_env`
# is also called by main() to pull credentials from .env when running
# locally; in CI the env vars come straight from GH Actions secrets.
import nuheat
import nest as nest_mod

HERE = Path(__file__).resolve().parent

# Action setpoints. Single source of truth so the email button labels and
# the actual values can't drift apart.
NUHEAT_AWAY_F = 41.0          # freeze-protect baseline (matches dashboard.py warn line)
NUHEAT_MASTER_BATH_F = 72.0   # comfortable arrival temp for the master bath floor
TUB_AWAY_F = 65.0             # cool-off baseline; matches cost-protection alert threshold
TUB_HOT_F = 104.0             # standard "ready to use" hot tub temp


# ---------- result aggregation ----------
def _result(device: str, ok: bool, detail: str = "") -> dict:
    return {"device": device, "ok": ok, "detail": detail}


# ---------- Nuheat actions ----------
def _is_master_bath(thermostat: dict) -> bool:
    """Heuristic: any thermostat whose room/name contains 'master' (case-insensitive).

    The user's three floors are named per-bathroom; the master bath floor
    is the one we want for the master_bath_72 action. If the name scheme
    ever changes this matcher will need to follow.
    """
    name = (thermostat.get("Room") or thermostat.get("Name") or "").lower()
    return "master" in name


def _nuheat_login() -> tuple[str, list[dict]]:
    """Log into Nuheat and return (session_id, list_of_thermostats)."""
    email = (os.environ.get("NUHEAT_EMAIL") or "").strip()
    password = (os.environ.get("NUHEAT_PASSWORD") or "").strip()
    if not email or not password:
        raise RuntimeError("NUHEAT_EMAIL / NUHEAT_PASSWORD not set")
    sid = nuheat.authenticate(email, password)
    return sid, nuheat.list_thermostats(sid)


def action_nuheat_set(temp_f: float, *, only_master_bath: bool) -> list[dict]:
    """Set Nuheat thermostat setpoints.

    only_master_bath=True  → just the master-bath floor, used for the
                             master_bath_72 button.
    only_master_bath=False → every thermostat the account has, used for
                             the away_all button.
    """
    out: list[dict] = []
    try:
        sid, thermostats = _nuheat_login()
    except Exception as e:
        return [_result("nuheat:login", False, str(e))]

    if not thermostats:
        return [_result("nuheat:list", False, "no thermostats found on this account")]

    targeted = [t for t in thermostats if _is_master_bath(t)] if only_master_bath else thermostats
    if only_master_bath and not targeted:
        # Surface this clearly — if the master-bath room name changes upstream
        # the button silently doing nothing would be the worst failure mode.
        names = ", ".join((t.get("Room") or t.get("Name") or "?") for t in thermostats)
        return [_result(
            "nuheat:master_bath", False,
            f"no thermostat name contains 'master' (have: {names})",
        )]

    for t in targeted:
        name = t.get("Room") or t.get("Name") or t.get("SerialNumber") or "?"
        serial = t.get("SerialNumber") or t.get("Serial")
        if not serial:
            out.append(_result(f"nuheat:{name}", False, "no SerialNumber on listing"))
            continue
        try:
            nuheat.set_setpoint_f(sid, serial, temp_f)
            out.append(_result(f"nuheat:{name}", True, f"setpoint → {temp_f:.0f}°F"))
        except Exception as e:
            out.append(_result(f"nuheat:{name}", False, str(e)))
    return out


# ---------- Nest actions ----------
def _nest_setup() -> tuple[str, list[dict]]:
    """Refresh access token, list thermostats. Returns (access_token, devices)."""
    missing = [k for k in ("NEST_PROJECT_ID", "NEST_CLIENT_ID",
                           "NEST_CLIENT_SECRET", "NEST_REFRESH_TOKEN")
               if not (os.environ.get(k) or "").strip()]
    if missing:
        raise RuntimeError(f"missing env: {', '.join(missing)}")
    access_token = nest_mod.refresh_access_token(
        os.environ["NEST_CLIENT_ID"].strip(),
        os.environ["NEST_CLIENT_SECRET"].strip(),
        os.environ["NEST_REFRESH_TOKEN"].strip(),
    )
    devices = nest_mod.list_devices(os.environ["NEST_PROJECT_ID"].strip(), access_token)
    thermostats = [d for d in devices if d.get("type") == "sdm.devices.types.THERMOSTAT"]
    return access_token, thermostats


def action_nest_eco_on() -> list[dict]:
    """Put every Nest thermostat into MANUAL_ECO. Used by away_all."""
    out: list[dict] = []
    try:
        access_token, thermostats = _nest_setup()
    except Exception as e:
        return [_result("nest:setup", False, str(e))]
    for d in thermostats:
        name = nest_mod._short_name(d)
        resource = d.get("name", "")
        try:
            nest_mod.execute_command(
                resource, access_token,
                "sdm.devices.commands.ThermostatEco.SetMode",
                {"mode": "MANUAL_ECO"},
            )
            out.append(_result(f"nest:{name}", True, "eco → MANUAL_ECO"))
        except Exception as e:
            out.append(_result(f"nest:{name}", False, str(e)))
    return out


def action_nest_eco_off() -> list[dict]:
    """Take every Nest thermostat OUT of eco mode (keep current setpoint).

    SetMode OFF is the SDM way of saying "stop eco override, return to
    whatever HEAT mode + setpoint was active before eco kicked in." That
    matches Daniel's chosen behavior: exit eco, keep current setpoint.
    """
    out: list[dict] = []
    try:
        access_token, thermostats = _nest_setup()
    except Exception as e:
        return [_result("nest:setup", False, str(e))]
    for d in thermostats:
        name = nest_mod._short_name(d)
        resource = d.get("name", "")
        try:
            nest_mod.execute_command(
                resource, access_token,
                "sdm.devices.commands.ThermostatEco.SetMode",
                {"mode": "OFF"},
            )
            out.append(_result(f"nest:{name}", True, "eco → OFF"))
        except Exception as e:
            out.append(_result(f"nest:{name}", False, str(e)))
    return out


# ---------- SmartTub actions ----------
# The python-smarttub library is async-only and requires aiohttp. We isolate
# all the async machinery inside one helper that returns a sync list of
# results so the action_* callers stay symmetric with the Nuheat/Nest paths.
async def _async_set_tub(temp_f: float, heat_mode_name: str) -> list[dict]:
    """Set the tub setpoint AND its heat mode.

    HeatMode enum values exposed by python-smarttub: ECONOMY DAY AUTO READY REST.
      - READY  = "keep at temp" (active heating to the setpoint)
      - REST   = "let it cool" (used for our away setting; the watercare
                 'AWAY_FROM_HOME' label has to be flipped manually in the
                 SmartTub app — the library doesn't expose a setter for it).
      - ECONOMY/DAY/AUTO = various energy-saving schedules; we don't use them.
    """
    import aiohttp  # local import: only needed for the tub path
    from smarttub.api import SmartTub, Spa

    email = (os.environ.get("SMARTTUB_EMAIL") or "").strip()
    password = (os.environ.get("SMARTTUB_PASSWORD") or "").strip()
    if not email or not password:
        return [_result("hottub:env", False, "SMARTTUB_EMAIL / SMARTTUB_PASSWORD not set")]

    out: list[dict] = []
    async with aiohttp.ClientSession() as session:
        st = SmartTub(session)
        try:
            await st.login(email, password)
        except Exception as e:
            return [_result("hottub:login", False, str(e))]

        try:
            account = await st.get_account()
            spas = await account.get_spas()
        except Exception as e:
            return [_result("hottub:list", False, str(e))]

        if not spas:
            return [_result("hottub:list", False, "no spas on this account")]

        # Convert temp_f → temp_c for the library. SmartTub rejects more than
        # 1 decimal place (returns HTTP 500) so we round before sending.
        temp_c = round((temp_f - 32) * 5 / 9, 1)

        for spa in spas:
            name = getattr(spa, "name", None) or getattr(spa, "id", "spa")
            # Issue temperature first, then mode. Order matters because some
            # heat modes (REST) won't actively heat regardless of setpoint —
            # if Daniel taps "Tub 104" we want the heat mode flipped LAST so
            # the active-heating signal is the most recent state change the
            # tub sees.
            try:
                await spa.set_temperature(temp_c)
                out.append(_result(
                    f"hottub:{name}:setpoint", True,
                    f"set → {temp_f:.0f}°F ({temp_c:.1f}°C)",
                ))
            except Exception as e:
                out.append(_result(f"hottub:{name}:setpoint", False, str(e)))

            try:
                mode = Spa.HeatMode[heat_mode_name]
                await spa.set_heat_mode(mode)
                out.append(_result(
                    f"hottub:{name}:heatmode", True,
                    f"heat mode → {heat_mode_name}",
                ))
            except Exception as e:
                out.append(_result(f"hottub:{name}:heatmode", False, str(e)))

    return out


def action_tub_set(temp_f: float, heat_mode_name: str) -> list[dict]:
    """Sync wrapper around the async SmartTub setter."""
    return asyncio.run(_async_set_tub(temp_f, heat_mode_name))


# ---------- Garage action (SmartThings) ----------
def action_garage_close() -> list[dict]:
    """Send CLOSE to the garage door. Close-only by design.

    Reuses garage.py's discovery + command path. Requires the OAuth token
    to carry x:devices:* (granted by the smartthings_bootstrap.py re-auth);
    a 403 here means the scope is missing.
    """
    import garage  # local import: keeps module load light for other actions
    import smartthings_oauth

    try:
        token = smartthings_oauth.get_access_token()
    except Exception as e:
        return [_result("garage:auth", False, str(e))]

    device_id = (os.environ.get("SMARTTHINGS_DEVICE_ID") or "").strip()
    capability = None
    if not device_id:
        try:
            door_devices = garage.find_door_devices(garage.list_devices(token))
        except Exception as e:
            return [_result("garage:discover", False, str(e))]
        if not door_devices:
            return [_result("garage:discover", False, "no door-capable devices visible")]
        if len(door_devices) > 1:
            names = ", ".join(d.get("label") or d.get("deviceId", "?")
                              for d in door_devices)
            return [_result("garage:discover", False,
                            f"multiple door devices ({names}) — pin SMARTTHINGS_DEVICE_ID")]
        device_id = door_devices[0]["deviceId"]
        capability = garage.device_has_door_capability(door_devices[0])

    try:
        garage.send_door_command(token, device_id, "close", capability)
        return [_result("garage:door", True, "close command accepted")]
    except Exception as e:
        return [_result("garage:door", False, str(e))]


# ---------- Awning actions (Somfy TaHoma via SmartThings) ----------
def action_awnings(verb: str) -> list[dict]:
    """Send open/close to every awning, through SmartThings.

    RTS = fire-and-forget: 'ok' means SmartThings accepted the command,
    not that the awning moved. Somfy's own NA cloud is closed to third
    parties (see awnings.py header), so this rides the same OAuth-In
    SmartApp as the garage and lock.
    """
    import awnings  # local import to keep module load light
    return awnings.run_command(verb)


# ---------- IN_PTOWN flag (committed by the workflow, not by us) ----------
# We don't write to git here. The Cloudflare Worker (the one that triggers
# this workflow) commits IN_PTOWN before dispatching. That keeps the git
# write path centralized — the worker already has the GitHub PAT for it,
# the workflow runner doesn't need extra permissions, and the IN_PTOWN
# state takes effect the moment the user taps the button rather than 30s
# later when this workflow finishes.


# ---------- entry point ----------
ACTIONS = {
    "away_all":       "Set everything to its away/eco/freeze baseline",
    "tub_104":        "Heat the tub to 104°F (READY mode)",
    "nest_off_eco":   "Take all Nest thermostats out of eco mode",
    "master_bath_72": "Set the master bath floor to 72°F",
    "garage_close":   "Close the garage door (close-only by design)",
    "awnings_close":  "Retract all Somfy awnings",
    "awnings_open":   "Extend all Somfy awnings",
}


def run_action(action: str) -> tuple[list[dict], bool]:
    """Run the named action. Returns (results, all_ok)."""
    results: list[dict] = []

    if action == "away_all":
        # Run all three system-side calls. They're independent — a Nest
        # failure shouldn't prevent us from setting the tub or floors.
        results += action_tub_set(TUB_AWAY_F, "REST")
        results += action_nest_eco_on()
        results += action_nuheat_set(NUHEAT_AWAY_F, only_master_bath=False)
    elif action == "tub_104":
        results += action_tub_set(TUB_HOT_F, "READY")
    elif action == "nest_off_eco":
        results += action_nest_eco_off()
    elif action == "master_bath_72":
        results += action_nuheat_set(NUHEAT_MASTER_BATH_F, only_master_bath=True)
    elif action == "garage_close":
        results += action_garage_close()
    elif action == "awnings_close":
        results += action_awnings("close")
    elif action == "awnings_open":
        results += action_awnings("open")
    else:
        return [_result("action", False, f"unknown action: {action}")], False

    all_ok = all(r["ok"] for r in results)
    return results, all_ok


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a Ptown control action")
    parser.add_argument("action", choices=sorted(ACTIONS.keys()),
                        help="which action to perform; see ACTIONS table")
    args = parser.parse_args()

    # Load .env for local invocations; CI ignores this (no .env present in CI).
    nuheat.load_env(HERE / ".env")

    started_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    results, ok = run_action(args.action)
    finished_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    print(json.dumps({
        "action": args.action,
        "started_at": started_at,
        "finished_at": finished_at,
        "ok": ok,
        "results": results,
    }, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
