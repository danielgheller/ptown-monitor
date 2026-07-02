#!/usr/bin/env python3
"""
One-shot SmartThings OAuth bootstrap / recovery for the Ptown Monitor
OAuth-In SmartApp (App Id a5811ecc-9d37-455f-9744-1c640f68bd4f).

Run this when BOTH refresh tokens are dead (cached + bootstrap secret) —
the state smartthings_oauth.py reports as "refresh failed for all candidate
tokens". That happens if the rotating token gets double-spent (the June 2026
hourly/daily concurrency bug) or the 30-day sliding window lapses.

What it does:
  1. Prints the authorize URL. Open it, sign into Samsung, approve.
     NOTE: we now request `x:devices:*` in addition to `r:devices:*` so the
     garage-close control action can send commands. If the authorize page
     rejects the scope, the SmartApp itself needs updating first:
         smartthings apps:oauth:update a5811ecc-9d37-455f-9744-1c640f68bd4f
     and add "x:devices:*" to the scope list.
  2. You paste back the `code=` param from the redirect URL.
  3. Exchanges the code for tokens, writes smartthings-oauth-state.json,
     and prints the refresh_token to store as the SMARTTHINGS_REFRESH_TOKEN
     GitHub secret.

After running:
  - Update the SMARTTHINGS_REFRESH_TOKEN secret in GH → Settings → Secrets.
  - Purge the stale `ptown-st-oauth-*` entries in GH → Actions → Caches
    (they hold the dead token and would win over the fresh secret).

Usage:
    python3 smartthings_bootstrap.py [--redirect-uri URI] [--scope "..."]

Reads SMARTTHINGS_CLIENT_ID / SMARTTHINGS_CLIENT_SECRET (and optionally
SMARTTHINGS_REDIRECT_URI) from .env.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import smartthings_oauth  # reuse the .env loader + state writer

HERE = Path(__file__).resolve().parent
AUTHORIZE_URL = "https://api.smartthings.com/oauth/authorize"
DEFAULT_SCOPE = "r:devices:* x:devices:*"


def main() -> int:
    parser = argparse.ArgumentParser(description="SmartThings OAuth bootstrap")
    parser.add_argument("--redirect-uri",
                        default=None,
                        help="must exactly match a redirect URI registered on "
                             "the SmartApp (falls back to SMARTTHINGS_REDIRECT_URI in .env)")
    parser.add_argument("--scope", default=DEFAULT_SCOPE,
                        help=f"OAuth scopes to request (default: {DEFAULT_SCOPE!r})")
    args = parser.parse_args()

    smartthings_oauth._load_env(HERE / ".env")
    client_id = (os.environ.get("SMARTTHINGS_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("SMARTTHINGS_CLIENT_SECRET") or "").strip()
    redirect_uri = (args.redirect_uri
                    or os.environ.get("SMARTTHINGS_REDIRECT_URI") or "").strip()
    if not client_id or not client_secret:
        print("SMARTTHINGS_CLIENT_ID / SMARTTHINGS_CLIENT_SECRET not set in .env",
              file=sys.stderr)
        return 1
    if not redirect_uri:
        print("No redirect URI — pass --redirect-uri or set "
              "SMARTTHINGS_REDIRECT_URI in .env. It must exactly match one "
              "registered on the SmartApp.", file=sys.stderr)
        return 1

    qs = urllib.parse.urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": args.scope,
    })
    print("\n1. Open this URL, sign in to Samsung, and approve:\n")
    print(f"   {AUTHORIZE_URL}?{qs}\n")
    print("2. After approving you'll land on the redirect URI with ?code=... —")
    code = input("   paste the code value here: ").strip()
    if not code:
        print("No code given; aborting.", file=sys.stderr)
        return 1

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }).encode()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        smartthings_oauth.TOKEN_URL,
        data=body,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "ptown-monitor/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        tokens = json.loads(resp.read().decode())

    state = {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_at": time.time() + int(tokens.get("expires_in", 86400)),
        "scope": tokens.get("scope"),
    }
    smartthings_oauth._write_state(smartthings_oauth._state_path(), state)

    print("\n✓ Token exchange succeeded. State file written.")
    print(f"  granted scope: {state['scope']}")
    if "x:devices" not in (state["scope"] or ""):
        print("  ⚠ x:devices:* NOT granted — garage-close button will 403. "
              "Update the SmartApp scopes and re-run.")
    print("\n3. Update the GitHub secret SMARTTHINGS_REFRESH_TOKEN to:\n")
    print(f"   {state['refresh_token']}\n")
    print("4. Purge the ptown-st-oauth-* entries under GH → Actions → Caches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
