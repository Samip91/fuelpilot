"""Spec 01 TC-37 (NFR-3, AC-33): the shared provider slot keeps calls ≥ 1 s apart.

Two `ProviderThrottle` instances on one test database stand in for two workers; each
has its own fake clock, so time moves only when a test (or a fake sleep) moves it.
"""

from unittest import mock

from django.conf import settings
from django.db import OperationalError

from fuelplan.errors import InternalError
from fuelplan.models import ProviderSlot
from fuelplan.routing import SLOT_MAX_WAIT_S, ProviderThrottle

from .base import FakeClock, OfflineTestCase

T = 1_000.0
MAX_SLEEPS_IN_2_S = 10  # a spin loop would sleep far more often


def _slot() -> float:
    return ProviderSlot.objects.get(pk="osrm").next_allowed_at


def _set_slot(next_allowed_at: float) -> None:
    ProviderSlot.objects.filter(pk="osrm").update(next_allowed_at=next_allowed_at)


class StaleClock(FakeClock):
    """B's clock: on its first read, worker A takes the slot, then B sees a slightly old `now`."""

    def __init__(self, *, now: float, before_first_read: ProviderThrottle) -> None:
        super().__init__(now=now)
        self._other = before_first_read
        self._has_read = False

    def now(self) -> float:
        if not self._has_read:
            self._has_read = True
            self._other.try_acquire()
        return super().now()


class ProviderThrottleTests(OfflineTestCase):
    def test_an_uncontended_grant_is_one_conditional_update(self) -> None:
        throttle = ProviderThrottle(clock=FakeClock(now=T))

        with self.assertNumQueries(1):
            granted = throttle.try_acquire()

        self.assertTrue(granted)
        self.assertEqual(_slot(), T + 1.0)

    def test_grants_are_at_least_one_second_apart(self) -> None:
        clock_a, clock_b = FakeClock(now=T), FakeClock(now=T + 0.4)
        worker_a, worker_b = ProviderThrottle(clock=clock_a), ProviderThrottle(clock=clock_b)

        self.assertTrue(worker_a.try_acquire())
        self.assertTrue(worker_b.try_acquire())

        self.assertGreaterEqual(clock_b.now() - T, 1.0)
        self.assertAlmostEqual(sum(clock_b.sleeps), 0.6)

    def test_a_waiter_with_a_stale_now_does_not_get_the_slot(self) -> None:
        worker_a = ProviderThrottle(clock=FakeClock(now=T))
        clock_b = StaleClock(now=T - 0.01, before_first_read=worker_a)
        worker_b = ProviderThrottle(clock=clock_b)

        self.assertTrue(worker_b.try_acquire())

        # A got the slot at T; B had to wait until T + 1.0 instead of passing at T - 0.01.
        self.assertTrue(clock_b.sleeps)
        self.assertGreaterEqual(clock_b.now(), T + 1.0)
        self.assertGreaterEqual(_slot(), T + 2.0)

    def test_a_holder_longer_than_2_s_makes_the_other_fail_fast(self) -> None:
        clock = FakeClock(now=T)
        _set_slot(T + 2.5)

        granted = ProviderThrottle(clock=clock).try_acquire()

        self.assertFalse(granted)
        self.assertLessEqual(sum(clock.sleeps), SLOT_MAX_WAIT_S)
        self.assertEqual(_slot(), T + 2.5)

    def test_waits_up_to_2_s_are_granted_and_longer_ones_are_not(self) -> None:
        for offset_s, is_granted in ((1.99, True), (2.0, True), (2.01, False)):
            with self.subTest(offset_s=offset_s):
                clock = FakeClock(now=T)
                _set_slot(T + offset_s)

                self.assertEqual(ProviderThrottle(clock=clock).try_acquire(), is_granted)
                self.assertLessEqual(sum(clock.sleeps), SLOT_MAX_WAIT_S)
                self.assertLessEqual(len(clock.sleeps), MAX_SLEEPS_IN_2_S)

    def test_a_clock_step_back_of_more_than_31_s_heals_at_once(self) -> None:
        for offset_s, is_healed in ((31.0, False), (31.01, True), (100.0, True)):
            with self.subTest(offset_s=offset_s):
                clock = FakeClock(now=T)
                _set_slot(T + offset_s)

                granted = ProviderThrottle(clock=clock).try_acquire()

                self.assertEqual(granted, is_healed)
                if is_healed:
                    self.assertEqual((_slot(), clock.sleeps), (T + 1.0, []))

    def test_a_locked_database_counts_as_busy_until_the_deadline(self) -> None:
        clock = FakeClock(now=T)
        locked = OperationalError("database is locked")

        with (
            mock.patch("django.db.models.query.QuerySet.update", side_effect=locked),
            self.assertLogs("fuelplan.routing", "INFO"),
        ):
            granted = ProviderThrottle(clock=clock).try_acquire()

        self.assertFalse(granted)
        self.assertLessEqual(sum(clock.sleeps), SLOT_MAX_WAIT_S)
        self.assertLessEqual(len(clock.sleeps), MAX_SLEEPS_IN_2_S)

    def test_a_lock_that_clears_lets_the_claim_through(self) -> None:
        clock = FakeClock(now=T)
        replies = [OperationalError("database is locked"), 1]

        with (
            mock.patch("django.db.models.query.QuerySet.update", side_effect=replies),
            self.assertLogs("fuelplan.routing", "INFO"),
        ):
            granted = ProviderThrottle(clock=clock).try_acquire()

        self.assertTrue(granted)
        self.assertEqual(len(clock.sleeps), 1)

    def test_other_database_errors_are_not_hidden(self) -> None:
        broken = OperationalError("no such table: fuelplan_providerslot")

        with (
            mock.patch("django.db.models.query.QuerySet.update", side_effect=broken),
            self.assertRaises(OperationalError),
        ):
            ProviderThrottle(clock=FakeClock(now=T)).try_acquire()

    def test_a_missing_slot_row_is_an_internal_error_not_a_503(self) -> None:
        ProviderSlot.objects.filter(pk="osrm").delete()

        with self.assertLogs("fuelplan.routing", "ERROR"), self.assertRaises(InternalError):
            ProviderThrottle(clock=FakeClock(now=T)).try_acquire()

    def test_requests_are_not_wrapped_in_a_transaction(self) -> None:
        # The claim must commit at once, or other workers could not see it.
        self.assertFalse(settings.DATABASES["default"].get("ATOMIC_REQUESTS"))
