"""Runs scaler/reaper.sh against a fake server: pgrep, kill, curl, logger and date are stubs that
read and record files in a temporary directory, and the runner installs live there too."""
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
    "curl": """
        echo "$*" >> "$STATE/curl"
        case "$*" in
          *instance-id*) echo 4242 ;;
          *DELETE*) [ -e "$STATE/delete-fails" ] && exit 22 ;;
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

    def run_at(self, minute):
        env = {
            "PATH": self.bin + os.pathsep + os.environ["PATH"],
            "STATE": self.state,
            "FAKE_NOW": str(BORN + (minute - BOOT_LAG) * 60),
            "REMOVE_FROM": "50", "REMOVE_UNTIL": "58", "HCLOUD_TOKEN": "t",
            "POOL_BORN_FILE": os.path.join(self.dir, "born"),
            "POOL_RUNNER_ROOT": self.dir,
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


if __name__ == "__main__":
    unittest.main()
