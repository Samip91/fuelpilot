"""Offline location resolving: place names to points, and the lower-48 + DC boundary.

Spec 01 FR-2, FR-3, FR-4, FR-7, NFR-11 (invariant I6). Nothing here makes a network call
or a database query: both data files are read once per process and kept in memory.
"""

import csv
import json
import math
import threading
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import LocationNotFound, UnsupportedLocation
from .regions import LOWER_48_AND_DC, OUTSIDE_SERVICE_AREA

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PLACES_PATH = DATA_DIR / "us_places.tsv"
BOUNDARY_PATH = DATA_DIR / "us_lower48_dc.geojson"
PLACES_COLUMNS = ["name", "name_norm", "state", "lat", "lon", "population"]
MIN_RING_POINTS = 4  # a closed ring: three corners plus the repeated first point

EndpointField = Literal["start", "finish"]
Ring = tuple[tuple[float, float], ...]  # (longitude, latitude) pairs, first == last
BoundingBox = tuple[float, float, float, float]  # lon_min, lat_min, lon_max, lat_max
GeoJsonRings = list[list[list[float]]]  # one GeoJSON polygon: rings of [lon, lat]


class LocationDataError(Exception):
    """A gazetteer or boundary file is missing or not in the expected format."""


def normalise_place_name(text: str) -> str:
    """The one matching key for a city name: accents, case, "." and extra spaces ignored.

    Used both when `build_location_data` writes the gazetteer and when a request is
    looked up, so stored keys and lookups can never drift apart (FR-2).
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(without_marks.casefold().replace(".", "").split())


def normalise_state(code: str) -> str:
    """A state code as stored: trimmed and upper case."""
    return code.strip().upper()


@dataclass(frozen=True, slots=True)
class Place:
    """One populated place from GeoNames."""

    name: str
    state: str
    latitude: float
    longitude: float
    population: int

    @property
    def matched_label(self) -> str:
        """The place as echoed to the caller, e.g. "Chicago, IL" (FR-7)."""
        return f"{self.name}, {self.state}"


@dataclass(frozen=True, slots=True)
class ResolvedEndpoint:
    """A start or finish turned into a point; `matched` is set only for city input."""

    field: EndpointField
    matched: str | None
    latitude: float
    longitude: float


class Gazetteer:
    """Lookup of (normalised city name, state) to its Place, built offline from GeoNames."""

    def __init__(self, places: dict[tuple[str, str], Place]) -> None:
        self._places = places

    @classmethod
    def from_places(cls, items: Iterable[tuple[str, Place]]) -> "Gazetteer":
        """A gazetteer from (any spelling of the name, Place) pairs; used by tests."""
        return cls({(normalise_place_name(name), place.state): place for name, place in items})

    @classmethod
    def from_tsv(cls, path: Path) -> "Gazetteer":
        """Load `us_places.tsv`, checking every row; raises LocationDataError."""
        try:
            with path.open(newline="", encoding="utf-8") as handle:
                return cls.from_lines(handle, path=path)
        except (OSError, UnicodeDecodeError) as error:
            raise LocationDataError(f"cannot read the places file {path}: {error}") from error

    @classmethod
    def from_lines(cls, lines: Iterable[str], *, path: Path) -> "Gazetteer":
        """Parse places-file text already read from `path` (named in errors only)."""
        try:
            return cls(_read_places(csv.reader(lines, delimiter="\t"), path=path))
        except csv.Error as error:
            raise LocationDataError(f"{path} is not a valid places file: {error}") from error

    def __len__(self) -> int:
        return len(self._places)

    def find(self, *, city: str, state: str) -> Place | None:
        """The place for a city name in a state, or None if there is none."""
        return self._places.get((normalise_place_name(city), normalise_state(state)))


def _read_places(rows: Iterable[list[str]], *, path: Path) -> dict[tuple[str, str], Place]:
    rows = iter(rows)
    header = next(rows, None)
    if header is None:
        raise LocationDataError(f"{path} is empty")
    if header != PLACES_COLUMNS:
        raise LocationDataError(f"{path} does not have the header {','.join(PLACES_COLUMNS)}")
    places: dict[tuple[str, str], Place] = {}
    # Keys of one place (its name, ascii name, alternates) share one object to save memory.
    shared: dict[tuple[str, ...], Place] = {}
    for line_number, row in enumerate(rows, start=2):
        if len(row) != len(PLACES_COLUMNS):
            raise LocationDataError(f"{path} line {line_number}: expected 6 columns")
        name, name_norm, state, lat_text, lon_text, population_text = row
        key = (name_norm, state)
        if key in places:
            raise LocationDataError(f"{path} line {line_number}: duplicate key {key!r}")
        signature = (name, state, lat_text, lon_text, population_text)
        if signature not in shared:
            shared[signature] = _parse_place(row, path=path, line_number=line_number)
        places[key] = shared[signature]
    return places


def _parse_place(row: list[str], *, path: Path, line_number: int) -> Place:
    name, _, state, lat_text, lon_text, population_text = row
    try:
        latitude, longitude = float(lat_text), float(lon_text)
    except ValueError:
        raise LocationDataError(f"{path} line {line_number}: bad coordinates") from None
    is_number = population_text.isascii() and population_text.isdecimal()
    if not (math.isfinite(latitude) and math.isfinite(longitude)) or not is_number:
        raise LocationDataError(f"{path} line {line_number}: bad coordinates or population")
    if state not in LOWER_48_AND_DC:
        raise LocationDataError(f"{path} line {line_number}: state {state!r} is not served")
    return Place(
        name=name,
        state=state,
        latitude=latitude,
        longitude=longitude,
        population=int(population_text),
    )


@dataclass(frozen=True, slots=True)
class AreaPolygon:
    """One polygon of a state: its outer ring, any holes, and a bounding box for speed."""

    state: str
    bbox: BoundingBox
    rings: tuple[Ring, ...]

    def contains(self, *, latitude: float, longitude: float) -> bool:
        """Even-odd ray casting over all rings, so holes count as outside."""
        lon_min, lat_min, lon_max, lat_max = self.bbox
        if not (lon_min <= longitude <= lon_max and lat_min <= latitude <= lat_max):
            return False
        inside = False
        for ring in self.rings:
            if _ray_crosses_ring_odd_times(ring, x=longitude, y=latitude):
                inside = not inside
        return inside


def _ray_crosses_ring_odd_times(ring: Ring, *, x: float, y: float) -> bool:
    """True if a ray from (x, y) towards +x crosses the ring's edges an odd number of times."""
    crossings_odd = False
    previous_x, previous_y = ring[-1]
    for current_x, current_y in ring:
        if (current_y > y) != (previous_y > y):
            edge_x = previous_x + (y - previous_y) * (current_x - previous_x) / (
                current_y - previous_y
            )
            if x < edge_x:
                crossings_odd = not crossings_odd
        previous_x, previous_y = current_x, current_y
    return crossings_odd


class Boundary:
    """The lower 48 states + DC as polygons (Census 5m), for coordinate input (FR-4)."""

    def __init__(self, polygons: Iterable[AreaPolygon]) -> None:
        self._polygons = tuple(polygons)

    @classmethod
    def from_geojson(cls, path: Path) -> "Boundary":
        """Load `us_lower48_dc.geojson`; it must hold exactly the 49 served areas."""
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise LocationDataError(f"cannot read the boundary file {path}: {error}") from error
        features = document.get("features") if isinstance(document, dict) else None
        if not isinstance(features, list):
            raise LocationDataError(f"{path} is not a GeoJSON FeatureCollection")
        polygons: list[AreaPolygon] = []
        states: list[str] = []
        for feature in features:
            state, coordinates = _state_multipolygon(feature, path=path)
            states.append(state)
            polygons.extend(polygon_from_rings(state=state, rings=rings) for rings in coordinates)
        if len(states) != len(LOWER_48_AND_DC) or set(states) != LOWER_48_AND_DC:
            raise LocationDataError(f"{path} must hold exactly the 48 states + DC once each")
        return cls(polygons)

    def contains(self, *, latitude: float, longitude: float) -> bool:
        """True if the point lies inside any served area."""
        return any(
            polygon.contains(latitude=latitude, longitude=longitude) for polygon in self._polygons
        )


def polygon_from_rings(*, state: str, rings: GeoJsonRings) -> AreaPolygon:
    """An AreaPolygon from GeoJSON rings ([[lon, lat], ...]); the first ring is the outer one."""
    parsed = tuple(tuple((lon, lat) for lon, lat in ring) for ring in rings)
    outer = parsed[0]
    longitudes = [lon for lon, _ in outer]
    latitudes = [lat for _, lat in outer]
    bbox = (min(longitudes), min(latitudes), max(longitudes), max(latitudes))
    return AreaPolygon(state=state, bbox=bbox, rings=parsed)


def _state_multipolygon(feature: object, *, path: Path) -> tuple[str, list[GeoJsonRings]]:
    """The (STUSPS, MultiPolygon coordinates) of one feature, after checking their shape."""
    properties = feature.get("properties") if isinstance(feature, dict) else None
    geometry = feature.get("geometry") if isinstance(feature, dict) else None
    state = properties.get("STUSPS") if isinstance(properties, dict) else None
    if state not in LOWER_48_AND_DC:
        raise LocationDataError(f"{path}: a feature has no served STUSPS ({state!r})")
    if not isinstance(geometry, dict) or geometry.get("type") != "MultiPolygon":
        raise LocationDataError(f"{path}: {state} is not a MultiPolygon")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list) or not coordinates:
        raise LocationDataError(f"{path}: {state} has no polygons")
    for rings in coordinates:
        if not isinstance(rings, list) or not rings or not all(_is_ring(ring) for ring in rings):
            raise LocationDataError(f"{path}: {state} has a malformed ring")
    return state, coordinates


def _is_ring(ring: object) -> bool:
    return (
        isinstance(ring, list)
        and len(ring) >= MIN_RING_POINTS
        and all(_is_position(position) for position in ring)
    )


def _is_position(position: object) -> bool:
    return (
        isinstance(position, list)
        and len(position) == 2
        and all(
            isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)
            for value in position
        )
    )


class LocationResolver:
    """Turns a request endpoint into a point inside the service area, or a domain error.

    State codes are checked for being real codes ("XX" → 400) by the request serializer
    before this runs (§7a: every 400 check comes first), so this never raises a 400.
    The boundary polygon applies to coordinate input only: city input is judged by its
    state, because a few real coastal towns sit just outside the generalised polygon.
    """

    def __init__(
        self,
        *,
        gazetteer_loader: Callable[[], Gazetteer],
        boundary_loader: Callable[[], Boundary],
    ) -> None:
        self._gazetteer_loader = gazetteer_loader
        self._boundary_loader = boundary_loader

    def resolve_city(self, *, field: EndpointField, city: str, state: str) -> ResolvedEndpoint:
        """The GeoNames point for a city; outside states and unknown names raise (FR-3, FR-4)."""
        if normalise_state(state) in OUTSIDE_SERVICE_AREA:
            raise UnsupportedLocation(field=field)
        place = self._gazetteer_loader().find(city=city, state=state)
        if place is None:
            raise LocationNotFound(
                field=field, city=city, state=state, state_code=normalise_state(state)
            )
        return ResolvedEndpoint(
            field=field,
            matched=place.matched_label,
            latitude=place.latitude,
            longitude=place.longitude,
        )

    def resolve_point(
        self, *, field: EndpointField, latitude: float, longitude: float
    ) -> ResolvedEndpoint:
        """The point itself if it lies in the lower 48 + DC polygon, else UnsupportedLocation."""
        if not self._boundary_loader().contains(latitude=latitude, longitude=longitude):
            raise UnsupportedLocation(field=field)
        return ResolvedEndpoint(field=field, matched=None, latitude=latitude, longitude=longitude)


class LazyValue[T]:
    """Builds a value on first use, once per process, even with several threads asking."""

    def __init__(self, load: Callable[[], T]) -> None:
        self._load = load
        self._value: T | None = None
        self._lock = threading.Lock()

    def get(self) -> T:
        if self._value is None:
            with self._lock:
                if self._value is None:
                    self._value = self._load()
        return self._value


# Separate values so each loads and fails on its own; `planner.warm_up` loads both at server start.
_gazetteer = LazyValue(lambda: Gazetteer.from_tsv(PLACES_PATH))
_boundary = LazyValue(lambda: Boundary.from_geojson(BOUNDARY_PATH))


def get_gazetteer() -> Gazetteer:
    """The process-wide gazetteer from `data/us_places.tsv`.

    Loaded at server start by `planner.warm_up`, else on first use.
    """
    return _gazetteer.get()


def get_boundary() -> Boundary:
    """The process-wide boundary from `data/us_lower48_dc.geojson`.

    Loaded at server start by `planner.warm_up`, else on first use.
    """
    return _boundary.get()
