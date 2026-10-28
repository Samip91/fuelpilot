"""Domain errors, the JSON error envelope and the handlers that use it (spec 01 §7a, FR-34/35).

Every error that leaves the API is `{"error": {"code", "message", "details"}}`. Messages
are one plain sentence that names the problem and the fix; they never carry exception
text, file paths, URLs, keys or provider text, because those stay in the server log.
"""

import logging
from collections.abc import Mapping
from typing import Literal

from django.core.exceptions import DisallowedHost
from django.http import Http404, HttpRequest, JsonResponse
from rest_framework import exceptions as drf_exceptions
from rest_framework.settings import api_settings

logger = logging.getLogger(__name__)

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]

SERVICE_AREA_SCOPE = "contiguous US + DC"
BODY_KEY = "_body"  # details key for problems with the body as a whole (AC-1)
DEFAULT_THROTTLE_WAIT_S = 60  # DRF may report no wait at all (`Throttled.wait` is None)

# The §7a catalogue, exactly: code -> HTTP status (FR-35).
ERROR_CATALOGUE: Mapping[str, int] = {
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
}
ERROR_CODES: frozenset[str] = frozenset(ERROR_CATALOGUE)

NOT_FOUND_MESSAGE = "No such endpoint. The API is POST /api/v1/fuel-plans."
INTERNAL_ERROR_MESSAGE = "Internal error; no plan was saved. Try again later."


class FuelPlanError(Exception):
    """Base class: each subclass fixes its §7a `code` and HTTP `status`."""

    code: str = "internal_error"
    status: int = 500

    def __init__(
        self,
        message: str,
        *,
        details: dict[str, JsonValue] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, JsonValue] = details or {}
        self.headers: dict[str, str] = headers or {}


class InvalidRequest(FuelPlanError):
    """The request failed a 400 check; `details` maps dotted field paths to messages (FR-1)."""

    code = "invalid_request"
    status = 400

    def __init__(self, details: dict[str, list[str]]) -> None:
        super().__init__(
            f"Invalid request in {_field_list(list(details))}; see details.", details=dict(details)
        )


MAX_FIELDS_IN_MESSAGE = 3


def _field_list(keys: list[str]) -> str:
    """At most 3 field names for the one-line message, then "and N more"."""
    names = ["the request body" if key == BODY_KEY else key for key in keys]
    shown = ", ".join(names[:MAX_FIELDS_IN_MESSAGE])
    hidden = len(names) - MAX_FIELDS_IN_MESSAGE
    return f"{shown} and {hidden} more" if hidden > 0 else shown


class LocationNotFound(FuelPlanError):
    """A valid lower-48/DC state, but no populated place with that name (FR-3)."""

    code = "location_not_found"
    status = 422

    def __init__(self, *, field: str, city: str, state: str, state_code: str) -> None:
        """`state` is echoed as sent in details; `state_code` is its normalised form."""
        super().__init__(
            f"No populated place with that name was found in {state_code} for {field}. "
            "Send coordinates (latitude and longitude) instead.",
            details={"field": field, "city": city, "state": state},
        )


class UnsupportedLocation(FuelPlanError):
    """A state, province or point outside the service area (FR-4, rule 4)."""

    code = "unsupported_location"
    status = 422

    def __init__(self, *, field: str) -> None:
        super().__init__(
            f"The {field} location is outside the service area: {SERVICE_AREA_SCOPE} only.",
            details={"field": field, "scope": SERVICE_AREA_SCOPE},
        )


NO_ROUTE_MESSAGES: Mapping[str, str] = {
    "NoRoute": "No drivable road route between start and finish.",
    "NoSegment": "One of the points could not be matched to a road; move it closer to a road.",
}


class NoRoute(FuelPlanError):
    """OSRM said explicitly that there is no route (`NoRoute` / `NoSegment`), never an outage."""

    code = "no_route"
    status = 422

    def __init__(self, *, provider_code: str, routing_calls: int) -> None:
        super().__init__(
            NO_ROUTE_MESSAGES[provider_code],
            details={"provider_code": provider_code, "routing_calls": routing_calls},
        )


class RoutingUnavailable(FuelPlanError):
    """The routing service failed or answered something we cannot use (502)."""

    code = "routing_unavailable"
    status = 502

    def __init__(self, *, routing_calls: int) -> None:
        super().__init__(
            "The routing service is unavailable right now; try again shortly.",
            details={"routing_calls": routing_calls, "retryable": True},
        )


RateLimitSource = Literal["provider", "slot"]


class RoutingRateLimited(FuelPlanError):
    """OSRM sent 429 (`provider`), or our own 2 s wait for a call slot ran out (`slot`)."""

    code = "routing_rate_limited"
    status = 503

    def __init__(self, *, routing_calls: int, retry_after_s: int, source: RateLimitSource) -> None:
        if source == "provider":
            message = (
                f"The routing service is rate-limiting requests; retry in {retry_after_s} seconds."
            )
        else:
            message = f"Route requests are queued; retry in {retry_after_s} seconds."
        super().__init__(
            message,
            details={
                "routing_calls": routing_calls,
                "retry_after_seconds": retry_after_s,
                "retryable": True,
            },
            headers={"Retry-After": str(retry_after_s)},
        )


class RoutingTimeout(FuelPlanError):
    """Connecting to OSRM, or reading its whole reply, took longer than allowed (504)."""

    code = "routing_timeout"
    status = 504

    def __init__(self, *, routing_calls: int, timeout_s: float) -> None:
        super().__init__(
            f"The routing service did not answer within {timeout_s:g} seconds; try again.",
            details={"routing_calls": routing_calls, "retryable": True},
        )


class InternalError(FuelPlanError):
    """A bug on our side; `routing_calls` is shown only when an OSRM reply revealed it."""

    code = "internal_error"
    status = 500

    def __init__(
        self, *, routing_calls: int | None = None, message: str = INTERNAL_ERROR_MESSAGE
    ) -> None:
        details: dict[str, JsonValue] = {}
        if routing_calls is not None:
            details["routing_calls"] = routing_calls
        super().__init__(message, details=details)


class CallBudgetExceeded(InternalError):
    """A fourth routing call was attempted (FR-10); its own class only for a specific log line."""


STATION_DATA_MISSING_MESSAGE = (
    "Station data not imported. Run `python manage.py import_fuel_stations`; "
    "the next request loads it."
)


class StationDataMissing(InternalError):
    """No Station rows or no DatasetImport record (FR-43): the operator must run the import.

    Still a 500 `internal_error` with `{}` details, but the message names the fix instead
    of "try again later", which would never help.
    """

    def __init__(self) -> None:
        super().__init__(message=STATION_DATA_MISSING_MESSAGE)


class NoFeasiblePlan(FuelPlanError):
    """No plan keeps every leg within range among the candidates (FR-23, I7); never partial.

    The serializer builds the message and the rounded detail strings; this class only
    fixes the §7a code, status and detail keys.
    """

    code = "no_feasible_plan"
    status = 422

    def __init__(
        self,
        *,
        message: str,
        gap_from_mile: str,
        gap_to_mile: str,
        gap_leg_miles: str,
        max_leg_miles: str,
        hint: str,
    ) -> None:
        super().__init__(
            message,
            details={
                "gap_from_mile": gap_from_mile,
                "gap_to_mile": gap_to_mile,
                "gap_leg_miles": gap_leg_miles,
                "max_leg_miles": max_leg_miles,
                "hint": hint,
            },
        )


def error_response(
    *,
    code: str,
    message: str,
    details: Mapping[str, JsonValue],
    status: int,
    headers: Mapping[str, str] | None = None,
) -> JsonResponse:
    """The one place the envelope is built (A-11); refuses codes outside the §7a catalogue."""
    if ERROR_CATALOGUE.get(code) != status:
        raise ValueError(f"{code!r} with HTTP {status} is not in the §7a catalogue")
    return JsonResponse(
        {"error": {"code": code, "message": message, "details": dict(details)}},
        status=status,
        headers=dict(headers or {}),
    )


def response_for(error: FuelPlanError) -> JsonResponse:
    """The envelope for a domain error."""
    return error_response(
        code=error.code,
        message=error.message,
        details=error.details,
        status=error.status,
        headers=error.headers,
    )


def not_found_response() -> JsonResponse:
    """404 for any unknown path, naming the one endpoint that exists."""
    return error_response(code="not_found", message=NOT_FOUND_MESSAGE, details={}, status=404)


def internal_error_response() -> JsonResponse:
    """500 with nothing about the cause: the traceback goes to the log only."""
    return response_for(InternalError())


def flatten_errors(detail: object, path: str = "") -> dict[str, list[str]]:
    """DRF's nested error detail as {dotted path: [messages]} (FR-1).

    A nested `non_field_errors` belongs to its parent path ("start"); at the top level
    it becomes `_body`, the key for problems with the body as a whole.
    """
    flat: dict[str, list[str]] = {}
    if isinstance(detail, Mapping):
        for key, value in detail.items():
            if key == api_settings.NON_FIELD_ERRORS_KEY:
                child_path = path or BODY_KEY
            else:
                child_path = f"{path}.{key}" if path else str(key)
            _merge(flat, flatten_errors(value, child_path))
    elif isinstance(detail, list):
        for item in detail:
            _merge(flat, flatten_errors(item, path))
    else:
        flat[path or BODY_KEY] = [str(detail)]
    return flat


def _merge(into: dict[str, list[str]], extra: Mapping[str, list[str]]) -> None:
    for key, messages in extra.items():
        into.setdefault(key, []).extend(messages)


def api_exception_handler(exc: Exception, context: Mapping[str, object]) -> JsonResponse:
    """DRF `EXCEPTION_HANDLER`: every exception inside an API view becomes the envelope.

    It never returns None, so DRF never re-raises: the API answers in JSON even with
    DEBUG on, and an unexpected exception is logged here, never shown.
    """
    if isinstance(exc, FuelPlanError):
        return response_for(exc)
    if isinstance(exc, drf_exceptions.ValidationError):
        return response_for(InvalidRequest(flatten_errors(exc.detail)))
    if isinstance(exc, drf_exceptions.ParseError):
        return response_for(InvalidRequest({BODY_KEY: [str(exc.detail)]}))
    if isinstance(exc, drf_exceptions.UnsupportedMediaType):
        return error_response(
            code="unsupported_media_type",
            message="Set the header Content-Type: application/json.",
            details={},
            status=415,
        )
    if isinstance(exc, drf_exceptions.MethodNotAllowed):
        return _method_not_allowed(context)
    if isinstance(exc, drf_exceptions.Throttled):
        return _too_many_requests(exc.wait or DEFAULT_THROTTLE_WAIT_S)
    if isinstance(exc, drf_exceptions.NotFound | Http404):
        return not_found_response()
    logger.exception("Unhandled error in an API view; answered 500 internal_error")
    return internal_error_response()


def _method_not_allowed(context: Mapping[str, object]) -> JsonResponse:
    """405 whose `Allow` header and `details.allowed` come from the same list."""
    allowed = list(getattr(context.get("view"), "allowed_methods", []))
    return error_response(
        code="method_not_allowed",
        message=f"Use {' or '.join(allowed)} for this endpoint.",
        details={"allowed": allowed},
        status=405,
        headers={"Allow": ", ".join(allowed)},
    )


def _too_many_requests(wait_s: int) -> JsonResponse:
    """429 from our own POST throttle; the header and the details carry the same integer."""
    return error_response(
        code="too_many_requests",
        message=f"Too many requests; retry in {wait_s} seconds.",
        details={"retry_after_seconds": wait_s, "retryable": True},
        status=429,
        headers={"Retry-After": str(wait_s)},
    )


def bad_request(request: HttpRequest, exception: Exception) -> JsonResponse:
    """Django `handler400`, e.g. a disallowed Host; never echoes the exception or the Host."""
    if isinstance(exception, DisallowedHost):
        reason = "The Host header is not allowed."
    else:
        reason = "Bad request."
    # Built directly: InvalidRequest's message would blame "the request body".
    return error_response(
        code="invalid_request", message=reason, details={BODY_KEY: [reason]}, status=400
    )


def page_not_found(request: HttpRequest, exception: Exception) -> JsonResponse:
    """Django `handler404` for paths outside `/api/v1/` (used when DEBUG is off)."""
    return not_found_response()


def server_error(request: HttpRequest) -> JsonResponse:
    """Django `handler500`: the generic 500 envelope, never a traceback."""
    return internal_error_response()
