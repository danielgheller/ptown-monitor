# One-tap toggle setup (Cloudflare Worker)

This deploys a tiny Cloudflare Worker that turns the email toggle button
into a true one-tap action: tap the button → Worker calls the GitHub API
to create or delete `IN_PTOWN` → next workflow run picks up the change.

Total time: ~30 minutes, mostly GitHub PAT and Cloudflare account setup.
You only do this once.

You'll create three secrets along the way:

1. **GitHub fine-grained PAT** — lets the Worker commit `IN_PTOWN` flips on
   your repo. Stored as a Worker secret.
2. **TOGGLE_SECRET** — a random string that signs the email links so a
   stranger can't fire your toggle. Same value goes in the Worker AND in
   the repo's GitHub Actions Secrets so `notify.py` can sign matching links.
3. **WORKER_URL** — the URL Cloudflare gives you after deploy
   (`https://ptown-toggle.<your-subdomain>.workers.dev`). Stored as a GitHub
   Actions Secret so `notify.py` knows where to point the button.

---

## Step 1 — Make a fine-grained GitHub PAT

1. Go to https://github.com/settings/personal-access-tokens/new
2. Token name: `ptown-toggle-worker`
3. Expiration: 1 year (longest available)
4. Repository access: **Only select repositories** → `danielgheller/ptown-monitor`
5. Permissions → Repository permissions → **Contents: Read and write**
6. Generate, copy the token (starts with `github_pat_…`). Don't lose it —
   GitHub only shows it once.

## Step 2 — Generate a TOGGLE_SECRET

Any random string works. From a terminal:

    openssl rand -hex 32

Copy the output. You'll paste it in two places (Worker + GitHub Actions
Secrets) — they must match exactly.

## Step 3 — Sign up for Cloudflare and install Wrangler

1. https://dash.cloudflare.com → Sign up (free).
2. In a terminal:

       npm install -g wrangler
       wrangler login

   `wrangler login` opens a browser tab and asks you to authorize.

## Step 4 — Deploy the Worker

From the repo root:

    cd cloudflare-worker
    wrangler deploy

The first deploy prompts you to pick a workers.dev subdomain — just accept
the default. After deploy completes, Wrangler prints the Worker URL:

    https://ptown-toggle.<your-subdomain>.workers.dev

**Copy this URL** — that's your `WORKER_URL`.

## Step 5 — Set the Worker secrets

Still in `cloudflare-worker/`:

    wrangler secret put GITHUB_TOKEN
    # (paste the github_pat_… token from Step 1, press Enter)

    wrangler secret put TOGGLE_SECRET
    # (paste the random string from Step 2, press Enter)

## Step 6 — Add the same secrets to GitHub Actions

Go to https://github.com/danielgheller/ptown-monitor/settings/secrets/actions
and create two new repository secrets:

- `WORKER_URL` → the workers.dev URL from Step 4
- `TOGGLE_SECRET` → the same random string from Step 2

(Note: the `GITHUB_TOKEN` from Step 1 only goes on the Worker side. The
GitHub Actions runner doesn't need it — it has its own auth.)

## Step 7 — Verify it works

The next hourly email (or trigger one manually with the "Run workflow"
button on the Hourly status check action) should contain a button whose
URL points at your Worker, not at github.com. Tap it. You should land on
a small "🏠 You're in Ptown" or "✈️ You've left Ptown" confirmation page.

A few seconds later, GitHub Actions runs the workflow because IN_PTOWN
changed (the `push: paths: [IN_PTOWN]` trigger we added). Within a minute
or two, the next email shows the new state in the top-of-email panel.

## Updating the Worker

Edit `worker.js`, then:

    cd cloudflare-worker
    wrangler deploy

Logs and request history are at https://dash.cloudflare.com/?to=/:account/workers .

## Rotating secrets

If `TOGGLE_SECRET` is ever exposed (e.g. in a screenshot), rotate both
ends in this order:

1. `wrangler secret put TOGGLE_SECRET` with a new random string
2. Update `TOGGLE_SECRET` in GitHub Actions Secrets to match

Old email links will start returning "link expired" until you receive
a new email signed with the new secret.

If `GITHUB_TOKEN` is exposed, revoke it at
https://github.com/settings/personal-access-tokens, generate a new one
(Step 1), and `wrangler secret put GITHUB_TOKEN` to update the Worker.
