# Buttons — where you can press things

Every control action is a URL on the Cloudflare Worker. Anything that can
hit a URL is a button. Two auth modes:

| Mode | Query params | Expires | Used by |
|---|---|---|---|
| Signed link | `?ts=<epoch>&t=<hmac>` | 30 days | Email buttons (notify.py builds these) |
| Personal token | `?k=<PERSONAL_TOKEN>` | Never (revoke by rotating the secret) | iOS Shortcuts, physical buttons |

## Actions

| Path | What it does | IN_PTOWN |
|---|---|---|
| `/arrive_summer` | Tub → 102°F READY; Nests → COOL 70°F, Cabana 73°F | → in |
| `/arrive_winter` | Tub → 104°F READY; Nests → HEAT 69°F; master bath floor → 75°F | → in |
| `/away_all` | Tub → 65°F REST; Nests → eco; floors → 41°F; front door → locked | → away |
| `/tub_104` | Tub → 104°F READY | → in |
| `/nest_off_eco` | Nests out of eco, resume saved setpoints | → in |
| `/master_bath_72` | Master bath floor → 72°F | → in |
| `/garage_close` | Close garage (close-only by design) | untouched |
| `/awnings_open` `/awnings_close` | Extend / retract all awnings | untouched |
| `/tvs_off` | All Samsung TVs off | untouched |

Arrival presets deliberately don't touch awnings or lights — those should
already be closed/off from the last departure.

## One-time setup

1. Generate a token: `openssl rand -hex 24`
2. Store it on the Worker: `wrangler secret put PERSONAL_TOKEN` (paste token)
3. Redeploy: `wrangler deploy` (from `cloudflare-worker/`)
4. Your button URL template: `https://<worker-url>/<action>?k=<token>`

Test in Safari first: paste a full URL — you should get the confirmation
page and see a control.yml run appear in the Actions tab.

## iOS Shortcuts

For each action you want (arrive_summer, arrive_winter, away_all are the
big three):

1. Shortcuts app → **+** → add action **Get Contents of URL**
2. Paste the button URL. Method GET (default).
3. Rename the shortcut (e.g. "Arrive — Summer"), pick an icon/color.
4. Share sheet → **Add to Home Screen** for a one-tap icon.

Free extras once the shortcut exists: run it from Apple Watch, CarPlay,
the Action Button, or by voice ("Hey Siri, Arrive Summer"). All manual —
no location automation by choice.

## Physical button

**Flic 2 + Flic Hub LR** is the fit: the hub speaks HTTP directly, so the
button works with no phone in the house. Per button you get three
gestures — a natural mapping:

- **Single press** → `/arrive_summer` (swap to winter in November)
- **Double press** → `/tub_104`
- **Hold** → `/away_all`

Configure in the Flic app: button → gesture → **Internet Request** → paste
the URL, method GET. Mount it by the door you actually leave through.

Cheaper alternative if a phone-tap is acceptable: NFC tag stickers +
Shortcuts automation (tap phone on tag by the door → runs the shortcut).

## Keeping action keys in sync

Renaming or adding an action means touching **four** places:
`control.py` ACTIONS, `control.yml` inputs.action choices, `worker.js`
ACTIONS, `notify.py` CONTROL_BUTTONS — plus any Shortcut/Flic URLs that
reference the old path.
