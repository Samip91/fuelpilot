"""Hand-derived optimiser cases (spec 01 TC-14 leg clause, TC-16, TC-17 unit half, TC-18, TC-41).

Expected values come from the senior-QA kickoff for step 2A, derived by hand from §15.
Defaults unless a case says otherwise: d = 0, every stop unverified (1 mi margin per stop
end), start 50 gal, reserve 5 gal, so F = 450 and U = 440 planned miles.
Every case is also solved with reversed and shuffled input and must give the same result.
"""

import ast
import random
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from unittest import mock

from fuelplan import optimizer
from fuelplan.optimizer import (
    Candidate,
    ExactPlan,
    FeasibilityHint,
    FuelGap,
    FuelPlan,
    OptimizerError,
    find_cheapest_exact_plan,
    plan_fuel_stops,
    stop_margin_miles,
)
from fuelplan.tests import optimizer_reference as ref
from fuelplan.tests.base import OfflineTestCase

D = Decimal
H = FeasibilityHint


def station(
    station_id: int, mile: str, price: str = "3.000", *, detour: str = "0", verified: bool = False
) -> Candidate:
    """A candidate built from strings, never from floats."""
    return Candidate(
        station_id=station_id,
        route_mile=D(mile),
        detour_miles=D(detour),
        price_per_gallon=D(price),
        is_detour_verified=verified,
    )


class OptimizerCase(OfflineTestCase):
    """Helpers that solve each case in three input orders and insist on one answer."""

    def solve(
        self,
        route_miles: str,
        stations: list[Candidate],
        *,
        start: str = "50",
        reserve: str = "5",
    ) -> FuelPlan | FuelGap:
        orders = [stations, stations[::-1], random.Random(7).sample(stations, len(stations))]
        results = [
            plan_fuel_stops(
                route_miles=D(route_miles),
                candidates=order,
                start_gallons=D(start),
                reserve_gallons=D(reserve),
            )
            for order in orders
        ]
        self.assertEqual(results[1], results[0], "reversed input changed the result")
        self.assertEqual(results[2], results[0], "shuffled input changed the result")
        return results[0]

    def solve_exact(
        self,
        route_miles: str,
        stations: list[Candidate],
        *,
        start: str = "50",
        reserve: str = "5",
    ) -> ExactPlan | None:
        orders = [stations, stations[::-1], random.Random(7).sample(stations, len(stations))]
        results = [
            find_cheapest_exact_plan(
                route_miles=D(route_miles),
                candidates=order,
                start_gallons=D(start),
                reserve_gallons=D(reserve),
            )
            for order in orders
        ]
        self.assertEqual(results[1], results[0], "reversed input changed the exact plan")
        self.assertEqual(results[2], results[0], "shuffled input changed the exact plan")
        return results[0]

    def assert_plan(self, result: FuelPlan | FuelGap) -> FuelPlan:
        self.assertIsInstance(result, FuelPlan, f"expected a plan, got {result}")
        assert isinstance(result, FuelPlan)  # narrows the type for the checker
        return result

    def assert_gap(
        self,
        result: FuelPlan | FuelGap,
        *,
        gap: tuple[str, str, str, str],
        hint: FeasibilityHint,
    ) -> None:
        self.assertIsInstance(result, FuelGap, f"expected a gap, got {result}")
        assert isinstance(result, FuelGap)
        self.assertEqual(
            (result.gap_from_mile, result.gap_to_mile, result.gap_leg_miles, result.max_leg_miles),
            tuple(D(value) for value in gap),
        )
        self.assertGreater(result.gap_leg_miles, result.max_leg_miles)
        self.assertEqual(result.hint, hint)

    def assert_exact(
        self, exact: ExactPlan | None, *, cost: str, stops: list[tuple[int, str, str]]
    ) -> None:
        """`stops` is [(station_id, rule, gallons)]."""
        self.assertIsNotNone(exact)
        assert exact is not None
        self.assertEqual(exact.cost, D(cost))
        self.assertEqual(
            [(s.candidate.station_id, s.rule, s.gallons) for s in exact.stops],
            [(station_id, rule, D(gallons)) for station_id, rule, gallons in stops],
        )

    def assert_stops(self, plan: FuelPlan, expected: list[tuple[int, str, str, int, str]]) -> None:
        """`expected` is [(station_id, arrival, purchase_rule, gallons, tank after purchase)]."""
        self.assertEqual(
            [
                (
                    s.candidate.station_id,
                    s.arrival_gallons,
                    s.purchase_rule,
                    s.gallons_to_buy,
                    s.tank_after_purchase_gallons,
                )
                for s in plan.stops
            ],
            [
                (sid, D(arrival), rule, gallons, D(tank))
                for sid, arrival, rule, gallons, tank in expected
            ],
        )


class LegFormulaTests(OptimizerCase):
    """TC-14 leg clause (FR-18) and the uniform FR-44 margin."""

    def test_corridor_and_detour(self) -> None:
        # Arrange: S at mile 100 with d 4.00 (half on each side); T at 300 with d 2.00.
        s_station = station(1, "100.00", "3.000", detour="4.00")
        t_station = station(2, "300.00", "4.000", detour="2.00")

        # Act: F 102 cannot cover the planned 103.00 start leg; with 16 gal the truck must
        # use S, then fill there because T is dearer.
        short_of_s = self.solve("500.00", [s_station], start="15.2")
        through_s_and_t = self.assert_plan(self.solve("700.00", [s_station, t_station], start="16"))

        # Assert: planned = route miles + d/2 + 1 mi margin per station end; driven has no margin.
        self.assert_gap(
            short_of_s,
            gap=("0", "100.00", "103.00", "102.0"),
            hint=FeasibilityHint.MORE_FUEL_OR_LOWER_RESERVE,
        )
        s_stop = through_s_and_t.stops[0]
        self.assertEqual(s_stop.arrival_gallons, D("16") - D("102.00") / 10)
        self.assertEqual(s_stop.planned_miles_to_next, D("205.00"))
        self.assertEqual(s_stop.driven_miles_to_next, D("203.00"))

    def test_leg_exactly_at_the_after_purchase_range_is_feasible(self) -> None:
        plan = self.assert_plan(self.solve("537.00", [station(1, "100.00", detour="4.00")]))

        self.assert_exact(
            self.solve_exact("537.00", [station(1, "100.00", detour="4.00")]),
            cost="27.90000",
            stops=[(1, "just_enough", "9.30")],
        )
        self.assert_stops(plan, [(1, "39.80", "to_finish", 10, "49.80")])
        self.assertEqual(plan.stops[0].planned_miles_to_next, D("440.00"))
        self.assertEqual(plan.finish_arrival_gallons, D("5.90"))
        self.assertEqual(plan.total_cost, D("30.000"))

    def test_leg_one_hundredth_over_the_after_purchase_range_is_a_gap(self) -> None:
        result = self.solve("537.01", [station(1, "100.00", detour="4.00")])

        self.assert_gap(
            result,
            gap=("100.00", "537.01", "440.01", "440"),
            hint=FeasibilityHint.LOWER_RESERVE,
        )

    def test_all_verified_candidates_carry_no_margin(self) -> None:
        verified = [station(1, "100.00", detour="4.00", verified=True)]

        exact = self.solve_exact("538.00", verified)
        plan = self.assert_plan(self.solve("538.00", verified))

        self.assert_exact(exact, cost="27.60000", stops=[(1, "just_enough", "9.20")])
        self.assertEqual(plan.stop_margin_miles, D("0"))
        self.assertEqual(plan.planning_margin_miles, D("0"))

    def test_one_unverified_candidate_keeps_the_margin_on_every_stop(self) -> None:
        mixed = [
            station(1, "100.00", "3.000", detour="4.00", verified=True),
            station(2, "300", "9.000"),
        ]

        exact = self.solve_exact("538", mixed)
        plan = self.assert_plan(self.solve("538", mixed))

        self.assert_exact(
            exact, cost="30.60000", stops=[(1, "fill", "9.30"), (2, "just_enough", "0.30")]
        )
        self.assert_stops(plan, [(1, "39.80", "fill_tank", 10, "49.80")])
        self.assertEqual(plan.stop_margin_miles, D("1"))
        self.assertEqual(plan.finish_arrival_gallons, D("5.80"))
        self.assertTrue(plan.has_merged_leg)

    def test_stop_margin_is_one_mile_when_there_are_no_candidates(self) -> None:
        self.assertEqual(stop_margin_miles([]), D("1"))


class RangeBoundariesTests(OptimizerCase):
    """TC-17 unit half (FR-20, FR-23, I2, I7): the spec table plus QA's extra boundaries."""

    def test_range_boundaries(self) -> None:
        at_100 = [station(1, "100")]
        cases = [
            # (name, L, stations, start, reserve, gap tuple, hint)
            ("b", "450.1", [], "50", "5", ("0", "450.1", "450.1", "450"), H.LOWER_RESERVE),
            (
                "b2",
                "450.1",
                [],
                "40",
                "5",
                ("0", "450.1", "450.1", "350"),
                H.MORE_FUEL_AND_LOWER_RESERVE,
            ),
            ("c", "450.01", [], "50", "5", ("0", "450.01", "450.01", "450"), H.LOWER_RESERVE),
            ("d", "500.1", [], "50", "0", ("0", "500.1", "500.1", "500"), H.NOT_FIXABLE),
            ("d 500.01", "500.01", [], "50", "0", ("0", "500.01", "500.01", "500"), H.NOT_FIXABLE),
            ("e", "539.1", at_100, "50", "5", ("100", "539.1", "440.1", "440"), H.LOWER_RESERVE),
            ("f", "100", [], "5", "5", ("0", "100", "100", "0"), H.MORE_FUEL),
            (
                "g",
                "800",
                [station(1, "449.5")],
                "50",
                "5",
                ("0", "449.5", "450.5", "450"),
                H.LOWER_RESERVE,
            ),
            ("h", "900", [station(1, "520")], "50", "0", ("0", "520", "521", "500"), H.NOT_FIXABLE),
            ("i 490.0", "589", at_100, "50", "5", ("100", "589", "490", "440"), H.LOWER_RESERVE),
            (
                "i 490.1",
                "589.1",
                at_100,
                "50",
                "5",
                ("100", "589.1", "490.1", "440"),
                H.NOT_FIXABLE,
            ),
            ("j", "495", [station(1, "1")], "10", "0", ("1", "495", "495", "490"), H.MORE_FUEL),
            (
                "k",
                "450.1",
                [],
                "46",
                "1",
                ("0", "450.1", "450.1", "450"),
                H.MORE_FUEL_OR_LOWER_RESERVE,
            ),
        ]
        for name, route_miles, stations, start, reserve, gap, hint in cases:
            with self.subTest(case=name):
                result = self.solve(route_miles, stations, start=start, reserve=reserve)
                self.assert_gap(result, gap=gap, hint=hint)

        feasible = [
            # (name, L, stations, start, reserve, finish arrival)
            ("a", "450.0", [], "50", "5", "5.0"),
            ("c", "450.00", [], "50", "5", "5.00"),
            ("d", "500.0", [], "50", "0", "0.0"),
            ("e", "539.0", at_100, "50", "5", "5.1"),
        ]
        for name, route_miles, stations, start, reserve, finish in feasible:
            with self.subTest(case=name):
                plan = self.assert_plan(
                    self.solve(route_miles, stations, start=start, reserve=reserve)
                )
                self.assertEqual(plan.finish_arrival_gallons, D(finish))

    def test_short_trip_needs_no_stop_and_ignores_a_cheap_station(self) -> None:
        plan = self.assert_plan(self.solve("300.00", [station(1, "100", "1.000")]))

        self.assertEqual(plan.stops, ())
        self.assertEqual(
            (plan.exact_cost, plan.total_cost, plan.gallons_bought), (D("0"), D("0"), 0)
        )
        self.assertEqual(plan.finish_arrival_gallons, D("20.0"))
        self.assertEqual(plan.driven_distance_miles, D("300.00"))
        self.assertEqual(plan.planning_margin_miles, D("0"))
        self.assertFalse(plan.has_merged_leg)

    def test_one_stop_trip_meets_the_reserve_plus_margin_exactly_at_the_finish(self) -> None:
        exact = self.solve_exact("539.0", [station(1, "100")])
        plan = self.assert_plan(self.solve("539.0", [station(1, "100")]))

        self.assert_exact(exact, cost="27.3000", stops=[(1, "just_enough", "9.1")])
        self.assert_stops(plan, [(1, "40.0", "to_finish", 9, "49.0")])
        self.assertEqual(plan.finish_arrival_gallons, D("5.1"))
        self.assertEqual(plan.total_cost, D("27.000"))

    def test_low_start_fuel_reaches_a_station_at_the_first_leg_limit(self) -> None:
        exact = self.solve_exact("400", [station(1, "49.00")], start="10")
        plan = self.assert_plan(self.solve("400", [station(1, "49.00")], start="10"))

        self.assert_exact(exact, cost="105.6000", stops=[(1, "just_enough", "35.2")])
        self.assert_stops(plan, [(1, "5.1", "to_finish", 36, "41.1")])
        self.assertEqual(plan.finish_arrival_gallons, D("6.0"))
        self.assertEqual(plan.total_cost, D("108.000"))

    def test_low_start_fuel_one_hundredth_short_of_a_station_is_a_gap(self) -> None:
        result = self.solve("400", [station(1, "49.01")], start="10")

        self.assert_gap(
            result,
            gap=("0", "49.01", "50.01", "50"),
            hint=FeasibilityHint.MORE_FUEL_OR_LOWER_RESERVE,
        )

    def test_fractional_start_fuel_still_buys_whole_gallons(self) -> None:
        exact = self.solve_exact("200", [station(1, "60")], start="12.5")
        plan = self.assert_plan(self.solve("200", [station(1, "60")], start="12.5"))

        self.assert_exact(exact, cost="38.1000", stops=[(1, "just_enough", "12.7")])
        self.assert_stops(plan, [(1, "6.5", "to_finish", 13, "19.5")])
        self.assertEqual(plan.finish_arrival_gallons, D("5.5"))


class GapLocationTests(OptimizerCase):
    """I7 / FR-23: the gap names the real stretch, from the furthest reachable node."""

    def test_gap_after_the_last_station_starts_at_that_station(self) -> None:
        stations = [station(1, "50", "3.000"), station(2, "150", "3.100")]

        result = self.solve("800", stations, reserve="0")

        self.assert_gap(result, gap=("150", "800", "651", "490"), hint=FeasibilityHint.NOT_FIXABLE)

    def test_gap_between_stations_ends_at_the_next_station_not_the_finish(self) -> None:
        stations = [station(1, "100"), station(2, "400"), station(3, "1000")]

        result = self.solve("1300", stations)

        self.assert_gap(result, gap=("400", "1000", "602", "440"), hint=FeasibilityHint.NOT_FIXABLE)

    def test_gap_starts_at_the_furthest_reachable_node_when_reach_is_not_a_prefix(self) -> None:
        # id 2 at 448.90 with d 4 needs 451.9 planned miles; id 1 just after it needs 450.
        stations = [station(1, "449.00"), station(2, "448.90", detour="4.00")]

        result = self.solve("1000", stations)

        self.assert_gap(
            result, gap=("449.00", "1000", "552.00", "440"), hint=FeasibilityHint.NOT_FIXABLE
        )


class OptimizerEdgesTests(OptimizerCase):
    """TC-16 (FR-19): ties, greedy traps and the purchase rules."""

    def test_optimizer_edges(self) -> None:
        """TC-16 evidence: the spec's six rows; the tests below hold the full values."""
        rows = [
            # (row, L, stations, start, expected (station_id, rule) sequence)
            ("equal prices at 200/300", "600", [station(2, "300"), station(1, "200")], "50",
             [(1, "just_enough")]),
            ("same mile and price, IDs 7 and 9", "600", [station(9, "300"), station(7, "300")],
             "50", [(7, "just_enough")]),
            ("equal cost, 1 vs 2 stops", "700",
             [station(1, "200", "3.780"), station(2, "400", "3.820")], "50", [(2, "just_enough")]),
            ("start fuel 50 above the 49-gal planning tank", "870",
             [station(1, "2", "4.000"), station(2, "300", "4.100"), station(3, "440", "1.000")],
             "50", [(3, "just_enough")]),
            ("greedy trap: cheaper than nearest-cheapest", "800",
             [station(1, "100", "3.000"), station(2, "400", "4.000")], "50",
             [(1, "fill"), (2, "just_enough")]),
        ]  # fmt: skip
        for row, route_miles, stations, start, expected in rows:
            with self.subTest(row=row):  # solve_exact also checks shuffled and reversed input
                exact = self.solve_exact(route_miles, stations, start=start)
                self.assertIsNotNone(exact)
                assert exact is not None
                self.assertEqual([(s.candidate.station_id, s.rule) for s in exact.stops], expected)

    def test_equal_cost_prefers_one_stop_over_two_when_all_are_verified(self) -> None:
        # Margin 0: 2 alone buys 40 gal; 1 then 3 buys 15 + 25 gal at the same price.
        stations = [
            station(1, "300", verified=True),
            station(2, "440", verified=True),
            station(3, "600", verified=True),
        ]

        exact = self.solve_exact("850", stations)

        self.assert_exact(exact, cost="120.000", stops=[(2, "just_enough", "40")])

    def test_equal_prices_choose_the_earlier_stop(self) -> None:
        stations = [station(2, "300"), station(1, "200")]

        exact = self.solve_exact("600", stations)
        plan = self.assert_plan(self.solve("600", stations))

        self.assert_exact(exact, cost="45.6000", stops=[(1, "just_enough", "15.2")])
        self.assert_stops(plan, [(1, "30.0", "to_finish", 16, "46.0")])
        self.assertEqual(plan.total_cost, D("48.000"))

    def test_same_mile_and_price_choose_the_lower_station_id(self) -> None:
        exact = self.solve_exact("600", [station(9, "300"), station(7, "300")])

        self.assert_exact(exact, cost="45.6000", stops=[(7, "just_enough", "15.2")])

    def test_station_ids_tie_in_numeric_not_string_order(self) -> None:
        exact = self.solve_exact("600", [station(10, "300"), station(9, "300")])

        self.assert_exact(exact, cost="45.6000", stops=[(9, "just_enough", "15.2")])

    def test_equal_cost_prefers_fewer_stops(self) -> None:
        stations = [station(1, "200", "3.780"), station(2, "400", "3.820")]

        exact = self.solve_exact("700", stations)
        plan = self.assert_plan(self.solve("700", stations))

        self.assert_exact(exact, cost="96.2640", stops=[(2, "just_enough", "25.2")])
        self.assert_stops(plan, [(2, "10.0", "to_finish", 26, "36.0")])
        self.assertEqual(plan.total_cost, D("99.320"))

    def test_a_slightly_cheaper_two_stop_plan_beats_one_stop(self) -> None:
        stations = [station(1, "200", "3.779"), station(2, "400", "3.820")]

        exact = self.solve_exact("700", stations)
        plan = self.assert_plan(self.solve("700", stations))

        self.assert_exact(
            exact, cost="96.2449", stops=[(1, "fill", "19.1"), (2, "just_enough", "6.3")]
        )
        self.assert_stops(
            plan, [(1, "30.0", "fill_tank", 20, "50.0"), (2, "30.0", "to_finish", 6, "36.0")]
        )
        self.assertEqual(plan.total_cost, D("98.500"))

    def test_tie_between_intermediate_stops_keeps_the_lower_id(self) -> None:
        stations = [
            station(9, "300", "3.000"),
            station(7, "300", "3.000"),
            station(5, "700", "3.200"),
        ]

        exact = self.solve_exact("1100", stations)
        plan = self.assert_plan(self.solve("1100", stations))

        self.assert_exact(
            exact, cost="203.4600", stops=[(7, "fill", "29.1"), (5, "just_enough", "36.3")]
        )
        self.assert_stops(
            plan, [(7, "20.0", "fill_tank", 30, "50.0"), (5, "10.0", "to_finish", 36, "46.0")]
        )
        self.assertEqual(plan.finish_arrival_gallons, D("6.0"))
        self.assertEqual(plan.total_cost, D("205.200"))

    def test_equal_key_states_at_one_station_keep_the_earlier_sequence(self) -> None:
        # Via 4: 13.1 x 2 + 22.5 x 3 = 93.7; via 5 (d 4): 14.3 x 2 + 21.7 x 3 = 93.7. Both
        # states at station 2 have key -8.9 and 1 stop, so only (mile, id) order decides.
        stations = [
            station(1, "260", "4"),
            station(2, "460", "3", detour="2"),
            station(3, "510", "3.5", detour="2"),
            station(4, "140", "2"),
            station(5, "150", "2", detour="4"),
        ]

        exact = self.solve_exact("800", stations)
        plan = self.assert_plan(self.solve("800", stations))

        self.assert_exact(
            exact, cost="93.7", stops=[(4, "fill", "13.1"), (2, "just_enough", "22.5")]
        )
        self.assert_stops(
            plan, [(4, "36.0", "fill_tank", 14, "50.0"), (2, "17.9", "to_finish", 22, "39.9")]
        )
        self.assertEqual(plan.finish_arrival_gallons, D("5.8"))
        self.assertEqual(plan.total_cost, D("94"))

    def test_cheapest_plan_beats_the_fewest_stops_greedy(self) -> None:
        stations = [station(1, "100", "3.000"), station(2, "400", "4.000")]

        exact = self.solve_exact("800", stations)
        greedy = self.solve_exact("800", [station(2, "400", "4.000")])
        plan = self.assert_plan(self.solve("800", stations))

        self.assert_exact(
            exact, cost="132.5000", stops=[(1, "fill", "9.1"), (2, "just_enough", "26.3")]
        )
        self.assert_exact(greedy, cost="140.8000", stops=[(2, "just_enough", "35.2")])
        self.assert_stops(
            plan, [(1, "40.0", "fill_tank", 10, "50.0"), (2, "20.0", "to_finish", 26, "46.0")]
        )
        self.assertEqual(plan.finish_arrival_gallons, D("6.0"))
        self.assertEqual(plan.total_cost, D("134.000"))

    def test_detour_fuel_makes_the_cheaper_listed_price_the_dearer_stop(self) -> None:
        far_cheap = station(1, "300", "3.000", detour="5.00")
        near_dearer = station(2, "300", "3.010")

        exact = self.solve_exact("600", [far_cheap, near_dearer])
        cheap_only = self.solve_exact("600", [far_cheap])
        plan = self.assert_plan(self.solve("600", [far_cheap, near_dearer]))

        self.assert_exact(exact, cost="45.7520", stops=[(2, "just_enough", "15.2")])
        self.assert_exact(cheap_only, cost="47.10000", stops=[(1, "just_enough", "15.70")])
        self.assert_stops(plan, [(2, "20.0", "to_finish", 16, "36.0")])
        self.assertEqual(plan.total_cost, D("48.160"))

    def test_equal_price_next_stop_means_just_enough_not_fill(self) -> None:
        stations = [station(1, "100", "3.000"), station(2, "500", "3.000")]

        exact = self.solve_exact("900", stations)
        plan = self.assert_plan(self.solve("900", stations))

        self.assertEqual(exact.cost if exact else None, D("136.2000"))
        self.assert_stops(
            plan, [(1, "40.0", "to_next_stop", 6, "46.0"), (2, "6.0", "to_finish", 40, "46.0")]
        )
        self.assertEqual(plan.finish_arrival_gallons, D("6.0"))
        self.assertEqual(plan.total_cost, D("138.000"))

    def test_fuel_above_the_planning_tank_is_never_sold_back_at_a_dearer_station(self) -> None:
        # A full 50-gal start reaches A with more than U; filling there would "sell" fuel.
        stations = [
            station(1, "2", "4.000"),
            station(2, "300", "4.100"),
            station(3, "440", "1.000"),
        ]

        exact = self.solve_exact("870", stations)
        plan = self.assert_plan(self.solve("870", stations))

        self.assert_exact(exact, cost="42.2000", stops=[(3, "just_enough", "42.2")])
        self.assert_stops(plan, [(3, "6.0", "to_finish", 43, "49.0")])
        self.assertEqual(plan.finish_arrival_gallons, D("6.0"))

    def test_result_does_not_depend_on_input_order(self) -> None:
        rng = random.Random(20261027)
        stations = [
            station(
                rng.randint(1, 9999) * 10 + k,
                str(D(rng.randint(0, 250_000)).scaleb(-2)),
                rng.choice(["3.000", "3.199", "3.459", "2.999"]),
                detour=str(D(2 * rng.randint(0, 250)).scaleb(-2)),
            )
            for k in range(40)
        ]
        expected = self.solve("2500.00", stations)

        for attempt in range(5):
            with self.subTest(shuffle=attempt):
                shuffled = random.Random(attempt).sample(stations, len(stations))
                self.assertEqual(self.solve("2500.00", shuffled), expected)


class SameMileOrderRegressionTests(OptimizerCase):
    """Stations sharing a mile: nodes sort by (mile, numeric id), the current spec rule.

    30000509 and 30003419 catch a mile-only sort; 30114097 and the hand case catch string ids.
    """

    def test_same_mile_stations_are_visited_in_numeric_id_order(self) -> None:
        # F = 301.5: station 10 (d 4) is out of reach from the start but 4 planned miles from 9.
        # Sorted "10" before "9", the edge 9 -> 10 would not exist and the plan would cost 160.2.
        stations = [station(10, "300", "2.000", detour="4"), station(9, "300", "4.000")]

        exact = self.solve_exact("700", stations, start="35.15")
        plan = self.assert_plan(self.solve("700", stations, start="35.15"))

        self.assert_exact(
            exact, cost="82.0", stops=[(9, "just_enough", "0.35"), (10, "just_enough", "40.3")]
        )
        self.assert_stops(
            plan, [(9, "5.15", "to_next_stop", 1, "6.15"), (10, "5.95", "to_finish", 40, "45.95")]
        )
        self.assertEqual(plan.finish_arrival_gallons, D("5.75"))

    def test_same_mile_groups_match_the_brute_force_reference_in_any_order(self) -> None:
        for seed in (30000509, 30003419, 30114097):
            instance = ref.general_instance(seed)
            with self.subTest(seed=seed):
                stations = list(instance.candidates)
                exact = self.solve_exact(
                    str(instance.route_miles),
                    stations,
                    start=str(instance.start_gallons),
                    reserve=str(instance.reserve_gallons),
                )
                for attempt in range(5):
                    shuffled = random.Random(attempt).sample(stations, len(stations))
                    again = find_cheapest_exact_plan(
                        **instance.as_kwargs() | {"candidates": shuffled}
                    )
                    self.assertEqual(again, exact)
                self.assertEqual(
                    ref.ReferencePlan(
                        cost=exact.cost,
                        sequence=tuple(
                            (s.candidate.route_mile, s.candidate.station_id) for s in exact.stops
                        ),
                    )
                    if exact
                    else None,
                    ref.brute_force_plan(instance),
                )


class ReplayHandCaseTests(OptimizerCase):
    """TC-18 hand cases (FR-20 to FR-22): the whole-gallon replay on a real tank series."""

    def test_chicago_denver_rounds_fill_down_and_just_enough_up(self) -> None:
        stations = [
            station(4021, "412.6", "3.199", detour="1.2"),
            station(7310, "844.6", "3.459", detour="0.8"),
        ]

        exact = self.solve_exact("1003.2", stations)
        plan = self.assert_plan(self.solve("1003.2", stations))

        self.assert_exact(
            exact, cost="182.91808", stops=[(4021, "fill", "40.42"), (7310, "just_enough", "15.5")]
        )
        self.assert_stops(
            plan,
            [(4021, "8.68", "fill_tank", 41, "49.68"), (7310, "6.38", "to_finish", 15, "21.38")],
        )
        self.assertEqual([s.line_cost for s in plan.stops], [D("131.159"), D("51.885")])
        self.assertEqual([s.planned_miles_to_next for s in plan.stops], [D("435.0"), D("160.0")])
        self.assertEqual([s.driven_miles_to_next for s in plan.stops], [D("433.0"), D("159.0")])
        self.assertEqual(plan.finish_arrival_gallons, D("5.48"))
        self.assertEqual(plan.total_cost, D("183.044"))
        self.assertEqual(plan.gallons_bought, 56)
        self.assertEqual(plan.driven_distance_miles, D("1005.2"))
        self.assertEqual(plan.planning_margin_miles, D("4.0"))
        self.assertFalse(plan.has_merged_leg)

    def test_stop_whose_purchase_rounds_to_zero_is_merged_into_the_previous_leg(self) -> None:
        stations = [station(1, "420", "3.000"), station(2, "849", "3.500")]

        exact = self.solve_exact("863", stations)
        plan = self.assert_plan(self.solve("863", stations))

        self.assert_exact(
            exact, cost="125.4000", stops=[(1, "fill", "41.1"), (2, "just_enough", "0.6")]
        )
        self.assert_stops(plan, [(1, "8.0", "fill_tank", 42, "50.0")])
        self.assertEqual(plan.stops[0].planned_miles_to_next, D("444.0"))
        self.assertEqual(plan.stops[0].driven_miles_to_next, D("443.0"))
        self.assertEqual(plan.finish_arrival_gallons, D("5.7"))
        self.assertEqual(plan.total_cost, D("126.000"))
        self.assertTrue(plan.has_merged_leg)

    def test_a_fill_whose_next_stop_merges_away_reaches_the_finish_on_its_own(self) -> None:
        # The just-enough stop at B rounds to 0 gal and merges: A's fill reaches the finish.
        stations = [station(1, "100", "3.000"), station(2, "300", "3.500")]

        plan = self.assert_plan(self.solve("549.0", stations))

        self.assert_stops(plan, [(1, "40.0", "fill_tank", 10, "50.0")])
        self.assertEqual(plan.finish_arrival_gallons, D("5.1"))
        self.assertEqual(plan.gallons_bought, 10)
        self.assertEqual(plan.total_cost, D("30.000"))
        self.assertTrue(plan.has_merged_leg)

    def test_shown_arrival_plus_purchase_never_exceeds_the_tank(self) -> None:
        stations = [station(1, "400", "3.000"), station(2, "798", "3.100")]

        exact = self.solve_exact("1000.0", stations)
        plan = self.assert_plan(self.solve("1000.0", stations))

        self.assertEqual(exact.cost if exact else None, D("167.8300"))
        self.assert_stops(
            plan, [(1, "10.0", "fill_tank", 40, "50.0"), (2, "10.2", "to_finish", 16, "26.2")]
        )
        self.assertEqual(plan.finish_arrival_gallons, D("6.0"))
        self.assertEqual(plan.total_cost, D("169.600"))
        for stop in plan.stops:
            shown_arrival = stop.arrival_gallons.quantize(D("0.1"), rounding=ROUND_FLOOR)
            self.assertLessEqual(shown_arrival + stop.gallons_to_buy, D("50.0"))


class InputValidationTests(OptimizerCase):
    """Broken preconditions are bugs upstream: they raise OptimizerError, never a plan."""

    def test_invalid_inputs_raise_optimizer_error(self) -> None:
        cases = {
            "duplicate station id": ("600", [station(1, "100"), station(1, "200")], "50", "5"),
            "start below reserve": ("600", [], "4", "5"),
            "reserve above 48": ("600", [], "50", "48.5"),
            "start above 50": ("600", [], "50.1", "5"),
            "mile beyond the finish": ("600", [station(1, "600.01")], "50", "5"),
            "negative detour": ("600", [station(1, "100", detour="-0.02")], "50", "5"),
            "zero price": ("600", [station(1, "100", "0")], "50", "5"),
            "negative route": ("-1", [], "50", "5"),
            "negative route mile": ("600", [station(1, "-0.01")], "50", "5"),
            "NaN route mile": ("600", [station(1, "NaN")], "50", "5"),
            "infinite price": ("600", [station(1, "100", "Infinity")], "50", "5"),
            "NaN route": ("NaN", [], "50", "5"),
            "text station id": ("600", [self._with_id("1")], "50", "5"),
            "boolean station id": ("600", [self._with_id(True)], "50", "5"),
        }
        for name, (route_miles, stations, start, reserve) in cases.items():
            with self.subTest(case=name), self.assertRaises(OptimizerError):
                plan_fuel_stops(
                    route_miles=D(route_miles),
                    candidates=stations,
                    start_gallons=D(start),
                    reserve_gallons=D(reserve),
                )

    def test_float_values_are_refused_on_the_money_path(self) -> None:
        float_price = Candidate(
            station_id=1,
            route_mile=D("100"),
            detour_miles=D("0"),
            price_per_gallon=3.1,  # type: ignore[arg-type]  # the point of the test
            is_detour_verified=False,
        )

        with self.assertRaises(OptimizerError):
            plan_fuel_stops(
                route_miles=D("600"),
                candidates=[float_price],
                start_gallons=D("50"),
                reserve_gallons=D("5"),
            )

    @staticmethod
    def _with_id(station_id: object) -> Candidate:
        return Candidate(
            station_id=station_id,  # type: ignore[arg-type]  # the point of the test
            route_mile=D("100"),
            detour_miles=D("0"),
            price_per_gallon=D("3"),
            is_detour_verified=False,
        )

    def test_input_too_large_for_exact_arithmetic_raises_optimizer_error(self) -> None:
        # 1E+60 - 100 + 1 needs 61 digits: the Inexact trap must surface as OptimizerError.
        with self.assertRaises(OptimizerError):
            plan_fuel_stops(
                route_miles=D("1E+60"),
                candidates=[station(1, "100")],
                start_gallons=D("50"),
                reserve_gallons=D("5"),
            )


class SafetyNetTests(OptimizerCase):
    """The post-replay checks and the DP/reachability guard really raise (they are not dead)."""

    def test_a_purchase_overfilling_the_tank_is_caught_by_the_invariant_checks(self) -> None:
        real_gallons_to_buy = optimizer._gallons_to_buy

        def one_gallon_too_many(**kwargs: object) -> int:
            return real_gallons_to_buy(**kwargs) + 1  # type: ignore[arg-type]  # pass-through

        with (
            mock.patch.object(optimizer, "_gallons_to_buy", one_gallon_too_many),
            self.assertRaises(OptimizerError),
        ):
            plan_fuel_stops(
                route_miles=D("800"),
                candidates=[station(1, "100", "3.000"), station(2, "400", "4.000")],
                start_gallons=D("50"),
                reserve_gallons=D("5"),
            )

    def test_dp_finding_no_plan_on_a_reachable_trip_raises_instead_of_a_gap(self) -> None:
        with (
            mock.patch.object(optimizer._CheapestPathSearch, "run", return_value=None),
            self.assertRaises(OptimizerError),
        ):
            plan_fuel_stops(
                route_miles=D("800"),
                candidates=[station(1, "400")],
                start_gallons=D("50"),
                reserve_gallons=D("5"),
            )


class OptimizerPurityTests(OfflineTestCase):
    """TC-41, optimiser half (NFR-13): optimizer.py imports only pure standard-library modules."""

    ALLOWED_MODULES = {"bisect", "dataclasses", "decimal", "enum", "collections.abc", "typing"}

    def test_optimizer_imports_only_pure_standard_library_modules(self) -> None:
        tree = ast.parse(Path(optimizer.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add("." * node.level + (node.module or ""))

        self.assertLessEqual(
            imported, self.ALLOWED_MODULES, f"optimizer.py imports {sorted(imported)}"
        )
