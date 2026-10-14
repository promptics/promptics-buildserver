# promptics-buildserver

Self-hosted GitHub Actions runners on Hetzner Cloud that scale to zero. A server is created when
a job queues for the pool's label, takes further jobs while its billing hour is paid, and deletes
itself when idle in the last minutes of that hour.

A repository of the organisation uses the pool with one line and no other setup:

```yaml
jobs:
  test:
    runs-on: [self-hosted, promptics-pool]
```

## Layout

| Path | What |
|---|---|
| `scaler/pool.py` | The controller: counts queued jobs and idle runners, creates servers, sweeps what a reaper missed |
| `scaler/bake.sh` | The server toolchain, baked into a snapshot so a server boots in about a minute |
| `scaler/runner-boot.sh` | First boot: registers the runners and arms the reaper |
| `scaler/reaper.sh` | On the server, every minute: deletes the server when idle at the end of its billing hour |
| `webhook/` | A Cloudflare Worker that receives the organisation's webhook: starts the scaler at once for a queued pool job, and optionally wakes a configured controller workflow when an issue becomes ready or a pull request is merged |
| `.github/workflows/pool-scale.yml` | Runs the controller (from the webhook; the schedule is a backstop) |
| `.github/workflows/pool-bake.yml` | Bakes the snapshot weekly |
| `docs/design.md` | The lease rule and why it looks like this |

## Setup

1. A Hetzner Cloud project of its own for the pool, and a read/write API token for it. The
   servers carry that token, so it must reach nothing but the pool's servers.
2. Repository secrets: `HCLOUD_TOKEN_CI` (that token) and `RUNNER_PAT` (a classic PAT with
   `admin:org` to register and remove organisation runners, and `repo` to read queued jobs of
   private repositories).
3. Run `pool bake` once; afterwards it runs weekly.
4. Deploy the Worker (`cd webhook && npx wrangler@4 deploy`, secrets `WEBHOOK_SECRET` and
   `DISPATCH_TOKEN` via `wrangler secret put`) and point an organisation webhook for
   `workflow_job` events at it.

Tests: `python3 -m unittest discover -s tests`.
