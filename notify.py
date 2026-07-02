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
import hashlib
import hmac
import html
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
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

# Repo URL is the base for one-tap toggle links in the email — GitHub's
# /new and /delete URL patterns deep-link straight to the create-file or
# delete-file UI on github.com mobile/web. Two taps from the email: tap
# link → GH UI loads → tap "Commit changes." Override via REPO_URL in .env
# if you ever fork.
DEFAULT_REPO_URL = "https://github.com/danielgheller/ptown-monitor"
IN_PTOWN_FILE = "IN_PTOWN"

# Setpoint-burst nudge thresholds. We compare each device's setpoint against
# the value from the previous run; if MIN_BURST_DEVICES or more changed by
# more than BURST_DELTA_F, the email surfaces a "did you arrive?" banner.
# 0.5°F filters out Nest's tiny eco-mode oscillation; 2 devices keeps
# scheduled single-thermostat changes from triggering.
BURST_DELTA_F = 0.5
MIN_BURST_DEVICES = 2

# Stale-state reminder: if the IN_PTOWN file hasn't flipped in this many days,
# every daily email gets a "still in/away from Ptown?" line with a one-tap
# toggle. Picked 30 to roughly match a long-but-not-snowbird absence.
STALE_REMINDER_DAYS = 30


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


_SYSTEM_SHORT = {"nuheat": "floors", "hottub": "tub", "nest": "indoor",
                 "garage": "garage", "lock": "lock", "caseta": "lights"}


def _bad_device_summary(sys_result: dict) -> str | None:
    """Build a short subject-line fragment for a non-OK system.

    Picks the worst-status device and pulls its current temp + watercare-style
    context so Daniel can decide from his lock screen whether to act. Examples:
      "tub 104°F (away)", "floor 33°F", "Cabana 38°F"
    Returns None if nothing useful can be summarized.
    """
    devices = sys_result.get("devices") or []
    if not devices:
        return None
    sev = {"ok": 0, "warn": 1, "crit": 2}
    bad = [d for d in devices if d.get("status") in ("warn", "crit")]
    if not bad:
        return None
    # Sort: highest severity first, then highest current temp first. The temp
    # tiebreaker matters when several devices are all WARN — show the warmest
    # one, since that's the worst offender.
    bad.sort(
        key=lambda d: (
            sev.get(d.get("status", "ok"), 0),
            d.get("current_f") if isinstance(d.get("current_f"), (int, float)) else -999,
        ),
        reverse=True,
    )
    dev = bad[0]
    system = sys_result.get("system", "?")
    cur = dev.get("current_f")
    # Garage: no temps; surface the door state directly so the subject reads
    # "garage open" / "garage opening" — that's exactly the triage signal.
    if system == "garage":
        if not dev.get("online", True):
            return "garage offline"
        state = (dev.get("mode") or "unknown").lower()
        return f"garage {state}"
    # Lock: same pattern. "lock unlocked" is the security signal we want
    # screaming from the subject line when away.
    if system == "lock":
        if not dev.get("online", True):
            return "lock offline"
        state = (dev.get("mode") or "unknown").lower()
        return f"lock {state}"
    # Caseta: roll all on-while-away devices into a single count. Per-device
    # names would blow the subject line; "3 lights on" tells Daniel enough
    # to know it's worth tapping in.
    if system == "caseta":
        devices = sys_result.get("devices") or []
        on_count = sum(1 for d in devices if (d.get("mode") or "").lower() == "on")
        if on_count:
            return f"{on_count} lights on"
        # Fall through to default; covers offline/unknown edge cases.
    # Offline / no-reading case: prefer the human reason over a literal "?"
    if not isinstance(cur, (int, float)):
        if not dev.get("online", True):
            return f"{_SYSTEM_SHORT.get(system, system)} offline"
        return f"{_SYSTEM_SHORT.get(system, system)} no reading"
    cur_s = f"{cur:.0f}°F"
    if system == "hottub":
        # Tub uses watercare ("away_from_home", "ready", etc.) — surface it so
        # "tub 104°F" vs "tub 104°F (away)" tells different stories at a glance.
        watercare = ((dev.get("extra") or {}).get("watercare") or "").lower()
        suffix = " (away)" if watercare == "away_from_home" else ""
        return f"tub {cur_s}{suffix}"
    if system == "nuheat":
        # Floors all named "<x> floor" — drop the "floor" suffix to save chars.
        name = (dev.get("name") or "floor").replace(" floor", "")
        return f"{name} floor {cur_s}"
    if system == "nest":
        return f"{dev.get('name', 'indoor')} {cur_s}"
    return f"{_SYSTEM_SHORT.get(system, system)} {cur_s}"


def _all_temps_summary(dashboard: dict) -> str | None:
    """Compact temp digest for the daily-OK subject — every device, in one line.

    Format: "tub 65°F · floors 41/41/41°F · indoor 60/60/65°F". Picks current
    temps (NOT setpoints) since the daily email's job is to confirm the actual
    state of the house. Slashes between sibling devices keep it tight enough
    to fit in a Gmail/iOS subject line preview without truncation.
    """
    parts: list[str] = []
    for sys_result in dashboard.get("systems", []):
        system = sys_result.get("system")
        devices = sys_result.get("devices") or []
        temps = [
            d.get("current_f")
            for d in devices
            if isinstance(d.get("current_f"), (int, float))
        ]
        if not temps:
            continue
        if system == "hottub":
            parts.append(f"tub {temps[0]:.0f}°F")
        elif system == "nuheat":
            parts.append("floors " + "/".join(f"{t:.0f}" for t in temps) + "°F")
        elif system == "nest":
            parts.append("indoor " + "/".join(f"{t:.0f}" for t in temps) + "°F")
    return " · ".join(parts) if parts else None


def _build_subject(dashboard: dict, *, is_daily: bool) -> str:
    status = dashboard.get("overall_status", "ok")
    prefix = STATUS_PREFIX.get(status, "Ptown")
    if is_daily:
        prefix += " — daily"
    # For non-OK runs: surface the offending devices' temps so the subject
    # line is enough on its own to triage from a lock screen / notification
    # ("⚠️ Ptown WARN — tub 104°F (away)" tells Daniel exactly what's up).
    if status != "ok":
        fragments = [
            frag
            for s in dashboard.get("systems", [])
            if s.get("overall_status") != "ok"
            for frag in [_bad_device_summary(s)]
            if frag
        ]
        if fragments:
            prefix += f" — {', '.join(fragments)}"
        else:
            # Fallback to the system name if we couldn't extract a temp.
            bad = [s["system"] for s in dashboard.get("systems", [])
                   if s.get("overall_status") != "ok"]
            if bad:
                prefix += f" — {', '.join(bad)}"
    elif is_daily:
        # Daily OK: append every device's current temp so the morning glance
        # answers "am I paying to heat anything I shouldn't be?" all in one
        # line, without opening the email.
        digest = _all_temps_summary(dashboard)
        if digest:
            prefix += f" — {digest}"
    # Date suffix on daily summaries so the archive is easy to scan by month
    # ("did I get my April 18 summary?") and so two same-day emails aren't
    # indistinguishable in a long thread.
    if is_daily:
        prefix += f" ({time.strftime('%b %d')})"
    return prefix


def _build_body(dashboard: dict, *, ctx: dict) -> str:
    """Turn the JSON dashboard into a plain-text email body.

    We render the same shape dashboard.py does when invoked without --json,
    but without emoji (iOS mail renders them fine but some email clients
    garble them — plain ASCII is safest).
    """
    lines = [f"Ptown status — {dashboard.get('timestamp', time.strftime('%Y-%m-%dT%H:%M:%S%z'))}", ""]
    # Top-of-body location + toggle. Plain text just shows the URL — the HTML
    # version turns this into a button.
    location = "in Ptown" if ctx["in_ptown"] else "away from Ptown"
    lines.append(f"Mode: {location} (cost-protection {'OFF' if ctx['in_ptown'] else 'ON'})")
    lines.append(f"Flip: {ctx['toggle_label']} → {ctx['toggle_url']}")
    lines.append("")
    # Quick-action one-tap controls. Plain-text just lists URLs; the HTML
    # body lays them out as buttons. We only render rows that have a real
    # signed URL (i.e. when the Worker is configured) so a half-deployed
    # state doesn't show fake-looking dead buttons in plain text.
    if ctx["control_actions"]:
        lines.append("Quick controls:")
        for _key, label, caption, url in ctx["control_actions"]:
            if url:
                lines.append(f"  {label} ({caption}): {url}")
        lines.append("")
    if ctx["burst_nudge"]:
        lines.append("Setpoints just changed: " + ", ".join(ctx["burst_changes"]))
        lines.append("Did you arrive? Tap the link above to flip the toggle.")
        lines.append("")
    if ctx["stale_nudge"]:
        lines.append(
            f"Heads up: it's been {ctx['stale_days']} days since you flipped "
            f"the toggle. Still {location}? Tap the link above to confirm."
        )
        lines.append("")
    tag = {"ok": "[ OK ]", "warn": "[WARN]", "crit": "[CRIT]"}

    for sys_result in dashboard.get("systems", []):
        system = sys_result.get("system", "?")
        label_map = {"nuheat": "Heated floors", "hottub": "Hot tub", "nest": "Nest",
                     "garage": "Garage door", "lock": "Front door lock",
                     "caseta": "Caseta lights"}
        label = label_map.get(system, system.title())
        overall = sys_result.get("overall_status", "ok")
        lines.append(f"{tag.get(overall, '[ ?? ]')} {label}")

        if sys_result.get("error"):
            lines.append(f"     ! {sys_result['error']}")
            lines.append("")
            continue

        # Caseta: 40+ devices would blow the body. Compact summary + list of
        # on-or-non-OK devices only, mirroring dashboard.py's rendering.
        if system == "caseta":
            devices = sys_result.get("devices") or []
            total = len(devices)
            on_devs = [d for d in devices if (d.get("mode") or "").lower() == "on"]
            warn_devs = [d for d in devices if d.get("status") not in (None, "ok")]
            if total == 0:
                lines.append("     (no devices reported)")
            elif not on_devs and not warn_devs:
                lines.append(f"     All {total} lights off")
            else:
                lines.append(f"     {len(on_devs)} of {total} lights on")
                for dev in (warn_devs if warn_devs else on_devs):
                    name = dev.get("name", "?")
                    level = (dev.get("extra") or {}).get("level")
                    level_str = f" ({level}%)" if isinstance(level, (int, float)) else ""
                    reason = f" — {dev['reason']}" if dev.get("reason") else ""
                    lines.append(f"       · {name}{level_str}{reason}")
            lines.append("")
            continue

        for dev in sys_result.get("devices", []):
            name = dev.get("name", "?")
            cur = dev.get("current_f")
            setp = dev.get("setpoint_f")
            mode = dev.get("mode") or ""
            detail = f" — {dev['reason']}" if dev.get("reason") else ""
            suffix = f"  [{dev.get('status', 'ok').upper()}]" if dev.get("status") not in (None, "ok") else ""
            # Non-temperature devices (garage door) render mode-only.
            if cur is None and setp is None:
                line = f"     {name:<20} {mode.upper()}{suffix}{detail}"
            else:
                cur_s = f"{cur:5.1f}°F" if cur is not None else "   ? °F"
                set_s = f"{setp:5.1f}°F" if setp is not None else "   ? °F"
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
_SYSTEM_LABELS = {"nuheat": "Heated floors", "hottub": "Hot tub", "nest": "Nest",
                  "garage": "Garage door", "lock": "Front door lock",
                  "caseta": "Caseta lights"}


def _fmt_f(v) -> str:
    return f"{v:.1f}°F" if v is not None else "—"


def _build_html_body(dashboard: dict, dashboard_url: str, *, ctx: dict) -> str:
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

    # Toggle button — top of email, always visible. State-aware: when in
    # Ptown, the button takes you to delete IN_PTOWN ("leaving"); when away,
    # to create it ("arriving"). Mode line above keeps the current state
    # legible without needing to read the button verb.
    location_label = "In Ptown" if ctx["in_ptown"] else "Away from Ptown"
    location_sub = (
        "Cost-protection OFF — you can crank everything up."
        if ctx["in_ptown"]
        else "Cost-protection ON — alerting on bumped setpoints."
    )
    safe_toggle_url = html.escape(ctx["toggle_url"], quote=True)
    parts.append(
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:collapse;background:#f3f4f6;border:1px solid #e5e7eb;'
        'border-radius:10px;padding:10px 14px;margin-bottom:10px;">'
        '<tr>'
        '<td align="left" style="vertical-align:middle;">'
        f'<div style="font-size:13px;font-weight:600;color:#111827;">{location_label}</div>'
        f'<div style="font-size:11px;color:#6b7280;margin-top:2px;">{location_sub}</div>'
        '</td>'
        '<td align="right" style="vertical-align:middle;padding-left:8px;">'
        f'<a href="{safe_toggle_url}" style="display:inline-block;background:#111827;'
        'color:#ffffff;text-decoration:none;font-size:12px;font-weight:600;'
        'padding:8px 14px;border-radius:8px;white-space:nowrap;">'
        f'{html.escape(ctx["toggle_label"])}</a>'
        '</td></tr></table>'
    )

    # Quick-action control buttons. 2-up grid on desktop, stacks naturally on
    # narrow mobile because each button is its own table cell with width:50%.
    # First button (away_all) is rendered in red to signal "this turns things
    # DOWN"; the warm-up buttons are dark/blue to signal "this turns things up."
    if ctx["control_actions"]:
        # Build cells in pairs so we get a 2-up layout with consistent spacing.
        rows: list[list[tuple[str, str, str, str]]] = []
        pair: list[tuple[str, str, str, str]] = []
        for key, label, caption, url in ctx["control_actions"]:
            pair.append((key, label, caption, url))
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)

        parts.append(
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            'style="border-collapse:collapse;margin-bottom:10px;">'
        )
        for row in rows:
            parts.append('<tr>')
            for key, label, caption, url in row:
                # Visual hierarchy: away_all is destructive-ish (turns the
                # house down), so it gets a muted red. The warm-up actions
                # share an ink-blue. Disabled (no Worker) state stays grey.
                if not url:
                    bg, fg, border = "#f3f4f6", "#9ca3af", "#e5e7eb"
                elif key == "away_all":
                    bg, fg, border = "#fef2f2", "#991b1b", "#fecaca"
                else:
                    bg, fg, border = "#eff6ff", "#1e40af", "#bfdbfe"
                href = html.escape(url, quote=True) if url else "#"
                disabled_attr = (
                    ' onclick="return false" aria-disabled="true"'
                    if not url else ""
                )
                parts.append(
                    '<td align="center" valign="top" width="50%" '
                    'style="padding:4px;">'
                    f'<a href="{href}"{disabled_attr} '
                    f'style="display:block;background:{bg};color:{fg};'
                    f'border:1px solid {border};border-radius:8px;'
                    f'padding:10px 8px;text-decoration:none;font-size:13px;'
                    f'font-weight:600;text-align:center;">'
                    f'<div>{html.escape(label)}</div>'
                    f'<div style="font-size:10px;font-weight:400;color:#6b7280;'
                    f'margin-top:3px;">{html.escape(caption)}</div>'
                    '</a></td>'
                )
            # Pad odd-length rows so the 2-up grid stays aligned.
            if len(row) == 1:
                parts.append('<td width="50%" style="padding:4px;"></td>')
            parts.append('</tr>')
        parts.append('</table>')

    # Setpoint-burst nudge — yellow banner, only when we suspect Daniel just
    # arrived (multiple setpoints jumped AND he's currently flagged "away").
    if ctx["burst_nudge"]:
        changes_text = html.escape(", ".join(ctx["burst_changes"]))
        parts.append(
            '<div style="background:#fef3c7;border:1px solid #fde68a;'
            'border-radius:10px;padding:10px 14px;margin-bottom:10px;'
            'font-size:13px;color:#92400e;">'
            f'<div style="font-weight:600;margin-bottom:2px;">Did you just arrive?</div>'
            f'<div style="color:#78350f;">Setpoints jumped: {changes_text}. '
            'Tap the toggle above to switch into "in Ptown" mode.</div>'
            '</div>'
        )

    # Stale-state nudge — same yellow banner, only on daily emails when it's
    # been STALE_REMINDER_DAYS+ since the last toggle flip.
    if ctx["stale_nudge"]:
        days = ctx["stale_days"]
        question = (
            "Still in Ptown?" if ctx["in_ptown"] else "Still away from Ptown?"
        )
        parts.append(
            '<div style="background:#fef3c7;border:1px solid #fde68a;'
            'border-radius:10px;padding:10px 14px;margin-bottom:10px;'
            'font-size:13px;color:#92400e;">'
            f'<div style="font-weight:600;margin-bottom:2px;">{question}</div>'
            f'<div style="color:#78350f;">It\'s been {days} days since the '
            'toggle was flipped. Tap the button above to confirm or change.</div>'
            '</div>'
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
        elif system == "caseta":
            # Compact Caseta layout: "N of M on" header pill + per-on-device
            # list below. Avoids the 40-row pill explosion that the standard
            # per-device renderer would produce for Caseta.
            devices = sys_result.get("devices") or []
            total = len(devices)
            on_devs = [d for d in devices if (d.get("mode") or "").lower() == "on"]
            warn_devs = [d for d in devices if d.get("status") not in (None, "ok")]
            summary_color = sm if on_devs or warn_devs else _STATUS_COLORS["ok"]
            if total == 0:
                summary_text = "(no devices reported)"
            elif not on_devs and not warn_devs:
                summary_text = f"All {total} lights off"
            else:
                summary_text = f"{len(on_devs)} of {total} lights on"
            parts.append(
                f'<div style="font-size:13px;color:#374151;padding:6px 0 4px 0;">'
                f'{html.escape(summary_text)}</div>'
            )
            shown = warn_devs if warn_devs else on_devs
            for dev in shown:
                name = html.escape(dev.get("name", "?"))
                level = (dev.get("extra") or {}).get("level")
                level_str = (
                    f' <span style="color:#9ca3af;">({int(level)}%)</span>'
                    if isinstance(level, (int, float)) else ""
                )
                dev_status = dev.get("status", "ok")
                dm = _STATUS_COLORS.get(dev_status, _STATUS_COLORS["ok"])
                color = dm["fg"] if dev_status != "ok" else "#374151"
                reason = (
                    f' <span style="color:{dm["fg"]};">— {html.escape(dev["reason"])}</span>'
                    if dev.get("reason") else ""
                )
                parts.append(
                    f'<div style="font-size:12px;color:{color};padding:2px 0 2px 12px;'
                    f'border-top:1px solid #f3f4f6;">'
                    f'· {name}{level_str}{reason}</div>'
                )
        else:
            devices = sys_result.get("devices") or []
            for dev in devices:
                name = html.escape(dev.get("name", "?"))
                mode = html.escape(dev.get("mode") or "")
                dev_status = dev.get("status", "ok")
                dm = _STATUS_COLORS.get(dev_status, _STATUS_COLORS["ok"])
                cur_val = dev.get("current_f")
                setp_val = dev.get("setpoint_f")

                # Non-temperature device (garage door): single STATE pill
                # instead of SET / NOW. The pill tints with the device status
                # so a CRIT door-open glows red on the lock-screen preview.
                if cur_val is None and setp_val is None:
                    state_label = (dev.get("mode") or "unknown").upper()
                    pill_bg = dm["bg"] if dev_status != "ok" else "#f9fafb"
                    pill_border = dm["dot"] if dev_status != "ok" else "#e5e7eb"
                    pill_fg = dm["fg"] if dev_status != "ok" else "#111827"
                    parts.append(
                        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                        'style="border-collapse:collapse;border-top:1px solid #f3f4f6;">'
                        '<tr>'
                        '<td align="left" style="padding:10px 0;vertical-align:middle;'
                        'font-size:13px;color:#374151;">'
                        f'{name}'
                        '</td>'
                        '<td align="right" width="120" style="padding:8px 0 8px 6px;vertical-align:middle;">'
                        f'<div style="background:{pill_bg};border:1px solid {pill_border};'
                        f'border-radius:6px;padding:6px 10px;text-align:center;">'
                        '<div style="font-size:9px;color:#6b7280;font-weight:700;letter-spacing:0.5px;">STATE</div>'
                        f'<div style="font-size:14px;font-weight:600;color:{pill_fg};">'
                        f'{html.escape(state_label)}</div>'
                        '</div>'
                        '</td>'
                        '</tr></table>'
                    )
                else:
                    cur_s = _fmt_f(cur_val)
                    set_s = _fmt_f(setp_val)
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


# ---------- toggle URL helpers ----------
def _repo_url() -> str:
    return (os.environ.get("REPO_URL") or DEFAULT_REPO_URL).strip().rstrip("/")


def _worker_url() -> str:
    """Cloudflare Worker base URL, e.g. https://ptown-toggle.daniel.workers.dev.
    Empty string means "Worker not configured" → fall back to GitHub UI URLs."""
    return (os.environ.get("WORKER_URL") or "").strip().rstrip("/")


def _toggle_secret() -> str:
    """Shared HMAC secret with the Worker. Empty → fall back to GitHub UI."""
    return os.environ.get("TOGGLE_SECRET") or ""


def _gh_ui_toggle_url(in_ptown: bool) -> str:
    """GitHub web-UI fallback (two-tap: link → 'Commit changes')."""
    if in_ptown:
        return f"{_repo_url()}/delete/main/{IN_PTOWN_FILE}"
    return f"{_repo_url()}/new/main?filename={IN_PTOWN_FILE}"


def _signed_action_url(action: str) -> str:
    """Build a signed Worker URL for any action (toggle or control).

    Same HMAC scheme as the original IN_PTOWN toggle — we just pass the
    action name straight through. The Worker's ACTIONS table decides what
    each action does (IN_PTOWN flip + optional workflow_dispatch). Returns
    "" when the Worker isn't configured, so callers can fall back.
    """
    base = _worker_url()
    secret = _toggle_secret()
    if not base or not secret:
        return ""
    ts = str(int(time.time()))
    msg = f"{action}:{ts}".encode()
    sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
    qs = urllib.parse.urlencode({"ts": ts, "t": sig})
    return f"{base}/{action}?{qs}"


def _toggle_url(in_ptown: bool) -> str:
    """One-tap toggle URL for the IN_PTOWN flag (existing behavior).

    When WORKER_URL + TOGGLE_SECRET are configured, returns a signed
    Cloudflare Worker URL — the recipient taps once and the Worker performs
    the GitHub commit on their behalf. The signature binds (action, ts) so
    a stale link can't be replayed after TOKEN_TTL_SECONDS (30 days, enforced
    on the Worker side); the timestamp is also passed as a query param for
    the Worker to validate.

    When the Worker isn't configured (no env), falls back to the GitHub
    create-file / delete-file UI. The button still works — just two taps
    instead of one. This means Daniel can deploy the Worker whenever, and
    until then the email keeps working with the original behavior.
    """
    action = "leave" if in_ptown else "arrive"
    signed = _signed_action_url(action)
    return signed or _gh_ui_toggle_url(in_ptown)


def _toggle_label(in_ptown: bool) -> str:
    """Verb tense matches the user's NEXT action, not their current state."""
    return "✈️ I'm leaving Ptown" if in_ptown else "🏠 I'm in Ptown now"


# ---------- control-action buttons (in addition to the in/away toggle) ----------
# Each entry: (action key, button label, plain-text caption).
# The action keys MUST match the Worker's ACTIONS table AND the choices in
# .github/workflows/control.yml's `inputs.action`. If you rename one, rename
# all three.
CONTROL_BUTTONS = [
    ("away_all",       "✈️ All away",            "everything → away/eco/freeze"),
    ("tub_104",        "🛁 Tub → 104°F",         "heat the tub for use"),
    ("nest_off_eco",   "🌡️ Thermostats off eco", "exit eco, keep last setpoint"),
    ("master_bath_72", "🦶 Master bath → 72°F",  "warm up the master bath floor"),
    ("garage_close",   "🚪 Close garage",        "close the garage door (close-only)"),
    ("awnings_close",  "⛱️ Awnings in",          "retract all awnings"),
    ("awnings_open",   "🏖️ Awnings out",         "extend all awnings"),
]


def _control_action_urls() -> list[tuple[str, str, str, str]]:
    """Return [(action_key, label, caption, url_or_empty)] for the email.

    URL is empty string when the Worker isn't configured — the email will
    show a disabled-looking note instead of a dead button. We deliberately
    don't fall back to a GH-UI URL pattern for control actions: there's no
    web flow that's equivalent to "dispatch this workflow with this input."
    """
    out = []
    for key, label, caption in CONTROL_BUTTONS:
        out.append((key, label, caption, _signed_action_url(key)))
    return out


# ---------- setpoint flattening + burst detection ----------
def _extract_setpoints(dashboard: dict) -> dict:
    """Flatten dashboard setpoints to {'<system>:<device>': setpoint_f}."""
    out: dict = {}
    for sys_result in dashboard.get("systems", []):
        system = sys_result.get("system", "?")
        for dev in sys_result.get("devices", []) or []:
            setp = dev.get("setpoint_f")
            if isinstance(setp, (int, float)):
                out[f"{system}:{dev.get('name', '?')}"] = round(setp, 1)
    return out


def _detect_setpoint_burst(current: dict, previous: dict | None) -> list[str]:
    """Return human-readable change strings if MIN_BURST_DEVICES+ setpoints
    moved by more than BURST_DELTA_F since the previous run. Returns [] if
    no prior state, no qualifying changes, or fewer than the threshold."""
    if not previous:
        return []
    changes: list[str] = []
    for key, cur in current.items():
        prev = previous.get(key)
        if not isinstance(prev, (int, float)):
            continue
        if abs(cur - prev) > BURST_DELTA_F:
            # "system:Cabana 60.0→64.0" → friendlier "Cabana 60→64°F"
            label = key.split(":", 1)[-1]
            changes.append(f"{label} {prev:.0f}→{cur:.0f}°F")
    if len(changes) < MIN_BURST_DEVICES:
        return []
    return changes


# ---------- stale-state computation ----------
def _parse_iso(ts: str | None) -> float | None:
    """Parse the ISO8601 stamp we write to the state file → epoch seconds."""
    if not ts:
        return None
    try:
        # Tolerate both "2026-05-02T22:00:00+0000" (no colon) and
        # "2026-05-02T22:00:00+00:00" by inserting the colon if needed.
        normalized = ts
        if len(ts) >= 5 and (ts[-5] in "+-") and ts[-3] != ":":
            normalized = ts[:-2] + ":" + ts[-2:]
        return time.mktime(time.strptime(normalized[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return None


def _stale_days(in_ptown_changed_at: str | None) -> int:
    """How many whole days since IN_PTOWN was last flipped. 0 if unknown."""
    epoch = _parse_iso(in_ptown_changed_at)
    if epoch is None:
        return 0
    seconds = max(0.0, time.time() - epoch)
    return int(seconds // 86400)


# ---------- state tracking (for --on-change-only) ----------
def _current_state(dashboard: dict) -> dict:
    """Extract just the status bits worth comparing across runs.

    Stays focused on STATUS for change-detection (`_state_differs`); the
    setpoint snapshot and in_ptown flag are tracked separately in the state
    file so they never gate "should I email." Only a status transition
    triggers an email — burst/stale nudges are passive additions to the body.
    """
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


def _save_state(state: dict, *, in_ptown: bool, in_ptown_changed_at: str,
                setpoints: dict) -> None:
    """Persist current state + bookkeeping fields used by the next run.
    Bookkeeping (setpoints, in_ptown, in_ptown_changed_at) lives alongside
    the status block; only the status block participates in change-detection.
    """
    state_with_ts = {
        **state,
        "in_ptown": in_ptown,
        "in_ptown_changed_at": in_ptown_changed_at,
        "setpoints": setpoints,
        "last_updated": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
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

    # ---------- compute "did you flip the toggle?" context ----------
    # in_ptown comes from the dashboard JSON (dashboard.py reads the IN_PTOWN
    # file inside the runner). We compare against the previous run's saved
    # value to track when the toggle last flipped, which feeds the stale-state
    # reminder. The setpoint snapshot drives burst detection.
    in_ptown = bool(dashboard.get("in_ptown", False))
    current_setpoints = _extract_setpoints(dashboard)
    previous_state = _load_previous_state()
    prev_in_ptown = previous_state.get("in_ptown") if previous_state else None
    prev_changed_at = previous_state.get("in_ptown_changed_at") if previous_state else None
    if prev_in_ptown is None or prev_in_ptown != in_ptown:
        # Either first ever run, or the toggle flipped since last run.
        in_ptown_changed_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    else:
        in_ptown_changed_at = prev_changed_at or time.strftime("%Y-%m-%dT%H:%M:%S%z")
    prev_setpoints = (previous_state or {}).get("setpoints", {})
    burst_changes = _detect_setpoint_burst(current_setpoints, prev_setpoints)
    # Burst nudge only fires when we're flagged "away" — at-the-house setpoint
    # changes are normal and shouldn't pester. The whole point is catching
    # the arrival case where Daniel forgot to flip the toggle.
    burst_nudge = bool(burst_changes) and not in_ptown
    # Stale reminder only on daily emails (the morning reassurance email is
    # the right place — hourly emails are noisier and not a calm context for
    # a "still in/away from Ptown?" prompt).
    stale_days = _stale_days(in_ptown_changed_at)
    stale_nudge = args.daily and stale_days >= STALE_REMINDER_DAYS

    ctx = {
        "in_ptown": in_ptown,
        "toggle_url": _toggle_url(in_ptown),
        "toggle_label": _toggle_label(in_ptown),
        "burst_changes": burst_changes,
        "burst_nudge": burst_nudge,
        "stale_days": stale_days,
        "stale_nudge": stale_nudge,
        # Control buttons (away_all + 3 warm-up buttons). Each entry is
        # (action_key, label, caption, signed_url_or_empty). Empty URL means
        # the Worker isn't configured — the email still renders but with
        # disabled-looking buttons. Re-evaluated every email so each link
        # carries a fresh timestamp / signature inside its 30-day TTL.
        "control_actions": _control_action_urls(),
    }

    # --on-change-only: suppress anything that matches prior state.
    if args.on_change_only:
        current_state = _current_state(dashboard)
        changed = _state_differs(current_state, previous_state)
        # Always persist current state, even if we won't send — this keeps the
        # state file fresh and lets us detect future changes correctly.
        _save_state(
            current_state,
            in_ptown=in_ptown,
            in_ptown_changed_at=in_ptown_changed_at,
            setpoints=current_setpoints,
        )
        if not changed:
            print(f"Status unchanged from previous run ({overall}); no email sent.")
            return 0
        print(f"Status changed (prev={previous_state}, now={current_state}); sending.")

    # Suppress non-daily OK runs (unless --on-change-only already decided to send).
    elif overall == "ok" and not args.daily:
        print("Status OK and --daily not set; suppressing email.")
        return 0

    subject = _build_subject(dashboard, is_daily=args.daily)
    body = _build_body(dashboard, ctx=ctx)
    # HTML body mirrors the web dashboard's card layout. Same DASHBOARD_URL
    # resolution as the text body so the "View live dashboard" link points
    # to the same place in both.
    dashboard_url = (os.environ.get("DASHBOARD_URL") or DEFAULT_DASHBOARD_URL).strip()
    html_body = _build_html_body(dashboard, dashboard_url, ctx=ctx)
    try:
        _send_resend(api_key, from_addr, to_addr, subject, body, html_body=html_body)
    except Exception as e:
        print(f"Send failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
