// Ptown Monitor — toggle + control Cloudflare Worker.
//
// Two families of one-tap email endpoints:
//
// 1. Location-toggle (existing): /arrive and /leave create or delete the
//    IN_PTOWN flag file. The next hourly run picks up the new state — and
//    we configure hourly.yml to trigger on push of IN_PTOWN, so the change
//    takes effect within ~30s rather than waiting for the next :15 cron.
//
// 2. Device-control (new): /away_all, /tub_104, /nest_off_eco,
//    /master_bath_72 dispatch the .github/workflows/control.yml workflow
//    with the matching action name. Each button has an implied IN_PTOWN
//    side effect — "all away" flips you out, the three warm-up buttons
//    flip you in — so we update IN_PTOWN inline before dispatching.
//
// Secrets (set via `wrangler secret put` or the dashboard):
//   GITHUB_TOKEN   - fine-grained PAT, contents:write AND actions:write
//                    on danielgheller/ptown-monitor only.
//   TOGGLE_SECRET  - shared secret with notify.py for HMAC signing.
//
// Token format (notify.py builds these, worker validates):
//   t  = HMAC-SHA256(TOGGLE_SECRET, "<action>:<ts>")
//   ts = unix epoch seconds (integer)
// Tokens expire after TOKEN_TTL_SECONDS so a stale email link can't be
// fired by an email scanner months later.

const GH_OWNER = "danielgheller";
const GH_REPO = "ptown-monitor";
const IN_PTOWN_FILE = "IN_PTOWN";
const CONTROL_WORKFLOW = "control.yml";
const TOKEN_TTL_SECONDS = 30 * 86400; // 30 days

// File body when creating IN_PTOWN. Keep it self-documenting so anyone
// browsing the repo (including Daniel six months from now) understands
// what the presence of this file means.
const IN_PTOWN_BODY =
  "# IN_PTOWN\n\n" +
  "Daniel is at the house. Cost-protection alerts are SUPPRESSED while\n" +
  "this file exists. To leave, tap the \"I'm leaving Ptown\" button in any\n" +
  "monitor email -- or delete this file from the GitHub UI.\n";

// Action registry. The keys here must match the URL path AND (for the
// control actions) the workflow_dispatch input value AND notify.py's
// signing message. Drift between any two of those three breaks one-tap
// silently — keep them in sync.
//
//   inPtownEffect  "create"  → ensure IN_PTOWN exists before dispatching
//                  "delete"  → ensure IN_PTOWN absent before dispatching
//                  "none"    → don't touch IN_PTOWN
//   workflowAction null      → no workflow dispatch (just IN_PTOWN flip,
//                              i.e. the original /arrive and /leave)
//                  "<name>"  → dispatch control.yml with this action input
const ACTIONS = {
  arrive:         { inPtownEffect: "create", workflowAction: null,             confirm: "🏠 You're in Ptown. Cost-protection is now OFF — adjust everything however you like." },
  leave:          { inPtownEffect: "delete", workflowAction: null,             confirm: "✈️ You've left Ptown. Cost-protection is now ON — you'll get a WARN email if any device is bumped above baseline." },
  away_all:       { inPtownEffect: "delete", workflowAction: "away_all",       confirm: "✈️ All-away applied. Tub → 65°F, Nest → eco, floors → 41°F. Cost-protection is now ON. Devices update over the next minute or two." },
  tub_104:        { inPtownEffect: "create", workflowAction: "tub_104",        confirm: "🛁 Heating the tub to 104°F. You're flagged in-Ptown so the cost-protection WARN won't fire. Allow ~30 min to reach temp." },
  nest_off_eco:   { inPtownEffect: "create", workflowAction: "nest_off_eco",   confirm: "🌡️ Taking all 3 Nest thermostats out of eco mode. They'll resume their last HEAT setpoints. You're flagged in-Ptown." },
  master_bath_72: { inPtownEffect: "create", workflowAction: "master_bath_72", confirm: "🦶 Master bath floor → 72°F. You're flagged in-Ptown. The floor takes a while to feel warm — check back in 20-30 min." },
};

// Base64-encode a string as UTF-8 bytes. The naive `btoa(str)` only handles
// Latin-1 input (any code point > 0xFF throws), so a sneaky character like
// an em-dash ('—', U+2014) breaks the GitHub Contents API call. Going
// through TextEncoder + a per-byte string fixes it for any Unicode.
function utf8ToBase64(str) {
  const bytes = new TextEncoder().encode(str);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const action = url.pathname.replace(/^\/+/, "").toLowerCase();

    if (action === "" || action === "favicon.ico") {
      return htmlResponse(
        "Ptown toggle endpoint. Tap the button in your monitor email.",
        200
      );
    }
    const spec = ACTIONS[action];
    if (!spec) {
      return htmlResponse(`Unknown action: ${action}`, 404);
    }

    // Validate token (timestamp + HMAC). Same scheme as before — every
    // action shares the signing format so notify.py only has one path to
    // build URLs. Catches stale email-scanner replays (TTL) and tampering.
    const ts = url.searchParams.get("ts");
    const t = url.searchParams.get("t");
    if (!ts || !t) {
      return expiredResponse(action, "missing token — link is malformed");
    }
    const tsNum = parseInt(ts, 10);
    if (!Number.isFinite(tsNum)) {
      return expiredResponse(action, "bad timestamp");
    }
    const now = Math.floor(Date.now() / 1000);
    if (Math.abs(now - tsNum) > TOKEN_TTL_SECONDS) {
      return expiredResponse(action, "this email link is older than 30 days");
    }
    const expected = await hmacHex(env.TOGGLE_SECRET, `${action}:${ts}`);
    if (!constantTimeEqual(t, expected)) {
      return expiredResponse(action, "signature didn't validate");
    }

    // Apply the IN_PTOWN side effect FIRST, then dispatch the workflow.
    // This ordering matters: hourly.yml has `push: paths: [IN_PTOWN]`, so a
    // flag flip kicks off a status email almost immediately. We want that
    // email to reflect Daniel's intent (in/away) right away, even before
    // control.yml finishes nudging the devices.
    try {
      if (spec.inPtownEffect === "create") {
        await ghCreateFile(env.GITHUB_TOKEN);
      } else if (spec.inPtownEffect === "delete") {
        await ghDeleteFile(env.GITHUB_TOKEN);
      }
    } catch (e) {
      return expiredResponse(action, `IN_PTOWN flip failed: ${e.message}`);
    }

    // Dispatch the control workflow if this action has one.
    if (spec.workflowAction) {
      try {
        await ghDispatchWorkflow(env.GITHUB_TOKEN, spec.workflowAction);
      } catch (e) {
        // Surface the dispatch failure but don't unwind the IN_PTOWN flip
        // — Daniel's intent is captured, the next hourly run will see it,
        // and the device commands can be retried by tapping the button
        // again or by re-running the workflow from the GH UI.
        return expiredResponse(action, `Workflow dispatch failed: ${e.message}`);
      }
    }

    return htmlResponse(spec.confirm);
  },
};

// ---------- GitHub Contents API (IN_PTOWN flag) ----------
async function ghCreateFile(token) {
  const url = `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/contents/${IN_PTOWN_FILE}`;
  // GET first — if the file already exists, the action is a no-op (Daniel
  // already flagged "in Ptown" via a previous tap or git push). Idempotent.
  const head = await fetch(url, { headers: ghHeaders(token) });
  if (head.status === 200) return;
  if (head.status !== 404) {
    throw new Error(`GET ${head.status}: ${await head.text().then(s => s.slice(0, 200))}`);
  }
  const body = JSON.stringify({
    message: "I'm in Ptown",
    content: utf8ToBase64(IN_PTOWN_BODY),
    branch: "main",
  });
  const resp = await fetch(url, {
    method: "PUT",
    headers: { ...ghHeaders(token), "Content-Type": "application/json" },
    body,
  });
  if (!resp.ok) {
    throw new Error(`PUT ${resp.status}: ${await resp.text().then(s => s.slice(0, 200))}`);
  }
}

async function ghDeleteFile(token) {
  const url = `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/contents/${IN_PTOWN_FILE}`;
  // GET to obtain the current SHA (required by the delete endpoint) and
  // also to short-circuit when the file is already gone.
  const head = await fetch(url, { headers: ghHeaders(token) });
  if (head.status === 404) return;
  if (head.status !== 200) {
    throw new Error(`GET ${head.status}: ${await head.text().then(s => s.slice(0, 200))}`);
  }
  const data = await head.json();
  const body = JSON.stringify({
    message: "I'm leaving Ptown",
    sha: data.sha,
    branch: "main",
  });
  const resp = await fetch(url, {
    method: "DELETE",
    headers: { ...ghHeaders(token), "Content-Type": "application/json" },
    body,
  });
  if (!resp.ok) {
    throw new Error(`DELETE ${resp.status}: ${await resp.text().then(s => s.slice(0, 200))}`);
  }
}

// ---------- GitHub Actions API (control.yml dispatch) ----------
async function ghDispatchWorkflow(token, actionInput) {
  // workflow_dispatch endpoint returns 204 No Content on success. The
  // workflow itself runs asynchronously — we don't (and can't) wait for it
  // here. Daniel will see the result either via control.yml run history in
  // the GH Actions UI, or — if the run fails — via the standard GH "your
  // workflow run failed" email that GitHub sends automatically.
  const url =
    `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}` +
    `/actions/workflows/${CONTROL_WORKFLOW}/dispatches`;
  const body = JSON.stringify({
    ref: "main",
    inputs: { action: actionInput },
  });
  const resp = await fetch(url, {
    method: "POST",
    headers: { ...ghHeaders(token), "Content-Type": "application/json" },
    body,
  });
  if (!resp.ok) {
    throw new Error(
      `dispatch ${resp.status}: ${await resp.text().then(s => s.slice(0, 200))}`
    );
  }
}

function ghHeaders(token) {
  return {
    Authorization: `Bearer ${token}`,
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "ptown-toggle-worker",
  };
}

// ---------- HMAC + crypto helpers ----------
async function hmacHex(secret, message) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw",
    enc.encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(message));
  return [...new Uint8Array(sig)]
    .map(b => b.toString(16).padStart(2, "0"))
    .join("");
}

function constantTimeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) {
    return false;
  }
  let result = 0;
  for (let i = 0; i < a.length; i++) {
    result |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return result === 0;
}

// ---------- HTML responses ----------
// Two kinds: success (green-ish) and expired/error (yellow-ish, with a
// fallback button linking to the GitHub UI flow so Daniel can still flip
// the file even if his email link is dead).
function htmlResponse(message, status = 200) {
  return new Response(pageShell({ heading: "Ptown Monitor", body: escapeHtml(message) }), {
    status,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}

function expiredResponse(action, reason) {
  // For the IN_PTOWN-only actions (arrive/leave), we can offer a GitHub
  // web-UI fallback that still works. For the device-control actions
  // there's no equivalent UI fallback — the user's only recourse is to
  // re-trigger from a fresh email or run the workflow manually from the
  // Actions tab — so we just explain that.
  let body;
  if (action === "arrive" || action === "leave") {
    const fallback =
      action === "arrive"
        ? `https://github.com/${GH_OWNER}/${GH_REPO}/new/main?filename=${IN_PTOWN_FILE}`
        : `https://github.com/${GH_OWNER}/${GH_REPO}/delete/main/${IN_PTOWN_FILE}`;
    const verb = action === "arrive" ? "🏠 In Ptown" : "✈️ Leaving Ptown";
    body =
      `<p style="margin:0 0 12px 0;">This link can't run automatically (${escapeHtml(reason)}).</p>` +
      `<p style="margin:0 0 16px 0;">You can still flip the toggle by hand:</p>` +
      `<a href="${escapeAttr(fallback)}" ` +
      `style="display:inline-block;background:#111827;color:#fff;text-decoration:none;` +
      `font-size:14px;font-weight:600;padding:10px 16px;border-radius:8px;">` +
      `${escapeHtml(verb)} →</a>`;
  } else {
    const runsUrl = `https://github.com/${GH_OWNER}/${GH_REPO}/actions/workflows/${CONTROL_WORKFLOW}`;
    body =
      `<p style="margin:0 0 12px 0;">This link couldn't run (${escapeHtml(reason)}).</p>` +
      `<p style="margin:0 0 16px 0;">You can run the action manually from the Actions tab:</p>` +
      `<a href="${escapeAttr(runsUrl)}" ` +
      `style="display:inline-block;background:#111827;color:#fff;text-decoration:none;` +
      `font-size:14px;font-weight:600;padding:10px 16px;border-radius:8px;">` +
      `Open Actions tab →</a>`;
  }
  return new Response(pageShell({ heading: "Link expired", body }), {
    status: 410,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}

function pageShell({ heading, body }) {
  return (
    "<!DOCTYPE html><html><head><meta charset=\"utf-8\">" +
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">" +
    "<title>Ptown Monitor</title></head>" +
    "<body style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;" +
    "background:#f5f5f7;margin:0;padding:40px 20px;color:#1a1a1a;\">" +
    "<div style=\"max-width:480px;margin:0 auto;background:#fff;border-radius:12px;" +
    "padding:24px;box-shadow:0 1px 3px rgba(0,0,0,0.08);\">" +
    `<div style="font-size:18px;font-weight:600;margin-bottom:10px;">${escapeHtml(heading)}</div>` +
    `<div style="font-size:15px;color:#374151;line-height:1.5;">${body}</div>` +
    "</div></body></html>"
  );
}

function escapeHtml(s) {
  return String(s).replace(
    /[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}
function escapeAttr(s) {
  return escapeHtml(s);
}
