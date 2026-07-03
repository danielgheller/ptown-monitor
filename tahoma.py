#!/usr/bin/env python3
"""
Somfy awning control via the Overkiz cloud API — SUPERSEDED, kept as a
post-mortem and for possible on-LAN use.

*** Production awning control lives in awnings.py (TaHoma → SmartThings).***

Why superseded (July 2026 investigation): Somfy's Dec 2024 unified-account
migration closed North America cloud access to third parties. Every cloud
path fails for a migrated account:
  - legacy userId/password on ha401-1  → 401
  - unified OAuth JWT as Bearer on NA  → "An API key is required..."
  - unified OAuth JWT→session exchange → 401 Bad credentials
  - unified OAuth on EU endpoint       → "No such user account"
  - app-generated Developer Mode token → cloud: Bad credentials
The Developer Mode token authorizes only the hub's LOCAL API (port 8443 on
the Ptown LAN) — unreachable from GitHub Actions. This module still works
against nothing cloud-side today; it MAY be useful from a laptop at the
house via the local API someday. See home-assistant/core#132228.

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


def _credentials() -> tuple[str, str, str]:
    """Return (token, email, password); token is the preferred auth.

    TAHOMA_TOKEN is an app-generated Developer Mode token (TaHoma app →
    Configure installation → gateway parameters → tap the PIN 7 times →
    Developer Mode → generate token). Since Somfy's Dec 2024 unified-account
    migration this is the ONLY auth the NA cloud platform accepts from
    third parties — password login returns 401/"An API key is required".
    Email/password are kept as a fallback for never-migrated accounts.
    """
    token = (os.environ.get("TAHOMA_TOKEN") or "").strip()
    email = (os.environ.get("TAHOMA_EMAIL") or "").strip()
    password = (os.environ.get("TAHOMA_PASSWORD") or "").strip()
    if not token and not (email and password):
        raise RuntimeError(
            "No TaHoma credentials — set TAHOMA_TOKEN (preferred; generate "
            "in the TaHoma app under Developer Mode) or TAHOMA_EMAIL + "
            "TAHOMA_PASSWORD in .env / GH secrets."
        )
    return token, email, password


# ---------- pyoverkiz import shim (v1 vs v2 API) ----------
# pyoverkiz had a v2 rewrite (credentials objects, Python 3.12+). Support
# both call styles so a version bump doesn't strand us.
#
# LOGIN PATH GOTCHA (learned the hard way, July 2026): pyoverkiz — v1 AND
# v2.0.3 — only routes the modern accounts.somfy.com OAuth login for
# Server.SOMFY_EUROPE. Server.SOMFY_AMERICA falls through to the legacy
# userId/userPassword form login on ha401-1.overkiz.com, which Somfy killed
# for accounts migrated to the unified Somfy login (Dec 2024 migration) —
# it just returns 401 even with correct credentials. The auth strategy is
# chosen by the `server` FIELD of the ServerConfig while requests go to the
# `endpoint` field, so we can force the unified OAuth against the NA
# endpoint with a hybrid config. We try, in order:
#   1. unified OAuth + NA endpoint  (migrated NA account, NA installation)
#   2. unified OAuth + EU endpoint  (installation moved to global platform)
#   3. legacy form login + NA endpoint  (never-migrated NA account)
def _client_factories():
    try:
        from pyoverkiz.client import OverkizClient  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "pyoverkiz is not installed (requires Python 3.12+). "
            "Run: pip install 'pyoverkiz>=2'"
        ) from e
    from pyoverkiz.client import OverkizClient
    from pyoverkiz.models import Command

    factories: list[tuple[str, object]] = []
    try:  # v2
        from pyoverkiz.auth.credentials import UsernamePasswordCredentials
        from pyoverkiz.auth.strategies import SomfyAuthStrategy
        from pyoverkiz.const import SUPPORTED_SERVERS
        from pyoverkiz.enums import APIType, Server
        from pyoverkiz.models import ServerConfig

        na_endpoint = SUPPORTED_SERVERS[Server.SOMFY_AMERICA].endpoint

        class _SomfyJwtSessionStrategy(SomfyAuthStrategy):
            """Unified Somfy OAuth → classic Overkiz cookie session.

            The NA platform (ha401-1) rejects raw Bearer JWTs with "An API
            key is required to access this setup", but accepts the classic
            cookie-session login when the JWT is posted as a form field —
            the same jwt-exchange pattern pyoverkiz uses for CozyTouch.
            Field name isn't documented for Somfy NA, so we try the known
            Overkiz variants in order.
            """

            async def login(self) -> None:
                await super().login()  # accounts.somfy.com OAuth → context token
                token = self.context.access_token
                last = None
                for payload in (
                    {"jwt": token},
                    {"accessToken": token},
                    {"userId": self.credentials.username, "ssoToken": token},
                ):
                    async with self.session.post(
                        f"{self.server.endpoint}login",
                        data=payload,
                        ssl=self._ssl,
                    ) as response:
                        if response.status in (200, 204):
                            return
                        last = f"{response.status} {(await response.text())[:120]}"
                raise RuntimeError(f"jwt→session exchange failed: {last}")

            async def auth_headers(self, path: str | None = None):
                return {}  # cookie session carries auth; no Bearer header

        _hybrid_na = ServerConfig(
            server=Server.SOMFY_EUROPE,  # selects SomfyAuthStrategy in factory
            name="Somfy (North America, unified login)",
            endpoint=na_endpoint,        # requests still go to ha401-1
            manufacturer="Somfy",
            api_type=APIType.CLOUD,
        )

        def _make(server_or_config, jwt_session: bool = False):
            def make_client(email: str, password: str) -> "OverkizClient":
                client = OverkizClient(
                    server=server_or_config,
                    credentials=UsernamePasswordCredentials(email, password),
                )
                if jwt_session:
                    client._auth = _SomfyJwtSessionStrategy(
                        UsernamePasswordCredentials(email, password),
                        client.session,
                        client.server_config,
                        client._ssl,
                    )
                return client
            return make_client

        factories = [
            ("somfy-oauth-jwt-session+NA", _make(_hybrid_na, jwt_session=True)),
            ("somfy-oauth-bearer+NA", _make(_hybrid_na)),
            ("somfy-oauth+EU-endpoint", _make(Server.SOMFY_EUROPE)),
            ("legacy-NA-login", _make(Server.SOMFY_AMERICA)),
        ]
    except ImportError:  # v1
        from pyoverkiz.const import SUPPORTED_SERVERS

        def make_client_v1(email: str, password: str) -> "OverkizClient":
            return OverkizClient(
                email, password, server=SUPPORTED_SERVERS["somfy_north_america"]
            )

        factories = [("legacy-NA-login", make_client_v1)]

    return factories, Command


def _token_factory():
    """Factory for the app-generated Developer Mode token (Bearer on cloud NA)."""
    from pyoverkiz.auth.credentials import TokenCredentials
    from pyoverkiz.client import OverkizClient
    from pyoverkiz.enums import Server

    def make_client(token: str) -> "OverkizClient":
        return OverkizClient(
            server=Server.SOMFY_AMERICA,
            credentials=TokenCredentials(token=token),
        )
    return make_client


async def _login_any(token: str, email: str, password: str):
    """Try each login path in order; return (client, devices, path_label).

    A path is accepted only if login succeeds AND the account has devices
    there — a unified account can authenticate against a platform that
    doesn't host the installation (0 devices), which shouldn't win.
    """
    candidates: list[tuple[str, object]] = []
    if token:
        candidates.append(("devmode-token+NA", None))  # placeholder, built below
    if email and password:
        factories, _ = _client_factories()
        candidates.extend(factories)

    errors: list[str] = []
    for label, make in candidates:
        client = None
        try:
            if label == "devmode-token+NA":
                client = _token_factory()(token)
            else:
                client = make(email, password)
            await client.login()
            devices = await client.get_devices()
            if devices:
                return client, devices, label
            errors.append(f"{label}: login ok but 0 devices")
        except Exception as e:  # try the next path
            errors.append(f"{label}: {e}")
        if client is not None:
            try:
                await client.close()
            except Exception:
                pass
    raise RuntimeError("all Somfy login paths failed — " + " | ".join(errors))


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
    _, Command = _client_factories()
    token, email, password = _credentials()

    out: dict = {"system": "tahoma", "action": verb, "results": [], "devices": []}
    client, devices, login_path = await _login_any(token, email, password)
    out["login_path"] = login_path
    try:
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
    finally:
        try:
            await client.close()
        except Exception:
            pass
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
        print(f"TaHoma devices — {len(out['devices'])} found "
              f"(login path: {out.get('login_path')}):")
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
