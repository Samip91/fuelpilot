"""Spec 01 TC-2 (gazetteer build, FR-2) and the boundary build (FR-4, NFR-11)."""

import csv
import shutil
import tempfile
import xml.etree.ElementTree as ElementTree
from io import StringIO
from pathlib import Path
from typing import TextIO

from django.core.management import call_command
from django.core.management.base import CommandError

from fuelplan.geocoding import LOCATION_FILE_COLUMNS
from fuelplan.locations import BOUNDARY_PATH, PLACES_COLUMNS, Boundary
from fuelplan.management.commands import build_location_data
from fuelplan.management.commands.build_location_data import (
    GEOCODE_OUTPUT_PATH,
    GeoNamesPlace,
    boundary_document,
    choose_places,
    read_census_states,
    write_atomically,
)

from .base import OfflineTestCase

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MINI_GEONAMES = FIXTURES / "mini_geonames.txt"
MINI_STATES = FIXTURES / "mini_states.kml"


class TempDirMixin:
    """A fresh output folder per test, removed afterwards."""

    def make_workdir(self) -> Path:
        workdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, workdir)
        return workdir


def _run_build(*args: str) -> str:
    output = StringIO()
    call_command("build_location_data", *args, stdout=output)
    return output.getvalue()


def _read_tsv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def _place(*, geonameid: int, feature_code: str = "PPL", population: int = 0) -> GeoNamesPlace:
    return GeoNamesPlace(
        geonameid=geonameid,
        name="Oakdale",
        ascii_name="Oakdale",
        alternate_names=(),
        latitude_text="45.0",
        longitude_text="-93.0",
        feature_code=feature_code,
        state="MN",
        population=population,
    )


def _kml_polygon(coordinates: str) -> str:
    return (
        "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
        f"{coordinates}"
        "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
    )


class PlacesBuildTests(TempDirMixin, OfflineTestCase):
    """`build_location_data --geonames` writes one row per (name key, state)."""

    def setUp(self) -> None:
        super().setUp()
        self.workdir = self.make_workdir()
        self.output = _run_build("--geonames", str(MINI_GEONAMES), "--out-dir", str(self.workdir))
        self.rows = _read_tsv(self.workdir / "us_places.tsv")
        self.by_key = {(row["name_norm"], row["state"]): row for row in self.rows}

    def test_build_keeps_only_populated_places_and_one_row_per_name_and_state(self) -> None:
        expected = {
            ("new york", "NY"),
            ("nyc", "NY"),
            ("big apple", "NY"),
            ("st louis", "MO"),
            ("saint louis", "MO"),
            ("san jose", "CA"),
            ("springfield", "IL"),
            ("columbus", "OH"),
            ("greenwood", "IN"),
            ("greenfield", "IN"),
            ("oakdale", "MN"),
        }

        self.assertEqual(set(self.by_key), expected)
        self.assertEqual(len(self.rows), len(expected))
        self.assertEqual(list(self.rows[0]), PLACES_COLUMNS)

    def test_a_name_twice_in_one_state_keeps_the_higher_population(self) -> None:
        row = self.by_key[("springfield", "IL")]

        self.assertEqual(
            (row["population"], row["lat"], row["lon"]), ("5000", "39.80172", "-89.64371")
        )

    def test_a_towns_own_name_beats_a_bigger_places_alternate_name(self) -> None:
        row = self.by_key[("greenfield", "IN")]

        self.assertEqual((row["name"], row["population"]), ("Greenfield", "23488"))

    def test_an_alternate_name_echoes_the_places_own_name(self) -> None:
        self.assertEqual(self.by_key[("nyc", "NY")]["name"], "New York")
        self.assertEqual(self.by_key[("san jose", "CA")]["name"], "San José")

    def test_equal_population_prefers_a_living_town_over_a_city_section(self) -> None:
        self.assertEqual(self.by_key[("oakdale", "MN")]["lat"], "44.96302")

    def test_equal_population_and_feature_rank_prefers_the_lowest_geonameid(self) -> None:
        chosen = choose_places([_place(geonameid=20), _place(geonameid=10), _place(geonameid=30)])

        self.assertEqual(chosen[("oakdale", "MN")].geonameid, 10)

    def test_non_latin_alternate_names_are_not_kept(self) -> None:
        self.assertTrue(all(row["name_norm"].isascii() for row in self.rows))

    def test_rebuilding_gives_a_byte_identical_file(self) -> None:
        first = (self.workdir / "us_places.tsv").read_bytes()

        _run_build("--geonames", str(MINI_GEONAMES), "--out-dir", str(self.workdir))

        self.assertEqual((self.workdir / "us_places.tsv").read_bytes(), first)
        self.assertFalse((self.workdir / "us_places.tsv.tmp").exists())

    def test_the_build_reports_counts_size_and_the_file_date(self) -> None:
        self.assertIn("8 populated places used, 11 name keys", self.output)
        self.assertIn("snapshot", self.output)
        self.assertIn("MB", self.output)

    def test_a_geonames_line_with_the_wrong_column_count_stops_the_build(self) -> None:
        broken = self.workdir / "broken.txt"
        broken.write_text("1\tOnly\tthree\n", encoding="utf-8")

        with self.assertRaisesMessage(CommandError, "expected 19 columns"):
            _run_build("--geonames", str(broken), "--out-dir", str(self.workdir))


class BoundaryBuildTests(TempDirMixin, OfflineTestCase):
    """`build_location_data --census-kml` keeps the lower 48 + DC as 5-dp MultiPolygons."""

    def test_build_writes_only_lower_48_and_dc_areas_as_multipolygons_at_5_dp(self) -> None:
        census = read_census_states(ElementTree.parse(MINI_STATES).getroot())

        document = boundary_document(census.states)

        features = {f["properties"]["STUSPS"]: f["geometry"] for f in document["features"]}
        self.assertEqual(census.placemark_count, 3)
        self.assertEqual(set(features), {"DC", "KS"})
        self.assertTrue(all(geometry["type"] == "MultiPolygon" for geometry in features.values()))
        self.assertEqual(len(features["KS"]["coordinates"]), 2)
        positions = [
            value
            for geometry in features.values()
            for polygon in geometry["coordinates"]
            for ring in polygon
            for position in ring
            for value in position
        ]
        self.assertTrue(all(round(value, 5) == value for value in positions))

    def test_rings_are_closed_and_points_merged_by_rounding_are_dropped(self) -> None:
        census = read_census_states(ElementTree.parse(MINI_STATES).getroot())
        shapes = {state.stusps: state for state in census.states}

        dc_ring = shapes["DC"].polygons[0][0]
        ks_second_ring = shapes["KS"].polygons[1][0]

        self.assertEqual(dc_ring[0], dc_ring[-1])
        self.assertEqual(len(dc_ring), 5)
        self.assertEqual(
            ks_second_ring,
            [[-97.12346, 37.0], [-96.0, 37.0], [-96.0, 37.5], [-97.12346, 37.5], [-97.12346, 37.0]],
        )

    def test_rings_that_collapse_at_5_dp_are_skipped_and_counted(self) -> None:
        speck = "-90.000001,40.0 -90.000002,40.000001 -90.000001,40.000002 -90.000001,40.0"
        kml = (
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><ExtendedData>'
            '<SchemaData><SimpleData name="STUSPS">IL</SimpleData></SchemaData></ExtendedData>'
            "<MultiGeometry>"
            f"{_kml_polygon('-91,40 -90,40 -90,41 -91,41 -91,40')}"
            f"{_kml_polygon(speck)}"
            "</MultiGeometry></Placemark></kml>"
        )

        census = read_census_states(ElementTree.fromstring(kml))

        self.assertEqual(census.skipped_ring_count, 1)
        self.assertEqual(len(census.states[0].polygons), 1)

    def test_a_census_file_without_all_49_areas_is_refused_and_nothing_is_written(self) -> None:
        workdir = self.make_workdir()

        with self.assertRaisesMessage(CommandError, "49 areas"):
            _run_build("--census-kml", str(MINI_STATES), "--out-dir", str(workdir))

        self.assertEqual(list(workdir.iterdir()), [])

    def test_a_file_that_is_not_xml_stops_the_build(self) -> None:
        workdir = self.make_workdir()
        broken = workdir / "broken.kml"
        broken.write_text("<kml><Placemark>", encoding="utf-8")

        with self.assertRaisesMessage(CommandError, "cannot read Census KML"):
            _run_build("--census-kml", str(broken), "--out-dir", str(workdir))

    def test_a_failing_write_leaves_no_temp_file_and_the_target_unchanged(self) -> None:
        workdir = self.make_workdir()
        target = workdir / "out.txt"
        target.write_text("old", encoding="utf-8")

        def failing_write(handle: TextIO) -> None:
            handle.write("half")
            raise OSError("disk full")

        for existing in (True, False):
            if not existing:
                target.unlink()
            with self.subTest(target_exists=existing):
                with self.assertRaisesMessage(CommandError, "disk full"):
                    write_atomically(target, failing_write)
                self.assertFalse((workdir / "out.txt.tmp").exists())
                if existing:
                    self.assertEqual(target.read_text(encoding="utf-8"), "old")
                else:
                    self.assertFalse(target.exists())

    def test_a_writer_raising_any_error_still_removes_the_temp_file(self) -> None:
        workdir = self.make_workdir()
        target = workdir / "out.txt"

        def broken_write(handle: TextIO) -> None:
            handle.write("half")
            raise ValueError("bug in the writer")

        with self.assertRaisesMessage(ValueError, "bug in the writer"):
            write_atomically(target, broken_write)

        self.assertEqual(list(workdir.iterdir()), [])

    def test_the_committed_boundary_file_holds_the_48_states_and_dc(self) -> None:
        boundary = Boundary.from_geojson(BOUNDARY_PATH)

        self.assertTrue(boundary.contains(latitude=39.0, longitude=-98.0))


class PlaceholderBuildTests(TempDirMixin, OfflineTestCase):
    """`--placeholder-locations` writes a dev-only file at each station's town point."""

    def setUp(self) -> None:
        super().setUp()
        self.workdir = self.make_workdir()
        shutil.copy(FIXTURES / "us_places_test.tsv", self.workdir / "us_places.tsv")
        self.out_path = self.workdir / "dev" / "placeholder.csv"

    def _build(self, out_path: Path) -> str:
        return _run_build(
            "--placeholder-locations",
            str(out_path),
            "--csv",
            str(FIXTURES / "dirty.csv"),
            "--out-dir",
            str(self.workdir),
        )

    def test_placeholder_rows_put_each_service_area_address_at_its_town_point(self) -> None:
        self._build(self.out_path)

        with self.out_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            rows = {row["address_key"]: row for row in reader}
        self.assertEqual(reader.fieldnames, LOCATION_FILE_COLUMNS)
        self.assertEqual(len(rows), 12)  # unique valid lower-48 + DC addresses in dirty.csv
        self.assertNotIn("1 BOW TRAIL|CALGARY|AB", rows)
        row = rows["100 MAIN ST|SPRINGFIELD|IL"]
        self.assertEqual(
            (row["lat"], row["lon"], row["result_type"], row["confidence"], row["source"]),
            ("39.80172", "-89.64371", "city", "", "placeholder"),
        )
        self.assertEqual(row["attribution"], "GeoNames")

    def test_the_placeholder_refuses_the_geocoding_output_file(self) -> None:
        with self.assertRaisesMessage(CommandError, "would treat them as geocoded"):
            self._build(GEOCODE_OUTPUT_PATH)

    def test_a_folder_given_as_the_geonames_file_says_it_is_not_a_file(self) -> None:
        with self.assertRaisesMessage(CommandError, f"{self.workdir} is not a file"):
            _run_build("--geonames", str(self.workdir), "--out-dir", str(self.workdir))

    def test_a_placeholder_csv_field_longer_than_the_csv_limit_is_a_clear_error(self) -> None:
        huge_csv = self.workdir / "huge.csv"
        huge_csv.write_bytes((FIXTURES / "dirty.csv").read_bytes() + b'"' + b"x" * 131_073 + b'"\n')

        with self.assertRaisesMessage(CommandError, f"cannot read the fuel-price CSV {huge_csv}"):
            _run_build(
                "--placeholder-locations",
                str(self.out_path),
                "--csv",
                str(huge_csv),
                "--out-dir",
                str(self.workdir),
            )
        self.assertFalse(self.out_path.exists())

    def test_help_shows_the_default_paths_relative_to_the_project(self) -> None:
        parser = build_location_data.Command().create_parser("manage.py", "build_location_data")

        text = " ".join(parser.format_help().split())

        self.assertIn("(default: data)", text)
        self.assertIn("(default: data/dev/station_locations.placeholder.csv)", text)
        self.assertIn("(default: data/raw/fuel-prices.csv)", text)

    def test_nothing_to_build_is_an_error(self) -> None:
        with self.assertRaisesMessage(CommandError, "Nothing to build"):
            _run_build()
