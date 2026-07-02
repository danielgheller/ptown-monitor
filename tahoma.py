#!/usr/bin/env python3
"""
Somfy awning control for the Ptown house (TaHoma / Overkiz cloud API).

The awnings are RTS motors — one-way radio, no state feedback by hardware
design — so this module is CONTROL-ONLY. It is deliberately NOT part of the
hourly monitoring run (there is nothing to read); it exists for the control
buttons (control.py / control.yml) and ad-hoc CLI use.

API path: the same Overkiz cloud endpoint the "TaHoma by Somfy" app uses,
via the pyoverkiz library, against the North America server. Somfy's old
Open API (api.somfy.com) was sunset in June 2022 — don't go looking for it.

Rate-limit etiquette (Somfy has banned abusive cloud pollers): one login +
one device listing per invocation, no retry loops on auth failure. Our
usage — a few button taps a day — is far below any threshold.

Credentials in `.env` (same login as the TaHoma app):
    TAHOMA_EMAIL=
    TAHOMA_PASSWORD=

Usage:
    python3 tahoma.py --discover          # list all devices + their commands
    python3 tahoma.py close               # retract ALL awnings
    python3 tahoma.py open                # extend ALL awnings
    python3 tahoma.py stop                # halt mid-travel
    python3 tahoma.py my                  # go to the saved "my" position
    python3 tahoma.py close --match deck  # only awnings whose label matches

Requires Python 3.12+ and pyoverkiz (see requirements.txt). The GH Actions
control workflow satisfies both; an older local venv will get a clear error.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Command preference lists per verb. Overkiz device definitions vary by
# model — horizontal awnings often use deploy/undeploy, verticals use
# open/close. We read each device's own definition and pick the first
# command it actually supports.
COMMAND_PREFS: dict[str, list[str]] = {
    "close": ["close", "undeploy", "up"],
    "open":  ["open", "deploy", "down"],
    "stop":  ["stop"],
    "my":    ["my"],
}


# ---------- tiny .env loader (matches garage.py et al) ----------
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


def _credentials() -> tuple[str, str]:
    email = (os.environ.get("TAHOMA_EMAIL") or "").strip()
    password = (os.environ.get("TAHOMA_PASSWORD") or "").strip()
    if not email or not password:
        raise RuntimeError("TAHOMA_EMAIL / TAHOMA_PASSWORD not set")
    return email, password


# ---------- pyoverkiz import shim (v1 vs v2 API) ----------
# pyoverkiz had a v2 rewrite (credentials objects, Python 3.12+). Support
# both call styles so a version bump doesn't strand us.
def _import_pyoverkiz():
    try:
        from pyoverkiz.client import OverkizClient  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "pyoverkiz is not installed (requires Python 3.12+). "
            "Run: pip install 'pyoverkiz>=2'"
        ) from e
    from pyoverkiz.client import OverkizClient
    from pyoverkiz.models import Command

    try:  # v2
        from pyoverkiz.auth.credentials import UsernamePasswordCredentials
        from pyoverkiz.enums import Server

        def make_client(email: str, password: str) -> "OverkizClient":
            return OverkizClient(
                server=Server.SOMFY_AMERICA,
                credentials=UsernamePasswordCredentials(email, password),
            )
    except ImportError:  # v1
        from pyoverkiz.const import SUPPORTED_SERVERS

        def make_client(email: str, password: str) -> "OverkizClient":
            return OverkizClient(
                email, password, server=SUPPORTED_SERVERS["somfy_north_america"]
            )

    return make_client, Command


# ---------- device inspection helpers ----------
def _label(device) -> str:
    return getattr(device, "label", None) or getattr(device, "device_url", "?")


def _ui_class(device) -> str:
    v = getattr(device, "ui_class", None) or getattr(device, "widget", None) or ""
    return str(v)


def _controllable_name(device) -> str:
    return str(getattr(device, "controllable_name", "") or "")


def _command_names(device) -> set[str]:
    names: set[str] = set()
    definition = getattr(device, "definition", None)
    for c in (getattr(definition, "commands", None) or []):
        n = (
            getattr(c, "command_name", None)
            or getattr(c, "name", None)
            or (c if isinstance(c, str) else None)
        )
        if n:
            names.add(str(n))
    return names


def _is_awning(device) -> bool:
    if "awning" in _ui_class(device).lower():
        return True
    return "awning" in _controllable_name(device).lower()


def _resolve_command(device, verb: str) -> str:
    prefs = COMMAND_PREFS[verb]
    available = _command_names(device)
    if available:
        for cmd in prefs:
            if cmd in available:
                return cmd
    # Definition unreadable or no match — try the top preference blind and
    # let the API error surface in the result detail.
    return prefs[0]


# ---------- core actions ----------
async def _run(verb: str | None, match: str | None, discover: bool) -> dict:
    make_client, Command = _import_pyoverkiz()
    email, password = _credentials()

    out: dict = {"system": "tahoma", "action": verb, "results": [], "devices": []}
    async with make_client(email, password) as client:
        await client.login()
        devices = await client.get_devices()

        if discover:
            for d in devices:
                out["devices"].append({
                    "label": _label(d),
                    "device_url": getattr(d, "device_url", None),
                    "ui_class": _ui_class(d),
                    "controllable_name": _controllable_name(d),
                    "commands": sorted(_command_names(d)),
                    "is_awning": _is_awning(d),
                })
            return out

        targets = [d for d in devices if _is_awning(d)]
        if match:
            targets = [d for d in targets if match.lower() in _label(d).lower()]
        if not targets:
            out["results"].append({
                "device": "tahoma:discover", "ok": False,
                "detail": f"no awning devices matched"
                          + (f" '{match}'" if match else "")
                          + f" (account has {len(devices)} devices — try --discover)",
            })
            return out

        assert verb is not None
        for d in targets:
            cmd = _resolve_command(d, verb)
            device_url = getattr(d, "device_url", None)
            try:
                if hasattr(client, "execute_command"):
                    await client.execute_command(device_url, Command(name=cmd))
                else:  # v2 action-group API
                    from pyoverkiz.models import Action
                    await client.execute_action_group(
                        actions=[Action(device_url=device_url,
                                        commands=[Command(name=cmd)])],
                        label=f"ptown-monitor {verb}",
                    )
                out["results"].append({
                    "device": f"tahoma:{_label(d)}", "ok": True,
                    "detail": f"command → {cmd}",
                })
            except Exception as e:  # per-device isolation, like control.py
                out["results"].append({
                    "device": f"tahoma:{_label(d)}", "ok": False, "detail": str(e),
                })
    return out


def run_verb(verb: str, match: str | None = None) -> list[dict]:
    """Sync entry point for control.py. Returns control.py-shaped results."""
    load_env(HERE / ".env")
    try:
        out = asyncio.run(_run(verb, match, discover=False))
    except Exception as e:
        return [{"device": "tahoma:login", "ok": False, "detail": str(e)}]
    return out["results"]


# ---------- CLI ----------
def main() -> int:
    parser = argparse.ArgumentParser(description="Somfy awning control (TaHoma/Overkiz)")
    parser.add_argument("verb", nargs="?", choices=sorted(COMMAND_PREFS.keys()),
                        help="command to send to all awnings")
    parser.add_argument("--match", help="only awnings whose label contains this")
    parser.add_argument("--discover", action="store_true",
                        help="list every device on the account with its commands")
    parser.add_argument("--json", action="store_true", help="emit JSON output")
    args = parser.parse_args()

    if not args.discover and not args.verb:
        parser.error("need a verb (close/open/stop/my) or --discover")

    load_env(HERE / ".env")
    try:
        out = asyncio.run(_run(args.verb, args.match, args.discover))
    except Exception as e:
        print(json.dumps({"system": "tahoma", "error": str(e)}) if args.json
              else f"tahoma: ERROR — {e}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(out, indent=2))
        return 0 if all(r["ok"] for r in out["results"]) else 1

    if args.discover:
        print(f"TaHoma devices — {len(out['devices'])} found:")
        for d in out["devices"]:
            tag = "  [AWNING]" if d["is_awning"] else ""
            print(f"  {d['label']:<30} {d['ui_class']:<16} "
                  f"{d['controllable_name']}{tag}")
            print(f"    {d['device_url']}")
            print(f"    commands: {', '.join(d['commands']) or '(unreadable)'}")
        return 0

    ok = True
    for r in out["results"]:
        mark = "✓" if r["ok"] else "✗"
        ok = ok and r["ok"]
        print(f"  {mark} {r['device']}: {r['detail']}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
