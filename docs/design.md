# Design: create, reuse, remove within the billing hour

A Hetzner Cloud server is billed per started hour from its own creation instant, running or
powered off; only deletion stops the meter. Every pool follows one rule:

- **Reuse first.** A queued job goes to an idle registered runner; GitHub routes that itself.
- **Create only for unmet demand**, up to the pool's maximum, from the baked snapshot. A create
  walks a list of server type and location pairs, because a single type often has no capacity
  in a single location.
- **Remove only when idle, and only in minutes 50–57 of the server's own billing hour.** An idle
  server earlier in its hour is already paid for and takes the next job for free.

**The server removes itself.** Scheduled workflows start minutes late and would miss that window,
so `scaler/reaper.sh` runs every minute on the server. That puts a Hetzner token on the server,
which is acceptable only because the pool has a project of its own: at worst the token deletes
servers of its own pool.

**A webhook starts the controller.** Scheduled workflows can fire hours apart, so they cannot be
what gives a queued job its server. The organisation's `workflow_job` webhook reaches a small
Worker, which checks the signature and, for a queued job carrying a pool label, dispatches
`pool-scale.yml`. The Worker exists only because a webhook cannot carry the Authorization header
GitHub's API needs. The five-minute schedule stays as a backstop that sweeps servers whose own
reaper failed.

**Secrets travel as user data, never in the image.** The snapshot holds only the toolchain; the
runner registration token and the Hetzner token arrive when a server is created.
