"""Owner-only: build the committed offline location files from downloads (no network).

- `--geonames US.txt` → `data/us_places.tsv`, the gazetteer behind city input (FR-2).
- `--census-kml cb_2025_us_state_5m.kml` → `data/us_lower48_dc.geojson`, the boundary
  behind coordinate input (FR-4, NFR-11).
- `--placeholder-locations` → a development-only station location file that puts every
  station at its town's GeoNames point, so `import_fuel_stations --dev` can run before
  the real geocoding is finished (FR-16).

Both downloads are untrusted: they are parsed with the standard library only (csv and
expat via ElementTree, which does not fetch external entities) and every number is checked.
"""

import csv
import json
import math
import os
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser

from fuelplan.fuel_csv import CsvHeaderError, parse_row, read_records
from fuelplan.geocoding import LOCATION_FILE_COLUMNS
from fuelplan.locations import (
    MIN_RING_POINTS,
    PLACES_COLUMNS,
    Gazetteer,
    GeoJsonRings,
    LocationDataError,
    normalise_place_name,
)
from fuelplan.regions import LOWER_48_AND_DC, is_in_service_area

DATA_DIR = Path(settings.BASE_DIR) / "data"
DEFAULT_CSV = DATA_DIR / "raw" / "fuel-prices.csv"
PLACES_FILE_NAME = "us_places.tsv"
BOUNDARY_FILE_NAME = "us_lower48_dc.geojson"
# Gitignored on purpose: placeholder points are town centres, never submission data.
DEFAULT_PLACEHOLDER_PATH = DATA_DIR / "dev" / "station_locations.placeholder.csv"
# `geocode_stations --resume` would treat placeholder rows there as already geocoded.
GEOCODE_OUTPUT_PATH = DATA_DIR / "station_locations.csv"

# GeoNames dump columns (download.geonames.org/export/dump/readme.txt).
GEONAMES_COLUMN_COUNT = 19
GEONAMES_ID, NAME, ASCII_NAME, ALTERNATE_NAMES = 0, 1, 2, 3
LATITUDE, LONGITUDE, FEATURE_CLASS, FEATURE_CODE = 4, 5, 6, 7
COUNTRY, ADMIN1, POPULATION = 8, 10, 14
POPULATED_PLACE_CLASS = "P"
# Abandoned, historical, destroyed places and city sections lose ties to living towns.
LOW_PRIORITY_FEATURE_CODES = frozenset({"PPLQ", "PPLH", "PPLW", "PPLX"})
PRIMARY_TIER, ALTERNATE_TIER = 0, 1

KML_NAMESPACE = "http://www.opengis.net/kml/2.2"
COORDINATE_DECIMALS = 5

PLACEHOLDER_RESULT_TYPE = "city"  # labelled `approximate` by the import
PLACEHOLDER_ROW_TAIL = ["placeholder", "GeoNames", "CC BY 4.0", "https://www.geonames.org/"]


@dataclass(frozen=True, slots=True)
class GeoNamesPlace:
    """One kept GeoNames row: a populated place in the lower 48 + DC."""

    geonameid: int
    name: str
    ascii_name: str
    alternate_names: tuple[str, ...]
    latitude_text: str
    longitude_text: str
    feature_code: str
    state: str
    population: int

    def rank(self) -> tuple[int, int, int]:
        """Sort key among places sharing a name: bigger first, then living towns, then oldest id."""
        is_low_priority = int(self.feature_code in LOW_PRIORITY_FEATURE_CODES)
        return (-self.population, is_low_priority, self.geonameid)


@dataclass(frozen=True, slots=True)
class StateShape:
    """One served area from the Census KML, as GeoJSON-ready polygons."""

    stusps: str
    name: str
    polygons: tuple[GeoJsonRings, ...]

    @property
    def vertex_count(self) -> int:
        return sum(len(ring) for rings in self.polygons for ring in rings)


@dataclass(frozen=True, slots=True)
class CensusRead:
    """The served areas of a Census KML and how many placemarks it had in all."""

    states: tuple[StateShape, ...]
    placemark_count: int
    skipped_ring_count: int  # specks of islands that collapse below 4 points at 5 dp


def parse_geonames_line(line: str, *, line_number: int) -> GeoNamesPlace | None:
    """The place on one GeoNames line, or None if it is not a lower-48 populated place."""
    columns = line.rstrip("\r\n").split("\t")
    if len(columns) != GEONAMES_COLUMN_COUNT:
        raise LocationDataError(f"GeoNames line {line_number}: expected 19 columns")
    is_kept = (
        columns[COUNTRY] == "US"
        and columns[FEATURE_CLASS] == POPULATED_PLACE_CLASS
        and columns[ADMIN1] in LOWER_48_AND_DC
    )
    if not is_kept:
        return None
    if not _is_whole_number(columns[GEONAMES_ID]) or not _is_whole_number(columns[POPULATION]):
        raise LocationDataError(f"GeoNames line {line_number}: bad id or population")
    if not (_is_finite(columns[LATITUDE]) and _is_finite(columns[LONGITUDE])):
        raise LocationDataError(f"GeoNames line {line_number}: bad coordinates")
    return GeoNamesPlace(
        geonameid=int(columns[GEONAMES_ID]),
        name=columns[NAME],
        ascii_name=columns[ASCII_NAME],
        alternate_names=tuple(name for name in columns[ALTERNATE_NAMES].split(",") if name),
        latitude_text=columns[LATITUDE],
        longitude_text=columns[LONGITUDE],
        feature_code=columns[FEATURE_CODE],
        state=columns[ADMIN1],
        population=int(columns[POPULATION]),
    )


def kept_geonames_places(lines: Iterable[str]) -> Iterator[GeoNamesPlace]:
    """Stream the kept places of a GeoNames dump; the 300 MB file is never held in memory."""
    for line_number, line in enumerate(lines, start=1):
        place = parse_geonames_line(line, line_number=line_number)
        if place is not None:
            yield place


def place_keys(place: GeoNamesPlace) -> tuple[set[str], set[str]]:
    """The place's own name keys, and its ASCII alternate-name keys that are not own names."""
    primary = {normalise_place_name(place.name), normalise_place_name(place.ascii_name)} - {""}
    alternates = {normalise_place_name(name) for name in place.alternate_names}
    # Non-Latin alternates never match a CSV city or a typed name; keeping them only adds size.
    return primary, {key for key in alternates - primary if key and key.isascii()}


def choose_places(places: Iterable[GeoNamesPlace]) -> dict[tuple[str, str], GeoNamesPlace]:
    """One place per (name key, state): own names beat alternates, then `rank()` decides.

    Owner decision 2026-10-27: a town's own name must win over a bigger city's
    alternate name (pure "highest population" mis-resolved 46 CSV city/state pairs).
    """
    best: dict[tuple[str, str], tuple[tuple[int, int, int, int], GeoNamesPlace]] = {}
    for place in places:
        primary, alternates = place_keys(place)
        for tier, keys in ((PRIMARY_TIER, primary), (ALTERNATE_TIER, alternates)):
            for key in keys:
                order = (tier, *place.rank())
                current = best.get((key, place.state))
                if current is None or order < current[0]:
                    best[(key, place.state)] = (order, place)
    return {slot: place for slot, (_, place) in best.items()}


def places_table(chosen: dict[tuple[str, str], GeoNamesPlace]) -> list[list[str]]:
    """TSV rows sorted by (state, name key), so a rebuild is byte-identical."""
    rows = []
    for name_norm, state in sorted(chosen, key=lambda slot: (slot[1], slot[0])):
        place = chosen[(name_norm, state)]
        rows.append(
            [
                place.name,
                name_norm,
                state,
                place.latitude_text,
                place.longitude_text,
                str(place.population),
            ]
        )
    return rows


def read_census_states(root: ElementTree.Element) -> CensusRead:
    """Served areas (lower 48 + DC) from a parsed Census state KML, sorted by STUSPS."""
    states: dict[str, StateShape] = {}
    placemarks = root.iter(_kml("Placemark"))
    placemark_count = skipped_ring_count = 0
    for placemark in placemarks:
        placemark_count += 1
        attributes = {
            item.get("name"): (item.text or "").strip()
            for item in placemark.iter(_kml("SimpleData"))
        }
        stusps = attributes.get("STUSPS", "")
        if stusps not in LOWER_48_AND_DC:
            continue
        if stusps in states:
            raise LocationDataError(f"Census KML: {stusps} appears twice")
        polygons: list[GeoJsonRings] = []
        for polygon in placemark.iter(_kml("Polygon")):
            rings, skipped = _polygon_rings(polygon)
            skipped_ring_count += skipped
            if rings:
                polygons.append(rings)
        if not polygons:
            raise LocationDataError(f"Census KML: {stusps} has no polygon")
        states[stusps] = StateShape(
            stusps=stusps, name=attributes.get("NAME", ""), polygons=tuple(polygons)
        )
    return CensusRead(
        states=tuple(states[code] for code in sorted(states)),
        placemark_count=placemark_count,
        skipped_ring_count=skipped_ring_count,
    )


def _polygon_rings(polygon: ElementTree.Element) -> tuple[GeoJsonRings, int]:
    """Outer ring first, then holes, and how many rings collapsed at 5 dp and were skipped.

    If the outer ring collapses, the whole polygon (holes included) is skipped.
    """
    outer = polygon.find(f"{_kml('outerBoundaryIs')}/{_kml('LinearRing')}/{_kml('coordinates')}")
    holes = polygon.findall(f"{_kml('innerBoundaryIs')}/{_kml('LinearRing')}/{_kml('coordinates')}")
    outer_ring = parse_kml_ring(outer.text or "") if outer is not None else None
    if outer_ring is None:
        return [], 1 + len(holes)
    hole_rings = [ring for hole in holes if (ring := parse_kml_ring(hole.text or "")) is not None]
    return [outer_ring, *hole_rings], len(holes) - len(hole_rings)


def parse_kml_ring(text: str) -> list[list[float]] | None:
    """A closed ring of [lon, lat] at 5 dp from KML "lon,lat[,alt] …" text.

    Consecutive points that become equal after rounding are dropped; a ring with fewer
    than 4 points left (a speck of an island) returns None.
    """
    ring: list[list[float]] = []
    for token in text.split():
        parts = token.split(",")
        if len(parts) not in (2, 3) or not (_is_finite(parts[0]) and _is_finite(parts[1])):
            raise LocationDataError(f"Census KML: bad coordinate {token[:40]!r}")
        position = [round(float(part), COORDINATE_DECIMALS) for part in parts[:2]]
        if not ring or ring[-1] != position:
            ring.append(position)
    if ring and ring[0] != ring[-1]:
        ring.append(ring[0])
    return ring if len(ring) >= MIN_RING_POINTS else None


def boundary_document(states: Iterable[StateShape]) -> dict[str, object]:
    """The boundary as a GeoJSON FeatureCollection of MultiPolygons (RFC 7946 order)."""
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "properties": {"STUSPS": state.stusps, "NAME": state.name},
                "geometry": {"type": "MultiPolygon", "coordinates": list(state.polygons)},
            }
            for state in states
        ],
    }


def placeholder_rows(*, records: Iterable[dict[str, str]], gazetteer: Gazetteer) -> list[list[str]]:
    """One location row per unique service-area address: its town's point, marked placeholder.

    A town missing from the gazetteer gets empty coordinates, so the import counts the
    station as unresolved instead of inventing a point.
    """
    rows: dict[str, list[str]] = {}
    for record in records:
        row = parse_row(record)
        if row is None or not is_in_service_area(row.state) or row.address_key in rows:
            continue
        place = gazetteer.find(city=row.city, state=row.state)
        latitude = f"{place.latitude:.5f}" if place else ""
        longitude = f"{place.longitude:.5f}" if place else ""
        rows[row.address_key] = [
            row.address_key,
            latitude,
            longitude,
            PLACEHOLDER_RESULT_TYPE,
            "",
            row.state,
            *PLACEHOLDER_ROW_TAIL,
        ]
    return [rows[key] for key in sorted(rows)]


def write_atomically(path: Path, write: Callable[[TextIO], None]) -> None:
    """Write `path` through `<path>.tmp` and a rename, so a failure never leaves half a file."""
    temp_path = path.with_name(f"{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temp_path.open("w", newline="", encoding="utf-8") as handle:
            write(handle)
        os.replace(temp_path, path)
    except OSError as error:
        raise CommandError(f"cannot write {path}: {error}") from error
    finally:
        # After a successful rename there is no temp file left; after any failure, remove it.
        temp_path.unlink(missing_ok=True)


def _kml(tag: str) -> str:
    return f"{{{KML_NAMESPACE}}}{tag}"


def _is_whole_number(text: str) -> bool:
    return text.isascii() and text.isdecimal()


def _is_finite(text: str) -> bool:
    try:
        return math.isfinite(float(text))
    except ValueError:
        return False


def _relative(path: Path) -> Path:
    """A default path as shown in --help: relative to the project folder."""
    return path.relative_to(settings.BASE_DIR)


def _require_file(path: Path, *, what: str) -> None:
    if not path.exists():
        raise CommandError(f"{what} {path} not found.")
    if not path.is_file():
        raise CommandError(f"{what} {path} is not a file.")


def _megabytes(path: Path) -> str:
    return f"{path.stat().st_size / 1_000_000:.2f} MB"


class Command(BaseCommand):
    help = "Owner only: build the place gazetteer and US boundary files (no network)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--geonames", type=Path, help="GeoNames US.txt (from US.zip).")
        parser.add_argument(
            "--census-kml", type=Path, help="Census cb_2025_us_state_5m.kml (from its zip)."
        )
        parser.add_argument(
            "--out-dir",
            type=Path,
            default=DATA_DIR,
            help=f"Folder for the two data files (default: {_relative(DATA_DIR)}).",
        )
        parser.add_argument(
            "--placeholder-locations",
            type=Path,
            nargs="?",
            const=DEFAULT_PLACEHOLDER_PATH,
            help="Also write a dev-only station location file "
            f"(default: {_relative(DEFAULT_PLACEHOLDER_PATH)}).",
        )
        parser.add_argument(
            "--csv",
            type=Path,
            default=DEFAULT_CSV,
            help=f"Fuel-price CSV for the placeholder (default: {_relative(DEFAULT_CSV)}).",
        )

    def handle(self, *args: object, **options: object) -> None:
        geonames_path: Path | None = options["geonames"]
        kml_path: Path | None = options["census_kml"]
        placeholder_path: Path | None = options["placeholder_locations"]
        out_dir: Path = options["out_dir"]
        if not (geonames_path or kml_path or placeholder_path):
            raise CommandError(
                "Nothing to build: pass --geonames, --census-kml and/or --placeholder-locations."
            )
        if placeholder_path is not None:
            self._refuse_geocode_output(placeholder_path)
        if geonames_path is not None:
            self._build_places(geonames_path, out_dir / PLACES_FILE_NAME)
        if kml_path is not None:
            self._build_boundary(kml_path, out_dir / BOUNDARY_FILE_NAME)
        if placeholder_path is not None:
            self._build_placeholder(
                csv_path=options["csv"],
                places_path=out_dir / PLACES_FILE_NAME,
                out_path=placeholder_path,
            )

    def _refuse_geocode_output(self, path: Path) -> None:
        if path.resolve() == GEOCODE_OUTPUT_PATH.resolve():
            raise CommandError(
                f"Refusing to write placeholder rows to {GEOCODE_OUTPUT_PATH}: geocode_stations "
                "--resume would treat them as geocoded. Use the default --placeholder-locations."
            )

    def _build_places(self, geonames_path: Path, out_path: Path) -> None:
        _require_file(geonames_path, what="GeoNames file")
        try:
            with geonames_path.open(encoding="utf-8", newline="") as handle:
                chosen = choose_places(kept_geonames_places(handle))
        except (OSError, UnicodeDecodeError, LocationDataError) as error:
            raise CommandError(f"cannot read GeoNames file {geonames_path}: {error}") from error
        rows = places_table(chosen)

        def write(handle: TextIO) -> None:
            writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
            writer.writerow(PLACES_COLUMNS)
            writer.writerows(rows)

        write_atomically(out_path, write)
        file_date = datetime.fromtimestamp(geonames_path.stat().st_mtime, tz=UTC).date()
        place_count = len({place.geonameid for place in chosen.values()})
        self.stdout.write(
            f"GeoNames ({file_date.isoformat()} snapshot): {place_count:,} populated places "
            f"used, {len(rows):,} name keys -> "
            f"{out_path} ({_megabytes(out_path)})"
        )

    def _build_boundary(self, kml_path: Path, out_path: Path) -> None:
        _require_file(kml_path, what="Census KML")
        try:
            census = read_census_states(ElementTree.parse(kml_path).getroot())
        except (OSError, ElementTree.ParseError, LocationDataError) as error:
            raise CommandError(f"cannot read Census KML {kml_path}: {error}") from error
        found = {state.stusps for state in census.states}
        if found != LOWER_48_AND_DC:
            raise CommandError(
                f"{kml_path} must hold the 48 states + DC (49 areas); found {len(found)}. "
                "Use the Census cb_2025_us_state_5m KML."
            )
        document = boundary_document(census.states)
        write_atomically(
            out_path, lambda handle: handle.write(json.dumps(document, separators=(",", ":")))
        )
        vertices = sum(state.vertex_count for state in census.states)
        self.stdout.write(
            f"Census KML: {census.placemark_count} areas read, {len(census.states)} kept "
            f"(48 states + DC), {vertices:,} vertices, {census.skipped_ring_count} tiny rings "
            f"skipped -> {out_path} ({_megabytes(out_path)})"
        )

    def _build_placeholder(self, *, csv_path: Path, places_path: Path, out_path: Path) -> None:
        _require_file(csv_path, what="Fuel-price CSV")
        try:
            gazetteer = Gazetteer.from_tsv(places_path)
            rows = placeholder_rows(records=read_records(csv_path), gazetteer=gazetteer)
        except LocationDataError as error:
            raise CommandError(f"{error} (build it first with --geonames)") from error
        except CsvHeaderError as error:
            raise CommandError(str(error)) from error
        except (OSError, UnicodeDecodeError, csv.Error) as error:
            raise CommandError(f"cannot read the fuel-price CSV {csv_path}: {error}") from error

        def write(handle: TextIO) -> None:
            writer = csv.writer(handle, lineterminator="\n")
            writer.writerow(LOCATION_FILE_COLUMNS)
            writer.writerows(rows)

        write_atomically(out_path, write)
        without_point = sum(1 for row in rows if not row[1])
        self.stdout.write(
            f"Placeholder locations (development only): {len(rows):,} addresses, "
            f"{without_point:,} without a town point -> {out_path}\n"
            f"Import them with: python manage.py import_fuel_stations --locations {out_path} --dev"
        )
