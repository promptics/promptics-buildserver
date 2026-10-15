import contextlib
import datetime as dt
import io
import os
import sys
import unittest
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scaler"))
import pool  # noqa: E402

T0 = dt.datetime(2026, 10, 13, 18, 0, tzinfo=dt.timezone.utc)


def at(minutes):
    return T0 + dt.timedelta(minutes=minutes)


class BillingHour(unittest.TestCase):
    def test_minute_counts_from_the_servers_own_creation(self):
        created = T0 + dt.timedelta(minutes=7)
        self.assertEqual(pool.minute_of_billing_hour(created, at(7 + 53)), 53)
        self.assertEqual(pool.minute_of_billing_hour(created, at(7 + 60 + 2)), 2)

    def test_window_is_the_last_minutes_of_each_hour(self):
        self.assertFalse(pool.in_removal_window(T0, at(10)))
        self.assertTrue(pool.in_removal_window(T0, at(50)))
        self.assertTrue(pool.in_removal_window(T0, at(57)))
        self.assertFalse(pool.in_removal_window(T0, at(58)))
        self.assertTrue(pool.in_removal_window(T0, at(60 + 52)))


class Create(unittest.TestCase):
    def test_idle_runners_are_reused_before_anything_is_created(self):
        self.assertEqual(pool.servers_to_create(queued=2, idle_slots=2, booting_slots=0, servers=1, max_servers=3, slots=2), 0)

    def test_booting_servers_count_as_capacity(self):
        self.assertEqual(pool.servers_to_create(queued=2, idle_slots=0, booting_slots=2, servers=1, max_servers=3, slots=2), 0)

    def test_unmet_demand_creates_whole_servers(self):
        self.assertEqual(pool.servers_to_create(queued=3, idle_slots=0, booting_slots=0, servers=0, max_servers=3, slots=2), 2)

    def test_never_past_the_maximum(self):
        self.assertEqual(pool.servers_to_create(queued=9, idle_slots=0, booting_slots=0, servers=2, max_servers=3, slots=2), 1)

    def test_nothing_queued_creates_nothing(self):
        self.assertEqual(pool.servers_to_create(queued=0, idle_slots=0, booting_slots=0, servers=0, max_servers=3, slots=2), 0)


class Remove(unittest.TestCase):
    def test_idle_early_in_its_hour_is_kept_for_reuse(self):
        self.assertFalse(pool.should_remove(T0, at(12), busy=False, queued=0, runners_online=2))

    def test_idle_at_the_end_of_its_hour_goes(self):
        self.assertTrue(pool.should_remove(T0, at(52), busy=False, queued=0, runners_online=2))

    def test_busy_never_goes(self):
        self.assertFalse(pool.should_remove(T0, at(52), busy=True, queued=0, runners_online=2))

    def test_jobs_waiting_keep_a_healthy_server(self):
        self.assertFalse(pool.should_remove(T0, at(52), busy=False, queued=1, runners_online=2))

    def test_a_server_whose_runners_never_came_up_goes_anyway(self):
        self.assertTrue(pool.should_remove(T0, at(52), busy=False, queued=1, runners_online=0))


class MayReap(unittest.TestCase):
    def test_every_runner_idle_goes(self):
        self.assertTrue(pool.may_reap([False, False]))

    def test_a_busy_runner_keeps_the_server(self):
        self.assertFalse(pool.may_reap([False, True]))

    def test_an_unknown_busy_state_keeps_the_server(self):
        self.assertFalse(pool.may_reap([False, None]))


class FakePool(pool.Pool):
    """A Pool whose two APIs are recorded and answered from a table instead of the network."""

    def __init__(self, runner_answers, deregister_errors=()):
        self.org, self.dry = "org", False
        self.runner_answers = runner_answers      # runner id -> dict, or an exception to raise
        self.deregister_errors = dict(deregister_errors)
        self.calls = []

    def gh(self, path, method="GET", body=None):
        self.calls.append((method, path))
        rid = int(path.rsplit("/", 1)[1])
        if method == "DELETE":
            if rid in self.deregister_errors:
                raise http_error(self.deregister_errors[rid])
            return {}
        answer = self.runner_answers[rid]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def hc(self, path, method="GET", body=None):
        self.calls.append((method, path))
        return {}


def http_error(code):
    return urllib.error.HTTPError("https://api.github.com/x", code, "", {}, None)


SERVER = {"id": 7, "name": "pool-1", "created": "2026-10-13T18:00:00Z"}
RUNNERS = [{"id": 1, "name": "pool-1-1"}, {"id": 2, "name": "pool-1-2"}]


class SweepRemove(unittest.TestCase):
    """The sweep's busy list is read before the queued-jobs scan, which can take minutes; a job
    that starts in between must still keep its server."""

    def setUp(self):
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def server_deleted(self, p):
        return ("DELETE", "/servers/7") in p.calls

    def test_a_runner_that_became_busy_since_the_sweep_keeps_the_server(self):
        p = FakePool({1: {"busy": False}, 2: {"busy": True}})
        self.assertFalse(p.remove(SERVER, RUNNERS))
        self.assertFalse(self.server_deleted(p))
        self.assertFalse(any(m == "DELETE" for m, _ in p.calls))

    def test_idle_runners_are_deregistered_and_the_server_goes(self):
        p = FakePool({1: {"busy": False}, 2: {"busy": False}})
        self.assertTrue(p.remove(SERVER, RUNNERS))
        self.assertIn(("DELETE", "/orgs/org/actions/runners/1"), p.calls)
        self.assertIn(("DELETE", "/orgs/org/actions/runners/2"), p.calls)
        self.assertTrue(self.server_deleted(p))

    def test_an_unreadable_busy_state_keeps_the_server(self):
        for answer in (http_error(500), urllib.error.URLError("timeout"), {"status": "online"}):
            p = FakePool({1: {"busy": False}, 2: answer})
            self.assertFalse(p.remove(SERVER, RUNNERS), answer)
            self.assertFalse(self.server_deleted(p), answer)

    def test_a_runner_that_no_longer_exists_holds_no_job(self):
        p = FakePool({1: {"busy": False}, 2: http_error(404)})
        self.assertTrue(p.remove(SERVER, RUNNERS))
        self.assertTrue(self.server_deleted(p))

    def test_a_runner_github_refuses_to_deregister_keeps_the_server(self):
        p = FakePool({1: {"busy": False}, 2: {"busy": False}}, deregister_errors={2: 422})
        self.assertFalse(p.remove(SERVER, RUNNERS))
        self.assertFalse(self.server_deleted(p))


class DropOrphans(unittest.TestCase):
    def setUp(self):
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_an_offline_runner_of_a_deleted_server_is_deregistered(self):
        p = FakePool({})
        p.drop_orphans([{"id": 1, "name": "pool-1-1", "status": "offline"},
                        {"id": 2, "name": "pool-2-1", "status": "offline"},
                        {"id": 3, "name": "pool-3-1", "status": "online"}], {"pool-2"})
        self.assertEqual(p.calls, [("DELETE", "/orgs/org/actions/runners/1")])

    def test_a_lost_job_github_still_counts_as_running_is_no_error(self):
        # A runner whose server went away mid-job stays busy until GitHub fails that job.
        p = FakePool({}, deregister_errors={1: 422})
        p.drop_orphans([{"id": 1, "name": "pool-1-1", "status": "offline"},
                        {"id": 2, "name": "pool-2-1", "status": "offline"}], set())
        self.assertIn(("DELETE", "/orgs/org/actions/runners/2"), p.calls)

    def test_other_errors_still_fail_the_run(self):
        p = FakePool({}, deregister_errors={1: 500})
        with self.assertRaises(urllib.error.HTTPError):
            p.drop_orphans([{"id": 1, "name": "pool-1-1", "status": "offline"}], set())


class StrayBake(unittest.TestCase):
    def test_a_bake_within_its_workflow_timeout_is_left_alone(self):
        self.assertFalse(pool.stray_bake(T0, at(40)))

    def test_a_bake_server_older_than_any_bake_workflow_goes(self):
        self.assertTrue(pool.stray_bake(T0, at(46)))


if __name__ == "__main__":
    unittest.main()
