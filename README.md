# Ptown Monitor

Small status-check scripts for the vacation home. Each script talks to one
vendor's cloud API and prints the current state of the devices in that system.

- `nuheat.py` — heated floors (Nuheat Signature thermostats) ✅
- `hottub.py` — Jacuzzi hot tub via SmartTub cloud ✅
- `nest.py` — Google Nest thermostats (Smart Device Management API) ✅
- `all.py` — runs all three in parallel and prints a combined status ✅

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
    ./ptown all           # all three in parallel (recommended daily check)

Each command supports `--raw` for debugging if output looks wrong:

    ./ptown hottub --raw

Example output:

    Nuheat heated floors — 3 thermostats:
      guest floor                  now  64.6°F   set  41.0°F   idle     online
      cabana floor                 now  58.1°F   set  41.0°F   idle     online
      primary floor                now  64.2°F   set  41.0°F   idle     online

## Notes

- `nuheat.py` is stdlib-only. `hottub.py` uses `python-smarttub` +
  `aiohttp`, installed automatically into `./.venv` on first run.
- Credentials live in `.env` alongside the scripts; never commit that file.
- If a vendor rotates their API shape, `--raw` is the quickest way to see
  what changed.
