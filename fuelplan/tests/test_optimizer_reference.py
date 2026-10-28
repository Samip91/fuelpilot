"""Seeded comparisons against the references (spec 01 TC-15, TC-18 properties, NFR-4).

Seeds, mismatch counts and the feasibility mix go into assertion messages (no print).
"""

from collections import Counter
from decimal import Decimal

from fuelplan.optimizer import (
    ExactPlan,
    FuelGap,
    FuelPlan,
    find_cheapest_exact_plan,
    plan_fuel_stops,
)
from fuelplan.tests import optimizer_reference as ref
from fuelplan.tests.base import OfflineTestCase

GENERAL_COUNT = 500
DROP_FOCUSED_COUNT = 200
PLAIN_DP_COUNT = 100
ANY_AMOUNT_COUNT = 300
MIN_MULTI_STOP_SHARE = Decimal("0.40")


def as_reference(exact: ExactPlan | None) -> ref.ReferencePlan | None:
    """The DP's exact plan in the references' terms: cost and (mile, id) sequence."""
    if exact is None:
        return None
    return ref.ReferencePlan(
        cost=exact.cost,
        sequence=tuple((s.candidate.route_mile, s.candidate.station_id) for s in exact.stops),
    )


class _SeededSet:
    """One seeded instance set, solved once and shared by the tests of a class."""

    def __init__(self, instances: list[ref.Instance]) -> None:
        self.instances = instances
        self.exact = [find_cheapest_exact_plan(**i.as_kwargs()) for i in instances]
        self.results = [plan_fuel_stops(**i.as_kwargs()) for i in instances]

    def feasible(self) -> list[tuple[ref.Instance, FuelPlan, ExactPlan]]:
        return [
            (instance, result, exact)
            for instance, result, exact in zip(
                self.instances, self.results, self.exact, strict=True
            )
            if isinstance(result, FuelPlan) and exact is not None
        ]

    def replay_violations(self) -> dict[int, list[str]]:
        """Seed -> problems, for every feasible plan that breaks a replay property."""
        return {
            instance.seed: problems
            for instance, plan, exact in self.feasible()
            if (problems := ref.replay_violations(instance, plan, exact))
        }


class BruteForceReferenceTests(OfflineTestCase):
    """TC-15: the sped-up DP equals Ref A on 500 seeded instances (n <= 8)."""

    seeded: _SeededSet

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.seeded = _SeededSet(
            [ref.general_instance(ref.GENERAL_SEED + k) for k in range(GENERAL_COUNT)]
        )

    def test_dp_matches_brute_force_on_cost_sequence_and_feasibility(self) -> None:
        mismatched_seeds = [
            instance.seed
            for instance, exact in zip(self.seeded.instances, self.seeded.exact, strict=True)
            if as_reference(exact) != ref.brute_force_plan(instance)
        ]

        self.assertEqual(
            mismatched_seeds, [], f"Ref A mismatches: {len(mismatched_seeds)} of {GENERAL_COUNT}"
        )

    def test_instance_mix_has_at_least_40_percent_feasible_with_two_or_more_stops(self) -> None:
        mix = Counter(
            "infeasible"
            if exact is None
            else ("0 stops", "1 stop", "2+ stops")[min(len(exact.stops), 2)]
            for exact in self.seeded.exact
        )

        share = Decimal(mix["2+ stops"]) / GENERAL_COUNT
        self.assertGreaterEqual(share, MIN_MULTI_STOP_SHARE, f"mix {dict(mix)}")

    def test_every_exact_purchase_is_positive(self) -> None:
        bad_seeds = [
            instance.seed
            for instance, exact in zip(self.seeded.instances, self.seeded.exact, strict=True)
            if exact is not None and any(stop.gallons <= 0 for stop in exact.stops)
        ]

        self.assertEqual(bad_seeds, [])

    def test_gap_is_returned_exactly_when_the_dp_finds_no_plan(self) -> None:
        bad_seeds = [
            instance.seed
            for instance, exact, result in zip(
                self.seeded.instances, self.seeded.exact, self.seeded.results, strict=True
            )
            if (exact is None) != isinstance(result, FuelGap)
            or (isinstance(result, FuelGap) and not result.gap_leg_miles > result.max_leg_miles)
        ]

        self.assertEqual(bad_seeds, [])

    def test_hint_names_only_settings_the_reference_finds_feasible(self) -> None:
        gaps = [
            (instance, result)
            for instance, result in zip(self.seeded.instances, self.seeded.results, strict=True)
            if isinstance(result, FuelGap)
        ]
        wrong = [
            (instance.seed, result.hint.value)
            for instance, result in gaps
            if result.hint.value != ref.expected_hint(instance)
        ]

        self.assertEqual(wrong, [], f"{len(wrong)} wrong hints among {len(gaps)} gaps")

    def test_replay_properties(self) -> None:
        violations = self.seeded.replay_violations()

        self.assertEqual(violations, {}, f"checked {len(self.seeded.feasible())} feasible plans")


class DropFocusedReplayTests(OfflineTestCase):
    """TC-18 drop branch: 200 instances built so whole-gallon rounding often merges a stop."""

    seeded: _SeededSet

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.seeded = _SeededSet(
            [
                ref.drop_focused_instance(ref.DROP_FOCUSED_SEED + k)
                for k in range(DROP_FOCUSED_COUNT)
            ]
        )

    def test_merged_plans_keep_every_replay_property(self) -> None:
        feasible = self.seeded.feasible()
        merged = [plan for _, plan, _ in feasible if plan.has_merged_leg]
        violations = self.seeded.replay_violations()

        self.assertGreater(len(merged), 0, f"no merge among {len(feasible)} feasible plans")
        self.assertEqual(violations, {}, f"{len(merged)} merged of {len(feasible)} feasible")

    def test_dp_matches_brute_force_on_the_drop_focused_set(self) -> None:
        mismatched_seeds = [
            instance.seed
            for instance, exact in zip(self.seeded.instances, self.seeded.exact, strict=True)
            if as_reference(exact) != ref.brute_force_plan(instance)
        ]

        self.assertEqual(mismatched_seeds, [])


class PlainDpComparisonTests(OfflineTestCase):
    """Owner 2026-10-28 (replaces Ref B): the speed-up equals a plain DP for n 20-60."""

    def test_sped_up_dp_matches_plain_dp_on_100_larger_instances(self) -> None:
        instances = [ref.plain_dp_instance(ref.PLAIN_DP_SEED + k) for k in range(PLAIN_DP_COUNT)]

        mismatched_seeds = [
            instance.seed
            for instance in instances
            if as_reference(find_cheapest_exact_plan(**instance.as_kwargs()))
            != ref.plain_dp_plan(instance)
        ]
        multi_stop = sum(
            1 for i in instances if (plan := ref.plain_dp_plan(i)) and len(plan.sequence) >= 2
        )

        self.assertEqual(
            mismatched_seeds,
            [],
            f"plain-DP mismatches: {len(mismatched_seeds)} of {PLAIN_DP_COUNT}",
        )
        self.assertGreaterEqual(multi_stop, PLAIN_DP_COUNT // 2, f"{multi_stop} with >= 2 stops")


class AnyAmountReferenceTests(OfflineTestCase):
    """Auditor M1: the fill / just-enough rule loses nothing against free purchase amounts."""

    def test_dp_cost_and_feasibility_match_the_any_amount_optimum(self) -> None:
        instances = [
            ref.whole_mile_instance(ref.ANY_AMOUNT_SEED + k) for k in range(ANY_AMOUNT_COUNT)
        ]

        results = [
            (
                instance.seed,
                find_cheapest_exact_plan(**instance.as_kwargs()),
                ref.any_amount_cost(instance),
            )
            for instance in instances
        ]
        mismatched_seeds = [
            seed for seed, exact, cost in results if (None if exact is None else exact.cost) != cost
        ]
        with_stops = sum(1 for _, exact, _ in results if exact is not None and exact.stops)

        self.assertEqual(
            mismatched_seeds,
            [],
            f"any-amount mismatches: {len(mismatched_seeds)} of {ANY_AMOUNT_COUNT}",
        )
        self.assertGreaterEqual(with_stops, ANY_AMOUNT_COUNT // 2, f"{with_stops} plans with stops")
