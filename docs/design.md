# Design: create, reuse, remove within the billing hour

A Hetzner Cloud server is billed per started hour from its own creation instant, running or
powered off; only deletion stops the meter. Every pool follows one rule:

- **Reuse first.** A queued job goes to an idle registered runner; GitHub routes that itself.
- **Create only for unmet demand**, up to the pool's maximum, from the baked snapshot. A create
  walks a list of server type and location pairs, because a single type often has no capacity
  in a single location.
- **A pair may name its own slot count** (`type:location:slots`). A larger type then runs that
  many jobs at once, each with the share a smaller server gives one job, so when the cheapest
  one-job type is sold out, one paid hour of a larger server covers two jobs and halves the
  idle tail per job. The pool's maximum counts slots, so a 2-slot server counts twice and the
  most the pool can run, and cost, stays bounded the same way. A create fills the unmet slots and
  skips a candidate with more slots than the maximum leaves room for. The warm cap counts
  servers: a 2-slot server kept warm is one of them. A one-job server always has one slot.
  Two jobs on one server share its Docker daemon and home directory, so no workflow the pool
  serves may clean up server-wide (all containers, the Gradle home, /tmp); each cleans up
  only what it started, or what is older than its job's time limit.
- **Remove only when idle, and only in minutes 50–57 of the server's own billing hour.** An idle
  server earlier in its hour is already paid for and takes the next job for free.
- **Optionally, keep an idle server warm** (`IDLE_KEEP_MIN`, off by default). In its removal window
  an idle server stays for another billed hour while one of its runners ended a job within that
  many minutes, so a short pause in the pipeline does not cost the next job a cold boot. The
  reaper labels such a server `pool-keep-until` (the end of the next hour's removal window), and
  the controller's sweep honours the label; a one-job pool never keeps a server. The sweep counts
  minutes from the server's creation and the reaper from its boot, so with the window on the sweep
  leaves an unlabelled server a few minutes into the window before it removes it. A sweep can still
  occasionally remove a server its reaper was about to keep; no job is lost (the sweep rereads
  whether it is busy and deregisters its runners first), only that server's warmth.
- **Optionally, cap how many servers are kept warm** (`IDLE_KEEP_MAX`, no cap by default). Before
  keeping its server, the reaper counts the other servers of its pool whose `pool-keep-until` label
  lies in the future, and at the cap it removes the server as usual. Two reapers can decide in the
  same minute, so each counts again after labelling and backs off if the pool is over the cap: the
  pool may briefly have fewer warm servers than the cap, never more. A count it cannot read keeps
  nothing. A warm server that takes a job removes its label at once, so a busy server never holds
  a warm place. If a backing-off server cannot remove its label, it is still removed by the plain
  rule; only if a job reaches it first does it keep the label, until that job makes it release it,
  or, should its delete fail as well, until its keep time ends (about an hour).
  The recount relies on Hetzner's server list showing a label right after it was set; that holds in
  practice but is not documented, and if it ever lagged the pool could briefly hold one warm server
  over the cap. The count reads one page of 50 servers, enough for any pool of up to 50.

**The server removes itself.** Scheduled workflows start minutes late and would miss that window,
so `scaler/reaper.sh` runs every minute on the server. That puts a Hetzner token on the server,
which is acceptable only because the pool has a project of its own: at worst the token deletes
servers of its own pool.

**A server holding a job is never removed.** Stopping a runner service while it holds a job
cancels the job, and a job can land between any check and the stop. So the reaper never stops a
runner: it checks every runner, freezes the runner listeners with SIGSTOP so none can take a new
job, checks again, and deletes the server only if every runner is still idle; otherwise it thaws
the listeners. A runner also counts as busy for a few minutes after its last job: with jobs
queued, GitHub hands it the next one within seconds, and until the listener has started that job's
worker no process shows it, so a server deleted then loses the job. A runner whose state cannot be
read counts as busy. The controller's sweep reads
each runner's `busy` from GitHub again right before it removes a server, and keeps the server
when a runner is busy, unreadable, or refuses to be deregistered.

**Nothing else restarts a runner either.** A stock Ubuntu server runs unattended-upgrades within
its first hour, and needrestart then restarts every service that maps an upgraded library, the
runner services among them, which cancels their jobs just as a reaper would. So a pool server
turns the apt timers off at boot, installs the pending security updates once and synchronously
(bounded to ten minutes; a failure is logged and the runners register anyway), and only then
registers its runners, with needrestart set to list what it would restart, never restart it. The
weekly bake upgrades the snapshot's packages, so little is left to install at boot. A kernel
installed at boot takes effect only on a reboot, which short-lived pool servers never do.

**A webhook starts the controller.** Scheduled workflows can fire hours apart, so they cannot be
what gives a queued job its server. The organisation's `workflow_job` webhook reaches a small
Worker, which checks the signature and, for a queued job carrying a pool label, dispatches
`pool-scale.yml`. The Worker exists only because a webhook cannot carry the Authorization header
GitHub's API needs. The five-minute schedule stays as a backstop that sweeps servers whose own
reaper failed.

**A one-job pool for what may not be shared.** A shared server runs jobs of several
repositories, one after another, within its hour. A job that needs more than such a server should
give any job, a privileged container for one, runs in a one-job pool instead: each server
registers an ephemeral runner, which takes exactly one job, and the reaper deletes the server as
soon as that job has ended, at any minute, since nothing is left to reuse. The pool serves only
runs on the main branch, started by a schedule, by hand, by a push to main or after another run on
main, of the repositories its configuration lists, in two
places: the scaler creates a server only for such a queued job, and the runner's job-started hook
refuses any other job before its first step, because GitHub hands a queued job to any idle runner
whose labels match. A pull request therefore never reaches it. The pool boots from the shared
pool's snapshot and has a server limit of its own.

**Secrets travel as user data, never in the image.** The snapshot holds only the toolchain; the
runner registration token and the Hetzner token arrive when a server is created.
