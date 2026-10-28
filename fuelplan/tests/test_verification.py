"""Spec 01 TC-46 (FR-44, FR-27, AC-38, AC-39): the detour-verification seam.

Fake verifiers are set only through `FUELPILOT_DETOUR_VERIFIER`, on the TC-45 golden trip
(Chicago → Denver, stations 4021 and 7310, which are also the only candidates).
"""

from decimal import Decimal

from django.core.cache import cache
from django.test import override_settings

from fuelplan.optimizer import Candidate, OptimizerError
from fuelplan.planner import apply_measured_detours
from fuelplan.routing import CallBudget
from fuelplan.verification import load_detour_verifier, verify_detours_noop

from . import fake_verifiers
from .base import ApiTestCase, OfflineTestCase, assert_envelope
from .planner_harness import PlannerHarness, json_body
from .test_plan_cache import plan_keys, planobj_keys, route_keys
from .test_response_body import GOLDEN_REQUEST, golden_index

FAKES = "fuelplan.tests.fake_verifiers."


class VerificationSeamTests(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        fake_verifiers.received_backups.clear()
        self.harness = PlannerHarness(index=golden_index())
        patcher = self.harness.patch_view()
        patcher.start()
        self.addCleanup(patcher.stop)

    def _plan(self) -> dict:
        self.harness.queue_fixture("chicago_denver")
        response = self.post_json(GOLDEN_REQUEST, keep_cache=True)
        self.assertEqual(response.status_code, 200, response.content)
        return json_body(response)

    def _stop_values(self, body: dict, *keys: str) -> list[tuple]:
        return [tuple(stop[key] for key in keys) for stop in body["fuel_stops"]]

    @override_settings(FUELPILOT_DETOUR_VERIFIER=FAKES + "verify_every_candidate")
    def test_every_candidate_verified_drops_the_margin_and_replans(self) -> None:
        body = self._plan()

        self.assertEqual(
            self._stop_values(
                body,
                "estimated_detour_miles",
                "fuel_on_arrival_gallons",
                "miles_to_next",
                "gallons_to_buy",
                "detour_verified",
            ),
            [("1.0", "8.6", "432.8", 41, True), ("0.6", "6.4", "158.9", 15, True)],
        )
        self.assertEqual(body["finish_fuel_on_arrival_gallons"], "5.5")
        self.assertEqual(body["route"]["driven_distance_miles"], "1004.8")  # 1003.2 + 1.0 + 0.6
        self.assertEqual(body["route"]["planning_margin_miles"], "0.0")
        self.assertEqual(body["totals"]["total_purchase_cost"], "183.04")
        assumptions = body["assumptions"]
        self.assertEqual(
            (assumptions["detour_method"], assumptions["unverified_stop_margin_miles"]),
            ("measured", "0.0"),
        )
        self.assertNotIn("1-mi margin", assumptions["optimality"])
        self.assertIn("measured detours", assumptions["optimality"])
        self.assertTrue(body["detours_verified"])
        self.assertFalse(any("safety margin" in note for note in body["notes"]))
        self.assertEqual(body["meta"]["routing_calls"], 1)
        self.assertEqual(fake_verifiers.received_backups, [[]])  # v1 passes no backups

    @override_settings(FUELPILOT_DETOUR_VERIFIER=FAKES + "verify_first_stop_only")
    def test_a_partly_verified_plan_keeps_the_margin_on_every_stop(self) -> None:
        body = self._plan()

        self.assertEqual(
            self._stop_values(
                body,
                "estimated_detour_miles",
                "fuel_on_arrival_gallons",
                "miles_to_next",
                "gallons_to_buy",
                "detour_verified",
            ),
            [("1.0", "8.6", "432.9", 41, True), ("0.8", "6.4", "159.0", 15, False)],
        )
        self.assertEqual(body["finish_fuel_on_arrival_gallons"], "5.5")
        self.assertEqual(body["route"]["driven_distance_miles"], "1005.0")
        self.assertEqual(body["route"]["planning_margin_miles"], "4.0")
        self.assertEqual(body["assumptions"]["detour_method"], "mixed")
        self.assertEqual(body["assumptions"]["unverified_stop_margin_miles"], "1.0")
        self.assertFalse(body["detours_verified"])

    @override_settings(FUELPILOT_DETOUR_VERIFIER=FAKES + "verify_failed")
    def test_a_failed_verification_is_answered_and_saved_but_never_reused(self) -> None:
        first = self._plan()

        self.assertFalse(first["detours_verified"])
        self.assertEqual(planobj_keys(), {f"planobj:v1:{first['plan_id']}"})
        self.assertEqual(plan_keys(), set())

        repeat = self._plan()
        self.assertNotEqual(repeat["plan_id"], first["plan_id"])
        self.assertEqual(repeat["meta"]["plan_cache"], "miss")

    @override_settings(FUELPILOT_DETOUR_VERIFIER=FAKES + "verify_with_a_far_detour")
    def test_a_stop_measured_beyond_5_miles_is_dropped_and_the_replan_can_fail(self) -> None:
        # Without 4021 the first station is 7310 at mile 844.6, beyond the 450-mi first leg.
        self.harness.queue_fixture("chicago_denver")

        response = self.post_json(GOLDEN_REQUEST, keep_cache=True)

        assert_envelope(self, response, status=422, code="no_feasible_plan")
        self.assertEqual((len(route_keys()), plan_keys(), planobj_keys()), (1, set(), set()))

    def test_the_v1_no_op_tries_nothing_and_makes_no_extra_call(self) -> None:
        body = self._plan()

        self.assertEqual(body["assumptions"]["detour_method"], "straight_line_estimate")
        self.assertFalse(any(stop["detour_verified"] for stop in body["fuel_stops"]))
        self.assertEqual((body["meta"]["routing_calls"], self.harness.transport.count), (1, 1))
        self.assertEqual(len(plan_keys()), 1)  # `not_tried` plans are reused (Q-1)


class VerificationUnitTests(OfflineTestCase):
    def test_the_setting_names_the_no_op_by_default(self) -> None:
        self.assertIs(load_detour_verifier(), verify_detours_noop)

    def test_the_no_op_returns_not_tried_and_spends_no_call(self) -> None:
        budget = CallBudget()

        result = verify_detours_noop(route=None, chosen_stops=[], backups=[], budget=budget)

        self.assertEqual((result.status, dict(result.measured_detour_miles)), ("not_tried", {}))
        self.assertEqual(budget.used, 0)

    def test_a_result_for_an_unknown_station_or_a_bad_detour_is_a_bug(self) -> None:
        candidates = [Candidate(1, Decimal("10.00"), Decimal("1.00"), Decimal("3.0"), False)]
        for measured in ({2: Decimal("1.0")}, {1: 1.0}, {1: Decimal("-0.1")}, {1: Decimal("NaN")}):
            with self.subTest(measured=measured), self.assertRaises(OptimizerError):
                apply_measured_detours(candidates, measured)
