"""Runs scaler/job-guard.sh, the job-started hook of a one-job pool's runner, with the GITHUB_*
variables the runner gives it, and checks which jobs it lets start."""
import os
import shutil
import subprocess
import tempfile
import unittest

GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scaler", "job-guard.sh")


class JobGuard(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)
        self.env_file = os.path.join(self.dir, "pool-job-guard.env")
        with open(self.env_file, "w") as f:
            f.write("ORG='org'\nEVENTS='schedule workflow_dispatch'\nALLOWED_REPOS='infra other'\n")

    def start(self, event, repository, env_file=None, ref="refs/heads/main"):
        env = {"PATH": os.environ["PATH"], "POOL_GUARD_ENV": env_file or self.env_file}
        if ref is not None:
            env["GITHUB_REF"] = ref
        if event is not None:
            env["GITHUB_EVENT_NAME"] = event
        if repository is not None:
            env["GITHUB_REPOSITORY"] = repository
        return subprocess.run(["bash", GUARD], env=env, capture_output=True, text=True)

    def test_a_scheduled_job_of_a_listed_repo_starts(self):
        self.assertEqual(self.start("schedule", "org/infra").returncode, 0)

    def test_a_manually_started_job_of_a_listed_repo_starts(self):
        self.assertEqual(self.start("workflow_dispatch", "org/other").returncode, 0)

    def test_a_pull_request_job_is_refused(self):
        result = self.start("pull_request", "org/infra")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing a job of event 'pull_request'", result.stderr)

    def test_every_other_event_is_refused(self):
        for event in ("pull_request_target", "push", "workflow_run", "issue_comment", "", None):
            with self.subTest(event=event):
                self.assertNotEqual(self.start(event, "org/infra").returncode, 0)

    def test_a_repo_not_listed_is_refused(self):
        result = self.start("schedule", "org/elsewhere")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("takes no job of repository", result.stderr)

    def test_a_listed_name_under_another_owner_is_refused(self):
        self.assertNotEqual(self.start("schedule", "fork/infra").returncode, 0)

    def test_a_name_that_only_contains_a_listed_one_is_refused(self):
        self.assertNotEqual(self.start("schedule", "org/infra-fork").returncode, 0)

    def test_a_manual_run_of_another_branch_is_refused(self):
        result = self.start("workflow_dispatch", "org/infra", ref="refs/heads/edited")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("takes only runs of refs/heads/main", result.stderr)

    def test_a_tag_a_pull_request_ref_or_no_ref_is_refused(self):
        for ref in ("refs/tags/v1", "refs/pull/7/merge", "refs/heads/main-copy", "", None):
            with self.subTest(ref=ref):
                self.assertNotEqual(self.start("workflow_dispatch", "org/infra", ref=ref).returncode, 0)

    def test_without_its_lists_every_job_is_refused(self):
        result = self.start("schedule", "org/infra", env_file=os.path.join(self.dir, "missing"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing the job", result.stderr)


if __name__ == "__main__":
    unittest.main()
