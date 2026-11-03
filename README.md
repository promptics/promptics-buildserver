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

A job that needs what it must not share with other jobs' servers, such as a privileged container,
uses the one-job pool instead. Each of its servers takes exactly one job and deletes itself when
that job ends. It serves only scheduled and manually started runs, on the main branch, of the repositories listed in
the `POOL_PRIVILEGED_REPOS` secret; its runner refuses every other job before the first step, so
a pull request job never runs there.

```yaml
on:
  schedule:
    - cron: "17 2 * * *"
  workflow_dispatch:
jobs:
  infra-test:
    runs-on: [self-hosted, promptics-privileged]
```

## Layout

| Path | What |
|---|---|
| `scaler/pool.py` | The controller: counts queued jobs and idle runners, creates servers, sweeps what a reaper missed |
| `scaler/bake.sh` | The server toolchain, baked into a snapshot so a server boots in about a minute |
| `scaler/runner-boot.sh` | First boot: registers the runners and arms the reaper |
| `scaler/reaper.sh` | On the server, every minute: deletes the server when idle at the end of its billing hour, or, in a one-job pool, as soon as its job has ended |
| `scaler/job-guard.sh` | The job-started hook of a one-job pool's runner: refuses any job outside the pool's events and repositories |
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
3. For the one-job pool, the repository secret `POOL_PRIVILEGED_REPOS`: the space-separated names
   of the repositories it serves. Unset, that pool creates nothing.
4. Repository variable `HCLOUD_SSH_KEY`: the name of an SSH key that exists in the pool's Hetzner
   project. Every server the scaler and the bake workflow create carries it, so Hetzner sends no
   root-password email. With it unset, or naming a missing key, the scaler creates nothing and
   fails after its cleanup has run, and the bake workflow fails.
5. Run `pool bake` once; afterwards it runs weekly.
6. Deploy the Worker (`cd webhook && npx wrangler@4 deploy`, secrets `WEBHOOK_SECRET` and
   `DISPATCH_TOKEN` via `wrangler secret put`) and point an organisation webhook for
   `workflow_job` events at it.

Tests: `python3 -m unittest discover -s tests`.
