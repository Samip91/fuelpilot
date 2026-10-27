"""The OSRM client, the per-request call budget and the cross-worker call slot (spec 01 §8).

One `fetch_route` makes at most one HTTP call and never retries (FR-9, FR-10). Its
order is fixed by §8: check the budget → wait for the provider slot → spend → send.
So a blocked fourth call never waits or opens a socket, and a slot timeout spends
nothing. Every OSRM reply is mapped by HTTP status first, then by its body `code`,
exactly per the §7a catalogue (FR-11); an outage is never reported as "no route".
"""

import json
import logging
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from django.conf import settings
from django.db import OperationalError
from django.db.models import Q

from .errors import (
    NO_ROUTE_MESSAGES,
    CallBudgetExceeded,
    InternalError,
    NoRoute,
    RoutingRateLimited,
    RoutingTimeout,
    RoutingUnavailable,
)
from .geo import LonLat, miles_from_meters
from .http import (
    HttpClientTransport,
    HttpResponse,
    Transport,
    TransportError,
    TransportTimeout,
    require_http_base_url,
)
from .models import ProviderSlot
from .parsing import whole_number

logger = logging.getLogger(__name__)

CALL_LIMIT = 3  # rule 2: at most 3 routing calls per request
MAX_ROUTE_BYTES = 8 * 1024 * 1024  # NY→LA is ~0.8 MB; anything above 8 MiB is a 502
USER_AGENT = "FuelPilot/1.0"

SLOT_NAME = "osrm"
SLOT_SPACING_S = 1.0  # NFR-3: at least 1 s between calls, across workers
SLOT_MAX_WAIT_S = 2.0  # then 503 with 0 calls
SLOT_HEAL_S = 31.0  # a slot this far ahead can only come from a clock step-back
SLOT_LOCKED_BACKOFF_S = 0.25  # SQLite "database is locked": try again shortly
SLOT_MIN_POLL_S = 0.05  # never spin on a slot that looks free but could not be claimed

DEFAULT_RETRY_AFTER_S = 2
RETRY_AFTER_MIN_S = 1
RETRY_AFTER_MAX_S = 60
MIN_GEOMETRY_CHECK_MILES = Decimal("0.1")  # shown at 0.1 mi; see `_check_route_has_length`
LOGGED_TEXT_MAX_CHARS = 200

INTERNAL_ERROR_CODES = frozenset({"TooBig"})  # plus every code starting with "Invalid"


class Clock(Protocol):
    """Wall time shared by all workers, a monotonic timer, and sleep; tests use a fake."""

    def now(self) -> float: ...

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock."""

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class CallBudget:
    """Routing calls one request may still make; the view creates one per request."""

    def __init__(self, limit: int = CALL_LIMIT) -> None:
        self._limit = limit
        self._used = 0

    @property
    def used(self) -> int:
        """Calls spent so far; this is `meta.routing_calls` / `details.routing_calls`."""
        return self._used

    def remaining(self) -> int:
        return self._limit - self._used

    def ensure_available(self) -> None:
        """Raise before any slot wait or socket if no call is left (FR-10 → 500)."""
        if self.remaining() <= 0:
            logger.error("Routing call blocked: the budget of %s calls is used up", self._limit)
            raise CallBudgetExceeded()

    def spend(self) -> None:
        """Count one call; called right before the request is sent."""
        self.ensure_available()
        self._used += 1


class ProviderThrottle:
    """Claims the shared `ProviderSlot` row so calls are ≥ 1 s apart across workers (§8 #3).

    One conditional UPDATE claims the slot: it succeeds when the slot is free, or when
    it is implausibly far ahead (a clock step-back). A waiter whose `now` is a little
    stale cannot pass, because the row already holds a later time. The UPDATE runs in
    autocommit (`ATOMIC_REQUESTS` is off), so the claim is visible to others at once.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        name: str = SLOT_NAME,
        spacing_s: float = SLOT_SPACING_S,
        max_wait_s: float = SLOT_MAX_WAIT_S,
        heal_s: float = SLOT_HEAL_S,
    ) -> None:
        self._clock = clock
        self._name = name
        self._spacing_s = spacing_s
        self._max_wait_s = max_wait_s
        self._heal_s = heal_s

    def try_acquire(self) -> bool:
        """True once the slot is ours; False if it cannot be ours within `max_wait_s`."""
        deadline = self._clock.monotonic() + self._max_wait_s
        while True:
            now = self._clock.now()
            claimed = self._claim(now=now)
            if claimed:
                return True
            wait_s = SLOT_LOCKED_BACKOFF_S if claimed is None else self._wait_for_slot(now=now)
            if self._clock.monotonic() + wait_s > deadline:
                return False  # fail fast: waiting would pass the deadline anyway
            self._clock.sleep(wait_s)

    def _claim(self, *, now: float) -> bool | None:
        """One UPDATE: True = claimed, False = taken, None = the database was locked."""
        is_free = Q(next_allowed_at__lte=now) | Q(next_allowed_at__gt=now + self._heal_s)
        try:
            updated = (
                ProviderSlot.objects.filter(pk=self._name)
                .filter(is_free)
                .update(next_allowed_at=now + self._spacing_s)
            )
        except OperationalError as error:
            if "locked" not in str(error):
                raise
            logger.info("Provider slot %r: database locked, treated as busy", self._name)
            return None
        return updated == 1

    def _wait_for_slot(self, *, now: float) -> float:
        """Seconds until the slot frees up; a missing row is a setup bug, never a 503."""
        next_allowed_at = (
            ProviderSlot.objects.filter(pk=self._name)
            .values_list("next_allowed_at", flat=True)
            .first()
        )
        if next_allowed_at is None:
            logger.error("Provider slot row %r is missing; run `manage.py migrate`", self._name)
            raise InternalError()
        return max(next_allowed_at - now, SLOT_MIN_POLL_S)


@dataclass(frozen=True, slots=True)
class OsrmRoute:
    """A validated OSRM route: the full geometry, its length, and how far each end snapped."""

    distance_m: float
    duration_s: float
    points: tuple[LonLat, ...]
    snap_m: tuple[float, float]  # (start, finish) input → road, for the FR-42 warning


def route_url(*, base_url: str, start: LonLat, finish: LonLat) -> str:
    """The OSRM route URL; only `%.6f` floats reach the path, never user text (FR-12)."""
    path = ";".join(
        f"{_coordinate_text(lon)},{_coordinate_text(lat)}" for lon, lat in (start, finish)
    )
    return f"{base_url}/route/v1/driving/{path}?overview=full&geometries=geojson"


def _coordinate_text(value: float) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"a coordinate must be a number, got {type(value).__name__}")
    if not math.isfinite(value):
        raise ValueError("a coordinate must be finite")
    return f"{value:.6f}"


def retry_after_seconds(headers: Mapping[str, str]) -> int:
    """OSRM's Retry-After as whole seconds clamped to 1–60; anything else gives 2."""
    seconds = whole_number(headers.get("retry-after", "").strip(), at_most=RETRY_AFTER_MAX_S)
    if seconds is None:  # missing, an HTTP date, "1.5" or "²"
        return DEFAULT_RETRY_AFTER_S
    return max(RETRY_AFTER_MIN_S, seconds)


class _MalformedReply(Exception):
    """An OSRM body we cannot use; always ends as a logged 502."""


def parse_route_response(response: HttpResponse, *, routing_calls: int) -> OsrmRoute:
    """Map one OSRM reply to a validated route or the §7a error (§8 #2, FR-11, FR-36).

    The HTTP status decides first: 429 → 503, anything but 200/400 → 502 (so a 503
    whose body says NoRoute is still an outage). Only for 200/400 does the body `code`
    decide. An `Ok` route must pass every check, or it is a 502 and is never cached.
    """
    if response.status == 429:
        logger.warning("OSRM answered 429 (calls used %s)", routing_calls)
        raise RoutingRateLimited(
            routing_calls=routing_calls,
            retry_after_s=retry_after_seconds(response.headers),
            source="provider",
        )
    try:
        if response.status not in (200, 400):
            raise _MalformedReply(f"HTTP status {response.status}")
        payload = _json_object(response.body)
        code = payload.get("code")
        if not isinstance(code, str):
            raise _MalformedReply("no string `code`")
        if code in NO_ROUTE_MESSAGES:
            logger.info("OSRM found no route: %s (calls used %s)", code, routing_calls)
            raise NoRoute(provider_code=code, routing_calls=routing_calls)
        if code.startswith("Invalid") or code in INTERNAL_ERROR_CODES:
            logger.error(
                "OSRM rejected our request as %r: %r (calls used %s)",
                code[:LOGGED_TEXT_MAX_CHARS],
                str(payload.get("message", ""))[:LOGGED_TEXT_MAX_CHARS],
                routing_calls,
            )
            raise InternalError(routing_calls=routing_calls)
        if code != "Ok" or response.status != 200:
            raise _MalformedReply(f"code {code[:LOGGED_TEXT_MAX_CHARS]!r} with {response.status}")
        return _validated_route(payload)
    except _MalformedReply as error:
        logger.warning("OSRM reply unusable: %s (calls used %s)", error, routing_calls)
        raise RoutingUnavailable(routing_calls=routing_calls) from None


def _json_object(body: bytes) -> dict[str, object]:
    """Strict JSON (no NaN/Infinity) that must be an object."""
    try:
        payload = json.loads(body, parse_constant=_refuse_constant)
    # A deeply nested body raises RecursionError, which is not a ValueError.
    except (ValueError, RecursionError):
        raise _MalformedReply("body is not valid JSON") from None
    if not isinstance(payload, dict):
        raise _MalformedReply("body is not a JSON object")
    return payload


def _refuse_constant(name: str) -> float:
    raise ValueError(f"{name} is not valid JSON")


def _validated_route(payload: Mapping[str, object]) -> OsrmRoute:
    routes = payload.get("routes")
    if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
        raise _MalformedReply("no routes")
    route = routes[0]
    distance_m = _non_negative(route.get("distance"), what="distance")
    duration_s = _non_negative(route.get("duration"), what="duration")
    points = _line_points(route.get("geometry"))
    snap_m = _snap_distances(payload.get("waypoints"))
    _check_route_has_length(points, distance_m=distance_m)
    return OsrmRoute(distance_m=distance_m, duration_s=duration_s, points=points, snap_m=snap_m)


def _finite_number(value: object) -> float | None:
    """The value as a finite float, or None for bools, non-numbers, NaN and ±inf."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:  # a huge JSON integer
        return None
    return number if math.isfinite(number) else None


def _non_negative(value: object, *, what: str) -> float:
    number = _finite_number(value)
    if number is None or number < 0:
        raise _MalformedReply(f"{what} is not a finite number >= 0")
    return number


def _line_points(geometry: object) -> tuple[LonLat, ...]:
    """GeoJSON LineString positions as (lon, lat), at least 2, all finite and in range."""
    if not isinstance(geometry, dict) or geometry.get("type") != "LineString":
        raise _MalformedReply("geometry is not a LineString")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or len(coordinates) < 2:
        raise _MalformedReply("geometry has fewer than 2 points")
    return tuple(_position(position) for position in coordinates)


def _position(position: object) -> LonLat:
    if not isinstance(position, list) or len(position) < 2:
        raise _MalformedReply("a position is not [lon, lat]")
    lon, lat = _finite_number(position[0]), _finite_number(position[1])
    if lon is None or lat is None or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise _MalformedReply("a position is not a finite [lon, lat] in range")
    return (lon, lat)


def _snap_distances(waypoints: object) -> tuple[float, float]:
    """Metres from each input point to the road (FR-42); OSRM sends one waypoint per end."""
    if not isinstance(waypoints, list) or len(waypoints) != 2:
        raise _MalformedReply("expected exactly 2 waypoints")
    if not all(isinstance(waypoint, dict) for waypoint in waypoints):
        raise _MalformedReply("a waypoint is not an object")
    start, finish = (
        _non_negative(waypoint.get("distance"), what="waypoint distance") for waypoint in waypoints
    )
    return (start, finish)


def _check_route_has_length(points: tuple[LonLat, ...], *, distance_m: float) -> None:
    """Refuse a route whose line has zero length but whose distance shows as ≥ 0.1 mi.

    Mile markers are scaled so the line's length equals OSRM's distance; a line of one
    repeated point cannot be scaled. Two equal points with a distance that shows as
    0.0 mi stay valid: both ends snapped to the same road spot.
    """
    if any(point != points[0] for point in points):
        return
    shown_miles = Decimal(repr(miles_from_meters(distance_m))).quantize(
        MIN_GEOMETRY_CHECK_MILES, rounding=ROUND_HALF_UP
    )
    if shown_miles >= MIN_GEOMETRY_CHECK_MILES:
        raise _MalformedReply("zero-length geometry for a non-zero distance")


class RoutingClient:
    """Fetches one route from OSRM per call, inside the request's call budget."""

    def __init__(
        self,
        *,
        base_url: str,
        transport: Transport,
        throttle: ProviderThrottle,
        clock: Clock,
        connect_timeout_s: float,
        total_timeout_s: float,
    ) -> None:
        self._base_url = require_http_base_url(base_url)
        self._transport = transport
        self._throttle = throttle
        self._clock = clock
        self._connect_timeout_s = connect_timeout_s
        self._total_timeout_s = total_timeout_s

    @property
    def base_url(self) -> str:
        """The configured OSRM base URL (part of the route and plan cache keys)."""
        return self._base_url

    def fetch_route(self, *, start: LonLat, finish: LonLat, budget: CallBudget) -> OsrmRoute:
        """One OSRM call, never retried; every failure raises its §7a domain error."""
        budget.ensure_available()
        url = route_url(base_url=self._base_url, start=start, finish=finish)
        if not self._throttle.try_acquire():
            logger.warning(
                "No provider slot within %.1f s (calls used %s)", SLOT_MAX_WAIT_S, budget.used
            )
            raise RoutingRateLimited(
                routing_calls=budget.used, retry_after_s=DEFAULT_RETRY_AFTER_S, source="slot"
            )
        budget.spend()
        started = self._clock.monotonic()
        try:
            response = self._transport.request(
                method="GET",
                url=url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                connect_timeout_s=self._connect_timeout_s,
                total_timeout_s=self._total_timeout_s,
                max_bytes=MAX_ROUTE_BYTES,
            )
        # TransportTimeout is a TransportError, so it has to be caught first.
        except TransportTimeout as error:
            self._log_failure(error, started=started, budget=budget)
            raise RoutingTimeout(
                routing_calls=budget.used, timeout_s=self._total_timeout_s
            ) from None
        except TransportError as error:  # refused, DNS, TLS, or a body over 8 MiB
            self._log_failure(error, started=started, budget=budget)
            raise RoutingUnavailable(routing_calls=budget.used) from None
        return parse_route_response(response, routing_calls=budget.used)

    def _log_failure(self, error: TransportError, *, started: float, budget: CallBudget) -> None:
        elapsed_s = self._clock.monotonic() - started
        logger.warning(
            "OSRM %s after %.1f s (calls used %s): %s",
            type(error).__name__,
            elapsed_s,
            budget.used,
            error,
        )


def build_routing_client() -> RoutingClient:
    """The client from settings; the one seam tests patch to inject fakes."""
    clock = SystemClock()
    return RoutingClient(
        base_url=settings.FUELPILOT_ROUTING_BASE_URL,
        transport=HttpClientTransport(),
        throttle=ProviderThrottle(clock=clock),
        clock=clock,
        connect_timeout_s=settings.FUELPILOT_ROUTING_CONNECT_TIMEOUT_S,
        total_timeout_s=settings.FUELPILOT_ROUTING_READ_TIMEOUT_S,
    )
