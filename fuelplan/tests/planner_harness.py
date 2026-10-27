"""Test wiring for the planner: fake OSRM, synthetic station index, fixture gazetteer.

Synthetic routes run north along the meridian −98.0 from (30.0, −98.0), near Austin TX.
Along a meridian the haversine distance is linear in latitude, so a station placed with
`meridian_point(mile)` sits exactly at that route mile with a detour of 0.00 mi.
"""

import json
from collections.abc import Callable, Sequence
from decimal import Decimal
from functools import cache
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.cache import caches

from fuelplan.geo import METERS_PER_MILE, MILES_PER_DEGREE_LATITUDE, LonLat
from fuelplan.locations import Gazetteer, LocationResolver, get_boundary
from fuelplan.planner import TripPlanner
from fuelplan.routing import ProviderThrottle, RoutingClient
from fuelplan.stations import StationIndex, StationRecord
from fuelplan.verification import load_detour_verifier

from .base import FakeClock, FakeTransport

FIXTURES = Path(__file__).resolve().parent / "fixtures"
OSRM_FIXTURES = FIXTURES / "osrm"
TEST_PLACES = FIXTURES / "us_places_test.tsv"
TEST_BASE_URL = "https://osrm.test"
TEST_DATASET_VERSION = "sha256:" + "0" * 64

MERIDIAN_LON = -98.0
MERIDIAN_START_LAT = 30.0
OFF_ROUTE_STATION_ID = 9_999  # ~30 mi east of every meridian route: never a candidate


@cache
def fixture_gazetteer() -> Gazetteer:
    """The small fixture gazetteer (Chicago, Denver, New York, ...), read once."""
    return Gazetteer.from_tsv(TEST_PLACES)


def meridian_point(mile: float) -> LonLat:
    """The point `mile` miles north of the meridian route's start."""
    return (MERIDIAN_LON, MERIDIAN_START_LAT + mile / MILES_PER_DEGREE_LATITUDE)


def osrm_ok(
    points: Sequence[LonLat], *, miles: float, snap_m: tuple[float, float] = (0.0, 0.0)
) -> dict:
    """An OSRM `Ok` reply with this line, length and endpoint snap distances."""
    return {
        "code": "Ok",
        "routes": [
            {
                "distance": miles * METERS_PER_MILE,
                "duration": miles * 60.0,
                "geometry": {"type": "LineString", "coordinates": [list(p) for p in points]},
            }
        ],
        "waypoints": [{"distance": snap_m[0]}, {"distance": snap_m[1]}],
    }


def meridian_reply(miles: float, *, snap_m: tuple[float, float] = (0.0, 0.0)) -> dict:
    """A straight route of `miles` north along the meridian."""
    return osrm_ok([meridian_point(0.0), meridian_point(miles)], miles=miles, snap_m=snap_m)


def meridian_request(miles: float, **fuel: object) -> dict:
    """A coordinates request whose endpoints are the meridian route's ends."""
    (start_lon, start_lat), (finish_lon, finish_lat) = meridian_point(0.0), meridian_point(miles)
    return {
        "start": {"latitude": start_lat, "longitude": start_lon},
        "finish": {"latitude": finish_lat, "longitude": finish_lon},
        **fuel,
    }


def osrm_fixture(name: str) -> bytes:
    """A recorded or synthetic OSRM reply from `fixtures/osrm/<name>.json`."""
    return (OSRM_FIXTURES / f"{name}.json").read_bytes()


def station(
    station_id: int,
    *,
    point: LonLat,
    price: str = "3.000",
    quality: str = "poi_match",
    name: str | None = None,
    address: str = "1 TEST RD",
    city: str = "Testville",
    state: str = "TX",
) -> StationRecord:
    """A synthetic station record; the price is CSV text, as the import stores it."""
    lon, lat = point
    return StationRecord(
        station_id=station_id,
        name=name or f"TEST STATION {station_id}",
        address=address,
        city=city,
        state=state,
        price_text=price,
        price_per_gallon=Decimal(price),
        latitude=lat,
        longitude=lon,
        location_quality=quality,
    )


def off_route_station() -> StationRecord:
    """The one station FR-43 needs when a test wants no candidates at all."""
    lon, lat = meridian_point(100.0)
    return station(OFF_ROUTE_STATION_ID, point=(lon + 0.6, lat))


def synthetic_index(
    records: Sequence[StationRecord] = (), *, dataset_version: str = TEST_DATASET_VERSION
) -> StationIndex:
    """An index of these stations plus the off-route one."""
    return StationIndex(dataset_version=dataset_version, records=(*records, off_route_station()))


def queue_reply(transport: FakeTransport, reply: object) -> None:
    """Queue an OSRM reply: a dict (JSON 200), an exception, or (status, body, headers)."""
    if isinstance(reply, dict):
        transport.queue_json(200, reply)
    elif isinstance(reply, Exception):
        transport.queue_exception(reply)
    else:
        status, body, headers = reply
        transport.queue_response(status, body, headers=headers)


class PlannerHarness:
    """Builds the real `TripPlanner` around fakes, once per request like the view does.

    Settings (verifier, plan TTL) are read when each request builds its planner, so
    `override_settings` around a POST works exactly as in production.
    """

    def __init__(self, *, index: StationIndex | None = None) -> None:
        self.index = index or synthetic_index()
        # Replaced by `StationIndex.from_database` in the FR-43 tests (empty test DB).
        self.station_loader: Callable[[], StationIndex] = lambda: self.index
        self.transport = FakeTransport()
        self.clock = FakeClock()
        self.base_url = TEST_BASE_URL

    def build(self) -> TripPlanner:
        return TripPlanner(
            resolver=LocationResolver(
                gazetteer_loader=fixture_gazetteer, boundary_loader=get_boundary
            ),
            station_loader=self.station_loader,
            routing=RoutingClient(
                base_url=self.base_url,
                transport=self.transport,
                throttle=ProviderThrottle(clock=self.clock),
                clock=self.clock,
                connect_timeout_s=3,
                total_timeout_s=10,
            ),
            verifier_loader=load_detour_verifier,
            cache=caches["default"],
            plan_ttl_s=settings.FUELPILOT_PLAN_TTL_S,
        )

    def patch_view(self) -> mock._patch:
        """Make the view build this planner for every request."""
        return mock.patch("fuelplan.views.build_trip_planner", side_effect=self.build)

    def queue(self, reply: object) -> None:
        queue_reply(self.transport, reply)

    def queue_fixture(self, name: str) -> None:
        self.transport.queue_response(200, osrm_fixture(name))


def json_body(response: object) -> dict:
    return json.loads(response.content)
