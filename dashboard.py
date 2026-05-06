#!/usr/bin/env python3
"""
Friendly Ptown status dashboard — aggregates nuheat + hottub + nest into a
single traffic-light view with per-device current temp and setpoint.

Each sub-script is invoked with `--json` and returns a normalized shape:

    {
      "system":  "nuheat" | "hottub" | "nest",
      "devices": [ {name, current_f, setpoint_f, mode, online, extra}, ... ],
      "error":   null | "..."
    }

Dashboard evaluates each system as OK / WARN / CRIT based on thresholds
defined near the top of this file, then prints a per-device summary.

Exit code: 0 = all OK, 1 = at least one WARN, 2 = at least one CRIT or
any subsystem errored out entirely. The exit code is what the scheduled
runner uses to decide whether to fire an alert email.

Usage (via wrapper):
    ./ptown dashboard          # pretty-printed, colored status
    ./ptown dashboard --json   # emit aggregated JSON (for the notifier)
    ./ptown dashboard --plain  # no emoji, pipe-safe text
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = ["nuheat", "hottub", "nest"]
PER_SCRIPT_TIMEOUT = 60  # seconds

# ---------- in-ptown toggle ----------
# Cost-protection rules (warn-when-warm) are gated on a single user-controlled
# signal: the presence of an empty file named "IN_PTOWN" at the repo root.
# Daniel creates the file when he arrives at the house (3 clicks in the GH
# web UI) and deletes it when he leaves. Polarity is "default-absent = away
# from Ptown = cost-protection ON" so a forgotten/missing toggle errs on the
# alerting side. The file IS the toggle — visible at a glance in the repo
# tree, and a commit to flip it triggers an immediate workflow run.
IN_PTOWN_FLAG_FILE = HERE / "IN_PTOWN"


def _in_ptown() -> bool:
    """True if Daniel is at the house. Cost-protection rules are SUPPRESSED
    when in Ptown (he's allowed to crank the heat); they fire when away."""
    return IN_PTOWN_FLAG_FILE.exists()


# ---------- thresholds ----------
# Indoor air shouldn't drop below this — otherwise pipes are at risk.
NEST_MIN_OK_F = 45.0
NEST_MIN_OK_F_CRIT = 40.0

# Heated floor slab shouldn't drop far below its setpoint (they run a freeze
# protection setpoint of ~41°F). If a floor reports a value far below that,
# something's probably wrong (likely a sensor/offline issue).
FLOOR_MIN_OK_F = 35.0

# Hot tub: if actual water temp is more than this far BELOW the setpoint,
# the heater isn't keeping up. (Allows some drift in away-mode.)
HOTTUB_MAX_UNDERSHOOT_F = 8.0

# Cost-protection thresholds — fire WARN when AWAY flag is set AND a device's
# setpoint or current temp climbs above the unattended baseline. Daniel's
# numbers (2026-05-02): tight enough that any meaningful nudge from baseline
# trips an alert.
#   Floors: freeze-protect baseline is 41°F → anything above is unintended heat.
#   Nest:   eco baseline is ~60°F → setpoint nudge upward is paid heat.
#   Tub:    65°F is comfortably above ambient drift but well below use temp;
#           also catches a power-surge factory-reset to 104°F.
FLOOR_AWAY_MAX_SETPOINT_F = 41.0
NEST_AWAY_MAX_SETPOINT_F = 60.0
HOTTUB_AWAY_MAX_F = 65.0


# ---------- status labels ----------
class Status:
    OK = "ok"
    WARN = "warn"
    CRIT = "crit"


SEVERITY = {Status.OK: 0, Status.WARN: 1, Status.CRIT: 2}
EMOJI = {Status.OK: "🟢", Status.WARN: "🟡", Status.CRIT: "🔴"}
PLAIN = {Status.OK: "[ OK ]", Status.WARN: "[WARN]", Status.CRIT: "[CRIT]"}


def _run_subsystem(name: str) -> dict:
    """Run `{name}.py --json` and return its parsed JSON (or an error dict)."""
    script = HERE / f"{name}.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--json"],
            capture_output=True,
            text=True,
            timeout=PER_SCRIPT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"system": name, "devices": [],
                "error": f"timed out after {PER_SCRIPT_TIMEOUT}s"}
    except FileNotFoundError:
        return {"system": name, "devices": [],
                "error": f"script not found at {script}"}

    stdout = proc.stdout.strip()
    if not stdout:
        err = proc.stderr.strip() or f"no output (rc={proc.returncode})"
        return {"system": name, "devices": [], "error": err}
    try:
        return json.loads(stdout)
    except json.JSONDecodeError as e:
        return {"system": name, "devices": [],
                "error": f"invalid JSON from {name}: {e}; stderr={proc.stderr[:200]}"}


# ---------- per-device health evaluation ----------
# All evaluators take an `away` flag so cost-protection rules can be enabled
# globally via the AWAY file. Evaluators always run the safety/freeze-protect
# checks regardless of away — those are about preventing damage, not waste.
def _evaluate_nuheat(device: dict, *, away: bool) -> tuple[str, str | None]:
    if not device.get("online"):
        return Status.CRIT, "offline"
    cur = device.get("current_f")
    setp = device.get("setpoint_f")
    if cur is None:
        return Status.WARN, "no reading"
    if cur < FLOOR_MIN_OK_F:
        return Status.CRIT, f"{cur:.1f}°F — floor well below freeze-protect setpoint"
    if away and setp is not None and setp > FLOOR_AWAY_MAX_SETPOINT_F:
        return Status.WARN, f"setpoint {setp:.1f}°F while away — heating bumped?"
    return Status.OK, None


def _evaluate_hottub(device: dict, *, away: bool) -> tuple[str, str | None]:
    if not device.get("online"):
        return Status.CRIT, "offline"
    extra = device.get("extra", {}) or {}
    if extra.get("error_code"):
        code = extra["error_code"]
        title = extra.get("error_title") or "unknown"
        return Status.CRIT, f"error {code}: {title}"
    cur = device.get("current_f")
    setp = device.get("setpoint_f")
    if cur is None:
        return Status.WARN, "no water temp reading"
    watercare = (extra.get("watercare") or "").lower()
    # Cost-protection branch: gated on the AWAY flag (single source of truth
    # for "Daniel is not at the house"). Key off SETPOINT only — both the
    # cases this is meant to catch (unauthorized bump, power-surge factory
    # reset to 104°F) raise the setpoint, so the setpoint check covers them.
    # Water temp is intentionally NOT checked: after a legitimate "All away"
    # the tub coasts down at ~4–5°F/day, so a water-temp clause produces
    # several days of spurious WARNs even though the heater is off and no
    # money is being spent.
    if away:
        if setp is not None and setp > HOTTUB_AWAY_MAX_F:
            return Status.WARN, f"setpoint {setp:.1f}°F while away — heater bumped?"
    # Undershoot rule still uses watercare as its gate (not the AWAY file)
    # because it's tub-specific: a high setpoint with watercare=away_from_home
    # is one the user clearly isn't trying to maintain, so don't complain that
    # the tub is "below setpoint" then.
    if (
        watercare != "away_from_home"
        and setp is not None
        and (setp - cur) > HOTTUB_MAX_UNDERSHOOT_F
    ):
        return Status.WARN, f"water {cur:.1f}°F is {setp - cur:.1f}°F below setpoint"
    return Status.OK, None


def _evaluate_nest(device: dict, *, away: bool) -> tuple[str, str | None]:
    if not device.get("online"):
        return Status.CRIT, "offline"
    cur = device.get("current_f")
    setp = device.get("setpoint_f")
    if cur is None:
        return Status.WARN, "no temperature reading"
    if cur < NEST_MIN_OK_F_CRIT:
        return Status.CRIT, f"{cur:.1f}°F — pipe-freeze risk"
    if cur < NEST_MIN_OK_F:
        return Status.WARN, f"{cur:.1f}°F — getting cold indoors"
    if away and setp is not None and setp > NEST_AWAY_MAX_SETPOINT_F:
        return Status.WARN, f"setpoint {setp:.1f}°F while away — heating bumped?"
    return Status.OK, None


EVALUATORS = {
    "nuheat": _evaluate_nuheat,
    "hottub": _evaluate_hottub,
    "nest": _evaluate_nest,
}

SYSTEM_LABELS = {
    "nuheat": "Heated floors",
    "hottub": "Hot tub",
    "nest": "Nest",
}


def _evaluate_system(result: dict, *, away: bool) -> dict:
    """Annotate the subsystem result with per-device and overall status."""
    system = result.get("system", "?")
    evaluator = EVALUATORS.get(system, lambda d, *, away: (Status.OK, None))

    if result.get("error"):
        return {
            **result,
            "overall_status": Status.CRIT,
            "overall_reason": result["error"],
            "evaluated": [],
        }

    evaluated = []
    worst = Status.OK
    reasons: list[str] = []
    for dev in result.get("devices", []):
        status, reason = evaluator(dev, away=away)
        evaluated.append({"device": dev, "status": status, "reason": reason})
        if SEVERITY[status] > SEVERITY[worst]:
            worst = status
        if reason:
            reasons.append(f"{dev.get('name', '?')}: {reason}")

    if not evaluated:
        # No devices AND no error → odd but not critical; call it a warning.
        worst = Status.WARN
        reasons = ["no devices reported"]

    return {
        **result,
        "overall_status": worst,
        "overall_reason": "; ".join(reasons) if reasons else None,
        "evaluated": evaluated,
    }


# ---------- rendering ----------
def _fmt_f(v) -> str:
    return f"{v:5.1f}°F" if v is not None else "   ? °F"


def _render_text(aggregated: list[dict], *, use_emoji: bool) -> str:
    glyphs = EMOJI if use_emoji else PLAIN
    lines = [f"Ptown status — {time.strftime('%a %b %d %H:%M:%S %Z %Y')}", ""]

    for sys_result in aggregated:
        system = sys_result["system"]
        label = SYSTEM_LABELS.get(system, system)
        overall = sys_result["overall_status"]
        lines.append(f"{glyphs[overall]} {label}")

        if sys_result.get("error"):
            lines.append(f"     ! {sys_result['error']}")
            lines.append("")
            continue

        for item in sys_result["evaluated"]:
            dev = item["device"]
            name = dev.get("name", "?")
            cur = _fmt_f(dev.get("current_f"))
            setp = _fmt_f(dev.get("setpoint_f"))
            mode = dev.get("mode") or ""
            online = dev.get("online", True)
            offline_marker = "" if online else "  OFFLINE"
            detail = f"({mode})" if mode else ""
            status_marker = ""
            if item["status"] != Status.OK:
                status_marker = f"  ← {glyphs[item['status']]} {item['reason']}"
            lines.append(f"     {name:<20} {cur}  (set {setp}, {mode}){offline_marker}{status_marker}"
                         if mode else
                         f"     {name:<20} {cur}  (set {setp}){offline_marker}{status_marker}")
        lines.append("")

    # Overall verdict
    worst = max((s["overall_status"] for s in aggregated),
                key=lambda s: SEVERITY[s], default=Status.OK)
    if worst == Status.OK:
        lines.append("All systems nominal.")
    elif worst == Status.WARN:
        lines.append("⚠️  Some systems need a look." if use_emoji else "WARNING: some systems need a look.")
    else:
        lines.append("🚨 Something's wrong — see above." if use_emoji else "CRITICAL: something is wrong — see above.")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Friendly aggregated Ptown status dashboard")
    parser.add_argument("--json", dest="emit_json", action="store_true",
                        help="emit aggregated JSON (for programmatic consumers)")
    parser.add_argument("--plain", action="store_true",
                        help="no emoji (pipe-safe ASCII)")
    args = parser.parse_args()

    # Run all three subsystems in parallel — I/O bound.
    with ThreadPoolExecutor(max_workers=len(SCRIPTS)) as ex:
        raw_results = list(ex.map(_run_subsystem, SCRIPTS))

    in_ptown = _in_ptown()
    away = not in_ptown
    aggregated = [_evaluate_system(r, away=away) for r in raw_results]

    if args.emit_json:
        # Trim the internal "evaluated" list to something easy to consume —
        # device dicts already carry all the data, we just merge status/reason in.
        payload = []
        for sys_result in aggregated:
            payload.append({
                "system": sys_result["system"],
                "overall_status": sys_result["overall_status"],
                "overall_reason": sys_result.get("overall_reason"),
                "error": sys_result.get("error"),
                "devices": [
                    {**item["device"], "status": item["status"], "reason": item["reason"]}
                    for item in sys_result.get("evaluated", [])
                ],
            })
        worst = max((s["overall_status"] for s in aggregated),
                    key=lambda s: SEVERITY[s], default=Status.OK)
        print(json.dumps({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "overall_status": worst,
            "in_ptown": in_ptown,
            "systems": payload,
        }, indent=2))
    else:
        sys.stdout.write(_render_text(aggregated, use_emoji=not args.plain))

    # Exit code reflects worst status for the scheduled runner.
    worst = max((s["overall_status"] for s in aggregated),
                key=lambda s: SEVERITY[s], default=Status.OK)
    return {Status.OK: 0, Status.WARN: 1, Status.CRIT: 2}[worst]


if __name__ == "__main__":
    sys.exit(main())
