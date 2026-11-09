import contextlib
import datetime as dt
import io
import os
import re
import sys
import unittest
import unittest.mock
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
        self.assertEqual(pool.unmet_slots(queued=2, idle_slots=2, booting_slots=0), 0)

    def test_booting_servers_count_as_capacity(self):
        self.assertEqual(pool.unmet_slots(queued=2, idle_slots=0, booting_slots=2), 0)

    def test_unmet_demand_is_counted_in_job_slots(self):
        self.assertEqual(pool.unmet_slots(queued=3, idle_slots=1, booting_slots=0), 2)

    def test_nothing_queued_creates_nothing(self):
        self.assertEqual(pool.unmet_slots(queued=0, idle_slots=0, booting_slots=0), 0)


def yaml_matrix(text):
    """The pool-scale matrix entries, read without a YAML library: `- pool:` starts an entry,
    `key: value` lines (comments and quotes stripped) fill it."""
    entries, cur, inside = [], None, False
    for raw in text.splitlines():
        line = raw.split(" #")[0].rstrip()
        if line.strip() == "include:":
            inside = True
            continue
        if not inside:
            continue
        if line.strip().startswith("steps:"):
            break
        stripped = line.strip()
        if stripped.startswith("- "):
            cur = {}
            entries.append(cur)
            stripped = stripped[2:]
        if cur is not None and ":" in stripped and not stripped.startswith("#"):
            k, v = stripped.split(":", 1)
            cur[k.strip()] = v.strip().strip('"')
    return entries


class Candidates(unittest.TestCase):
    """CANDIDATES names a slot count per type: type:location[:slots]."""

    def test_a_pair_without_slots_gets_the_pool_default(self):
        self.assertEqual(pool.parse_candidates("cx43:fsn1 cx53:nbg1:2", 1), [("cx43", "fsn1", 1), ("cx53", "nbg1", 2)])

    def test_every_pool_runs_one_job_per_server(self):
        """Two build jobs on one shared-vCPU server ran slower, flakier and dearer per job (docs/design.md)."""
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".github", "workflows", "pool-scale.yml")) as f:
            matrix = yaml_matrix(f.read())
        for entry in matrix:
            two = [c for c in pool.parse_candidates(entry["candidates"], int(entry["slots"])) if c[2] > 1]
            self.assertEqual([], two, entry["pool"])
        two_slot = next(e for e in matrix if e["pool"] == "promptics-pool-2slot")
        self.assertEqual(two_slot.get("image_pool"), "promptics-pool")

    def test_one_type_has_one_slot_count(self):
        with self.assertRaises(SystemExit):
            pool.parse_candidates("cx53:fsn1:2 cx53:nbg1:1", 1)

    def test_every_pool_wakes_the_scaler_through_the_webhook(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", ".github", "workflows", "pool-scale.yml")) as f:
            pools = {e["pool"] for e in yaml_matrix(f.read())}
        with open(os.path.join(here, "..", "webhook", "src", "index.js")) as f:
            line = next(l for l in f if l.startswith("const POOLS"))
        listed = set(re.findall(r'"([^"]+)"', line.split("//")[0]))
        self.assertEqual(pools, listed, "a pool missing from the webhook's POOLS waits for the cron backstop")

    def test_a_malformed_candidate_stops_the_controller(self):
        for bad in ("cx43", "cx43:fsn1:2:x", "cx43::2", "cx53:fsn1:0", "cx43:fsn1:x"):
            with self.assertRaises(SystemExit):
                pool.parse_candidates(bad, 1)



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


class IdleWindow(unittest.TestCase):
    """The sweep honours the reaper's pool-keep-until label."""

    def test_a_kept_server_stays_until_its_label_passes(self):
        later = int(at(52).timestamp()) + 60
        self.assertFalse(pool.should_remove(T0, at(52), busy=False, queued=0, runners_online=1, keep_until=later))

    def test_a_kept_server_whose_label_passed_goes(self):
        earlier = int(at(52).timestamp()) - 60
        self.assertTrue(pool.should_remove(T0, at(52), busy=False, queued=0, runners_online=1, keep_until=earlier))

    def test_a_broken_server_goes_despite_its_label(self):
        later = int(at(52).timestamp()) + 3600
        self.assertTrue(pool.should_remove(T0, at(52), busy=False, queued=0, runners_online=0, keep_until=later))

    def test_an_unlabelled_server_gets_a_few_minutes_for_its_reaper(self):
        self.assertFalse(pool.should_remove(T0, at(51), busy=False, queued=0, runners_online=1, idle_keep_min=240))
        self.assertTrue(pool.should_remove(T0, at(54), busy=False, queued=0, runners_online=1, idle_keep_min=240))

    def test_without_the_window_the_sweep_is_unchanged(self):
        self.assertTrue(pool.should_remove(T0, at(51), busy=False, queued=0, runners_online=1))

    def test_an_empty_setting_is_off(self):
        for value in ("", None):
            with unittest.mock.patch.dict(os.environ, {} if value is None else {"IDLE_KEEP_MIN": value}, clear=False):
                if value is None:
                    os.environ.pop("IDLE_KEEP_MIN", None)
                self.assertEqual(pool.idle_keep_min_from_env(), 0)
        with unittest.mock.patch.dict(os.environ, {"IDLE_KEEP_MIN": "240"}):
            self.assertEqual(pool.idle_keep_min_from_env(), 240)

    def test_an_empty_cap_is_no_cap(self):
        for value in ("", None):
            with unittest.mock.patch.dict(os.environ, {} if value is None else {"IDLE_KEEP_MAX": value}, clear=False):
                if value is None:
                    os.environ.pop("IDLE_KEEP_MAX", None)
                self.assertEqual(pool.idle_keep_max_from_env(), 0)
        with unittest.mock.patch.dict(os.environ, {"IDLE_KEEP_MAX": "2"}):
            self.assertEqual(pool.idle_keep_max_from_env(), 2)

    def test_the_label_is_read_from_the_server(self):
        self.assertEqual(pool.keep_until({"labels": {"pool": "p", "pool-keep-until": "123"}}), 123)
        self.assertIsNone(pool.keep_until({"labels": {"pool": "p"}}))
        self.assertIsNone(pool.keep_until({"labels": {"pool-keep-until": "soon"}}))


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


class CreateWithSshKey(unittest.TestCase):
    """Server creation carries the configured SSH key, or creates nothing."""

    def setUp(self):
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def make(self, key="ci-key", project_keys=("ci-key",)):
        calls = []

        class P(pool.Pool):
            def __init__(self):
                self.pool, self.org, self.slots, self.dry = "pool", "org", 1, False
                self.candidates = [("cx43", "fsn1")]
                self.hc_token = "token"
                self.ssh_key = key

            def gh(self, path, method="GET", body=None):
                calls.append((method, path, body))
                return {"token": "t"}

            def hc(self, path, method="GET", body=None):
                calls.append((method, path, body))
                if path.startswith("/ssh_keys"):
                    return {"ssh_keys": [{"name": k} for k in project_keys if f"name={k}" in path]}
                return {}

            def image(self):
                return 1

        return P(), calls

    def posts(self, calls):
        return [c for c in calls if c[0] == "POST" and c[1] == "/servers"]

    def test_the_key_is_in_the_create_body(self):
        p, calls = self.make()
        p.create(1, T0)
        posts = self.posts(calls)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0][2]["ssh_keys"], ["ci-key"])

    def test_an_unknown_key_creates_nothing_and_names_the_setting(self):
        p, calls = self.make(key="ci-key", project_keys=("other",))
        error = p.create(1, T0)
        self.assertIn("HCLOUD_SSH_KEY", error)
        self.assertEqual(self.posts(calls), [])
        self.assertFalse([c for c in calls if c[1].startswith("/orgs")])

    def test_a_missing_setting_refuses_to_create_but_never_blocks_a_sweep(self):
        with unittest.mock.patch.dict(os.environ, {
                "POOL": "pool", "ORG": "org", "CANDIDATES": "cx43:fsn1",
                "HCLOUD_TOKEN": "x", "GH_TOKEN": "y", "HCLOUD_SSH_KEY": ""}):
            self.assertEqual(pool.Pool().ssh_key, "")
        p, calls = self.make(key="")
        self.assertIn("HCLOUD_SSH_KEY", p.create(1, T0))
        self.assertEqual(self.posts(calls), [])

    def test_reconcile_cleans_up_before_failing_on_a_missing_key(self):
        calls = []

        class P(pool.Pool):
            def __init__(self):
                self.pool, self.org, self.slots, self.dry = "pool", "org", 1, False
                self.max_servers, self.candidates, self.ssh_key = 8, [("cx43", "fsn1")], ""
                self.hc_token = "token"

            def servers(self):
                return [SERVER]

            def bake_servers(self):
                return []

            def runners(self):
                return [{"id": 1, "name": "pool-1-1", "status": "offline", "busy": False}]

            def waiting_jobs(self, now):
                return [{"repo": "r", "run_id": 1, "job_id": i, "name": "j", "created_at": None}
                        for i in range(3)]

            def gh(self, path, method="GET", body=None):
                calls.append((method, path))
                return {"busy": False}

            def hc(self, path, method="GET", body=None):
                calls.append((method, path))
                return {}

        with unittest.mock.patch.object(pool, "utcnow", lambda: at(52)):
            with self.assertRaises(SystemExit) as cm:
                P().reconcile()
        self.assertIn("HCLOUD_SSH_KEY", str(cm.exception))
        self.assertIn(("DELETE", "/servers/7"), calls)
        self.assertFalse([c for c in calls if c == ("POST", "/servers")])


class SlotsPerType(unittest.TestCase):
    """A 2-slot candidate registers two runners, counts twice against the maximum, and a refused
    type falls back to the next candidate."""

    def make(self, refuse=(), servers=(), max_servers=8, one_job=False):
        posts = []

        class P(pool.Pool):
            def __init__(self):
                self.pool, self.org, self.slots, self.dry = "pool", "org", 1, False
                self.max_servers, self.ssh_key, self.hc_token = max_servers, "ci-key", "token"
                self.one_job = one_job
                self.repos, self.events = (["r"], ["push"]) if one_job else ([], [])
                self.candidates = pool.parse_candidates(
                    "cx43:fsn1 cx53:fsn1:2 cpx62:fsn1:2 cpx42:fsn1", 1)

            def ssh_key_error(self):
                return None

            def image(self):
                return 1

            def gh(self, path, method="GET", body=None):
                return {"token": "t"}

            def hc(self, path, method="GET", body=None):
                if method == "POST" and path == "/servers":
                    posts.append(body)
                    if body["server_type"] in refuse:
                        raise EveryCandidateRefused.refused("resource_unavailable")
                return {}

        return P(), posts

    @staticmethod
    def placed(posts, refuse):
        return [(p["server_type"], p["user_data"]) for p in posts if p["server_type"] not in refuse]

    def run_create(self, n, room=None, **kw):
        p, posts = self.make(**kw)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(p.create(n, T0, room=room))
        return p, posts

    def test_the_cheapest_type_comes_first_with_one_slot(self):
        _, posts = self.run_create(1)
        self.assertEqual([p["server_type"] for p in posts], ["cx43"])
        self.assertIn("SLOTS='1'", posts[0]["user_data"])

    def test_a_refused_type_falls_back_to_a_2_slot_server_that_registers_two_runners(self):
        refuse = ("cx43",)
        _, posts = self.run_create(2, refuse=refuse)
        self.assertEqual([p["server_type"] for p in posts], ["cx43", "cx53"])
        self.assertIn("SLOTS='2'", posts[-1]["user_data"])

    def test_two_unmet_jobs_need_one_2_slot_server(self):
        refuse = ("cx43", "cx53")
        _, posts = self.run_create(2, refuse=refuse)
        self.assertEqual([t for t, _ in self.placed(posts, refuse)], ["cpx62"])

    def test_the_dear_1_slot_type_comes_last(self):
        refuse = ("cx43", "cx53", "cpx62")
        _, posts = self.run_create(1, refuse=refuse)
        self.assertEqual([p["server_type"] for p in posts], ["cx43", "cx53", "cpx62", "cpx42"])
        self.assertIn("SLOTS='1'", posts[-1]["user_data"])

    def test_a_2_slot_candidate_is_skipped_when_the_maximum_leaves_one_slot(self):
        refuse = ("cx43",)
        _, posts = self.run_create(2, room=1, refuse=refuse)
        self.assertEqual([p["server_type"] for p in posts], ["cx43", "cpx42"])

    def test_a_one_job_server_has_one_slot_whatever_its_type(self):
        _, posts = self.run_create(1, refuse=("cx43",), one_job=True)
        self.assertEqual(posts[-1]["server_type"], "cx53")
        self.assertIn("SLOTS='1'", posts[-1]["user_data"])

    def test_a_one_job_pools_server_counts_one_slot_whatever_its_type(self):
        p, _ = self.make(one_job=True)
        self.assertEqual(p.slots_of({"server_type": {"name": "cx53"}}), 1)

    def test_no_room_creates_nothing(self):
        _, posts = self.run_create(3, room=0)
        self.assertEqual(posts, [])

    def test_room_that_no_candidate_fits_creates_nothing_and_says_so(self):
        p, posts = self.make()
        p.candidates = pool.parse_candidates("cx53:fsn1:2", 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(p.create(1, T0, room=1))
        self.assertEqual(posts, [])
        self.assertIn("which no candidate fits", out.getvalue())

    def test_a_young_2_slot_server_with_one_runner_online_still_books_its_second_slot(self):
        servers = [dict(SERVER, server_type={"name": "cpx62"}, name="pool-a", id=11)]
        p, posts = self.make(max_servers=8)
        p.servers = lambda: servers
        p.bake_servers = lambda: []
        p.runners = lambda: [{"id": 1, "name": "pool-a-1", "status": "online", "busy": True}]
        p.waiting_jobs = lambda now: [{"repo": "r", "run_id": 1, "job_id": 1, "name": "j", "created_at": None}]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with unittest.mock.patch.object(pool, "utcnow", lambda: T0 + dt.timedelta(minutes=2)):
                p.reconcile()
        self.assertIn("1 booting slot(s)", out.getvalue())
        self.assertEqual(posts, [], "the one queued job waits for the second runner")

    def test_a_server_counts_its_types_slots(self):
        p, _ = self.make()
        self.assertEqual(p.slots_of({"server_type": {"name": "cpx62"}}), 2)
        self.assertEqual(p.slots_of({"server_type": {"name": "cpx42"}}), 1)
        self.assertEqual(p.slots_of({"server_type": {"name": "ccx33"}}), 1, "a type no candidate names gets SLOTS")
        self.assertEqual(p.slots_of({}), 1)

    def test_reconcile_counts_a_2_slot_server_twice_against_the_maximum(self):
        # Three booting 2-slot servers hold 6 of 7 slots; 7 queued jobs leave 1 unmet, and the one
        # slot of room left fits only a 1-slot type: with cx43 sold out, cpx42, not a 2-slot cx53.
        servers = [dict(SERVER, server_type={"name": "cpx62"}, name=f"pool-{i}", id=10 + i) for i in range(3)]
        p, posts = self.make(max_servers=7, refuse=("cx43",))
        p.servers = lambda: servers
        p.bake_servers = lambda: []
        p.runners = lambda: []
        p.waiting_jobs = lambda now: [{"repo": "r", "run_id": 1, "job_id": i, "name": "j", "created_at": None}
                                      for i in range(7)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with unittest.mock.patch.object(pool, "utcnow", lambda: T0 + dt.timedelta(minutes=2)):
                p.reconcile()
        self.assertEqual([b["server_type"] for b in posts], ["cx43", "cpx42"])
        self.assertIn("6 booting slot(s)", out.getvalue())


class EveryCandidateRefused(unittest.TestCase):
    """A placement every candidate refuses, such as a project at its core limit, stops creating
    for this run with a warning; it never ends the run before its cleanup."""

    @staticmethod
    def refused(code="resource_limit_exceeded"):
        body = io.BytesIO(('{"error": {"code": "%s"}}' % code).encode())
        return urllib.error.HTTPError("https://api.example/servers", 403, "Forbidden", {}, body)

    def make(self, calls, servers=()):
        refused = self.refused

        class P(pool.Pool):
            def __init__(self):
                self.pool, self.org, self.slots, self.dry = "pool", "org", 1, False
                self.max_servers, self.ssh_key, self.hc_token = 8, "ci-key", "token"
                self.candidates = [("cx43", "fsn1"), ("cx43", "nbg1"), ("cpx42", "fsn1")]

            def ssh_key_error(self):
                return None

            def image(self):
                return 1

            def servers(self):
                return list(servers)

            def bake_servers(self):
                return []

            def runners(self):
                return [{"id": 1, "name": "pool-1-1", "status": "offline", "busy": False}]

            def waiting_jobs(self, now):
                return [{"repo": "r", "run_id": 1, "job_id": i, "name": "j", "created_at": None}
                        for i in range(3)]

            def gh(self, path, method="GET", body=None):
                calls.append((method, path))
                return {"token": "t", "busy": False}

            refusal_code, refusal_body = "resource_limit_exceeded", None

            def hc(self, path, method="GET", body=None):
                calls.append((method, path))
                if method == "POST" and path == "/servers":
                    if self.refusal_body is not None:
                        return_body = io.BytesIO(self.refusal_body)
                        raise urllib.error.HTTPError("https://api.example/servers", 403, "Forbidden", {}, return_body)
                    raise refused(self.refusal_code)
                return {}

        return P()

    def test_create_stops_after_one_server_is_refused_everywhere_and_warns(self):
        calls, out = [], io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertIsNone(self.make(calls).create(3, T0))
        posts = [c for c in calls if c == ("POST", "/servers")]
        self.assertEqual(len(posts), 3, "each candidate is tried once for the first server, then creating stops")
        self.assertIn("warning:", out.getvalue())
        self.assertIn("resource_limit_exceeded", out.getvalue())

    def test_a_refusal_that_is_not_about_capacity_fails_the_run_after_its_cleanup(self):
        calls = []
        p = self.make(calls, servers=[SERVER])
        p.refusal_code = "invalid_input"
        with contextlib.redirect_stdout(io.StringIO()):
            with unittest.mock.patch.object(pool, "utcnow", lambda: at(52)):
                with self.assertRaises(SystemExit) as cm:
                    p.reconcile()
        self.assertIn("invalid_input", str(cm.exception))
        self.assertIn(("DELETE", "/servers/7"), calls)

    def test_an_error_body_that_is_not_json_is_named_by_its_status(self):
        calls = []
        p = self.make(calls)
        p.refusal_body = b"<html>bad gateway</html>"
        with contextlib.redirect_stdout(io.StringIO()):
            error = p.create(1, T0)
        self.assertIn("403", error)

    def test_reconcile_still_cleans_up_and_does_not_fail(self):
        calls = []
        with contextlib.redirect_stdout(io.StringIO()):
            with unittest.mock.patch.object(pool, "utcnow", lambda: at(52)):
                self.make(calls, servers=[SERVER]).reconcile()
        self.assertIn(("DELETE", "/servers/7"), calls)


class OneJobPool(unittest.TestCase):
    """A one-job pool serves only the events and repositories it lists; the shared pool is unchanged."""

    EVENTS = ["schedule", "workflow_dispatch", "push", "workflow_run"]

    def test_a_pull_request_job_asks_for_no_one_job_server(self):
        self.assertFalse(pool.job_wanted("pull_request", ["self-hosted", "priv"], "priv", self.EVENTS))
        self.assertFalse(pool.job_wanted("pull_request_target", ["self-hosted", "priv"], "priv", self.EVENTS))
        self.assertFalse(pool.job_wanted("issue_comment", ["self-hosted", "priv"], "priv", self.EVENTS))

    def test_a_scheduled_or_manual_job_asks_for_one(self):
        self.assertTrue(pool.job_wanted("schedule", ["self-hosted", "priv"], "priv", self.EVENTS))
        self.assertTrue(pool.job_wanted("workflow_dispatch", ["self-hosted", "priv"], "priv", self.EVENTS))
        self.assertTrue(pool.job_wanted("push", ["self-hosted", "priv"], "priv", self.EVENTS))
        self.assertTrue(pool.job_wanted("workflow_run", ["self-hosted", "priv"], "priv", self.EVENTS))

    def test_the_privileged_pool_is_configured_with_exactly_these_events(self):
        path = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows", "pool-scale.yml")
        with open(path) as f:
            text = f.read()
        entry = text[text.index("- pool: promptics-privileged"):]
        line = next(l for l in entry.splitlines() if l.strip().startswith("events:"))
        events = line.split(":", 1)[1].split("#", 1)[0].strip().strip('"').split()
        self.assertEqual(sorted(events), sorted(self.EVENTS))

    def test_a_job_for_another_label_asks_for_nothing(self):
        self.assertFalse(pool.job_wanted("schedule", ["self-hosted", "promptics-pool"], "priv", self.EVENTS))

    def test_the_shared_pool_takes_every_event(self):
        self.assertTrue(pool.job_wanted("pull_request", ["self-hosted", "shared"], "shared", []))

    def test_a_one_job_pool_needs_both_lists(self):
        self.assertIn("REPOS", pool.one_job_config_error(True, [], self.EVENTS))
        self.assertIn("EVENTS", pool.one_job_config_error(True, ["repo"], []))
        self.assertIsNone(pool.one_job_config_error(True, ["repo"], self.EVENTS))
        self.assertIsNone(pool.one_job_config_error(False, [], []))

    def test_settings_come_from_the_environment(self):
        with unittest.mock.patch.dict(os.environ, {
                "POOL": "priv", "ORG": "org", "CANDIDATES": "cx43:fsn1", "HCLOUD_TOKEN": "x",
                "GH_TOKEN": "y", "ONE_JOB": "1", "EVENTS": "schedule workflow_dispatch",
                "REPOS": "a b", "IMAGE_POOL": "shared"}):
            p = pool.Pool()
        self.assertTrue(p.one_job)
        self.assertEqual(p.events, ["schedule", "workflow_dispatch"])
        self.assertEqual(p.repos, ["a", "b"])
        self.assertEqual(p.image_pool, "shared")

    def test_without_the_settings_a_pool_stays_shared(self):
        with unittest.mock.patch.dict(os.environ, {
                "POOL": "shared", "ORG": "org", "CANDIDATES": "cx43:fsn1", "HCLOUD_TOKEN": "x",
                "GH_TOKEN": "y", "ONE_JOB": "", "EVENTS": "", "IMAGE_POOL": ""}):
            p = pool.Pool()
        self.assertFalse(p.one_job)
        self.assertEqual(p.events, [])
        self.assertEqual(p.image_pool, "shared")

    def make(self, one_job, repos=("repo",), events=("schedule", "workflow_dispatch")):
        calls = []

        class P(pool.Pool):
            def __init__(self):
                self.pool, self.org, self.slots, self.dry = "priv", "org", 1, False
                self.candidates = [("cx43", "fsn1")]
                self.hc_token, self.ssh_key = "token", "ci-key"
                self.one_job, self.repos, self.events = one_job, list(repos), list(events)

            def gh(self, path, method="GET", body=None):
                calls.append((method, path, body))
                return {"token": "t"}

            def hc(self, path, method="GET", body=None):
                calls.append((method, path, body))
                if path.startswith("/ssh_keys"):
                    return {"ssh_keys": [{"name": "ci-key"}]}
                return {}

            def image(self):
                return 1

        quiet = contextlib.redirect_stdout(io.StringIO())
        with quiet:
            error = P().create(1, T0)
        posts = [c[2] for c in calls if c[0] == "POST" and c[1] == "/servers"]
        return error, posts

    def test_a_one_job_server_carries_its_guard_and_lists(self):
        error, posts = self.make(one_job=True)
        self.assertIsNone(error)
        user_data = posts[0]["user_data"]
        self.assertIn("export ONE_JOB='1' IDLE_KEEP_MIN='0' IDLE_KEEP_MAX='0'", user_data)
        self.assertIn("export EVENTS='schedule workflow_dispatch' ALLOWED_REPOS='repo'", user_data)
        self.assertIn(f"cat > {pool.GUARD_BIN} <<'GUARD'", user_data)
        self.assertIn(f"export POOL_GUARD_BIN='{pool.GUARD_BIN}'", user_data)
        self.assertEqual(posts[0]["labels"], {"pool": "priv"})

    def test_the_hook_path_is_one_the_runner_runs(self):
        # The runner refuses a hook whose path does not end in .sh, .ps1 or .js.
        self.assertTrue(pool.GUARD_BIN.endswith((".sh", ".ps1", ".js")), pool.GUARD_BIN)

    def test_a_one_job_pool_without_repos_creates_nothing(self):
        error, posts = self.make(one_job=True, repos=())
        self.assertIn("REPOS", error)
        self.assertEqual(posts, [])

    def test_a_shared_server_gets_no_guard(self):
        error, posts = self.make(one_job=False, repos=(), events=())
        self.assertIsNone(error)
        user_data = posts[0]["user_data"]
        self.assertIn("export ONE_JOB='0'", user_data)
        self.assertNotIn(f"cat > {pool.GUARD_BIN}", user_data)
        self.assertNotIn("export EVENTS=", user_data)

    def test_a_pull_request_run_is_never_counted_for_a_one_job_pool(self):
        p = FakeRunsPool(["schedule", "workflow_dispatch"], [[
            {"id": 1, "event": "pull_request", "status": "queued"},
            {"id": 2, "event": "schedule", "status": "queued"},
        ]], {1: [QUEUED_JOB], 2: [QUEUED_JOB]})
        self.assertEqual(p.queued_jobs(T0), 1)
        self.assertNotIn(1, p.jobs_read)


QUEUED_JOB = {"id": 10, "name": "smoke", "status": "queued", "labels": ["self-hosted", "priv"],
              "created_at": "2026-10-13T17:50:00Z"}


class FakeRunsPool(pool.Pool):
    """A Pool that answers the runs list from pages and each run's jobs from a table. Like GitHub
    can, it answers the runs list's status filter with nothing unless `by_status` has runs for it
    (as for a re-run of an old run, which the created window leaves out)."""

    def __init__(self, events, pages, jobs, repos=("repo",), by_status=None):
        self.by_status = by_status or {}
        self.pool, self.org, self.events, self.repos = "priv", "org", list(events), list(repos)
        self.pages, self.jobs = pages, jobs
        self.list_calls, self.jobs_read = [], []

    def gh(self, path, method="GET", body=None):
        if "/actions/runs?" in path:
            self.list_calls.append(path)
            if "status=" in path:
                pages = self.by_status.get(path.split("status=")[1].split("&")[0], [])
                page = int(path.rsplit("page=", 1)[1])
                return {"workflow_runs": pages[page - 1] if page <= len(pages) else []}
            page = int(path.rsplit("page=", 1)[1])
            return {"workflow_runs": self.pages[page - 1] if page <= len(self.pages) else []}
        run_id = int(path.split("/actions/runs/")[1].split("/")[0])
        self.jobs_read.append(run_id)
        return {"jobs": self.jobs.get(run_id, [])}


def runs(n, status="completed", start=0):
    return [{"id": start + i, "event": "push", "status": status} for i in range(n)]


class QueuedJobs(unittest.TestCase):
    def setUp(self):
        quiet = contextlib.redirect_stdout(io.StringIO())
        self.out = quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    def test_a_queued_run_the_status_filter_leaves_out_is_still_counted(self):
        p = FakeRunsPool(["push"], [[{"id": 5, "event": "push", "status": "queued"}]],
                         {5: [QUEUED_JOB]})
        self.assertEqual(p.queued_jobs(T0), 1)

    def test_a_rerun_of_a_run_older_than_the_window_is_counted(self):
        old = {"id": 77, "event": "push", "status": "queued", "created_at": "2026-10-01T10:00:00Z",
               "updated_at": "2026-10-13T17:50:00Z"}
        p = FakeRunsPool(["push"], [[]], {77: [QUEUED_JOB]}, by_status={"queued": [[old]]})
        self.assertEqual(p.queued_jobs(T0), 1)
        self.assertEqual(p.jobs_read, [77])

    def test_an_in_progress_rerun_older_than_the_window_with_a_queued_job_is_counted(self):
        old = {"id": 78, "event": "push", "status": "in_progress", "created_at": "2026-10-01T10:00:00Z",
               "updated_at": "2026-10-13T17:50:00Z"}
        jobs = [dict(QUEUED_JOB, id=1, status="completed"), QUEUED_JOB]
        p = FakeRunsPool(["push"], [[]], {78: jobs}, by_status={"in_progress": [[old]]})
        self.assertEqual(p.queued_jobs(T0), 1)
        self.assertEqual(p.jobs_read, [78])

    def test_a_run_in_both_sources_is_read_once(self):
        run = {"id": 5, "event": "push", "status": "queued"}
        p = FakeRunsPool(["push"], [[run]], {5: [QUEUED_JOB]}, by_status={"queued": [[run]]})
        self.assertEqual(p.queued_jobs(T0), 1)
        self.assertEqual(p.jobs_read, [5])

    def test_a_listed_run_that_is_not_active_or_not_an_event_is_ignored(self):
        done = {"id": 1, "event": "push", "status": "completed"}
        pr = {"id": 2, "event": "pull_request", "status": "queued"}
        p = FakeRunsPool(["push"], [[]], {1: [QUEUED_JOB], 2: [QUEUED_JOB]},
                         by_status={"queued": [[done, pr]]})
        self.assertEqual(p.queued_jobs(T0), 0)

    def test_a_rerun_on_a_later_page_of_the_status_list_is_found(self):
        old = {"id": 7777, "event": "push", "status": "queued", "created_at": "2026-10-01T10:00:00Z",
               "updated_at": "2026-10-13T17:50:00Z"}
        p = FakeRunsPool([], [[]], {7777: [QUEUED_JOB]},
                         by_status={"queued": [runs(100, status="queued"), runs(100, status="queued", start=100), [old]]})
        p.queued_jobs(T0)
        self.assertIn(7777, p.jobs_read)

    def test_list_calls_per_repo_stay_bounded(self):
        p = FakeRunsPool([], [runs(100, start=100 * k) for k in range(10)], {})
        p.queued_jobs(T0)
        self.assertLessEqual(len(p.list_calls), pool.RUN_PAGES * (1 + len(pool.RERUN_STATUSES)))

    def test_the_list_is_bounded_to_the_last_day(self):
        p = FakeRunsPool([], [[]], {})
        p.queued_jobs(T0)
        self.assertIn("created=%3E%3D2026-10-12T18%3A00%3A00Z", p.list_calls[0])

    def test_every_active_status_counts_and_finished_runs_are_not_read(self):
        page = [{"id": i, "event": "push", "status": s} for i, s in
                enumerate(["queued", "in_progress", "waiting", "pending", "completed"])]
        p = FakeRunsPool([], [page], {i: [QUEUED_JOB] for i in range(5)})
        self.assertEqual(p.queued_jobs(T0), 4)
        self.assertEqual(sorted(p.jobs_read), [0, 1, 2, 3])

    def test_only_queued_jobs_for_this_label_count(self):
        jobs = [QUEUED_JOB, dict(QUEUED_JOB, status="in_progress"),
                dict(QUEUED_JOB, labels=["self-hosted", "other"])]
        p = FakeRunsPool([], [[{"id": 1, "event": "push", "status": "in_progress"}]], {1: jobs})
        self.assertEqual(p.queued_jobs(T0), 1)

    def test_pages_are_followed_until_a_short_one(self):
        p = FakeRunsPool([], [runs(100), runs(100, start=100), [{"id": 999, "event": "push", "status": "queued"}]],
                         {999: [QUEUED_JOB]})
        self.assertEqual(p.queued_jobs(T0), 1)
        self.assertEqual(len([c for c in p.list_calls if "created=" in c]), 3)

    def test_the_run_list_stops_at_its_page_cap(self):
        p = FakeRunsPool([], [runs(100, start=100 * k) for k in range(pool.RUN_PAGES + 2)], {})
        p.queued_jobs(T0)
        self.assertEqual(len([c for c in p.list_calls if "created=" in c]), pool.RUN_PAGES)
        self.assertIn("older runs were not read", self.out.getvalue())

    def test_jobs_are_read_for_at_most_the_cap_of_active_runs(self):
        p = FakeRunsPool([], [runs(pool.MAX_ACTIVE_RUNS + 10, status="queued")], {})
        p.queued_jobs(T0)
        self.assertEqual(len(p.jobs_read), pool.MAX_ACTIVE_RUNS)
        self.assertEqual(p.jobs_read[0], 0)   # newest first, as GitHub lists them
        self.assertIn("active runs", self.out.getvalue())

    def test_each_watched_repo_is_read(self):
        p = FakeRunsPool([], [[{"id": 1, "event": "push", "status": "queued"}]], {1: [QUEUED_JOB]},
                         repos=("a", "b"))
        self.assertEqual(p.queued_jobs(T0), 2)
        self.assertEqual(sorted({c.split("/")[3] for c in p.list_calls}), ["a", "b"])


class StaleJobs(unittest.TestCase):
    def job(self, minutes_ago):
        created = (T0 - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"repo": "repo", "run_id": 1, "job_id": minutes_ago, "name": "smoke", "created_at": created}

    def test_only_jobs_queued_longer_than_the_limit_are_stale(self):
        stale = pool.stale_jobs([self.job(10), self.job(45), self.job(180), self.job(30)], T0)
        self.assertEqual([j["job_id"] for j in stale], [180, 45])

    def test_a_job_without_a_creation_time_is_not_stale(self):
        self.assertEqual(pool.stale_jobs([dict(self.job(90), created_at=None)], T0), [])

    def test_reconcile_names_a_job_it_still_cannot_serve(self):
        job = self.job(180)

        class P(pool.Pool):
            def __init__(self):
                self.pool, self.org, self.slots, self.dry = "priv", "org", 1, True
                self.max_servers = 1

            def servers(self):
                return [{"id": 7, "name": "priv-1", "created": "2026-10-13T17:55:00Z"}]

            def bake_servers(self):
                return []

            def runners(self):
                return []

            def waiting_jobs(self, now):
                return [job]

        out = io.StringIO()
        with unittest.mock.patch.object(pool, "utcnow", lambda: T0), contextlib.redirect_stdout(out):
            P().reconcile()
        self.assertIn("warning: job 180 (smoke) of org/repo run 1 has been queued for 180 min", out.getvalue())

if __name__ == "__main__":
    unittest.main()
