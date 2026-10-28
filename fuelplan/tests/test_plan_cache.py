"""Spec 01 build 2C-b: route, plan and saved-plan caching over HTTP.

TC-7 (a)–(d), the repeats of TC-19 and TC-24, TC-27, TC-29 (plan hits), TC-30 to TC-34,
FR-43 before the cache, and the query count of each hot path. Requests use
`keep_cache=True`, so only the throttle count is reset between them.
"""

import json
import uuid
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.core.cache import cache, caches
from django.db import DatabaseError, OperationalError
from django.test import override_settings

from fuelplan import planner
from fuelplan.geo import RouteGeometryError
from fuelplan.locations import get_boundary, get_gazetteer
from fuelplan.models import ProviderSlot
from fuelplan.planner import (
    ROUTE_TTL_S,
    CachedRoute,
    build_trip_planner,
    plan_cache_key,
    plan_key_ttl_s,
    planobj_cache_key,
    route_cache_key,
)
from fuelplan.routing import RoutingClient
from fuelplan.stations import StationIndex, get_station_index
from fuelplan.verification import load_detour_verifier, verify_detours_noop

from .base import (
    PLAN_URL,
    ApiTestCase,
    OfflineTestCase,
    assert_envelope,
    cache_keys,
    expire_cache_key,
    throttle_cache_key,
    ttl_of,
)
from .planner_harness import (
    TEST_BASE_URL,
    PlannerHarness,
    json_body,
    meridian_point,
    meridian_reply,
    meridian_request,
    station,
    synthetic_index,
)
from .test_routing import provider_failure_rows

TTL_SLACK_S = 5
CHICAGO_POINT = {"latitude": 41.85003, "longitude": -87.65005}


def meridian_route_key(miles: float) -> str:
    """The route key the planner uses for `meridian_request(miles)` (ends at 5 dp)."""
    (start_lon, start_lat), (finish_lon, finish_lat) = meridian_point(0.0), meridian_point(miles)
    return route_cache_key(
        base_url=TEST_BASE_URL,
        start=(round(start_lon, 5), round(start_lat, 5)),
        finish=(round(finish_lon, 5), round(finish_lat, 5)),
    )


def _keys(prefix: str) -> set[str]:
    return {key for key in cache_keys() if key.startswith(prefix)}


def route_keys() -> set[str]:
    return _keys("route:")


def plan_keys() -> set[str]:
    return _keys("plan:")


def planobj_keys() -> set[str]:
    return _keys("planobj:")


def stored_keys() -> set[str]:
    """Route, plan and saved-plan keys (the throttle's own key is left out)."""
    return route_keys() | plan_keys() | planobj_keys()


def meta(routing_calls: int, route_cache: str, plan_cache: str) -> dict:
    return {"routing_calls": routing_calls, "route_cache": route_cache, "plan_cache": plan_cache}


class PlanCacheTestCase(ApiTestCase):
    """A planner harness, a 300-mile meridian route, and requests that keep the cache."""

    def setUp(self) -> None:
        super().setUp()
        cache.clear()
        self.harness = PlannerHarness(
            index=synthetic_index([station(1, point=meridian_point(30.0))])
        )
        patcher = self.harness.patch_view()
        patcher.start()
        self.addCleanup(patcher.stop)

    @property
    def calls(self) -> int:
        return self.harness.transport.count

    def post(self, payload: dict, **extra: str) -> object:
        return self.post_json(payload, keep_cache=True, **extra)

    def send_without_reset(self, payload: dict) -> object:
        """POST with nothing reset first: the throttle keeps counting (TC-29, query counts)."""
        body = json.dumps(payload).encode()
        return self.client.generic("POST", PLAN_URL, body, content_type="application/json")

    def plan_ok(self, payload: dict, *, reply: dict | None = None) -> dict:
        """POST and return the 200 body; `reply` is queued first when a call is expected."""
        if reply is not None:
            self.harness.queue(reply)
        response = self.post(payload)
        self.assertEqual(response.status_code, 200, response.content)
        return json_body(response)


class CallCountPerPathTests(PlanCacheTestCase):
    """TC-7 (a)–(d) (FR-8, FR-9, NFR-1): `meta.routing_calls` always equals the fake count."""

    def test_call_count_per_path(self) -> None:
        trip = meridian_request(300.0)
        rows = [  # (row, payload, OSRM reply to queue, calls made, meta)
            ("(a) cold", trip, meridian_reply(300.0), 1, meta(1, "miss", "miss")),
            ("(b) same ends, other fuel", {**trip, "current_fuel_gallons": 40}, None, 0, None),
            ("(c) identical repeat", trip, None, 0, meta(0, "skipped", "hit")),
            (
                "(d) start = finish",
                {"start": CHICAGO_POINT, "finish": CHICAGO_POINT},
                None,
                0,
                None,
            ),
        ]
        expected_meta = {
            "(b) same ends, other fuel": meta(0, "hit", "miss"),
            "(d) start = finish": meta(0, "skipped", "miss"),
        }
        for row, payload, reply, calls, row_meta in rows:
            with self.subTest(row=row):
                calls_before = self.calls

                body = self.plan_ok(payload, reply=reply)

                self.assertEqual(self.calls - calls_before, calls)
                self.assertEqual(body["meta"], row_meta or expected_meta[row])
                self.assertEqual(body["meta"]["routing_calls"], calls)

    def test_an_identical_repeat_returns_the_same_plan_id_with_0_calls(self) -> None:
        first = self.plan_ok(meridian_request(300.0), reply=meridian_reply(300.0))

        repeat = self.plan_ok(meridian_request(300.0))

        self.assertEqual(repeat["plan_id"], first["plan_id"])
        self.assertEqual(self.calls, 1)

    def test_start_equal_to_finish_is_saved_and_its_repeat_is_a_plan_hit(self) -> None:
        payload = {"start": CHICAGO_POINT, "finish": CHICAGO_POINT}
        first = self.plan_ok(payload)

        repeat = self.plan_ok(payload)

        self.assertEqual(repeat["plan_id"], first["plan_id"])
        self.assertEqual(repeat["meta"], meta(0, "skipped", "hit"))
        self.assertEqual(self.calls, 0)

    def test_endpoints_that_differ_only_past_5_dp_reuse_the_route(self) -> None:
        self.plan_ok(meridian_request(300.0), reply=meridian_reply(300.0))
        nudged = meridian_request(300.0, current_fuel_gallons=40)
        nudged["start"]["latitude"] += 0.000001

        body = self.plan_ok(nudged)

        self.assertEqual(body["meta"], meta(0, "hit", "miss"))
        self.assertEqual(self.calls, 1)


class RepeatTests(PlanCacheTestCase):
    """The second POSTs of TC-19 and TC-24."""

    def test_a_repeated_infeasible_trip_is_422_again_from_the_cached_route(self) -> None:
        # TC-19: the route key is kept after `no_feasible_plan`, no plan is ever saved.
        self.harness.index = synthetic_index([station(3, point=meridian_point(150.0))])
        payload = meridian_request(800.0, reserve_gallons=0)
        self.harness.queue(meridian_reply(800.0))

        first = self.post(payload)
        repeat = self.post(payload)

        for response in (first, repeat):
            details = assert_envelope(self, response, status=422, code="no_feasible_plan")
            self.assertEqual(details["gap_leg_miles"], "651.0")
        self.assertEqual(self.calls, 1)
        self.assertEqual((len(route_keys()), plan_keys(), planobj_keys()), (1, set(), set()))

    def test_low_fuel_on_a_cached_300_mile_route_is_planned_with_0_calls(self) -> None:
        # TC-24: f0 50 needs no stop; f0 10 on the same ends stops at mile 30.
        self.plan_ok(meridian_request(300.0), reply=meridian_reply(300.0))

        body = self.plan_ok(meridian_request(300.0, current_fuel_gallons=10))

        [stop] = body["fuel_stops"]
        self.assertEqual((stop["route_mile"], stop["gallons_to_buy"]), ("30.0", 26))
        self.assertEqual(body["meta"], meta(0, "hit", "miss"))
        self.assertEqual(self.calls, 1)


class MetaTests(PlanCacheTestCase):
    """TC-27 (FR-32): meta is built per response and never saved."""

    def test_meta_per_path_and_never_in_the_saved_plan(self) -> None:
        cold = self.plan_ok(meridian_request(300.0), reply=meridian_reply(300.0))
        route_hit = self.plan_ok(meridian_request(300.0, current_fuel_gallons=40))
        plan_hit = self.plan_ok(meridian_request(300.0))

        self.assertEqual(
            [cold["meta"], route_hit["meta"], plan_hit["meta"]],
            [meta(1, "miss", "miss"), meta(0, "hit", "miss"), meta(0, "skipped", "hit")],
        )
        saved = cache.get(planobj_cache_key(cold["plan_id"]))
        self.assertNotIn("meta", saved)
        self.assertNotIn("map_url", saved)
        self.assertEqual(saved["plan_id"], cold["plan_id"])
        cold.pop("meta"), plan_hit.pop("meta")
        self.assertEqual(plan_hit, cold)

    def test_a_plan_hit_echoes_this_requests_input_not_the_saved_one(self) -> None:
        # Saved from a city request, hit from coordinates at the same 5-dp point.
        self.plan_ok({"start": {"city": "Chicago", "state": "IL"}, "finish": CHICAGO_POINT})

        body = self.plan_ok({"start": CHICAGO_POINT, "finish": CHICAGO_POINT})

        self.assertEqual(body["meta"]["plan_cache"], "hit")
        self.assertEqual(body["start"]["input"], CHICAGO_POINT)
        self.assertIsNone(body["start"]["matched"])

    def test_a_plan_hit_summary_names_this_requests_places(self) -> None:
        # Saved from a city request, hit from coordinates at the same 5-dp point.
        self.plan_ok({"start": {"city": "Chicago", "state": "IL"}, "finish": CHICAGO_POINT})

        body = self.plan_ok({"start": CHICAGO_POINT, "finish": CHICAGO_POINT})

        self.assertEqual(body["meta"]["plan_cache"], "hit")
        self.assertEqual(
            body["summary"]["headline"],
            "41.85003, -87.65005 → 41.85003, -87.65005: 0.0 mi, no fuel stop needed — "
            "the start tank covers this trip ($0.00).",
        )
        self.assertNotIn("summary", cache.get(planobj_cache_key(body["plan_id"])))

    @override_settings(ALLOWED_HOSTS=["testserver", "localhost"])
    def test_a_plan_hit_builds_map_url_from_this_requests_host(self) -> None:
        first = self.plan_ok(meridian_request(300.0), reply=meridian_reply(300.0))

        response = self.post(meridian_request(300.0), HTTP_HOST="localhost")

        self.assertEqual(
            json_body(response)["map_url"],
            f"http://localhost/api/v1/fuel-plans/{first['plan_id']}/map",
        )

    def test_a_plan_hit_does_no_route_or_geometry_work(self) -> None:
        self.plan_ok(meridian_request(300.0), reply=meridian_reply(300.0))

        with mock.patch.object(
            planner, "simplify_for_response", wraps=planner.simplify_for_response
        ) as simplify:
            self.plan_ok(meridian_request(300.0))

        simplify.assert_not_called()


class ThrottleTests(PlanCacheTestCase):
    """TC-29: plan hits still count against our 30/min POST throttle."""

    def test_the_31st_identical_post_in_a_minute_is_429(self) -> None:
        self.harness.queue(meridian_reply(300.0))
        body = json_body(self.post(meridian_request(300.0)))  # resets the count once
        statuses = []
        for _ in range(30):
            response = self.send_without_reset(meridian_request(300.0))
            statuses.append(response.status_code)

        self.assertEqual(statuses[:29], [200] * 29)
        assert_envelope(self, response, status=429, code="too_many_requests")
        self.assertIn("Retry-After", response)
        self.assertEqual(self.calls, 1)
        self.assertTrue(body["plan_id"])


class NoKeysOnErrorTests(PlanCacheTestCase):
    """TC-30 (FR-36): errors write no plan or saved plan; only a valid `Ok` route is cached."""

    def test_provider_failures_write_no_keys_and_a_resend_calls_again(self) -> None:
        for name, (reply, status, code) in provider_failure_rows().items():
            with self.subTest(row=name):
                cache.clear()
                self.harness.queue(reply)
                calls_before = self.calls

                with self.assertLogs("fuelplan", "INFO"):
                    response = self.post(meridian_request(300.0))
                assert_envelope(self, response, status=status, code=code)
                self.assertEqual(stored_keys(), set())

                resent = self.plan_ok(meridian_request(300.0), reply=meridian_reply(300.0))
                self.assertEqual(resent["meta"], meta(1, "miss", "miss"))
                self.assertEqual(self.calls - calls_before, 2)

    def test_a_slot_held_longer_than_2_s_is_503_with_0_calls_and_no_keys(self) -> None:
        ProviderSlot.objects.filter(pk="osrm").update(
            next_allowed_at=self.harness.clock.now() + 2.5
        )

        with self.assertLogs("fuelplan.routing", "WARNING"):
            response = self.post(meridian_request(300.0))

        details = assert_envelope(self, response, status=503, code="routing_rate_limited")
        self.assertEqual(details["routing_calls"], 0)
        self.assertEqual((self.calls, stored_keys()), (0, set()))

    def test_a_route_whose_geometry_fails_is_never_cached(self) -> None:
        self.harness.queue(meridian_reply(300.0))

        with (
            mock.patch.object(
                planner, "build_route_line", side_effect=RouteGeometryError("broken line")
            ),
            self.assertLogs("fuelplan.planner", "ERROR"),
        ):
            response = self.post(meridian_request(300.0))

        self.assertEqual(assert_envelope(self, response, status=500, code="internal_error"), {})
        self.assertEqual((self.calls, stored_keys()), (1, set()))

    def test_a_database_error_on_a_cache_read_is_a_500_with_0_calls(self) -> None:
        # Reads are not best effort: a broken cache must not turn into silent re-routing.
        self.harness.queue(meridian_reply(300.0))
        cache_class = type(caches["default"])
        real_get = cache_class.get

        def get_failing_on_plan_keys(self, key, default=None, version=None):
            if key.startswith("plan:"):
                raise OperationalError("disk I/O error")
            return real_get(self, key, default, version)

        with (
            mock.patch.object(cache_class, "get", get_failing_on_plan_keys),
            self.assertLogs("fuelplan.errors", "ERROR"),
        ):
            response = self.post(meridian_request(300.0))

        self.assertEqual(assert_envelope(self, response, status=500, code="internal_error"), {})
        self.assertEqual((self.calls, stored_keys()), (0, set()))

    def test_no_feasible_plan_keeps_only_the_route_key(self) -> None:
        self.harness.queue(meridian_reply(800.0))

        response = self.post(meridian_request(800.0, reserve_gallons=0))

        assert_envelope(self, response, status=422, code="no_feasible_plan")
        self.assertEqual((len(route_keys()), plan_keys(), planobj_keys()), (1, set(), set()))


class UnreadableRouteTests(PlanCacheTestCase):
    """A cached route this code cannot read is a miss: fetched again, then overwritten."""

    def test_an_unreadable_cached_route_is_fetched_again_and_replaced(self) -> None:
        good = CachedRoute.at_point((-98.0, 30.0)).to_cache_value()
        rows = {
            "empty dict": {},
            "no snap_m": {key: value for key, value in good.items() if key != "snap_m"},
            "1 point": {**good, "points": [[-98.0, 30.0]]},
            "distance is text (ValueError)": {**good, "distance_m": "far"},
            "points is None (TypeError)": {**good, "points": None},
            "not a dict": "garbage",
        }
        key = meridian_route_key(300.0)
        for name, bad in rows.items():
            with self.subTest(row=name):
                cache.clear()
                cache.set(key, bad)

                with self.assertLogs("fuelplan.planner", "WARNING"):
                    body = self.plan_ok(meridian_request(300.0), reply=meridian_reply(300.0))

                self.assertEqual(body["meta"], meta(1, "miss", "miss"))
                self.assertIsNotNone(CachedRoute.from_cache_value(cache.get(key)))


class RouteReuseTests(PlanCacheTestCase):
    """TC-31 (FR-37): a route is reused for 24 h for the same ends and base URL."""

    def test_route_reuse_follows_ttl_and_base_url(self) -> None:
        self.plan_ok(meridian_request(300.0), reply=meridian_reply(300.0))
        [route_key] = route_keys()
        self.assertAlmostEqual(ttl_of(route_key), ROUTE_TTL_S, delta=TTL_SLACK_S)

        self.plan_ok(meridian_request(300.0, current_fuel_gallons=40))
        self.assertEqual(self.calls, 1)

        expire_cache_key(route_key)
        self.plan_ok(meridian_request(300.0, current_fuel_gallons=30), reply=meridian_reply(300.0))
        self.assertEqual(self.calls, 2)

        self.harness.base_url = "https://osrm-other.test"
        self.plan_ok(meridian_request(300.0, current_fuel_gallons=20), reply=meridian_reply(300.0))
        self.assertEqual(self.calls, 3)
        self.assertEqual(len(route_keys()), 2)

    def test_the_route_key_depends_on_base_url_and_both_ends(self) -> None:
        start, finish = (-98.0, 30.0), (-98.0, 34.0)
        key = route_cache_key(base_url="https://a.test", start=start, finish=finish)

        self.assertTrue(key.startswith("route:v1:"))
        for other in (
            route_cache_key(base_url="https://b.test", start=start, finish=finish),
            route_cache_key(base_url="https://a.test", start=finish, finish=start),
            route_cache_key(base_url="https://a.test", start=start, finish=(-98.0, 34.00001)),
        ):
            self.assertNotEqual(other, key)


def _plan_key(**overrides: object) -> str:
    values = {
        "dataset_version": "sha256:" + "a" * 64,
        "base_url": "https://osrm.test",
        "start": (-98.0, 30.0),
        "finish": (-98.0, 34.0),
        "current_gallons": Decimal("50.0"),
        "reserve_gallons": Decimal("5.0"),
        **overrides,
    }
    return plan_cache_key(**values)


class PlanSavingTests(PlanCacheTestCase):
    """TC-32 (FR-38, FR-39): every success saved under a fresh uuid4 via `cache.add`."""

    def test_two_plans_get_distinct_uuid4_ids(self) -> None:
        first = self.plan_ok(meridian_request(300.0), reply=meridian_reply(300.0))
        second = self.plan_ok(meridian_request(300.0, current_fuel_gallons=40))

        self.assertNotEqual(first["plan_id"], second["plan_id"])
        for body in (first, second):
            self.assertEqual(uuid.UUID(body["plan_id"]).version, 4)

    def test_saved_plan_and_plan_key_ttls_follow_the_setting(self) -> None:
        for plan_ttl_s, plan_key_ttl in ((604_800, 86_400), (3_600, 3_600)):
            with (
                self.subTest(plan_ttl_s=plan_ttl_s),
                override_settings(FUELPILOT_PLAN_TTL_S=plan_ttl_s),
            ):
                cache.clear()

                body = self.plan_ok(meridian_request(300.0), reply=meridian_reply(300.0))

                [plan_key] = plan_keys()
                self.assertAlmostEqual(
                    ttl_of(planobj_cache_key(body["plan_id"])), plan_ttl_s, delta=TTL_SLACK_S
                )
                self.assertAlmostEqual(ttl_of(plan_key), plan_key_ttl, delta=TTL_SLACK_S)

    def test_equal_fuel_values_share_a_plan_and_other_values_do_not(self) -> None:
        first = self.plan_ok(
            meridian_request(300.0, current_fuel_gallons=12.5, reserve_gallons=5),
            reply=meridian_reply(300.0),
        )
        same = [
            {"current_fuel_gallons": "12.50", "reserve_gallons": 5.0},
            {"current_fuel_gallons": 12.5, "reserve_gallons": "5.0"},
        ]
        for fuel in same:
            with self.subTest(fuel=fuel):
                body = self.plan_ok(meridian_request(300.0, **fuel))
                self.assertEqual(body["plan_id"], first["plan_id"])

        other = self.plan_ok(
            meridian_request(300.0, current_fuel_gallons=12.5, reserve_gallons=5.1)
        )
        self.assertNotEqual(other["plan_id"], first["plan_id"])
        self.assertEqual(self.calls, 1)
        self.assertEqual(
            _plan_key(reserve_gallons=Decimal("5")), _plan_key(reserve_gallons=Decimal("5.0"))
        )

    def test_a_saved_plan_that_cannot_be_added_leaves_no_plan_key(self) -> None:
        with (
            mock.patch.object(type(caches["default"]), "add", return_value=False),
            self.assertLogs("fuelplan.planner", "WARNING") as logs,
        ):
            body = self.plan_ok(meridian_request(300.0), reply=meridian_reply(300.0))

        self.assertTrue(body["plan_id"])
        self.assertEqual(plan_keys(), set())
        self.assertIn("no plan key", "\n".join(logs.output))

    def test_a_database_error_on_a_cache_write_is_logged_and_the_plan_still_answered(
        self,
    ) -> None:
        with (
            mock.patch.object(type(caches["default"]), "add", side_effect=DatabaseError("locked")),
            self.assertLogs("fuelplan.planner", "WARNING"),
        ):
            body = self.plan_ok(meridian_request(300.0), reply=meridian_reply(300.0))

        self.assertEqual(body["meta"], meta(1, "miss", "miss"))
        self.assertEqual((plan_keys(), planobj_keys()), (set(), set()))
        self.assertEqual(len(route_keys()), 1)


class VersionTests(PlanCacheTestCase):
    """TC-33 (FR-40): another dataset or algorithm version never returns an older plan."""

    def test_plan_keys_differ_for_every_input_that_changes_the_plan(self) -> None:
        key = _plan_key()
        self.assertTrue(key.startswith(f"plan:v1:sha256:{'a' * 64}:1:"))
        self.assertLess(len(key), 200)
        others = [
            _plan_key(dataset_version="sha256:" + "b" * 64),
            _plan_key(base_url="https://other.test"),
            _plan_key(start=(-98.0, 30.00001)),
            _plan_key(finish=(-98.0, 34.00001)),
            _plan_key(current_gallons=Decimal("49.9")),
            _plan_key(reserve_gallons=Decimal("5.1")),
        ]
        with mock.patch.object(planner, "ALGORITHM_VERSION", "2"):
            others.append(_plan_key())
        self.assertEqual(len({key, *others}), len(others) + 1)

    def test_a_new_dataset_version_is_a_plan_miss_on_the_cached_route(self) -> None:
        first = self.plan_ok(meridian_request(300.0), reply=meridian_reply(300.0))
        self.harness.index = synthetic_index(
            [station(1, point=meridian_point(30.0))], dataset_version="sha256:" + "b" * 64
        )

        body = self.plan_ok(meridian_request(300.0))

        self.assertNotEqual(body["plan_id"], first["plan_id"])
        self.assertEqual(body["meta"], meta(0, "hit", "miss"))

    def test_a_new_algorithm_version_is_a_plan_miss(self) -> None:
        first = self.plan_ok(meridian_request(300.0), reply=meridian_reply(300.0))

        with mock.patch.object(planner, "ALGORITHM_VERSION", "2"):
            body = self.plan_ok(meridian_request(300.0))

        self.assertNotEqual(body["plan_id"], first["plan_id"])
        self.assertEqual(body["assumptions"]["algorithm_version"], "2")


class DanglingPlanKeyTests(PlanCacheTestCase):
    """TC-34 (FR-41): a plan key whose saved plan is gone is a miss and is rebuilt."""

    def test_a_plan_key_without_its_saved_plan_is_rebuilt_from_the_cached_route(self) -> None:
        first = self.plan_ok(meridian_request(300.0), reply=meridian_reply(300.0))
        cache.delete(planobj_cache_key(first["plan_id"]))

        with self.assertLogs("fuelplan.planner", "INFO"):
            body = self.plan_ok(meridian_request(300.0))

        self.assertNotEqual(body["plan_id"], first["plan_id"])
        self.assertEqual(body["meta"], meta(0, "hit", "miss"))
        self.assertIsNotNone(cache.get(planobj_cache_key(body["plan_id"])))
        self.assertEqual(self.calls, 1)

    def test_the_plan_key_never_outlives_the_saved_plan(self) -> None:
        self.assertEqual(plan_key_ttl_s(3_600), 3_600)
        self.assertEqual(plan_key_ttl_s(604_800), 86_400)


class StationDataBeforeCacheTests(PlanCacheTestCase):
    """TC-43 (FR-43): the station check runs before the cache, so a saved plan cannot hide it."""

    def test_a_saved_plan_does_not_hide_missing_station_data(self) -> None:
        self.plan_ok(meridian_request(300.0), reply=meridian_reply(300.0))
        keys_before = stored_keys()
        self.harness.station_loader = StationIndex.from_database  # the test DB is empty

        response = self.post(meridian_request(300.0))

        assert_envelope(self, response, status=500, code="internal_error")
        self.assertIn("station data not imported", json_body(response)["error"]["message"].lower())
        self.assertEqual(stored_keys(), keys_before)
        self.assertEqual(self.calls, 1)


class QueryCountTests(PlanCacheTestCase):
    """No N+1: each hot path makes a fixed number of queries, whatever the station count."""

    # Measured on the DatabaseCache. A get is 1 query; a set or add is 5 (cull COUNT,
    # SAVEPOINT, SELECT, INSERT, RELEASE). Every POST pays the throttle: get + set = 6.
    # Plan hit: + plan key get + planobj get = 8.
    # Route hit: + plan key get + route get + planobj add + plan key set = 18.
    # Cold: + plan key get + route get + slot UPDATE + route set + planobj add + plan set = 24.
    PLAN_HIT_QUERIES = 8
    ROUTE_HIT_QUERIES = 18
    COLD_QUERIES = 24

    def _index_with(self, count: int) -> StationIndex:
        records = [
            station(number, point=meridian_point(20.0 + (number - 1) * (270.0 / count)))
            for number in range(1, count + 1)
        ]
        return synthetic_index(records)

    def _assert_queries(self, payload: dict, *, expected: int) -> None:
        cache.delete(throttle_cache_key())  # outside the count
        with self.assertNumQueries(expected):
            response = self.send_without_reset(payload)
        self.assertEqual(response.status_code, 200, response.content)

    def test_each_path_makes_the_same_queries_with_2_or_200_stations(self) -> None:
        for count in (2, 200):
            with self.subTest(stations=count):
                cache.clear()
                self.harness.index = self._index_with(count)
                self.harness.queue(meridian_reply(300.0))
                self.harness.clock.advance(5.0)  # the call slot is free again

                cold = meridian_request(300.0, current_fuel_gallons=10)
                self._assert_queries(cold, expected=self.COLD_QUERIES)
                route_hit = meridian_request(300.0, current_fuel_gallons=20)
                self._assert_queries(route_hit, expected=self.ROUTE_HIT_QUERIES)
                self._assert_queries(route_hit, expected=self.PLAN_HIT_QUERIES)


class PlannerWiringTests(OfflineTestCase):
    """`build_trip_planner()` wires the production pieces (review-auditor 2C)."""

    @override_settings(FUELPILOT_PLAN_TTL_S=604_800)
    def test_the_real_planner_uses_the_real_loaders_cache_ttl_and_no_op_verifier(self) -> None:
        trip_planner = build_trip_planner()

        resolver = trip_planner._resolver
        self.assertIs(resolver._gazetteer_loader, get_gazetteer)
        self.assertIs(resolver._boundary_loader, get_boundary)
        self.assertIs(trip_planner._station_loader, get_station_index)
        self.assertIsInstance(trip_planner._routing, RoutingClient)
        self.assertEqual(trip_planner._routing.base_url, settings.FUELPILOT_ROUTING_BASE_URL)
        self.assertIs(trip_planner._cache, caches["default"])
        self.assertEqual(trip_planner._plan_ttl_s, 604_800)
        self.assertIs(trip_planner._verifier_loader, load_detour_verifier)
        self.assertIs(trip_planner._verifier_loader(), verify_detours_noop)

    @override_settings(FUELPILOT_PLAN_TTL_S=3_600)
    def test_the_real_planner_reads_the_plan_ttl_setting(self) -> None:
        self.assertEqual(build_trip_planner()._plan_ttl_s, 3_600)
