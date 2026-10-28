"""Spec 01 build 2C-a over HTTP: the planner behind `POST /api/v1/fuel-plans`.

The HTTP halves of TC-2/3/4/5/9/17, plus TC-6, TC-19, TC-24, TC-26, TC-28 and TC-43.
Every test runs the real `TripPlanner` with a fake OSRM transport, a synthetic station
index and the fixture gazetteer (see `planner_harness`); nothing reaches the network.
"""

import json

from django.test import override_settings

from fuelplan.serializers import (
    ARRIVAL_ROUNDING_NOTE,
    DETOUR_NOTE,
    MONEY_NOTE,
    PRICE_NOTE,
    SAME_POINT_NOTE,
    WHOLE_GALLON_NOTE,
)
from fuelplan.stations import StationIndex

from .base import ApiTestCase, assert_envelope
from .planner_harness import (
    PlannerHarness,
    json_body,
    meridian_point,
    meridian_reply,
    meridian_request,
    station,
    synthetic_index,
)
from .test_locations import BORDER_PAIRS, OUTSIDE_POINTS
from .test_routing import provider_failure_rows

CHICAGO = {"city": "Chicago", "state": "IL"}
CHICAGO_POINT = {"latitude": 41.85003, "longitude": -87.65005}
DENVER_POINT = {"latitude": 39.73915, "longitude": -104.9847}
NOT_FIXABLE = "no station in the CSV covers this stretch; fuel settings cannot fix it"


def _point(latitude: float, longitude: float) -> dict:
    return {"latitude": latitude, "longitude": longitude}


class PlanApiTestCase(ApiTestCase):
    """A harness whose planner the view builds for every request of the test."""

    def setUp(self) -> None:
        super().setUp()
        self.harness = PlannerHarness()
        patcher = self.harness.patch_view()
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def calls(self) -> int:
        return self.harness.transport.count

    def assert_ok(self, response: object) -> dict:
        self.assertEqual(response.status_code, 200, response.content)
        return json_body(response)


class LocationHttpTests(PlanApiTestCase):
    """The HTTP halves of TC-2, TC-3 and TC-4: resolving costs 0 routing calls."""

    def test_city_input_is_matched_offline_and_echoed_with_its_5_dp_point(self) -> None:
        # TC-2: two spellings of one city are the same point, so start = finish (0 calls).
        body = self.assert_ok(
            self.post_json(
                {
                    "start": {"city": " new  YORK ", "state": " ny"},
                    "finish": {"city": "New York", "state": "NY"},
                }
            )
        )

        self.assertEqual(
            body["start"],
            {
                "input": {"city": "new  YORK", "state": "ny"},
                "matched": "New York, NY",
                "location": {"type": "Point", "coordinates": [-74.00597, 40.71427]},
            },
        )
        self.assertEqual(body["finish"]["matched"], "New York, NY")
        self.assertEqual((self.calls, body["meta"]["routing_calls"]), (0, 0))

    def test_coordinate_input_is_echoed_with_matched_null(self) -> None:
        body = self.assert_ok(self.post_json({"start": CHICAGO_POINT, "finish": CHICAGO_POINT}))

        self.assertEqual(
            body["start"],
            {
                "input": CHICAGO_POINT,
                "matched": None,
                "location": {"type": "Point", "coordinates": [-87.65005, 41.85003]},
            },
        )

    def test_an_unknown_city_is_location_not_found_with_0_calls(self) -> None:
        # TC-3
        response = self.post_json(
            {"start": {"city": "Nowhereville", "state": "KS"}, "finish": DENVER_POINT}
        )

        details = assert_envelope(self, response, status=422, code="location_not_found")
        self.assertEqual(details, {"field": "start", "city": "Nowhereville", "state": "KS"})
        self.assertIn("coordinates", json_body(response)["error"]["message"])
        self.assertEqual(self.calls, 0)

    def test_border_pairs_reject_the_foreign_side_and_accept_the_us_side(self) -> None:
        # TC-4 over HTTP: the US side is planned as start = finish, so 0 calls either way.
        for pair, (us_side, foreign_side) in BORDER_PAIRS.items():
            us_point, foreign_point = _point(*us_side), _point(*foreign_side)
            with self.subTest(pair=pair, side="US"):
                self.assert_ok(self.post_json({"start": us_point, "finish": us_point}))
            for field, payload in (
                ("start", {"start": foreign_point, "finish": us_point}),
                ("finish", {"start": us_point, "finish": foreign_point}),
            ):
                with self.subTest(pair=pair, side="foreign", field=field):
                    response = self.post_json(payload)

                    details = assert_envelope(
                        self, response, status=422, code="unsupported_location"
                    )
                    self.assertEqual(details, {"field": field, "scope": "contiguous US + DC"})
        self.assertEqual(self.calls, 0)

    def test_states_and_points_outside_the_service_area_are_422_and_xx_is_400(self) -> None:
        # TC-4: AK/HI/PR/GU/ON by state, Honolulu/Anchorage by point; "XX" fails validation.
        rows = {
            **{code: {"city": "Anywhere", "state": code} for code in ("AK", "HI", "PR", "GU")},
            "ON (Toronto)": {"city": "Toronto", "state": "ON"},
            **{name: _point(*OUTSIDE_POINTS[name]) for name in ("Honolulu", "Anchorage")},
        }
        for name, start in rows.items():
            with self.subTest(row=name):
                response = self.post_json({"start": start, "finish": DENVER_POINT})

                assert_envelope(self, response, status=422, code="unsupported_location")
                self.assertIn("contiguous US + DC", json_body(response)["error"]["message"])
        response = self.post_json({"start": {"city": "Chicago", "state": "XX"}, "finish": CHICAGO})
        assert_envelope(self, response, status=400, code="invalid_request")
        self.assertEqual(self.calls, 0)


class FuelInputHttpTests(PlanApiTestCase):
    """The 200 half of TC-5 on the 15-mile route: accepted fuel values are echoed."""

    def test_accepted_fuel_values_are_echoed_with_one_decimal(self) -> None:
        route = meridian_request(15.0)
        coordinates = json.dumps(route)[1:-1]
        rows = {  # name: (raw body, expected start fuel, expected reserve)
            "defaults": (json.dumps(route), "50.0", "5.0"),
            "12.5": (json.dumps({**route, "current_fuel_gallons": 12.5}), "12.5", "5.0"),
            '"12.5"': (json.dumps({**route, "current_fuel_gallons": "12.5"}), "12.5", "5.0"),
            "12.50": ("{" + coordinates + ', "current_fuel_gallons": 12.50}', "12.5", "5.0"),
            '"12.50"': (json.dumps({**route, "current_fuel_gallons": "12.50"}), "12.5", "5.0"),
            "reserve 48 with current 50": (
                json.dumps({**route, "current_fuel_gallons": 50, "reserve_gallons": 48}),
                "50.0",
                "48.0",
            ),
        }
        for name, (raw, start_fuel, reserve) in rows.items():
            with self.subTest(row=name):
                self.harness.queue(meridian_reply(15.0))

                body = self.assert_ok(self.post_raw(raw.encode()))

                assumptions = body["assumptions"]
                self.assertEqual(assumptions["start_fuel_gallons"], start_fuel)
                self.assertEqual(assumptions["reserve_gallons"], reserve)
                self.assertEqual(body["fuel_stops"], [])


class SamePointTests(PlanApiTestCase):
    """TC-6 (FR-8): start = finish at 5 dp is a 200 with no route call."""

    def test_the_same_coordinates_give_an_empty_plan_with_0_calls(self) -> None:
        body = self.assert_ok(self.post_json({"start": CHICAGO_POINT, "finish": CHICAGO_POINT}))

        self.assertEqual(body["fuel_stops"], [])
        self.assertEqual(body["totals"]["total_purchase_cost"], "0.00")
        self.assertEqual(body["totals"]["gallons_bought"], 0)
        self.assertEqual(body["totals"]["fuel_burned_gallons"], "0.0")
        self.assertEqual(
            body["route"],
            {
                "distance_miles": "0.0",
                "driven_distance_miles": "0.0",
                "planning_margin_miles": "0.0",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-87.65005, 41.85003], [-87.65005, 41.85003]],
                },
            },
        )
        self.assertEqual(body["finish_fuel_on_arrival_gallons"], "50.0")
        self.assertIn("Start and finish are the same point", body["notes"][0])
        self.assertEqual(
            body["meta"], {"routing_calls": 0, "route_cache": "skipped", "plan_cache": "miss"}
        )
        self.assertEqual(self.calls, 0)

    def test_a_city_and_coordinates_equal_at_5_dp_are_the_same_point(self) -> None:
        near_chicago = {"latitude": 41.850032, "longitude": -87.650048}

        body = self.assert_ok(self.post_json({"start": CHICAGO, "finish": near_chicago}))

        self.assertEqual(body["route"]["distance_miles"], "0.0")
        self.assertEqual(self.calls, 0)

    def test_points_that_differ_at_5_dp_are_routed_with_1_call(self) -> None:
        self.harness.queue(meridian_reply(1.0))
        other = {"latitude": 41.85004, "longitude": -87.65005}

        body = self.assert_ok(self.post_json({"start": CHICAGO, "finish": other}))

        self.assertEqual(body["meta"]["route_cache"], "miss")
        self.assertEqual(self.calls, 1)

    def test_endpoints_are_rounded_to_5_dp_before_the_route_call(self) -> None:
        self.harness.queue(meridian_reply(1.0))

        self.post_json(
            {
                "start": {"latitude": 41.850032, "longitude": -87.650048},
                "finish": CHICAGO_POINT | {"latitude": 41.9},
            }
        )

        path = self.harness.transport.requests[0].url.split("/driving/")[1].split("?")[0]
        self.assertEqual(path, "-87.650050,41.850030;-87.650050,41.900000")


class ProviderFailureHttpTests(PlanApiTestCase):
    """The HTTP half of TC-9: every provider failure class costs exactly 1 call."""

    def test_each_provider_failure_gives_its_status_with_exactly_1_call(self) -> None:
        rows = provider_failure_rows()
        self.assertEqual(len(rows), 23)
        for name, (reply, status, code) in rows.items():
            with self.subTest(row=name):
                self.harness.transport.requests.clear()
                self.harness.queue(reply)

                with self.assertLogs("fuelplan", "INFO"):
                    response = self.post_json(meridian_request(300.0))

                details = assert_envelope(self, response, status=status, code=code)
                self.assertEqual((self.calls, details["routing_calls"]), (1, 1))


class NoFeasiblePlanHttpTests(PlanApiTestCase):
    """TC-17 (b)(c)(h) and TC-19 over HTTP: the 422 names the gap, never a partial plan."""

    def _gap(self, payload: dict, *, miles: float) -> tuple[dict, str]:
        self.harness.queue(meridian_reply(miles))
        response = self.post_json(payload)
        details = assert_envelope(self, response, status=422, code="no_feasible_plan")
        self.assertEqual(set(json_body(response)), {"error"})  # no fuel_stops, no plan_id
        message = json_body(response)["error"]["message"]
        self.assertIn("among the stations checked", message)
        return details, message

    def test_a_450_1_mile_trip_without_stations_needs_a_lower_reserve(self) -> None:
        # TC-17 (b): F = 450.0 with a full tank, so only a lower reserve helps.
        details, message = self._gap(meridian_request(450.1), miles=450.1)

        self.assertEqual(
            details,
            {
                "gap_from_mile": "0.0",
                "gap_to_mile": "450.1",
                "gap_leg_miles": "450.1",
                "max_leg_miles": "450.0",
                "hint": "lower reserve_gallons",
            },
        )
        self.assertEqual(
            message,
            "Cannot plan this trip among the stations checked: from the start the next stop "
            "(the finish at mile 450.1) is 450.1 planned miles away (including detours and "
            "the safety margin), but the truck can cover at most 450.0 miles there.",
        )

    def test_the_gap_leg_is_rounded_up_while_the_mile_is_rounded_half_up(self) -> None:
        # TC-17 (c): 450.01 shows as mile "450.0" but as leg "450.1", never as fitting.
        self.harness.queue(meridian_reply(450.0))
        self.assert_ok(self.post_json(meridian_request(450.0)))

        details, _ = self._gap(meridian_request(450.01), miles=450.01)

        self.assertEqual(
            (details["gap_to_mile"], details["gap_leg_miles"], details["max_leg_miles"]),
            ("450.0", "450.1", "450.0"),
        )

    def test_a_first_station_beyond_the_range_with_reserve_0_cannot_be_fixed(self) -> None:
        # TC-17 (h): r 0 → F 500; the station at 520 costs 520 + 1 margin.
        self.harness.index = synthetic_index([station(1, point=meridian_point(520.0))])

        details, message = self._gap(meridian_request(900.0, reserve_gallons=0), miles=900.0)

        self.assertEqual(
            details,
            {
                "gap_from_mile": "0.0",
                "gap_to_mile": "520.0",
                "gap_leg_miles": "521.0",
                "max_leg_miles": "500.0",
                "hint": NOT_FIXABLE,
            },
        )
        self.assertIn("from the start the next stop (a station at mile 520.0)", message)

    def test_start_fuel_equal_to_the_reserve_is_worded_without_a_zero_length_gap(self) -> None:
        # TC-17 (f) / review-auditor N1: f0 = r = 5 with a station at mile 0.
        self.harness.index = synthetic_index([station(1, point=meridian_point(0.0))])
        payload = meridian_request(100.0, current_fuel_gallons=5, reserve_gallons=5)

        details, message = self._gap(payload, miles=100.0)

        self.assertEqual(
            details,
            {
                "gap_from_mile": "0.0",
                "gap_to_mile": "0.0",
                "gap_leg_miles": "1.0",
                "max_leg_miles": "0.0",
                # (5, 0) reaches the station at mile 0 and buys there, so both settings help.
                "hint": "start with more fuel or lower reserve_gallons",
            },
        )
        self.assertEqual(
            message,
            "Cannot plan this trip among the stations checked: start fuel equals "
            "reserve_gallons, so the truck has no usable fuel to leave the start.",
        )

    def test_an_800_mile_gap_after_the_last_station_is_named_with_1_call(self) -> None:
        # TC-19: stations only up to mile 150; r 0 → U 490 < 650 + 1 margin.
        self.harness.index = synthetic_index(
            [
                station(1, point=meridian_point(50.0), price="3.100"),
                station(2, point=meridian_point(100.0), price="3.200"),
                station(3, point=meridian_point(150.0), price="3.300"),
            ]
        )

        details, message = self._gap(meridian_request(800.0, reserve_gallons=0), miles=800.0)

        self.assertEqual(
            details,
            {
                "gap_from_mile": "150.0",
                "gap_to_mile": "800.0",
                "gap_leg_miles": "651.0",
                "max_leg_miles": "490.0",
                "hint": NOT_FIXABLE,
            },
        )
        self.assertIn("from the station at mile 150.0 the next stop (the finish at", message)
        self.assertEqual(self.calls, 1)


class ShortTripTests(PlanApiTestCase):
    """TC-24 (FR-29) and TC-26 (FR-31) on short synthetic routes."""

    def test_a_full_tank_covers_300_miles_with_no_stop_and_says_so(self) -> None:
        # TC-24, first POST
        self.harness.index = synthetic_index([station(1, point=meridian_point(30.0))])
        self.harness.queue(meridian_reply(300.0))

        body = self.assert_ok(self.post_json(meridian_request(300.0)))

        self.assertEqual(body["fuel_stops"], [])
        self.assertEqual(body["totals"]["total_purchase_cost"], "0.00")
        self.assertEqual(body["finish_fuel_on_arrival_gallons"], "20.0")
        self.assertEqual(
            body["notes"][0],
            "No fuel stop is needed: the start tank (50.0 gal) covers the 300.0-mile trip "
            "with 20.0 gal left.",
        )
        self.assertEqual(self.calls, 1)

    def test_a_trip_with_no_stop_leaves_out_the_detour_money_and_whole_gallon_notes(
        self,
    ) -> None:
        self.harness.index = synthetic_index([station(1, point=meridian_point(30.0))])
        self.harness.queue(meridian_reply(300.0))

        body = self.assert_ok(self.post_json(meridian_request(300.0)))

        self.assertEqual(
            body["notes"],
            [
                "No fuel stop is needed: the start tank (50.0 gal) covers the 300.0-mile trip "
                "with 20.0 gal left.",
                "Start tank (50.0 gal) is not charged; 20.0 gal left at finish.",
                PRICE_NOTE,
                ARRIVAL_ROUNDING_NOTE,
            ],
        )

    def test_start_equal_to_finish_leaves_out_the_stop_notes_too(self) -> None:
        body = self.assert_ok(self.post_json({"start": CHICAGO_POINT, "finish": CHICAGO_POINT}))

        self.assertEqual(
            body["notes"],
            [
                SAME_POINT_NOTE,
                "Start tank (50.0 gal) is not charged; 50.0 gal left at finish.",
                PRICE_NOTE,
                ARRIVAL_ROUNDING_NOTE,
            ],
        )

    def test_one_stop_keeps_the_detour_and_whole_gallon_notes_but_not_the_money_note(
        self,
    ) -> None:
        # With one stop its line cost is the total, so there is no rounding to explain.
        self.harness.index = synthetic_index([station(1, point=meridian_point(30.0))])
        self.harness.queue(meridian_reply(300.0))

        body = self.assert_ok(self.post_json(meridian_request(300.0, current_fuel_gallons=10)))

        self.assertEqual(len(body["fuel_stops"]), 1)
        for note in (DETOUR_NOTE, WHOLE_GALLON_NOTE):
            with self.subTest(note=note):
                self.assertEqual(body["notes"].count(note), 1)
        self.assertNotIn(MONEY_NOTE, body["notes"])
        self.assertNotIn(MONEY_NOTE, body["summary"]["headline"])

    def test_a_fill_whose_next_stop_merged_away_gets_the_neutral_reason_in_the_summary(
        self,
    ) -> None:
        # The optimiser's merge hand case: B rounds to 0 gal, so A's fill reaches the finish.
        self.harness.index = synthetic_index(
            [
                station(1, point=meridian_point(100.0), price="3.000"),
                station(2, point=meridian_point(300.0), price="3.500"),
            ]
        )
        self.harness.queue(meridian_reply(549.0))

        body = self.assert_ok(self.post_json(meridian_request(549.0)))

        [stop] = body["fuel_stops"]
        self.assertEqual((stop["purchase_rule"], stop["gallons_to_buy"]), ("fill_tank", 10))
        [line] = body["summary"]["stops"]
        self.assertTrue(line.endswith(" — fill up."), line)
        self.assertNotIn("costs more", line)

    def test_low_start_fuel_buys_just_enough_at_mile_30(self) -> None:
        # TC-24, low fuel: arrive 7.0, buy ceil(5 + 271/10 - 7.0) = 26 at 3.000.
        self.harness.index = synthetic_index([station(1, point=meridian_point(30.0))])
        self.harness.queue(meridian_reply(300.0))

        body = self.assert_ok(self.post_json(meridian_request(300.0, current_fuel_gallons=10)))

        [stop] = body["fuel_stops"]
        self.assertEqual(
            (stop["route_mile"], stop["fuel_on_arrival_gallons"], stop["purchase_rule"]),
            ("30.0", "7.0", "to_finish"),
        )
        self.assertEqual((stop["gallons_to_buy"], stop["line_cost"]), (26, "78.00"))
        self.assertEqual(body["finish_fuel_on_arrival_gallons"], "6.0")

    def test_fuel_burned_is_driven_miles_over_10_rounded_half_up(self) -> None:
        # 300.5 driven miles → 30.05 gal: HALF_UP shows "30.1" (ROUND_DOWN would say "30.0").
        self.harness.queue(meridian_reply(300.5))

        body = self.assert_ok(self.post_json(meridian_request(300.5)))

        self.assertEqual(body["route"]["driven_distance_miles"], "300.5")
        self.assertEqual(body["totals"]["fuel_burned_gallons"], "30.1")

    def test_assumptions_state_every_planning_constant(self) -> None:
        # TC-26: 15-mile route, f0 30, r 5.
        self.harness.queue(meridian_reply(15.0))

        body = self.assert_ok(
            self.post_json(meridian_request(15.0, current_fuel_gallons=30, reserve_gallons=5))
        )

        assumptions = body["assumptions"]
        self.assertEqual(
            {key: assumptions[key] for key in list(assumptions)[:11]},
            {
                "mpg": "10",
                "tank_gallons": "50",
                "planning_tank_gallons": "49",
                "reserve_gallons": "5.0",
                "start_fuel_gallons": "30.0",
                "start_fuel_charged": False,
                "effective_range_miles": {
                    "first_leg": "250.0",
                    "after_purchase": "440.0",
                    "hard_max": "450.0",
                },
                "max_detour_miles": "5.0",
                "unverified_stop_margin_miles": "1.0",
                "detour_method": "straight_line_estimate",
                "price_rule": (
                    "lowest listed CSV price per station among rows with a resolved location"
                ),
            },
        )
        self.assertEqual(assumptions["price_source"], "provided CSV snapshot, not live")
        self.assertEqual(assumptions["dataset_version"], self.harness.index.dataset_version)
        self.assertEqual(assumptions["algorithm_version"], "1")
        self.assertEqual(len(assumptions["attribution"]), 4)
        self.assertEqual(body["route"]["planning_margin_miles"], "0.0")
        self.assertFalse(any("safety margin" in note for note in body["notes"]))


class MapUrlTests(PlanApiTestCase):
    """TC-28 (FR-33, spec 01 Q-4 d): `map_url` is absolute, per request, and opens the map."""

    def _plan_id_and_map_url(self, **extra: str) -> tuple[str, str]:
        body = self.assert_ok(
            self.post_json({"start": CHICAGO_POINT, "finish": CHICAGO_POINT}, **extra)
        )
        return body["plan_id"], body["map_url"]

    def test_map_url_is_absolute_on_the_request_host_and_names_the_plan(self) -> None:
        plan_id, map_url = self._plan_id_and_map_url()

        self.assertEqual(map_url, f"http://testserver/api/v1/fuel-plans/{plan_id}/map")

    @override_settings(ALLOWED_HOSTS=["localhost"])
    def test_map_url_follows_the_host_of_each_request(self) -> None:
        _, map_url = self._plan_id_and_map_url(HTTP_HOST="localhost")

        self.assertTrue(map_url.startswith("http://localhost/api/v1/fuel-plans/"))

    def test_the_map_url_of_a_saved_plan_opens_the_map_page_for_get_and_head(self) -> None:
        _, map_url = self._plan_id_and_map_url()

        page, head = self.client.get(map_url), self.client.head(map_url)

        self.assertEqual(page.status_code, 200)
        self.assertEqual(page["Content-Type"], "text/html; charset=utf-8")
        self.assertTrue(page.content.startswith(b"<!doctype html>"))
        self.assertEqual(head.status_code, 200)

    def test_a_map_url_with_an_unknown_plan_id_is_plan_not_found(self) -> None:
        unknown_id = "00000000-0000-4000-8000-000000000000"

        response = self.client.get(f"/api/v1/fuel-plans/{unknown_id}/map")

        details = assert_envelope(self, response, status=404, code="plan_not_found")
        self.assertEqual(details, {"plan_id": unknown_id})

    def test_other_methods_on_the_map_route_are_405_with_allow(self) -> None:
        _, map_url = self._plan_id_and_map_url()

        response = self.client.post(map_url)

        details = assert_envelope(self, response, status=405, code="method_not_allowed")
        self.assertEqual(details, {"allowed": ["GET", "HEAD"]})
        self.assertEqual(response["Allow"], "GET, HEAD")

    def test_a_plan_id_that_is_not_a_uuid_is_the_plain_404(self) -> None:
        response = self.client.get("/api/v1/fuel-plans/not-a-uuid/map")

        assert_envelope(self, response, status=404, code="not_found")

    def test_a_disallowed_host_gets_the_400_envelope(self) -> None:
        with self.assertLogs("django.security.DisallowedHost", "ERROR"):
            response = self.post_json(
                {"start": CHICAGO_POINT, "finish": CHICAGO_POINT}, HTTP_HOST="evil.example"
            )

        assert_envelope(self, response, status=400, code="invalid_request")


class StationDataMissingTests(PlanApiTestCase):
    """TC-43 (FR-43): no imported stations → 500 naming the import, before any call."""

    def setUp(self) -> None:
        super().setUp()
        self.harness.station_loader = StationIndex.from_database  # the test DB is empty

    def _assert_station_data_missing(self, response: object) -> None:
        details = assert_envelope(self, response, status=500, code="internal_error")
        self.assertEqual(details, {})
        message = json_body(response)["error"]["message"]
        self.assertIn("station data not imported", message.lower())
        self.assertIn("python manage.py import_fuel_stations", message)
        self.assertEqual(self.calls, 0)

    def test_a_valid_request_without_stations_is_500_with_0_calls(self) -> None:
        self._assert_station_data_missing(self.post_json(meridian_request(300.0)))

    def test_start_equal_to_finish_also_needs_station_data(self) -> None:
        self._assert_station_data_missing(
            self.post_json({"start": CHICAGO_POINT, "finish": CHICAGO_POINT})
        )

    def test_location_errors_still_come_first(self) -> None:
        response = self.post_json({"start": {"city": "Anywhere", "state": "AK"}, "finish": CHICAGO})

        assert_envelope(self, response, status=422, code="unsupported_location")
