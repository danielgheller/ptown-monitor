"""SmartThings OAuth-In access-token manager.

Wraps the refresh_token → access_token flow for the Ptown Monitor
OAuth-In SmartApp (App Id a5811ecc-9d37-455f-9744-1c640f68bd4f).
Used by garage.py, lock.py, and caseta.py — replacing the static
SMARTTHINGS_TOKEN PAT which was retired after Samsung's 2024-12-30
policy change capped PAT lifetime at 24 hours.

Public API:
    get_access_token() -> str

Environment:
    SMARTTHINGS_CLIENT_ID       (required)
    SMARTTHINGS_CLIENT_SECRET   (required)
    SMARTTHINGS_REFRESH_TOKEN   (bootstrap only — used when the state
                                 file doesn't exist or is invalid)
    SMARTTHINGS_STATE_FILE      (optional override; default
                                 smartthings-oauth-state.json next
                                 to this file)

State file shape (JSON):
    {
      "access_token":  "<uuid>",
      "refresh_token": "<uuid>",       # rotates on every refresh
      "expires_at":    <epoch_seconds>,
      "scope":         "r:devices:*"
    }

The refresh_token ROTATES on every use — each refresh response returns
a new refresh_token and invalidates the old one. The state file is the
source of truth; the env var is only the bootstrap value. In GH Actions
the state file is persisted across runs via actions/cache (same pattern
as notify-state.json).
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


TOKEN_URL = "https://api.smartthings.com/oauth/token"
DEFAULT_STATE_FILE = Path(__file__).resolve().parent / "smartthings-oauth-state.json"
REFRESH_BUFFER_S = 300  # refresh if access_token expires within 5 minutes


# ---------- tiny .env loader (matches the pattern in garage.py et al) ----------
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


def _state_path() -> Path:
    override = os.environ.get("SMARTTHINGS_STATE_FILE")
    return Path(override) if override else DEFAULT_STATE_FILE


def _read_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                return json.load(f)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    except (json.JSONDecodeError, OSError):
        return None


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write to a sibling tmp file then atomic-rename, so a crash mid-write
    # can't leave the state file half-truncated and invalidate the rolling
    # refresh_token.
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            json.dump(state, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    tmp.replace(path)


def _do_refresh(client_id: str, client_secret: str, refresh_token: str) -> dict[str, Any]:
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        TOKEN_URL,
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
        return json.loads(resp.read().decode())


def get_access_token() -> str:
    """Return a current SmartThings access_token, refreshing if needed.

    Looks at the state file first; if its access_token still has > 5
    minutes left, returns it. Otherwise calls the OAuth token endpoint
    with the rotated refresh_token (or the env-var bootstrap value),
    persists the new tokens to the state file, and returns the new
    access_token.
    """
    # Load .env on the caller's behalf so device scripts can just import
    # this module without worrying about ordering.
    _load_env(Path(__file__).resolve().parent / ".env")

    client_id = (os.environ.get("SMARTTHINGS_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("SMARTTHINGS_CLIENT_SECRET") or "").strip()
    env_refresh = (os.environ.get("SMARTTHINGS_REFRESH_TOKEN") or "").strip()
    if not client_id or not client_secret:
        raise RuntimeError(
            "Missing SMARTTHINGS_CLIENT_ID / SMARTTHINGS_CLIENT_SECRET "
            "(set in .env or GH Actions Secrets)"
        )

    path = _state_path()
    state = _read_state(path) or {}

    now = time.time()
    access = state.get("access_token")
    expires_at = state.get("expires_at", 0)
    if access and expires_at - now > REFRESH_BUFFER_S:
        return access

    refresh = state.get("refresh_token") or env_refresh
    if not refresh:
        raise RuntimeError(
            "No refresh_token available — set SMARTTHINGS_REFRESH_TOKEN "
            "in .env (or GH Secrets) and re-run, or re-do the OAuth auth "
            "flow if it's been > 30 days."
        )

    resp = _do_refresh(client_id, client_secret, refresh)
    new_state = {
        "access_token": resp["access_token"],
        # SmartThings rotates refresh tokens; fall back to the previous
        # value if the response doesn't include one (defensive).
        "refresh_token": resp.get("refresh_token", refresh),
        "expires_at": now + int(resp.get("expires_in", 86400)),
        "scope": resp.get("scope"),
    }
    _write_state(path, new_state)
    return new_state["access_token"]


if __name__ == "__main__":
    # Smoke test: print the current access token (or refresh if needed).
    print(get_access_token())
