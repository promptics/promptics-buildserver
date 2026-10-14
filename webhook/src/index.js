// The org webhook's receiver (Cloudflare Worker). GitHub sends one `workflow_job` event per job
// in any promptics repo; for a queued job whose runs-on carries a pool label, this starts the
// pool's scaler (pool-scale.yml) at once, which scans every org repo's queue. Repos need nothing
// but `runs-on: [self-hosted, promptics-pool]`.
//
// It also wakes one more workflow, a work controller, when an issue becomes ready
// (labelled READY_LABEL) or a pull request is merged, so queued work starts without waiting for
// that workflow's schedule. Which workflow is configuration, not code: CONTROLLER_REPO
// ("owner/name") and CONTROLLER_WORKFLOW (file name). Without them that part is off.
//
// Secrets (wrangler secret put): WEBHOOK_SECRET, the org webhook's secret; DISPATCH_TOKEN, a
// token allowed to dispatch workflows (Actions: write) in this repository and, when configured,
// in CONTROLLER_REPO; CONTROLLER_REPO and CONTROLLER_WORKFLOW (optional).

const POOLS = ["promptics-pool"];
const READY_LABEL = "status:agent-ready";
const SCALER = "https://api.github.com/repos/promptics/promptics-buildserver/actions/workflows/pool-scale.yml/dispatches";

async function signatureOk(secret, body, header) {
  if (!header || !header.startsWith("sha256=")) return false;
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const mac = new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body)));
  const hex = [...mac].map((b) => b.toString(16).padStart(2, "0")).join("");
  const sent = header.slice("sha256=".length);
  if (sent.length !== hex.length) return false;
  let diff = 0;
  for (let i = 0; i < hex.length; i++) diff |= hex.charCodeAt(i) ^ sent.charCodeAt(i);
  return diff === 0;
}

async function dispatch(url, token) {
  return fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "User-Agent": "promptics-pool-webhook",
      "X-GitHub-Api-Version": "2022-11-28",
    },
    body: JSON.stringify({ ref: "main" }),
  });
}

// An issue labelled ready, or a merged pull request: either can make queued work placeable.
async function wakeController(event, payload, env) {
  if (!env.CONTROLLER_REPO || !env.CONTROLLER_WORKFLOW) return new Response("no controller configured\n");
  const ready = event === "issues" && payload.action === "labeled" && payload.label?.name === READY_LABEL;
  const merged = event === "pull_request" && payload.action === "closed" && payload.pull_request?.merged === true;
  if (!ready && !merged) return new Response("not for the controller\n");
  const url = `https://api.github.com/repos/${env.CONTROLLER_REPO}/actions/workflows/${env.CONTROLLER_WORKFLOW}/dispatches`;
  const resp = await dispatch(url, env.DISPATCH_TOKEN);
  console.log(`${ready ? "ready" : "merged"} in ${payload.repository?.name ?? ""}: controller dispatch ${resp.status}`);
  return new Response(`controller ${resp.status}\n`, { status: resp.ok ? 202 : 502 });
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("promptics pool webhook\n");
    const body = await request.text();
    if (!(await signatureOk(env.WEBHOOK_SECRET, body, request.headers.get("X-Hub-Signature-256")))) {
      return new Response("bad signature\n", { status: 401 });
    }
    const event = request.headers.get("X-GitHub-Event");
    if (event === "ping") return new Response("pong\n");
    const payload = JSON.parse(body);
    if (event === "issues" || event === "pull_request") return wakeController(event, payload, env);
    if (event !== "workflow_job") return new Response("ignored\n");

    const labels = payload.workflow_job?.labels ?? [];
    const pool = POOLS.find((p) => labels.includes(p));
    if (payload.action !== "queued" || !pool) return new Response("not for a pool\n");

    const repo = payload.repository?.name ?? "";
    const resp = await dispatch(SCALER, env.DISPATCH_TOKEN);
    const text = resp.ok ? "" : await resp.text();
    console.log(`queued ${repo} job ${payload.workflow_job?.id} for ${pool}: dispatch ${resp.status} ${text}`);
    return new Response(`dispatched ${resp.status}\n`, { status: resp.ok ? 202 : 502 });
  },
};
