"""Spec 01 TC-1 (FR-1, FR-5, NFR-10), TC-5 (FR-6) and the "XX" half of TC-4.

These tests stop at validation: a valid request reaches the planner seam
(`views.build_trip_planner`), which they replace with a recording fake. The 200 halves
(echo in `assumptions`, the 15-mi fixture route) are in `test_plan_api.py`.
"""

import json
from decimal import Decimal
from unittest import mock

from rest_framework.exceptions import ValidationError

from fuelplan.errors import InvalidRequest, flatten_errors
from fuelplan.planner import PlanOutcome
from fuelplan.routing import CALL_LIMIT
from fuelplan.serializers import CityInput, PlanRequest, PointInput, parse_plan_request

from .base import ApiTestCase, FakeTransport, OfflineTestCase, assert_envelope, stub_outcome

CHICAGO = {"latitude": 41.85003, "longitude": -87.65005}
DENVER = {"latitude": 39.73915, "longitude": -104.9847}
BASE = {"start": CHICAGO, "finish": DENVER}
MAX_BODY_BYTES = 10_240


def _with_start(start: object) -> dict:
    return {**BASE, "start": start}


def _padded(payload: dict, *, size: int) -> bytes:
    """`payload` as JSON padded with trailing spaces to exactly `size` bytes."""
    text = json.dumps(payload).encode()
    return text + b" " * (size - len(text))


def _raw_start(literal: str) -> bytes:
    """A body whose start latitude is the raw JSON text `literal` (NaN, Infinity, 1e400)."""
    return (
        b'{"start":{"latitude":' + literal.encode() + b',"longitude":-87.65005},'
        b'"finish":{"latitude":39.73915,"longitude":-104.9847}}'
    )


class SeamRecorder:
    """Stands in for the planner: records what the view hands over and answers 200."""

    def __init__(self) -> None:
        self.calls: list[tuple[PlanRequest, object]] = []

    def plan(self, *, plan_request: PlanRequest, budget: object) -> PlanOutcome:
        self.calls.append((plan_request, budget))
        return stub_outcome()


class RequestValidationHttpTests(ApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.seam = SeamRecorder()
        patcher = mock.patch("fuelplan.views.build_trip_planner", return_value=self.seam)
        patcher.start()
        self.addCleanup(patcher.stop)
        # No routing call may happen on any 400; the fake proves it (calls=0).
        self.transport = FakeTransport()

    def _assert_rejected(self, response: object, *, keys: set[str]) -> dict:
        details = assert_envelope(self, response, status=400, code="invalid_request")
        self.assertEqual(set(details), keys)
        for messages in details.values():
            self.assertIsInstance(messages, list)
            self.assertTrue(all(isinstance(text, str) and text for text in messages))
        self.assertEqual(self.seam.calls, [])
        self.assertEqual(self.transport.count, 0)
        return details

    def test_input_validation_table(self) -> None:
        json_rows = {
            "city only": (_with_start({"city": "Chicago"}), {"start.state"}),
            "city + state + latitude": (
                _with_start({"city": "Chicago", "state": "IL", "latitude": 41.0}),
                {"start.latitude"},
            ),
            "extra key in start": (_with_start({**CHICAGO, "foo": 1}), {"start.foo"}),
            'latitude "abc"': (_with_start({**CHICAGO, "latitude": "abc"}), {"start.latitude"}),
            'latitude "41.85"': (_with_start({**CHICAGO, "latitude": "41.85"}), {"start.latitude"}),
            "latitude true": (_with_start({**CHICAGO, "latitude": True}), {"start.latitude"}),
            "latitude null": (_with_start({**CHICAGO, "latitude": None}), {"start.latitude"}),
            "latitude 91": (_with_start({**CHICAGO, "latitude": 91}), {"start.latitude"}),
            "latitude 90.0000001": (
                _with_start({**CHICAGO, "latitude": 90.0000001}),
                {"start.latitude"},
            ),
            "longitude -181": (_with_start({**CHICAGO, "longitude": -181}), {"start.longitude"}),
            "unknown top-level key": ({**BASE, "foo": 1}, {"foo"}),
            "city of 101 chars": (
                _with_start({"city": "a" * 101, "state": "KS"}),
                {"start.city"},
            ),
            "start is a list": ({"start": []}, {"start", "finish"}),
            "state is a number": (_with_start({"city": "Chicago", "state": 12}), {"start.state"}),
            "city is empty": (_with_start({"city": "", "state": "IL"}), {"start.city"}),
            "city is only spaces": (_with_start({"city": "   ", "state": "IL"}), {"start.city"}),
            "state XX": (_with_start({"city": "Chicago", "state": "XX"}), {"start.state"}),
        }
        for name, (payload, keys) in json_rows.items():
            with self.subTest(row=name):
                self._assert_rejected(self.post_json(payload), keys=keys)

        body = json.dumps(BASE).encode()
        raw_rows = {  # name: (body, extra WSGI headers, expected details keys)
            "NaN": (_raw_start("NaN"), {}, {"_body"}),
            "Infinity": (_raw_start("Infinity"), {}, {"_body"}),
            "-Infinity": (_raw_start("-Infinity"), {}, {"_body"}),
            "1e400 parses to inf": (_raw_start("1e400"), {}, {"start.latitude"}),
            "body of 10,241 bytes": (_padded(BASE, size=MAX_BODY_BYTES + 1), {}, {"_body"}),
            "no Content-Length": (body, {"CONTENT_LENGTH": ""}, {"_body"}),
            'Content-Length "abc"': (body, {"CONTENT_LENGTH": "abc"}, {"_body"}),
            'Content-Length "-1"': (body, {"CONTENT_LENGTH": "-1"}, {"_body"}),
            "empty body with Content-Length 0": (b"", {"CONTENT_LENGTH": "0"}, {"_body"}),
            "empty body without Content-Length": (b"", {}, {"_body"}),
            "body is a list": (b"[]", {}, {"_body"}),
            "body is a string": (b'"x"', {}, {"_body"}),
            "body is a number": (b"1", {}, {"_body"}),
            "body is null": (b"null", {}, {"_body"}),
            "invalid UTF-8": (b'{"start":"\xff"}', {}, {"_body"}),
            "5,000 nested lists": (b"[" * 5000 + b"]" * 5000, {}, {"_body"}),
            # Too deep for json: RecursionError, which is not a ValueError.
            "10,240 unclosed brackets": (b"[" * MAX_BODY_BYTES, {}, {"_body"}),
            # More digits than int() converts: must be refused, never a 500.
            "Content-Length of 5,000 nines": (body, {"CONTENT_LENGTH": "9" * 5000}, {"_body"}),
            "Content-Length 2 behind 4,400 zeros": (
                body,
                {"CONTENT_LENGTH": "0" * 4400 + "2"},
                {"_body"},
            ),
        }
        for name, (raw_body, extra, keys) in raw_rows.items():
            with self.subTest(row=name):
                self._assert_rejected(self.post_raw(raw_body, **extra), keys=keys)

    def test_parse_level_reasons_are_plain_sentences(self) -> None:
        cases = {
            b"{": "Body is not valid JSON.",
            b"[" * MAX_BODY_BYTES: "Body is not valid JSON.",
            _raw_start("NaN"): "NaN and Infinity are not allowed in JSON numbers.",
            b"\xff": "Body is not valid UTF-8.",
            b"[]": "The body must be a JSON object.",
            _padded(BASE, size=MAX_BODY_BYTES + 1): "The body is larger than 10,240 bytes.",
        }
        for body, reason in cases.items():
            with self.subTest(reason=reason):
                details = self._assert_rejected(self.post_raw(body), keys={"_body"})
                self.assertEqual(details["_body"], [reason])

    def test_field_messages_name_the_field_and_the_fix(self) -> None:
        cases = {
            "string latitude": (
                _with_start({**CHICAGO, "latitude": "41.85"}),
                "start.latitude",
                "Must be a JSON number, not a string.",
            ),
            "latitude out of range": (
                _with_start({**CHICAGO, "latitude": 91}),
                "start.latitude",
                "Must be between -90 and 90.",
            ),
            "mixed forms": (
                _with_start({"city": "Chicago", "state": "IL", "latitude": 41.0}),
                "start.latitude",
                "Send either city + state or latitude + longitude, not both.",
            ),
            "unknown key": (
                {**BASE, "foo": 1},
                "foo",
                "Unknown field. Allowed: start, finish, current_fuel_gallons, reserve_gallons.",
            ),
            "bad state code": (
                _with_start({"city": "Chicago", "state": "XX"}),
                "start.state",
                "XX is not a US state code; use a 2-letter USPS code such as IL.",
            ),
            "long city": (
                _with_start({"city": "a" * 101, "state": "KS"}),
                "start.city",
                "At most 100 characters.",
            ),
        }
        for name, (payload, key, message) in cases.items():
            with self.subTest(case=name):
                details = self._assert_rejected(self.post_json(payload), keys={key})
                self.assertEqual(details[key], [message])

    def test_boundary_values_are_accepted_and_reach_the_planner(self) -> None:
        city_100 = {"city": "a" * 100, "state": "KS"}
        rows = {
            "city of 100 chars": (json.dumps(_with_start(city_100)).encode(), "application/json"),
            "body of exactly 10,240 bytes": (
                _padded(_with_start({"city": "Nowhereville", "state": "KS"}), size=MAX_BODY_BYTES),
                "application/json",
            ),
            "latitude 90": (json.dumps(_with_start({**CHICAGO, "latitude": 90})).encode(), None),
            "latitude -90": (json.dumps(_with_start({**CHICAGO, "latitude": -90})).encode(), None),
            "longitude -180": (
                json.dumps(_with_start({**CHICAGO, "longitude": -180})).encode(),
                None,
            ),
            "integer latitude": (
                json.dumps(_with_start({**CHICAGO, "latitude": 41})).encode(),
                None,
            ),
            "charset parameter": (json.dumps(BASE).encode(), "application/json; charset=utf-8"),
            "AK passes to the resolver": (
                json.dumps(_with_start({"city": "Anchorage", "state": "ak"})).encode(),
                None,
            ),
        }
        for name, (body, content_type) in rows.items():
            with self.subTest(row=name):
                calls_before = len(self.seam.calls)

                response = self.post_raw(body, content_type=content_type or "application/json")

                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(len(self.seam.calls), calls_before + 1)
        self.assertEqual(self.transport.count, 0)

    def test_the_view_hands_the_planner_a_fresh_budget_of_three_calls(self) -> None:
        self.post_json(BASE)

        [(plan_request, budget)] = self.seam.calls
        self.assertEqual(CALL_LIMIT, 3)
        self.assertEqual((budget.used, budget.remaining()), (0, 3))
        self.assertEqual(plan_request.start, PointInput(latitude=41.85003, longitude=-87.65005))
        self.assertEqual(plan_request.finish, PointInput(latitude=39.73915, longitude=-104.9847))

    def test_rejected_fuel_values_are_400_with_the_field_named(self) -> None:
        rows = {
            "12.55": ({"current_fuel_gallons": 12.55}, "current_fuel_gallons"),
            '"12.55"': ({"current_fuel_gallons": "12.55"}, "current_fuel_gallons"),
            "-0.1": ({"current_fuel_gallons": -0.1}, "current_fuel_gallons"),
            "50.1": ({"current_fuel_gallons": 50.1}, "current_fuel_gallons"),
            "true": ({"current_fuel_gallons": True}, "current_fuel_gallons"),
            "null": ({"current_fuel_gallons": None}, "current_fuel_gallons"),
            "[]": ({"current_fuel_gallons": []}, "current_fuel_gallons"),
            "{}": ({"current_fuel_gallons": {}}, "current_fuel_gallons"),
            "reserve 49": ({"reserve_gallons": 49}, "reserve_gallons"),
            "current < reserve": (
                {"current_fuel_gallons": 10, "reserve_gallons": 12},
                "reserve_gallons",
            ),
        }
        for name, (fuel, key) in rows.items():
            with self.subTest(row=name):
                self._assert_rejected(self.post_json({**BASE, **fuel}), keys={key})


class FuelInputTests(OfflineTestCase):
    """TC-5 at serializer level: accepted forms, their Decimal value and the 1-dp echo."""

    def _fuel(self, **fuel: object) -> PlanRequest:
        return parse_plan_request({**BASE, **fuel})

    def _rejection(self, **fuel: object) -> dict[str, list[str]]:
        with self.assertRaises(ValidationError) as raised:
            self._fuel(**fuel)
        return flatten_errors(raised.exception.detail)

    def test_fuel_inputs_table(self) -> None:
        accepted = {
            "omitted": ({}, ("50.0", "5.0")),
            "12.5": ({"current_fuel_gallons": 12.5}, ("12.5", "5.0")),
            '"12.5"': ({"current_fuel_gallons": "12.5"}, ("12.5", "5.0")),
            "12.50": ({"current_fuel_gallons": 12.50}, ("12.5", "5.0")),
            '"12.50"': ({"current_fuel_gallons": "12.50"}, ("12.5", "5.0")),
            "12.3 (not a 50-digit float)": ({"current_fuel_gallons": 12.3}, ("12.3", "5.0")),
            "0.1 reserve": ({"reserve_gallons": 0.1}, ("50.0", "0.1")),
            "48.7 current": ({"current_fuel_gallons": 48.7}, ("48.7", "5.0")),
            "current 50 + reserve 48": (
                {"current_fuel_gallons": 50, "reserve_gallons": 48},
                ("50.0", "48.0"),
            ),
            "current = reserve": (
                {"current_fuel_gallons": 5, "reserve_gallons": 5},
                ("5.0", "5.0"),
            ),
            "both 0": ({"current_fuel_gallons": 0, "reserve_gallons": 0}, ("0.0", "0.0")),
            "integer -0": ({"current_fuel_gallons": 0, "reserve_gallons": -0}, ("0.0", "0.0")),
            "float -0.0": ({"current_fuel_gallons": 0, "reserve_gallons": -0.0}, ("0.0", "0.0")),
        }
        for name, (fuel, (current, reserve)) in accepted.items():
            with self.subTest(accepted=name):
                plan_request = self._fuel(**fuel)

                self.assertIsInstance(plan_request.current_fuel_gallons, Decimal)
                self.assertEqual(str(plan_request.current_fuel_gallons), current)
                self.assertEqual(str(plan_request.reserve_gallons), reserve)

        rejected = {
            "12.55": {"current_fuel_gallons": 12.55},
            '"12.55"': {"current_fuel_gallons": "12.55"},
            "-0.1": {"current_fuel_gallons": -0.1},
            "50.1": {"current_fuel_gallons": 50.1},
            "reserve 48.1": {"reserve_gallons": 48.1},
            "reserve 49": {"reserve_gallons": 49},
            "true": {"current_fuel_gallons": True},
            "false": {"reserve_gallons": False},
            "null": {"current_fuel_gallons": None},
            "[]": {"current_fuel_gallons": []},
            "{}": {"current_fuel_gallons": {}},
            '"NaN"': {"current_fuel_gallons": "NaN"},
            '"sNaN"': {"current_fuel_gallons": "sNaN"},
            '"Infinity"': {"current_fuel_gallons": "Infinity"},
            '"-Infinity"': {"current_fuel_gallons": "-Infinity"},
            '"1e2"': {"current_fuel_gallons": "1e2"},
            '""': {"current_fuel_gallons": ""},
            '"12,5"': {"current_fuel_gallons": "12,5"},
            "10^400": {"current_fuel_gallons": 10**400},
            "1e400 float": {"current_fuel_gallons": float("inf")},
        }
        for name, fuel in rejected.items():
            with self.subTest(rejected=name):
                self.assertEqual(set(self._rejection(**fuel)), set(fuel))

    def test_current_below_reserve_shows_both_values_under_reserve_gallons(self) -> None:
        errors = self._rejection(current_fuel_gallons=10, reserve_gallons=12)

        self.assertEqual(
            errors,
            {
                "reserve_gallons": [
                    "reserve_gallons (12.0) must not be more than current_fuel_gallons (10.0)."
                ]
            },
        )

    def test_fuel_messages_name_the_fix(self) -> None:
        cases = {
            "12.55": ({"current_fuel_gallons": 12.55}, "At most 1 decimal place (e.g. 12.5)."),
            "50.1": ({"current_fuel_gallons": 50.1}, "Must be between 0 and 50."),
            "reserve 49": ({"reserve_gallons": 49}, "Must be between 0 and 48."),
        }
        for name, (fuel, message) in cases.items():
            with self.subTest(case=name):
                [(key, messages)] = self._rejection(**fuel).items()
                self.assertEqual(messages, [message])

    def test_city_input_becomes_a_city_value_with_trimmed_text(self) -> None:
        plan_request = parse_plan_request({**BASE, "start": {"city": "  Chicago ", "state": " il"}})

        self.assertEqual(plan_request.start, CityInput(city="Chicago", state="il"))

    def test_a_body_that_is_not_an_object_is_refused_before_the_schema(self) -> None:
        for body in ([], "x", 1, None):
            with self.subTest(body=body), self.assertRaises(InvalidRequest) as raised:
                parse_plan_request(body)
            self.assertEqual(
                raised.exception.details, {"_body": ["The body must be a JSON object."]}
            )


class RequestValidationMessageTests(ApiTestCase):
    def test_a_missing_content_length_says_how_to_fix_it(self) -> None:
        response = self.post_raw(json.dumps(BASE).encode(), CONTENT_LENGTH="")

        details = assert_envelope(self, response, status=400, code="invalid_request")
        self.assertEqual(
            details["_body"],
            ["The body is empty or has no Content-Length header; send a JSON object."],
        )

    def test_a_huge_content_length_is_reported_as_too_large(self) -> None:
        response = self.post_raw(json.dumps(BASE).encode(), CONTENT_LENGTH="9" * 5000)

        details = assert_envelope(self, response, status=400, code="invalid_request")
        self.assertEqual(details["_body"], ["The body is larger than 10,240 bytes."])

    def test_the_message_names_at_most_three_fields(self) -> None:
        payload = {"start": {"latitude": "a", "longitude": "b"}, "finish": [], "foo": 1, "bar": 2}

        response = self.post_json(payload)

        assert_envelope(self, response, status=400, code="invalid_request")
        self.assertEqual(
            json.loads(response.content)["error"]["message"],
            "Invalid request in start.latitude, start.longitude, finish and 2 more; see details.",
        )
