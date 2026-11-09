#!/usr/bin/env python3
"""
Scale one Hetzner pool of GitHub Actions runners to the jobs waiting for it.

One pool = one Hetzner Cloud project (its token reaches nothing else) and one runner label.
Every pool works the same way and runs this same file, configured by environment only:

  POOL            runner label and Hetzner label value, e.g. promptics-pool
  ORG             GitHub organisation the runners register with, e.g. promptics
  REPOS           space-separated repos to watch for queued jobs (default: every repo of ORG)
  CANDIDATES      server type and location pairs to try in order, e.g. "cx43:fsn1 cx43:nbg1 cpx42:fsn1";
                  Hetzner often has no capacity for one type in one location, so a create walks the list;
                  when every pair refuses for capacity or a project limit, creating stops for that run
                  with a warning; any other refusal fails the run, in both cases after the sweep
  MAX_SERVERS     upper bound on servers in the pool (default 2)
  SLOTS           runners per server (default 2)
  HCLOUD_TOKEN    read/write token of the pool's own Hetzner project
  HCLOUD_SSH_KEY  name of an SSH key in the pool's Hetzner project; every server is created with it,
                  so Hetzner sends no root-password email. Required to create: with it unset, or naming
                  a key the project does not have, no server is created and the run fails; the sweep
                  and cleanup still run first, then the run exits non-zero
  GH_TOKEN        classic PAT with admin:org (org runner registration and removal) and repo
                  (reading queued jobs of private repos)
  DRY_RUN         "1": decide and print, change nothing
  IMAGE_POOL      whose baked snapshot servers boot from (default: POOL), so a pool can share another's
  ONE_JOB         "1": a one-job pool. Each server registers an ephemeral runner that takes exactly one
                  job, refuses any job outside EVENTS and REPOS before its first step (job-guard.sh),
                  and deletes itself as soon as that job has ended. Such a pool needs REPOS and EVENTS,
                  or it creates nothing
  IDLE_KEEP_MIN   minutes (default 0 = off): an idle server in its removal window stays for another
                  billed hour while one of its runners ended a job within this many minutes; the
                  reaper labels it pool-keep-until=<epoch> and the sweep honours the label
  IDLE_KEEP_MAX   servers (default 0 = no cap): at most this many servers of the pool are kept warm
                  by IDLE_KEEP_MIN at once; the reaper counts the pool's live pool-keep-until labels
  EVENTS          space-separated workflow run events whose jobs a one-job pool serves, e.g.
                  "schedule workflow_dispatch push workflow_run"; queued jobs of other events create
                  no server

The rule. A server is billed per started hour from its own
creation instant, running or powered off; only deletion stops the meter. So:

  reuse   a queued job goes to an idle registered runner first; GitHub routes that itself
  create  only for queued jobs that idle and booting runners cannot take, up to MAX_SERVERS
  remove  only when idle, and only in minutes [REMOVE_FROM, REMOVE_UNTIL) of the server's own
          billing hour: an idle server earlier in its hour is already paid for and is reused

The server removes itself (reaper.sh, every minute, on the server): GitHub's scheduled
workflows start minutes late and would miss the window. This controller runs every few
minutes, creates what demand needs, and sweeps what a dead reaper left behind. It also deletes
a bake server whose bake workflow died before deleting it: such a server would bill forever.
"""
import datetime as dt
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

HC = "https://api.hetzner.cloud/v1"
GH = "https://api.github.com"
REMOVE_FROM, REMOVE_UNTIL = 50, 58          # minutes into the billing hour
SWEEP_LAG_MIN = 3                           # with the idle window on, the sweep waits for the reaper's label
BOOT_GRACE_MIN = 15                         # a server younger than this may still be booting
BAKE_MAX_MIN = 45                           # pool-bake.yml times out at 40; an older bake server lost its workflow
# Reading queued jobs. GitHub cancels a job that has been queued for 24 hours, but a re-run keeps
# the old run's id and creation time, so a run older than that can still hold a queued job. Per
# watched repo and reconcile: at most RUN_PAGES windowed run-list calls, up to RUN_PAGES per RERUN_STATUSES
# entry (GitHub lists by creation time, so a re-run sits at the tail), and one jobs call per active
# run, up to MAX_ACTIVE_RUNS: RUN_PAGES * (1 + len(RERUN_STATUSES)) + MAX_ACTIVE_RUNS = 59 calls at most.
RUN_WINDOW_H = 24
RERUN_STATUSES = ("queued", "in_progress")  # unwindowed lists that catch re-runs of old runs
RUN_PAGES = 3                               # of 100 runs each
MAX_ACTIVE_RUNS = 50
ACTIVE_RUN_STATUSES = ("queued", "in_progress", "waiting", "pending", "requested")
STALE_QUEUED_MIN = 30                       # a queued job older than this is named in a warning
HERE = os.path.dirname(os.path.abspath(__file__))
# Where a one-job server installs its job-started hook. The runner runs a hook only from a path
# ending in .sh, .ps1 or .js and fails the job's set-up otherwise.
GUARD_BIN = "/usr/local/bin/pool-job-guard.sh"


# ---------------------------------------------------------------- pure decisions (tested)

def minute_of_billing_hour(created: dt.datetime, now: dt.datetime) -> int:
    """Whole minutes into the current billing hour, counted from the server's own creation."""
    return int((now - created).total_seconds() // 60) % 60


def in_removal_window(created: dt.datetime, now: dt.datetime) -> bool:
    return REMOVE_FROM <= minute_of_billing_hour(created, now) < REMOVE_UNTIL


def servers_to_create(queued: int, idle_slots: int, booting_slots: int,
                      servers: int, max_servers: int, slots: int) -> int:
    """Servers to add so every queued job has a runner, never past the pool's maximum."""
    unmet = max(0, queued - idle_slots - booting_slots)
    return max(0, min(math.ceil(unmet / slots), max_servers - servers))


def should_remove(created: dt.datetime, now: dt.datetime, busy: bool, queued: int,
                  runners_online: int, keep_until: int | None = None, idle_keep_min: int = 0) -> bool:
    """The sweep's rule, the same as the reaper's: idle, nothing waiting, end of its hour.
    A server with no runner online past the boot grace is broken and goes at the same point.
    A server its reaper labelled pool-keep-until (the idle window) stays until that time,
    unless it is broken; once the time passes, a dead reaper's server goes as before."""
    if busy:
        return False
    if not in_removal_window(created, now):
        return False
    age_min = (now - created).total_seconds() / 60
    broken = runners_online == 0 and age_min > BOOT_GRACE_MIN
    if broken:
        return True
    if keep_until is not None and now.timestamp() < keep_until:
        return False
    # With the idle window on, a server its reaper has not yet labelled may be about to be kept:
    # the reaper decides from its own clock (boot time plus a lag), which can trail Hetzner's
    # `created` by a minute or two. The sweep leaves it SWEEP_LAG_MIN more minutes to decide.
    if idle_keep_min > 0 and keep_until is None and minute_of_billing_hour(created, now) < REMOVE_FROM + SWEEP_LAG_MIN:
        return False
    return queued == 0


def idle_keep_min_from_env() -> int:
    """IDLE_KEEP_MIN, or 0 when unset or empty: a matrix entry without it passes an empty string,
    and env() would refuse that as a missing required value."""
    return int(os.environ.get("IDLE_KEEP_MIN") or "0")


def idle_keep_max_from_env() -> int:
    """IDLE_KEEP_MAX, or 0 (no cap) when unset or empty, for the same reason."""
    return int(os.environ.get("IDLE_KEEP_MAX") or "0")


def keep_until(server) -> int | None:
    """The idle window's end its reaper labelled on the server, or None."""
    value = (server.get("labels") or {}).get("pool-keep-until")
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def may_reap(busy_states) -> bool:
    """Whether a server may go, given the busy state of each of its runners right before the
    reap: True (holds a job), False (idle) or None (could not be read). Fail safe: only a server
    whose every runner is known to be idle goes."""
    return all(state is False for state in busy_states)


def job_wanted(run_event: str, job_labels, pool: str, events) -> bool:
    """Whether a queued job asks for this pool's server. With EVENTS set, only jobs of runs started
    by one of those events count, so a job of any other event never makes a server appear."""
    if pool not in job_labels:
        return False
    return not events or run_event in events


def run_active(run, events) -> bool:
    """Whether a run may still hold a queued job: its own status is not completed, and with
    EVENTS set its event is one of them."""
    if run.get("status") not in ACTIVE_RUN_STATUSES:
        return False
    return not events or run.get("event") in events


def stale_jobs(jobs, now: dt.datetime, minutes: int = STALE_QUEUED_MIN):
    """The queued jobs that have waited longer than `minutes`, oldest first."""
    def since(job):
        return dt.datetime.fromisoformat(job["created_at"].replace("Z", "+00:00"))
    old = [j for j in jobs if j.get("created_at") and (now - since(j)).total_seconds() / 60 > minutes]
    return sorted(old, key=since)


def one_job_config_error(one_job: bool, repos, events):
    """Why a one-job pool may not create a server, or None. Its runner guards against jobs outside
    an explicit list of repositories and events, so both lists must be set."""
    if not one_job:
        return None
    if not repos:
        return "ONE_JOB pool without REPOS: its runner would have no repository list to guard with. Nothing was created."
    if not events:
        return "ONE_JOB pool without EVENTS: its runner would have no event list to guard with. Nothing was created."
    return None


def stray_bake(created: dt.datetime, now: dt.datetime) -> bool:
    """A bake server outlives its workflow only when that workflow died before its cleanup step."""
    return (now - created).total_seconds() / 60 > BAKE_MAX_MIN


# ---------------------------------------------------------------- the two APIs

def _call(url, token, method="GET", body=None, github=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "promptics-buildserver-pool")
    if github:
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


def env(name, default=None):
    value = os.environ.get(name, default)
    if value is None or value == "":
        sys.exit(f"{name} is required")
    return value


class Pool:
    # A shared pool unless the environment says otherwise.
    repos, events, one_job, image_pool, idle_keep_min, idle_keep_max = [], [], False, None, 0, 0

    def __init__(self):
        self.pool = env("POOL")
        self.org = env("ORG")
        self.repos = os.environ.get("REPOS", "").split()
        self.candidates = [tuple(c.split(":", 1)) for c in env("CANDIDATES").split()]
        self.max_servers = int(env("MAX_SERVERS", "2"))
        self.slots = int(env("SLOTS", "2"))
        self.hc_token = env("HCLOUD_TOKEN")
        self.ssh_key = os.environ.get("HCLOUD_SSH_KEY", "")   # checked in create(): deleting never needs it
        self.gh_token = env("GH_TOKEN")
        self.dry = os.environ.get("DRY_RUN") == "1"
        self.image_pool = os.environ.get("IMAGE_POOL") or self.pool
        self.one_job = os.environ.get("ONE_JOB") == "1"
        self.idle_keep_min = idle_keep_min_from_env()
        self.idle_keep_max = idle_keep_max_from_env()
        self.events = os.environ.get("EVENTS", "").split()

    # Hetzner
    def hc(self, path, method="GET", body=None):
        return _call(f"{HC}{path}", self.hc_token, method, body)

    def servers(self):
        sel = urllib.parse.quote(f"pool={self.pool}")
        return self.hc(f"/servers?label_selector={sel}&per_page=50")["servers"]

    def bake_servers(self):
        sel = urllib.parse.quote(f"pool-bake={self.pool}")
        return self.hc(f"/servers?label_selector={sel}&per_page=50")["servers"]

    def ssh_key_error(self):
        """Why no server may be created, or None. A server created without the configured SSH key
        makes Hetzner mail out a root password."""
        if not self.ssh_key:
            return ("HCLOUD_SSH_KEY is required to create servers: set the repository variable to the "
                    "name of an SSH key in the Hetzner project. Nothing was created.")
        found = self.hc(f"/ssh_keys?name={urllib.parse.quote(self.ssh_key)}")["ssh_keys"]
        if not any(k["name"] == self.ssh_key for k in found):
            return ("HCLOUD_SSH_KEY: the configured SSH key does not exist in the Hetzner project; "
                    "nothing was created. Set HCLOUD_SSH_KEY to the name of an existing key.")
        return None

    def image(self):
        """The newest snapshot baked for this pool, or None (then the server bakes at boot)."""
        sel = urllib.parse.quote(f"pool-image={self.image_pool or self.pool}")
        images = self.hc(f"/images?type=snapshot&label_selector={sel}&sort=created:desc")["images"]
        return images[0]["id"] if images else None

    # GitHub
    def gh(self, path, method="GET", body=None):
        return _call(f"{GH}{path}", self.gh_token, method, body, github=True)

    def runners(self):
        out, page = [], 1
        while True:
            batch = self.gh(f"/orgs/{self.org}/actions/runners?per_page=100&page={page}")["runners"]
            out += [r for r in batch if any(l["name"] == self.pool for l in r["labels"])]
            if len(batch) < 100:
                return out
            page += 1

    def watched_repos(self):
        if self.repos:
            return self.repos
        out, page = [], 1
        while True:
            batch = self.gh(f"/orgs/{self.org}/repos?per_page=100&page={page}&type=all")
            out += [r["name"] for r in batch if not r.get("archived")]
            if len(batch) < 100:
                return out
            page += 1

    def run_list(self, repo, query, pages):
        """Up to `pages` pages of 100 workflow runs of one repo for a query string, newest first,
        and whether the last page was full (more may exist)."""
        out, full = [], False
        for page in range(1, pages + 1):
            try:
                batch = self.gh(f"/repos/{self.org}/{repo}/actions/runs?{query}&per_page=100&page={page}")
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    sys.exit(f"{self.org}/{repo}: 404 reading its workflow runs. A private repo needs "
                             f"the PAT's `repo` scope as well as admin:org.")
                raise
            runs = batch.get("workflow_runs", [])
            out += runs
            full = len(runs) == 100
            if not full:
                break
        return out, full

    def active_runs(self, repo, now):
        """Runs of one repo that may still hold a queued job, most recently updated first. Two
        sources, joined by run id. (1) The runs created in the last RUN_WINDOW_H hours, read
        without a status filter, because that filter can leave out a run whose own status is
        queued; each run's own status decides. (2) The runs the status filter lists as queued or
        in progress, with no time window, because a re-run keeps its old run id and creation time
        and so falls outside (1). Bounded: at most RUN_PAGES * (1 + len(RERUN_STATUSES)) list calls and
        at most MAX_ACTIVE_RUNS runs."""
        since = (now - dt.timedelta(hours=RUN_WINDOW_H)).strftime("%Y-%m-%dT%H:%M:%SZ")
        listed, full = self.run_list(repo, f"created={urllib.parse.quote('>=' + since)}", RUN_PAGES)
        if full:
            print(f"warning: {self.org}/{repo}: more than {RUN_PAGES * 100} runs in {RUN_WINDOW_H}h; "
                  f"older runs were not read")
        for status in RERUN_STATUSES:
            more, full = self.run_list(repo, f"status={status}", RUN_PAGES)
            listed += more
            if full:
                print(f"warning: {self.org}/{repo}: more than {RUN_PAGES * 100} {status} runs; "
                      f"older ones were not read")
        seen, out = set(), []
        for r in listed:
            if r["id"] not in seen and run_active(r, self.events):
                seen.add(r["id"])
                out.append(r)
        out.sort(key=lambda r: r.get("updated_at") or r.get("created_at") or "", reverse=True)
        if len(out) > MAX_ACTIVE_RUNS:
            print(f"warning: {self.org}/{repo}: {len(out)} active runs; reading the jobs of the "
                  f"newest {MAX_ACTIVE_RUNS} only")
            out = out[:MAX_ACTIVE_RUNS]
        return out

    def waiting_jobs(self, now):
        """Queued jobs that ask for this pool's label, across the watched repos, each as
        {repo, run_id, job_id, name, created_at}."""
        out = []
        for repo in self.watched_repos():
            for run in self.active_runs(repo, now):
                jobs = self.gh(f"/repos/{self.org}/{repo}/actions/runs/{run['id']}/jobs?per_page=100")
                for j in jobs.get("jobs", []):
                    if j.get("status") == "queued" and job_wanted(run.get("event", ""), j.get("labels", []),
                                                                  self.pool, self.events):
                        out.append({"repo": repo, "run_id": run["id"], "job_id": j.get("id"),
                                    "name": j.get("name", ""),
                                    "created_at": j.get("created_at") or run.get("created_at")})
        return out

    def queued_jobs(self, now=None):
        """How many jobs wait for this pool's label, across the watched repos."""
        return len(self.waiting_jobs(now or utcnow()))

    # actions
    def create(self, n, now):
        """Creates n servers. Returns an error message, creating nothing, when the SSH key is not
        usable; the caller finishes its cleanup first and then fails with it."""
        if n <= 0:
            return None
        one_job, repos, events = self.one_job, self.repos, self.events
        error = one_job_config_error(one_job, repos, events) or self.ssh_key_error()
        if error:
            print(error)
            return error
        reg = "dry-run" if self.dry else self.gh(f"/orgs/{self.org}/actions/runners/registration-token", "POST")["token"]
        image = self.image()
        boot = open(os.path.join(HERE, "runner-boot.sh")).read()
        bake = "" if image else open(os.path.join(HERE, "bake.sh")).read()
        reaper = open(os.path.join(HERE, "reaper.sh")).read()
        guard = open(os.path.join(HERE, "job-guard.sh")).read()
        for i in range(n):
            name = f"{self.pool}-{now:%Y%m%d%H%M%S}-{i}"
            refusals = []
            # Secrets travel as user data, never in the image. The Hetzner token reaches only
            # this pool's project, which is what makes a token on the server acceptable.
            user_data = "\n".join([
                "#!/bin/bash",
                "set -euo pipefail",
                f"export ORG='{self.org}' POOL='{self.pool}' NAME='{name}' SLOTS='{self.slots}'",
                f"export REG_TOKEN='{reg}' HCLOUD_TOKEN='{self.hc_token}'",
                f"export REMOVE_FROM='{REMOVE_FROM}' REMOVE_UNTIL='{REMOVE_UNTIL}'",
                "cat > /usr/local/bin/pool-reaper <<'REAPER'",
                reaper,
                "REAPER",
                "chmod 755 /usr/local/bin/pool-reaper",
                f"export ONE_JOB='{1 if one_job else 0}' IDLE_KEEP_MIN='{self.idle_keep_min}' IDLE_KEEP_MAX='{self.idle_keep_max}'",
            ] + ([
                f"export EVENTS='{' '.join(events)}' ALLOWED_REPOS='{' '.join(repos)}'",
                f"export POOL_GUARD_BIN='{GUARD_BIN}'",
                f"cat > {GUARD_BIN} <<'GUARD'",
                guard,
                "GUARD",
                f"chmod 755 {GUARD_BIN}",
            ] if one_job else []) + [
                bake,
                boot,
            ])
            for server_type, location in self.candidates:
                body = {
                    "name": name,
                    "server_type": server_type,
                    "location": location,
                    "image": image or "ubuntu-24.04",
                    "user_data": user_data,
                    "ssh_keys": [self.ssh_key],
                    "labels": {"pool": self.pool},
                    "start_after_create": True,
                    # IPv4 only: each primary IP counts against the account's limit, and nothing a
                    # job reaches needs IPv6.
                    "public_net": {"enable_ipv4": True, "enable_ipv6": False},
                }
                print(f"create {name} ({server_type}, {location}, image {image or 'ubuntu-24.04 + bake'})")
                if self.dry:
                    break
                try:
                    self.hc("/servers", "POST", body)
                    break
                except urllib.error.HTTPError as e:
                    reason = refusal_code(e)
                    refusals.append((f"{server_type}:{location}", reason))
                    print(f"  {server_type} in {location}: {reason}; trying the next candidate")
            else:
                # Every candidate refused, most often because the project has reached its core
                # or server limit, which no other type or location escapes. Creating stops for
                # this run and the run goes on to its cleanup; the next run tries again.
                named = "; ".join(f"{c} {r}" for c, r in refusals)
                if all(r in CAPACITY_REFUSALS for _, r in refusals):
                    print(f"warning: no candidate could place {name} ({named}); "
                          f"creating stops for this run, {n - i - 1} more server(s) not attempted")
                    return None
                # A refusal that is not about capacity (bad input, a missing permission) will not
                # pass by itself: the run fails, after its cleanup, like a missing SSH key.
                print(f"no candidate could place {name} ({named})")
                return f"no candidate could place {name}: {named}"
        return None

    def runner_busy(self, runner):
        """The runner's busy state read fresh from GitHub: True, False, or None when it cannot
        be read. A runner that no longer exists holds no job."""
        try:
            busy = self.gh(f"/orgs/{self.org}/actions/runners/{runner['id']}").get("busy")
        except urllib.error.HTTPError as e:
            return False if e.code == 404 else None
        except (urllib.error.URLError, OSError, ValueError):
            return None
        return busy if isinstance(busy, bool) else None

    def remove(self, server, runners):
        """Deletes the server unless one of its runners holds a job. The runner list the sweep
        decided on is minutes old by now, so every runner's busy state is read again here."""
        minute = minute_of_billing_hour(created(server), utcnow())
        states = []
        for r in runners:
            state = self.runner_busy(r)
            states.append(state)
            print(f"  runner {r['name']}: busy={'unknown' if state is None else str(state).lower()}")
        if not may_reap(states):
            print(f"keep {server['name']} (minute {minute} of its hour): a runner is busy or its state is unknown")
            return False
        print(f"remove {server['name']} (idle, minute {minute} of its hour)")
        if self.dry:
            return True
        # Deregistering first stops GitHub from giving the runner another job. A runner that
        # cannot be deregistered may have just taken one, so its server stays.
        for r in runners:
            try:
                self.gh(f"/orgs/{self.org}/actions/runners/{r['id']}", "DELETE")
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    print(f"  runner {r['name']}: {e.code} deregistering it; keeping {server['name']}")
                    return False
        self.hc(f"/servers/{server['id']}", "DELETE")
        return True

    def reconcile(self):
        now = utcnow()
        servers = self.servers()
        runners = self.runners()
        waiting = self.waiting_jobs(now)
        queued = len(waiting)

        def own(server):
            return [r for r in runners if r["name"].startswith(server["name"] + "-")]

        idle = sum(1 for r in runners if r["status"] == "online" and not r["busy"])
        booting = sum(self.slots for s in servers
                      if not any(r["status"] == "online" for r in own(s))
                      and (now - created(s)).total_seconds() / 60 <= BOOT_GRACE_MIN)
        print(f"pool {self.pool}: {len(servers)} server(s), {len(runners)} runner(s), "
              f"{idle} idle slot(s), {booting} booting slot(s), {queued} queued job(s)")

        # reuse is GitHub's routing; create for what it cannot cover
        create_error = self.create(servers_to_create(queued, idle, booting, len(servers), self.max_servers, self.slots), now)

        # a job that has waited this long was seen by earlier runs and still has no runner
        for j in stale_jobs(waiting, now):
            age = int((now - dt.datetime.fromisoformat(j["created_at"].replace("Z", "+00:00"))).total_seconds() // 60)
            print(f"warning: job {j['job_id']} ({j['name']}) of {self.org}/{j['repo']} run {j['run_id']} "
                  f"has been queued for {age} min and is still not served")

        # sweep what a reaper missed
        for s in servers:
            mine = own(s)
            busy = any(r["busy"] for r in mine)
            online = sum(1 for r in mine if r["status"] == "online")
            if should_remove(created(s), now, busy, queued, online, keep_until(s), self.idle_keep_min):
                self.remove(s, mine)

        # bake servers whose workflow died before deleting them
        for b in self.bake_servers():
            if stray_bake(created(b), now):
                print(f"delete stray bake server {b['name']} ({b['status']}, created {b['created']})")
                if not self.dry:
                    self.hc(f"/servers/{b['id']}", "DELETE")

        self.drop_orphans(runners, {s["name"] for s in servers})
        if create_error:
            sys.exit(create_error)

    def drop_orphans(self, runners, server_names):
        """Runners whose server is gone stay listed as offline; deregisters them. GitHub refuses
        (422) while it still counts such a runner's lost job as running: the job fails on its own
        and a later run deregisters the runner, so that is no error."""
        for r in runners:
            server_name = r["name"].rsplit("-", 1)[0]
            if r["status"] != "offline" or server_name in server_names:
                continue
            print(f"deregister orphan runner {r['name']}")
            if self.dry:
                continue
            try:
                self.gh(f"/orgs/{self.org}/actions/runners/{r['id']}", "DELETE")
            except urllib.error.HTTPError as e:
                if e.code != 422:
                    raise
                print(f"  {r['name']}: GitHub still counts a job as running on it; a later run retries")


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


# Hetzner error codes that mean "not now, not here": no capacity for a type in a location, or the
# project at one of its limits. Any other refusal is a fault the run reports.
CAPACITY_REFUSALS = {"resource_limit_exceeded", "resource_unavailable", "placement_error"}


def refusal_code(error):
    """The error code of a refused Hetzner call, or its HTTP status when the body is not the API's
    JSON (a proxy's error page, an empty body)."""
    try:
        return json.loads(error.read() or b"{}").get("error", {}).get("code", error.code)
    except (ValueError, AttributeError):
        return error.code


def created(server):
    return dt.datetime.fromisoformat(server["created"].replace("Z", "+00:00"))


if __name__ == "__main__":
    if sys.argv[1:] != ["reconcile"]:
        sys.exit("usage: pool.py reconcile")
    Pool().reconcile()
