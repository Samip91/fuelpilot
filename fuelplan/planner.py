"""The trip planner: one validated request in, one saved plan (or a domain error) out.

Spec 01 §15 "Cache flow". It orchestrates only; geometry, prices, HTTP and rounding live
in their own modules:

    resolve start and finish → station data loaded? (FR-43) → saved plan? (0 calls)
    → start = finish? → route: cached, or 1 OSRM call → corridor → candidates
    → optimise → verification seam → save planobj, then the plan key

Every collaborator is passed in, so tests run the whole flow with a fake transport, a
synthetic station index and a test gazetteer. Cache writes are best effort: a failed
write is logged and the answer still goes out, but a plan key is only ever written after
its saved plan, so a key can never point at nothing the code wrote (FR-38, FR-41).
"""

import hashlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from uuid import uuid4

from django.conf import settings
from django.core.cache import caches
from django.core.cache.backends.base import BaseCache
from django.db import DatabaseError, connections

from .errors import InternalError, JsonValue, StationDataMissing
from .geo import (
    ROUTE_TOLERANCE_MILES,
    LonLat,
    RouteCorridor,
    RouteGeometryError,
    RouteLine,
    build_route_line,
    is_within_detour_limit,
    miles_from_meters,
    quantise_miles,
    simplify_for_response,
    simplify_polyline,
)
from .locations import (
    EndpointField,
    LocationDataError,
    LocationResolver,
    ResolvedEndpoint,
    get_boundary,
    get_gazetteer,
)
from .optimizer import (
    Candidate,
    FuelGap,
    FuelPlan,
    OptimizerError,
    first_leg_range_miles,
    plan_fuel_stops,
)
from .routing import CallBudget, RoutingClient, build_routing_client
from .serializers import (
    CityInput,
    EndpointInput,
    PlanFacts,
    PlanRequest,
    endpoint_echo,
    no_feasible_plan_error,
    plan_body,
)
from .stations import StationIndex, get_station_index
from .verification import DetourVerifier, VerificationStatus, load_detour_verifier

logger = logging.getLogger(__name__)

# Part of the plan key: bump when the detour verifier or planning rules change.
ALGORITHM_VERSION = "1"
ROUTE_TTL_S = 86_400  # FR-37: a fetched route is reused for 24 h
PLAN_KEY_MAX_TTL_S = 86_400  # §6: the plan key lives min(24 h, PLAN_TTL_S)
ENDPOINT_DECIMALS = 5  # endpoints are compared, cached and routed at 5 dp
FUEL_KEY_STEP = Decimal("0.1")

RouteCacheStatus = Literal["hit", "miss", "skipped"]
PlanCacheStatus = Literal["hit", "miss"]


@dataclass(frozen=True, slots=True)
class Meta:
    """FR-32: how this response was made; built per response, never saved."""

    routing_calls: int
    route_cache: RouteCacheStatus
    plan_cache: PlanCacheStatus

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "routing_calls": self.routing_calls,
            "route_cache": self.route_cache,
            "plan_cache": self.plan_cache,
        }


@dataclass(frozen=True, slots=True)
class PlanOutcome:
    """The saved body (no `map_url`, no `meta`) with this request's start/finish echo."""

    plan_id: str
    body: dict[str, JsonValue]
    meta: Meta


@dataclass(frozen=True, slots=True)
class CachedRoute:
    """What the planner keeps of an OSRM route: its length, the 10 m line and the snaps.

    It is cached as plain lists and floats, never as a pickled class, so a code change
    cannot break reading an older entry; an unreadable entry is simply a miss.
    """

    distance_m: float
    duration_s: float
    points: tuple[LonLat, ...]
    snap_m: tuple[float, float]

    @classmethod
    def at_point(cls, point: LonLat) -> "CachedRoute":
        """The FR-8 "route" when start = finish: no distance, geometry [start, start]."""
        return cls(distance_m=0.0, duration_s=0.0, points=(point, point), snap_m=(0.0, 0.0))

    def to_cache_value(self) -> dict[str, object]:
        return {
            "distance_m": self.distance_m,
            "duration_s": self.duration_s,
            "points": [[lon, lat] for lon, lat in self.points],
            "snap_m": list(self.snap_m),
        }

    @classmethod
    def from_cache_value(cls, value: object) -> "CachedRoute | None":
        """The route this code cached, or None for any other shape (the caller logs it)."""
        if not isinstance(value, dict):
            return None
        try:
            points = tuple((float(lon), float(lat)) for lon, lat in value["points"])
            start_snap_m, finish_snap_m = value["snap_m"]
            route = cls(
                distance_m=float(value["distance_m"]),
                duration_s=float(value["duration_s"]),
                points=points,
                snap_m=(float(start_snap_m), float(finish_snap_m)),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return route if len(points) >= 2 else None


@dataclass(frozen=True, slots=True)
class TripEnds:
    """Start and finish of one request: the 5-dp points and their FR-7 echoes."""

    start: LonLat
    finish: LonLat
    echoes: dict[str, JsonValue]  # {"start": …, "finish": …} for this request

    @property
    def is_same_point(self) -> bool:
        return self.start == self.finish


@dataclass(frozen=True, slots=True)
class NewPlan:
    """A freshly built plan body and how it was made (route status, verification status)."""

    plan_id: str
    body: dict[str, JsonValue]
    route_cache: RouteCacheStatus
    verification_status: VerificationStatus


def rounded_point(endpoint: ResolvedEndpoint) -> LonLat:
    """The endpoint as (lon, lat) at 5 dp: the value compared, cached and sent to OSRM."""
    return (
        round(endpoint.longitude, ENDPOINT_DECIMALS),
        round(endpoint.latitude, ENDPOINT_DECIMALS),
    )


def endpoint_text(point: LonLat) -> str:
    """One endpoint inside a cache key: "lon,lat" with exactly 5 decimals."""
    lon, lat = point
    return f"{lon:.5f},{lat:.5f}"


def _digest(*parts: str) -> str:
    """sha256 of the parts, one per line: user input reaches a key only through this."""
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _fuel_key_text(gallons: Decimal) -> str:
    """Fuel at 0.1 in the plan key, so 5, 5.0 and "5.0" share one key (TC-32)."""
    return str(gallons.quantize(FUEL_KEY_STEP, rounding=ROUND_HALF_UP))


def route_cache_key(*, base_url: str, start: LonLat, finish: LonLat) -> str:
    """§6 route key: base URL and both endpoints, so another OSRM server never hits."""
    # Bump v1 when the stored line's tolerance (ROUTE_TOLERANCE_MILES) changes.
    return "route:v1:" + _digest(base_url, endpoint_text(start), endpoint_text(finish))


def plan_cache_key(
    *,
    dataset_version: str,
    base_url: str,
    start: LonLat,
    finish: LonLat,
    current_gallons: Decimal,
    reserve_gallons: Decimal,
) -> str:
    """§6 plan key: a new import or algorithm version never returns an older plan (FR-40)."""
    digest = _digest(
        base_url,
        endpoint_text(start),
        endpoint_text(finish),
        _fuel_key_text(current_gallons),
        _fuel_key_text(reserve_gallons),
    )
    return f"plan:v1:{dataset_version}:{ALGORITHM_VERSION}:{digest}"


def planobj_cache_key(plan_id: str) -> str:
    """§6 saved-plan key; spec 02's map page reads the plan from here."""
    return f"planobj:v1:{plan_id}"


def plan_key_ttl_s(plan_ttl_s: int) -> int:
    """FR-41: the plan key never outlives the saved plan it points to."""
    return min(PLAN_KEY_MAX_TTL_S, plan_ttl_s)


def apply_measured_detours(
    candidates: Sequence[Candidate], measured_detour_miles: Mapping[int, Decimal]
) -> tuple[Candidate, ...]:
    """FR-44: a measured detour replaces the estimate and marks the station verified.

    A station measured above 5 mi stops being a candidate. A result naming an unknown
    station or a detour that is not a finite Decimal ≥ 0 is a verifier bug (→ 500).
    """
    unknown = set(measured_detour_miles) - {candidate.station_id for candidate in candidates}
    if unknown:
        raise OptimizerError(f"verifier measured stations that are not candidates: {unknown}")
    for station_id, detour in measured_detour_miles.items():
        if not isinstance(detour, Decimal) or not detour.is_finite() or detour < 0:
            raise OptimizerError(f"verifier gave station {station_id} the detour {detour!r}")
    updated = []
    for candidate in candidates:
        detour = measured_detour_miles.get(candidate.station_id)
        if detour is not None:
            if not is_within_detour_limit(detour):
                continue
            candidate = replace(candidate, detour_miles=detour, is_detour_verified=True)
        updated.append(candidate)
    return tuple(updated)


class TripPlanner:
    """Runs one request through the §15 flow; the view creates one per request."""

    def __init__(
        self,
        *,
        resolver: LocationResolver,
        station_loader: Callable[[], StationIndex],
        routing: RoutingClient,
        verifier_loader: Callable[[], DetourVerifier],
        cache: BaseCache,
        plan_ttl_s: int,
    ) -> None:
        self._resolver = resolver
        self._station_loader = station_loader
        self._routing = routing
        self._verifier_loader = verifier_loader
        self._cache = cache
        self._plan_ttl_s = plan_ttl_s

    def plan(self, *, plan_request: PlanRequest, budget: CallBudget) -> PlanOutcome:
        """The plan for this request, or a domain error; our own bugs become a logged 500."""
        try:
            return self._plan(plan_request=plan_request, budget=budget)
        except (OptimizerError, RouteGeometryError, LocationDataError):
            logger.exception("Planning failed on our side (routing calls used %s)", budget.used)
            raise InternalError() from None

    def _plan(self, *, plan_request: PlanRequest, budget: CallBudget) -> PlanOutcome:
        ends = self._resolve_ends(plan_request)
        stations = self._station_loader()  # FR-43: raises before any cache or route lookup
        plan_key = plan_cache_key(
            dataset_version=stations.dataset_version,
            base_url=self._routing.base_url,
            start=ends.start,
            finish=ends.finish,
            current_gallons=plan_request.current_fuel_gallons,
            reserve_gallons=plan_request.reserve_gallons,
        )
        saved = self._saved_plan(plan_key)
        if saved is not None:
            plan_id, body = saved
            # The saved body with this request's echo: a coordinate request may hit a
            # plan first made from city names (and the other way round).
            return PlanOutcome(
                plan_id=plan_id,
                body={**body, **ends.echoes},
                meta=Meta(routing_calls=budget.used, route_cache="skipped", plan_cache="hit"),
            )

        new_plan = self._new_plan(
            plan_request=plan_request, ends=ends, stations=stations, budget=budget
        )
        self._save_plan(plan_key=plan_key, new_plan=new_plan)
        return PlanOutcome(
            plan_id=new_plan.plan_id,
            body=new_plan.body,
            meta=Meta(
                routing_calls=budget.used, route_cache=new_plan.route_cache, plan_cache="miss"
            ),
        )

    def _resolve_ends(self, plan_request: PlanRequest) -> TripEnds:
        start = self._resolve(plan_request.start, field="start")
        finish = self._resolve(plan_request.finish, field="finish")
        start_point, finish_point = rounded_point(start), rounded_point(finish)
        return TripEnds(
            start=start_point,
            finish=finish_point,
            echoes={
                "start": endpoint_echo(
                    endpoint=plan_request.start, resolved=start, point=start_point
                ),
                "finish": endpoint_echo(
                    endpoint=plan_request.finish, resolved=finish, point=finish_point
                ),
            },
        )

    def _resolve(self, endpoint: EndpointInput, *, field: EndpointField) -> ResolvedEndpoint:
        """City input via the gazetteer, coordinates via the boundary polygon (0 calls)."""
        if isinstance(endpoint, CityInput):
            return self._resolver.resolve_city(
                field=field, city=endpoint.city, state=endpoint.state
            )
        return self._resolver.resolve_point(
            field=field, latitude=endpoint.latitude, longitude=endpoint.longitude
        )

    def _new_plan(
        self,
        *,
        plan_request: PlanRequest,
        ends: TripEnds,
        stations: StationIndex,
        budget: CallBudget,
    ) -> NewPlan:
        """Route → candidates → optimise → verify → body; raises 422 when infeasible."""
        if ends.is_same_point:
            route, route_status = CachedRoute.at_point(ends.start), "skipped"
        else:
            route, route_status = self._route(start=ends.start, finish=ends.finish, budget=budget)
        line = build_route_line(route.points, length_miles=miles_from_meters(route.distance_m))
        if route_status == "miss":
            # Cached only now that its geometry is usable; a validated OSRM `Ok` (FR-36),
            # kept even if this request then ends in `no_feasible_plan`.
            self._cache_route(start=ends.start, finish=ends.finish, route=route)
        route_miles = quantise_miles(line.length_miles)
        candidates = () if ends.is_same_point else stations.candidates_on(RouteCorridor(line))

        plan = self._optimise(
            route_miles=route_miles, candidates=candidates, plan_request=plan_request
        )
        plan, verification_status = self._verify(
            plan=plan,
            line=line,
            candidates=candidates,
            route_miles=route_miles,
            plan_request=plan_request,
            budget=budget,
        )
        plan_id = str(uuid4())
        body = plan_body(
            PlanFacts(
                plan_id=plan_id,
                start_echo=ends.echoes["start"],
                finish_echo=ends.echoes["finish"],
                route_miles=route_miles,
                geometry=simplify_for_response(route.points),
                snap_m=None if ends.is_same_point else route.snap_m,
                plan=plan,
                stations=stations,
                start_gallons=plan_request.current_fuel_gallons,
                reserve_gallons=plan_request.reserve_gallons,
                algorithm_version=ALGORITHM_VERSION,
                is_same_point=ends.is_same_point,
            )
        )
        return NewPlan(
            plan_id=plan_id,
            body=body,
            route_cache=route_status,
            verification_status=verification_status,
        )

    def _route(
        self, *, start: LonLat, finish: LonLat, budget: CallBudget
    ) -> tuple[CachedRoute, RouteCacheStatus]:
        """The cached route for these ends, or one OSRM call cut to its 10 m line."""
        key = route_cache_key(base_url=self._routing.base_url, start=start, finish=finish)
        value = self._cache.get(key)
        cached = CachedRoute.from_cache_value(value)
        if cached is not None:
            return cached, "hit"
        if value is not None:
            logger.warning("Cached route %s… is unreadable; fetching it again", key[:20])

        osrm = self._routing.fetch_route(start=start, finish=finish, budget=budget)
        route = CachedRoute(
            distance_m=osrm.distance_m,
            duration_s=osrm.duration_s,
            points=simplify_polyline(osrm.points, tolerance_miles=ROUTE_TOLERANCE_MILES),
            snap_m=osrm.snap_m,
        )
        return route, "miss"

    def _cache_route(self, *, start: LonLat, finish: LonLat, route: CachedRoute) -> None:
        """Keep a fetched route for 24 h (FR-37); best effort, like every cache write."""
        key = route_cache_key(base_url=self._routing.base_url, start=start, finish=finish)
        self._write_best_effort(
            lambda: self._cache.set(key, route.to_cache_value(), ROUTE_TTL_S), what="route"
        )

    def _optimise(
        self, *, route_miles: Decimal, candidates: Sequence[Candidate], plan_request: PlanRequest
    ) -> FuelPlan:
        """The cheapest plan, or the 422 `no_feasible_plan` naming the gap (FR-23)."""
        result = plan_fuel_stops(
            route_miles=route_miles,
            candidates=candidates,
            start_gallons=plan_request.current_fuel_gallons,
            reserve_gallons=plan_request.reserve_gallons,
        )
        if isinstance(result, FuelGap):
            raise no_feasible_plan_error(
                gap=result,
                route_miles=route_miles,
                first_leg_miles=first_leg_range_miles(
                    start_gallons=plan_request.current_fuel_gallons,
                    reserve_gallons=plan_request.reserve_gallons,
                ),
            )
        return result

    def _verify(
        self,
        *,
        plan: FuelPlan,
        line: RouteLine,
        candidates: Sequence[Candidate],
        route_miles: Decimal,
        plan_request: PlanRequest,
        budget: CallBudget,
    ) -> tuple[FuelPlan, VerificationStatus]:
        """FR-44: ask the verifier; on `verified`, re-plan once with the measured detours."""
        verifier = self._verifier_loader()
        # Future work (README): road-measured detours would pass backups here; v1 sends none.
        result = verifier(route=line, chosen_stops=plan.stops, backups=[], budget=budget)
        if result.status != "verified":
            return plan, result.status
        measured = apply_measured_detours(candidates, result.measured_detour_miles)
        replanned = self._optimise(
            route_miles=route_miles, candidates=measured, plan_request=plan_request
        )
        return replanned, result.status

    def _saved_plan(self, plan_key: str) -> tuple[str, dict[str, JsonValue]] | None:
        """(plan_id, saved body) when both keys are present; a dangling key is a miss (FR-41)."""
        plan_id = self._cache.get(plan_key)
        if plan_id is None:
            return None
        body = self._cache.get(planobj_cache_key(plan_id)) if isinstance(plan_id, str) else None
        if not isinstance(body, dict):
            logger.info("Plan key found but its saved plan is gone; planning again (FR-41)")
            return None
        return plan_id, body

    def _save_plan(self, *, plan_key: str, new_plan: NewPlan) -> None:
        """planobj with `cache.add`, then the plan key only if that write succeeded (FR-38)."""
        plan_id = new_plan.plan_id
        is_saved = self._write_best_effort(
            lambda: self._cache.add(planobj_cache_key(plan_id), new_plan.body, self._plan_ttl_s),
            what="saved plan",
        )
        if not is_saved:
            logger.warning("Plan %s was not saved, so no plan key is written", plan_id)
            return
        if new_plan.verification_status == "failed":
            return  # FR-44: a plan whose verification failed is never reused
        self._write_best_effort(
            lambda: self._cache.set(plan_key, plan_id, plan_key_ttl_s(self._plan_ttl_s)),
            what="plan key",
        )

    @staticmethod
    def _write_best_effort(write: Callable[[], object], *, what: str) -> bool:
        """Run one cache write; a database error is logged and the request goes on.

        Returns False when the write raised or `cache.add` found the key taken. Never
        retried: a lost route write costs the next request one call, within budget.
        """
        try:
            result = write()
        except DatabaseError:
            logger.warning(
                "Cache write of the %s failed; continuing without it", what, exc_info=True
            )
            return False
        return result is not False


def build_trip_planner() -> TripPlanner:
    """The planner wired from settings; the one seam the view uses and tests replace."""
    return TripPlanner(
        resolver=LocationResolver(gazetteer_loader=get_gazetteer, boundary_loader=get_boundary),
        station_loader=get_station_index,
        routing=build_routing_client(),
        verifier_loader=load_detour_verifier,
        cache=caches["default"],
        plan_ttl_s=settings.FUELPILOT_PLAN_TTL_S,
    )


def warm_up() -> None:
    """Load the boundary, places and station index once, before the server takes requests.

    Called from `config/wsgi.py`, so only runserver and gunicorn pay it (migrate and tests
    never import that module); it makes no external call. Boundary and places are tracked
    files, so their errors propagate (fail fast); missing station data only logs a warning.
    """
    try:
        get_boundary()
        get_gazetteer()
        stations = _load_stations_for_warm_up()
        if stations is not None:
            logger.info(
                "Startup warm-up loaded %s stations (dataset %s), places and boundary",
                len(stations),
                stations.dataset_version,
            )
    finally:
        # Like runserver after its migration check: no connection opened here is kept
        # for the serving threads. Closes only this (the calling) thread's connections.
        connections.close_all()


def _load_stations_for_warm_up() -> StationIndex | None:
    """The station index, or None after logging why it is missing.

    The server still starts: a failed load is not kept, so requests answer FR-43 until
    `migrate` and the import run, and then the next request loads the stations (no restart).
    """
    try:
        return get_station_index()
    except (StationDataMissing, DatabaseError) as error:
        logger.warning(
            "Startup warm-up could not load stations (%s: %s); requests report it until "
            "`python manage.py migrate` and `python manage.py import_fuel_stations` run, "
            "then the next request loads them",
            type(error).__name__,
            error,
        )
        return None
