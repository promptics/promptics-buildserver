import datetime as dt
import os
import sys
import unittest

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


if __name__ == "__main__":
    unittest.main()
