# Ptown Monitor

Small status-check scripts for the vacation home. Each script talks to one
vendor's cloud API and prints the current state of the devices in that system.

- `nuheat.py` — heated floors (Nuheat Signature thermostats) ✅
- `hottub.py` — Jacuzzi hot tub via SmartTub cloud ✅
- `nest.py` — Google Nest thermostats (Smart Device Management API) ✅
- `garage.py` — Overhead Door garage via OHD Anywhere → SmartThings ✅
- `lock.py` — Yale front door lock via Yale Access → SmartThings ✅
- `caseta.py` — Lutron Caseta lighting via Caseta Bridge → SmartThings ✅
- `awnings.py` — Somfy awnings via TaHoma → SmartThings (control-only; RTS
  motors have no status readback; `tahoma.py` is the superseded direct-API
  attempt, kept as a post-mortem) ✅
- `tv.py` — Samsung Frame TVs via SmartThings (on/off + art mode status,
  off control; dormant until the TVs are linked to the account) ✅
- `all.py` — runs all monitored systems in parallel and prints a
  combined status ✅

## One-time setup

1. Copy the template and lock permissions:

       cd /path/to/ptown-monitor     # whatever the real path is
       cp .env.template .env
       chmod 600 .env

2. Open `.env` in your editor and fill in credentials as you're ready. You
   can start with just Nuheat and add SmartTub / Nest later — each device
   script is independent.

## Running a check

Use the `./ptown` wrapper — it sets up a Python virtualenv on first run and
installs any needed packages:

    ./ptown nuheat        # heated floors
    ./ptown hottub        # hot tub
    ./ptown nest          # Nest thermostats
    ./ptown garage        # Overhead Door garage (via SmartThings)
    ./ptown lock          # Yale front door lock (via SmartThings)
    ./ptown caseta        # Lutron Caseta lights (via SmartThings)
    ./ptown all           # everything in parallel (recommended daily check)

Each command supports `--raw` for debugging if output looks wrong:

    ./ptown hottub --raw

Example output:

    Nuheat heated floors — 3 thermostats:
      guest floor                  now  64.6°F   set  41.0°F   idle     online
      cabana floor                 now  58.1°F   set  41.0°F   idle     online
      primary floor                now  64.2°F   set  41.0°F   idle     online

## "In Ptown" toggle (cost-protection alerts)

The dashboard adds **warn-when-warm** rules whenever Daniel is *not* at the
house — floor setpoint > 41°F, Nest setpoint > 60°F, or hot tub water/setpoint
> 65°F all trip a WARN. Catches an unauthorized bump or a power-surge
factory-reset to 104°F. The toggle is a single file at the repo root named
`IN_PTOWN`:

- **File EXISTS**: Daniel is in Ptown → cost-protection rules **OFF** (he's
  allowed to crank the heat).
- **File ABSENT**: Daniel is away from Ptown → cost-protection rules **ON**.

Default-absent means a forgotten toggle errs on the alerting side. Toggle it
from the GitHub web UI (or via a one-tap link in any monitor email):

- **Arriving in Ptown**: "Add file" → "Create new file" → name `IN_PTOWN` →
  empty body → commit. Cost-protection rules go silent immediately.
- **Leaving Ptown**: navigate to the `IN_PTOWN` file → trash icon → commit.
  Cost-protection rules go live on the next hourly run.

The freeze-protect / pipe-risk alerts (RED if a floor < 35°F or a Nest < 40°F)
run all the time regardless of the toggle — those are about preventing damage,
not waste.

## Garage door (Overhead Door / OHD Anywhere)

Genie cut off direct API access to Aladdin Connect / OHD Anywhere in
January 2024 — the old `aladdin-connect` Python libraries no longer work.
We route through Samsung SmartThings, which Genie still supports as a
"Works With" partner.

One-time setup:

1. In the **OHD Anywhere app** → **Works With** → **Samsung SmartThings**,
   sign in to a free Samsung account and authorize the link.
2. Open the **SmartThings app**, confirm the garage door device appears
   and reads open/closed correctly. Note the device's display name.
3. Auth is the OAuth-In SmartApp shared by garage/lock/caseta (see
   `smartthings_oauth.py`; Samsung capped PATs to 24h in Dec 2024). If
   the tokens are ever fully dead, run `python3 smartthings_bootstrap.py`
   and follow the prompts — it grants `r:devices:*` + `x:devices:*`, then
   update the `SMARTTHINGS_REFRESH_TOKEN` GH secret and purge the
   `ptown-st-oauth-*` Actions caches.
4. Run `./ptown garage` once. It will auto-discover the device, print
   the ID, and tell you to add `SMARTTHINGS_DEVICE_ID=<uuid>` to `.env`
   to pin it.

Garage status feeds the dashboard: **door open while away** is a CRIT
(security, not cost). Door state while in Ptown is informational only.

`./ptown garage --discover` lists every device the token can see, useful
if more than one door-capable device shows up. `./ptown garage --close`
sends the CLOSE command (the email's "Close garage" button does the same
via control.py) — close-only by design, there is no remote open.

## Front door lock (Yale via SmartThings)

We tried the `yalexs` Python library first, but Yale migrated all individual
accounts to OAuth-only auth in late 2024 — the legacy password endpoint
returns 403 and yalexs has no OAuth support. We then discovered Yale's
SmartThings integration is alive after all (despite the consumer "Works
With" page in the Yale Access app not showing it), so we route lock state
through SmartThings just like the garage. Same PAT, same script pattern.

Setup (one-time):

1. In the Yale Access app, link the lock to SmartThings via Settings →
   Integrations → SmartThings. Authorize with your Samsung account.
2. Run `./ptown lock` once — it auto-discovers the lock-capable device on
   your SmartThings account and prints the device ID.
3. If your account has more than one lock (e.g., multiple properties),
   `./ptown lock` will list them and ask you to pin one. Set
   `SMARTTHINGS_LOCK_DEVICE_ID=<uuid>` in `.env`. Same secret name lives
   in GH Actions.

Dashboard rule: **unlocked while away** is a CRIT. Locked / locking /
unlocking are OK. Jammed or unknown surface as WARN.

## Caseta lights (Lutron via SmartThings)

40+ Caseta dimmers/switches pair to SmartThings via the Caseta Smart
Bridge's built-in SmartThings integration. `caseta.py` auto-discovers
all `switch`-capable devices each run (excluding the garage door, which
is `doorControl`) and reports on/off state per device. Dashboard
renders a compact "N of M on" summary instead of one line per device.

Dashboard rule: any Caseta device on **while away** = WARN, listed in
the email body. When in Ptown, all-on is OK.

No new credentials needed — caseta.py shares the OAuth-In SmartApp
credentials with garage and lock.

## Awnings (Somfy via TaHoma)

The awnings pair to a TaHoma hub and are **RTS motors — one-way radio with
no position feedback**. The dashboard shows an **assumed** position — the
last command sent through this system (recorded in `awnings-state.json`,
persisted via the Actions cache) — labeled "(assumed)" because the physical
wall remotes are invisible to it. The row is informational only and never
alerts; until the first button press after deploy it reads "unknown (no
command recorded)".

API path: **TaHoma → SmartThings** (`awnings.py`), riding the same
OAuth-In SmartApp as the garage and lock — zero extra credentials. Somfy's
own cloud is a dead end for third parties in North America: the Dec 2024
unified-account migration killed password login, and the app-generated
Developer Mode token only authorizes the hub's LOCAL API (unreachable from
GH Actions). `tahoma.py` documents that whole investigation and every
failed cloud path; don't re-walk it.

One-time setup: SmartThings app → Add device → Partner devices → **"Somfy
Window Treatment"** → sign in with the TaHoma account → authorize. The
awnings then show up as `windowShade` devices.

    ./ptown awnings --discover    # list windowShade devices on the account
    ./ptown awnings close         # retract all awnings
    ./ptown awnings open          # extend all awnings
    ./ptown awnings close --match deck   # subset by label

Email buttons: **Awnings in** / **Awnings out** dispatch `awnings_close` /
`awnings_open` through control.yml, same one-tap plumbing as the tub and
Nest buttons. "Success" means SmartThings accepted the command —
fire-and-forget by hardware design.

## Samsung Frame TVs (via SmartThings)

`tv.py` monitors the two Frame TVs and provides a **TVs off** button.
Samsung TVs are SmartThings-native — one-time setup is signing each TV
into the Samsung account (TV → Settings → General → System Manager →
Samsung Account) or adding them in the SmartThings app on the house WiFi.
Until then the module reports zero devices quietly (no nagging).

Dashboard rule: **TV on while away = WARN**, and **Art Mode counts as ON**
(Daniel's call — a Frame showing art still draws power and burns panel
hours). Deep-standby TVs often drop offline in SmartThings; that reads as
OK, not an alert.

Known Frame quirk to verify once linked: some firmware lands SmartThings
`switch off` in Art Mode rather than standby. If the **📺 TVs off** button
"succeeds" but the art keeps glowing, see tv.py's header before debugging.

    ./ptown tv                    # status of both TVs
    ./ptown tv --discover         # confirm the TVs joined the account
    ./ptown tv --off              # turn them off

## Notes

- `nuheat.py`, `nest.py`, `garage.py`, `lock.py`, and `caseta.py` are
  stdlib-only — they all hit cloud APIs via plain HTTPS. `hottub.py`
  uses `python-smarttub` + `aiohttp`, installed automatically into
  `./.venv` on first run.
- Credentials live in `.env` alongside the scripts; never commit that file.
- If a vendor rotates their API shape, `--raw` is the quickest way to see
  what changed.
