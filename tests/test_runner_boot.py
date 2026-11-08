"""Runs scaler/runner-boot.sh against a fake server: systemctl, sudo and the runners' config.sh
and svc.sh are stubs that append to one shared call log, so the tests see what ran in which
order."""
import os
import shutil
import sys
import subprocess
import tempfile
import textwrap
import unittest

BOOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scaler", "runner-boot.sh")

STUBS = {
    "systemctl": """
        echo "systemctl $*" >> "$STATE/calls"
        case "$*" in *apt-daily*) [ -e "$STATE/apt-units-fail" ] && exit 1 ;; esac
        exit 0
    """,
    "apt-get": """
        echo "apt-get $*" >> "$STATE/calls"
        [ -e "$STATE/apt-get-fail" ] && exit 100
        exit 0
    """,
    # The real timeout kills its command after N seconds; here N is only logged.
    "timeout": """
        echo "timeout $1" >> "$STATE/calls"
        shift
        exec "$@"
    """,
    # sudo -u runner bash -c CMD
    "sudo": 'shift 2; exec "$@"',
}


class RunnerBoot(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.bin = os.path.join(self.dir, "bin")
        self.state = os.path.join(self.dir, "state")
        self.etc = os.path.join(self.dir, "etc")
        for d in (self.bin, self.state, os.path.join(self.etc, "systemd", "system")):
            os.makedirs(d)
        for name, body in STUBS.items():
            self.script(os.path.join(self.bin, name), body)
        for i in (1, 2):
            d = os.path.join(self.dir, f"actions-runner-{i}")
            os.makedirs(d)
            self.script(os.path.join(d, "config.sh"),
                        f'echo "config.sh slot {i}" >> "$STATE/calls"; echo "$*" > "$STATE/config-args-{i}"')
            self.script(os.path.join(d, "svc.sh"),
                        f'echo "svc.sh slot {i} $*" >> "$STATE/calls"\n'
                        f'[ "$1" = start ] && grep -q "^ACTIONS_RUNNER_HOOK_JOB_STARTED=" .env 2>/dev/null'
                        f' && echo "hook set at start, slot {i}" >> "$STATE/calls"\nexit 0')

    def script(self, path, body):
        with open(path, "w") as f:
            f.write("#!/bin/bash\n" + textwrap.dedent(body))
        os.chmod(path, 0o755)

    def boot(self, **extra):
        env = {
            "PATH": self.bin + os.pathsep + os.environ["PATH"],
            "STATE": self.state,
            "POOL_ETC": self.etc,
            "POOL_RUNNER_ROOT": self.dir,
            "ORG": "org", "POOL": "pool", "NAME": "pool-x", "SLOTS": "2", "REG_TOKEN": "r",
            "HCLOUD_TOKEN": "t", "REMOVE_FROM": "50", "REMOVE_UNTIL": "58",
            **extra,
        }
        # pool.py runs this script under these options; a failing command must not end the boot.
        script = f'set -euo pipefail; source "{BOOT}"'
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)

    def calls(self):
        with open(os.path.join(self.state, "calls")) as f:
            return f.read().splitlines()

    def test_unattended_upgrades_are_off_before_the_first_runner_starts(self):
        result = self.boot()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        first_runner = calls.index("config.sh slot 1")
        disable = calls.index("systemctl disable --now apt-daily.timer apt-daily-upgrade.timer")
        stop = calls.index("systemctl stop apt-daily.service apt-daily-upgrade.service")
        self.assertLess(disable, first_runner)
        self.assertLess(stop, first_runner)
        self.assertIn("svc.sh slot 2 start", calls)

    def test_needrestart_never_restarts_a_service(self):
        self.boot()
        with open(os.path.join(self.etc, "needrestart", "conf.d", "pool.conf")) as f:
            conf = f.read()
        self.assertIn("$nrconf{restart} = 'l';", conf)

    def test_a_server_whose_apt_timers_cannot_be_changed_still_registers_its_runners(self):
        self.write_flag("apt-units-fail")
        result = self.boot()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertIn("svc.sh slot 1 start", calls)
        self.assertIn("svc.sh slot 2 start", calls)
        self.assertIn("systemctl enable --now pool-reaper.timer", calls)
        self.assertIn("cannot disable the apt timers", result.stderr)

    def test_security_upgrade_runs_bounded_and_before_the_first_runner_registers(self):
        result = self.boot()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        disable = calls.index("systemctl disable --now apt-daily.timer apt-daily-upgrade.timer")
        update = calls.index("apt-get update -q")
        upgrade = calls.index("apt-get -yq -o Dpkg::Options::=--force-confold upgrade")
        self.assertLess(disable, update)
        self.assertLess(update, upgrade)
        self.assertLess(upgrade, calls.index("config.sh slot 1"))
        self.assertIn("timeout 600", calls)

    def test_a_failed_security_upgrade_still_registers_the_runners(self):
        self.write_flag("apt-get-fail")
        result = self.boot()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertNotIn("apt-get -yq -o Dpkg::Options::=--force-confold upgrade", calls)
        self.assertIn("svc.sh slot 1 start", calls)
        self.assertIn("svc.sh slot 2 start", calls)
        self.assertIn("systemctl enable --now pool-reaper.timer", calls)
        self.assertIn("boot-time security upgrade failed", result.stderr)

    def test_the_reaper_timer_is_armed(self):
        self.boot()
        self.assertIn("systemctl enable --now pool-reaper.timer", self.calls())
        self.assertTrue(os.path.exists(os.path.join(self.etc, "pool-born")))

    def read(self, *path):
        full = os.path.join(*path)
        if not os.path.exists(full):
            return ""
        with open(full) as f:
            return f.read()

    def one_job_boot(self):
        result = self.boot(ONE_JOB="1", EVENTS="schedule workflow_dispatch push workflow_run", ALLOWED_REPOS="repo",
                           POOL_GUARD_BIN="/guard")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_shared_pool_registers_reusable_runners_without_a_guard(self):
        self.boot()
        self.assertNotIn("--ephemeral", self.read(self.state, "config-args-1"))
        self.assertNotIn("--disableupdate", self.read(self.state, "config-args-1"))
        self.assertNotIn("ACTIONS_RUNNER_HOOK_JOB_STARTED", self.read(self.dir, "actions-runner-1", ".env"))
        self.assertFalse(os.path.exists(os.path.join(self.etc, "pool-job-guard.env")))
        self.assertIn("ONE_JOB='0'", self.read(self.etc, "pool.env"))
        self.assertIn("POOL_IDLE_KEEP_MIN='0'", self.read(self.etc, "pool.env"))   # unset: off

    def test_the_idle_window_and_pool_reach_the_reaper(self):
        self.boot(IDLE_KEEP_MIN="240")
        env = self.read(self.etc, "pool.env")
        self.assertIn("POOL_IDLE_KEEP_MIN='240'", env)
        self.assertRegex(env, r"(?m)^POOL='[^']+'$")

    def test_a_one_job_pool_registers_ephemeral_runners(self):
        self.one_job_boot()
        for i in (1, 2):
            self.assertIn("--ephemeral", self.read(self.state, f"config-args-{i}"))
            self.assertIn("--disableupdate", self.read(self.state, f"config-args-{i}"))
        self.assertIn("ONE_JOB='1'", self.read(self.etc, "pool.env"))

    def test_a_one_job_runner_runs_the_guard_before_every_job(self):
        self.one_job_boot()
        for i in (1, 2):
            self.assertIn("ACTIONS_RUNNER_HOOK_JOB_STARTED=/guard",
                          self.read(self.dir, f"actions-runner-{i}", ".env").splitlines())
        guard_env = self.read(self.etc, "pool-job-guard.env")
        self.assertIn("EVENTS='schedule workflow_dispatch push workflow_run'", guard_env)
        self.assertIn("ALLOWED_REPOS='repo'", guard_env)
        self.assertIn("ORG='org'", guard_env)

    def test_the_default_hook_path_is_one_the_runner_runs(self):
        # Without POOL_GUARD_BIN, as on a server whose user data predates it.
        result = self.boot(ONE_JOB="1", EVENTS="schedule", ALLOWED_REPOS="repo")
        self.assertEqual(result.returncode, 0, result.stderr)
        hooks = [l.split("=", 1)[1] for l in self.read(self.dir, "actions-runner-1", ".env").splitlines()
                 if l.startswith("ACTIONS_RUNNER_HOOK_JOB_STARTED=")]
        self.assertEqual(len(hooks), 1)
        self.assertTrue(hooks[0].endswith((".sh", ".ps1", ".js")), hooks[0])

    def test_boot_and_scaler_name_the_same_hook(self):
        result = self.boot(ONE_JOB="1", EVENTS="schedule", ALLOWED_REPOS="repo")
        self.assertEqual(result.returncode, 0, result.stderr)
        sys.path.insert(0, os.path.join(os.path.dirname(BOOT)))
        import pool
        self.assertIn(f"ACTIONS_RUNNER_HOOK_JOB_STARTED={pool.GUARD_BIN}",
                      self.read(self.dir, "actions-runner-1", ".env").splitlines())

    def test_the_guard_is_in_place_before_the_runner_starts(self):
        # The runner reads .env when its service starts; the svc.sh stub records what it holds then.
        self.one_job_boot()
        calls = self.calls()
        self.assertIn("hook set at start, slot 1", calls)
        self.assertIn("hook set at start, slot 2", calls)

    def write_flag(self, name):
        open(os.path.join(self.state, name), "w").close()


if __name__ == "__main__":
    unittest.main()
