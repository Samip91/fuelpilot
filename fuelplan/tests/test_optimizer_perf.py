"""TC-44 (NFR-2): the optimiser stays well inside its regression bound on 600 candidates.

The one deliberate real-clock test (CODING_STANDARDS §8 exception): it only asserts an
upper bound with large headroom, and the timings go into the assertion message.
"""

import statistics
import time

from fuelplan.optimizer import FuelGap, FuelPlan, plan_fuel_stops
from fuelplan.tests import optimizer_reference as ref
from fuelplan.tests.base import OfflineTestCase

RUNS = 5
LIMIT_MS = 2_000


def timed_plan(instance: ref.Instance) -> tuple[float, FuelPlan | FuelGap]:
    """Wall time of one optimiser call in milliseconds; the instance is built beforehand."""
    started = time.perf_counter()
    result = plan_fuel_stops(**instance.as_kwargs())
    return (time.perf_counter() - started) * 1000, result


class OptimizerSpeedTests(OfflineTestCase):
    def test_600_candidates_over_2800_miles_plan_in_under_two_seconds(self) -> None:
        instance = ref.perf_instance()

        runs = [timed_plan(instance) for _ in range(RUNS)]

        timings_ms = [round(ms, 1) for ms, _ in runs]
        message = f"runs {timings_ms} ms, median {statistics.median(timings_ms)} ms"
        self.assertTrue(all(isinstance(result, FuelPlan) for _, result in runs), message)
        self.assertLess(max(timings_ms), LIMIT_MS, message)

    def test_infeasible_600_candidate_trip_reports_its_gap_in_under_two_seconds(self) -> None:
        # Start 45 gal so all three hint passes run (current < 50 and reserve > 0).
        instance = ref.perf_instance(empty_from_mile=1400, empty_to_mile=1960)

        elapsed_ms, result = timed_plan(instance)

        self.assertEqual(len(instance.candidates), 600)
        self.assertIsInstance(result, FuelGap)
        self.assertLess(elapsed_ms, LIMIT_MS, f"gap search + 3 hint passes {elapsed_ms:.1f} ms")
