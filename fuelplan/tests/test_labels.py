"""Spec 01 TC-13 (labelling part, FR-17): raw geocoding results become location labels.

The fetch part of TC-13 is proven in `test_geocode_stations.py`
(`test_each_unique_service_area_address_is_sent_once`,
`test_resume_sends_no_request_for_addresses_already_saved`,
`test_the_api_key_never_appears_in_errors_tracebacks_or_logs`).
"""

import ast
from decimal import Decimal
from pathlib import Path

from fuelplan.locations import Place
from fuelplan.management.commands.import_fuel_stations import (
    CONFIDENCE_THRESHOLD,
    FAR_FROM_CITY_KM,
    IMPORT_RULES_VERSION,
    RawLocation,
    label_location,
)
from fuelplan.models import LocationQuality

from .base import OfflineTestCase

SPRINGFIELD = Place(
    name="Springfield", state="IL", latitude=39.80172, longitude=-89.64371, population=114394
)
NEAR = ("39.81000", "-89.65000")  # 1.07 km from SPRINGFIELD
AT_25_KM = ("40.02655", "-89.64371")  # 24.99999 km raw, 25.00 after rounding: kept as near
AT_25_2_KM = ("40.02835", "-89.64371")
AT_25_003_KM = ("40.02658", "-89.64371")  # 25.00333 km raw → 25.00: near
AT_25_006_KM = ("40.02660", "-89.64371")  # 25.00555 km raw → 25.01 (HALF_UP): far
AT_30_KM = ("40.07152", "-89.64371")
X = CONFIDENCE_THRESHOLD
STEP = Decimal("0.01")
POI = LocationQuality.POI_MATCH
ADDRESS = LocationQuality.ADDRESS_MATCH
APPROXIMATE = LocationQuality.APPROXIMATE
FUELPLAN_DIR = Path(__file__).resolve().parent.parent


def _raw(
    point: tuple[str, str],
    *,
    result_type: str = "amenity",
    confidence: str = "",
    state_code: str = "IL",
) -> RawLocation:
    return RawLocation(
        address_key="1 MAIN ST|SPRINGFIELD|IL",
        lat=point[0],
        lon=point[1],
        result_type=result_type,
        confidence=confidence,
        state_code=state_code,
        source="geoapify",
    )


def _label(raw: RawLocation, *, city_place: Place | None = SPRINGFIELD) -> str | None:
    labelled = label_location(raw=raw, csv_state="IL", city_place=city_place)
    return None if labelled is None else labelled.quality


class LocationLabelTests(OfflineTestCase):
    """`label_location` follows the FR-17 rule exactly, boundaries included."""

    def test_geocode_and_labels_by_result_type_near_the_town(self) -> None:
        cases = [
            ("amenity", POI),
            ("building", ADDRESS),
            ("street", ADDRESS),
            ("city", APPROXIMATE),
            ("postcode", APPROXIMATE),
            ("suburb", APPROXIMATE),
            ("unknown", APPROXIMATE),
            ("", APPROXIMATE),
        ]
        for result_type, expected in cases:
            with self.subTest(result_type=result_type):
                self.assertEqual(_label(_raw(NEAR, result_type=result_type)), expected)

    def test_a_result_without_a_usable_point_is_unresolved(self) -> None:
        points = [("", ""), ("abc", "-89.6"), ("nan", "-89.6"), ("inf", "-89.6")]
        outside_box = [("21.30694", "-157.85833"), ("61.21806", "-149.90028")]
        for point in points + outside_box:
            with self.subTest(point=point):
                self.assertIsNone(_label(_raw(point)))

    def test_a_state_code_that_differs_from_the_csv_state_is_unresolved(self) -> None:
        for state_code in ("MO", "", "ILL"):
            with self.subTest(state_code=state_code):
                self.assertIsNone(_label(_raw(NEAR, state_code=state_code)))

    def test_the_state_code_is_compared_case_insensitively(self) -> None:
        self.assertEqual(_label(_raw(NEAR, state_code=" il ")), POI)

    def test_a_match_25_km_from_the_town_keeps_its_label_at_any_confidence(self) -> None:
        self.assertEqual(FAR_FROM_CITY_KM, Decimal("25.00"))
        for result_type, expected in (("amenity", POI), ("building", ADDRESS)):
            with self.subTest(result_type=result_type):
                raw = _raw(AT_25_KM, result_type=result_type, confidence=str(X - STEP))
                self.assertEqual(_label(raw), expected)

    def test_the_distance_is_rounded_to_0_01_km_half_up_before_the_25_km_check(self) -> None:
        cases = [(AT_25_003_KM, POI), (AT_25_006_KM, APPROXIMATE)]
        for point, expected in cases:
            with self.subTest(point=point):
                self.assertEqual(_label(_raw(point, confidence=str(X - STEP))), expected)

    def test_a_far_match_keeps_its_label_only_with_confidence_of_at_least_x(self) -> None:
        cases = [
            (AT_25_2_KM, "amenity", X - STEP, APPROXIMATE),
            (AT_30_KM, "amenity", X, POI),
            (AT_30_KM, "amenity", X - STEP, APPROXIMATE),
            (AT_30_KM, "building", X - STEP, APPROXIMATE),
            (AT_30_KM, "street", X, ADDRESS),
            (AT_30_KM, "city", X, APPROXIMATE),
        ]
        if X + STEP <= 1:
            cases.append((AT_30_KM, "amenity", X + STEP, POI))
        for point, result_type, confidence, expected in cases:
            with self.subTest(point=point, result_type=result_type, confidence=confidence):
                raw = _raw(point, result_type=result_type, confidence=str(confidence))
                self.assertEqual(_label(raw), expected)

    def test_a_blank_or_bad_confidence_counts_as_below_x(self) -> None:
        for confidence in ("", "abc", "NaN", "Infinity"):
            with self.subTest(confidence=confidence):
                self.assertEqual(_label(_raw(AT_30_KM, confidence=confidence)), APPROXIMATE)

    def test_a_town_missing_from_the_gazetteer_counts_as_far(self) -> None:
        cases = [(X - STEP, APPROXIMATE), (X, POI), (None, APPROXIMATE)]
        for confidence, expected in cases:
            with self.subTest(confidence=confidence):
                raw = _raw(NEAR, confidence="" if confidence is None else str(confidence))
                self.assertEqual(_label(raw, city_place=None), expected)

    def test_a_labelled_point_keeps_the_geocoded_coordinates(self) -> None:
        labelled = label_location(raw=_raw(NEAR), csv_state="IL", city_place=SPRINGFIELD)

        self.assertEqual((labelled.latitude, labelled.longitude), (39.81, -89.65))

    def test_import_rules_version_names_the_threshold_and_the_far_distance(self) -> None:
        self.assertIn(f"X={CONFIDENCE_THRESHOLD}", IMPORT_RULES_VERSION)
        self.assertIn("far_km=25.00", IMPORT_RULES_VERSION)
        self.assertIn("0.01km-half-up", IMPORT_RULES_VERSION)


class GeocodingIsolationTests(OfflineTestCase):
    """FR-17: Geoapify is called only by the owner command, never by the import or requests."""

    def test_geocoding_client_and_transport_are_used_only_by_the_owner_command(self) -> None:
        checked = [*FUELPLAN_DIR.glob("*.py"), *FUELPLAN_DIR.glob("management/commands/*.py")]
        allowed = {"geocoding.py", "http.py", "geocode_stations.py"}
        for path in checked:
            if path.name in allowed:
                continue
            with self.subTest(module=path.name):
                names = _imported_names(path)
                self.assertNotIn("GeoapifyBatchClient", names)
                self.assertNotIn("fuelplan.http", names)
                self.assertNotIn(".http", names)
                self.assertNotIn("HttpClientTransport", names)


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add("." * node.level + (node.module or ""))
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names
