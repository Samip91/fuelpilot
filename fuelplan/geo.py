"""Pure route geometry: distances, the station corridor and line simplification.

Spec 01 FR-18, NFR-6, NFR-13, §8 #10 and §15 "Corridor (D7)". No Django, no I/O and
no project imports, so every function here can be tested and explained on its own.
Floats are fine for geometry; values that reach the money path (route miles and detour
miles) leave this module as `Decimal`, quantised exactly once.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

EARTH_RADIUS_MILES = 3958.7613  # mean Earth radius, 6371.0088 km
KM_PER_MILE = 1.609344
METERS_PER_MILE = 1609.344
# The one radius used everywhere: miles along a meridian per degree of latitude.
MILES_PER_DEGREE_LATITUDE = EARTH_RADIUS_MILES * math.pi / 180

# The segment grid uses 0.1° cells. An int factor keeps cell numbers free of 0.1 float error.
CELLS_PER_DEGREE = 10
# Pad around each segment: must exceed MAX_DETOUR_MILES / 2 plus half a MILE_STEP (2.505),
# so any segment that could still give a kept station is registered in the station's cell.
CORRIDOR_PAD_MILES = 2.6
MAX_DETOUR_MILES = Decimal("5.00")
MILE_STEP = Decimal("0.01")

ROUTE_TOLERANCE_MILES = 10 / METERS_PER_MILE  # the 10 m line the route cache stores
RESPONSE_TOLERANCE_MILES = 50 / METERS_PER_MILE  # the response map line (NFR-6)
RESPONSE_MAX_POSITIONS = 1_999  # NFR-6: fewer than 2,000 positions

LonLat = tuple[float, float]  # (longitude, latitude), GeoJSON order as OSRM sends it
GridCell = tuple[int, int]  # (latitude cell, longitude cell)


class RouteGeometryError(ValueError):
    """A broken precondition (e.g. fewer than 2 points): a programmer error, reported as a 500."""


@dataclass(frozen=True, slots=True)
class RouteLine:
    """Route points with their mile markers; the last marker equals the OSRM distance."""

    points: tuple[LonLat, ...]
    cumulative_miles: tuple[float, ...]
    length_miles: float


@dataclass(frozen=True, slots=True)
class RoutePosition:
    """Where a kept station sits on the route; all values are miles quantised to 0.01."""

    route_mile: Decimal
    offset_miles: Decimal
    estimated_detour_miles: Decimal


@dataclass(frozen=True, slots=True)
class SegmentFoot:
    """The closest point of one segment to a station: `fraction` 0..1 along it, offset in miles."""

    fraction: float
    offset_miles: float


def haversine_miles(*, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points in degrees, in miles."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = phi2 - phi1
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(a))


def miles_from_meters(meters: float) -> float:
    """Convert an OSRM distance in metres to miles."""
    return meters / METERS_PER_MILE


def quantise_miles(miles: float) -> Decimal:
    """A float distance as Decimal miles to 0.01, ROUND_HALF_UP (§8 #10).

    `Decimal(repr(x))` keeps the shortest decimal text of the float, so 2.505 rounds
    to 2.51 as a reader expects (`Decimal(2.505)` would give 2.50). A zero result is
    always "0.00", never "-0.00".
    """
    if not math.isfinite(miles):
        raise RouteGeometryError(f"distance must be finite, got {miles!r}")
    quantised = Decimal(repr(miles)).quantize(MILE_STEP, rounding=ROUND_HALF_UP)
    return quantised if quantised else Decimal("0.00")


def estimated_detour_miles(offset_miles: float) -> Decimal:
    """Out to the station and back: twice the quantised offset, a lower bound (§15)."""
    return 2 * quantise_miles(offset_miles)


def is_within_detour_limit(detour_miles: Decimal) -> bool:
    """FR-18: a station is a candidate only if its detour is at most 5.00 miles."""
    return detour_miles <= MAX_DETOUR_MILES


def build_route_line(points: Sequence[LonLat], *, length_miles: float) -> RouteLine:
    """Mile markers along the points, scaled so the finish equals `length_miles` exactly.

    The haversine sum of a simplified line is a little shorter than the road, so the
    markers are stretched to the OSRM distance. A line of identical points has no
    length to scale: every marker is 0.0 except the finish, which is still L.
    """
    _require_line(points)
    if not math.isfinite(length_miles) or length_miles < 0:
        raise RouteGeometryError(f"route length must be finite and >= 0, got {length_miles!r}")
    raw_miles = [0.0]
    for (lon1, lat1), (lon2, lat2) in zip(points, points[1:], strict=False):
        raw_miles.append(
            raw_miles[-1] + haversine_miles(lat1=lat1, lon1=lon1, lat2=lat2, lon2=lon2)
        )
    raw_total = raw_miles[-1]
    scale = length_miles / raw_total if raw_total > 0 else 0.0
    # min(): scaling can land 1 ulp above L (e.g. a line ending in a repeated point).
    cumulative = [min(mile * scale, length_miles) for mile in raw_miles]
    cumulative[-1] = length_miles  # pin the finish: float drift must not move it off L
    return RouteLine(
        points=tuple(points), cumulative_miles=tuple(cumulative), length_miles=length_miles
    )


def project_onto_segment(
    *, latitude: float, longitude: float, start: LonLat, end: LonLat
) -> SegmentFoot:
    """The foot of a station on one segment, in a flat map centred on the station.

    Within a few miles a local equirectangular projection is accurate enough to find
    the foot; the offset itself is the haversine distance from the station to that foot.
    """
    cos_latitude = math.cos(math.radians(latitude))
    start_x = (start[0] - longitude) * cos_latitude
    start_y = start[1] - latitude
    delta_x = (end[0] - start[0]) * cos_latitude
    delta_y = end[1] - start[1]
    length_squared = delta_x * delta_x + delta_y * delta_y
    if length_squared == 0:
        fraction = 0.0  # zero-length segment: the foot is its only point
    else:
        fraction = -(start_x * delta_x + start_y * delta_y) / length_squared
        fraction = min(max(fraction, 0.0), 1.0)
    foot_lon = start[0] + fraction * (end[0] - start[0])
    foot_lat = start[1] + fraction * (end[1] - start[1])
    offset_miles = haversine_miles(lat1=latitude, lon1=longitude, lat2=foot_lat, lon2=foot_lon)
    return SegmentFoot(fraction=fraction, offset_miles=offset_miles)


def grid_cell(*, latitude: float, longitude: float) -> GridCell:
    """The 0.1° cell of a point; the same function serves grid inserts and lookups."""
    return (
        math.floor(latitude * CELLS_PER_DEGREE),
        math.floor(longitude * CELLS_PER_DEGREE),
    )


class RouteCorridor:
    """Says where a station sits on one route, or that it is too far off it (FR-18).

    Each segment is registered in every grid cell its bounding box, padded by
    CORRIDOR_PAD_MILES, touches. A lookup then reads only the station's own cell.
    """

    def __init__(self, line: RouteLine) -> None:
        self._line = line
        self._segments_by_cell = self._build_grid(line.points)

    def locate(self, *, latitude: float, longitude: float) -> RoutePosition | None:
        """The station's mile, offset and detour, or None if its detour would exceed 5.00 mi.

        Nearest segment wins on the offset quantised to 0.01; a tie goes to the smaller
        mile, so an out-and-back road puts the station on the outbound pass (owner
        decision 2026-10-27).
        """
        best_key: tuple[Decimal, float] | None = None
        cell = grid_cell(latitude=latitude, longitude=longitude)
        for segment in self._segments_by_cell.get(cell, ()):
            foot = project_onto_segment(
                latitude=latitude,
                longitude=longitude,
                start=self._line.points[segment],
                end=self._line.points[segment + 1],
            )
            if foot.offset_miles > CORRIDOR_PAD_MILES:
                continue  # its detour is at least 5.20 mi, so it can never keep the station
            key = (quantise_miles(foot.offset_miles), self._mile_at(segment, foot.fraction))
            if best_key is None or key < best_key:
                best_key = key
        if best_key is None:
            return None
        offset, mile = best_key
        detour = 2 * offset  # the same value estimated_detour_miles gives, offset quantised once
        if not is_within_detour_limit(detour):
            return None
        return RoutePosition(
            route_mile=quantise_miles(mile), offset_miles=offset, estimated_detour_miles=detour
        )

    def _mile_at(self, segment: int, fraction: float) -> float:
        """Interpolated mile marker, clamped to [0, L] so a station never lies off the route."""
        miles = self._line.cumulative_miles
        mile = miles[segment] + fraction * (miles[segment + 1] - miles[segment])
        return min(max(mile, 0.0), self._line.length_miles)

    @staticmethod
    def _build_grid(points: Sequence[LonLat]) -> dict[GridCell, tuple[int, ...]]:
        """Map each cell to the segments (ascending, i.e. in mile order) that may reach it."""
        pad_lat = CORRIDOR_PAD_MILES / MILES_PER_DEGREE_LATITUDE
        grid: dict[GridCell, list[int]] = {}
        for segment, ((lon1, lat1), (lon2, lat2)) in enumerate(
            zip(points, points[1:], strict=False)
        ):
            # A degree of longitude is shortest at the highest latitude the padded box reaches;
            # the 89° cap only keeps cos() away from zero (routes stay in the lower 48).
            widest_lat = min(max(abs(lat1), abs(lat2)) + pad_lat, 89.0)
            pad_lon = pad_lat / math.cos(math.radians(widest_lat))
            low_lat, low_lon = grid_cell(
                latitude=min(lat1, lat2) - pad_lat, longitude=min(lon1, lon2) - pad_lon
            )
            high_lat, high_lon = grid_cell(
                latitude=max(lat1, lat2) + pad_lat, longitude=max(lon1, lon2) + pad_lon
            )
            for cell_lat in range(low_lat, high_lat + 1):
                for cell_lon in range(low_lon, high_lon + 1):
                    grid.setdefault((cell_lat, cell_lon), []).append(segment)
        return {cell: tuple(segments) for cell, segments in grid.items()}


def simplify_polyline(points: Sequence[LonLat], *, tolerance_miles: float) -> tuple[LonLat, ...]:
    """Douglas–Peucker: drop points closer than the tolerance to the kept line; ends kept.

    Iterative with an explicit stack, so a long route never hits the recursion limit.
    Distances are to the chord *segment*, not its infinite line, so the tip of a U-turn
    is never dropped.
    """
    _require_line(points)
    if not tolerance_miles >= 0:  # also catches NaN
        raise RouteGeometryError(f"tolerance must be >= 0, got {tolerance_miles!r}")
    lons = [point[0] for point in points]
    lats = [point[1] for point in points]
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    tolerance_squared = tolerance_miles * tolerance_miles
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        farthest, farthest_squared = _farthest_from_chord(
            lons=lons, lats=lats, first=first, last=last
        )
        if farthest_squared > tolerance_squared:
            keep[farthest] = True
            stack.append((first, farthest))
            stack.append((farthest, last))
    return tuple(point for point, is_kept in zip(points, keep, strict=True) if is_kept)


def _farthest_from_chord(
    *, lons: list[float], lats: list[float], first: int, last: int
) -> tuple[int, float]:
    """Index and squared distance (miles²) of the point between `first` and `last` that is
    farthest from the chord segment joining them (local equirectangular, chord mid-latitude).

    This is the hot loop of Douglas–Peucker, so it avoids function calls per point.
    """
    x_scale = MILES_PER_DEGREE_LATITUDE * math.cos(math.radians((lats[first] + lats[last]) / 2))
    y_scale = MILES_PER_DEGREE_LATITUDE
    first_lon, first_lat = lons[first], lats[first]
    chord_x = (lons[last] - first_lon) * x_scale
    chord_y = (lats[last] - first_lat) * y_scale
    chord_squared = chord_x * chord_x + chord_y * chord_y
    farthest, farthest_squared = first, -1.0
    for index in range(first + 1, last):
        x = (lons[index] - first_lon) * x_scale
        y = (lats[index] - first_lat) * y_scale
        along = x * chord_x + y * chord_y
        if along <= 0:  # before the chord start; also every point of a zero-length chord
            distance_squared = x * x + y * y
        elif along >= chord_squared:  # past the chord end
            distance_squared = (x - chord_x) ** 2 + (y - chord_y) ** 2
        else:  # beside the chord: perpendicular distance from the cross product
            cross = x * chord_y - y * chord_x
            distance_squared = cross * cross / chord_squared
        if distance_squared > farthest_squared:
            farthest, farthest_squared = index, distance_squared
    return farthest, farthest_squared


def decimate_by_stride(points: Sequence[LonLat], *, max_positions: int) -> tuple[LonLat, ...]:
    """Keep every k-th point plus the last, so at most `max_positions` remain; ends kept.

    The fallback when Douglas–Peucker alone still leaves too many positions (NFR-6).
    """
    if max_positions < 2:
        raise RouteGeometryError(f"max_positions must be at least 2, got {max_positions}")
    count = len(points)
    if count <= max_positions:
        return tuple(points)
    step = math.ceil((count - 1) / (max_positions - 1))
    return tuple(points[index] for index in range(0, count - 1, step)) + (points[-1],)


def simplify_for_response(
    points: Sequence[LonLat],
    *,
    tolerance_miles: float = RESPONSE_TOLERANCE_MILES,
    max_positions: int = RESPONSE_MAX_POSITIONS,
) -> tuple[LonLat, ...]:
    """The response map line: the 10 m route at about 50 m, then the position cap (NFR-6)."""
    simplified = simplify_polyline(points, tolerance_miles=tolerance_miles)
    return decimate_by_stride(simplified, max_positions=max_positions)


def _require_line(points: Sequence[LonLat]) -> None:
    """Fail fast: geometry needs at least 2 points with finite coordinates."""
    if len(points) < 2:
        raise RouteGeometryError(f"a route needs at least 2 points, got {len(points)}")
    if not all(math.isfinite(lon) and math.isfinite(lat) for lon, lat in points):
        raise RouteGeometryError("route points must have finite coordinates")
