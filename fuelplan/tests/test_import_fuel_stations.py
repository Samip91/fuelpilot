"""Spec 01 TC-11 (FR-13 to FR-15, NFR-12) and TC-12 (FR-16): `import_fuel_stations`."""

import csv
import re
import shutil
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from fuelplan.fuel_csv import EXPECTED_HEADER
from fuelplan.geocoding import LOCATION_FILE_COLUMNS
from fuelplan.management.commands import import_fuel_stations
from fuelplan.management.commands.import_fuel_stations import ImportInputs
from fuelplan.models import DatasetImport, Station

from .base import OfflineTestCase

FIXTURES = Path(__file__).resolve().parent / "fixtures"
DIRTY_CSV = FIXTURES / "dirty.csv"
DIRTY_LOCATIONS = FIXTURES / "dirty_locations.csv"
PLACEHOLDER_LOCATIONS = FIXTURES / "placeholder_locations.csv"
PLACES = FIXTURES / "us_places_test.tsv"
DIRTY_COUNTS = {
    "read": 23,
    "invalid_skipped": 7,
    "non_us_dropped": 4,
    "unresolved_excluded": 2,
    "location_missing": 1,
    "duplicates_merged": 2,
    "imported": 7,
}
# (id, price_text, price_variants, name, state, location_quality, latitude, longitude)
DIRTY_STATIONS = [
    (101, "3.100", [], "PLAIN POI", "IL", "poi_match", 39.81, -89.65),
    (102, "3.200", [], "PLAIN BUILDING", "OH", "address_match", 39.97, -83.0),
    (103, "3.300", [], "PLAIN STREET", "OH", "address_match", 39.97, -83.0),
    (104, "99.999", [], "PLAIN TOP PRICE", "IL", "approximate", 39.81, -89.65),
    # The kept row's own point (2 DUP RD), not the first or last row's.
    (200, "3.199", ["3.299"], "DUP SECOND", "IL", "address_match", 39.82, -89.66),
    (400, "3.500", [], "MIXED US", "OH", "address_match", 39.97, -83.0),
    (600, "3.400", [], "Z DEAR RESOLVED", "OH", "address_match", 39.97, -83.0),
]
STATION_FIELDS = (
    "id",
    "price_text",
    "price_variants",
    "name",
    "state",
    "location_quality",
    "latitude",
    "longitude",
)


def _read_csv_rows(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.reader(handle))


def _write_csv(path: Path, rows: list[list[str]], *, header: list[str] = EXPECTED_HEADER) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def _inputs(*, csv_path: Path, locations_path: Path, places_path: Path) -> ImportInputs:
    return ImportInputs(
        csv_path=csv_path,
        csv_bytes=csv_path.read_bytes(),
        locations_path=locations_path,
        locations_bytes=locations_path.read_bytes(),
        places_path=places_path,
        places_bytes=places_path.read_bytes(),
    )


class ImportTestCase(OfflineTestCase):
    """Runs the command on fixture files; every test gets its own scratch folder."""

    def setUp(self) -> None:
        super().setUp()
        self.workdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.workdir)

    def run_import(
        self,
        *,
        csv_path: Path = DIRTY_CSV,
        locations: Path = DIRTY_LOCATIONS,
        places: Path = PLACES,
        dev: bool = False,
    ) -> tuple[str, str]:
        stdout, stderr = StringIO(), StringIO()
        args = ["--csv", str(csv_path), "--locations", str(locations), "--places", str(places)]
        call_command(
            "import_fuel_stations", *args, *(["--dev"] if dev else []), stdout=stdout, stderr=stderr
        )
        return stdout.getvalue(), stderr.getvalue()

    def station_values(self) -> list[tuple]:
        return list(Station.objects.order_by("id").values_list(*STATION_FIELDS))

    def table_snapshot(self) -> tuple[list[dict], list[tuple]]:
        stations = list(Station.objects.order_by("id").values())
        imports = list(DatasetImport.objects.values_list("dataset_version", "dev_mode", "counts"))
        return stations, imports

    def copy_with(self, source: Path, name: str, edit: str = "") -> Path:
        """A copy of a fixture with `edit` appended, so its digest changes."""
        path = self.workdir / name
        path.write_bytes(source.read_bytes() + edit.encode())
        return path


class DirtyCsvImportTests(ImportTestCase):
    """TC-11: the FR-13 steps run in order and every counter is exact."""

    def test_dirty_csv_counters_are_exact(self) -> None:
        stdout, _ = self.run_import()

        record = DatasetImport.objects.get()
        self.assertEqual(record.counts, DIRTY_COUNTS)
        self.assertEqual(record.locations_source, "geoapify")
        self.assertFalse(record.dev_mode)
        expected_lines = [
            "Rows read:                23",
            "Invalid rows skipped:      7",
            "Non-US rows dropped:       4",
            "Unresolved locations:      2",
            "Location missing:          1",
            "Duplicate IDs merged:      2",
            "Stations imported:         7",
            f"Dataset version: {record.dataset_version}",
        ]
        for line in expected_lines:
            self.assertIn(line, stdout)

    def test_dirty_csv_imports_exactly_these_stations(self) -> None:
        self.run_import()

        self.assertEqual(
            [list(row) for row in self.station_values()], [list(row) for row in DIRTY_STATIONS]
        )
        self.assertEqual(Station.objects.get(id=200).address, "2 DUP RD")

    def test_a_canadian_row_never_displaces_a_us_row_under_the_same_id(self) -> None:
        self.run_import()

        station = Station.objects.get(id=400)
        self.assertEqual(
            (station.price_text, station.state, station.price_variants), ("3.500", "OH", [])
        )

    def test_an_unresolved_cheaper_row_never_displaces_a_resolved_row(self) -> None:
        self.run_import()

        station = Station.objects.get(id=600)
        self.assertEqual((station.price_text, station.price_variants), ("3.400", []))

    def test_a_blank_or_unknown_csv_state_is_dropped_as_non_us(self) -> None:
        rows = _read_csv_rows(DIRTY_CSV)[1:2]  # row 101, a resolvable US station
        rows += [["700", "BLANK", "1 A ST", "Columbus", "", "1", "3.0"]]
        rows += [["701", "UNKNOWN", "2 A ST", "Columbus", "ZZ", "1", "3.0"]]
        csv_path = _write_csv(self.workdir / "states.csv", rows)

        self.run_import(csv_path=csv_path)

        counts = DatasetImport.objects.get().counts
        self.assertEqual((counts["non_us_dropped"], counts["imported"]), (2, 1))

    def test_price_variants_are_distinct_values_in_ascending_order(self) -> None:
        rows = [
            ["800", "V1", "100 MAIN ST", "Springfield", "IL", "1", "3.50"],
            ["800", "V2", "100 MAIN ST", "Springfield", "IL", "1", "3.10"],
            ["800", "V3", "100 MAIN ST", "Springfield", "IL", "1", "3.5"],
            ["800", "V4", "100 MAIN ST", "Springfield", "IL", "1", "3.20"],
            ["800", "V5", "100 MAIN ST", "Springfield", "IL", "1", "3.100"],
        ]
        csv_path = _write_csv(self.workdir / "variants.csv", rows)

        self.run_import(csv_path=csv_path)

        station = Station.objects.get(id=800)
        self.assertEqual((station.name, station.price_text), ("V2", "3.10"))
        self.assertEqual(station.price_variants, ["3.20", "3.50"])

    def test_every_imported_id_and_price_appear_in_the_csv(self) -> None:
        self.run_import()

        csv_prices = {(row[0], row[6]) for row in _read_csv_rows(DIRTY_CSV)[1:]}
        for station in Station.objects.all():
            for price_text in [station.price_text, *station.price_variants]:
                with self.subTest(station=station.id, price=price_text):
                    self.assertIn((str(station.id), price_text), csv_prices)

    def test_the_output_ends_with_the_restart_reminder_and_the_missing_location_hint(self) -> None:
        stdout, stderr = self.run_import()

        self.assertIn("Locations source: geoapify", stdout)
        self.assertIn("Confidence threshold X: 0.9\n", stdout)
        self.assertNotIn("provisional", stdout)
        self.assertIn(
            "Hint: CSV rows with no entry in the location file yet: 1. Finish geocode_stations "
            "--resume (owner only, needs the Geoapify key), then import again.",
            stdout,
        )
        self.assertTrue(stdout.rstrip().endswith("so it loads this dataset."))
        self.assertEqual(stderr, "")


class ImportRunTests(ImportTestCase):
    """FR-15: idempotent, whole-table replacement, atomic, versioned, offline."""

    def test_reimporting_the_same_files_is_idempotent(self) -> None:
        self.run_import()
        first = self.table_snapshot()

        self.run_import()

        self.assertEqual(self.table_snapshot(), first)
        self.assertEqual(DatasetImport.objects.count(), 1)

    def test_reimport_replaces_the_whole_table(self) -> None:
        self.run_import()
        rows = [row for row in _read_csv_rows(DIRTY_CSV)[1:] if row[0] != "104"]
        csv_path = _write_csv(self.workdir / "without_104.csv", rows)

        self.run_import(csv_path=csv_path)

        self.assertFalse(Station.objects.filter(id=104).exists())
        self.assertEqual(Station.objects.count(), 6)
        self.assertEqual(DatasetImport.objects.count(), 2)

    def test_reimporting_an_earlier_dataset_makes_it_the_newest_record(self) -> None:
        rows = [row for row in _read_csv_rows(DIRTY_CSV)[1:] if row[0] != "104"]
        without_104 = _write_csv(self.workdir / "without_104.csv", rows)
        self.run_import()
        version_a = DatasetImport.objects.get().dataset_version
        self.run_import(csv_path=without_104)

        self.run_import()

        self.assertEqual(DatasetImport.objects.count(), 2)
        self.assertEqual(DatasetImport.objects.first().dataset_version, version_a)

    def test_the_version_matches_the_content_that_was_parsed_even_if_a_file_grows(self) -> None:
        locations = self.copy_with(DIRTY_LOCATIONS, "growing.csv")
        original = _inputs(csv_path=DIRTY_CSV, locations_path=locations, places_path=PLACES)
        # Station 106's real key: re-reading the file after this append would import it.
        extra_row = (
            "500 BIRCH LN|COLUMBUS|OH,39.97000,-83.00000,building,0.95,OH,geoapify,"
            "© OpenStreetMap contributors,Open Database License,"
            "https://www.openstreetmap.org/copyright\n"
        )
        real_read = import_fuel_stations.read_input

        def read_then_append(path: Path) -> bytes:
            content = real_read(path)
            if path == locations:  # an owner's geocoding run appends right after our read
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(extra_row)
            return content

        with mock.patch.object(import_fuel_stations, "read_input", read_then_append):
            self.run_import(locations=locations)

        grown = _inputs(csv_path=DIRTY_CSV, locations_path=locations, places_path=PLACES)
        record = DatasetImport.objects.get()
        self.assertIn(extra_row, locations.read_text(encoding="utf-8"))
        self.assertEqual(record.dataset_version, import_fuel_stations.dataset_version(original))
        self.assertNotEqual(record.dataset_version, import_fuel_stations.dataset_version(grown))
        self.assertEqual(record.counts, DIRTY_COUNTS)

    def test_a_failure_during_the_write_leaves_the_previous_table_unchanged(self) -> None:
        self.run_import()
        before = self.table_snapshot()
        csv_path = self.copy_with(DIRTY_CSV, "changed.csv", "900,NEW,1 B ST,Columbus,OH,1,3.0\n")

        with (
            mock.patch.object(Station.objects, "bulk_create", side_effect=DatabaseError("disk")),
            self.assertRaisesMessage(CommandError, "previous data was kept"),
        ):
            self.run_import(csv_path=csv_path)

        self.assertEqual(self.table_snapshot(), before)

    def test_dataset_version_changes_with_any_input(self) -> None:
        def version(
            csv_path: Path = DIRTY_CSV,
            locations_path: Path = DIRTY_LOCATIONS,
            places_path: Path = PLACES,
        ) -> str:
            return import_fuel_stations.dataset_version(
                _inputs(csv_path=csv_path, locations_path=locations_path, places_path=places_path)
            )

        base = version()
        changed = {
            "csv": version(csv_path=self.copy_with(DIRTY_CSV, "a.csv", "\n")),
            "locations": version(locations_path=self.copy_with(DIRTY_LOCATIONS, "b.csv", "\n")),
            "places": version(places_path=self.copy_with(PLACES, "c.tsv", "\n")),
        }
        with mock.patch.object(import_fuel_stations, "IMPORT_RULES_VERSION", "labels-2"):
            changed["rules"] = version()

        self.assertRegex(base, r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(version(), base)
        for name, other in changed.items():
            with self.subTest(changed=name):
                self.assertNotEqual(other, base)

    @override_settings(GEOAPIFY_API_KEY="")
    def test_import_needs_no_api_key_and_no_network(self) -> None:
        # OfflineTestCase fails any socket connect for the whole test.
        self.run_import()

        self.assertEqual(Station.objects.count(), 7)

    def test_import_query_count_does_not_grow_with_the_number_of_rows(self) -> None:
        rows = _read_csv_rows(DIRTY_CSV)[1:]
        tenfold = [
            [str(int(row[0]) + copy * 10_000) if row[0].isdecimal() else row[0], *row[1:]]
            for copy in range(10)
            for row in rows
        ]
        big_csv = _write_csv(self.workdir / "tenfold.csv", tenfold)

        with CaptureQueriesContext(connection) as small:
            self.run_import()
        with CaptureQueriesContext(connection) as big:
            self.run_import(csv_path=big_csv)

        self.assertEqual(Station.objects.count(), 70)
        self.assertEqual(len(big.captured_queries), len(small.captured_queries))
        self.assertLessEqual(len(big.captured_queries), 10)


class ImportRefusalTests(ImportTestCase):
    """Exit 1 (CommandError) with the table unchanged on any bad input."""

    def setUp(self) -> None:
        super().setUp()
        self.run_import()
        self.before = self.table_snapshot()

    def assert_refused(self, message: str, **files: Path) -> None:
        with self.assertRaisesMessage(CommandError, message):
            self.run_import(**files)
        self.assertEqual(self.table_snapshot(), self.before)

    def test_a_wrong_header_stops_the_import_and_keeps_the_table(self) -> None:
        self.assert_refused("unexpected header", csv_path=FIXTURES / "wrong_header.csv")

    def test_a_location_file_with_the_wrong_header_stops_the_import(self) -> None:
        path = self.workdir / "locations.csv"
        path.write_text("address_key,lat,lon\nA|B|KS,38.0,-98.0\n", encoding="utf-8")

        self.assert_refused("does not have the expected header", locations=path)

    def test_a_missing_file_stops_the_import_naming_it_and_the_fix(self) -> None:
        missing = self.workdir / "missing.csv"
        cases = {
            "csv_path": "pass --csv",
            "locations": "run geocode_stations first",
            "places": "build it with build_location_data --geonames",
        }
        for argument, fix in cases.items():
            with self.subTest(file=argument):
                self.assert_refused(f"{missing} not found: {fix}", **{argument: missing})

    def test_a_duplicate_address_key_stops_the_import(self) -> None:
        rows = _read_csv_rows(DIRTY_LOCATIONS)
        path = _write_csv(self.workdir / "dup.csv", rows[1:] + rows[1:2], header=rows[0])

        self.assert_refused("twice", locations=path)

    def test_an_unknown_source_stops_the_import(self) -> None:
        rows = _read_csv_rows(DIRTY_LOCATIONS)
        rows[1][LOCATION_FILE_COLUMNS.index("source")] = "guess"
        path = _write_csv(self.workdir / "source.csv", rows[1:], header=rows[0])

        self.assert_refused("unknown source 'guess'", locations=path)

    def test_an_import_with_no_station_left_is_refused(self) -> None:
        rows = [row for row in _read_csv_rows(DIRTY_CSV)[1:] if row[4] in ("AB", "ON", "AK")]
        csv_path = _write_csv(self.workdir / "canada.csv", rows)

        self.assert_refused("No station could be imported", csv_path=csv_path)


class PlaceholderImportTests(ImportTestCase):
    """TC-12: placeholder locations need --dev, and the import records it."""

    def test_a_placeholder_locations_file_is_refused_without_dev(self) -> None:
        self.run_import()
        before = self.table_snapshot()

        with self.assertRaisesMessage(CommandError, "pass --dev"):
            self.run_import(locations=PLACEHOLDER_LOCATIONS)

        self.assertEqual(self.table_snapshot(), before)

    def test_dev_mode_imports_a_placeholder_file_and_records_it(self) -> None:
        stdout, stderr = self.run_import(locations=PLACEHOLDER_LOCATIONS, dev=True)

        record = DatasetImport.objects.get()
        self.assertTrue(record.dev_mode)
        self.assertEqual(record.locations_source, "placeholder")
        self.assertEqual(Station.objects.count(), 7)
        self.assertIn("WARNING: development mode: placeholder locations are town centres", stderr)
        self.assertIn("Rebuild it with build_location_data --placeholder-locations.", stdout)
        self.assertIn("Locations source: placeholder", stdout)
        self.assertRegex(stdout, re.compile(r"^Dataset version: sha256:[0-9a-f]{64}$", re.M))

    def test_dev_mode_on_real_locations_warns_without_the_town_centre_sentence(self) -> None:
        _, stderr = self.run_import(dev=True)

        self.assertEqual(
            stderr.strip(), "WARNING: development mode: this import is marked dev_mode."
        )
        self.assertTrue(DatasetImport.objects.get().dev_mode)


class InputFileErrorTests(ImportTestCase):
    """Bad input files give one clear CommandError line naming the file."""

    def test_a_field_longer_than_the_csv_limit_is_a_clear_error(self) -> None:
        huge = "x" * 131_073
        cases = {
            "csv_path": _write_csv(
                self.workdir / "huge.csv", [["1", huge, "A", "B", "IL", "1", "3"]]
            ),
            "locations": self.copy_with(DIRTY_LOCATIONS, "huge_locations.csv", f'"{huge}"\n'),
            "places": self.copy_with(PLACES, "huge.tsv", f'"{huge}"\n'),
        }
        for argument, path in cases.items():
            with self.subTest(file=argument), self.assertRaisesMessage(CommandError, str(path)):
                self.run_import(**{argument: path})

    def test_an_empty_csv_says_it_is_empty(self) -> None:
        empty = self.workdir / "empty.csv"
        empty.write_text("", encoding="utf-8")

        with self.assertRaisesMessage(CommandError, f"{empty} is empty"):
            self.run_import(csv_path=empty)

    def test_a_folder_instead_of_a_file_says_it_is_not_a_file(self) -> None:
        with self.assertRaisesMessage(CommandError, f"{self.workdir} is not a file"):
            self.run_import(locations=self.workdir)

    def test_help_shows_the_default_paths_relative_to_the_project(self) -> None:
        parser = import_fuel_stations.Command().create_parser("manage.py", "import_fuel_stations")

        text = " ".join(parser.format_help().split())

        self.assertIn("(default: data/raw/fuel-prices.csv)", text)
        self.assertIn("(default: data/station_locations.csv)", text)
        self.assertIn("(default: data/us_places.tsv)", text)
