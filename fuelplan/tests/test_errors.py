"""Spec 01 TC-29 (FR-34, FR-35, AC-26), the A-11 `error_response` contract and open items.

Tests run with DEBUG off (Django's test default) unless they say otherwise; the owner
decision of 2026-10-28 also requires JSON for every `/api/v1/` path with DEBUG on.
"""

import json
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import SuspiciousOperation
from django.http import JsonResponse
from django.test import RequestFactory, override_settings

from fuelplan.errors import (
    ERROR_CATALOGUE,
    ERROR_CODES,
    NOT_FOUND_MESSAGE,
    bad_request,
    error_response,
    flatten_errors,
    server_error,
)
from fuelplan.geo import RouteGeometryError
from fuelplan.locations import LocationDataError

from .base import PLAN_URL, ApiTestCase, OfflineTestCase, assert_envelope

VALID_BODY = json.dumps(
    {
        "start": {"latitude": 41.85003, "longitude": -87.65005},
        "finish": {"latitude": 39.73915, "longitude": -104.9847},
    }
)
SECRET_PATH = "/var/www/fuelpilot/data/us_places.tsv line 3: expected 6 columns"
THROTTLE_LIMIT = 30


class ErrorEnvelopeHttpTests(ApiTestCase):
    def test_error_envelope(self) -> None:
        for method in ("get", "put", "patch", "delete", "options"):
            with self.subTest(method=method):
                cache.clear()
                response = getattr(self.client, method)(PLAN_URL)

                details = assert_envelope(self, response, status=405, code="method_not_allowed")
                self.assertEqual(details, {"allowed": ["POST"]})
                self.assertEqual(response["Allow"], "POST")

        cache.clear()
        head = self.client.head(PLAN_URL)
        self.assertEqual((head.status_code, head["Allow"]), (405, "POST"))

        for content_type in ("text/plain", ""):
            with self.subTest(content_type=content_type):
                response = self.post_raw(VALID_BODY.encode(), content_type=content_type)
                details = assert_envelope(self, response, status=415, code="unsupported_media_type")
                self.assertEqual(details, {})

        details = assert_envelope(self, self.post_raw(b"{"), status=400, code="invalid_request")
        self.assertEqual(details, {"_body": ["Body is not valid JSON."]})

    def test_unknown_paths_get_the_json_404_naming_the_endpoint(self) -> None:
        requests = {
            "GET /api/v1/nope": lambda: self.client.get("/api/v1/nope", HTTP_ACCEPT="text/html"),
            "POST /api/v1/nope": lambda: self.client.post("/api/v1/nope", {}),
            "POST with a trailing slash": lambda: self.client.post(
                f"{PLAN_URL}/", VALID_BODY, content_type="application/json"
            ),
            "GET /nope": lambda: self.client.get("/nope", HTTP_ACCEPT="text/html"),
            "GET /api/v1": lambda: self.client.get("/api/v1"),
            "POST /api/v1": lambda: self.client.post(
                "/api/v1", VALID_BODY, content_type="application/json"
            ),
        }
        for name, send in requests.items():
            with self.subTest(request=name):
                response = send()

                details = assert_envelope(self, response, status=404, code="not_found")
                self.assertEqual(details, {})
                self.assertEqual(
                    json.loads(response.content)["error"]["message"], NOT_FOUND_MESSAGE
                )

    @override_settings(DEBUG=True)
    def test_api_paths_stay_json_with_debug_on(self) -> None:
        # With DEBUG on, Django logs every 4xx/5xx to the console; capture those lines.
        for path in ("/api/v1/nope", f"{PLAN_URL}/", "/api/v1/a%0Ab", "/api/v1"):
            with self.subTest(path=path), self.assertLogs("django.request", "WARNING"):
                response = self.client.get(path, HTTP_ACCEPT="text/html")

            assert_envelope(self, response, status=404, code="not_found")

        with self.assertLogs("django.request", "WARNING"):
            response = self.client.post("/api/v1", VALID_BODY, content_type="application/json")
        assert_envelope(self, response, status=404, code="not_found")

        with (
            mock.patch("fuelplan.views.plan_trip", side_effect=RuntimeError("secret /var/x")),
            self.assertLogs("fuelplan.errors", "ERROR"),
            self.assertLogs("django.request", "ERROR"),
        ):
            response = self.post_raw(VALID_BODY.encode())
        self.assertEqual(assert_envelope(self, response, status=500, code="internal_error"), {})
        self.assertNotIn(b"secret", response.content)

    def test_a_crash_in_the_view_is_a_logged_500_without_a_traceback(self) -> None:
        with (
            mock.patch("fuelplan.views.plan_trip", side_effect=RuntimeError("secret /var/x")),
            self.assertLogs("fuelplan.errors", "ERROR") as logs,
        ):
            response = self.post_raw(VALID_BODY.encode())

        self.assertEqual(assert_envelope(self, response, status=500, code="internal_error"), {})
        for leaked in (b"Traceback", b"RuntimeError", b"secret", b"/var/"):
            self.assertNotIn(leaked, response.content)
        self.assertIn("RuntimeError", "\n".join(logs.output))

    def test_until_2c_a_valid_request_ends_at_the_planner_seam_as_500(self) -> None:
        with self.assertLogs("fuelplan.errors", "ERROR") as logs:
            response = self.post_raw(VALID_BODY.encode())

        assert_envelope(self, response, status=500, code="internal_error")
        self.assertIn("TODO(2C)", "\n".join(logs.output))

    def test_location_data_error_becomes_internal_error_without_detail(self) -> None:
        for error in (LocationDataError(SECRET_PATH), RouteGeometryError("need 2 points, got 1")):
            with self.subTest(error=type(error).__name__):
                with (
                    mock.patch("fuelplan.views.plan_trip", side_effect=error),
                    self.assertLogs("fuelplan.errors", "ERROR") as logs,
                ):
                    response = self.post_raw(VALID_BODY.encode())

                details = assert_envelope(self, response, status=500, code="internal_error")
                self.assertEqual(details, {})
                for leaked in (b"us_places", b"/var/", b"points"):
                    self.assertNotIn(leaked, response.content)
                self.assertIn(type(error).__name__, "\n".join(logs.output))

    def test_a_disallowed_host_gets_the_json_400(self) -> None:
        with self.assertLogs("django.security.DisallowedHost", "ERROR"):
            response = self.client.post(
                PLAN_URL, VALID_BODY, content_type="application/json", HTTP_HOST="evil.example"
            )

        details = assert_envelope(self, response, status=400, code="invalid_request")
        self.assertEqual(
            details,
            {"_body": ["The Host header is not allowed."]},
        )
        self.assertEqual(
            json.loads(response.content)["error"]["message"], "The Host header is not allowed."
        )
        for leaked in (b"evil.example", b"FUELPILOT_ALLOWED_HOSTS", b"request body"):
            self.assertNotIn(leaked, response.content)

    def test_accept_html_still_gets_json_never_406(self) -> None:
        response = self.post_raw(b"{", HTTP_ACCEPT="text/html")

        assert_envelope(self, response, status=400, code="invalid_request")

    def test_the_31st_post_in_a_minute_gets_429_with_retry_after(self) -> None:
        for spoof_forwarded_for in (False, True):
            with self.subTest(spoof_forwarded_for=spoof_forwarded_for):
                cache.clear()
                responses = []
                for number in range(THROTTLE_LIMIT + 1):
                    # NUM_PROXIES = 0: a new X-Forwarded-For per request must not reset the count.
                    extra = (
                        {"HTTP_X_FORWARDED_FOR": f"10.0.0.{number}"} if spoof_forwarded_for else {}
                    )
                    responses.append(
                        self.client.generic(
                            "POST", PLAN_URL, b"{", content_type="application/json", **extra
                        )
                    )

                self.assertEqual({r.status_code for r in responses[:THROTTLE_LIMIT]}, {400})
                last = responses[-1]
                details = assert_envelope(self, last, status=429, code="too_many_requests")
                self.assertEqual(set(details), {"retry_after_seconds", "retryable"})
                self.assertIsInstance(details["retry_after_seconds"], int)
                self.assertIs(details["retryable"], True)
                self.assertEqual(last["Retry-After"], str(details["retry_after_seconds"]))


class ErrorCatalogueTests(OfflineTestCase):
    def test_error_code_set_equals_catalogue(self) -> None:
        self.assertEqual(
            dict(ERROR_CATALOGUE),
            {
                "invalid_request": 400,
                "not_found": 404,
                "plan_not_found": 404,
                "method_not_allowed": 405,
                "unsupported_media_type": 415,
                "location_not_found": 422,
                "unsupported_location": 422,
                "no_route": 422,
                "no_feasible_plan": 422,
                "too_many_requests": 429,
                "internal_error": 500,
                "routing_unavailable": 502,
                "routing_rate_limited": 503,
                "routing_timeout": 504,
            },
        )
        self.assertEqual(ERROR_CODES, frozenset(ERROR_CATALOGUE))

    def test_error_response_builds_json_response(self) -> None:
        response = error_response(
            code="too_many_requests",
            message="Too many requests; retry in 7 seconds.",
            details={"retry_after_seconds": 7, "retryable": True},
            status=429,
            headers={"Retry-After": "7"},
        )

        self.assertIsInstance(response, JsonResponse)
        self.assertEqual((response.status_code, response["Retry-After"]), (429, "7"))
        self.assertEqual(
            json.loads(response.content),
            {
                "error": {
                    "code": "too_many_requests",
                    "message": "Too many requests; retry in 7 seconds.",
                    "details": {"retry_after_seconds": 7, "retryable": True},
                }
            },
        )
        plain = error_response(code="not_found", message="x", details={}, status=404, headers=None)
        self.assertEqual(plain.status_code, 404)

    def test_error_response_refuses_codes_outside_the_catalogue(self) -> None:
        for code, status in (("teapot", 418), ("not_found", 400)):
            with self.subTest(code=code, status=status), self.assertRaises(ValueError):
                error_response(code=code, message="x", details={}, status=status)

    def test_nested_errors_flatten_to_dotted_paths(self) -> None:
        detail = {
            "start": {"latitude": ["bad"], "non_field_errors": ["pick one form"]},
            "non_field_errors": ["whole body"],
            "foo": ["unknown"],
        }

        self.assertEqual(
            flatten_errors(detail),
            {
                "start.latitude": ["bad"],
                "start": ["pick one form"],
                "_body": ["whole body"],
                "foo": ["unknown"],
            },
        )

    def test_django_handlers_never_echo_the_exception(self) -> None:
        request = RequestFactory().get("/")

        response = bad_request(request, SuspiciousOperation("secret header value"))

        details = assert_envelope(self, response, status=400, code="invalid_request")
        self.assertEqual(details, {"_body": ["Bad request."]})
        self.assertEqual(
            assert_envelope(self, server_error(request), status=500, code="internal_error"), {}
        )
