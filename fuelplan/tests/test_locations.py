"""Spec 01 TC-2, TC-3 and TC-4 at resolver level (FR-2, FR-3, FR-4, FR-7, NFR-11).

The HTTP halves (status codes, the error envelope, the response echo and
`meta.routing_calls`) are added with the serializer and views in builds 2B and 2C.
"""

import json
import shutil
import tempfile
import threading
from io import StringIO
from pathlib import Path

from django.core.management import call_command

from fuelplan.errors import LocationNotFound, UnsupportedLocation
from fuelplan.locations import (
    AreaPolygon,
    Boundary,
    Gazetteer,
    LazyValue,
    LocationDataError,
    LocationResolver,
    Place,
    get_boundary,
    normalise_place_name,
    polygon_from_rings,
)
from fuelplan.regions import KNOWN_REGION_CODES

from .base import OfflineTestCase

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIELDS = ("start", "finish")
SCOPE = "contiguous US + DC"

# Each point is at least 4.9 km from the Census 5m border (checked with ray casting).
BORDER_PAIRS = {
    "Detroit / Windsor": ((42.37000, -83.10000), (42.27600, -83.00000)),
    "Buffalo / Fort Erie": ((42.92000, -78.80000), (42.93000, -79.02000)),
    "San Diego / Tijuana": ((32.75000, -117.10000), (32.50000, -117.00000)),
    "El Paso / Juarez": ((31.80000, -106.40000), (31.69000, -106.42000)),
}
OTHER_US_POINTS = {
    "White House (DC)": (38.89770, -77.03650),
    "Upper Peninsula (MI, second polygon)": (46.30000, -87.40000),
}
OUTSIDE_POINTS = {
    "Honolulu": (21.30694, -157.85833),
    "Anchorage": (61.21806, -149.90028),
    "Toronto": (43.65000, -79.38000),
}


def _square(*, west: float, south: float, size: float) -> list[list[float]]:
    east, north = west + size, south + size
    return [[west, south], [east, south], [east, north], [west, north], [west, south]]


def _unused_boundary() -> Boundary:
    raise AssertionError("city input must never load the boundary")


def _unused_gazetteer() -> Gazetteer:
    raise AssertionError("coordinate input must never load the gazetteer")


class GazetteerFromBuildTests(OfflineTestCase):
    """TC-2: names written by `build_location_data` are found however they are typed."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        workdir = Path(tempfile.mkdtemp())
        cls.addClassCleanup(shutil.rmtree, workdir)
        call_command(
            "build_location_data",
            "--geonames",
            str(FIXTURES / "mini_geonames.txt"),
            "--out-dir",
            str(workdir),
            stdout=StringIO(),
        )
        cls.gazetteer = Gazetteer.from_tsv(workdir / "us_places.tsv")

    def setUp(self) -> None:
        super().setUp()
        self.resolver = LocationResolver(
            gazetteer_loader=lambda: self.gazetteer, boundary_loader=_unused_boundary
        )

    def test_city_names_match_regardless_of_case_spacing_accents_and_dots(self) -> None:
        pairs = [
            ((" new  YORK ", " ny "), ("New York", "NY")),
            (("St. Louis", "MO"), ("st louis", "mo")),
            (("San José", "CA"), ("san jose", "CA")),
            (("NYC", "NY"), ("New York", "NY")),
        ]
        for (city_a, state_a), (city_b, state_b) in pairs:
            with self.subTest(city_a=city_a, city_b=city_b):
                first = self.resolver.resolve_city(field="start", city=city_a, state=state_a)
                second = self.resolver.resolve_city(field="start", city=city_b, state=state_b)
                self.assertEqual(first, second)

    def test_a_name_twice_in_one_state_resolves_to_the_higher_population(self) -> None:
        place = self.gazetteer.find(city="Springfield", state="IL")

        self.assertEqual(place.population, 5000)

    def test_resolving_a_city_returns_the_matched_place_and_its_5_dp_point(self) -> None:
        endpoint = self.resolver.resolve_city(field="finish", city="springfield", state="il")

        self.assertEqual(endpoint.field, "finish")
        self.assertEqual(endpoint.matched, "Springfield, IL")
        self.assertEqual((endpoint.latitude, endpoint.longitude), (39.80172, -89.64371))

    def test_resolving_makes_no_query_and_no_network_call(self) -> None:
        # OfflineTestCase fails any socket connect; the resolver has no transport at all.
        with self.assertNumQueries(0):
            self.resolver.resolve_city(field="start", city="Columbus", state="OH")

    def test_an_unknown_city_in_a_valid_state_is_location_not_found(self) -> None:
        with self.assertRaises(LocationNotFound) as raised:
            self.resolver.resolve_city(field="start", city="Nowhereville", state="KS")

        error = raised.exception
        self.assertEqual((error.code, error.status), ("location_not_found", 422))
        self.assertEqual(error.details, {"field": "start", "city": "Nowhereville", "state": "KS"})
        self.assertIn("coordinates", error.message)

    def test_location_not_found_names_the_normalised_state_and_keeps_the_raw_input(self) -> None:
        with self.assertRaises(LocationNotFound) as raised:
            self.resolver.resolve_city(field="finish", city="Nowhereville", state=" ks ")

        self.assertIn(" in KS ", raised.exception.message)
        self.assertEqual(raised.exception.details["state"], " ks ")


class NormalisationTests(OfflineTestCase):
    def test_the_normaliser_drops_accents_case_dots_and_extra_spaces(self) -> None:
        self.assertEqual(
            normalise_place_name("  Coeur d'Alène  St.  Mary "), "coeur d'alene st mary"
        )


class StateCodeTests(OfflineTestCase):
    """TC-3 / TC-4 state codes: unknown codes are bad input, outside codes unsupported."""

    def setUp(self) -> None:
        super().setUp()
        self.resolver = LocationResolver(
            gazetteer_loader=lambda: Gazetteer.from_places([]), boundary_loader=_unused_boundary
        )

    def test_an_unknown_state_code_is_not_a_known_region_code(self) -> None:
        # The 2B serializer turns these into 400 before the resolver runs (§7a order).
        for code in ("XX", "", "ZZ", "USA"):
            with self.subTest(code=code):
                self.assertNotIn(code, KNOWN_REGION_CODES)
        for code in ("KS", "DC", "AK", "ON", "PR"):
            with self.subTest(code=code):
                self.assertIn(code, KNOWN_REGION_CODES)

    def test_states_and_provinces_outside_the_service_area_are_unsupported(self) -> None:
        for state in ("AK", "HI", "PR", "GU", "ON", "on", " qc "):
            for field in FIELDS:
                with self.subTest(state=state, field=field), self.assertNumQueries(0):
                    with self.assertRaises(UnsupportedLocation) as raised:
                        self.resolver.resolve_city(field=field, city="Toronto", state=state)
                    _assert_unsupported(self, raised.exception, field=field)


class BorderPairTests(OfflineTestCase):
    """TC-4 on the committed Census boundary file (a missing file fails, never skips)."""

    def setUp(self) -> None:
        super().setUp()
        self.resolver = LocationResolver(
            gazetteer_loader=_unused_gazetteer, boundary_loader=get_boundary
        )

    def test_border_pairs(self) -> None:
        for pair, (us_side, foreign_side) in BORDER_PAIRS.items():
            for field in FIELDS:
                with self.subTest(pair=pair, field=field), self.assertNumQueries(0):
                    endpoint = self.resolver.resolve_point(
                        field=field, latitude=us_side[0], longitude=us_side[1]
                    )
                    self.assertEqual((endpoint.latitude, endpoint.longitude), us_side)
                    self.assertIsNone(endpoint.matched)
                    with self.assertRaises(UnsupportedLocation) as raised:
                        self.resolver.resolve_point(
                            field=field, latitude=foreign_side[0], longitude=foreign_side[1]
                        )
                    _assert_unsupported(self, raised.exception, field=field)

    def test_points_inside_dc_and_the_upper_peninsula_are_accepted(self) -> None:
        for name, (latitude, longitude) in OTHER_US_POINTS.items():
            for field in FIELDS:
                with self.subTest(point=name, field=field):
                    endpoint = self.resolver.resolve_point(
                        field=field, latitude=latitude, longitude=longitude
                    )
                    self.assertEqual(endpoint.field, field)

    def test_points_in_hawaii_alaska_and_canada_are_unsupported(self) -> None:
        for name, (latitude, longitude) in OUTSIDE_POINTS.items():
            for field in FIELDS:
                with self.subTest(point=name, field=field), self.assertNumQueries(0):
                    with self.assertRaises(UnsupportedLocation) as raised:
                        self.resolver.resolve_point(
                            field=field, latitude=latitude, longitude=longitude
                        )
                    _assert_unsupported(self, raised.exception, field=field)


class BoundaryShapeTests(OfflineTestCase):
    def test_a_hole_in_a_polygon_counts_as_outside(self) -> None:
        polygon = polygon_from_rings(
            state="KS",
            rings=[_square(west=-100, south=38, size=2), _square(west=-99.5, south=38.5, size=1)],
        )
        boundary = Boundary([polygon])

        self.assertTrue(boundary.contains(latitude=38.2, longitude=-99.8))
        self.assertFalse(boundary.contains(latitude=39.0, longitude=-99.0))

    def test_a_point_in_any_polygon_of_a_multipolygon_is_inside(self) -> None:
        polygons: list[AreaPolygon] = [
            polygon_from_rings(state="MI", rings=[_square(west=-90, south=45, size=1)]),
            polygon_from_rings(state="MI", rings=[_square(west=-85, south=42, size=1)]),
        ]
        boundary = Boundary(polygons)

        self.assertTrue(boundary.contains(latitude=45.5, longitude=-89.5))
        self.assertTrue(boundary.contains(latitude=42.5, longitude=-84.5))
        self.assertFalse(boundary.contains(latitude=44.0, longitude=-87.0))

    def test_a_boundary_file_without_all_49_areas_is_refused(self) -> None:
        workdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, workdir)
        path = workdir / "two.geojson"
        feature = {
            "type": "Feature",
            "properties": {"STUSPS": "KS"},
            "geometry": {
                "type": "MultiPolygon",
                "coordinates": [[_square(west=-100, south=38, size=1)]],
            },
        }
        path.write_text(json.dumps({"type": "FeatureCollection", "features": [feature]}))

        with self.assertRaisesMessage(LocationDataError, "exactly the 48 states + DC"):
            Boundary.from_geojson(path)


class PlacesFileTests(OfflineTestCase):
    def test_a_places_file_with_a_bad_header_or_row_is_refused(self) -> None:
        workdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, workdir)
        header = "name\tname_norm\tstate\tlat\tlon\tpopulation\n"
        cases = {
            "wrong header": "name\tstate\n",
            "five columns": header + "A\ta\tKS\t38.0\t-98.0\n",
            "not finite": header + "A\ta\tKS\tnan\t-98.0\t10\n",
            "bad population": header + "A\ta\tKS\t38.0\t-98.0\t-1\n",
            "state not served": header + "A\ta\tAK\t61.0\t-150.0\t10\n",
            "duplicate key": header + "A\ta\tKS\t38.0\t-98.0\t1\nA\ta\tKS\t38.1\t-98.0\t2\n",
        }
        for name, content in cases.items():
            path = workdir / f"{name}.tsv"
            path.write_text(content, encoding="utf-8")
            with self.subTest(case=name), self.assertRaises(LocationDataError):
                Gazetteer.from_tsv(path)

    def test_places_sharing_one_geonames_row_share_one_object(self) -> None:
        workdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, workdir)
        path = workdir / "places.tsv"
        path.write_text(
            "name\tname_norm\tstate\tlat\tlon\tpopulation\n"
            "New York\tnew york\tNY\t40.71427\t-74.00597\t8804190\n"
            "New York\tnyc\tNY\t40.71427\t-74.00597\t8804190\n",
            encoding="utf-8",
        )

        gazetteer = Gazetteer.from_tsv(path)

        self.assertIs(
            gazetteer.find(city="nyc", state="NY"), gazetteer.find(city="New York", state="NY")
        )


class LazyValueTests(OfflineTestCase):
    def test_the_value_is_built_once_even_with_several_threads(self) -> None:
        calls: list[int] = []
        lazy = LazyValue(lambda: calls.append(1) or Place("A", "KS", 38.0, -98.0, 1))
        threads = [threading.Thread(target=lazy.get) for _ in range(8)]

        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(calls), 1)
        self.assertIs(lazy.get(), lazy.get())


def _assert_unsupported(test: OfflineTestCase, error: UnsupportedLocation, *, field: str) -> None:
    test.assertEqual((error.code, error.status), ("unsupported_location", 422))
    test.assertIn(SCOPE, error.message)
    test.assertEqual(error.details, {"field": field, "scope": SCOPE})
