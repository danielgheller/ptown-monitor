# Ptown Monitor

Small status-check scripts for the vacation home. Each script talks to one
vendor's cloud API and prints the current state of the devices in that system.

- `nuheat.py` — heated floors (Nuheat Signature thermostats) ✅
- `hottub.py` — Jacuzzi hot tub via SmartTub cloud ✅
- `nest.py` — Google Nest thermostats (Smart Device Management API) ✅
- `garage.py` — Overhead Door garage via OHD Anywhere → SmartThings ✅
- `all.py` — runs all four in parallel and prints a combined status ✅

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
3. At https://account.smartthings.com/tokens, generate a Personal Access
   Token named `ptown-monitor`. Check **Devices: List all devices** and
   **Devices: See all devices**. Skip the write/control scopes — we don't
   need them for read-only monitoring.
4. Paste the token into `.env` as `SMARTTHINGS_TOKEN=...`.
5. Run `./ptown garage` once. It will auto-discover the device, print
   the ID, and tell you to add `SMARTTHINGS_DEVICE_ID=<uuid>` to `.env`
   to pin it.

Garage status feeds the dashboard: **door open while away** is a CRIT
(security, not cost). Door state while in Ptown is informational only.

`./ptown garage --discover` lists every device the token can see, useful
if more than one door-capable device shows up.

## Notes

- `nuheat.py`, `nest.py`, and `garage.py` are stdlib-only. `hottub.py`
  uses `python-smarttub` + `aiohttp`, installed automatically into
  `./.venv` on first run.
- Credentials live in `.env` alongside the scripts; never commit that file.
- If a vendor rotates their API shape, `--raw` is the quickest way to see
  what changed.
