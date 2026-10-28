"""Spec 01 TC-7 rows e/f/g, TC-8, TC-9 and TC-10: the OSRM client and its call budget.

Every provider reply is faked; `transport.count` is the number of routing calls.
The plan-path rows of TC-7 (a–d) and the HTTP 200 half of TC-9 close in 2C.
"""

import json
import socket
import ssl
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

from django.test import override_settings

from fuelplan.errors import (
    CallBudgetExceeded,
    FuelPlanError,
    RoutingRateLimited,
    response_for,
)
from fuelplan.http import ResponseTooLarge, TransportError, TransportTimeout, require_http_base_url
from fuelplan.models import ProviderSlot
from fuelplan.planner import PlanOutcome
from fuelplan.routing import (
    MAX_ROUTE_BYTES,
    USER_AGENT,
    CallBudget,
    OsrmRoute,
    ProviderThrottle,
    RoutingClient,
    build_routing_client,
    parse_route_response,
    retry_after_seconds,
    route_url,
)

from .base import (
    ApiTestCase,
    FakeClock,
    FakeTransport,
    OfflineTestCase,
    assert_envelope,
    stub_outcome,
)

START = (-87.65005, 41.85003)
FINISH = (-104.9847, 39.73915)
BASE = {
    "start": {"latitude": START[1], "longitude": START[0]},
    "finish": {"latitude": FINISH[1], "longitude": FINISH[0]},
}
LINE = [[-87.65005, 41.85003], [-87.7, 41.9], [-104.9847, 39.73915]]
ROUTING_TIMEOUTS = {
    "FUELPILOT_ROUTING_CONNECT_TIMEOUT_S": 3,
    "FUELPILOT_ROUTING_READ_TIMEOUT_S": 10,
}


def ok_route(**route_overrides: object) -> dict:
    """An OSRM `Ok` reply; keyword arguments replace fields of `routes[0]`."""
    route = {
        "distance": 1_614_524.6,
        "duration": 55_000.0,
        "geometry": {"type": "LineString", "coordinates": LINE},
        **route_overrides,
    }
    return {"code": "Ok", "routes": [route], "waypoints": [{"distance": 3.2}, {"distance": 1.1}]}


class SpyThrottle:
    """A provider slot that always (or never) grants, counting every attempt."""

    def __init__(self, *, grants: bool = True) -> None:
        self.grants = grants
        self.calls = 0

    def try_acquire(self) -> bool:
        self.calls += 1
        return self.grants


def client_from_settings(
    transport: FakeTransport, throttle: object, *, base_url: str = "https://osrm.test"
) -> RoutingClient:
    """`build_routing_client()` with the fake transport and throttle swapped in."""
    with (
        override_settings(**ROUTING_TIMEOUTS, FUELPILOT_ROUTING_BASE_URL=base_url),
        mock.patch("fuelplan.routing.HttpClientTransport", return_value=transport),
        mock.patch("fuelplan.routing.ProviderThrottle", return_value=throttle),
    ):
        return build_routing_client()


def _fetch(client: RoutingClient, budget: CallBudget) -> OsrmRoute:
    return client.fetch_route(start=START, finish=FINISH, budget=budget)


def provider_failure_rows() -> dict[str, tuple[object, int, str]]:
    """TC-9: each OSRM outcome class → (queued reply or error, HTTP status, §7a code)."""

    def body(payload: object) -> bytes:
        return json.dumps(payload).encode()

    def code(name: str, status: int = 400) -> tuple[int, bytes, dict]:
        return (status, body({"code": name, "message": "provider text"}), {})

    no_route_line = {"type": "LineString", "coordinates": [[-87.65, 41.85]]}
    return {  # name: (reply, HTTP status, code)
        "connect timeout": (TransportTimeout("connect"), 504, "routing_timeout"),
        "slow body": (TransportTimeout("body"), 504, "routing_timeout"),
        "429 Retry-After 120": (
            (429, b"", {"Retry-After": "120"}),
            503,
            "routing_rate_limited",
        ),
        "429 without header": ((429, b"", {}), 503, "routing_rate_limited"),
        "HTTP 500": ((500, b"oops", {}), 502, "routing_unavailable"),
        "HTTP 503": ((503, b"busy", {}), 502, "routing_unavailable"),
        "connection refused": (
            _wrapped(ConnectionRefusedError()),
            502,
            "routing_unavailable",
        ),
        "DNS failure": (_wrapped(socket.gaierror("no host")), 502, "routing_unavailable"),
        "TLS failure": (_wrapped(ssl.SSLError("bad cert")), 502, "routing_unavailable"),
        "non-JSON 200": ((200, b"<html>", {}), 502, "routing_unavailable"),
        "Ok with no routes": (
            (200, body({**ok_route(), "routes": []}), {}),
            502,
            "routing_unavailable",
        ),
        "Ok with 1 point": (
            (200, body(ok_route(geometry=no_route_line)), {}),
            502,
            "routing_unavailable",
        ),
        "Ok with NaN coordinates": (
            (200, body(ok_route()).replace(b"-87.7", b"NaN"), {}),
            502,
            "routing_unavailable",
        ),
        "body over 8 MiB": (ResponseTooLarge("big"), 502, "routing_unavailable"),
        "302 redirect": (
            (302, b"", {"Location": "http://elsewhere/"}),
            502,
            "routing_unavailable",
        ),
        "418": ((418, b"teapot", {}), 502, "routing_unavailable"),
        "NoRoute": (code("NoRoute"), 422, "no_route"),
        "NoSegment": (code("NoSegment"), 422, "no_route"),
        "InvalidQuery": (code("InvalidQuery"), 500, "internal_error"),
        "InvalidUrl": (code("InvalidUrl"), 500, "internal_error"),
        "TooBig": (code("TooBig"), 500, "internal_error"),
        "DisabledDataset": (code("DisabledDataset"), 502, "routing_unavailable"),
        "Weird": (code("Weird"), 502, "routing_unavailable"),
    }


class ProviderFailureMappingTests(OfflineTestCase):
    """TC-9 (FR-10, FR-11, AC-9): each provider outcome → its §7a status, 1 call, no retry."""

    def _queue(self, transport: FakeTransport, reply: object) -> None:
        if isinstance(reply, Exception):
            transport.queue_exception(reply)
        else:
            status, body, headers = reply
            transport.queue_response(status, body, headers=headers)

    def _fetch_failure(self, reply: object) -> tuple[FuelPlanError, FakeTransport, CallBudget]:
        transport, budget = FakeTransport(), CallBudget()
        self._queue(transport, reply)
        client = client_from_settings(transport, SpyThrottle())
        with (
            self.assertLogs("fuelplan.routing", level="INFO"),
            self.assertRaises(FuelPlanError) as raised,
        ):
            _fetch(client, budget)
        return raised.exception, transport, budget

    def test_provider_failure_mapping(self) -> None:
        rows = provider_failure_rows()
        self.assertEqual(len(rows), 23)
        for name, (reply, status, error_code) in rows.items():
            with self.subTest(row=name):
                error, transport, budget = self._fetch_failure(reply)

                details = assert_envelope(self, response_for(error), status=status, code=error_code)
                self.assertEqual(transport.count, 1)
                self.assertEqual(budget.used, 1)
                self.assertEqual(details["routing_calls"], 1)
                if status in (502, 503, 504):
                    self.assertIs(details["retryable"], True)
                else:
                    self.assertNotIn("retryable", details)

    def test_extra_provider_replies_map_by_status_first_then_by_code(self) -> None:
        def body(payload: object) -> bytes:
            return json.dumps(payload).encode()

        rows = {
            "InvalidOptions": ((400, body({"code": "InvalidOptions"}), {}), 500),
            "InvalidValue": ((400, body({"code": "InvalidValue"}), {}), 500),
            "InvalidService": ((400, body({"code": "InvalidService"}), {}), 500),
            "InvalidVersion": ((400, body({"code": "InvalidVersion"}), {}), 500),
            "503 whose body says NoRoute": ((503, body({"code": "NoRoute"}), {}), 502),
            "200 whose body says NoRoute": ((200, body({"code": "NoRoute"}), {}), 422),
            "429 with an HTML body": ((429, b"<html>slow down</html>", {}), 503),
            "400 that is not JSON": ((400, b"bad", {}), 502),
            "400 without a code": ((400, body({"message": "x"}), {}), 502),
            "Ok with HTTP 400": ((400, body(ok_route()), {}), 502),
            "code is a number": ((400, body({"code": 5}), {}), 502),
            "lower-case invalidquery": ((400, body({"code": "invalidquery"}), {}), 502),
            "empty 200 body": ((200, b"", {}), 502),
            "invalid UTF-8": ((200, b"\xff\xfe", {}), 502),
            "JSON list": ((200, b"[]", {}), 502),
            "50,000 nested lists (RecursionError)": ((200, b"[" * 50_000 + b"]" * 50_000, {}), 502),
            "no geometry": ((200, body(ok_route(geometry=None)), {}), 502),
            "geometry is a Point": (
                (200, body(ok_route(geometry={"type": "Point", "coordinates": [1, 2]})), {}),
                502,
            ),
            "a position is a string": (
                (
                    200,
                    body(ok_route(geometry={"type": "LineString", "coordinates": ["a", "b"]})),
                    {},
                ),
                502,
            ),
            "a position holds a bool": (
                (
                    200,
                    body(
                        ok_route(
                            geometry={"type": "LineString", "coordinates": [[True, 1], [2, 3]]}
                        )
                    ),
                    {},
                ),
                502,
            ),
            "latitude out of range": (
                (
                    200,
                    body(
                        ok_route(geometry={"type": "LineString", "coordinates": [[0, 91], [0, 1]]})
                    ),
                    {},
                ),
                502,
            ),
            "distance missing": ((200, body(ok_route(distance=None)), {}), 502),
            "distance negative": ((200, body(ok_route(distance=-1)), {}), 502),
            'distance "abc"': ((200, body(ok_route(distance="abc")), {}), 502),
            "distance true": ((200, body(ok_route(distance=True)), {}), 502),
            "distance 1e400": ((200, body(ok_route()).replace(b"1614524.6", b"1e400"), {}), 502),
            "coordinate 1e400": ((200, body(ok_route()).replace(b"-87.7", b"1e400"), {}), 502),
            "no waypoints": ((200, body({**ok_route(), "waypoints": None}), {}), 502),
            "only 1 waypoint": (
                (200, body({**ok_route(), "waypoints": [{"distance": 1}]}), {}),
                502,
            ),
            "3 waypoints": (
                (200, body({**ok_route(), "waypoints": [{"distance": 1}] * 3}), {}),
                502,
            ),
        }
        for name, (reply, status) in rows.items():
            with self.subTest(row=name):
                error, transport, budget = self._fetch_failure(reply)

                self.assertEqual(error.status, status)
                self.assertEqual((transport.count, budget.used), (1, 1))
                self.assertEqual(error.details["routing_calls"], 1)

    def test_an_unexpected_bug_propagates_and_is_never_dressed_up_as_502(self) -> None:
        transport, budget = FakeTransport(), CallBudget()
        transport.queue_exception(RuntimeError("bug"))
        client = client_from_settings(transport, SpyThrottle())

        with self.assertRaises(RuntimeError) as raised:
            _fetch(client, budget)

        self.assertNotIsInstance(raised.exception, FuelPlanError)
        self.assertEqual((transport.count, budget.used), (1, 1))

    def test_provider_429_retry_after_is_clamped_and_echoed_in_header_and_details(self) -> None:
        for header, expected in (("120", 60), (None, 2)):
            with self.subTest(header=header):
                headers = {} if header is None else {"Retry-After": header}
                error, _, _ = self._fetch_failure((429, b"", headers))

                response = response_for(error)
                details = assert_envelope(self, response, status=503, code="routing_rate_limited")
                self.assertEqual(details["retry_after_seconds"], expected)
                self.assertEqual(response["Retry-After"], str(expected))
                self.assertIn("rate-limiting", error.message)

    def test_retry_after_parse_table(self) -> None:
        cases = {
            "30": 30,
            "0": 1,
            "120": 60,
            "61": 60,  # just above the cap
            "60": 60,
            None: 2,
            "abc": 2,
            "1.5": 2,
            "-5": 2,
            "²": 2,
            "Wed, 21 Oct 2026 07:28:00 GMT": 2,
            " 7 ": 7,
            "9" * 5000: 60,  # more digits than int() converts
            "0" * 4400 + "7": 7,
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                headers = {} if header is None else {"retry-after": header}
                self.assertEqual(retry_after_seconds(headers), expected)

    def test_the_call_uses_the_configured_timeouts_size_cap_and_user_agent(self) -> None:
        transport = FakeTransport()
        transport.queue_json(200, ok_route())

        _fetch(client_from_settings(transport, SpyThrottle()), CallBudget())

        [sent] = transport.requests
        self.assertEqual(
            (sent.method, sent.connect_timeout_s, sent.total_timeout_s), ("GET", 3, 10)
        )
        self.assertEqual(sent.max_bytes, MAX_ROUTE_BYTES)
        self.assertEqual(MAX_ROUTE_BYTES, 8 * 1024 * 1024)
        self.assertEqual(sent.headers["User-Agent"], USER_AGENT)

    def test_no_message_carries_provider_text_or_the_base_url(self) -> None:
        reply = (
            400,
            json.dumps({"code": "InvalidQuery", "message": "secret osrm.test"}).encode(),
            {},
        )
        error, _, _ = self._fetch_failure(reply)

        content = response_for(error).content
        self.assertNotIn(b"secret", content)
        self.assertNotIn(b"osrm.test", content)


def _wrapped(cause: BaseException) -> TransportError:
    """A TransportError raised from `cause`, as the real transport does."""
    try:
        raise TransportError("failed calling the provider") from cause
    except TransportError as error:
        return error


class ValidRouteTests(OfflineTestCase):
    """FR-36 (validated route only) and FR-42 (snap distances parsed)."""

    def _parse(self, payload: dict) -> OsrmRoute:
        response = FakeTransport()
        response.queue_json(200, payload)
        return parse_route_response(response.responses[0], routing_calls=1)

    def test_an_ok_reply_becomes_a_route_with_full_geometry_and_snap_distances(self) -> None:
        route = self._parse(ok_route())

        self.assertEqual(route.distance_m, 1_614_524.6)
        self.assertEqual(route.duration_s, 55_000.0)
        self.assertEqual(route.points, tuple(tuple(position) for position in LINE))
        self.assertEqual(route.snap_m, (3.2, 1.1))

    def test_zero_length_geometry_is_refused_when_the_distance_shows_as_at_least_0_1_mi(
        self,
    ) -> None:
        same_point = {"type": "LineString", "coordinates": [[-87.65, 41.85], [-87.65, 41.85]]}
        # Owner rule (2026-10-28): compare the distance as shown, 0.1 mi HALF_UP.
        rows = {
            "160.9344 m = exactly 0.1 mi": (160.9344, True),
            "160.9 m shows as 0.1 mi": (160.9, True),
            "80.4672 m = 0.05 mi rounds up to 0.1": (80.4672, True),
            "80.4 m shows as 0.0 mi": (80.4, False),
            "0 m: both ends snapped to one spot": (0.0, False),
        }
        for name, (distance_m, is_refused) in rows.items():
            with self.subTest(row=name):
                payload = ok_route(distance=distance_m, geometry=same_point)
                transport = FakeTransport()
                transport.queue_json(200, payload)
                reply = transport.responses[0]
                if is_refused:
                    with (
                        self.assertLogs("fuelplan.routing", "WARNING"),
                        self.assertRaises(FuelPlanError) as raised,
                    ):
                        parse_route_response(reply, routing_calls=1)
                    self.assertEqual(raised.exception.status, 502)
                else:
                    self.assertEqual(
                        parse_route_response(reply, routing_calls=1).distance_m, distance_m
                    )


class CallBudgetTests(OfflineTestCase):
    """TC-8 (FR-10, AC-8): the fourth call is blocked before the slot wait and the socket."""

    def test_budget_blocks_the_fourth_call_before_the_slot_and_socket(self) -> None:
        budget = CallBudget(3)
        for _ in range(3):
            budget.spend()
        self.assertEqual((budget.used, budget.remaining()), (3, 0))
        with self.assertLogs("fuelplan.routing", "ERROR"), self.assertRaises(CallBudgetExceeded):
            budget.spend()

        throttle, transport = SpyThrottle(), FakeTransport()
        client = client_from_settings(transport, throttle)
        with (
            self.assertLogs("fuelplan.routing", "ERROR"),
            self.assertRaises(CallBudgetExceeded) as raised,
        ):
            _fetch(client, budget)

        self.assertEqual((throttle.calls, transport.count), (0, 0))
        assert_envelope(self, response_for(raised.exception), status=500, code="internal_error")
        self.assertEqual(raised.exception.details, {})

    def test_an_exhausted_budget_never_touches_the_slot_row(self) -> None:
        budget = CallBudget(0)
        clock, transport = FakeClock(), FakeTransport()
        client = client_from_settings(transport, ProviderThrottle(clock=clock))
        before = ProviderSlot.objects.get(pk="osrm").next_allowed_at

        with self.assertLogs("fuelplan.routing", "ERROR"), self.assertRaises(CallBudgetExceeded):
            _fetch(client, budget)

        self.assertEqual(ProviderSlot.objects.get(pk="osrm").next_allowed_at, before)
        self.assertEqual(transport.count, 0)

    def test_slot_timeout_spends_no_budget(self) -> None:
        budget, transport = CallBudget(), FakeTransport()
        client = client_from_settings(transport, SpyThrottle(grants=False))

        with (
            self.assertLogs("fuelplan.routing", "WARNING"),
            self.assertRaises(RoutingRateLimited) as raised,
        ):
            _fetch(client, budget)

        self.assertEqual((budget.used, transport.count), (0, 0))
        response = response_for(raised.exception)
        details = assert_envelope(self, response, status=503, code="routing_rate_limited")
        self.assertEqual(details, {"routing_calls": 0, "retry_after_seconds": 2, "retryable": True})
        self.assertEqual(response["Retry-After"], "2")
        self.assertIn("queued", raised.exception.message)


class CallCountTests(ApiTestCase):
    """TC-7 rows e, f, g (FR-9, NFR-1): routing calls per path in 2B."""

    def setUp(self) -> None:
        super().setUp()
        self.transport = FakeTransport()
        self.clock = FakeClock()
        self.client_under_test = client_from_settings(
            self.transport, ProviderThrottle(clock=self.clock)
        )

    def _route_through_the_seam(self, *, calls: int = 1):
        """A stand-in planner that makes `calls` route calls with the view's budget."""
        client = self.client_under_test

        class CallingPlanner:
            def plan(self, *, plan_request: object, budget: CallBudget) -> PlanOutcome:
                for _ in range(calls):
                    _fetch(client, budget)
                return stub_outcome(routing_calls=budget.used)

        return mock.patch("fuelplan.views.build_trip_planner", return_value=CallingPlanner())

    def test_call_count_per_path(self) -> None:
        # Cold call at client level: exactly 1.
        self.transport.queue_json(200, ok_route())
        budget = CallBudget()
        self.assertIsInstance(_fetch(self.client_under_test, budget), OsrmRoute)
        self.assertEqual((self.transport.count, budget.used), (1, 1))

        # (e) a 400 and a 415 make 0 calls: the planner seam is never reached.
        with self._route_through_the_seam():
            for name, response in {
                "invalid_request": self.post_json({"start": []}),
                "unsupported_media_type": self.post_raw(
                    json.dumps(BASE).encode(), content_type="text/plain"
                ),
            }.items():
                with self.subTest(row=f"(e) {name}"):
                    status = 400 if name == "invalid_request" else 415
                    details = assert_envelope(self, response, status=status, code=name)
                    self.assertNotIn("routing_calls", details)
        self.assertEqual(self.transport.count, 1)

    def test_a_slot_held_longer_than_2_s_gives_503_with_0_calls_over_http(self) -> None:
        ProviderSlot.objects.filter(pk="osrm").update(next_allowed_at=self.clock.now() + 2.5)

        with self._route_through_the_seam(), self.assertLogs("fuelplan.routing", "WARNING"):
            response = self.post_json(BASE)

        details = assert_envelope(self, response, status=503, code="routing_rate_limited")
        self.assertEqual(details["routing_calls"], 0)
        self.assertEqual(response["Retry-After"], str(details["retry_after_seconds"]))
        self.assertEqual(self.transport.count, 0)

    def test_provider_502_and_504_make_exactly_one_call_over_http(self) -> None:
        rows = {
            "502": (lambda: self.transport.queue_body(500, b"down"), 502, "routing_unavailable"),
            "504": (
                lambda: self.transport.queue_exception(TransportTimeout("slow")),
                504,
                "routing_timeout",
            ),
        }
        for name, (queue, status, code) in rows.items():
            with self.subTest(row=f"(g) {name}"):
                self.transport.requests.clear()
                queue()

                with self._route_through_the_seam(), self.assertLogs("fuelplan.routing", "WARNING"):
                    response = self.post_json(BASE)

                details = assert_envelope(self, response, status=status, code=code)
                self.assertEqual((self.transport.count, details["routing_calls"]), (1, 1))
                self.assertIs(details["retryable"], True)

    def test_a_planner_forcing_a_fourth_call_gets_500_after_three_calls(self) -> None:
        for _ in range(3):
            self.transport.queue_json(200, ok_route())

        with self._route_through_the_seam(calls=4), self.assertLogs("fuelplan.routing", "ERROR"):
            response = self.post_json(BASE)

        details = assert_envelope(self, response, status=500, code="internal_error")
        self.assertEqual(details, {})
        self.assertEqual(self.transport.count, 3)


class SsrfTests(OfflineTestCase):
    """TC-10 (FR-12, AC-10): only configured host and path, only `%.6f` floats from input."""

    def test_ssrf_url_shape(self) -> None:
        url = route_url(base_url="https://osrm.example/prefix", start=START, finish=FINISH)

        parts = urlsplit(url)
        self.assertEqual((parts.scheme, parts.hostname), ("https", "osrm.example"))
        number = r"-?\d+\.\d{6}"
        self.assertRegex(
            parts.path, rf"^/prefix/route/v1/driving/{number},{number};{number},{number}$"
        )
        self.assertIn("/-87.650050,41.850030;-104.984700,39.739150", parts.path)  # lon,lat
        self.assertEqual(
            dict(parse_qsl(parts.query)), {"overview": "full", "geometries": "geojson"}
        )

    def test_only_finite_numbers_can_reach_the_url(self) -> None:
        for bad, error in (("../x?y#", TypeError), (True, TypeError), (float("inf"), ValueError)):
            with self.subTest(value=bad), self.assertRaises(error):
                route_url(base_url="https://osrm.example", start=(bad, 41.0), finish=FINISH)

    def test_the_client_calls_the_configured_host_and_base_path_only(self) -> None:
        transport = FakeTransport()
        transport.queue_json(200, ok_route())
        client = client_from_settings(
            transport, SpyThrottle(), base_url="https://osrm.example/prefix/"
        )

        _fetch(client, CallBudget())

        parts = urlsplit(transport.requests[0].url)
        self.assertEqual(parts.hostname, "osrm.example")
        self.assertTrue(parts.path.startswith("/prefix/route/v1/driving/"))
        self.assertEqual(client.base_url, "https://osrm.example/prefix")

    def test_base_urls_with_a_query_fragment_user_or_other_scheme_are_refused(self) -> None:
        for url in (
            "ftp://x",
            "https://x/?a=1",
            "https://x/#f",
            "https://x/?",
            "https://x/#",
            "https://user:pw@x",
            "https://",
            "x",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError) as raised:
                require_http_base_url(url)
            self.assertNotIn("pw", str(raised.exception))
        self.assertEqual(require_http_base_url("https://x/base/"), "https://x/base")
