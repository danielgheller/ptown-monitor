#!/usr/bin/env python3
"""
Send the Ptown dashboard output to Daniel via email, using Resend.

Resend is a transactional email service (https://resend.com). We picked it
over Gmail SMTP so that if the API key ever leaks, the blast radius is
"someone sends up to 3000 emails/month from onboarding@resend.dev" rather
than "someone has full read/write access to a primary inbox."

The free tier without a verified domain requires that the recipient email
match the account owner's email. That's us, so no domain verification is
needed.

Reads from `.env` in the same folder:

    RESEND_API_KEY=re_...             # API key from resend.com dashboard
    NOTIFY_TO_EMAIL=danielgheller@gmail.com   # recipient (your Resend signup address)
    NOTIFY_FROM_EMAIL=Ptown Monitor <onboarding@resend.dev>   # optional override
    DASHBOARD_URL=https://...         # optional; appended to email body as
                                      # "Live dashboard: <url>". Defaults to
                                      # the GH Pages URL below.

Usage:
    ./ptown notify                      # send email if overall status is not OK
    ./ptown notify --daily              # always send (for the daily summary)
    ./ptown notify --test               # send a hardcoded test email and exit
    ./ptown notify --stdin              # read dashboard JSON from stdin and send
    ./ptown notify --on-change-only     # send ONLY when status differs from the
                                        # last run (tracked in notify-state.json).
                                        # This is what the hourly scheduler uses
                                        # to avoid sending 24 emails during a
                                        # sustained outage.

When invoked without --stdin, it shells out to `dashboard.py --json` itself
so the scheduler can just call `./ptown notify` / `./ptown notify --daily`.

Both a plain-text body and an HTML body (mirroring the web dashboard's
card layout — status pill + SET/NOW boxed pills per device) are sent in the
same Resend payload. Clients that render HTML see the styled version; ones
that don't fall back to the plain-text version automatically.

Exit codes:
    0 = email sent (or intentionally suppressed, i.e. all-OK on a non-daily run)
    1 = email send failed
    2 = configuration error (missing API key, bad recipient, etc.)
"""
from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESEND_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "Ptown Monitor <onboarding@resend.dev>"
# Where the live HTML dashboard is hosted. Can be overridden with
# DASHBOARD_URL in .env (e.g. to point at a staging fork or localhost).
DEFAULT_DASHBOARD_URL = "https://danielgheller.github.io/ptown-monitor/"
DASHBOARD_TIMEOUT = 90
STATE_FILE = HERE / "notify-state.json"


# ---------- tiny .env loader ----------
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


# ---------- subject-line building ----------
STATUS_PREFIX = {
    "ok":   "✅ Ptown OK",
    "warn": "⚠️  Ptown WARN",
    "crit": "🚨 Ptown ALERT",
}


def _build_subject(dashboard: dict, *, is_daily: bool) -> str:
    status = dashboard.get("overall_status", "ok")
    prefix = STATUS_PREFIX.get(status, "Ptown")
    if is_daily:
        prefix += " — daily"
    # Append short system hint for at-a-glance triage in iOS notifications.
    if status != "ok":
        bad_systems = [
            s["system"]
            for s in dashboard.get("systems", [])
            if s.get("overall_status") != "ok"
        ]
        if bad_systems:
            prefix += f" — {', '.join(bad_systems)}"
    # Date suffix on daily summaries so the archive is easy to scan by month
    # ("did I get my April 18 summary?") and so two same-day emails aren't
    # indistinguishable in a long thread.
    if is_daily:
        prefix += f" ({time.strftime('%b %d')})"
    return prefix


def _build_body(dashboard: dict) -> str:
    """Turn the JSON dashboard into a plain-text email body.

    We render the same shape dashboard.py does when invoked without --json,
    but without emoji (iOS mail renders them fine but some email clients
    garble them — plain ASCII is safest).
    """
    lines = [f"Ptown status — {dashboard.get('timestamp', time.strftime('%Y-%m-%dT%H:%M:%S%z'))}", ""]
    tag = {"ok": "[ OK ]", "warn": "[WARN]", "crit": "[CRIT]"}

    for sys_result in dashboard.get("systems", []):
        system = sys_result.get("system", "?")
        label_map = {"nuheat": "Heated floors", "hottub": "Hot tub", "nest": "Nest"}
        label = label_map.get(system, system.title())
        overall = sys_result.get("overall_status", "ok")
        lines.append(f"{tag.get(overall, '[ ?? ]')} {label}")

        if sys_result.get("error"):
            lines.append(f"     ! {sys_result['error']}")
            lines.append("")
            continue

        for dev in sys_result.get("devices", []):
            name = dev.get("name", "?")
            cur = dev.get("current_f")
            setp = dev.get("setpoint_f")
            mode = dev.get("mode") or ""
            cur_s = f"{cur:5.1f}°F" if cur is not None else "   ? °F"
            set_s = f"{setp:5.1f}°F" if setp is not None else "   ? °F"
            detail = f" — {dev['reason']}" if dev.get("reason") else ""
            suffix = f"  [{dev.get('status', 'ok').upper()}]" if dev.get("status") not in (None, "ok") else ""
            line = f"     {name:<20} {cur_s}  (set {set_s}, {mode}){suffix}{detail}"
            lines.append(line)
        lines.append("")

    overall = dashboard.get("overall_status", "ok")
    if overall == "ok":
        lines.append("All systems nominal.")
    elif overall == "warn":
        lines.append("WARNING: one or more systems need a look (see above).")
    else:
        lines.append("CRITICAL: something is wrong (see above).")
    lines.append("")
    # Link to the live GH Pages dashboard — gives 24h + 7d trend sparklines
    # per device, which this text body intentionally doesn't try to render.
    # Most mail clients (Gmail, Apple Mail) autolink plain-text URLs.
    dashboard_url = (os.environ.get("DASHBOARD_URL") or DEFAULT_DASHBOARD_URL).strip()
    if dashboard_url:
        lines.append(f"Live dashboard: {dashboard_url}")
        lines.append("")
    lines.append("— Ptown Monitor")
    return "\n".join(lines)


# ---------- HTML body ----------
# Status palette — each tier has a light background, a strong foreground (for
# WARN/CRIT text), and a dot color. Kept inline because email clients strip
# <style> blocks inconsistently (Gmail web keeps them, Outlook.com does not),
# so every color has to be written at the tag it applies to.
_STATUS_COLORS = {
    "ok":   {"bg": "#dcfce7", "fg": "#166534", "dot": "#16a34a", "label": "OK"},
    "warn": {"bg": "#fef3c7", "fg": "#92400e", "dot": "#d97706", "label": "WARN"},
    "crit": {"bg": "#fee2e2", "fg": "#991b1b", "dot": "#dc2626", "label": "CRIT"},
}
_SYSTEM_LABELS = {"nuheat": "Heated floors", "hottub": "Hot tub", "nest": "Nest"}


def _fmt_f(v) -> str:
    return f"{v:.1f}°F" if v is not None else "—"


def _build_html_body(dashboard: dict, dashboard_url: str) -> str:
    """Render an HTML email body that mirrors the web dashboard's card layout.

    Uses table-based layout with inline styles for maximum email-client
    compatibility (Gmail, Apple Mail, Outlook, iOS Mail). No <style> blocks,
    no external CSS, no web fonts — everything renders from inline attributes.
    Each device gets a SET pill + NOW pill side-by-side, color-coded when a
    device is off-target.
    """
    overall = dashboard.get("overall_status", "ok")
    ov = _STATUS_COLORS.get(overall, _STATUS_COLORS["ok"])
    ts = html.escape(dashboard.get("timestamp", ""))

    parts: list[str] = []
    parts.append(
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '</head>'
        '<body style="margin:0;padding:20px;background:#f5f5f7;'
        "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
        'color:#1a1a1a;">'
        '<div style="max-width:560px;margin:0 auto;">'
    )

    # Header: title + overall status pill
    parts.append(
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;margin-bottom:14px;">'
        '<tr>'
        '<td align="left" style="vertical-align:middle;">'
        '<div style="font-size:18px;font-weight:600;color:#111827;">Ptown Monitor</div>'
        f'<div style="font-size:12px;color:#6b7280;margin-top:2px;">{ts}</div>'
        '</td>'
        f'<td align="right" style="vertical-align:middle;">'
        f'<span style="display:inline-block;background:{ov["bg"]};color:{ov["fg"]};'
        f'font-size:11px;font-weight:700;letter-spacing:0.5px;padding:5px 12px;'
        f'border-radius:999px;">{ov["label"]}</span>'
        '</td></tr></table>'
    )

    # System cards
    for sys_result in dashboard.get("systems", []):
        system = sys_result.get("system", "?")
        label = _SYSTEM_LABELS.get(system, system.title())
        sys_overall = sys_result.get("overall_status", "ok")
        sm = _STATUS_COLORS.get(sys_overall, _STATUS_COLORS["ok"])

        parts.append(
            '<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;'
            'padding:14px 16px;margin-bottom:10px;">'
            # Card header: system name + status label
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="border-collapse:collapse;margin-bottom:6px;">'
            '<tr>'
            '<td align="left" style="font-size:14px;font-weight:600;color:#111827;">'
            f'<span style="color:{sm["dot"]};margin-right:6px;">●</span>'
            f'{html.escape(label)}'
            '</td>'
            f'<td align="right" style="font-size:11px;font-weight:700;'
            f'letter-spacing:0.5px;color:{sm["fg"]};">{sm["label"]}</td>'
            '</tr></table>'
        )

        if sys_result.get("error"):
            err = html.escape(sys_result["error"])
            parts.append(
                '<div style="font-size:13px;color:#991b1b;padding:8px 0 2px 0;">'
                f'! {err}</div>'
            )
        else:
            devices = sys_result.get("devices") or []
            for dev in devices:
                name = html.escape(dev.get("name", "?"))
                mode = html.escape(dev.get("mode") or "")
                dev_status = dev.get("status", "ok")
                dm = _STATUS_COLORS.get(dev_status, _STATUS_COLORS["ok"])
                cur_s = _fmt_f(dev.get("current_f"))
                set_s = _fmt_f(dev.get("setpoint_f"))
                # NOW pill gets tinted if this specific device is off-target;
                # SET pill stays neutral since setpoint isn't "wrong" per se.
                now_bg = dm["bg"] if dev_status != "ok" else "#f9fafb"
                now_border = dm["dot"] if dev_status != "ok" else "#e5e7eb"
                now_fg = dm["fg"] if dev_status != "ok" else "#111827"

                parts.append(
                    '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                    'style="border-collapse:collapse;border-top:1px solid #f3f4f6;">'
                    '<tr>'
                    # Device name + mode (mode is a small grey subtitle, like "heat", "standby")
                    '<td align="left" style="padding:10px 0;vertical-align:middle;'
                    'font-size:13px;color:#374151;">'
                    f'{name}'
                    + (f'<div style="font-size:11px;color:#9ca3af;margin-top:2px;">{mode}</div>'
                       if mode else '')
                    + '</td>'
                    # SET pill
                    '<td align="right" width="82" style="padding:8px 0 8px 6px;vertical-align:middle;">'
                    '<div style="background:#f3f4f6;border:1px solid #e5e7eb;'
                    'border-radius:6px;padding:6px 10px;text-align:center;">'
                    '<div style="font-size:9px;color:#6b7280;font-weight:700;letter-spacing:0.5px;">SET</div>'
                    f'<div style="font-size:14px;font-weight:600;color:#111827;'
                    f'font-variant-numeric:tabular-nums;">{set_s}</div>'
                    '</div>'
                    '</td>'
                    # NOW pill
                    '<td align="right" width="82" style="padding:8px 0 8px 6px;vertical-align:middle;">'
                    f'<div style="background:{now_bg};border:1px solid {now_border};'
                    f'border-radius:6px;padding:6px 10px;text-align:center;">'
                    '<div style="font-size:9px;color:#6b7280;font-weight:700;letter-spacing:0.5px;">NOW</div>'
                    f'<div style="font-size:14px;font-weight:600;color:{now_fg};'
                    f'font-variant-numeric:tabular-nums;">{cur_s}</div>'
                    '</div>'
                    '</td>'
                    '</tr></table>'
                )
                if dev.get("reason") and dev_status != "ok":
                    reason = html.escape(dev["reason"])
                    parts.append(
                        f'<div style="font-size:12px;color:{dm["fg"]};'
                        f'padding:0 0 8px 0;">{reason}</div>'
                    )

        parts.append('</div>')  # /card

    # Footer: dashboard link + signoff
    if dashboard_url:
        safe_url = html.escape(dashboard_url, quote=True)
        parts.append(
            '<div style="text-align:center;margin-top:18px;font-size:13px;">'
            f'<a href="{safe_url}" style="color:#2563eb;text-decoration:none;'
            'font-weight:500;">View live dashboard →</a>'
            '</div>'
        )
    parts.append(
        '<div style="text-align:center;margin-top:10px;font-size:11px;color:#9ca3af;">'
        '— Ptown Monitor</div>'
        '</div></body></html>'
    )

    return "".join(parts)


# ---------- Resend HTTP ----------
def _send_resend(api_key: str, from_addr: str, to_addr: str,
                 subject: str, text_body: str,
                 html_body: str | None = None) -> None:
    # Sending both "text" and "html" lets modern clients render the styled
    # version while still giving text-only / HTML-blocked clients a clean
    # fallback. Resend treats "text" as the preferred plain-text alternative
    # when both are provided.
    payload_dict: dict = {
        "from": from_addr,
        "to": [to_addr],
        "subject": subject,
        "text": text_body,
    }
    if html_body:
        payload_dict["html"] = html_body
    payload = json.dumps(payload_dict).encode()
    req = urllib.request.Request(
        RESEND_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "ptown-monitor/0.1",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode()
            # Resend returns {"id": "..."} on success
            try:
                parsed = json.loads(body)
                msg_id = parsed.get("id", "?")
            except json.JSONDecodeError:
                msg_id = "?"
            print(f"Sent (id={msg_id})")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Resend HTTP {e.code}: {body}") from e


# ---------- state tracking (for --on-change-only) ----------
def _current_state(dashboard: dict) -> dict:
    """Extract just the status bits worth comparing across runs."""
    return {
        "overall_status": dashboard.get("overall_status", "ok"),
        "system_statuses": {
            s.get("system", "?"): s.get("overall_status", "ok")
            for s in dashboard.get("systems", [])
        },
    }


def _load_previous_state() -> dict | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


def _save_state(state: dict) -> None:
    state_with_ts = {**state, "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    STATE_FILE.write_text(json.dumps(state_with_ts, indent=2) + "\n")


def _state_differs(current: dict, previous: dict | None) -> bool:
    """True if we should alert — either first-run-with-issues or any status changed."""
    if previous is None:
        # No prior state. Only alert if something is currently wrong. Otherwise
        # silently establish a baseline.
        return current.get("overall_status", "ok") != "ok"
    if current.get("overall_status") != previous.get("overall_status"):
        return True
    cur_sys = current.get("system_statuses", {})
    prev_sys = previous.get("system_statuses", {})
    if set(cur_sys.keys()) != set(prev_sys.keys()):
        return True
    for k, v in cur_sys.items():
        if prev_sys.get(k) != v:
            return True
    return False


def _fetch_dashboard_json() -> dict:
    """Invoke dashboard.py --json in the same interpreter and return parsed dict."""
    result = subprocess.run(
        [sys.executable, str(HERE / "dashboard.py"), "--json"],
        capture_output=True, text=True, timeout=DASHBOARD_TIMEOUT,
    )
    if not result.stdout.strip():
        raise RuntimeError(f"dashboard.py produced no output; stderr={result.stderr[:500]}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"dashboard.py emitted invalid JSON: {e}; "
                           f"stdout_head={result.stdout[:300]}") from e


def main() -> int:
    parser = argparse.ArgumentParser(description="Email the Ptown status via Resend")
    parser.add_argument("--daily", action="store_true",
                        help="always send, regardless of status (daily summary)")
    parser.add_argument("--test", action="store_true",
                        help="send a short hardcoded test email and exit")
    parser.add_argument("--stdin", action="store_true",
                        help="read dashboard JSON from stdin instead of invoking dashboard.py")
    parser.add_argument("--on-change-only", dest="on_change_only", action="store_true",
                        help="send email only if status changed since last run "
                             "(state persisted in notify-state.json)")
    args = parser.parse_args()

    _load_env(HERE / ".env")
    api_key = (os.environ.get("RESEND_API_KEY") or "").strip()
    to_addr = (os.environ.get("NOTIFY_TO_EMAIL") or "").strip()
    from_addr = (os.environ.get("NOTIFY_FROM_EMAIL") or DEFAULT_FROM).strip()

    if not api_key:
        print("Missing RESEND_API_KEY in .env", file=sys.stderr)
        return 2
    if not to_addr:
        print("Missing NOTIFY_TO_EMAIL in .env", file=sys.stderr)
        return 2

    if args.test:
        subject = "Ptown Monitor — test email"
        body = (
            "This is a test email from the Ptown monitor.\n"
            "If you're seeing this, Resend is wired up correctly.\n"
        )
        try:
            _send_resend(api_key, from_addr, to_addr, subject, body)
        except Exception as e:
            print(f"Send failed: {e}", file=sys.stderr)
            return 1
        return 0

    # Get the dashboard JSON.
    try:
        if args.stdin:
            dashboard = json.loads(sys.stdin.read())
        else:
            dashboard = _fetch_dashboard_json()
    except Exception as e:
        print(f"Could not obtain dashboard data: {e}", file=sys.stderr)
        return 1

    overall = dashboard.get("overall_status", "ok")

    # --on-change-only: suppress anything that matches prior state.
    if args.on_change_only:
        current_state = _current_state(dashboard)
        previous_state = _load_previous_state()
        changed = _state_differs(current_state, previous_state)
        # Always persist current state, even if we won't send — this keeps the
        # state file fresh and lets us detect future changes correctly.
        _save_state(current_state)
        if not changed:
            print(f"Status unchanged from previous run ({overall}); no email sent.")
            return 0
        print(f"Status changed (prev={previous_state}, now={current_state}); sending.")

    # Suppress non-daily OK runs (unless --on-change-only already decided to send).
    elif overall == "ok" and not args.daily:
        print("Status OK and --daily not set; suppressing email.")
        return 0

    subject = _build_subject(dashboard, is_daily=args.daily)
    body = _build_body(dashboard)
    # HTML body mirrors the web dashboard's card layout. Same DASHBOARD_URL
    # resolution as the text body so the "View live dashboard" link points
    # to the same place in both.
    dashboard_url = (os.environ.get("DASHBOARD_URL") or DEFAULT_DASHBOARD_URL).strip()
    html_body = _build_html_body(dashboard, dashboard_url)
    try:
        _send_resend(api_key, from_addr, to_addr, subject, body, html_body=html_body)
    except Exception as e:
        print(f"Send failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
