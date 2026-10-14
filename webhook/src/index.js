// The org webhook's receiver (Cloudflare Worker). GitHub sends one `workflow_job` event per job
// in any promptics repo; for a queued job whose runs-on carries a pool label, this starts the
// pool's scaler (pool-scale.yml) at once, which scans every org repo's queue. Repos need nothing
// but `runs-on: [self-hosted, promptics-pool]`.
//
// Secrets (wrangler secret put): WEBHOOK_SECRET, the org webhook's secret; DISPATCH_TOKEN, a
// token allowed to dispatch workflows in promptics/promptics-buildserver (Actions: write).

const POOLS = ["promptics-pool"];
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

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("promptics pool webhook\n");
    const body = await request.text();
    if (!(await signatureOk(env.WEBHOOK_SECRET, body, request.headers.get("X-Hub-Signature-256")))) {
      return new Response("bad signature\n", { status: 401 });
    }
    const event = request.headers.get("X-GitHub-Event");
    if (event === "ping") return new Response("pong\n");
    if (event !== "workflow_job") return new Response("ignored\n");

    const payload = JSON.parse(body);
    const labels = payload.workflow_job?.labels ?? [];
    const pool = POOLS.find((p) => labels.includes(p));
    if (payload.action !== "queued" || !pool) return new Response("not for a pool\n");

    const repo = payload.repository?.name ?? "";
    const resp = await fetch(SCALER, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.DISPATCH_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "promptics-pool-webhook",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ ref: "main" }),
    });
    const text = resp.ok ? "" : await resp.text();
    console.log(`queued ${repo} job ${payload.workflow_job?.id} for ${pool}: dispatch ${resp.status} ${text}`);
    return new Response(`dispatched ${resp.status}\n`, { status: resp.ok ? 202 : 502 });
  },
};
