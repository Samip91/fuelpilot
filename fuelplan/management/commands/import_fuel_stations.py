"""Load the provided fuel CSV and the station locations into the Station table.

Spec 01 FR-13 to FR-17, NFR-12, invariant I5. No API key and no network: the reviewer
runs this. Rows go through the FR-13 steps in order, each one counted:

1. skip invalid rows (bad ID or price, FR-14);
2. drop rows outside the lower 48 + DC (Canada, AK/HI/territories, blank codes);
3. join each row to its location: no row → `location_missing`, unusable → `unresolved`;
4. merge rows of one OPIS ID, keeping the lowest price (first row on a tie).

Every check runs before the database is touched; the table is then replaced in one
transaction, so a failed import leaves the previous data in place.
"""

import csv
import hashlib
import io
import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import DatabaseError, transaction

from fuelplan.fuel_csv import CsvHeaderError, FuelRow, parse_records, parse_row
from fuelplan.geo import KM_PER_MILE, haversine_miles
from fuelplan.geocoding import LocationFileError, parse_location_rows
from fuelplan.locations import Gazetteer, LocationDataError, Place
from fuelplan.models import DatasetImport, LocationQuality, LocationsSource, Station
from fuelplan.regions import is_in_service_area, is_inside_lower_48_bbox

DATA_DIR = Path(settings.BASE_DIR) / "data"
DEFAULT_CSV = DATA_DIR / "raw" / "fuel-prices.csv"
DEFAULT_LOCATIONS = DATA_DIR / "station_locations.csv"
DEFAULT_PLACES = DATA_DIR / "us_places.tsv"

# X (Q-11): set by the owner on 2026-10-27 from the 50-address Geoapify sample.
CONFIDENCE_THRESHOLD = Decimal("0.9")
FAR_FROM_CITY_KM = Decimal("25.00")
DISTANCE_STEP_KM = Decimal("0.01")
# Part of `dataset_version`: changing a labelling rule gives a new version (and plan keys).
IMPORT_RULES_VERSION = (
    f"labels-1;X={CONFIDENCE_THRESHOLD};far_km={FAR_FROM_CITY_KM};"
    f"dist={DISTANCE_STEP_KM}km-half-up;bad-point=unresolved"
)

POI_RESULT_TYPES = frozenset({"amenity"})
ADDRESS_RESULT_TYPES = frozenset({"building", "street"})
PRECISE_LABELS = frozenset({LocationQuality.POI_MATCH, LocationQuality.ADDRESS_MATCH})
KNOWN_SOURCES = frozenset(LocationsSource.values)
BULK_BATCH_SIZE = 1_000
# Printed in FR-13 order, which is also the order rows are filtered in.
COUNT_LABELS = (
    ("read", "Rows read"),
    ("invalid_skipped", "Invalid rows skipped"),
    ("non_us_dropped", "Non-US rows dropped"),
    ("unresolved_excluded", "Unresolved locations"),
    ("location_missing", "Location missing"),
    ("duplicates_merged", "Duplicate IDs merged"),
    ("imported", "Stations imported"),
)


class ImportCountMismatch(Exception):
    """The counters do not add up to the rows read: a bug in the import, never bad data."""


@dataclass(frozen=True, slots=True)
class RawLocation:
    """The raw fields of one location-file row that labelling needs (all CSV text)."""

    address_key: str
    lat: str
    lon: str
    result_type: str
    confidence: str
    state_code: str
    source: str


@dataclass(frozen=True, slots=True)
class LabelledPoint:
    """A usable station point and its FR-17 location label."""

    latitude: float
    longitude: float
    quality: LocationQuality


@dataclass(frozen=True, slots=True)
class ImportedStation:
    """One station as it will be stored: the cheapest kept row of its OPIS ID."""

    id: int
    name: str
    address: str
    city: str
    state: str
    price_text: str
    price_variants: tuple[str, ...]
    address_key: str
    latitude: float
    longitude: float
    location_quality: LocationQuality


@dataclass(frozen=True, slots=True)
class ImportCounts:
    """Rows per FR-13 step; every row read lands in exactly one counter except merges."""

    read: int
    invalid_skipped: int
    non_us_dropped: int
    unresolved_excluded: int
    location_missing: int
    duplicates_merged: int
    imported: int

    def sums_to_read(self) -> bool:
        outcomes = (
            self.invalid_skipped
            + self.non_us_dropped
            + self.unresolved_excluded
            + self.location_missing
            + self.duplicates_merged
            + self.imported
        )
        return outcomes == self.read


@dataclass(frozen=True, slots=True)
class ImportResult:
    stations: tuple[ImportedStation, ...]
    counts: ImportCounts


@dataclass(frozen=True, slots=True)
class _KeptRow:
    """A row that passed steps 1 to 3, waiting for the merge by OPIS ID."""

    row: FuelRow
    point: LabelledPoint


def label_location(
    *, raw: RawLocation, csv_state: str, city_place: Place | None
) -> LabelledPoint | None:
    """The FR-17 label for a raw geocoding result, or None when it is unresolved.

    There is no city-centre fallback: an unusable point excludes the station.
    """
    point = _parse_point(raw.lat, raw.lon)
    if point is None or raw.state_code.strip().upper() != csv_state:
        return None
    latitude, longitude = point
    quality = _label_for_result_type(raw.result_type.strip())
    if quality in PRECISE_LABELS and not _is_trusted_precise_match(
        latitude=latitude, longitude=longitude, raw=raw, city_place=city_place
    ):
        quality = LocationQuality.APPROXIMATE
    return LabelledPoint(latitude=latitude, longitude=longitude, quality=quality)


def distance_to_place_km(*, latitude: float, longitude: float, place: Place) -> Decimal:
    """Straight-line km to a place, rounded to 0.01 km HALF_UP so 25.00 is a clean boundary."""
    miles = haversine_miles(
        lat1=latitude, lon1=longitude, lat2=place.latitude, lon2=place.longitude
    )
    return Decimal(repr(miles * KM_PER_MILE)).quantize(DISTANCE_STEP_KM, rounding=ROUND_HALF_UP)


def _parse_point(lat_text: str, lon_text: str) -> tuple[float, float] | None:
    try:
        latitude, longitude = float(lat_text), float(lon_text)
    except ValueError:
        return None
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return None
    if not is_inside_lower_48_bbox(latitude=latitude, longitude=longitude):
        return None
    return latitude, longitude


def _label_for_result_type(result_type: str) -> LocationQuality:
    if result_type in POI_RESULT_TYPES:
        return LocationQuality.POI_MATCH
    if result_type in ADDRESS_RESULT_TYPES:
        return LocationQuality.ADDRESS_MATCH
    return LocationQuality.APPROXIMATE


def _is_trusted_precise_match(
    *, latitude: float, longitude: float, raw: RawLocation, city_place: Place | None
) -> bool:
    """Near the CSV town, or far (or town unknown) but with confidence of at least X."""
    if city_place is not None:
        distance_km = distance_to_place_km(latitude=latitude, longitude=longitude, place=city_place)
        if distance_km <= FAR_FROM_CITY_KM:
            return True
    confidence = _parse_confidence(raw.confidence)
    return confidence is not None and confidence >= CONFIDENCE_THRESHOLD


def _parse_confidence(text: str) -> Decimal | None:
    """Confidence as a Decimal; blank or unparseable counts as below X (owner 2026-10-27)."""
    try:
        confidence = Decimal(text.strip())
    except InvalidOperation:
        return None
    return confidence if confidence.is_finite() else None


def select_stations(
    *,
    records: Iterable[dict[str, str]],
    locations: Mapping[str, RawLocation],
    gazetteer: Gazetteer,
) -> ImportResult:
    """Apply the FR-13 steps in order and return the stations to store with the counters."""
    read = invalid = non_us = unresolved = missing = 0
    groups: dict[int, list[_KeptRow]] = {}
    for record in records:
        read += 1
        row = parse_row(record)
        if row is None:
            invalid += 1
            continue
        if not is_in_service_area(row.state):
            non_us += 1
            continue
        raw = locations.get(row.address_key)
        if raw is None:
            missing += 1
            continue
        city_place = gazetteer.find(city=row.city, state=row.state)
        point = label_location(raw=raw, csv_state=row.state, city_place=city_place)
        if point is None:
            unresolved += 1
            continue
        groups.setdefault(row.opis_id, []).append(_KeptRow(row=row, point=point))

    stations = tuple(_merge_by_id(group) for group in groups.values())
    counts = ImportCounts(
        read=read,
        invalid_skipped=invalid,
        non_us_dropped=non_us,
        unresolved_excluded=unresolved,
        location_missing=missing,
        duplicates_merged=sum(len(group) for group in groups.values()) - len(groups),
        imported=len(stations),
    )
    if not counts.sums_to_read():
        raise ImportCountMismatch(f"import counters do not add up: {counts}")
    return ImportResult(stations=stations, counts=counts)


def _merge_by_id(group: list[_KeptRow]) -> ImportedStation:
    """The cheapest row of one ID (min keeps the first on a tie) plus its other prices."""
    kept = min(group, key=lambda item: item.row.price)
    other_prices: dict[Decimal, str] = {}
    for item in group:
        if item.row.price != kept.row.price:
            other_prices.setdefault(item.row.price, item.row.price_text)
    row, point = kept.row, kept.point
    return ImportedStation(
        id=row.opis_id,
        name=row.name,
        address=row.address,
        city=row.city,
        state=row.state,
        price_text=row.price_text,
        price_variants=tuple(other_prices[price] for price in sorted(other_prices)),
        address_key=row.address_key,
        latitude=point.latitude,
        longitude=point.longitude,
        location_quality=point.quality,
    )


@dataclass(frozen=True, slots=True)
class ImportInputs:
    """The bytes of the three input files, read once: parsed and hashed from the same copy.

    The location file may still be growing (an owner's geocoding run appends to it), so
    reading it twice could pair rows from one version with the hash of another.
    """

    csv_path: Path
    csv_bytes: bytes
    locations_path: Path
    locations_bytes: bytes
    places_path: Path
    places_bytes: bytes


def read_input(path: Path) -> bytes:
    """The whole content of one input file."""
    return path.read_bytes()


def dataset_version(inputs: ImportInputs) -> str:
    """ "sha256:" over the three input files and the labelling rules (spec 01 §6)."""
    digest = hashlib.sha256(b"fuelpilot-import\0")
    parts = (
        ("csv", inputs.csv_bytes),
        ("locations", inputs.locations_bytes),
        ("places", inputs.places_bytes),
    )
    for label, content in parts:
        digest.update(f"{label}\0{hashlib.sha256(content).hexdigest()}\0".encode())
    digest.update(f"rules\0{IMPORT_RULES_VERSION}".encode())
    return f"sha256:{digest.hexdigest()}"


def _text_stream(content: bytes, *, encoding: str = "utf-8") -> io.StringIO:
    """Decoded file content as a stream the csv module can read."""
    return io.StringIO(content.decode(encoding), newline="")


def index_locations(rows: Iterable[dict[str, str]], *, path: Path) -> dict[str, RawLocation]:
    """Location rows by address key; a repeated key or an unknown source is a corrupt file."""
    locations: dict[str, RawLocation] = {}
    for row in rows:
        key = row["address_key"]
        if key in locations:
            raise CommandError(
                f"{path} has the address_key {key!r} twice; keep one row per address "
                "and run the import again."
            )
        if row["source"] not in KNOWN_SOURCES:
            raise CommandError(
                f"{path} has an unknown source {row['source']!r} for {key!r}; expected "
                f"{' or '.join(sorted(KNOWN_SOURCES))}."
            )
        locations[key] = RawLocation(
            address_key=key,
            lat=row["lat"],
            lon=row["lon"],
            result_type=row["result_type"],
            confidence=row["confidence"],
            state_code=row["state_code"],
            source=row["source"],
        )
    return locations


def locations_source(locations: Mapping[str, RawLocation]) -> LocationsSource:
    """`placeholder` if any row is a placeholder (a mixed file is never real data)."""
    if any(raw.source == LocationsSource.PLACEHOLDER for raw in locations.values()):
        return LocationsSource.PLACEHOLDER
    return LocationsSource.GEOAPIFY


def replace_stations(
    *,
    result: ImportResult,
    version: str,
    source: LocationsSource,
    is_dev_mode: bool,
) -> None:
    """Replace the whole Station table and record the import, all in one transaction."""
    stations = [
        Station(**{**asdict(station), "price_variants": list(station.price_variants)})
        for station in result.stations
    ]
    with transaction.atomic():
        Station.objects.all().delete()
        Station.objects.bulk_create(stations, batch_size=BULK_BATCH_SIZE)
        # Delete and recreate, so `imported_at` is fresh even when a version comes back.
        DatasetImport.objects.filter(dataset_version=version).delete()
        DatasetImport.objects.create(
            dataset_version=version,
            locations_source=source,
            dev_mode=is_dev_mode,
            counts=asdict(result.counts),
        )


def _relative(path: Path) -> Path:
    """A default path as shown in --help: relative to the project folder."""
    return path.relative_to(settings.BASE_DIR)


class Command(BaseCommand):
    help = "Import the fuel CSV and station locations into the database (no key, no network)."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--csv",
            type=Path,
            default=DEFAULT_CSV,
            help=f"Fuel-price CSV (default: {_relative(DEFAULT_CSV)}).",
        )
        parser.add_argument(
            "--locations",
            type=Path,
            default=DEFAULT_LOCATIONS,
            help=f"Station location file (default: {_relative(DEFAULT_LOCATIONS)}).",
        )
        parser.add_argument(
            "--places",
            type=Path,
            default=DEFAULT_PLACES,
            help=f"Place gazetteer (default: {_relative(DEFAULT_PLACES)}).",
        )
        parser.add_argument(
            "--dev",
            action="store_true",
            help="Allow a placeholder location file (development only).",
        )

    def handle(self, *args: object, **options: object) -> None:
        is_dev_mode = bool(options["dev"])
        inputs = self._read_inputs(
            csv_path=options["csv"],
            locations_path=options["locations"],
            places_path=options["places"],
        )
        locations = self._parse_locations(inputs)
        source = locations_source(locations)
        if source == LocationsSource.PLACEHOLDER and not is_dev_mode:
            raise CommandError(
                f"{inputs.locations_path} contains placeholder locations; pass --dev to import "
                "them for development, or use the real geocoded file."
            )
        result = self._select(inputs, locations=locations)
        if result.counts.imported == 0:
            raise CommandError(
                f"No station could be imported from {inputs.csv_path} with "
                f"{inputs.locations_path}; the table was left unchanged. Check that the "
                "location file matches the CSV."
            )
        version = dataset_version(inputs)

        if is_dev_mode:
            self._warn_dev_mode(source)
        try:
            replace_stations(result=result, version=version, source=source, is_dev_mode=is_dev_mode)
        except DatabaseError as error:
            raise CommandError(
                f"Writing the stations failed, so the previous data was kept: {error}. "
                "Run python manage.py migrate, or stop the server and retry, then import again."
            ) from error
        self._report(result=result, version=version, source=source, inputs=inputs)

    def _read_inputs(
        self, *, csv_path: Path, locations_path: Path, places_path: Path
    ) -> ImportInputs:
        """Check and read each input once, before anything is parsed or written."""
        fixes = (
            (csv_path, "pass --csv with the provided fuel-price CSV"),
            (
                locations_path,
                "run geocode_stations first, or for development build a placeholder with "
                "build_location_data --placeholder-locations and import it with --dev",
            ),
            (places_path, "build it with build_location_data --geonames"),
        )
        contents: list[bytes] = []
        for path, fix in fixes:
            if not path.exists():
                raise CommandError(f"{path} not found: {fix}.")
            if not path.is_file():
                raise CommandError(f"{path} is not a file: {fix}.")
            try:
                contents.append(read_input(path))
            except OSError as error:
                raise CommandError(f"cannot read {path}: {error}") from error
        csv_bytes, locations_bytes, places_bytes = contents
        return ImportInputs(
            csv_path=csv_path,
            csv_bytes=csv_bytes,
            locations_path=locations_path,
            locations_bytes=locations_bytes,
            places_path=places_path,
            places_bytes=places_bytes,
        )

    def _parse_locations(self, inputs: ImportInputs) -> dict[str, RawLocation]:
        path = inputs.locations_path
        try:
            rows = parse_location_rows(inputs.locations_bytes.decode("utf-8"), path=path)
        except (LocationFileError, csv.Error) as error:
            raise CommandError(
                f"{path} is malformed ({error}); if geocode_stations is still running, wait "
                "for it to finish; otherwise rerun geocode_stations --resume."
            ) from error
        except UnicodeDecodeError as error:
            raise CommandError(f"cannot read the location file {path}: {error}") from error
        return index_locations(rows, path=path)

    def _select(
        self, inputs: ImportInputs, *, locations: Mapping[str, RawLocation]
    ) -> ImportResult:
        try:
            # Its own instance: the request path's cached gazetteer is never touched here.
            gazetteer = Gazetteer.from_lines(
                _text_stream(inputs.places_bytes), path=inputs.places_path
            )
        except (LocationDataError, UnicodeDecodeError) as error:
            raise CommandError(f"{error}; rebuild it with build_location_data.") from error
        csv_path = inputs.csv_path
        try:
            records = parse_records(
                _text_stream(inputs.csv_bytes, encoding="utf-8-sig"), path=csv_path
            )
            return select_stations(records=records, locations=locations, gazetteer=gazetteer)
        except CsvHeaderError as error:
            raise CommandError(f"{error}; pass the provided fuel-price CSV.") from error
        except (csv.Error, UnicodeDecodeError) as error:
            raise CommandError(f"cannot read the fuel-price CSV {csv_path}: {error}") from error

    def _warn_dev_mode(self, source: LocationsSource) -> None:
        if source == LocationsSource.PLACEHOLDER:
            self.stderr.write(
                "WARNING: development mode: placeholder locations are town centres, not "
                "station positions. Never use this data for real plans."
            )
        else:
            self.stderr.write("WARNING: development mode: this import is marked dev_mode.")

    def _report(
        self,
        *,
        result: ImportResult,
        version: str,
        source: LocationsSource,
        inputs: ImportInputs,
    ) -> None:
        counts = asdict(result.counts)
        width = max(len(label) for _, label in COUNT_LABELS)
        for key, label in COUNT_LABELS:
            self.stdout.write(f"{label + ':':<{width + 1}} {counts[key]:>6,}")
        self.stdout.write(f"Dataset version: {version}")
        self.stdout.write(f"Locations source: {source.value} ({inputs.locations_path})")
        self.stdout.write(f"Confidence threshold X: {CONFIDENCE_THRESHOLD}")
        if result.counts.location_missing:
            self.stdout.write(
                "Hint: CSV rows with no entry in the location file yet: "
                f"{result.counts.location_missing:,}. {self._missing_location_fix(source)}"
            )
        self.stdout.write(
            "Restart the server (python manage.py runserver) so it loads this dataset."
        )

    def _missing_location_fix(self, source: LocationsSource) -> str:
        if source == LocationsSource.PLACEHOLDER:
            return "Rebuild it with build_location_data --placeholder-locations."
        return (
            "Finish geocode_stations --resume (owner only, needs the Geoapify key), "
            "then import again."
        )
