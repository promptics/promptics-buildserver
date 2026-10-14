#!/usr/bin/env python3
"""
Scale one Hetzner pool of GitHub Actions runners to the jobs waiting for it.

One pool = one Hetzner Cloud project (its token reaches nothing else) and one runner label.
Every pool works the same way and runs this same file, configured by environment only:

  POOL            runner label and Hetzner label value, e.g. promptics-pool
  ORG             GitHub organisation the runners register with, e.g. promptics
  REPOS           space-separated repos to watch for queued jobs (default: every repo of ORG)
  CANDIDATES      server type and location pairs to try in order, e.g. "cx43:fsn1 cx43:nbg1 cpx42:fsn1";
                  Hetzner often has no capacity for one type in one location, so a create walks the list
  MAX_SERVERS     upper bound on servers in the pool (default 2)
  SLOTS           runners per server (default 2)
  HCLOUD_TOKEN    read/write token of the pool's own Hetzner project
  GH_TOKEN        classic PAT with admin:org (org runner registration and removal) and repo
                  (reading queued jobs of private repos)
  DRY_RUN         "1": decide and print, change nothing

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
BOOT_GRACE_MIN = 15                         # a server younger than this may still be booting
BAKE_MAX_MIN = 45                           # pool-bake.yml times out at 40; an older bake server lost its workflow
HERE = os.path.dirname(os.path.abspath(__file__))


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
                  runners_online: int) -> bool:
    """The sweep's rule, the same as the reaper's: idle, nothing waiting, end of its hour.
    A server with no runner online past the boot grace is broken and goes at the same point."""
    if busy:
        return False
    if not in_removal_window(created, now):
        return False
    age_min = (now - created).total_seconds() / 60
    broken = runners_online == 0 and age_min > BOOT_GRACE_MIN
    return queued == 0 or broken


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
    def __init__(self):
        self.pool = env("POOL")
        self.org = env("ORG")
        self.repos = os.environ.get("REPOS", "").split()
        self.candidates = [tuple(c.split(":", 1)) for c in env("CANDIDATES").split()]
        self.max_servers = int(env("MAX_SERVERS", "2"))
        self.slots = int(env("SLOTS", "2"))
        self.hc_token = env("HCLOUD_TOKEN")
        self.gh_token = env("GH_TOKEN")
        self.dry = os.environ.get("DRY_RUN") == "1"

    # Hetzner
    def hc(self, path, method="GET", body=None):
        return _call(f"{HC}{path}", self.hc_token, method, body)

    def servers(self):
        sel = urllib.parse.quote(f"pool={self.pool}")
        return self.hc(f"/servers?label_selector={sel}&per_page=50")["servers"]

    def bake_servers(self):
        sel = urllib.parse.quote(f"pool-bake={self.pool}")
        return self.hc(f"/servers?label_selector={sel}&per_page=50")["servers"]

    def image(self):
        """The newest snapshot baked for this pool, or None (then the server bakes at boot)."""
        sel = urllib.parse.quote(f"pool-image={self.pool}")
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

    def queued_jobs(self):
        """Jobs waiting for this pool's label, across the watched repos."""
        count = 0
        for repo in self.watched_repos():
            for status in ("queued", "in_progress"):
                try:
                    runs = self.gh(f"/repos/{self.org}/{repo}/actions/runs?status={status}&per_page=50")
                except urllib.error.HTTPError as e:
                    if e.code == 404:
                        sys.exit(f"{self.org}/{repo}: 404 reading its workflow runs. A private repo needs "
                                 f"the PAT's `repo` scope as well as admin:org.")
                    raise
                for run in runs.get("workflow_runs", []):
                    jobs = self.gh(f"/repos/{self.org}/{repo}/actions/runs/{run['id']}/jobs?per_page=100")
                    count += sum(1 for j in jobs.get("jobs", [])
                                 if j["status"] == "queued" and self.pool in j.get("labels", []))
        return count

    # actions
    def create(self, n, now):
        if n <= 0:
            return
        reg = "dry-run" if self.dry else self.gh(f"/orgs/{self.org}/actions/runners/registration-token", "POST")["token"]
        image = self.image()
        boot = open(os.path.join(HERE, "runner-boot.sh")).read()
        bake = "" if image else open(os.path.join(HERE, "bake.sh")).read()
        reaper = open(os.path.join(HERE, "reaper.sh")).read()
        for i in range(n):
            name = f"{self.pool}-{now:%Y%m%d%H%M%S}-{i}"
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
                    "labels": {"pool": self.pool},
                    "start_after_create": True,
                }
                print(f"create {name} ({server_type}, {location}, image {image or 'ubuntu-24.04 + bake'})")
                if self.dry:
                    break
                try:
                    self.hc("/servers", "POST", body)
                    break
                except urllib.error.HTTPError as e:
                    reason = json.loads(e.read() or b"{}").get("error", {}).get("code", e.code)
                    print(f"  {server_type} in {location}: {reason}; trying the next candidate")
            else:
                sys.exit(f"no candidate could place {name}: {' '.join(':'.join(c) for c in self.candidates)}")

    def remove(self, server, runners):
        print(f"remove {server['name']} (idle, minute {minute_of_billing_hour(created(server), utcnow())} of its hour)")
        if self.dry:
            return
        for r in runners:
            try:
                self.gh(f"/orgs/{self.org}/actions/runners/{r['id']}", "DELETE")
            except urllib.error.HTTPError as e:
                print(f"  runner {r['name']}: {e.code}")
        self.hc(f"/servers/{server['id']}", "DELETE")

    def reconcile(self):
        now = utcnow()
        servers = self.servers()
        runners = self.runners()
        queued = self.queued_jobs()
        names = {s["name"] for s in servers}

        def own(server):
            return [r for r in runners if r["name"].startswith(server["name"] + "-")]

        idle = sum(1 for r in runners if r["status"] == "online" and not r["busy"])
        booting = sum(self.slots for s in servers
                      if not any(r["status"] == "online" for r in own(s))
                      and (now - created(s)).total_seconds() / 60 <= BOOT_GRACE_MIN)
        print(f"pool {self.pool}: {len(servers)} server(s), {len(runners)} runner(s), "
              f"{idle} idle slot(s), {booting} booting slot(s), {queued} queued job(s)")

        # reuse is GitHub's routing; create for what it cannot cover
        self.create(servers_to_create(queued, idle, booting, len(servers), self.max_servers, self.slots), now)

        # sweep what a reaper missed
        for s in servers:
            mine = own(s)
            busy = any(r["busy"] for r in mine)
            online = sum(1 for r in mine if r["status"] == "online")
            if should_remove(created(s), now, busy, queued, online):
                self.remove(s, mine)

        # bake servers whose workflow died before deleting them
        for b in self.bake_servers():
            if stray_bake(created(b), now):
                print(f"delete stray bake server {b['name']} ({b['status']}, created {b['created']})")
                if not self.dry:
                    self.hc(f"/servers/{b['id']}", "DELETE")

        # runners whose server is gone stay listed as offline; drop them
        for r in runners:
            server_name = r["name"].rsplit("-", 1)[0]
            if r["status"] == "offline" and server_name not in names:
                print(f"deregister orphan runner {r['name']}")
                if not self.dry:
                    self.gh(f"/orgs/{self.org}/actions/runners/{r['id']}", "DELETE")


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def created(server):
    return dt.datetime.fromisoformat(server["created"].replace("Z", "+00:00"))


if __name__ == "__main__":
    if sys.argv[1:] != ["reconcile"]:
        sys.exit("usage: pool.py reconcile")
    Pool().reconcile()
