"""Runs scaler/reaper.sh against a fake server: pgrep, kill, curl, logger and date are stubs that
read and record files in a temporary directory, and the runner installs live there too."""
import json
import os
import shutil
import subprocess
import tempfile
import textwrap
import time
import unittest

REAPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scaler", "reaper.sh")
BORN = 1_000_000
BOOT_LAG = 2

STUBS = {
    # pgrep -f PATTERN over the fake process table "<pid> <command line>" in $STATE/procs.
    "pgrep": """
        [ -e "$STATE/pgrep-fails" ] && exit 2
        out=$(grep -F -- "$2" "$STATE/procs" | cut -d' ' -f1)
        [ -n "$out" ] || exit 1
        echo "$out"
    """,
    # Records every signal. A job that lands just before the listeners freeze is simulated by
    # $STATE/job-on-freeze: the first SIGSTOP adds that job's Runner.Worker to the process table.
    "kill": """
        echo "$*" >> "$STATE/kills"
        if [ "$1" = -STOP ] && [ -e "$STATE/job-on-freeze" ]; then
          cat "$STATE/job-on-freeze" >> "$STATE/procs"; rm "$STATE/job-on-freeze"
        fi
        # $STATE/log-on-freeze names a runner whose job ends as the listeners freeze.
        if [ "$1" = -STOP ] && [ -e "$STATE/log-on-freeze" ]; then
          d=$(cat "$STATE/log-on-freeze"); rm "$STATE/log-on-freeze"
          mkdir -p "$d/_diag" && echo job > "$d/_diag/Worker_20261013-180000-utc.log"
        fi
    """,
    # A server list (label_selector) prints $STATE/servers-<n> for the n-th list call, else
    # $STATE/servers; $STATE/list-fails makes it fail.
    "curl": """
        echo "$*" >> "$STATE/curl"
        case "$*" in
          *instance-id*) echo 4242 ;;
          *label_selector*)
            [ -e "$STATE/list-fails" ] && exit 22
            n=$(( $(cat "$STATE/lists" 2>/dev/null || echo 0) + 1 )); echo $n > "$STATE/lists"
            if [ -e "$STATE/servers-$n" ]; then cat "$STATE/servers-$n"; else cat "$STATE/servers"; fi ;;
          *DELETE*) [ -e "$STATE/delete-fails" ] && exit 22 ;;
          *'{"labels":{"pool":"pool-x"}}'*) [ -e "$STATE/unmark-fails" ] && exit 22 ;;
        esac
        exit 0
    """,
    "logger": 'shift 2; echo "$*" >> "$STATE/log"',
    "date": 'echo "$FAKE_NOW"',
}


class Reaper(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.bin = os.path.join(self.dir, "bin")
        self.state = os.path.join(self.dir, "state")
        os.makedirs(self.bin)
        os.makedirs(self.state)
        for name, body in STUBS.items():
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write("#!/bin/bash\n" + textwrap.dedent(body))
            os.chmod(path, 0o755)
        with open(os.path.join(self.dir, "born"), "w") as f:
            f.write(str(BORN))
        procs = []
        for i in (1, 2):
            d = self.slot(i)
            os.makedirs(os.path.join(d, "bin"))
            with open(os.path.join(d, ".runner"), "w") as f:
                f.write('{\n  "agentId": %d,\n  "agentName": "pool-x-%d",\n  "poolId": 1\n}\n' % (i, i))
            procs.append(f"{100 + i} {d}/bin/Runner.Listener run --startuptype service")
        self.write("procs", "\n".join(procs) + "\n")

    def slot(self, i):
        return os.path.join(self.dir, f"actions-runner-{i}")

    def worker(self, i):
        return f"{200 + i} {self.slot(i)}/bin/Runner.Worker spawnclient 7 8\n"

    def write(self, name, text, mode="w"):
        with open(os.path.join(self.state, name), mode) as f:
            f.write(text)

    def read(self, name):
        path = os.path.join(self.state, name)
        if not os.path.exists(path):
            return ""
        with open(path) as f:
            return f.read()

    def run_at(self, minute, **extra):
        env = {
            "PATH": self.bin + os.pathsep + os.environ["PATH"],
            "STATE": self.state,
            "FAKE_NOW": str(BORN + (minute - BOOT_LAG) * 60),
            "REMOVE_FROM": "50", "REMOVE_UNTIL": "58", "HCLOUD_TOKEN": "t",
            "POOL_BORN_FILE": os.path.join(self.dir, "born"),
            "POOL_RUNNER_ROOT": self.dir,
            "POOL_KEPT_FILE": os.path.join(self.dir, "kept"),
            **extra,
        }
        # kill is a shell builtin, which a stub on PATH cannot replace; a function can.
        script = f'kill() {{ "{self.bin}/kill" "$@"; }}; source "{REAPER}"'
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)

    def deleted(self):
        return "DELETE" in self.read("curl")

    def test_outside_the_window_nothing_happens(self):
        self.run_at(30)
        self.assertFalse(self.deleted())
        self.assertEqual(self.read("kills"), "")

    def test_idle_runners_at_the_end_of_the_hour_delete_the_server(self):
        self.assertEqual(self.run_at(52).returncode, 0)
        self.assertTrue(self.deleted())
        self.assertIn("/servers/4242", self.read("curl"))
        self.assertEqual(self.read("kills"), "-STOP 101\n-STOP 102\n")   # frozen, never stopped
        self.assertIn("runner pool-x-1 busy=idle", self.read("log"))
        self.assertIn("runner pool-x-2 busy=idle", self.read("log"))

    def test_a_busy_runner_keeps_the_server_and_is_never_signalled(self):
        self.write("procs", self.worker(2), "a")
        self.run_at(52)
        self.assertFalse(self.deleted())
        self.assertEqual(self.read("kills"), "")
        self.assertIn("runner pool-x-1 busy=idle", self.read("log"))
        self.assertIn("runner pool-x-2 busy=busy", self.read("log"))
        self.assertIn("keeping server", self.read("log"))

    def test_an_unknown_busy_state_keeps_the_server(self):
        self.write("pgrep-fails", "")
        self.run_at(52)
        self.assertFalse(self.deleted())
        self.assertEqual(self.read("kills"), "")
        self.assertIn("busy=unknown", self.read("log"))

    def test_a_job_that_starts_just_before_the_deadline_completes(self):
        # Minute 57 is the last minute of the window. The job lands after the first check and
        # before the listeners freeze: the second check sees it, the listeners are thawed, the
        # job's worker is never signalled and the server stays.
        self.write("job-on-freeze", self.worker(1))
        self.run_at(57)
        self.assertFalse(self.deleted())
        kills = self.read("kills").split("\n")
        self.assertIn("-CONT 101 102", kills)
        self.assertFalse(any(k.endswith(" 201") or "TERM" in k or "INT" in k for k in kills))
        self.assertIn("(frozen): runner pool-x-1 busy=busy", self.read("log"))

    def test_a_worker_of_no_known_runner_keeps_the_server(self):
        self.write("procs", "300 /elsewhere/bin/Runner.Worker spawnclient 1 2\n", "a")
        self.run_at(52)
        self.assertFalse(self.deleted())

    def worker_log(self, i, age_seconds):
        diag = os.path.join(self.slot(i), "_diag")
        os.makedirs(diag, exist_ok=True)
        path = os.path.join(diag, "Worker_20261013-180000-utc.log")
        with open(path, "w") as f:
            f.write("job\n")
        then = time.time() - age_seconds
        os.utime(path, (then, then))

    def test_a_runner_between_two_jobs_keeps_the_server(self):
        # Its last job ended seconds ago and GitHub is handing it the next one: no worker runs yet,
        # but the worker log of the job before was written just now.
        self.worker_log(2, age_seconds=5)
        self.run_at(52)
        self.assertFalse(self.deleted())
        self.assertEqual(self.read("kills"), "")
        self.assertIn("runner pool-x-2 busy=recent", self.read("log"))

    def test_a_runner_quiet_for_minutes_lets_the_server_go(self):
        self.worker_log(2, age_seconds=10 * 60)
        self.run_at(52)
        self.assertTrue(self.deleted())
        self.assertIn("runner pool-x-2 busy=idle", self.read("log"))

    def test_a_job_handed_over_while_the_listeners_freeze_keeps_the_server(self):
        # The job before ends between the first check and the freeze: the frozen re-check sees
        # its fresh worker log although the next job's worker never started.
        self.write("log-on-freeze", self.slot(1))
        self.run_at(55)
        self.assertFalse(self.deleted())
        self.assertIn("(frozen): runner pool-x-1 busy=recent", self.read("log"))
        self.assertIn("-CONT 101 102", self.read("kills"))

    def test_a_failed_delete_thaws_the_listeners(self):
        self.write("delete-fails", "")
        self.assertEqual(self.run_at(52).returncode, 1)
        self.assertIn("-CONT 101 102", self.read("kills"))
        self.assertIn("deleting server 4242 failed", self.read("log"))


class IdleWindowReaper(Reaper):
    """POOL_IDLE_KEEP_MIN: an idle server stays for another billed hour while a job ended recently."""

    def test_a_job_within_the_window_keeps_the_server_and_labels_it(self):
        self.worker_log(2, age_seconds=30 * 60)
        self.run_at(52, POOL_IDLE_KEEP_MIN="240", POOL="pool-x")
        self.assertFalse(self.deleted())
        self.assertEqual(self.read("kills"), "")          # kept before any listener is frozen
        self.assertIn("-X PUT", self.read("curl"))
        self.assertIn('"pool":"pool-x"', self.read("curl"))
        # until the end of the next hour's removal window (minute 58) plus 2 minutes
        self.assertIn('"pool-keep-until":"%d"' % (BORN + (52 - BOOT_LAG) * 60 + (60 - 52 + 58 + 2) * 60), self.read("curl"))
        self.assertIn("/servers/4242", self.read("curl"))
        self.assertIn("a job ended within the last 240 minutes: keeping server", self.read("log"))

    def test_a_job_older_than_the_window_lets_the_server_go(self):
        self.worker_log(2, age_seconds=250 * 60)
        self.run_at(52, POOL_IDLE_KEEP_MIN="240", POOL="pool-x")
        self.assertTrue(self.deleted())
        self.assertNotIn("-X PUT", self.read("curl"))

    def test_no_job_at_all_lets_the_server_go(self):
        self.run_at(52, POOL_IDLE_KEEP_MIN="240", POOL="pool-x")
        self.assertTrue(self.deleted())

    def test_the_window_off_is_todays_rule(self):
        self.worker_log(2, age_seconds=30 * 60)
        self.run_at(52)
        self.assertTrue(self.deleted())

    def test_a_failed_label_still_keeps_the_server(self):
        self.worker_log(2, age_seconds=30 * 60)
        self.run_at(52, POOL_IDLE_KEEP_MIN="240")         # no POOL: the label cannot be written
        self.assertFalse(self.deleted())
        self.assertIn("POOL unknown: cannot label the server kept", self.read("log"))


class WarmCapReaper(Reaper):
    """POOL_IDLE_KEEP_MAX: at most this many servers of the pool are kept warm at once."""

    def now(self, minute=52):
        return BORN + (minute - BOOT_LAG) * 60

    def servers(self, *keep_untils, name="servers"):
        # This server (4242) is listed too, with a live label, to prove it never counts itself.
        servers = [{"id": 4242, "labels": {"pool": "pool-x", "pool-keep-until": str(self.now() + 9999)}}]
        servers += [{"id": 10 + i, "labels": {"pool": "pool-x", "pool-keep-until": str(k)}}
                    for i, k in enumerate(keep_untils)]
        self.write(name, json.dumps({"servers": servers}))

    def run_kept(self, **extra):
        self.worker_log(2, age_seconds=30 * 60)
        return self.run_at(52, POOL_IDLE_KEEP_MIN="60", POOL_IDLE_KEEP_MAX="2", POOL="pool-x", **extra)

    def puts(self):
        return [line for line in self.read("curl").splitlines() if "-X PUT" in line]

    def test_under_the_cap_the_server_is_kept(self):
        self.servers(self.now() + 600, self.now() - 60)      # one live label, one expired
        self.run_kept()
        self.assertFalse(self.deleted())
        self.assertEqual(len(self.puts()), 1)
        self.assertIn('"pool-keep-until"', self.puts()[0])
        self.assertIn("label_selector=pool%3Dpool-x%2Cpool-keep-until", self.read("curl"))
        self.assertTrue(os.path.exists(os.path.join(self.dir, "kept")))

    def test_at_the_cap_the_server_goes(self):
        self.servers(self.now() + 600, self.now() + 1200)
        self.run_kept()
        self.assertTrue(self.deleted())
        self.assertEqual(self.puts(), [])
        self.assertIn("2 servers are already kept warm (cap 2): not keeping server", self.read("log"))

    def test_a_reaper_that_kept_its_server_in_the_same_minute_makes_it_back_off(self):
        self.servers(self.now() + 600, name="servers-1")
        self.servers(self.now() + 600, self.now() + 700, name="servers-2")
        self.run_kept()
        self.assertTrue(self.deleted())
        self.assertEqual(len(self.puts()), 2)
        self.assertIn('"pool-keep-until"', self.puts()[0])
        self.assertIn('{"labels":{"pool":"pool-x"}}', self.puts()[1])   # its own label removed
        self.assertIn("backing off", self.read("log"))
        self.assertFalse(os.path.exists(os.path.join(self.dir, "kept")))

    def test_a_backing_off_server_whose_label_cannot_be_removed_still_goes(self):
        self.servers(self.now() + 600, name="servers-1")
        self.servers(self.now() + 600, self.now() + 700, name="servers-2")
        self.write("unmark-fails", "")
        self.run_kept()
        self.assertTrue(self.deleted())
        self.assertIn("could not remove the keep label: applying the plain rule anyway", self.read("log"))

    def test_an_unreadable_count_keeps_nothing(self):
        self.write("list-fails", "")
        self.run_kept()
        self.assertTrue(self.deleted())
        self.assertEqual(self.puts(), [])
        self.assertIn("cannot count the warm servers: not keeping server", self.read("log"))

    def test_no_cap_lists_nothing(self):
        self.worker_log(2, age_seconds=30 * 60)
        self.run_at(52, POOL_IDLE_KEEP_MIN="60", POOL="pool-x")
        self.assertFalse(self.deleted())
        self.assertNotIn("label_selector", self.read("curl"))

    def test_a_kept_server_that_takes_a_job_gives_its_place_back(self):
        self.write("procs", self.worker(1), mode="a")
        with open(os.path.join(self.dir, "kept"), "w") as f:
            f.write("1")
        self.run_at(20, POOL_IDLE_KEEP_MIN="60", POOL_IDLE_KEEP_MAX="2", POOL="pool-x")
        self.assertEqual(len(self.puts()), 1)
        self.assertIn('{"labels":{"pool":"pool-x"}}', self.puts()[0])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "kept")))
        self.assertIn("busy again: keep label removed", self.read("log"))

    def test_a_busy_server_that_was_not_kept_changes_no_label(self):
        self.write("procs", self.worker(1), mode="a")
        self.run_at(20, POOL_IDLE_KEEP_MIN="60", POOL_IDLE_KEEP_MAX="2", POOL="pool-x")
        self.assertEqual(self.puts(), [])


class OneJobIdleWindow(Reaper):
    def test_a_one_job_server_never_uses_the_window(self):
        self.worker_log(1, age_seconds=30 * 60)          # its job ended; no listener or worker left
        self.write("procs", "")
        self.run_at(20, ONE_JOB="1", POOL_IDLE_KEEP_MIN="240", POOL="pool-x")
        self.assertTrue(self.deleted())
        self.assertNotIn("-X PUT", self.read("curl"))


class OneJobReaper(Reaper):
    """A one-job server goes as soon as its job has ended, at any minute."""

    def job_ended(self):
        # The ephemeral listeners have exited and no worker runs; a worker log shows a job ran.
        self.write("procs", "")
        self.worker_log(1, age_seconds=5)

    def test_a_server_whose_job_ended_goes_at_once(self):
        self.job_ended()
        self.assertEqual(self.run_at(10, ONE_JOB="1").returncode, 0)
        self.assertTrue(self.deleted())
        self.assertIn("its one job has ended", self.read("log"))

    def test_a_server_whose_job_still_runs_stays(self):
        self.write("procs", self.worker(1))
        self.worker_log(1, age_seconds=5)
        self.run_at(10, ONE_JOB="1")
        self.assertFalse(self.deleted())

    def test_a_server_still_waiting_for_its_job_stays_until_its_hour_ends(self):
        self.run_at(10, ONE_JOB="1")
        self.assertFalse(self.deleted())
        self.run_at(52, ONE_JOB="1")
        self.assertTrue(self.deleted())

    def test_a_listener_still_up_keeps_the_server(self):
        self.worker_log(1, age_seconds=5)
        self.run_at(10, ONE_JOB="1")
        self.assertFalse(self.deleted())

    def test_an_unreadable_process_table_keeps_the_server(self):
        self.job_ended()
        self.write("pgrep-fails", "")
        self.run_at(10, ONE_JOB="1")
        self.assertFalse(self.deleted())

    def test_a_shared_server_never_goes_early(self):
        self.job_ended()
        self.run_at(10)
        self.assertFalse(self.deleted())


if __name__ == "__main__":
    unittest.main()
